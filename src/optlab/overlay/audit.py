"""Audit trail: every overlay verdict can be reproduced from its record.

Each record holds the rule, configuration and indicator versions, the data source and
timestamp, the scan time, the input values (each condition's observed value and requirement),
the outputs (status, score, counts), the formula and the reason code. Only state changes are
appended (a record is written when (rule, symbol, timeframe, bar, status) differs from the
last one written), so the log grows with events, not with page refreshes.

Nothing from config.yaml is written, so credentials such as the EDGAR identity never reach the log.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pandas as pd

from ..config import PROJECT_ROOT
from .engine import OverlayScan

AUDIT_PATH = PROJECT_ROOT / "data" / "overlay" / "audit.jsonl"


def record(r, scan_time: datetime) -> dict:
    return {"scan_time": scan_time.isoformat(timespec="seconds"), "symbol": r.symbol, "rule_id": r.rule_id,
            "rule_name": r.rule_name, "timeframe": r.timeframe, "bar": r.data_timestamp, "status": r.status,
            "reason_code": r.reason_code, "score": r.score, "passed": r.passed, "near": r.near, "failed": r.failed,
            "unavailable": r.unavailable, "transition": r.transition or None, "data_source": r.data_source,
            "data_flags": r.data_flags, "versions": r.versions, "formula": r.formula,
            "inputs": {c["id"]: {"expression": c["expression"], "observed": c["observed"], "requirement": c["required_value"],
                                 "status": c["status"], "points": c["points"]} for c in r.conditions},
            "explanation": r.explanation}


def append(sc: OverlayScan, path: Path = AUDIT_PATH) -> int:
    """Append records for results whose state changed since the last record. Returns how many were written."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    state_path = path.with_suffix(".state.json")
    last = json.loads(state_path.read_text()) if state_path.exists() else {}
    out = []
    for r in sc.results:
        key = f"{r.rule_id}|{r.symbol}|{r.timeframe}"
        sig = f"{r.data_timestamp}|{r.status}|{sc.versions.get('config')}"
        if last.get(key) == sig:
            continue
        last[key] = sig
        out.append(json.dumps(record(r, sc.now), default=str, ensure_ascii=False))
    if out:
        with path.open("a") as fh:
            fh.write("\n".join(out) + "\n")
        state_path.write_text(json.dumps(last))
    return len(out)


def read(path: Path = AUDIT_PATH, limit: int = 2000) -> pd.DataFrame:
    path = Path(path)
    if not path.exists():
        return pd.DataFrame()
    lines = path.read_text().splitlines()[-limit:]
    return pd.DataFrame([json.loads(x) for x in lines if x.strip()])
