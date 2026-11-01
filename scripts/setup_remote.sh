#!/usr/bin/env bash
# Open the optlab dashboard from any of your devices through a private Tailscale link.
#
#   bash scripts/setup_remote.sh             set everything up (Linux with systemd)
#   bash scripts/setup_remote.sh --dry-run   print what it would do, change nothing
#   bash scripts/setup_remote.sh --no-monitor --no-ingest   dashboard only
#   bash scripts/setup_remote.sh --port 8520 use another port for the Research Lab (default 8517)
#   bash scripts/setup_remote.sh --no-api    keep the Streamlit app as the main link (no new dashboard)
#   bash scripts/setup_remote.sh --api-port 8610  another port for the dashboard (default 8600)
#   bash scripts/setup_remote.sh --uninstall remove the services and the Tailscale links
#
# What it does:
#   1. reads this computer's Tailscale name (e.g. mybox.tail1234.ts.net)
#   2. writes .streamlit/config.toml: the dashboard listens on this computer only (127.0.0.1)
#      on its own port, 8517 (not Streamlit's usual 8501, which another Streamlit project on
#      this computer may already use), and accepts the browser coming through that name
#   3. installs systemd user services that start at boot and restart after a crash:
#        optlab-api         the dashboard and API (python -m optlab.api) on 127.0.0.1:8600
#        optlab-dashboard   the Research Lab: streamlit run app/streamlit_app.py on 127.0.0.1:8517
#        optlab-monitor     python scripts/monitor.py (waits for market hours by itself)
#        optlab-ingest      python scripts/ingest.py, weekdays 17:30 New York time (a timer)
#   4. runs `tailscale serve`, with HTTPS, reachable only from devices signed in to your Tailscale account:
#        https://<name>/        the dashboard (port 8600)
#        https://<name>:8443/   the Research Lab (Streamlit). Separate ports, not a /research path:
#                               Streamlit's websocket is unreliable behind a path prefix.
#      With --no-api, https://<name>/ is the Research Lab as before.
#
# Nothing is opened to the public internet and no port is forwarded on your router.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
UNITS="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
PORT=8517
API_PORT=8600
DRY=0 MONITOR=1 INGEST=1 UNINSTALL=0 API=1
while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run) DRY=1 ;;
    --no-monitor) MONITOR=0 ;;
    --no-ingest) INGEST=0 ;;
    --uninstall) UNINSTALL=1 ;;
    --no-api) API=0 ;;
    --api-port) API_PORT="${2:?--api-port needs a number}"; shift ;;
    --port) PORT="${2:?--port needs a number}"; shift ;;
    -h|--help) sed -n '2,30p' "$0"; exit 0 ;;
    *) echo "Unknown option: $1 (see --help)"; exit 2 ;;
  esac
  shift
done

run() { if [ "$DRY" = 1 ]; then echo "  would run: $*"; else "$@"; fi; }
write() {  # write <path>; contents on stdin
  if [ "$DRY" = 1 ]; then echo "  would write $1:"; sed 's/^/    | /'; else mkdir -p "$(dirname "$1")"; cat > "$1"; fi
}
say() { echo "==> $*"; }
die() { echo "!! $*" >&2; exit 1; }

command -v systemctl >/dev/null || die "systemd not found. On macOS or Windows follow the manual steps in docs/REMOTE_ACCESS.md."

if [ "$UNINSTALL" = 1 ]; then
  say "Removing the optlab services and the Tailscale link"
  run systemctl --user disable --now optlab-api.service optlab-dashboard.service optlab-monitor.service optlab-ingest.timer || true
  for u in optlab-api.service optlab-dashboard.service optlab-monitor.service optlab-ingest.service optlab-ingest.timer; do
    run rm -f "$UNITS/$u"
  done
  run systemctl --user daemon-reload
  run sudo tailscale serve --https=443 off || true
  run sudo tailscale serve --https=8443 off || true
  say "Done. .streamlit/config.toml is left in place; delete it to go back to the defaults."
  exit 0
fi

# ---------------------------------------------------------------- checks
[ -x "$ROOT/.venv/bin/python" ] || die "No .venv in $ROOT. First: python -m venv .venv && .venv/bin/pip install -r requirements.txt"
[ -f "$ROOT/data/market.duckdb" ] || say "No database yet; run .venv/bin/python scripts/ingest.py once before opening the dashboard"
command -v tailscale >/dev/null || die "Tailscale is not installed. Install it with:
    curl -fsSL https://tailscale.com/install.sh | sh
    sudo tailscale up
then run this script again."

NAME="$(tailscale status --json 2>/dev/null | "$ROOT/.venv/bin/python" -c \
  'import json, sys; print(json.load(sys.stdin).get("Self", {}).get("DNSName", "").rstrip("."))' || true)"
[ -n "$NAME" ] || die "Tailscale is installed but not signed in. Run: sudo tailscale up"
say "This computer's Tailscale name: $NAME"

# A port must be free, or already ours: otherwise the link would show whatever app holds it.
port_check() {  # port_check <port> <our service> <hint>
  if "$ROOT/.venv/bin/python" -c "import socket, sys; sys.exit(socket.socket().connect_ex(('127.0.0.1', $1)) != 0)"; then
    if ! systemctl --user is-active --quiet "$2"; then
      command -v ss >/dev/null && ss -Hltnp "sport = :$1" 2>/dev/null | sed 's/^/    /'
      die "Port $1 is already used by another program (perhaps optlab in Docker: docker compose down). $3"
    fi
  fi
}
port_check "$PORT" optlab-dashboard.service "Stop it, or pick a free port: bash scripts/setup_remote.sh --port 8520"
if [ "$API" = 1 ]; then
  port_check "$API_PORT" optlab-api.service "Stop it first."
  "$ROOT/.venv/bin/python" -c "import fastapi, uvicorn, sqlalchemy, alembic, multipart" 2>/dev/null \
    || die "The API's packages are missing. Run: $ROOT/.venv/bin/pip install -r $ROOT/requirements.txt"
  [ -f "$ROOT/web/dist/index.html" ] || die "web/dist is missing (git pull, or build it: cd web && npm ci && npm run build)"
fi

# ---------------------------------------------------------------- streamlit config
say "Writing .streamlit/config.toml"
write "$ROOT/.streamlit/config.toml" <<EOF
# Written by scripts/setup_remote.sh. The dashboard listens on this computer only;
# Tailscale forwards https://$NAME/ to it.
[server]
address = "127.0.0.1"
port = $PORT
headless = true
corsAllowedOrigins = ["https://$NAME"]

[browser]
gatherUsageStats = false
EOF

# ---------------------------------------------------------------- services
say "Installing systemd user services in $UNITS"
ENABLE=()
if [ "$API" = 1 ]; then
  write "$UNITS/optlab-api.service" <<EOF
[Unit]
Description=optlab dashboard and API (FastAPI; informational only, never places orders)
After=network-online.target

[Service]
WorkingDirectory=$ROOT
Environment=PYTHONPATH=$ROOT/src
Environment=OPTLAB_API_HOST=127.0.0.1
Environment=OPTLAB_API_PORT=$API_PORT
Environment=OPTLAB_RESEARCH_URL=https://$NAME:8443/
ExecStart=$ROOT/.venv/bin/python -m optlab.api
Restart=on-failure
RestartSec=10

[Install]
WantedBy=default.target
EOF
  ENABLE+=(optlab-api.service)
fi
write "$UNITS/optlab-dashboard.service" <<EOF
[Unit]
Description=optlab Research Lab (Streamlit)
After=network-online.target

[Service]
WorkingDirectory=$ROOT
ExecStart=$ROOT/.venv/bin/streamlit run app/streamlit_app.py
Restart=on-failure
RestartSec=10

[Install]
WantedBy=default.target
EOF
ENABLE+=(optlab-dashboard.service)

if [ "$MONITOR" = 1 ]; then
  write "$UNITS/optlab-monitor.service" <<EOF
[Unit]
Description=optlab live monitor (scans.yaml; checks only, never trades)
After=network-online.target

[Service]
WorkingDirectory=$ROOT
ExecStart=$ROOT/.venv/bin/python scripts/monitor.py
Restart=on-failure
RestartSec=30

[Install]
WantedBy=default.target
EOF
  ENABLE+=(optlab-monitor.service)
fi

if [ "$INGEST" = 1 ]; then
  write "$UNITS/optlab-ingest.service" <<EOF
[Unit]
Description=optlab nightly data refresh (scripts/ingest.py)
After=network-online.target

[Service]
Type=oneshot
WorkingDirectory=$ROOT
ExecStart=$ROOT/.venv/bin/python scripts/ingest.py
EOF
  write "$UNITS/optlab-ingest.timer" <<EOF
[Unit]
Description=Run the optlab data refresh after the US close on weekdays

[Timer]
OnCalendar=Mon..Fri 17:30 America/New_York
Persistent=true

[Install]
WantedBy=timers.target
EOF
  ENABLE+=(optlab-ingest.timer)
fi

run systemctl --user daemon-reload
run systemctl --user enable "${ENABLE[@]}"
# restart, not just start: a dashboard left running from an earlier setup must pick up the new config
run systemctl --user restart optlab-dashboard.service
[ "$API" = 1 ] && run systemctl --user restart optlab-api.service
[ "$MONITOR" = 1 ] && run systemctl --user restart optlab-monitor.service
[ "$INGEST" = 1 ] && run systemctl --user start optlab-ingest.timer
# keep the services running after a reboot, before anyone logs in
run loginctl enable-linger "${USER:-$(id -un)}" || say "Could not enable lingering; the services start when you log in instead"

# ---------------------------------------------------------------- tailscale link
say "Linking https://$NAME/ (Tailscale may ask you to enable HTTPS once)"
if [ "$API" = 1 ]; then
  run sudo tailscale serve --bg "$API_PORT"                    # https://name/      -> the dashboard
  run sudo tailscale serve --bg --https=8443 "$PORT"           # https://name:8443/ -> the Research Lab
else
  run sudo tailscale serve --bg "$PORT"
fi

if [ "$DRY" = 0 ]; then
  say "Waiting for the dashboard on 127.0.0.1:$PORT"
  for _ in $(seq 1 60); do
    curl -fsS "http://127.0.0.1:$PORT/_stcore/health" >/dev/null 2>&1 && break
    sleep 1
  done
  if ! systemctl --user is-active --quiet optlab-dashboard.service \
     || ! curl -fsS "http://127.0.0.1:$PORT/_stcore/health" >/dev/null 2>&1; then
    journalctl --user -u optlab-dashboard -n 20 --no-pager || true
    die "The dashboard did not start on port $PORT (log above)."
  fi
  if [ "$API" = 1 ]; then
    for _ in $(seq 1 60); do
      curl -fsS "http://127.0.0.1:$API_PORT/api/v1/health" >/dev/null 2>&1 && break
      sleep 1
    done
    curl -fsS "http://127.0.0.1:$API_PORT/api/v1/health" >/dev/null 2>&1 || {
      journalctl --user -u optlab-api -n 30 --no-pager || true
      die "The API did not start on port $API_PORT (log above)."; }
  fi
  say "What the links point to:"
  tailscale serve status 2>/dev/null | sed 's/^/    /' || true
fi

cat <<EOF

Done. From any device signed in to your Tailscale account:

    https://$NAME/          $([ "$API" = 1 ] && echo "dashboard" || echo "Research Lab")
$([ "$API" = 1 ] && echo "    https://$NAME:8443/     Research Lab")

  status     systemctl --user status optlab-api optlab-dashboard optlab-monitor optlab-ingest.timer
  logs       journalctl --user -u optlab-api -f        (or -u optlab-dashboard)
  after a git pull:      systemctl --user restart optlab-api optlab-dashboard optlab-monitor
  after editing scans.yaml: systemctl --user restart optlab-monitor
  remove     bash scripts/setup_remote.sh --uninstall
EOF
