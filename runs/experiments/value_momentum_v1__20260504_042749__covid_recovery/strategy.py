from __future__ import annotations

"""
Agent-editable strategy file.

Purpose
-------
This file is the ONLY place where the strategy logic should live.

It should:
- read a prepared per-strategy dataset
- build any additional derived features from columns already present
- compute a cross-sectional score for each (trade_date, ts_code)
- output a signal file for the fixed backtest engine

It should NOT:
- connect to the raw database
- change point-in-time logic
- change joins
- change benchmark logic
- change backtest rules
- change evaluation rules

Expected output
---------------
A parquet/csv/pickle file with at least these columns:
- trade_date
- ts_code
- score

Long-only note
--------------
This project assumes A-share long-only research by default.
So the score should usually rank names from more attractive to less attractive.
The fixed backtest decides how to turn score into positions.
"""

import argparse
import json
from pathlib import Path
from typing import Iterable

import numpy as np
import pandas as pd


REQUIRED_BASE_COLUMNS = ["trade_date", "ts_code"]


# -----------------------------------------------------------------------------
# I/O helpers
# -----------------------------------------------------------------------------


def load_dataset(path: str | Path) -> pd.DataFrame:
    path = Path(path)
    suffix = path.suffix.lower()
    if suffix == ".parquet":
        return pd.read_parquet(path)
    if suffix in {".pkl", ".pickle"}:
        return pd.read_pickle(path)
    if suffix == ".csv":
        return pd.read_csv(path)
    raise ValueError(f"Unsupported dataset format: {path}")


def save_signals(df: pd.DataFrame, path: str | Path) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    suffix = path.suffix.lower()
    if suffix == ".parquet":
        df.to_parquet(path, index=False)
    elif suffix in {".pkl", ".pickle"}:
        df.to_pickle(path)
    elif suffix == ".csv":
        df.to_csv(path, index=False)
    else:
        raise ValueError(f"Unsupported output format: {path}")


# -----------------------------------------------------------------------------
# Small reusable feature helpers
# -----------------------------------------------------------------------------


def cross_sectional_rank(df: pd.DataFrame, col: str, ascending: bool = True) -> pd.Series:
    return df.groupby("trade_date")[col].rank(pct=True, ascending=ascending)


def zscore_by_date(df: pd.DataFrame, col: str) -> pd.Series:
    grouped = df.groupby("trade_date")[col]
    mean = grouped.transform("mean")
    std = grouped.transform("std")
    std = std.replace(0, np.nan)
    return (df[col] - mean) / std


def safe_ratio(num: pd.Series, den: pd.Series) -> pd.Series:
    den = den.replace(0, np.nan)
    return num / den


# -----------------------------------------------------------------------------
# Agent-editable strategy logic
# -----------------------------------------------------------------------------
# The functions below are the intended edit zone for the agent.
# Keep the signatures stable so the rest of the pipeline does not change.
# -----------------------------------------------------------------------------


def build_features(df: pd.DataFrame) -> pd.DataFrame:
    """
    Build or normalize any extra features from columns already in the dataset.

    Agent guidance:
    - You may add new columns here.
    - Use only columns that already exist in the provided dataset.
    - Do not try to fetch new data from the DB here.
    """
    out = df.copy()

    # Example fallback features. Agents can replace / extend these.
    if "close" in out.columns:
        out = out.sort_values(["ts_code", "trade_date"]).reset_index(drop=True)
        if "mom_20_local" not in out.columns:
            out["mom_20_local"] = out.groupby("ts_code")["close"].pct_change(20)
        if "mom_60_local" not in out.columns:
            out["mom_60_local"] = out.groupby("ts_code")["close"].pct_change(60)

    if {"fin_roe", "pb"}.issubset(out.columns):
        out["quality_value_ratio"] = safe_ratio(out["fin_roe"], out["pb"])

    return out


def compute_score(df: pd.DataFrame) -> pd.DataFrame:
    """
    Compute a cross-sectional score for each stock on each trade_date.

    Higher score = more attractive for long-only ranking.

    Agent guidance:
    - This is the main function to edit.
    - Keep output columns trade_date, ts_code, score.
    - Use cross-sectional ranking to make combined signals more stable.
    """
    out = df.copy()
    components = []

    # A simple baseline that uses whatever is available.
    if "pb" in out.columns:
        # Lower PB is better for value
        out["score_value_pb"] = 1.0 - cross_sectional_rank(out, "pb", ascending=True)
        components.append("score_value_pb")

    if "fin_roe" in out.columns:
        # Higher ROE is better
        out["score_quality_roe"] = cross_sectional_rank(out, "fin_roe", ascending=True)
        components.append("score_quality_roe")

    if "mom_60" in out.columns:
        out["score_mom_60"] = cross_sectional_rank(out, "mom_60", ascending=True)
        components.append("score_mom_60")
    elif "mom_60_local" in out.columns:
        out["score_mom_60"] = cross_sectional_rank(out, "mom_60_local", ascending=True)
        components.append("score_mom_60")

    if "mom_20" in out.columns:
        out["score_mom_20"] = cross_sectional_rank(out, "mom_20", ascending=True)
        components.append("score_mom_20")
    elif "mom_20_local" in out.columns:
        out["score_mom_20"] = cross_sectional_rank(out, "mom_20_local", ascending=True)
        components.append("score_mom_20")

    if "quality_value_ratio" in out.columns:
        out["score_qv"] = cross_sectional_rank(out, "quality_value_ratio", ascending=True)
        components.append("score_qv")

    if not components:
        raise ValueError(
            "No usable scoring inputs found. The dataset does not contain any of the "
            "baseline columns needed by the default strategy logic."
        )

    out["score"] = out[components].mean(axis=1, skipna=True)

    return out[["trade_date", "ts_code", "score"]]


def postprocess_signals(signals: pd.DataFrame, base_df: pd.DataFrame) -> pd.DataFrame:
    """
    Final cleanup / optional filtering before writing signals.

    Agent guidance:
    - Keep the output columns trade_date, ts_code, score.
    - This is a good place for simple filters.
    """
    out = signals.copy()

    # If tradability flag is available, keep only tradable rows.
    if "is_tradable" in base_df.columns:
        tmp = base_df[["trade_date", "ts_code", "is_tradable"]].drop_duplicates()
        out = out.merge(tmp, on=["trade_date", "ts_code"], how="left")
        out = out[out["is_tradable"].fillna(0).astype(int) == 1].drop(columns=["is_tradable"])

    out = out.dropna(subset=["trade_date", "ts_code", "score"])
    out = out.sort_values(["trade_date", "score", "ts_code"], ascending=[True, False, True])
    out = out.drop_duplicates(subset=["trade_date", "ts_code"], keep="first").reset_index(drop=True)
    return out


# -----------------------------------------------------------------------------
# Fixed orchestration inside strategy.py
# -----------------------------------------------------------------------------


def validate_dataset(df: pd.DataFrame) -> None:
    missing = [c for c in REQUIRED_BASE_COLUMNS if c not in df.columns]
    if missing:
        raise ValueError(f"Dataset missing required columns: {missing}")

    if not pd.api.types.is_datetime64_any_dtype(df["trade_date"]):
        try:
            df["trade_date"] = pd.to_datetime(df["trade_date"])
        except Exception as exc:
            raise ValueError("trade_date must be parseable as datetime") from exc


def generate_signals(df: pd.DataFrame) -> pd.DataFrame:
    validate_dataset(df)
    features = build_features(df)
    signals = compute_score(features)
    signals = postprocess_signals(signals, features)
    return signals


def run_strategy(dataset_path: str | Path, output_path: str | Path) -> tuple[pd.DataFrame, dict]:
    df = load_dataset(dataset_path)
    signals = generate_signals(df)

    metadata = {
        "dataset_path": str(dataset_path),
        "output_path": str(output_path),
        "rows_in_dataset": int(len(df)),
        "rows_in_signals": int(len(signals)),
        "num_dates": int(signals["trade_date"].nunique()) if not signals.empty else 0,
        "num_stocks": int(signals["ts_code"].nunique()) if not signals.empty else 0,
        "score_min": float(signals["score"].min()) if not signals.empty else None,
        "score_max": float(signals["score"].max()) if not signals.empty else None,
    }

    save_signals(signals, output_path)
    return signals, metadata


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate long-only signals from a strategy dataset.")
    parser.add_argument("--dataset", required=True, help="Path to prepared strategy dataset")
    parser.add_argument("--output", required=True, help="Path to write signal file (.parquet/.csv/.pkl)")
    parser.add_argument("--metadata-output", default=None, help="Optional JSON metadata output path")
    args = parser.parse_args()

    signals, metadata = run_strategy(args.dataset, args.output)

    if args.metadata_output:
        meta_path = Path(args.metadata_output)
        meta_path.parent.mkdir(parents=True, exist_ok=True)
        meta_path.write_text(json.dumps(metadata, indent=2), encoding="utf-8")

    print(f"[OK] Wrote signals: {args.output}")
    print(f"[INFO] signal rows={len(signals)} dates={metadata['num_dates']} stocks={metadata['num_stocks']}")


if __name__ == "__main__":
    main()
