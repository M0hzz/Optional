# Open optlab from anywhere (private link)

The dashboard keeps running on your own computer. [Tailscale](https://tailscale.com) (free for
personal use) gives it a private HTTPS link such as `https://mybox.tail1234.ts.net/` that opens on
your phone, laptop or tablet wherever you are, but only on devices signed in to your Tailscale
account. Nothing is exposed to the public internet and no router port is opened.

Your computer has to stay on (and not asleep) for the link to work.

## One-time setup (Ubuntu or any Linux with systemd)

1. Install Tailscale on the computer that runs optlab and sign in:

   ```bash
   curl -fsSL https://tailscale.com/install.sh | sh
   sudo tailscale up
   ```

2. Install the Tailscale app on your phone and other devices and sign in with the same account.

3. In the project folder:

   ```bash
   bash scripts/setup_remote.sh --dry-run   # shows what it will do
   bash scripts/setup_remote.sh
   ```

   It prints your link at the end. The first time, Tailscale may show a link to turn on HTTPS
   for your account; open it, click enable, and run the script again.

4. Stop the computer from sleeping: Ubuntu Settings → Power → Automatic Suspend → Off.

The script sets up five things:

| Piece | What it is | When it runs |
|---|---|---|
| `.streamlit/config.toml` | The dashboard listens on this computer only (127.0.0.1) and accepts the browser through your Tailscale name | Read by every `streamlit run` |
| `optlab-dashboard` | `streamlit run app/streamlit_app.py` as a service | At boot; restarted if it crashes |
| `optlab-monitor` | `python scripts/monitor.py` as a service | At boot; it waits for market hours by itself |
| `optlab-ingest.timer` | `python scripts/ingest.py` | Weekdays 17:30 New York time; runs at the next start if the computer was off |
| `tailscale serve` | Forwards `https://<name>/` to the dashboard | Always, until removed |

Options: `--no-monitor` (run the monitor yourself in a terminal instead), `--no-ingest` (refresh
data yourself), `--uninstall` (remove the services and the link).

## Day to day

| Task | Command |
|---|---|
| Is everything running? | `systemctl --user status optlab-dashboard optlab-monitor optlab-ingest.timer` |
| When does the next refresh run? | `systemctl --user list-timers optlab-ingest.timer` |
| Dashboard log | `journalctl --user -u optlab-dashboard -f` |
| Monitor log (scan passes, alerts) | `journalctl --user -u optlab-monitor -f` |
| Last data refresh | `journalctl --user -u optlab-ingest -n 50` |
| Refresh data now | `systemctl --user start optlab-ingest` |
| After `git pull`, or after editing `scans.yaml` or `config.yaml` | `systemctl --user restart optlab-dashboard optlab-monitor` |

The dashboard picks up each refresh by itself within 10 minutes: it opens the database for each
read and closes it again, so `ingest.py` can write while the dashboard is open.

Desktop notifications from the monitor appear on the computer, not on your phone. From your
phone, open **Live › Monitor** in the dashboard: it shows every pass, position status and alert, and
refreshes every 20 seconds.

## macOS or Windows (manual)

The script needs systemd, so on a Mac or Windows PC do the same by hand:

1. Install Tailscale from tailscale.com/download and sign in (same account on your phone).
2. Create `.streamlit/config.toml` in the project folder, using your computer's Tailscale name
   (shown in the Tailscale app, or `tailscale status`):

   ```toml
   [server]
   address = "127.0.0.1"
   port = 8501
   headless = true
   corsAllowedOrigins = ["https://mybox.tail1234.ts.net"]
   ```

3. Start the dashboard: `streamlit run app/streamlit_app.py`
4. In another terminal: `tailscale serve --bg 8501`
5. Open `https://<your-name>/` on any of your devices.

Keep the dashboard and the monitor running in terminals (or use launchd / Task Scheduler to
start them at login), and schedule `python scripts/ingest.py` for after the close.

## Security

- Only devices signed in to your Tailscale account can open the link. Anyone you add to your
  tailnet can open it too, so don't share your tailnet with people who shouldn't see your account settings.
- The dashboard has no login of its own. Don't expose port 8501 another way (router port
  forwarding, `tailscale funnel`, a public tunnel): anyone who can reach it can change overlay
  settings and see your positions.
- Your `config.yaml` (including the SEC identity) stays on the computer; nothing is uploaded.

## If the link doesn't load

| Symptom | Fix |
|---|---|
| Page never loads | The device isn't connected to Tailscale (open the app), or the computer is off or asleep |
| "Please wait…" forever, or the app keeps reconnecting | The name in `.streamlit/config.toml` doesn't match `tailscale status`; run the setup script again, then `systemctl --user restart optlab-dashboard` |
| `tailscale serve` asks to enable HTTPS | Follow the link it prints (one click in the Tailscale admin console), then run the script again |
| Services stop when you log out | `loginctl enable-linger $USER` |
| "No database yet" in the dashboard | Run `systemctl --user start optlab-ingest`, or `python scripts/ingest.py` |
