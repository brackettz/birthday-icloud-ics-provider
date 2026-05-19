# birthday-icloud-ics-provider

Fetches birthdays from iCloud contacts via CardDAV and serves them as a subscribable `.ics` calendar feed.

## How it works

On each request (or after the cache TTL expires), the server connects to iCloud via CardDAV, collects all contacts with a `BDAY` field, and returns a calendar with yearly recurring all-day events.

## Setup

### 1. App-specific password

Create one at [appleid.apple.com](https://appleid.apple.com) → Sign-In and Security → App-Specific Passwords. Do **not** use your regular Apple ID password.

### 2. Configure

```bash
cp .env.example .env
$EDITOR .env
```

| Variable | Required | Default | Description |
|---|---|---|---|
| `ICLOUD_USERNAME` | yes | — | Your Apple ID (email) |
| `ICLOUD_PASSWORD` | yes | — | App-specific password |
| `SECRET_TOKEN` | no | — | If set, required in the URL |
| `CACHE_TTL_SECONDS` | no | `3600` | How long to cache the feed |
| `PORT` | no | `5000` | Listening port |

### 3. Run

**Docker (recommended):**
```bash
docker compose up -d
```

**Directly (Debian/Ubuntu):**
```bash
pip install -r requirements.txt
gunicorn --bind 0.0.0.0:5000 --workers 2 app:app
```

**systemd service:**
```ini
[Unit]
After=network.target

[Service]
WorkingDirectory=/opt/birthday-ics
EnvironmentFile=/opt/birthday-ics/.env
ExecStart=gunicorn --bind 0.0.0.0:5000 --workers 2 app:app
Restart=always

[Install]
WantedBy=multi-user.target
```

## Endpoints

| Endpoint | Description |
|---|---|
| `GET /birthdays.ics` | Calendar feed (no token protection) |
| `GET /birthdays/<token>.ics` | Calendar feed protected by `SECRET_TOKEN` |
| `GET /health` | Status check |

## Subscribe

Add the URL to any calendar app that supports webcal subscriptions:

```
https://your-server/birthdays/YOUR_SECRET_TOKEN.ics
```

On iOS: Settings → Calendar → Accounts → Add Account → Other → Add Subscribed Calendar.

## Event format

- All-day event, repeats yearly
- Summary: `Geburtstag Name (*1990)` (birth year shown if known)
- Transparent (does not block time)
