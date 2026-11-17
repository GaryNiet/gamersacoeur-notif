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

## How streamers sign up

Sign-up is **open**. Everyone gets the same link: the app's home page (`python -m gamersacoeur_notif link` prints it). A streamer clicks **Se connecter avec Streamlabs**, approves, and is in. There's no list to fill in beforehand and no account on the app's side.

On first login the app creates the streamer from their Streamlabs account. Their Streamlabs name and any connected Twitch/YouTube names become their **keywords**. The confirmation page tells them which names donors should mention.

On `/admin` you can, for each streamer:

- edit the keywords,
- tick **principal** (receives every donation),
- untick **actif** (stops receiving alerts),
- send a test alert,
- delete them.

Tokens are stored in the SQLite database, so each streamer connects **once**, before the event. Reconnecting later refreshes the token and keeps your edits.

The **main account** is whichever account has `principal` ticked; there can be several. To make it automatic, put its Streamlabs/Twitch name in `main_accounts` in `config.yaml` before it connects.

If you'd rather vet people, set `new_streamers_enabled: false`. Newcomers are then saved but receive nothing until you tick **actif**.

## How a donation is routed

Set `parsing.streamer` to a regex that pulls the relevant part of the email, for example the `Pour : …` line or the payment reference. The app looks in that text for each active streamer's keywords. Matching is on whole words and ignores case and accents. If several keywords appear, the first mention wins. If nobody matches, the donation goes to the main account(s) only, and the dashboard shows "no streamer matched".

Because sign-up is open, keep `parsing.streamer` narrow, so the app only searches the field where the donor names a streamer. Otherwise someone whose Twitch name is a common word, like "merci", could catch donations meant for others. The main account still gets every donation either way, and nobody can touch the money: at worst, an alert pops up on the wrong stream. You can fix the keywords or disable the streamer on `/admin`.

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
.venv/bin/python -m gamersacoeur_notif link     # the link to send to all streamers
```

`GAC_SECRET_KEY` signs the login sessions. Keep it fixed between restarts.

The web app has to be reachable **from the streamers' browsers** for the login step, so in production it runs on a VPS. See **[deploy/README.md](deploy/README.md)** for the step-by-step guide: a subdomain in GoDaddy DNS, one install script, then systemd and Caddy with automatic HTTPS.

Admin dashboard: `https://<domain>/admin`. The username can be anything; the password is `GAC_ADMIN_PASSWORD`.

### Before the stream: checklist

- [ ] `parse` gives the right result on a real email.
- [ ] Every streamer appears on `/admin` with sensible keywords, and the main account has **principal** ticked.
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
