from __future__ import annotations

"""
Fixed cross-sectional backtest engine for the quant research loop.

Design goals
------------
- Keep trading assumptions fixed and comparable across experiments.
- Accept agent-produced scores/weights but NOT agent-controlled execution logic.
- Be simple enough to audit and robust enough for prototype research.

Expected inputs
---------------
1) strategy_dataset parquet/csv built by build_strategy_dataset.py. It should contain:
   - trade_date
   - ts_code
   - close
   - amount (recommended)
   - is_tradable (recommended)
   - split (optional)
   - forward-return labels like ret_20d_fwd or excess_ret_20d_fwd

2) signals dataframe or file with at least:
   - trade_date
   - ts_code
   - score
   Optional:
   - weight

Backtest logic
--------------
- Rebalance on the selected schedule (daily / weekly / monthly).
- Filter to tradable rows unless explicitly disabled.
- Form long-only or long-short portfolios from cross-sectional ranks.
- Hold until next rebalance date using the chosen forward-return label.
- Apply simple turnover-based cost model.

This file is intended to be FIXED / read-only infrastructure.
"""

from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Literal, Sequence
import argparse
import json

import numpy as np
import pandas as pd


class BacktestError(Exception):
    """Raised when backtest inputs or configuration are invalid."""


@dataclass(frozen=True)
class BacktestConfig:
    label_col: str = "ret_20d_fwd"
    trade_date_col: str = "trade_date"
    asset_col: str = "ts_code"
    price_col: str = "close"
    score_col: str = "score"
    weight_col: str = "weight"
    tradable_col: str = "is_tradable"
    split_col: str = "split"

    rebalance_frequency: Literal["daily", "weekly", "monthly"] = "monthly"
    portfolio_type: Literal["long_only", "long_short"] = "long_only"

    long_quantile: float = 0.10
    short_quantile: float = 0.10
    max_positions: int | None = 50
    min_positions: int = 5

    use_tradable_only: bool = True
    use_equal_weight_if_missing: bool = True

    transaction_cost_bps: float = 10.0
    slippage_bps: float = 5.0

    evaluation_split: str | None = "valid"

    output_return_col: str = "portfolio_return"
    output_net_return_col: str = "portfolio_net_return"


# -----------------------------------------------------------------------------
# I/O helpers
# -----------------------------------------------------------------------------


def load_dataframe(path: str | Path) -> pd.DataFrame:
    path = Path(path)
    if not path.exists():
        raise BacktestError(f"Input file does not exist: {path}")
    suffix = path.suffix.lower()
    if suffix == ".parquet":
        return pd.read_parquet(path)
    if suffix in {".csv", ".txt"}:
        return pd.read_csv(path)
    if suffix in {".pkl", ".pickle"}:
        return pd.read_pickle(path)
    raise BacktestError(f"Unsupported input format: {path.suffix}")


def save_dataframe(df: pd.DataFrame, path: str | Path) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    suffix = path.suffix.lower()
    if suffix == ".parquet":
        df.to_parquet(path, index=False)
    elif suffix == ".csv":
        df.to_csv(path, index=False)
    elif suffix in {".pkl", ".pickle"}:
        df.to_pickle(path)
    else:
        raise BacktestError(f"Unsupported output format: {path.suffix}")


# -----------------------------------------------------------------------------
# Validation and prep
# -----------------------------------------------------------------------------


def _ensure_datetime(df: pd.DataFrame, col: str) -> pd.DataFrame:
    out = df.copy(deep=False)
    out[col] = pd.to_datetime(out[col], errors="coerce")
    return out


def validate_strategy_dataset(df: pd.DataFrame, cfg: BacktestConfig) -> None:
    required = {cfg.trade_date_col, cfg.asset_col, cfg.label_col}
    missing = sorted(required - set(df.columns))
    if missing:
        raise BacktestError(f"Strategy dataset missing required columns: {missing}")


def validate_signals(df: pd.DataFrame, cfg: BacktestConfig) -> None:
    required = {cfg.trade_date_col, cfg.asset_col, cfg.score_col}
    missing = sorted(required - set(df.columns))
    if missing:
        raise BacktestError(f"Signals missing required columns: {missing}")


def prepare_inputs(
    strategy_dataset: pd.DataFrame,
    signals: pd.DataFrame,
    cfg: BacktestConfig,
) -> pd.DataFrame:
    validate_strategy_dataset(strategy_dataset, cfg)
    validate_signals(signals, cfg)

    ds = _ensure_datetime(strategy_dataset, cfg.trade_date_col)
    sg = _ensure_datetime(signals, cfg.trade_date_col)

    ds = ds.dropna(subset=[cfg.trade_date_col, cfg.asset_col]).copy(deep=False)
    sg = sg.dropna(subset=[cfg.trade_date_col, cfg.asset_col]).copy(deep=False)

    merge_cols = [cfg.trade_date_col, cfg.asset_col]
    sg_cols = [cfg.trade_date_col, cfg.asset_col, cfg.score_col] + ([cfg.weight_col] if cfg.weight_col in sg.columns else [])
    ds = ds.drop(columns=[cfg.score_col], errors="ignore")
    merged = ds.merge(
        sg[sg_cols],
        on=merge_cols,
        how="inner",
        validate="one_to_one",
    )

    if cfg.evaluation_split is not None and cfg.split_col in merged.columns:
        merged = merged[merged[cfg.split_col] == cfg.evaluation_split].copy(deep=False)

    if cfg.use_tradable_only and cfg.tradable_col in merged.columns:
        merged = merged[merged[cfg.tradable_col].fillna(0).astype(int) == 1].copy(deep=False)

    # Score and selected label must be valid finite numbers before portfolio
    # formation. Pandas sum() skips NaNs by default, so failing to drop invalid
    # labels can create distorted or fake portfolio returns.
    merged[cfg.score_col] = pd.to_numeric(merged[cfg.score_col], errors="coerce")
    merged[cfg.label_col] = pd.to_numeric(merged[cfg.label_col], errors="coerce")
    numeric_cols = [cfg.score_col, cfg.label_col]

    if cfg.weight_col in merged.columns:
        merged[cfg.weight_col] = pd.to_numeric(merged[cfg.weight_col], errors="coerce")
        numeric_cols.append(cfg.weight_col)

    merged[numeric_cols] = merged[numeric_cols].replace([np.inf, -np.inf], np.nan)
    merged = merged.dropna(subset=[cfg.score_col, cfg.label_col]).copy(deep=False)

    if merged.empty:
        raise BacktestError(
            "No rows left after merging signals with strategy dataset, applying filters, "
            "and dropping missing/invalid score or label values."
        )

    merged = merged.sort_values([cfg.trade_date_col, cfg.asset_col]).reset_index(drop=True)
    return merged


# -----------------------------------------------------------------------------
# Rebalance date logic
# -----------------------------------------------------------------------------


def mark_rebalance_dates(dates: Sequence[pd.Timestamp], frequency: str) -> pd.Series:
    idx = pd.DatetimeIndex(pd.to_datetime(pd.Series(dates).dropna().unique())).sort_values()
    if len(idx) == 0:
        return pd.Series([], dtype="datetime64[ns]")

    if frequency == "daily":
        return pd.Series(idx)

    s = pd.Series(idx)
    if frequency == "weekly":
        keys = s.dt.to_period("W-FRI")
    elif frequency == "monthly":
        keys = s.dt.to_period("M")
    else:
        raise BacktestError(f"Unsupported rebalance_frequency: {frequency}")

    rebalance_dates = s.groupby(keys).max().sort_values().reset_index(drop=True)
    return rebalance_dates


# -----------------------------------------------------------------------------
# Portfolio formation
# -----------------------------------------------------------------------------


def _normalize_weights(weights: pd.Series) -> pd.Series:
    total = weights.abs().sum()
    if total == 0 or pd.isna(total):
        return weights * 0.0
    return weights / total


def select_positions(cross_section: pd.DataFrame, cfg: BacktestConfig) -> pd.DataFrame:
    if cross_section.empty:
        return cross_section.copy(deep=False)

    cs = cross_section.sort_values(cfg.score_col).copy(deep=False)
    n = len(cs)

    if n < cfg.min_positions:
        return cs.iloc[0:0].copy(deep=False)

    if cfg.portfolio_type == "long_only":
        k = max(cfg.min_positions, int(np.ceil(n * cfg.long_quantile)))
        if cfg.max_positions is not None:
            k = min(k, cfg.max_positions)
        selected = cs.nlargest(k, cfg.score_col).copy(deep=False)
        if cfg.weight_col in selected.columns and not cfg.use_equal_weight_if_missing:
            w = selected[cfg.weight_col].astype(float).clip(lower=0)
            if (w.abs().sum() <= 0) or w.isna().all():
                w = pd.Series(1.0, index=selected.index)
        else:
            w = pd.Series(1.0, index=selected.index)
        selected["target_weight"] = _normalize_weights(w)
        return selected

    if cfg.portfolio_type == "long_short":
        k_long = max(cfg.min_positions, int(np.ceil(n * cfg.long_quantile)))
        k_short = max(cfg.min_positions, int(np.ceil(n * cfg.short_quantile)))
        if cfg.max_positions is not None:
            k_long = min(k_long, cfg.max_positions)
            k_short = min(k_short, cfg.max_positions)

        longs = cs.nlargest(k_long, cfg.score_col).copy(deep=False)
        shorts = cs.nsmallest(k_short, cfg.score_col).copy(deep=False)

        long_w = pd.Series(1.0, index=longs.index)
        short_w = pd.Series(-1.0, index=shorts.index)

        longs["target_weight"] = _normalize_weights(long_w) * 0.5
        shorts["target_weight"] = _normalize_weights(short_w) * 0.5
        return pd.concat([longs, shorts], axis=0, ignore_index=True)

    raise BacktestError(f"Unsupported portfolio_type: {cfg.portfolio_type}")


def build_rebalance_book(merged: pd.DataFrame, cfg: BacktestConfig) -> pd.DataFrame:
    rebalance_dates = set(mark_rebalance_dates(merged[cfg.trade_date_col], cfg.rebalance_frequency))
    rows = []
    for date, group in merged.groupby(cfg.trade_date_col, sort=True):
        if date not in rebalance_dates:
            continue
        selected = select_positions(group, cfg)
        if not selected.empty:
            selected = selected.copy(deep=False)
            selected["rebalance_date"] = date
            rows.append(selected)
    if not rows:
        raise BacktestError("No rebalance portfolios could be formed from the signals.")
    out = pd.concat(rows, axis=0, ignore_index=True)
    return out.sort_values(["rebalance_date", cfg.asset_col]).reset_index(drop=True)


# -----------------------------------------------------------------------------
# Return calculation
# -----------------------------------------------------------------------------


def compute_portfolio_returns(book: pd.DataFrame, cfg: BacktestConfig) -> pd.DataFrame:
    required = {"rebalance_date", cfg.asset_col, "target_weight", cfg.label_col}
    missing = sorted(required - set(book.columns))
    if missing:
        raise BacktestError(f"Backtest book missing required columns: {missing}")

    rows = []
    prev_weights = pd.Series(dtype=float)

    for date, group in book.groupby("rebalance_date", sort=True):
        g = group.copy(deep=False)
        gross_ret = float((g["target_weight"] * g[cfg.label_col].astype(float)).sum())

        current = g.set_index(cfg.asset_col)["target_weight"].astype(float).sort_index()
        universe = prev_weights.index.union(current.index)
        prev_aligned = prev_weights.reindex(universe).fillna(0.0)
        curr_aligned = current.reindex(universe).fillna(0.0)
        turnover = float((curr_aligned - prev_aligned).abs().sum())

        total_cost = turnover * (cfg.transaction_cost_bps + cfg.slippage_bps) / 10000.0
        net_ret = gross_ret - total_cost

        rows.append(
            {
                cfg.trade_date_col: pd.Timestamp(date),
                cfg.output_return_col: gross_ret,
                "turnover": turnover,
                "transaction_cost": total_cost,
                cfg.output_net_return_col: net_ret,
                "n_positions": int(len(g)),
                "gross_exposure": float(g["target_weight"].abs().sum()),
            }
        )
        prev_weights = current

    result = pd.DataFrame(rows).sort_values(cfg.trade_date_col).reset_index(drop=True)
    result["equity_curve"] = (1.0 + result[cfg.output_net_return_col].fillna(0.0)).cumprod()
    return result


# -----------------------------------------------------------------------------
# Top-level orchestration
# -----------------------------------------------------------------------------


def run_backtest(
    strategy_dataset: pd.DataFrame,
    signals: pd.DataFrame,
    cfg: BacktestConfig | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    cfg = cfg or BacktestConfig()
    merged = prepare_inputs(strategy_dataset, signals, cfg)
    book = build_rebalance_book(merged, cfg)
    returns = compute_portfolio_returns(book, cfg)
    return book, returns


def load_config(path: str | Path | None) -> BacktestConfig:
    if path is None:
        return BacktestConfig()
    cfg_json = json.loads(Path(path).read_text(encoding="utf-8"))
    return BacktestConfig(**cfg_json)


def save_metadata(path: str | Path, cfg: BacktestConfig, book: pd.DataFrame, returns: pd.DataFrame) -> None:
    meta = {
        "config": asdict(cfg),
        "n_book_rows": int(len(book)),
        "n_rebalances": int(len(returns)),
        "date_min": returns[cfg.trade_date_col].min().strftime("%Y-%m-%d") if not returns.empty else None,
        "date_max": returns[cfg.trade_date_col].max().strftime("%Y-%m-%d") if not returns.empty else None,
    }
    Path(path).write_text(json.dumps(meta, indent=2), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description="Fixed backtest engine for strategy signals.")
    parser.add_argument("--dataset", required=True, help="Path to strategy dataset parquet/csv/pickle.")
    parser.add_argument("--signals", required=True, help="Path to signals parquet/csv/pickle.")
    parser.add_argument("--config", default=None, help="Optional JSON config for backtest parameters.")
    parser.add_argument("--book-output", required=True, help="Output path for portfolio book.")
    parser.add_argument("--returns-output", required=True, help="Output path for portfolio returns.")
    parser.add_argument("--metadata-output", default=None, help="Optional metadata JSON output path.")
    args = parser.parse_args()

    cfg = load_config(args.config)
    dataset = load_dataframe(args.dataset)
    signals = load_dataframe(args.signals)
    book, returns = run_backtest(dataset, signals, cfg)

    save_dataframe(book, args.book_output)
    save_dataframe(returns, args.returns_output)
    if args.metadata_output:
        save_metadata(args.metadata_output, cfg, book, returns)

    print(f"[PASS] Backtest completed. Rebalances: {len(returns)} | Book rows: {len(book)}")


if __name__ == "__main__":
    main()
