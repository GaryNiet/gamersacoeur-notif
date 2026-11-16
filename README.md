# gamersacoeur-notif

Turns TWINT donation **emails** into **Streamlabs alerts** during the Gamers à Coeur charity stream.

Streamlabs Charity does not support TWINT. Our backup payment provider does, but it only tells us about a payment by email. This app:

1. watches a mailbox over IMAP (emails are read, never marked as read or deleted),
2. extracts the amount, donor, message and target streamer from each donation email,
3. sends a Streamlabs donation/alert to **that streamer** and to the **main account** (every donation always goes to the main account),
4. shows everything on an admin dashboard. From there you can re-send a donation or send a test alert.

```
 TWINT payment ──► provider email ──► mailbox ──(IMAP)──► gamersacoeur-notif ──► Streamlabs (streamer)
                                                                             └──► Streamlabs (main account)
```

## How streamers log in

Each streamer gets a **personal link** (`/connect/<slug>/<signature>`). Opening it sends them to "Log in with Streamlabs". After they approve, their token is saved and alerts start showing in their normal Alert Box. Nothing else needs setting up in OBS. The links are signed, so nobody can link their own account in place of someone else's. Streamers only need to connect **once**, before the event; tokens are kept in the SQLite database.

## How a donation is routed

Set `parsing.streamer` to a regex that pulls the relevant part of the email, for example the `Pour : …` line or the payment reference. The app then looks in that text for each streamer's `keywords`. Matching is on whole words and ignores case and accents. If several keywords appear, the first mention wins. If nobody matches, the donation goes to the main account only, and the dashboard shows "no streamer matched".

## Setup

### 1. Install

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp config.example.yaml config.yaml
```

### 2. Create a Streamlabs app

At <https://streamlabs.com/dashboard#/settings/api-settings> → **Register an App**:

- Redirect URI: `https://<your-public-domain>/callback`. It must match `streamlabs.redirect_uri` exactly.
- Copy the client ID and secret into the `STREAMLABS_CLIENT_ID` / `STREAMLABS_CLIENT_SECRET` environment variables.
- While the app is unapproved, Streamlabs normally only lets **whitelisted** accounts authorize it. Add every streamer's Streamlabs account as a tester/whitelisted user in the app settings, or submit the app for approval well before the event.

The app requests the scopes `donations.create` and `alerts.create`.

### 3. Mailbox

Use IMAP access to the mailbox that receives the provider's emails.

- **Gmail**: `imap.gmail.com`, with an *App Password* (requires 2-step verification).
- **Infomaniak**: `mail.infomaniak.com`.
- **Outlook**: `outlook.office365.com`.

A dedicated address or folder (for example a Gmail filter → label `Dons`, with `folder: Dons`) keeps things clean. Also set `from_contains` / `subject_contains` so that only real donation emails count.

### 4. Teach it the email format

As soon as you have a real donation email, download it as `.eml` (in Gmail: ⋮ → *Download message*) and run:

```bash
.venv/bin/python -m gamersacoeur_notif parse donation.eml
```

This prints the extracted amount, donor, message and streamer, plus the text the regexes are applied to. Adjust the `parsing:` regexes in `config.yaml` until the output is right. Amounts like `1'000.50`, `25.-` and `12,50` are understood.

### 5. Run it

Set the secrets in the environment, then start everything:

```bash
export GAC_SECRET_KEY=$(python3 -c "import secrets; print(secrets.token_urlsafe(32))")  # keep it fixed!
export GAC_ADMIN_PASSWORD=...  GAC_MAIL_PASSWORD=...
export STREAMLABS_CLIENT_ID=...  STREAMLABS_CLIENT_SECRET=...

.venv/bin/python -m gamersacoeur_notif run      # web app + mailbox watcher
.venv/bin/python -m gamersacoeur_notif links    # personal links to send to streamers
```

`GAC_SECRET_KEY` signs the personal links, so the links change if the key changes.

The web app has to be reachable **from the streamers' browsers** for the login step. Choose one of these:

- a small VPS, Render, Railway, fly.io and so on, behind HTTPS;
- your own PC plus a tunnel, e.g. `cloudflared tunnel --url http://localhost:8000`. Then set `redirect_uri` to the tunnel URL in both `config.yaml` and the Streamlabs app.

Admin dashboard: `https://<domain>/admin`. The username can be anything; the password is `GAC_ADMIN_PASSWORD`.

### Before the stream: checklist

- [ ] `parse` gives the right result on a real email.
- [ ] Every streamer shows **connected** on `/admin`.
- [ ] Press **Test** for each streamer and check the alert appears in their OBS Alert Box.
- [ ] Make a real small TWINT donation and check that it arrives in under 30 s.

Set `streamlabs.dry_run: true` to try the full email flow without sending anything to Streamlabs.

## Options

| Setting | Meaning |
|---|---|
| `streamlabs.mode: donation` | Creates a real Streamlabs donation. It shows as a donation alert and appears in recent events and donation-goal widgets. Note that it counts in the streamer's Streamlabs *tip* stats, not in Streamlabs Charity totals. |
| `streamlabs.mode: alert` | Only shows an Alert Box popup, using `alert_message`. Nothing is recorded. |
| `mail.poll_seconds` | How often to check for new mail (default 15 s). |
| `mail.process_existing` | `false`: on the first start, ignore emails already in the mailbox. |

## Reliability

- The last processed IMAP UID and each email's Message-ID are stored, so restarts never cause duplicate alerts.
- If a streamer isn't connected, or Streamlabs returns an error, the donation is marked `partial`/`failed` on the dashboard. **Renvoyer** sends it again only to the accounts that missed it.
- If the amount can't be read, nothing is sent and the email is marked `unparsed`, so that a wrong amount never appears on stream.
- The IMAP connection reconnects automatically with backoff.

## Development

```bash
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest
```

The Streamlabs endpoints used are `POST /api/v2.0/donations`, `POST /api/v2.0/alerts` and the OAuth `authorize`/`token` endpoints, with a bearer token. They're in `gamersacoeur_notif/streamlabs.py`. Use the **Test** button to check them against your account.
