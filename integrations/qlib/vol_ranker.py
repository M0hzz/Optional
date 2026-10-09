"""Qlib: learn which underlyings' volatility is about to fall (best to sell premium on).

Label:    forward 21-day realized vol minus trailing 20-day realized vol.
          Most negative = vol about to compress = best short-premium candidate.
Features: realized vol at several windows, vol ratios, returns, drawdown, volume,
          plus Qlib's Alpha158 if --alpha158 is passed.
Model:    LightGBM, trained on a rolling split, evaluated by rank IC.

Setup (Qlib does not support Python 3.13 yet; use a 3.10-3.12 environment):
    pip install pyqlib lightgbm
    # 1. Export prices from the optlab store into Qlib's binary format
    python integrations/qlib/vol_ranker.py export --out ~/.qlib/optlab_csv
    python -m qlib.cli.dump_bin dump_all --data_path ~/.qlib/optlab_csv \
        --qlib_dir ~/.qlib/qlib_data/optlab --include_fields open,high,low,close,volume --date_field_name date
    #    (older Qlib: python scripts/dump_bin.py from the Qlib repo, same arguments)
    # 2. Train and rank
    python integrations/qlib/vol_ranker.py train --provider ~/.qlib/qlib_data/optlab
    # For a broad universe, use Qlib's own US data instead:
    #    python -m qlib.cli.data qlib_data --target_dir ~/.qlib/qlib_data/us_data --region us
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

RET = "($close/Ref($close,1)-1)"
FEATURES = {
    "rv5": f"Std({RET}, 5)",
    "rv10": f"Std({RET}, 10)",
    "rv20": f"Std({RET}, 20)",
    "rv60": f"Std({RET}, 60)",
    "rv10_20": f"Std({RET}, 10)/Std({RET}, 20)",
    "rv20_60": f"Std({RET}, 20)/Std({RET}, 60)",
    "vol_of_vol": f"Std(Std({RET}, 20), 20)/Mean(Std({RET}, 20), 20)",
    "ret5": "$close/Ref($close,5)-1",
    "ret20": "$close/Ref($close,20)-1",
    "ret60": "$close/Ref($close,60)-1",
    "dd252": "$close/Max($high,252)-1",
    "range20": "Mean(($high-$low)/$close, 20)",
    "volu_ratio": "Mean($volume,5)/Mean($volume,60)",
    "rv_rank": f"(Std({RET},20)-Min(Std({RET},20),252))/(Max(Std({RET},20),252)-Min(Std({RET},20),252)+1e-12)",
}
LABEL = f"Ref(Std({RET}, 21), -21) - Std({RET}, 20)"


def export(out: str) -> None:
    import polars as pl

    from optlab.config import load_config
    from optlab.data import Store

    cfg = load_config()
    out_dir = Path(out).expanduser()
    out_dir.mkdir(parents=True, exist_ok=True)
    with Store(cfg["data"]["db_path"], read_only=True) as store:
        for sym in store.symbols():
            df = store.prices(sym).select(["date", "open", "high", "low", "close", "volume"])
            df.with_columns(pl.col("date").cast(pl.Utf8)).write_csv(out_dir / f"{sym.lower()}.csv")
            print(f"wrote {sym}: {df.height} rows")


def train(provider: str, alpha158: bool, test_start: str, out: str) -> None:
    import qlib
    from qlib.contrib.model.gbdt import LGBModel
    from qlib.data.dataset import DatasetH
    from qlib.data.dataset.handler import DataHandlerLP
    from qlib.data.dataset.processor import CSZScoreNorm, DropnaLabel, Fillna
    from qlib.contrib.eva.alpha import calc_ic

    qlib.init(provider_uri=str(Path(provider).expanduser()), region="us")

    fields, names = list(FEATURES.values()), list(FEATURES.keys())
    if alpha158:
        from qlib.contrib.data.loader import Alpha158DL
        a_fields, a_names = Alpha158DL.get_feature_config()
        fields, names = fields + a_fields, names + a_names

    handler = DataHandlerLP(
        instruments="all", start_time="2018-01-01", end_time=None,
        data_loader={"class": "QlibDataLoader", "kwargs": {
            "config": {"feature": (fields, names), "label": ([LABEL], ["LABEL0"])}}},
        infer_processors=[{"class": "Fillna", "kwargs": {"fields_group": "feature"}}],
        learn_processors=[DropnaLabel(), CSZScoreNorm(fields_group="label"), Fillna(fields_group="feature")],
    )
    import pandas as pd

    t0 = pd.Timestamp(test_start)
    dataset = DatasetH(handler, segments={
        "train": ("2018-06-01", str((t0 - pd.Timedelta(days=200)).date())),
        "valid": (str((t0 - pd.Timedelta(days=180)).date()), str((t0 - pd.Timedelta(days=30)).date())),
        "test": (test_start, None),
    })
    model = LGBModel(loss="mse", num_leaves=31, learning_rate=0.03, max_depth=6,
                     colsample_bytree=0.8, subsample=0.8, lambda_l1=1.0, lambda_l2=5.0)
    model.fit(dataset)
    pred = model.predict(dataset, segment="test")
    label = dataset.prepare("test", col_set="label", data_key=DataHandlerLP.DK_L)["LABEL0"]
    ic, ric = calc_ic(pred, label.reindex(pred.index))
    print(f"Test IC {ic.mean():.3f}  Rank IC {ric.mean():.3f}  (ICIR {ic.mean() / (ic.std() + 1e-9):.2f})")

    last_day = pred.index.get_level_values("datetime").max()
    ranks = pred.xs(last_day, level="datetime").sort_values()
    ranks.name = "predicted_fwd_vol_change"
    out_path = Path(out)
    ranks.to_csv(out_path)
    print(f"\nPremium-selling candidates on {last_day.date()} (most negative = vol expected to fall):")
    print(ranks.head(15).to_string())
    print(f"\nSaved to {out_path}. The dashboard's Research tab reads this file if present.")


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    e = sub.add_parser("export")
    e.add_argument("--out", default="~/.qlib/optlab_csv")
    t = sub.add_parser("train")
    t.add_argument("--provider", default="~/.qlib/qlib_data/optlab")
    t.add_argument("--alpha158", action="store_true")
    t.add_argument("--test-start", default="2025-01-01")
    t.add_argument("--out", default=str(ROOT / "data" / "qlib_rankings.csv"))
    a = ap.parse_args()
    if a.cmd == "export":
        export(a.out)
    else:
        train(a.provider, a.alpha158, a.test_start, a.out)


if __name__ == "__main__":
    main()
