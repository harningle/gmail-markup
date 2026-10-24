# gmail-markup

Manually add flight/hotel reservations from Gmail to Google Calendar using Markup

Some booking emails (booking.com) carry [schema.org markup](https://developers.google.com/workspace/gmail/markup/reference), so Gmail adds them to Google Calendar by itself. Others (easyJet) don't. Gmail ignores markup from unregistered senders, **except in emails you send to yourself**. This project sends those emails for you: ask Claude to add a booking, Claude reads the confirmation email and calls an MCP tool, and the tool emails the markup to you.

```
claude.ai --Gmail connector--> reads the booking email
    \--custom connector--> https://<your-domain>/gmail-markup/<secret>/mcp   (VM: nginx -> 127.0.0.1:8002)
            MCP tools validate the booking and render JSON-LD + a short HTML summary
            --POST {token, subject, htmlBody}--> Apps Script web app (runs as you) --MailApp--> you
                                                                           Gmail --> Calendar
```

Apps Script does the sending because it already runs as you inside Google. The VM holds no Gmail credentials, only a token whose worst case is "email some markup to myself".

## MCP tools

`add_flight`, `add_train`, `add_bus`, `add_hotel`, `add_event`, defined in `src/gmail_markup/server.py`. Inputs are typed pydantic models (`src/gmail_markup/markup.py`), so mistakes that would make Gmail silently ignore the markup are rejected before anything is sent: times without a UTC offset, arrival before departure, malformed IATA codes. Flights, trains and buses produce one reservation per passenger per leg, as Google expects. Sending the same reservation number again updates the existing calendar event.

Only flights are sure to come with a booking reference. For the other types the reference is optional: without one, the tool generates `NOREF-` plus 8 hex digits, so re-adding the same booking yields the same reference and updates the event instead of duplicating it. The reference is derived only from what the LLM extracts consistently between runs:

| Tool | Reference derived from |
| --- | --- |
| `add_bus`, `add_train` | each leg's departure and arrival times (not stop, station or company names, which may be worded differently) |
| `add_hotel` | check-in and check-out dates (times are often missing from the booking and guessed) |
| `add_event` | start time (the end is often missing from tickets) |

When a booking changes, the new details would give a new reference, so Claude has to reuse the old one. It finds it in the subject of the earlier "... reservation NOREF-..." email. Gmail then updates the existing event in place: this was tested by moving a bus from 10:00 to 14:00.

## Configuration

`.env` at the repo root (gitignored), read by the systemd unit:

| Variable | Notes |
| --- | --- |
| `APPS_SCRIPT_URL` | `/exec` URL of the "gmail-markup MCP" web app deployment |
| `APPS_SCRIPT_TOKEN` | must equal the `API_TOKEN` script property of the Apps Script project |
| `MCP_PATH_SECRET` | random path segment in the connector URL; it is the connector's only credential |
| `PUBLIC_HOST` | the domain the connector URL uses, e.g. `example.org`; requests with any other Host header (except localhost) are rejected |

The server refuses to start if any of them is missing.

## Apps Script (`apps-script/`)

An Apps Script project owned by the Gmail account whose calendar should get the events, synced with [clasp](https://github.com/google/clasp). `doPost` in `code.js` checks the token and emails `htmlBody` to the script owner. `testSchemas()` and the `*.html` templates are hand-written samples (with made-up data) for manual tests. Keep every file in the folder, because `clasp push` deletes remote files that are missing locally.

Set-up: create an Apps Script project, copy `.clasp.json.example` to `.clasp.json` (gitignored) and fill in its script ID, then push and deploy. Set the `API_TOKEN` script property in the editor (Project Settings, Script properties) to a long random string, and put the same value in `.env` as `APPS_SCRIPT_TOKEN`.

```bash
clasp push --force   # --force: without it clasp skips the push when the manifest changed
clasp create-deployment --description "gmail-markup MCP"   # first time only
clasp list-deployments   # find the "gmail-markup MCP" deployment ID
clasp update-deployment <deployment-id> --description "gmail-markup MCP"   # keeps the /exec URL
```

The web app is deployed as "execute as me, anyone can access" (`appsscript.json`). The token check in `doPost` is what keeps it closed. With no `API_TOKEN` property set, it rejects every request.

## Local development

```bash
export VIRTUAL_ENV=~/.virtualenvs/gmail-markup   # then activate it
uv sync --active
pytest
ruff check src tests
```

## Deploy on a VM

An Ubuntu VM with nginx and a TLS certificate (e.g. certbot) for your domain, user `ubuntu`. The server listens on 127.0.0.1:8002; change `PORT` in `server.py` and `deploy/nginx-location.conf` if that is taken. Python 3.15 needs uv 0.13 or newer (`~/.local/bin/uv self update`).

First time:

```bash
git clone https://github.com/harningle/gmail-markup.git ~/gmail-markup
cd ~/gmail-markup
UV_PROJECT_ENVIRONMENT=~/.virtualenvs/gmail-markup ~/.local/bin/uv sync --frozen --no-dev
# copy .env into ~/gmail-markup, then:
chmod 600 .env
sudo cp deploy/gmail-markup.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now gmail-markup
# paste deploy/nginx-location.conf into the 443 server block of your site's nginx config
sudo nginx -t && sudo systemctl reload nginx
```

Code changes:

```bash
cd ~/gmail-markup && git pull
UV_PROJECT_ENVIRONMENT=~/.virtualenvs/gmail-markup ~/.local/bin/uv sync --frozen --no-dev   # only if dependencies changed
sudo systemctl restart gmail-markup
journalctl -u gmail-markup -n 100 --no-pager
```

Then in claude.ai: Settings, Connectors, Add custom connector, URL `https://<PUBLIC_HOST>/gmail-markup/<MCP_PATH_SECRET>/mcp`, no authentication. The access log records that URL, secret included, so treat the logs as sensitive.

## Troubleshooting

| Symptom | Likely cause |
| --- | --- |
| Tool error "Apps Script failed: unauthorised" | `APPS_SCRIPT_TOKEN` in `.env` differs from the `API_TOKEN` script property |
| Tool error "Apps Script returned non-JSON" | `doPost` threw, e.g. the MailApp daily quota (100 emails) is used up; see Executions in the Apps Script editor |
| Email arrives but no calendar event | Gmail's "smart features" / Calendar's "Events from Gmail" setting is off, or the sender and recipient are not the same account |
| claude.ai cannot connect, nothing in `journalctl` | a CDN or firewall in front of nginx (e.g. Cloudflare Bot Fight Mode) is blocking Anthropic's requests |
| 421 "Invalid Host header" | the request's Host is not `PUBLIC_HOST` |
