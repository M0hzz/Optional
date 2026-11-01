"""Request hardening for the local API (Phase 7). optlab has no login: it relies on listening on 127.0.0.1 and
being reached through Tailscale. These checks keep a web page you happen to visit from using your browser
to reach it.

    Host allowlist   DNS rebinding: a hostile site can make its own name resolve to 127.0.0.1, so its page
                     talks to your API "same-origin". Requests whose Host isn't on the list get 400.
                     OPTLAB_ALLOWED_HOSTS, comma separated; default localhost, 127.0.0.1, ::1 and *.ts.net
                     (your Tailscale HTTPS name). "*" switches the check off (not recommended).
    Origin check     a browser sends Origin on cross-site requests. A POST / PUT / PATCH / DELETE whose
                     Origin isn't this server or an allowed CORS origin gets 403, as does one marked
                     Sec-Fetch-Site: cross-site. Requests without Origin (curl, scripts) are allowed.
    Body limit       request bodies over OPTLAB_MAX_BODY_BYTES (default 2 MB; CSV uploads have their own,
                     smaller limit) get 413, before they're read into memory.
    Headers          nosniff, no referrer, no framing, a restrictive Permissions-Policy, and a
                     Content-Security-Policy for the dashboard (scripts only from this server).
"""
from __future__ import annotations

import json
import os
from urllib.parse import urlsplit

DEFAULT_HOSTS = "localhost,127.0.0.1,::1,*.ts.net"
DEFAULT_MAX_BODY = 2_000_000
UNSAFE = {"POST", "PUT", "PATCH", "DELETE"}
LOOPBACK = {"localhost", "127.0.0.1", "::1"}
CSP = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
       "font-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'self'; form-action 'self'; "
       "frame-ancestors 'none'")
HEADERS = [(b"x-content-type-options", b"nosniff"), (b"referrer-policy", b"no-referrer"), (b"x-frame-options", b"DENY"),
           (b"permissions-policy", b"camera=(), microphone=(), geolocation=(), payment=(), usb=()"),
           (b"cross-origin-opener-policy", b"same-origin")]


def allowed_hosts_from_env() -> list[str]:
    return [h.strip().lower() for h in os.environ.get("OPTLAB_ALLOWED_HOSTS", DEFAULT_HOSTS).split(",") if h.strip()]


def host_only(value: str) -> str:
    v = value.strip().lower()
    if v.startswith("["):                       # [::1]:8600
        return v[1:v.index("]")] if "]" in v else v
    if v.count(":") == 1:                       # name:port
        return v.split(":")[0]
    return v                                    # a bare IPv6 address or a name


def host_ok(host: str, allowed: list[str]) -> bool:
    if "*" in allowed:
        return True
    h = host_only(host)
    for a in allowed:
        if a.startswith("*.") and (h.endswith(a[1:]) and len(h) > len(a) - 1):
            return True
        if h == a:
            return True
    return False


def _error(status: int, code: str, message: str) -> tuple[int, bytes]:
    return status, json.dumps({"error": {"code": code, "message": message, "field_errors": [], "details": None}}).encode()


class SecurityMiddleware:
    """Pure ASGI middleware (also wraps the streaming endpoints without buffering them)."""

    def __init__(self, app, allowed_hosts: list[str], cors_origins: list[str], max_body: int = DEFAULT_MAX_BODY):
        self.app, self.allowed, self.cors, self.max_body = app, allowed_hosts, set(cors_origins), max_body

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
        method = scope.get("method", "GET")
        host = headers.get("host", "")
        refusal = None
        if not host_ok(host, self.allowed):
            refusal = _error(400, "HOST_NOT_ALLOWED", "This server doesn't answer to that host name. Add it to "
                                                       "OPTLAB_ALLOWED_HOSTS if you use it on purpose.")
        elif method in UNSAFE:
            origin = headers.get("origin")
            if headers.get("sec-fetch-site") == "cross-site" and (not origin or origin not in self.cors):
                refusal = _error(403, "CROSS_SITE_REQUEST", "Changes can't be made from another website.")
            elif origin and origin != "null" and origin not in self.cors:
                o = urlsplit(origin)
                oh = host_only(o.netloc) if o.hostname else ""
                fwd = host_only(headers.get("x-forwarded-host", "").split(",")[0]) if headers.get("x-forwarded-host") else ""
                via_proxy = host_only(host) in LOOPBACK or fwd == oh   # tailscale serve / a local reverse proxy
                same = oh and (oh == host_only(host) or via_proxy) and host_ok(o.netloc, self.allowed)
                if not same:
                    refusal = _error(403, "ORIGIN_NOT_ALLOWED", "Changes can't be made from another website.")
            elif origin == "null":
                refusal = _error(403, "ORIGIN_NOT_ALLOWED", "Changes can't be made from another website.")
        if refusal is None and method in UNSAFE:
            try:
                length = int(headers.get("content-length", "0") or 0)
            except ValueError:
                length = 0
            if length > self.max_body:
                refusal = _error(413, "BODY_TOO_LARGE", f"Request bodies are limited to {self.max_body // 1_000_000} MB.")
        if refusal is not None:
            status, body = refusal
            await send({"type": "http.response.start", "status": status,
                        "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode()), *HEADERS]})
            await send({"type": "http.response.body", "body": body})
            return

        received = 0
        max_body = self.max_body

        async def limited_receive():
            nonlocal received
            msg = await receive()
            if msg["type"] == "http.request":
                received += len(msg.get("body", b""))
                if received > max_body:                 # chunked bodies without a Content-Length
                    raise BodyTooLarge()
            return msg

        is_api = scope.get("path", "").startswith("/api/")

        started = False

        async def send_with_headers(msg):
            nonlocal started
            if msg["type"] == "http.response.start":
                started = True
                hs = [(k, v) for k, v in msg.get("headers", []) if k.lower() not in {h for h, _ in HEADERS}]
                hs += HEADERS
                if not is_api:
                    hs.append((b"content-security-policy", CSP.encode()))
                msg = {**msg, "headers": hs}
            await send(msg)

        try:
            await self.app(scope, limited_receive, send_with_headers)
        except BodyTooLarge:
            if started:
                return
            status, body = _error(413, "BODY_TOO_LARGE", f"Request bodies are limited to {self.max_body // 1_000_000} MB.")
            await send({"type": "http.response.start", "status": status,
                        "headers": [(b"content-type", b"application/json"), *HEADERS]})
            await send({"type": "http.response.body", "body": body})


class BodyTooLarge(Exception):
    pass
