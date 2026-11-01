"""Seed a scratch database at a realistic-to-heavy scale and time the API endpoints the dashboard calls.

    python scripts/benchmark.py                       # default scale, prints a table
    python scripts/benchmark.py --positions 400 --events 20000 --news 20000 --days 365 --runs 5

Uses synthetic quotes and a temporary database (never data/optlab.sqlite). Nothing touches the network.
"""
from __future__ import annotations

import argparse
import random
import statistics
import sys
import tempfile
import time
from datetime import timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def seed(db, n_positions: int, n_events: int, n_news: int, days: int, n_journal: int) -> str:
    from optlab.portfolio.ids import new_id
    from optlab.portfolio.models import (Account, AssetType, Event, JournalEntry, Lifecycle, NewsArticle, NewsEntity,
                                         NewsSource, OptionRight, Position, PositionLeg, Side, TradeReview, utcnow)

    rnd = random.Random(7)
    now = utcnow()
    syms = ["SPY", "QQQ", "IWM", "AAPL", "MSFT", "NVDA", "AMZN", "JPM", "XYZ", "ACME"]
    with db.session(write=True) as s:
        acc = Account(name="Bench", is_default=True, created_at=now - timedelta(days=days))
        s.add(acc)
        s.flush()
        for i in range(n_positions):
            opened = now - timedelta(days=rnd.randint(0, days), hours=rnd.randint(0, 6))
            closed = None if i % 8 == 0 else opened + timedelta(days=rnd.randint(1, 30))
            if closed and closed > now:
                closed = None
            p = Position(account_id=acc.id, name=f"P{i}", underlying=rnd.choice(syms), strategy="PUT_CREDIT_SPREAD",
                         opened_at=opened, closed_at=closed, lifecycle=Lifecycle.CLOSED if closed else Lifecycle.OPEN,
                         realized_pnl_units=rnd.randint(-5_000_000, 5_000_000) if closed else None)
            exp = (opened + timedelta(days=40)).date()
            p.legs = [PositionLeg(leg_index=0, asset_type=AssetType.OPTION, side=Side.SELL, quantity=1, multiplier=100,
                                  option_right=OptionRight.PUT, strike_units=950_000, expiration=exp, fill_price_units=11_000),
                      PositionLeg(leg_index=1, asset_type=AssetType.OPTION, side=Side.BUY, quantity=1, multiplier=100,
                                  option_right=OptionRight.PUT, strike_units=900_000, expiration=exp, fill_price_units=1_500)]
            s.add(p)
            if closed and i % 3 == 0:
                s.flush()
                s.add(TradeReview(position_id=p.id, account_id=acc.id, followed_plan="no_plan", exit_reason="target",
                                  exit_followed_plan="no_plan", lessons="ok", completed_at=closed + timedelta(days=1),
                                  mistakes_json=[], metadata_json={}))
        for i in range(n_journal):
            d = (now - timedelta(days=rnd.randint(0, days))).date()
            s.add(JournalEntry(account_id=acc.id, kind=rnd.choice(["daily_thesis", "eod_review", "note"]), entry_date=d,
                               body="text " * 20, tags_json=[], metadata_json={}))
        for i in range(n_events):
            s.add(Event(type=rnd.choice(["STATUS_CHANGE", "ALERT", "NEWS"]), severity=rnd.choice(["info", "warning"]),
                        title=f"event {i}", message="m" * 80, symbol=rnd.choice(syms), account_id=acc.id,
                        occurred_at=now - timedelta(minutes=rnd.randint(0, days * 1440)), source_json={}))
        s.add(NewsSource(code="bench", name="Bench", kind="news", credibility=0.8, status="ok", last_success_at=now))
        s.flush()
        for i in range(n_news):
            pub = now - timedelta(minutes=rnd.randint(0, days * 1440))
            a = NewsArticle(id=new_id(), source_code="bench", external_id=f"b{i}", title=f"Headline {i} about {rnd.choice(syms)}",
                            summary="Summary text.", summary_origin="provider", published_at=pub, first_seen_at=pub,
                            fingerprint=f"{i:032x}", category=rnd.choice(["earnings", "other", "macro_release"]),
                            category_confidence=0.6, classification_json={"importance": "medium"}, raw_json={})
            s.add(a)
            s.add(NewsEntity(article_id=a.id, symbol=rnd.choice(syms), method="provider", relevance=0.8, evidence="x"))
        return acc.id


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--positions", type=int, default=300)
    ap.add_argument("--events", type=int, default=20_000)
    ap.add_argument("--news", type=int, default=10_000)
    ap.add_argument("--journal", type=int, default=500)
    ap.add_argument("--days", type=int, default=365)
    ap.add_argument("--runs", type=int, default=5)
    a = ap.parse_args()

    import logging

    logging.disable(logging.INFO)
    from fastapi.testclient import TestClient

    from optlab.api.main import create_app
    from optlab.portfolio.db import Database, upgrade
    from optlab.portfolio.quotes import QuoteService, SyntheticProvider

    tmp = tempfile.mkdtemp(prefix="optlab-bench-")
    url = f"sqlite:///{tmp}/bench.sqlite"
    upgrade(url)
    db = Database(url)
    t0 = time.time()
    acc = seed(db, a.positions, a.events, a.news, a.days, a.journal)
    print(f"seeded in {time.time() - t0:.1f}s: {a.positions} positions, {a.events} events, {a.news} news, "
          f"{a.journal} journal entries over {a.days} days ({tmp})")
    app = create_app(db, quotes=QuoteService(SyntheticProvider(), mode="synthetic"), web_dist=None, monitor=False,
                     news=False, allowed_hosts=["testserver"])
    eps = [
        ("overview", f"/api/v1/accounts/{acc}/overview"),
        ("positions (page)", f"/api/v1/positions?account_id={acc}&page_size=50"),
        ("positions (open)", f"/api/v1/positions?account_id={acc}&lifecycle=OPEN&page_size=200"),
        ("events (50)", f"/api/v1/events?account_id={acc}&limit=50"),
        ("events unacked", f"/api/v1/events?account_id={acc}&unacknowledged=true&limit=50"),
        ("progress (refresh)", f"/api/v1/accounts/{acc}/progress"),
        ("process-score", f"/api/v1/accounts/{acc}/process-score"),
        ("score history 30d", f"/api/v1/accounts/{acc}/process-score/history?days=30"),
        ("ledger", f"/api/v1/accounts/{acc}/ledger"),
        ("risk dashboard", f"/api/v1/accounts/{acc}/risk"),
        ("news (held)", "/api/v1/news?held=true&limit=30"),
        ("news (all)", "/api/v1/news?limit=30"),
        ("news (symbol)", "/api/v1/news?symbol=AAPL&limit=30"),
        ("journal", f"/api/v1/journal?account_id={acc}&limit=100"),
        ("reviews pending", f"/api/v1/accounts/{acc}/reviews/pending"),
    ]
    rows = []
    with TestClient(app) as c:
        for name, path in eps:
            times, status = [], None
            for i in range(a.runs + 1):
                t = time.perf_counter()
                r = c.get(path)
                dt = (time.perf_counter() - t) * 1000
                status = r.status_code
                if i:                                   # the first call warms caches
                    times.append(dt)
            rows.append((name, status, statistics.median(times), max(times)))
    db.dispose()
    print(f"\n{'endpoint':22} {'status':>6} {'median ms':>10} {'max ms':>8}")
    for name, st, med, mx in rows:
        print(f"{name:22} {st:>6} {med:>10.1f} {mx:>8.1f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
