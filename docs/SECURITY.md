# Security

optlab is a single-user application that runs on your own computer. It never places orders and holds no
brokerage credentials. These are the protections that matter for that setup, and how to check them.

## Exposure

| Piece | Listens on | Reached from |
|---|---|---|
| API and dashboard (`optlab-api`) | 127.0.0.1:8600 | this computer; your devices through `tailscale serve` (HTTPS, your tailnet only) |
| Research Lab (Streamlit) | 127.0.0.1:8517 | the same way, on port 8443 |
| Docker Compose | 127.0.0.1 only (published ports) | the same |

There's no login. Anyone who can reach the port can use the app, so keep it on 127.0.0.1 and use Tailscale (never Funnel) for remote access.

## Browser-based attacks

A web page you visit could try to use your browser to reach the local API. The API refuses this:

| Attack | Protection |
|---|---|
| DNS rebinding (a hostile name resolving to 127.0.0.1) | **Host allowlist**: only `localhost`, `127.0.0.1`, `::1` and `*.ts.net` are answered by default (400 otherwise). Set `OPTLAB_ALLOWED_HOSTS` to change it |
| Cross-site requests (CSRF) | state-changing requests from another origin get 403: `Origin` must be this server (or your Tailscale name through `tailscale serve`) or a listed CORS origin, and `Sec-Fetch-Site: cross-site` is refused. Bodies must be JSON (a form or `text/plain` post is refused) |
| Cross-origin reads | CORS allows only the origins in `OPTLAB_CORS_ORIGINS`; `*` is rejected at startup |
| Script injection (XSS) | React escapes all text; there's no `dangerouslySetInnerHTML`; news and links accept only `http(s)` URLs and open with `rel="noopener noreferrer"`. The dashboard sends a **Content-Security-Policy**: scripts only from this server, no plugins, no framing |
| Clickjacking | `X-Frame-Options: DENY` and `frame-ancestors 'none'` |

Other headers on every response:
- `X-Content-Type-Options: nosniff`
- `Referrer-Policy: no-referrer`
- a `Permissions-Policy` that denies the camera, microphone, location, payment and USB
- `Cross-Origin-Opener-Policy: same-origin`

## Input handling

- Request bodies over 2 MB are refused (`OPTLAB_MAX_BODY_BYTES`). CSV uploads have their own 1 MB limit (`OPTLAB_MAX_UPLOAD_BYTES`).
- **Rule conditions** use a restricted expression language. They're parsed to a syntax tree and only comparisons, arithmetic, `and`/`or`/`not` and whitelisted study functions are allowed: no attribute access, indexing, comprehensions or lambdas.
  - Exponentiation is computed in floating point, so `2 ** 2 ** 40` can't hang the monitor. Before Phase 7 it could.
- Money is validated to $0.0001 and unsupported precision is refused. Every write uses optimistic versioning.
- SQL goes through SQLAlchemy with bound parameters. The two f-string statements in the market-data store only interpolate fixed column lists.
- The static file server only serves files inside `web/dist`.
- YAML is read with `safe_load`. RSS, Atom and XML are parsed with `defusedxml`: no entity expansion, no external DTDs.

## Errors and secrets

- Unexpected errors return `INTERNAL_ERROR` with a request id. The details, including the stack trace, go only to the server log.
- Secrets live in the environment, never in `config.yaml` or the repository: `FINNHUB_API_KEY` and `OPTLAB_SEC_IDENTITY`.
  - The Finnhub key is sent in a header, never in a URL.
  - Fetch errors report the host and path only, never query strings or headers.
- `.env`, `data/` (the databases, the news cache and the sentiment dictionary) and backups are git-ignored.

## Dependencies (audited for Phase 7)

| Finding | Status |
|---|---|
| `pip-audit`: Pillow 12.3.0, 12 advisories (fixed in 13.0.0) | Not reachable: optlab never opens images from outside (the only upload takes CSV or strategy YAML). Pillow 13 needs Streamlit 1.66.1, which wasn't tested with the Research Lab in this phase; upgrade both together (`pip install -U "pillow>=13" "streamlit>=1.66.1"`) and check the Research Lab |
| `pip-audit`: fsspec 2026.2.0 (PYSEC-2026-4217) | Pulled in only by `nautilus_trader`, which pins it and which neither the API nor the dashboard use (it's left out of the Docker image) |
| `npm audit`: 11 moderate, all in `minimatch` under `eslint` | Development tooling only, not in the built dashboard. `npm audit --omit=dev` reports 0 |

Re-run them with `pip-audit -r requirements.txt` and `cd web && npm audit --omit=dev`.

## Checking it yourself

```bash
# a foreign Host is refused (DNS rebinding)
curl -s -H 'Host: evil.example' http://127.0.0.1:8600/api/v1/health            # HOST_NOT_ALLOWED
# a cross-site change is refused
curl -s -X POST -H 'Origin: https://evil.example' -H 'Content-Type: application/json' \
     -d '{"name":"x"}' http://127.0.0.1:8600/api/v1/accounts                     # ORIGIN_NOT_ALLOWED
# headers
curl -sI http://127.0.0.1:8600/ | grep -iE 'content-security|x-frame|referrer|nosniff'
```

The automated checks are in `tests/test_security.py`.
