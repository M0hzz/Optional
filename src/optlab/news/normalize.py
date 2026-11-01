"""Text clean-up, canonical URLs, opening sentences and headline fingerprints. Deterministic."""
from __future__ import annotations

import hashlib
import html
import re
import unicodedata
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from bs4 import BeautifulSoup

TRACKING = re.compile(r"^(utm_|fbclid$|gclid$|mc_|ref$|cmpid$|ncid$|guccounter$)", re.I)
STOP = frozenset("a an and the of to in on for at by with from as is are was were be its it this that inc corp co "
                 "ltd plc llc says said reports report announces announced amid after over new".split())
MAX_TITLE, MAX_SUMMARY = 500, 1200


def clean_text(s: str | None, limit: int = MAX_SUMMARY) -> str | None:
    """Plain text from HTML or text: tags removed, entities decoded, whitespace collapsed, control chars dropped."""
    if not s:
        return None
    if "<" in s and ">" in s:
        soup = BeautifulSoup(s, "html.parser")
        for tag in soup(["script", "style", "iframe", "object", "noscript"]):
            tag.decompose()
        s = soup.get_text(" ")
    s = html.unescape(s)
    s = "".join(ch for ch in s if unicodedata.category(ch)[0] != "C" or ch in " ")
    s = re.sub(r"\s+", " ", s).strip()
    if not s:
        return None
    return s if len(s) <= limit else s[: limit - 1].rsplit(" ", 1)[0] + "…"


def safe_url(u: str | None) -> str | None:
    """Only http(s) URLs are kept (anything else, e.g. javascript:, is dropped)."""
    if not u:
        return None
    u = u.strip()
    parts = urlsplit(u)
    if parts.scheme.lower() not in ("http", "https") or not parts.netloc:
        return None
    return u


def canonical_url(u: str | None) -> str | None:
    """Lower-case scheme and host, no fragment, no tracking parameters, no trailing slash."""
    u = safe_url(u)
    if u is None:
        return None
    p = urlsplit(u)
    q = urlencode([(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True) if not TRACKING.match(k)])
    path = p.path.rstrip("/") or "/"
    return urlunsplit(("https" if p.scheme.lower() in ("http", "https") else p.scheme.lower(), p.netloc.lower(), path, q, ""))


def opening_sentences(text: str | None, n: int = 2, limit: int = 300) -> str | None:
    """The first n sentences of the item's own text (no generated wording)."""
    t = clean_text(text, 5000)
    if not t:
        return None
    parts = re.split(r"(?<=[.!?])\s+(?=[A-Z0-9\"“(])", t)
    out = " ".join(parts[:n]).strip()
    return out if len(out) <= limit else out[: limit - 1].rsplit(" ", 1)[0] + "…"


def tokens(title: str) -> list[str]:
    t = unicodedata.normalize("NFKD", title.lower())
    return [w for w in re.findall(r"[a-z0-9]+", t) if w not in STOP and len(w) > 1]


def fingerprint(title: str) -> str:
    """Order-insensitive hash of the headline's significant words (exact-duplicate headlines)."""
    return hashlib.sha256(" ".join(sorted(set(tokens(title)))).encode()).hexdigest()[:32]


def numbers(title: str) -> frozenset[str]:
    """The figures in a headline (0.6%, 3rd, 2026, $1.2 billion): different figures mean a different story."""
    return frozenset(re.findall(r"\d+(?:[.,]\d+)*", title))


def similarity(a: str, b: str) -> float:
    """Jaccard similarity of headline word sets plus 2-word shingles (0..1)."""
    ta, tb = tokens(a), tokens(b)
    sa = set(ta) | {f"{x} {y}" for x, y in zip(ta, ta[1:])}
    sb = set(tb) | {f"{x} {y}" for x, y in zip(tb, tb[1:])}
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)
