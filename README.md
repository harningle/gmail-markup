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

## Set-up from scratch

### Prerequisites

- A Google account. Its Gmail receives the bookings and its Calendar gets the events. Gmail's smart features and Calendar's "Events from Gmail" setting must be on.
- A claude.ai plan with custom connectors, with Claude's Gmail connector linked to the same Google account.
- A Linux VM (these steps assume Ubuntu with user `ubuntu`) with a domain pointing at it, nginx, and a TLS certificate (e.g. from certbot).
- On your own machine: git, Node.js (for clasp) and Python 3.15 through [uv](https://docs.astral.sh/uv/) 0.13 or newer.

### 1. Clone the repo

```bash
git clone https://github.com/harningle/gmail-markup.git
cd gmail-markup
```

### 2. Deploy the Apps Script web app

1. Turn on the Google Apps Script API at <https://script.google.com/home/usersettings>.
2. Install clasp and log in as the Google account from the prerequisites:
   ```bash
   npm install -g @google/clasp
   clasp login
   ```
3. Create an empty project at <https://script.google.com> (New project) and copy its script ID from Project Settings, IDs.
4. Point clasp at it: `cp .clasp.json.example .clasp.json` (gitignored) and replace `<your Apps Script project ID>` with that ID.
5. Push the code and create the web app deployment:
   ```bash
   clasp push --force   # --force: without it clasp skips the push when the manifest changed
   clasp create-deployment --description "gmail-markup MCP"
   ```
   The second command prints a deployment ID. The web app URL is `https://script.google.com/macros/s/<deployment-id>/exec`.
6. Grant the web app permission to send email as you. In the Apps Script editor, open `code.gs`, pick `doPost` in the function list and click Run. Accept the authorisation prompt; for your own script, "Google hasn't verified this app" is expected, so choose Advanced, then go to the project. The run then fails with an error about `postData`, because there is no request. That's fine: only the permission grant matters.
7. Create the shared token and store it as a script property. In Project Settings, Script properties, add `API_TOKEN` with the output of:
   ```bash
   python -c "import secrets; print(secrets.token_urlsafe(32))"
   ```
8. Check that the web app is reachable and closed:
   ```bash
   curl -sL -H 'Content-Type: application/json' --data '{"token":"wrong"}' https://script.google.com/macros/s/<deployment-id>/exec
   # {"ok":false,"error":"unauthorised"}
   ```

### 3. Write `.env`

Create `.env` at the repo root (gitignored; see [Configuration](#configuration)):

```
APPS_SCRIPT_URL=https://script.google.com/macros/s/<deployment-id>/exec
APPS_SCRIPT_TOKEN=<the API_TOKEN value from step 2.7>
MCP_PATH_SECRET=<another output of the secrets command above>
PUBLIC_HOST=<your domain, e.g. example.org>
```

Optionally, run the tests now (see [Local development](#local-development)).

### 4. Run the MCP server on the VM

On the VM:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh   # installs uv to ~/.local/bin, if not there yet
git clone https://github.com/harningle/gmail-markup.git ~/gmail-markup
cd ~/gmail-markup
UV_PROJECT_ENVIRONMENT=~/.virtualenvs/gmail-markup ~/.local/bin/uv sync --frozen --no-dev
```

Copy `.env` from your machine (e.g. `scp .env ubuntu@<vm>:gmail-markup/.env`), then on the VM:

```bash
chmod 600 ~/gmail-markup/.env
sudo cp ~/gmail-markup/deploy/gmail-markup.service /etc/systemd/system/   # edit the user and paths first if not ubuntu
sudo systemctl daemon-reload && sudo systemctl enable --now gmail-markup
systemctl is-active gmail-markup   # active
```

Then route the path through nginx: paste `deploy/nginx-location.conf` into the `listen 443` server block of your site's config (e.g. `/etc/nginx/sites-available/<site>`), and reload:

```bash
sudo nginx -t && sudo systemctl reload nginx
curl -s -o /dev/null -w '%{http_code}\n' https://<your-domain>/gmail-markup/wrong/mcp   # 404: nginx reaches the server, wrong secret
```

The server listens on 127.0.0.1:8002. If that port is taken, change `PORT` in `src/gmail_markup/server.py` and `deploy/nginx-location.conf`.

### 5. Add the connector to claude.ai

In claude.ai, Settings, Connectors, Add custom connector: URL `https://<PUBLIC_HOST>/gmail-markup/<MCP_PATH_SECRET>/mcp`, no authentication. The secret path is the connector's only credential, and the access logs record it, so treat them as sensitive.

### 6. Try it

In a chat with both Gmail and gmail-markup turned on, ask for example:

> Find my latest easyJet booking confirmation in Gmail and add the flights to my calendar using gmail-markup.

An email from yourself titled e.g. "Flight reservation ABC123" arrives, and the event appears in Google Calendar about 20 seconds later.

## MCP tools

`add_flight`, `add_train`, `add_bus`, `add_hotel`, `add_event`, defined in `src/gmail_markup/server.py`. Inputs are typed pydantic models (`src/gmail_markup/markup.py`), so mistakes that would make Gmail silently ignore the markup are rejected before anything is sent: blank text, times without a UTC offset, arrival before departure, malformed IATA codes. Flights, trains and buses produce one reservation per passenger per leg, as Google expects. Sending the same reservation number again updates the existing calendar event.

Only flights are sure to come with a booking reference. For the other types the reference is optional: without one, the tool generates `NOREF-` plus 8 hex digits from the booking's identity, so re-adding the same booking yields the same reference and updates the event instead of duplicating it. Names in the identity are reduced to lowercase ASCII letters and digits, so case, punctuation and accents don't matter:

| Tool | Reference derived from |
| --- | --- |
| `add_bus`, `add_train` | each leg's departure and arrival times. Stop, station and company names are left out because they may be worded differently between runs, and two different trips with identical times can't both be taken |
| `add_hotel` | check-in and check-out dates (times are often missing and guessed), postcode and hotel name, so two stays on the same dates don't overwrite each other |
| `add_event` | start time (the end is often missing from tickets) and event name, so two events starting together don't overwrite each other |

If Claude words a hotel or event name differently on a later run, you get a duplicate event, which you can see and delete, rather than a booking silently overwriting another. When a booking changes, the new details would give a new reference, so Claude has to reuse the old one. It finds it in the subject of the earlier "... reservation NOREF-..." email. Gmail then updates the existing event in place: this was tested by moving a bus from 10:00 to 14:00.

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

`doPost` in `code.js` checks the token and emails `htmlBody` to the script owner. It is deployed as "execute as me, anyone can access" (`appsscript.json`); the token check is what keeps it closed, and with no `API_TOKEN` property set it rejects every request. `testSchemas()` and the `*.html` templates are hand-written samples (with made-up data) for manual tests. Keep every file in the folder, because `clasp push` deletes remote files that are missing locally.

## Updating

Apps Script, from your machine:

```bash
clasp push --force
clasp list-deployments   # find the "gmail-markup MCP" deployment ID
clasp update-deployment <deployment-id> --description "gmail-markup MCP"   # keeps the /exec URL
```

MCP server, on the VM:

```bash
cd ~/gmail-markup && git pull
UV_PROJECT_ENVIRONMENT=~/.virtualenvs/gmail-markup ~/.local/bin/uv sync --frozen --no-dev   # only if dependencies changed
sudo systemctl restart gmail-markup
journalctl -u gmail-markup -n 100 --no-pager
```

## Local development

```bash
export VIRTUAL_ENV=~/.virtualenvs/gmail-markup   # then activate it
uv sync --active
pytest
ruff check src tests
```

## Troubleshooting

| Symptom | Likely cause |
| --- | --- |
| Tool error "Apps Script failed: unauthorised" | `APPS_SCRIPT_TOKEN` in `.env` differs from the `API_TOKEN` script property |
| Tool error "Apps Script returned non-JSON" | `doPost` threw: the permission grant (step 2.6) is missing, or the MailApp daily quota (100 emails) is used up; see Executions in the Apps Script editor |
| Email arrives but no calendar event | Gmail's smart features or Calendar's "Events from Gmail" setting is off, or the sender and recipient are not the same account |
| claude.ai cannot connect, nothing in `journalctl` | a CDN or firewall in front of nginx (e.g. Cloudflare Bot Fight Mode) is blocking Anthropic's requests |
| 421 "Invalid Host header" | the request's Host is not `PUBLIC_HOST` |

## Licence

[MIT](LICENSE)
