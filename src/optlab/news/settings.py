"""News settings from config.yaml (`news:` section) and the environment. Every setting has a default."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from ..config import PROJECT_ROOT

PLACEHOLDER_IDENTITY = "Your Name your.email@example.com"
DEFAULT_BLS_FEEDS = ["cpi", "empsit", "ppi", "jolts", "eci", "realer", "prod2", "ximpim"]
# 424B2 / 424B3 are left out: banks file hundreds of structured-note prospectuses under them every week.
DEFAULT_SEC_FORMS = ["8-K", "8-K/A", "10-Q", "10-K", "S-1", "S-3", "424B1", "424B4", "424B5", "SC 13D", "SC TO-T", "6-K"]
SEC_PER_FORM_CAP = 5
DEFAULT_CREDIBILITY = {"sec_edgar": 0.95, "federal_reserve": 0.95, "bls": 0.95, "bea": 0.95, "company_ir": 0.85,
                       "finnhub": 0.7}


@dataclass
class NewsSettings:
    enabled: bool = True
    interval_minutes: int = 15
    identity: str | None = None                  # SEC fair-access contact: "Name email"; required for EDGAR
    sec_lookback_days: int = 14
    sec_forms: list[str] = field(default_factory=lambda: list(DEFAULT_SEC_FORMS))
    sec_submissions_every_minutes: int = 60
    bls_feeds: list[str] = field(default_factory=lambda: list(DEFAULT_BLS_FEEDS))
    ir_feeds: dict[str, list[str]] = field(default_factory=dict)  # SYMBOL -> RSS/Atom URLs of the company's IR feed
    finnhub_key: str | None = None
    finnhub_lookback_days: int = 3
    credibility: dict[str, float] = field(default_factory=lambda: dict(DEFAULT_CREDIBILITY))
    disabled_sources: list[str] = field(default_factory=list)
    extra_symbols: list[str] = field(default_factory=list)
    lexicon_path: Path | None = None
    cache_dir: Path = PROJECT_ROOT / "data" / "news"
    timeout_seconds: float = 20.0
    max_items_per_source: int = 200

    @property
    def identity_ok(self) -> bool:
        i = (self.identity or "").strip()
        return bool(i) and i != PLACEHOLDER_IDENTITY and "@" in i

    @property
    def user_agent(self) -> str:
        return f"optlab/1.0 ({self.identity.strip()})" if self.identity_ok and self.identity else "optlab/1.0 (personal research)"


def load(cfg: dict | None = None) -> NewsSettings:
    if cfg is None:
        try:
            from ..config import load_config

            cfg = load_config()
        except (OSError, KeyError, TypeError):
            cfg = {}
    n = dict(cfg.get("news") or {})
    st = NewsSettings()
    st.enabled = bool(n.get("enabled", True))
    st.interval_minutes = max(5, int(n.get("interval_minutes", 15)))
    st.identity = os.environ.get("OPTLAB_SEC_IDENTITY") or (cfg.get("edgar") or {}).get("identity")
    sec = n.get("sec") or {}
    st.sec_lookback_days = int(sec.get("lookback_days", st.sec_lookback_days))
    st.sec_forms = list(sec.get("forms", st.sec_forms))
    st.sec_submissions_every_minutes = int(sec.get("submissions_every_minutes", st.sec_submissions_every_minutes))
    st.bls_feeds = list((n.get("bls") or {}).get("feeds", st.bls_feeds))
    st.ir_feeds = {str(k).upper(): ([v] if isinstance(v, str) else list(v)) for k, v in (n.get("company_ir") or {}).items()}
    st.finnhub_key = os.environ.get("FINNHUB_API_KEY") or None       # never read from config.yaml (no secrets in files)
    st.finnhub_lookback_days = int((n.get("finnhub") or {}).get("lookback_days", st.finnhub_lookback_days))
    st.credibility.update({k: float(v) for k, v in (n.get("credibility") or {}).items()})
    st.disabled_sources = list(n.get("disabled_sources") or [])
    st.extra_symbols = [str(x).upper() for x in (n.get("symbols") or [])]
    if n.get("lexicon_path"):
        p = Path(n["lexicon_path"])
        st.lexicon_path = p if p.is_absolute() else PROJECT_ROOT / p
    return st
