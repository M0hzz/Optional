"""News and official-event sources (Phase 5).

    python scripts/news.py status                              # sources, last fetch, dictionary, counts
    python scripts/news.py install-lexicon --accept-license    # download the Loughran–McDonald dictionary
    python scripts/news.py install-lexicon --file <csv> --accept-license   # or install a copy you downloaded
    python scripts/news.py fetch                               # one ingest run now (if no API is fetching)
    python scripts/news.py rescore                             # score items that have no score yet

The API fetches news on its own every news.interval_minutes (OPTLAB_NEWS=on, the default); `fetch` is for
a manual run and refuses while another process holds the news lock.
"""
from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from optlab.news import sentiment as ns  # noqa: E402
from optlab.news.settings import load  # noqa: E402
from optlab.portfolio import db as dbm  # noqa: E402


def install(args) -> int:
    print(ns.LM_LICENSE)
    if not args.accept_license:
        print("\nRe-run with --accept-license to confirm that your use fits these terms.")
        return 2
    st = load()
    dest = Path(args.dest) if args.dest else (st.lexicon_path or ns.DEFAULT_PATH)
    if args.file:
        data = Path(args.file).read_bytes()
    else:
        from optlab.news.http import FetchError, http_get

        print(f"Downloading from the official link on {ns.LM_PAGE} ...")
        try:
            data = http_get(ns.LM_URL, {"User-Agent": st.user_agent}, timeout=120, max_bytes=40_000_000)
        except FetchError as exc:
            print(f"Download failed: {exc}. Download the CSV from {ns.LM_PAGE} and use --file.")
            return 1
    try:
        lex = ns.Lexicon.from_csv(data, dest.name)
    except ValueError as exc:
        print(f"Not installed: {exc}")
        return 1
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(data)
    print(f"Installed {dest} (sha256 {hashlib.sha256(data).hexdigest()[:16]}…): "
          + ", ".join(f"{k} {len(v)}" for k, v in lex.lists.items()))
    print(f"Model version: {ns.LoughranMcDonald(lex).version}. Existing items are scored on the next run "
          "(or `python scripts/news.py rescore`).")
    return 0


def status(_args) -> int:
    from sqlalchemy import func, select

    from optlab.news.providers import ALL
    from optlab.portfolio.models import NewsArticle, NewsSource, SentimentScore

    st = load()
    model, note = ns.default_model(st.lexicon_path)
    print(f"dictionary: {model.version if model else 'not installed'}" + (f" — {note}" if note else ""))
    print(f"SEC identity: {'set' if st.identity_ok else 'NOT SET (edgar.identity in config.yaml)'}; "
          f"Finnhub: {'key set' if st.finnhub_key else 'off (no FINNHUB_API_KEY)'}; every {st.interval_minutes} min")
    db = dbm.Database()
    with db.session() as s:
        rows = {r.code: r for r in s.scalars(select(NewsSource))}
        counts = dict(s.execute(select(NewsArticle.source_code, func.count()).group_by(NewsArticle.source_code)).all())
        scored = s.scalar(select(func.count(func.distinct(SentimentScore.article_id))))
        for p in ALL:
            r = rows.get(p.code)
            ok, why = p.available(st)
            print(f"  {p.code:16} {('on' if (r is None or r.enabled) else 'off'):3} {r.status if r else 'never':11} "
                  f"items {counts.get(p.code, 0):5}  last ok {r.last_success_at if r and r.last_success_at else '—'}"
                  + (f"  ({why})" if not ok else (f"  ({r.status_detail[:90]})" if r and r.status_detail else "")))
        print(f"articles {sum(counts.values())}, scored {scored}")
    db.dispose()
    return 0


def fetch(_args) -> int:
    from optlab.news.pipeline import ingest
    from optlab.news.runner import NewsLock

    db = dbm.Database()
    dbm.upgrade(db.url)
    lk = NewsLock(db.url)
    if not lk.try_acquire():
        print(f"Another process fetches news for this database (pid {lk.holder() or '?'}); use the dashboard's Refresh.")
        return 1
    try:
        r = ingest(db, load())
    finally:
        lk.release()
        db.dispose()
    for x in r.sources:
        print(f"  {x.code:16} {x.status:11} items {x.items:4} new {x.new:4} duplicates {x.duplicates:3} scheduled {x.scheduled:3}"
              + (f"  {x.detail[:100]}" if x.detail else ""))
    print(f"{r.new_articles} new, {r.rescored} rescored, {r.events} events; model {r.model or 'none'}")
    return 0


def rescore(_args) -> int:
    from optlab.news.pipeline import rescore as do

    st = load()
    model, note = ns.default_model(st.lexicon_path)
    if model is None:
        print(note)
        return 1
    db = dbm.Database()
    with db.session(write=True) as s:
        n = do(s, model)
    db.dispose()
    print(f"scored {n} items with {model.version}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    i = sub.add_parser("install-lexicon")
    i.add_argument("--accept-license", action="store_true")
    i.add_argument("--file")
    i.add_argument("--dest")
    sub.add_parser("fetch")
    sub.add_parser("rescore")
    a = ap.parse_args()
    return {"status": status, "install-lexicon": install, "fetch": fetch, "rescore": rescore}[a.cmd](a)


if __name__ == "__main__":
    raise SystemExit(main())
