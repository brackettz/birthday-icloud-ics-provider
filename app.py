#!/usr/bin/env python3
"""iCloud Birthday ICS Provider — exposes iCloud contact birthdays as a subscribable .ics calendar."""

from __future__ import annotations

import hashlib
import logging
import os
import re
import threading
import time
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone
from urllib.parse import urlparse, urlunparse

import requests
from flask import Flask, Response, abort
from icalendar import Calendar, Event
from requests.auth import HTTPBasicAuth

ICLOUD_USERNAME = os.environ.get("ICLOUD_USERNAME", "")
ICLOUD_PASSWORD = os.environ.get("ICLOUD_PASSWORD", "")
SECRET_TOKEN = os.environ.get("SECRET_TOKEN", "")
CACHE_TTL = int(os.environ.get("CACHE_TTL_SECONDS", "3600"))
PORT = int(os.environ.get("PORT", "5000"))

_D = "{DAV:}"
_C = "{urn:ietf:params:xml:ns:carddav}"
_CARDDAV_NS = "urn:ietf:params:xml:ns:carddav"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

app = Flask(__name__)

_lock = threading.Lock()
_cache: dict = {"ics": None, "at": 0.0}


# ---------------------------------------------------------------------------
# CardDAV client
# ---------------------------------------------------------------------------

def _abs(resp: requests.Response, href: str) -> str:
    if href.startswith("http"):
        return href
    p = urlparse(resp.url)
    return urlunparse((p.scheme, p.netloc, href, "", "", ""))


def _propfind(url: str, auth, depth: int, body: str) -> requests.Response:
    r = requests.request(
        "PROPFIND", url, auth=auth,
        headers={"Depth": str(depth), "Content-Type": "application/xml; charset=utf-8"},
        data=body.encode(),
        allow_redirects=True,
        timeout=30,
    )
    r.raise_for_status()
    return r


def _find_href(root: ET.Element, tag: str) -> str | None:
    for el in root.iter(tag):
        h = el.find(f"{_D}href")
        if h is not None and h.text:
            return h.text
    return None


def _iter_addressbooks(username: str, password: str):
    """Yield (auth, addressbook_url) for every CardDAV addressbook on iCloud."""
    auth = HTTPBasicAuth(username, password)

    _MULTI_BODY = (
        '<?xml version="1.0" encoding="utf-8"?>'
        f'<D:propfind xmlns:D="DAV:" xmlns:C="{_CARDDAV_NS}"><D:prop>'
        '<D:current-user-principal/>'
        '<D:principal-URL/>'
        '<C:addressbook-home-set/>'
        '</D:prop></D:propfind>'
    )

    # The well-known URL redirects (301/302). PROPFIND with allow_redirects=True
    # silently downgrades to GET after the redirect, so we follow manually.
    probe = requests.request(
        "PROPFIND", "https://contacts.icloud.com/.well-known/carddav",
        auth=auth,
        headers={"Depth": "0", "Content-Type": "application/xml; charset=utf-8"},
        data=_MULTI_BODY.encode(),
        allow_redirects=False,
        timeout=30,
    )
    if probe.status_code in (301, 302, 307, 308):
        carddav_root = probe.headers["Location"]
    elif probe.status_code == 207:
        carddav_root = probe.url
    else:
        probe.raise_for_status()
        carddav_root = probe.url

    log.info(f"CardDAV root: {carddav_root}")

    r1 = _propfind(carddav_root, auth, 0, _MULTI_BODY)
    root1 = ET.fromstring(r1.content)
    log.debug(f"CardDAV root response:\n{r1.text}")

    # Prefer addressbook-home-set directly; fall back to principal discovery
    home_href = _find_href(root1, f"{_C}addressbook-home-set")
    if home_href:
        home_url = _abs(r1, home_href)
        log.info(f"Found addressbook-home-set directly: {home_url}")
    else:
        principal_href = (
            _find_href(root1, f"{_D}current-user-principal")
            or _find_href(root1, f"{_D}principal-URL")
        )
        if principal_href:
            principal_url = _abs(r1, principal_href)
            log.info(f"Following principal: {principal_url}")
            r2 = _propfind(
                principal_url, auth, 0,
                f'<?xml version="1.0" encoding="utf-8"?>'
                f'<D:propfind xmlns:D="DAV:" xmlns:C="{_CARDDAV_NS}">'
                f'<D:prop><C:addressbook-home-set/></D:prop></D:propfind>',
            )
            root2 = ET.fromstring(r2.content)
            home_href = _find_href(root2, f"{_C}addressbook-home-set")
            if not home_href:
                raise RuntimeError(f"No addressbook-home-set found at {principal_url}")
            home_url = _abs(r2, home_href)
        else:
            # Last resort: treat carddav root itself as the addressbook home
            log.warning(
                f"Could not find principal or addressbook-home-set in response — "
                f"trying carddav root as home.\nResponse XML: {r1.text[:800]}"
            )
            home_url = carddav_root

    r3 = _propfind(
        home_url, auth, 1,
        f'<?xml version="1.0" encoding="utf-8"?>'
        f'<D:propfind xmlns:D="DAV:" xmlns:C="{_CARDDAV_NS}">'
        f'<D:prop><D:resourcetype/><D:displayname/></D:prop></D:propfind>',
    )
    root3 = ET.fromstring(r3.content)
    for resp_el in root3.iter(f"{_D}response"):
        href_el = resp_el.find(f"{_D}href")
        if href_el is None:
            continue
        has_ab = any(
            rt.find(f"{_C}addressbook") is not None
            for rt in resp_el.iter(f"{_D}resourcetype")
        )
        if has_ab:
            yield auth, _abs(r3, href_el.text)


def _fetch_vcards(auth, addressbook_url: str) -> list[str]:
    r = requests.request(
        "REPORT", addressbook_url, auth=auth,
        headers={"Depth": "1", "Content-Type": "application/xml; charset=utf-8"},
        data=(
            f'<?xml version="1.0" encoding="utf-8"?>'
            f'<C:addressbook-query xmlns:D="DAV:" xmlns:C="{_CARDDAV_NS}">'
            f'<D:prop><C:address-data/></D:prop>'
            f'</C:addressbook-query>'
        ).encode(),
        allow_redirects=True,
        timeout=60,
    )
    r.raise_for_status()
    root = ET.fromstring(r.content)
    return [
        el.text
        for el in root.iter(f"{_C}address-data")
        if el.text and "BDAY" in el.text
    ]


# ---------------------------------------------------------------------------
# vCard birthday parsing
# ---------------------------------------------------------------------------

def _parse_vcards(raw_vcards: list[str]) -> list[dict]:
    results = []
    for text in raw_vcards:
        # Unfold RFC 6350 line continuations
        text = re.sub(r"\r?\n[ \t]", "", text)

        name = None
        bday_date = None
        has_year = True

        for line in text.splitlines():
            if ":" not in line:
                continue
            prop, _, val = line.partition(":")
            prop_upper = prop.upper()
            val = val.strip()

            if prop_upper == "FN" or prop_upper.startswith("FN;"):
                name = val
            elif prop_upper == "BDAY" or prop_upper.startswith("BDAY;"):
                omit_year = "X-APPLE-OMIT-YEAR" in prop_upper
                if val.startswith("--"):
                    # Format: --MMDD or --MM-DD (no year)
                    digits = val[2:].replace("-", "")
                    try:
                        bday_date = date(1900, int(digits[:2]), int(digits[2:4]))
                        has_year = False
                    except (ValueError, IndexError):
                        pass
                else:
                    digits = val.replace("-", "")
                    if len(digits) == 8:
                        try:
                            y, m, d = int(digits[:4]), int(digits[4:6]), int(digits[6:])
                            if omit_year or y < 1900:
                                bday_date = date(1900, m, d)
                                has_year = False
                            else:
                                bday_date = date(y, m, d)
                        except ValueError:
                            pass

        if name and bday_date:
            results.append({"name": name, "date": bday_date, "has_year": has_year})

    return results


# ---------------------------------------------------------------------------
# ICS generation
# ---------------------------------------------------------------------------

def _build_ics(birthdays: list[dict]) -> bytes:
    cal = Calendar()
    cal.add("prodid", "-//birthday-icloud-ics//EN")
    cal.add("version", "2.0")
    cal.add("x-wr-calname", "Geburtstage")
    cal.add("calscale", "GREGORIAN")
    cal.add("method", "PUBLISH")

    now = datetime.now(timezone.utc)

    for b in sorted(birthdays, key=lambda x: (x["date"].month, x["date"].day)):
        ev = Event()
        name = b["name"]
        bday: date = b["date"]

        if b["has_year"] and bday.year > 1900:
            ev.add("summary", f"Geburtstag {name} (*{bday.year})")
        else:
            ev.add("summary", f"Geburtstag {name}")

        ev.add("dtstart", bday)
        ev.add("dtend", bday + timedelta(days=1))
        ev.add("rrule", {"freq": "yearly"})
        ev.add("transp", "TRANSPARENT")
        ev.add("dtstamp", now)

        uid = hashlib.md5(f"{name}-{bday.month:02d}-{bday.day:02d}".encode()).hexdigest()
        ev.add("uid", f"{uid}@birthday-icloud-ics")

        cal.add_component(ev)

    return cal.to_ical()


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------

def refresh() -> bytes:
    log.info("Refreshing birthday cache from iCloud...")
    all_vcards: list[str] = []
    book_count = 0

    for auth, book_url in _iter_addressbooks(ICLOUD_USERNAME, ICLOUD_PASSWORD):
        book_count += 1
        vcards = _fetch_vcards(auth, book_url)
        all_vcards.extend(vcards)
        log.info(f"  {book_url}: {len(vcards)} contacts with BDAY")

    log.info(f"Total: {len(all_vcards)} contacts with birthdays across {book_count} addressbook(s)")
    birthdays = _parse_vcards(all_vcards)
    ics = _build_ics(birthdays)

    with _lock:
        _cache["ics"] = ics
        _cache["at"] = time.monotonic()

    log.info(f"Cache updated: {len(birthdays)} birthdays")
    return ics


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/birthdays.ics")
@app.route("/birthdays/<token>.ics")
def serve_ics(token: str | None = None):
    if SECRET_TOKEN and token != SECRET_TOKEN:
        abort(404)

    with _lock:
        cached_ics = _cache["ics"]
        age = time.monotonic() - _cache["at"]

    if cached_ics is None or age > CACHE_TTL:
        try:
            cached_ics = refresh()
        except Exception:
            log.exception("Cache refresh failed")
            if cached_ics is None:
                abort(503)
            log.warning("Serving stale cache due to refresh error")

    return Response(
        cached_ics,
        mimetype="text/calendar; charset=utf-8",
        headers={"Content-Disposition": 'inline; filename="birthdays.ics"'},
    )


@app.route("/health")
def health():
    with _lock:
        age = int(time.monotonic() - _cache["at"])
        has_cache = _cache["ics"] is not None
    return {"status": "ok", "cache_age_seconds": age, "has_cache": has_cache}


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    if not ICLOUD_USERNAME or not ICLOUD_PASSWORD:
        raise SystemExit("ICLOUD_USERNAME and ICLOUD_PASSWORD must be set")

    try:
        refresh()
    except Exception:
        log.exception("Initial cache warm-up failed — will retry on first request")

    app.run(host="0.0.0.0", port=PORT)
