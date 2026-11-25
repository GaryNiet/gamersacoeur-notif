#!/usr/bin/env bash
# One-time setup on a fresh Debian 12+ / Ubuntu 22.04+ VPS. Safe to re-run.
#
#   sudo apt-get install -y git
#   sudo git clone -b claude/twint-donation-notifications-eloo7h https://github.com/GaryNiet/gamersacoeur-notif /opt/gamersacoeur-notif
#   sudo bash /opt/gamersacoeur-notif/deploy/install.sh notif.example.ch
#
# Creates:
#   /opt/gamersacoeur-notif          the code (+ .venv)
#   /etc/gamersacoeur-notif/config.yaml   settings (edit this)
#   /etc/gamersacoeur-notif/env      secrets (edit this, readable by root/gac only)
#   /var/lib/gamersacoeur-notif      database (Streamlabs tokens, donations)
#   systemd service "gamersacoeur-notif" behind Caddy (automatic HTTPS)
set -euo pipefail

DOMAIN="${1:-}"
if [[ -z "$DOMAIN" ]]; then
  echo "usage: sudo bash $0 <domain, e.g. notif.example.ch>" >&2
  exit 1
fi
if [[ $EUID -ne 0 ]]; then
  echo "Run as root (sudo)." >&2
  exit 1
fi

APP=/opt/gamersacoeur-notif
ETC=/etc/gamersacoeur-notif
DATA=/var/lib/gamersacoeur-notif
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [[ "$SRC" != "$APP" ]]; then
  echo "The repository must be cloned at $APP (found at $SRC)." >&2
  exit 1
fi

echo "==> Installing packages"
apt-get update -q
apt-get install -y -q python3 python3-venv git curl gnupg
if ! apt-cache show caddy >/dev/null 2>&1; then
  # Older distributions don't ship Caddy: use the official Caddy repository.
  curl -1sLf https://dl.cloudsmith.io/public/caddy/stable/gpg.key \
    | gpg --dearmor --yes -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  curl -1sLf https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt \
    > /etc/apt/sources.list.d/caddy-stable.list
  apt-get update -q
fi
apt-get install -y -q caddy

echo "==> Service user and directories"
id gac >/dev/null 2>&1 || useradd --system --home-dir "$DATA" --shell /usr/sbin/nologin gac
install -d -o gac -g gac -m 750 "$DATA"
install -d -o root -g gac -m 750 "$ETC"

echo "==> Python environment"
python3 -m venv "$APP/.venv"
"$APP/.venv/bin/pip" install -q --upgrade pip
"$APP/.venv/bin/pip" install -q -r "$APP/requirements.txt"

echo "==> Configuration"
if [[ ! -f "$ETC/config.yaml" ]]; then
  sed -e "s#^host: .*#host: 127.0.0.1#" \
      -e "s#^port: .*#port: 8000#" \
      -e "s#^database: .*#database: $DATA/gamersacoeur.sqlite3#" \
      -e "s#redirect_uri: .*#redirect_uri: https://$DOMAIN/callback#" \
      "$APP/config.example.yaml" > "$ETC/config.yaml"
  chown root:gac "$ETC/config.yaml"; chmod 640 "$ETC/config.yaml"
  echo "    created $ETC/config.yaml"
fi
if [[ ! -f "$ETC/env" ]]; then
  secret="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
  adminpw="$(python3 -c 'import secrets; print(secrets.token_urlsafe(12))')"
  cat > "$ETC/env" <<EOF
GAC_SECRET_KEY=$secret
GAC_ADMIN_PASSWORD=$adminpw
MS_CLIENT_SECRET=CHANGE_ME
FTP_PASSWORD=CHANGE_ME
# GAC_MAIL_PASSWORD=   (only if mail.provider is imap)
STREAMLABS_CLIENT_ID=CHANGE_ME
STREAMLABS_CLIENT_SECRET=CHANGE_ME
EOF
  chown root:gac "$ETC/env"; chmod 640 "$ETC/env"
  echo "    created $ETC/env"
  echo "    admin password for https://$DOMAIN/admin : $adminpw"
fi

install -m 755 "$APP/deploy/gac.sh" /usr/local/bin/gac

echo "==> systemd service"
install -m 644 "$APP/deploy/gamersacoeur-notif.service" /etc/systemd/system/
systemctl daemon-reload
systemctl enable gamersacoeur-notif >/dev/null

echo "==> Caddy (HTTPS)"
SITE="$DOMAIN {
	reverse_proxy 127.0.0.1:8000
}"
if grep -q "^$DOMAIN " /etc/caddy/Caddyfile 2>/dev/null; then
  echo "    $DOMAIN already in /etc/caddy/Caddyfile"
elif grep -q "The Caddyfile is an easy way" /etc/caddy/Caddyfile 2>/dev/null \
     || [[ ! -s /etc/caddy/Caddyfile ]]; then
  echo "$SITE" > /etc/caddy/Caddyfile        # replace the package's example file
  systemctl reload caddy || systemctl restart caddy
else
  printf '\n%s\n' "$SITE" >> /etc/caddy/Caddyfile
  systemctl reload caddy || systemctl restart caddy
fi

if command -v ufw >/dev/null && ufw status | grep -q "Status: active"; then
  ufw allow 80/tcp >/dev/null; ufw allow 443/tcp >/dev/null
fi

echo
if grep -q CHANGE_ME "$ETC/env"; then
  cat <<EOF
Almost done. Next:
  1. sudo nano $ETC/env           # Microsoft client secret, FTP password, Streamlabs id/secret
  2. sudo nano $ETC/config.yaml   # mailbox, parsing rules, main_accounts
  3. gac check-mail                 # test the mailbox (sends nothing)
     gac check-progress --no-upload   # check the progress bar values
  4. sudo systemctl restart gamersacoeur-notif
  5. sudo journalctl -u gamersacoeur-notif -f    # check the logs
EOF
else
  systemctl restart gamersacoeur-notif
  echo "Service restarted. Logs: sudo journalctl -u gamersacoeur-notif -f"
fi
echo "Streamer link: https://$DOMAIN/    Admin: https://$DOMAIN/admin"
