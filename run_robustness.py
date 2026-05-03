from __future__ import annotations

"""
Run fixed robustness checks across protocol-defined date windows.

This script is a wrapper around run_experiment_clean.py.

It does NOT implement a new backtest.
It does NOT change evaluation logic.
It simply runs the same request + same strategy across multiple fixed
date windows from config/experiment_protocol.json and collects the results.

Typical use:
    python run_robustness.py ^
      --db "D:\\HKU\\bigdatainfinance\\SLOP\\Data\\data.db" ^
      --request "requests/runs/value_momentum_v1.json" ^
      --request-schema "config/strategy_request.schema.json" ^
      --protocol "config/experiment_protocol.json" ^
      --base-dir "runs"
"""

import argparse
import csv
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from run_experiment_clean import run_experiment, slugify  # noqa: E402


ROBUSTNESS_COLUMNS = [
    "window",
    "start_date",
    "end_date",
    "run_id",
    "strategy_name",
    "evaluation_profile",
    "benchmark",
    "score",
    "annualized_return",
    "annualized_volatility",
    "sharpe",
    "sortino",
    "max_drawdown",
    "avg_turnover",
    "n_periods",
    "dataset_rows",
    "dataset_cols",
    "signals_rows",
    "book_rows",
    "return_rows",
    "experiment_dir",
    "summary_path",
    "evaluation_path",
]


class RobustnessError(Exception):
    """Raised when robustness checking cannot complete."""


def load_json(path: str | Path) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write_json(obj: dict[str, Any], path: str | Path) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2, ensure_ascii=False), encoding="utf-8")


def write_csv(rows: list[dict[str, Any]], path: str | Path, fieldnames: list[str]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({k: row.get(k) for k in fieldnames})


def default_windows_from_protocol(protocol: dict[str, Any], include_primary: bool = True) -> list[str]:
    primary = protocol.get("primary_date_window")
    robustness = list(protocol.get("robustness_date_windows", []))

    windows: list[str] = []
    if include_primary and primary:
        windows.append(primary)
    windows.extend(robustness)

    # Dedupe while preserving order.
    out: list[str] = []
    seen: set[str] = set()
    for w in windows:
        if w not in seen:
            seen.add(w)
            out.append(w)
    return out


def validate_windows(protocol: dict[str, Any], windows: list[str]) -> None:
    available = set(protocol.get("date_windows", {}).keys())
    missing = [w for w in windows if w not in available]
    if missing:
        raise RobustnessError(
            f"Unknown date window(s): {missing}. Available windows: {sorted(available)}"
        )


def classify_robustness(rows: list[dict[str, Any]]) -> dict[str, Any]:
    scores = [r.get("score") for r in rows if isinstance(r.get("score"), (int, float))]
    sharpes = [r.get("sharpe") for r in rows if isinstance(r.get("sharpe"), (int, float))]
    drawdowns = [r.get("max_drawdown") for r in rows if isinstance(r.get("max_drawdown"), (int, float))]

    if not scores:
        return {
            "num_windows": len(rows),
            "num_valid_scores": 0,
            "score_min": None,
            "score_mean": None,
            "score_max": None,
            "all_scores_positive": False,
            "robustness_flag": "no_valid_scores",
        }

    score_min = min(scores)
    score_mean = sum(scores) / len(scores)
    score_max = max(scores)
    all_scores_positive = all(s > 0 for s in scores)

    if score_min > 0:
        flag = "strong"
    elif score_mean > 0 and score_min > -0.25:
        flag = "mixed_but_acceptable"
    else:
        flag = "weak_or_unstable"

    return {
        "num_windows": len(rows),
        "num_valid_scores": len(scores),
        "score_min": score_min,
        "score_mean": score_mean,
        "score_max": score_max,
        "sharpe_min": min(sharpes) if sharpes else None,
        "sharpe_mean": sum(sharpes) / len(sharpes) if sharpes else None,
        "max_drawdown_worst": max(drawdowns) if drawdowns else None,
        "all_scores_positive": all_scores_positive,
        "robustness_flag": flag,
    }


def make_row(summary: dict[str, Any], protocol: dict[str, Any], window: str) -> dict[str, Any]:
    metrics = summary.get("evaluation", {})
    date_window_cfg = protocol.get("date_windows", {}).get(window, {})

    return {
        "window": window,
        "start_date": date_window_cfg.get("start_date"),
        "end_date": date_window_cfg.get("end_date"),
        "run_id": summary.get("run_id"),
        "strategy_name": summary.get("strategy_name"),
        "evaluation_profile": summary.get("evaluation_profile"),
        "benchmark": summary.get("benchmark"),
        "score": metrics.get("score"),
        "annualized_return": metrics.get("annualized_return"),
        "annualized_volatility": metrics.get("annualized_volatility"),
        "sharpe": metrics.get("sharpe"),
        "sortino": metrics.get("sortino"),
        "max_drawdown": metrics.get("max_drawdown"),
        "avg_turnover": metrics.get("avg_turnover"),
        "n_periods": metrics.get("n_periods"),
        "dataset_rows": summary.get("dataset", {}).get("row_count"),
        "dataset_cols": summary.get("dataset", {}).get("column_count"),
        "signals_rows": summary.get("signals", {}).get("row_count"),
        "book_rows": summary.get("backtest", {}).get("book_rows"),
        "return_rows": summary.get("backtest", {}).get("return_rows"),
        "experiment_dir": summary.get("folders", {}).get("experiment_dir"),
        "summary_path": summary.get("artifacts", {}).get("summary"),
        "evaluation_path": summary.get("artifacts", {}).get("evaluation"),
    }


def run_robustness(
    *,
    db_path: str | Path,
    request_path: str | Path,
    base_dir: str | Path,
    request_schema_path: str | Path | None = None,
    protocol_path: str | Path = "config/experiment_protocol.json",
    windows: list[str] | None = None,
    include_primary: bool = True,
    run_id: str | None = None,
    cleanup_cache: bool = True,
    keep_heavy_files: bool = False,
    promote_score: float | None = None,
) -> dict[str, Any]:
    request = load_json(request_path)
    protocol = load_json(protocol_path)

    strategy_name = request.get("strategy_name", "strategy")
    run_id = run_id or datetime.now().strftime("%Y%m%d_%H%M%S")

    if windows is None or len(windows) == 0:
        windows = default_windows_from_protocol(protocol, include_primary=include_primary)

    validate_windows(protocol, windows)

    base_dir = Path(base_dir)
    robustness_dir = base_dir / "robustness" / f"{slugify(strategy_name)}__{run_id}"
    robustness_dir.mkdir(parents=True, exist_ok=True)

    rows: list[dict[str, Any]] = []
    summaries: dict[str, Any] = {}

    for window in windows:
        window_run_id = f"{run_id}__{window}"
        summary = run_experiment(
            db_path=db_path,
            request_path=request_path,
            base_dir=base_dir,
            request_schema_path=request_schema_path,
            protocol_path=protocol_path,
            date_window_name=window,
            run_id=window_run_id,
            cleanup_cache=cleanup_cache,
            keep_heavy_files=keep_heavy_files,
            promote_score=promote_score,
            status="robustness",
        )
        summaries[window] = summary
        rows.append(make_row(summary, protocol, window))

    aggregate = classify_robustness(rows)

    results_csv = robustness_dir / "robustness_results.csv"
    summary_json = robustness_dir / "robustness_summary.json"

    write_csv(rows, results_csv, ROBUSTNESS_COLUMNS)

    output = {
        "strategy_name": strategy_name,
        "run_id": run_id,
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "request_path": str(Path(request_path).resolve()),
        "protocol_path": str(Path(protocol_path).resolve()),
        "windows": windows,
        "aggregate": aggregate,
        "results_csv": str(results_csv.resolve()),
        "robustness_dir": str(robustness_dir.resolve()),
        "window_results": rows,
        "window_summaries": {
            window: {
                "summary_path": summaries[window].get("artifacts", {}).get("summary"),
                "experiment_dir": summaries[window].get("folders", {}).get("experiment_dir"),
                "score": summaries[window].get("evaluation", {}).get("score"),
            }
            for window in summaries
        },
    }

    write_json(output, summary_json)
    output["summary_json"] = str(summary_json.resolve())
    return output


def main() -> None:
    parser = argparse.ArgumentParser(description="Run robustness checks across fixed protocol date windows.")
    parser.add_argument("--db", required=True, help="Path to DuckDB database file")
    parser.add_argument("--request", required=True, help="Path to strategy_request.json")
    parser.add_argument("--base-dir", required=True, help="Base directory that contains runs cache/experiments/logs")
    parser.add_argument("--request-schema", default=None, help="Optional strategy_request.schema.json path")
    parser.add_argument("--protocol", default="config/experiment_protocol.json", help="Path to fixed experiment_protocol.json")
    parser.add_argument(
        "--windows",
        nargs="*",
        default=None,
        help="Optional list of protocol date windows. Defaults to primary + robustness windows.",
    )
    parser.add_argument(
        "--exclude-primary",
        action="store_true",
        help="Only run robustness_date_windows, excluding primary_date_window.",
    )
    parser.add_argument("--run-id", default=None, help="Optional fixed robustness run id")
    parser.add_argument("--no-cleanup-cache", action="store_true", help="Keep cached dataset/signals/book/returns after scoring")
    parser.add_argument("--keep-heavy-files", action="store_true", help="Copy heavy files into each experiment folder")
    parser.add_argument("--promote-score", type=float, default=None, help="If score >= threshold, keep heavy files")
    args = parser.parse_args()

    result = run_robustness(
        db_path=args.db,
        request_path=args.request,
        base_dir=args.base_dir,
        request_schema_path=args.request_schema,
        protocol_path=args.protocol,
        windows=args.windows,
        include_primary=not bool(args.exclude_primary),
        run_id=args.run_id,
        cleanup_cache=not bool(args.no_cleanup_cache),
        keep_heavy_files=bool(args.keep_heavy_files),
        promote_score=args.promote_score,
    )

    agg = result["aggregate"]
    print("[PASS] Robustness run completed.")
    print(f"[INFO] Robustness flag: {agg.get('robustness_flag')}")
    print(f"[INFO] Score mean: {agg.get('score_mean')}")
    print(f"[INFO] Score min: {agg.get('score_min')}")
    print(f"[INFO] Results: {result['results_csv']}")
    print(f"[INFO] Summary: {result['summary_json']}")


if __name__ == "__main__":
    main()
