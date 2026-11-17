# Deploying on a VPS

This takes about 20 minutes. The WordPress site stays on GoDaddy; the app runs on a subdomain such as `notif.gamersacoeur.ch`.

## 1. Get a VPS

The smallest plan of any provider is plenty: 1 vCPU and 1 GB RAM, for example Infomaniak VPS Lite, Hetzner CX22 or Exoscale Micro. Choose **Debian 12** or **Ubuntu 24.04**, and add your SSH key while creating it. Note the server's **IPv4 address**.

## 2. Point a subdomain to it (in GoDaddy)

GoDaddy → *My Products* → your domain → **DNS** → *Add New Record*:

| Type | Name | Value | TTL |
|---|---|---|---|
| A | `notif` | the VPS IPv4 address | 1 hour |

This doesn't touch the WordPress site. Check that it works (it can take a few minutes): `ping notif.yourdomain.ch` should show the VPS address.

## 3. Install

```bash
ssh root@<vps-ip>          # or your user, with sudo

sudo apt-get install -y git
sudo git clone https://github.com/GaryNiet/gamersacoeur-notif /opt/gamersacoeur-notif
sudo bash /opt/gamersacoeur-notif/deploy/install.sh notif.yourdomain.ch
```

> If the GitHub repository is private, `git clone` asks for a username and password. Use your GitHub username and a [personal access token](https://github.com/settings/tokens) with read access to the repository as the password.

The script does the following:

- installs Python and Caddy (the web server; it gets and renews the HTTPS certificate on its own),
- creates the service,
- generates the **admin password** and prints it. Keep it; it's also stored in `/etc/gamersacoeur-notif/env`.

## 4. Fill in the settings

```bash
sudo nano /etc/gamersacoeur-notif/env
```

Replace the `CHANGE_ME` values with the mailbox password and the Streamlabs client ID and secret. If a value contains spaces or `#`, put it in single quotes: `GAC_MAIL_PASSWORD='my pass#1'`.

```bash
sudo nano /etc/gamersacoeur-notif/config.yaml
```

Set the mailbox, the email filters, the parsing rules and `main_accounts` (the Streamlabs/Twitch name of the account that gets every donation). `redirect_uri` is already set to your domain. In the Streamlabs app settings, set the **same** redirect URI: `https://notif.yourdomain.ch/callback`.

Then start the app and watch the logs:

```bash
sudo systemctl restart gamersacoeur-notif
sudo journalctl -u gamersacoeur-notif -f      # Ctrl+C to quit
```

You should see `Connected to imap...`. Open `https://notif.yourdomain.ch/admin` (any username, plus the admin password).

## Day-to-day

| What | Command |
|---|---|
| Logs | `sudo journalctl -u gamersacoeur-notif -f` |
| Restart after editing the config | `sudo systemctl restart gamersacoeur-notif` |
| Update to the latest code | `sudo bash /opt/gamersacoeur-notif/deploy/update.sh` |
| Test a real email's parsing | copy the `.eml` to the server, then `sudo -u gac env $(sudo cat /etc/gamersacoeur-notif/env \| xargs) PYTHONPATH=/opt/gamersacoeur-notif /opt/gamersacoeur-notif/.venv/bin/python -m gamersacoeur_notif -c /etc/gamersacoeur-notif/config.yaml parse mail.eml` |
| Backup (streamer tokens + donations) | `sudo cp /var/lib/gamersacoeur-notif/gamersacoeur.sqlite3 ~/backup.sqlite3` |

The service starts on boot and restarts automatically if it crashes.

The parse test is easier on your own PC: run the `parse` command from the main README with a local `config.yaml`, then copy the working regexes to the server.

## After the event

Before cancelling the VPS, keep a backup of the database if you want the donation history. Streamers will reconnect next year anyway. Remove the `notif` DNS record in GoDaddy.
