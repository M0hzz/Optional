"""Load config.yaml from the project root (or OPTLAB_CONFIG)."""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[2]


@lru_cache(maxsize=1)
def load_config(path: str | None = None) -> dict:
    cfg_path = Path(path or os.environ.get("OPTLAB_CONFIG", PROJECT_ROOT / "config.yaml"))
    with open(cfg_path) as fh:
        cfg = yaml.safe_load(fh)
    db = Path(cfg["data"]["db_path"])
    if not db.is_absolute():
        cfg["data"]["db_path"] = str(PROJECT_ROOT / db)
    return cfg
