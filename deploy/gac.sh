#!/usr/bin/env bash
# Run an app command on the server with its config and secrets, as the
# service user. Installed as /usr/local/bin/gac by install.sh.
#
#   gac check-mail                 test the mailbox (sends nothing)
#   gac check-progress --no-upload show the progress bar values
#   gac check-progress             ... and upload them
#   gac link                       the link to send to streamers
set -euo pipefail
if [[ $EUID -ne 0 ]]; then
  exec sudo "$0" "$@"
fi
set -a
# shellcheck disable=SC1091
. /etc/gamersacoeur-notif/env
set +a
cd /var/lib/gamersacoeur-notif
export PYTHONPATH=/opt/gamersacoeur-notif
exec runuser -u gac -- /opt/gamersacoeur-notif/.venv/bin/python -m gamersacoeur_notif \
  -c /etc/gamersacoeur-notif/config.yaml "$@"
