from __future__ import annotations

"""
Fixed evaluation layer for quant strategy research.

Purpose
-------
This file scores the output of the fixed backtest engine.
It converts the return series into:
- standard performance metrics
- one final scalar score used for experiment comparison

Design principles
-----------------
- Keep evaluation fixed across experiments
- Long-only A-share friendly defaults
- Penalize turnover and large drawdowns
- Be simple enough to audit

Expected input
--------------
A returns file produced by backtest.py with columns such as:
- trade_date
- portfolio_return
- portfolio_net_return
- turnover
- n_positions
- equity_curve

This file is intended to be FIXED / read-only infrastructure.
"""

from dataclasses import dataclass, asdict
from pathlib import Path
import argparse
import json
import math

import numpy as np
import pandas as pd


class EvaluationError(Exception):
    """Raised when evaluation inputs or configuration are invalid."""


@dataclass(frozen=True)
class EvaluationConfig:
    trade_date_col: str = "trade_date"
    gross_return_col: str = "portfolio_return"
    net_return_col: str = "portfolio_net_return"
    turnover_col: str = "turnover"
    equity_col: str = "equity_curve"

    periods_per_year: int = 12
    risk_free_rate_annual: float = 0.0

    turnover_penalty: float = 0.10
    drawdown_soft_limit: float = 0.20
    drawdown_penalty: float = 2.00
    min_rebalances: int = 6

    # final score = sharpe - turnover_penalty * avg_turnover - drawdown_penalty * max(0, max_dd - soft_limit)
    # optionally downweight very short runs
    short_history_penalty: float = 0.50

    score_name: str = "score"


# -----------------------------------------------------------------------------
# I/O helpers
# -----------------------------------------------------------------------------


def load_dataframe(path: str | Path) -> pd.DataFrame:
    path = Path(path)
    if not path.exists():
        raise EvaluationError(f"Input file does not exist: {path}")
    suffix = path.suffix.lower()
    if suffix == ".parquet":
        return pd.read_parquet(path)
    if suffix in {".csv", ".txt"}:
        return pd.read_csv(path)
    if suffix in {".pkl", ".pickle"}:
        return pd.read_pickle(path)
    raise EvaluationError(f"Unsupported input format: {path.suffix}")


def save_json(obj: dict, path: str | Path) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2), encoding="utf-8")


# -----------------------------------------------------------------------------
# Validation
# -----------------------------------------------------------------------------


def validate_returns(df: pd.DataFrame, cfg: EvaluationConfig) -> pd.DataFrame:
    required = {cfg.trade_date_col, cfg.net_return_col}
    missing = sorted(required - set(df.columns))
    if missing:
        raise EvaluationError(f"Returns file missing required columns: {missing}")

    out = df.copy(deep=False)
    out[cfg.trade_date_col] = pd.to_datetime(out[cfg.trade_date_col], errors="coerce")
    out = out.dropna(subset=[cfg.trade_date_col, cfg.net_return_col]).copy(deep=False)
    out = out.sort_values(cfg.trade_date_col).reset_index(drop=True)

    if out.empty:
        raise EvaluationError("No valid rows available for evaluation.")
    return out


# -----------------------------------------------------------------------------
# Metric helpers
# -----------------------------------------------------------------------------


def annualized_return(returns: pd.Series, periods_per_year: int) -> float:
    returns = returns.dropna().astype(float)
    n = len(returns)
    if n == 0:
        return float("nan")
    growth = float((1.0 + returns).prod())
    if growth <= 0:
        return -1.0
    return growth ** (periods_per_year / n) - 1.0


def annualized_volatility(returns: pd.Series, periods_per_year: int) -> float:
    returns = returns.dropna().astype(float)
    if len(returns) <= 1:
        return float("nan")
    return float(returns.std(ddof=1) * math.sqrt(periods_per_year))


def sharpe_ratio(returns: pd.Series, periods_per_year: int, risk_free_rate_annual: float = 0.0) -> float:
    returns = returns.dropna().astype(float)
    if len(returns) <= 1:
        return float("nan")
    rf_per_period = (1.0 + risk_free_rate_annual) ** (1.0 / periods_per_year) - 1.0
    excess = returns - rf_per_period
    vol = excess.std(ddof=1)
    if pd.isna(vol) or vol == 0:
        return float("nan")
    return float(excess.mean() / vol * math.sqrt(periods_per_year))


def max_drawdown_from_equity(equity: pd.Series) -> float:
    equity = equity.dropna().astype(float)
    if equity.empty:
        return float("nan")
    running_max = equity.cummax()
    drawdown = equity / running_max - 1.0
    return float(drawdown.min())


def hit_rate(returns: pd.Series) -> float:
    returns = returns.dropna().astype(float)
    if len(returns) == 0:
        return float("nan")
    return float((returns > 0).mean())


def downside_volatility(returns: pd.Series, periods_per_year: int) -> float:
    returns = returns.dropna().astype(float)
    downside = returns[returns < 0]
    if len(downside) <= 1:
        return 0.0
    return float(downside.std(ddof=1) * math.sqrt(periods_per_year))


def sortino_ratio(returns: pd.Series, periods_per_year: int, risk_free_rate_annual: float = 0.0) -> float:
    returns = returns.dropna().astype(float)
    if len(returns) == 0:
        return float("nan")
    rf_per_period = (1.0 + risk_free_rate_annual) ** (1.0 / periods_per_year) - 1.0
    excess = returns - rf_per_period
    downside_vol = downside_volatility(excess, periods_per_year)
    if downside_vol == 0:
        return float("nan")
    return float(excess.mean() * periods_per_year / downside_vol)


# -----------------------------------------------------------------------------
# Core evaluation
# -----------------------------------------------------------------------------


def evaluate_returns(returns_df: pd.DataFrame, cfg: EvaluationConfig | None = None) -> dict:
    cfg = cfg or EvaluationConfig()
    df = validate_returns(returns_df, cfg)

    net = df[cfg.net_return_col].astype(float)
    gross = df[cfg.gross_return_col].astype(float) if cfg.gross_return_col in df.columns else pd.Series(dtype=float)
    turnover = df[cfg.turnover_col].astype(float) if cfg.turnover_col in df.columns else pd.Series(dtype=float)
    equity = df[cfg.equity_col].astype(float) if cfg.equity_col in df.columns else (1.0 + net.fillna(0.0)).cumprod()

    ann_ret = annualized_return(net, cfg.periods_per_year)
    ann_vol = annualized_volatility(net, cfg.periods_per_year)
    sharpe = sharpe_ratio(net, cfg.periods_per_year, cfg.risk_free_rate_annual)
    sortino = sortino_ratio(net, cfg.periods_per_year, cfg.risk_free_rate_annual)
    max_dd = abs(max_drawdown_from_equity(equity))
    avg_turnover = float(turnover.mean()) if len(turnover) else float("nan")
    avg_gross = float(gross.mean()) if len(gross) else float("nan")
    avg_net = float(net.mean())
    hr = hit_rate(net)

    n_periods = int(len(df))
    penalty_turnover = 0.0 if pd.isna(avg_turnover) else cfg.turnover_penalty * avg_turnover
    penalty_drawdown = cfg.drawdown_penalty * max(0.0, max_dd - cfg.drawdown_soft_limit)

    if pd.isna(sharpe):
        base_score = -999.0
    else:
        base_score = sharpe

    score = base_score - penalty_turnover - penalty_drawdown

    if n_periods < cfg.min_rebalances:
        score -= cfg.short_history_penalty

    metrics = {
        "config": asdict(cfg),
        "n_periods": n_periods,
        "date_min": df[cfg.trade_date_col].min().strftime("%Y-%m-%d"),
        "date_max": df[cfg.trade_date_col].max().strftime("%Y-%m-%d"),
        "avg_gross_return": None if pd.isna(avg_gross) else float(avg_gross),
        "avg_net_return": float(avg_net),
        "annualized_return": None if pd.isna(ann_ret) else float(ann_ret),
        "annualized_volatility": None if pd.isna(ann_vol) else float(ann_vol),
        "sharpe": None if pd.isna(sharpe) else float(sharpe),
        "sortino": None if pd.isna(sortino) else float(sortino),
        "max_drawdown": None if pd.isna(max_dd) else float(max_dd),
        "hit_rate": None if pd.isna(hr) else float(hr),
        "avg_turnover": None if pd.isna(avg_turnover) else float(avg_turnover),
        "penalty_turnover": float(penalty_turnover),
        "penalty_drawdown": float(penalty_drawdown),
        cfg.score_name: float(score),
    }
    return metrics


def load_config(path: str | Path | None) -> EvaluationConfig:
    if path is None:
        return EvaluationConfig()
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    return EvaluationConfig(**payload)


def main() -> None:
    parser = argparse.ArgumentParser(description="Fixed evaluation layer for backtest returns.")
    parser.add_argument("--returns", required=True, help="Backtest returns parquet/csv/pickle")
    parser.add_argument("--config", default=None, help="Optional evaluation config JSON")
    parser.add_argument("--output", required=True, help="Output JSON path for evaluation metrics")
    args = parser.parse_args()

    cfg = load_config(args.config)
    returns_df = load_dataframe(args.returns)
    metrics = evaluate_returns(returns_df, cfg)
    save_json(metrics, args.output)

    print(f"[PASS] Evaluation completed. score={metrics[cfg.score_name]:.6f}")


if __name__ == "__main__":
    main()
