"""Saved strategies (YAML files in strategies/) and saved searches (in the history archive).

A saved search keeps the exact strategy YAML, its version hash, every data report and a
fingerprint of the bars each symbol was searched on (count, first/last timestamp and a
hash of the closes). Re-running it later shows whether the data has changed since, so
two result sets are only compared when they really used the same inputs.
"""
from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from .bars import DEFAULT_ARCHIVE, Archive
from .strategy import load_yaml, parse_strategy

STRATEGY_DIR = Path(__file__).resolve().parents[3] / "strategies"


def slug(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")
    return s or "strategy"


def list_strategies(folder: Path = STRATEGY_DIR) -> dict[str, Path]:
    """{strategy name: file}, read from each file's name field."""
    out = {}
    for f in sorted(folder.glob("*.y*ml")):
        try:
            d = load_yaml(f.read_text()) or {}
            name = (d.get("historical_signal_search", d) or {}).get("name") or f.stem
        except Exception:
            name = f"{f.stem} (unreadable)"
        out[str(name)] = f
    return out


def save_strategy(text: str, folder: Path = STRATEGY_DIR, overwrite: bool = False, name: str | None = None) -> Path:
    """Validate, then write <slug of name>.yaml. Raises StrategyError if invalid, FileExistsError if taken."""
    data = load_yaml(text) or {}
    if name:
        body = data.get("historical_signal_search", data)
        body["name"] = name
        import yaml

        text = yaml.safe_dump(data, sort_keys=False, allow_unicode=True)
    s = parse_strategy(text)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{slug(s.name)}.yaml"
    if path.exists() and not overwrite:
        raise FileExistsError(f"{path.name} already exists")
    path.write_text(text)
    return path


def duplicate_strategy(path: Path, new_name: str, folder: Path = STRATEGY_DIR) -> Path:
    return save_strategy(Path(path).read_text(), folder, name=new_name)


def fingerprint(bars: pd.DataFrame) -> dict:
    closes = bars["close"].round(6).to_numpy().tobytes() if len(bars) else b""
    return {"bars": int(len(bars)), "first": str(bars.index[0]) if len(bars) else None,
            "last": str(bars.index[-1]) if len(bars) else None, "closes_sha": hashlib.sha256(closes).hexdigest()[:16]}


def save_search(result, strategy_yaml: str, archive_path=DEFAULT_ARCHIVE) -> str:
    sid = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]
    s = result.strategy
    meta = {"strategy_version": s.version, "symbols": s.symbols, "timeframe": s.timeframe, "start": s.start,
            "end": s.end, "timezone": s.timezone, "source": s.source, "seconds": round(result.seconds, 2),
            "bars_evaluated": result.bars_evaluated, "not_evaluable": result.not_evaluable,
            "total_matches": result.total_matches, "errors": result.errors,
            "reports": {k: r.as_dict() for k, r in result.reports.items()},
            "fingerprints": {k: fingerprint(b) for k, b in result.bars.items()}}
    with Archive(archive_path) as a:
        a.con.execute("INSERT INTO signal_searches VALUES (?, ?, ?, ?, ?, ?, ?)",
                      [sid, datetime.now(timezone.utc).replace(tzinfo=None), s.name, s.version, strategy_yaml,
                       json.dumps(meta, default=str), int(len(result.results))])
        df = result.results
        if not df.empty:
            rows = pd.DataFrame({"search_id": sid, "symbol": df["symbol"], "ts": df["timestamp"].astype(str),
                                 "match_type": df["match_type"], "score": df["score"],
                                 "row_json": df.assign(timestamp=df["timestamp"].astype(str)).apply(
                                     lambda r: r.to_json(default_handler=str), axis=1)})
            a.con.register("_r", rows)
            a.con.execute("INSERT INTO signal_results SELECT * FROM _r")
            a.con.unregister("_r")
    return sid


def list_searches(archive_path=DEFAULT_ARCHIVE) -> pd.DataFrame:
    with Archive(archive_path) as a:
        return a.con.execute("SELECT search_id, created_at, strategy_name, strategy_version, results "
                             "FROM signal_searches ORDER BY created_at DESC").df()


def load_search(search_id: str, archive_path=DEFAULT_ARCHIVE) -> tuple[str, dict, pd.DataFrame]:
    """(strategy YAML, metadata, results) of a saved search."""
    with Archive(archive_path) as a:
        row = a.con.execute("SELECT strategy_yaml, metadata FROM signal_searches WHERE search_id = ?",
                            [search_id]).fetchone()
        if row is None:
            raise KeyError(search_id)
        res = a.con.execute("SELECT row_json FROM signal_results WHERE search_id = ?", [search_id]).df()
    results = pd.DataFrame([json.loads(j) for j in res["row_json"]]) if len(res) else pd.DataFrame()
    return row[0], json.loads(row[1]), results


def compare_to_saved(result, saved_meta: dict) -> list[str]:
    """Differences between a fresh run and a saved one, so results are compared honestly."""
    notes = []
    if result.strategy.version != saved_meta.get("strategy_version"):
        notes.append("The strategy has changed since the saved search (different version).")
    for sym, fp in saved_meta.get("fingerprints", {}).items():
        now = fingerprint(result.bars[sym]) if sym in result.bars else None
        if now != fp:
            notes.append(f"{sym}: the bars differ from the saved search ({fp['bars']} then, "
                         f"{now['bars'] if now else 0} now), e.g. new data was fetched or the archive changed.")
    return notes
