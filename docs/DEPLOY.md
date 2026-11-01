# Deploying optlab (dashboard + Research Lab)

Two supported ways to run it. Pick **one** for the dashboard API: both use the same database
(`data/optlab.sqlite`), and a second API on the same database refuses to start.

| | systemd + Tailscale (primary) | Docker Compose (alternative) |
|---|---|---|
| Dashboard / API | `optlab-api` service, 127.0.0.1:8600 | `api` container, 127.0.0.1:8600 |
| Research Lab | `optlab-dashboard` service, 127.0.0.1:8517 | `research` container, 127.0.0.1:8518 |
| Portfolio monitor (Phase 2) | inside `optlab-api` | inside the `api` container |
| YAML monitor, nightly refresh | `optlab-monitor`, `optlab-ingest.timer` | not included (keep them on systemd) |
| Remote access | `tailscale serve` (script) | add `tailscale serve` yourself (below) |
| Node.js needed | no (`web/dist` is committed) | no (built in the image) |

Nothing is exposed publicly: services bind to 127.0.0.1, and Tailscale serves them only to your devices.

## systemd + Tailscale (your current install)

Updating an existing install from Phase 0:

```bash
cd ~/Downloads/Optional/Optional
git pull origin claude/adoring-gauss-b5wtx9
.venv/bin/pip install -r requirements.txt        # adds fastapi, uvicorn, sqlalchemy, alembic
.venv/bin/python -m pytest -q                    # expect all tests to pass
bash scripts/setup_remote.sh --dry-run           # shows exactly what will change
bash scripts/setup_remote.sh
```

The script adds the `optlab-api` service and changes the Tailscale links:

| Link | Before | After |
|---|---|---|
| `https://<name>/` | Streamlit (Research Lab) | the new dashboard (port 8600) |
| `https://<name>:8443/` | — | the Research Lab (Streamlit, port 8517) |

The Research Lab gets its own HTTPS port rather than a `/research` path: Streamlit's websocket and
base-path handling behind a proxy prefix is fragile, separate ports are not. The dashboard's sidebar
links to it. `--no-api` keeps the old single link and skips the new service.

The script never deletes data; it restarts the Streamlit and monitor services so they pick up the
configuration (as before) and starts the new API. Day to day:

| Task | Command |
|---|---|
| Status | `systemctl --user status optlab-api optlab-dashboard optlab-monitor` |
| API log | `journalctl --user -u optlab-api -f` |
| After `git pull` | `.venv/bin/pip install -r requirements.txt && systemctl --user restart optlab-api optlab-dashboard optlab-monitor` |
| Stop the dashboard only | `systemctl --user stop optlab-api` |

Run the API by hand instead (for development): `PYTHONPATH=src .venv/bin/python -m optlab.api`.

## Docker Compose (alternative)

```bash
cp .env.example .env                 # optional: ports, your user id (id -u / id -g)
docker compose up -d --build
docker compose ps                    # both services should become "healthy"
```

Dashboard: `http://127.0.0.1:8600` · Research Lab: `http://127.0.0.1:8518` · API docs: `/api/docs`.

- The project's `data/` folder is bind-mounted at `/app/data`, so there is exactly one database file,
  shared with the host. `config.yaml` and `scans.yaml` are mounted read-only.
- Containers run as your user (`OPTLAB_UID`/`OPTLAB_GID`, default 1000) so `data/` keeps your ownership.
- Health checks: the API's `/api/v1/health` (it reports the migration revision) and Streamlit's
  `/_stcore/health`.
- If Docker Hub answers `429 Too Many Requests`, set `OPTLAB_PYTHON_IMAGE` and `OPTLAB_NODE_IMAGE` in
  `.env` to a mirror (examples in `.env.example`).
- Behind a TLS-intercepting proxy, build with its CA certificate as a secret:
  `docker build --secret id=ca_bundle,src=/path/ca.pem --build-arg HTTPS_PROXY=$HTTPS_PROXY -t optlab:local .`
- Remote access with Docker: `sudo tailscale serve --bg 8600` and
  `sudo tailscale serve --bg --https=8443 8518`.

### Switching the dashboard from systemd to Docker (if you choose to later)

```bash
python scripts/portfolio_db.py backup                    # safety copy first
systemctl --user disable --now optlab-api optlab-dashboard   # keep optlab-monitor and the ingest timer
docker compose up -d --build
sudo tailscale serve --bg 8600 && sudo tailscale serve --bg --https=8443 8518
```

Going back: `docker compose down`, then `bash scripts/setup_remote.sh`.

## The portfolio monitor (Phase 2)

The API runs the portfolio monitor in a background thread: positions, watched contracts and alert rules in
`data/optlab.sqlite`, on the schedule in the dashboard's Monitor → Settings (initially scans.yaml's
schedule). Nothing to install or start separately.

- **One per database.** It holds `data/optlab.monitor.lock`. A second API is already refused by the
  instance lock; `scripts/portfolio_monitor.py` (a standalone runner, mostly for `--once` checks) says
  the monitor runs elsewhere and exits. The lock works across the Docker bind mount.
- **Off switch:** `OPTLAB_MONITOR=off` (environment of `optlab-api`, or `.env` for Docker). *Run now* still
  works then.
- **Alongside the YAML monitor.** `optlab-monitor` keeps running the scans in scans.yaml. Positions you
  import into the dashboard (Monitor → Import scans.yaml) are skipped by it automatically, so the same
  trade never alerts twice; the rest it still checks. Restart it after importing:
  `systemctl --user restart optlab-monitor`.
- **Desktop notifications** use `notify-send` (package `libnotify-bin`) from the `optlab-api` user service,
  as the YAML monitor already does. Inside Docker there's no desktop: use the browser notifications.
- **Upgrading from Phase 1:** `git pull`, `pip install -r requirements.txt`,
  `python scripts/portfolio_db.py backup`, then `systemctl --user restart optlab-api`. The API applies
  migration `0002` at startup (it switches monitoring on for open positions). The new frontend is in
  `web/dist` (committed).

**Upgrading to Phase 3:** the same steps (`git pull`, `pip install -r requirements.txt`,
`python scripts/portfolio_db.py backup`, restart `optlab-api`). Migration `0003` adds the plan and policy
tables and three columns to `positions`; it changes no existing data. Until you save a risk policy the
defaults apply (small-account limits below $1,000 of account value).

**Upgrading to Phase 4:** the same steps. Migration `0004` adds the journal, review, points, objective
and achievement tables only. Points for positions, plans and reviews you already have are counted the first
time the dashboard loads.

**Completing Phase 4 (migration `0005`):** the same steps. Back up first:
`python scripts/portfolio_db.py backup`. Migration `0005`:

- adds the stored score history (`daily_scores`);
- extends the points ledger with its audit columns;
- makes the score history, the ledger and the achievements append-only (database triggers).

Existing points are kept as legacy awards. The first time the dashboard loads, finished days from the last 90 days are stored and flagged as backfilled.

**Trying an upgrade on a copy first (Docker):** never test an upgrade on your only database.

```bash
mkdir -p ~/optlab-upgrade-test
python scripts/portfolio_db.py backup ~/optlab-upgrade-test/optlab.sqlite
OPTLAB_DATA_DIR=~/optlab-upgrade-test OPTLAB_API_PORT=8601 OPTLAB_RESEARCH_PORT=8519 \
  docker compose up -d --build
curl -s http://127.0.0.1:8601/api/v1/health      # "revision": "0005"
docker compose down
```

The copy is migrated; `./data` is untouched. Use a different port from your running API.

## Database: migrations, backups, restore

- The API applies **forward** migrations at startup, under a lock, and never downgrades or resets.
- `python scripts/portfolio_db.py status` shows the current and latest revision.
- `python scripts/portfolio_db.py backup [dest]` makes a consistent online copy (SQLite backup API),
  safe while the API is running. Copying `optlab.sqlite` with `cp` is not safe: the recent writes live
  in `optlab.sqlite-wal` until checkpointed. Default destination: `data/backups/optlab-<UTC time>.sqlite`.
- Restore: stop the API, then `python scripts/portfolio_db.py restore data/backups/<file> --yes`
  (it backs up the current file first and refuses while the API is running).
- Rollback a migration: `python scripts/portfolio_db.py downgrade <revision> --yes` (backs up first).
  Downgrading `0001` to `base` drops all portfolio tables and their data.
- Daily backup with systemd, if you want one: a timer running `scripts/portfolio_db.py backup`.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `Another optlab API instance is already using this database` | Something already runs the API (systemd or Docker). Stop one: `systemctl --user stop optlab-api` or `docker compose stop api`. |
| `Port 8600 is already used by another program` | `ss -ltnp | grep 8600` shows what; stop it or use `--api-port`. |
| Dashboard says "isn't built yet" | `web/dist` missing: `git pull`, or `cd web && npm ci && npm run build` (Node ≥ 20.19). |
| `No module named 'fastapi'` / `'sqlalchemy'` | `.venv/bin/pip install -r requirements.txt` |
| Quotes always *Unavailable* | Check the Research Lab's option chain works; Cboe through OpenBB needs internet. Outside market hours quotes are the last session's. |
| Quotes say *Synthetic* | `config.yaml` has `data.source: synthetic` (or `OPTLAB_QUOTE_SOURCE=synthetic`). |
| Every position shows *Stale data* | Read the reason on the position. *option quotes unavailable (...)*: the provider failed (outside market hours Cboe sometimes errors); *Last checked ... ago*: the monitor isn't running or it's outside the monitoring window (Monitor panel says which). |
| Monitor panel: *Runs in another process* | Another process holds `data/optlab.monitor.lock` (Docker, or `scripts/portfolio_monitor.py`). Use *Run now* in that one, or stop it. |
| No desktop notifications | `sudo apt install libnotify-bin`; check Monitor → Settings → Desktop; the event says *no desktop notifier on the server* when `notify-send` is missing. |
| Browser notifications can't be ticked | Use the `https://` Tailscale link (browsers need HTTPS or localhost), and allow notifications for the site. |
| The old app opens at the main link | You ran with `--no-api`, or `tailscale serve status` still points / at 8517: re-run `bash scripts/setup_remote.sh`. |
| Docker: `permission denied` on `data/` | Set `OPTLAB_UID`/`OPTLAB_GID` in `.env` to your `id -u` / `id -g`. |
