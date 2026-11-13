#!/usr/bin/env bash
# Run scripts/autoupdate.sh every 5 minutes (and 2 minutes after the computer starts), as a systemd user timer.
#
#   bash scripts/install_autoupdate.sh          turn automatic updates on
#   bash scripts/install_autoupdate.sh --off    turn them off
#   journalctl --user -u optlab-update          see what it did
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
UNITS="$HOME/.config/systemd/user"

if [ "${1:-}" = "--off" ]; then
  systemctl --user disable --now optlab-update.timer 2>/dev/null || true
  rm -f "$UNITS/optlab-update.service" "$UNITS/optlab-update.timer"
  systemctl --user daemon-reload
  echo "Automatic updates are off."
  exit 0
fi

mkdir -p "$UNITS"
cat > "$UNITS/optlab-update.service" <<UNIT
[Unit]
Description=optlab: pull the latest version and restart the app if needed
After=network-online.target

[Service]
Type=oneshot
ExecStart=/usr/bin/env bash $ROOT/scripts/autoupdate.sh
UNIT
cat > "$UNITS/optlab-update.timer" <<UNIT
[Unit]
Description=optlab: check for a new version every 5 minutes

[Timer]
OnBootSec=2min
OnUnitActiveSec=5min
Persistent=true

[Install]
WantedBy=timers.target
UNIT
systemctl --user daemon-reload
systemctl --user enable --now optlab-update.timer
echo "Automatic updates are on: checked every 5 minutes. See what it did with: journalctl --user -u optlab-update"
