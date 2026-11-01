"""Which symbols an item is about, how that was decided, and how relevant the item is to each (0..1).

    filer      the SEC filer's CIK maps to the ticker                              1.00
    company    the item comes from the company's own investor-relations feed       0.95
    exchange   "(NYSE: XYZ)" or "Nasdaq: XYZ" in the text                          0.90
    provider   the provider tagged the symbol: 0.85 when the headline also names it, else 0.60
    cashtag    "$XYZ" in the text                                                  0.80
    name       the company's name (from SEC's ticker list) in the headline 0.70, in the summary 0.50
               (followed symbols only, to keep false matches out)
    market     macro and central-bank releases are about the whole market: symbol _MARKET, 1.00
A symbol matched several ways keeps its highest relevance and every piece of evidence.
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from pathlib import Path

MARKET = "_MARKET"
TICKER = re.compile(r"^[A-Z]{1,5}([.-][A-Z]{1,2})?$")
EXCHANGE = re.compile(r"\b(?:NYSE(?: American| Arca)?|NASDAQ|Nasdaq|NYSEAMERICAN|AMEX|Cboe|CBOE|OTC(?:QX|QB)?)\s*:\s*\$?([A-Z]{1,5}(?:[.-][A-Z]{1,2})?)\b")
CASHTAG = re.compile(r"(?<![\w$])\$([A-Z]{1,5}(?:\.[A-Z])?)\b")
SUFFIX = re.compile(r"\b(inc|incorporated|corp|corporation|co|company|ltd|limited|plc|llc|lp|l\.p|n\.v|s\.a|ag|se|holdings?"
                    r"|group|class [a-c]|the|trust|fund|etf|/[a-z]{2})\b\.?", re.I)


@dataclass
class Entity:
    symbol: str
    cik: str | None
    method: str
    relevance: float
    evidence: str


class SymbolMap:
    """Tickers, CIKs and company names from SEC's company_tickers.json (cached on disk)."""

    def __init__(self, rows: list[dict] | None = None):
        self.by_ticker: dict[str, tuple[str, str]] = {}
        self.by_cik: dict[str, list[str]] = {}
        for r in rows or []:
            t, cik = str(r.get("ticker", "")).upper(), str(r.get("cik_str", "")).zfill(10)
            if not t:
                continue
            self.by_ticker.setdefault(t, (cik, str(r.get("title", ""))))
            self.by_cik.setdefault(cik, []).append(t)

    def __len__(self) -> int:
        return len(self.by_ticker)

    def known(self, t: str) -> bool:
        return t in self.by_ticker if self.by_ticker else bool(TICKER.match(t))

    def cik(self, t: str) -> str | None:
        return self.by_ticker.get(t, (None, ""))[0]

    def name(self, t: str) -> str | None:
        return self.by_ticker.get(t, (None, None))[1] or None

    @classmethod
    def from_json(cls, data: bytes) -> SymbolMap:
        d = json.loads(data)
        return cls(list(d.values()) if isinstance(d, dict) else list(d))

    @classmethod
    def cached(cls, path: Path, max_age_days: float = 7) -> tuple[SymbolMap | None, bool]:
        """(map, fresh). Missing file → (None, False)."""
        if not path.exists():
            return None, False
        try:
            m = cls.from_json(path.read_bytes())
        except (OSError, ValueError):
            return None, False
        return m, (time.time() - path.stat().st_mtime) < max_age_days * 86400


def short_name(title: str) -> str | None:
    """'NVIDIA CORP' -> 'nvidia'; 'JPMORGAN CHASE & CO' -> 'jpmorgan chase'. None when too short to match safely."""
    n = SUFFIX.sub(" ", title.lower())
    n = re.sub(r"[^a-z0-9& ]+", " ", n)
    n = re.sub(r"\s+", " ", n).strip(" &")
    return n if len(n) >= 4 else None


def _name_hit(name: str, text: str) -> str | None:
    rx = re.compile(r"(?<![A-Za-z])" + r"\s+".join(map(re.escape, name.split())) + r"(?![A-Za-z])", re.I)
    for m in rx.finditer(text or ""):
        if m.group(0)[0].isupper():             # a proper noun, not the common word
            return m.group(0)
    return None


def map_entities(title: str, summary: str | None, smap: SymbolMap, followed: set[str], *, filer_cik: str | None = None,
                 provider_symbols: list[str] | None = None, company_symbol: str | None = None,
                 market: bool = False) -> list[Entity]:
    found: dict[str, Entity] = {}

    def add(sym: str, method: str, rel: float, ev: str) -> None:
        sym = sym.upper()
        cur = found.get(sym)
        if cur is None:
            found[sym] = Entity(sym, smap.cik(sym), method, rel, ev)
        else:
            if rel > cur.relevance:
                cur.relevance, cur.method = rel, method
            if ev not in cur.evidence:
                cur.evidence = f"{cur.evidence}; {ev}"

    text = f"{title}\n{summary or ''}"
    if market:
        add(MARKET, "market", 1.0, "macro or central-bank release")
    if filer_cik:
        for t in smap.by_cik.get(str(filer_cik).zfill(10), []):
            add(t, "filer", 1.0, f"SEC filer CIK {int(str(filer_cik))}")
    if company_symbol:
        add(company_symbol, "company", 0.95, "the company's own feed")
    for m in EXCHANGE.finditer(text):
        if smap.known(m.group(1)):
            add(m.group(1), "exchange", 0.9, m.group(0))
    for m in CASHTAG.finditer(text):
        if smap.known(m.group(1)):
            add(m.group(1), "cashtag", 0.8, m.group(0))
    for t in provider_symbols or []:
        t = t.strip().upper()
        if not t or not TICKER.match(t):
            continue
        nm = short_name(smap.name(t) or "")
        in_title = re.search(rf"(?<![A-Za-z]){re.escape(t)}(?![A-Za-z])", title) or (nm and _name_hit(nm, title))
        add(t, "provider", 0.85 if in_title else 0.6, "tagged by the provider" + (" and named in the headline" if in_title else ""))
    for t in followed:
        nm = short_name(smap.name(t) or "")
        if not nm:
            continue
        if (hit := _name_hit(nm, title)):
            add(t, "name", 0.7, f"'{hit}' in the headline")
        elif (hit := _name_hit(nm, summary or "")):
            add(t, "name", 0.5, f"'{hit}' in the summary")
    return sorted(found.values(), key=lambda e: (-e.relevance, e.symbol))
