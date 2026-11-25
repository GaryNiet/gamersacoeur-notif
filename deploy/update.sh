#!/usr/bin/env bash
# Pull the latest code and restart:   sudo bash /opt/gamersacoeur-notif/deploy/update.sh
set -euo pipefail
APP=/opt/gamersacoeur-notif
git -C "$APP" pull --ff-only
"$APP/.venv/bin/pip" install -q -r "$APP/requirements.txt"
install -m 644 "$APP/deploy/gamersacoeur-notif.service" /etc/systemd/system/
install -m 755 "$APP/deploy/gac.sh" /usr/local/bin/gac
systemctl daemon-reload
systemctl restart gamersacoeur-notif
systemctl --no-pager --lines=5 status gamersacoeur-notif
