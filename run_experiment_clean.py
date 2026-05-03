from __future__ import annotations

"""
Clean experiment runner for the quant research loop.

Folder policy
-------------
base_dir/
├─ cache/
│  ├─ datasets/
│  ├─ signals/
│  ├─ books/
│  └─ returns/
├─ experiments/
│  └─ <strategy_name>__<run_id>/
│     ├─ request.json
│     ├─ strategy.py
│     ├─ summary.json
│     ├─ evaluation.json
│     ├─ dataset.metadata.json
│     ├─ signals.metadata.json
│     ├─ backtest.metadata.json
│     ├─ experiment_protocol.json
│     └─ notes.md
└─ logs/
   └─ results.csv

Heavy rebuildable files go to cache by default.
Small reproducibility records go to experiments.
Heavy files are copied into the experiment folder only if explicitly kept
or if the score crosses a promotion threshold.

Protocol logic
--------------
The agent chooses an approved evaluation_profile in the request JSON.
The fixed experiment_protocol.json resolves that profile into:
- label_col
- rebalance_frequency
- periods_per_year
- benchmark
- date window

The agent is not allowed to define arbitrary labels, rebalance rules,
benchmarks, annualization factors, or date ranges.
"""

import argparse
import csv
import json
import shutil
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from build_strategy_dataset import build_strategy_dataset  # noqa: E402
from strategy import run_strategy  # noqa: E402
from backtest import (  # noqa: E402
    BacktestConfig,
    load_dataframe as load_bt_dataframe,
    run_backtest,
    save_dataframe as save_bt_dataframe,
    save_metadata as save_backtest_metadata,
)
from evaluation import (  # noqa: E402
    EvaluationConfig,
    evaluate_returns,
    save_json as save_evaluation_json,
)


class ExperimentRunnerError(Exception):
    """Raised when the experiment runner cannot complete a stage."""


RESULTS_COLUMNS = [
    "run_id",
    "timestamp",
    "strategy_name",
    "evaluation_profile",
    "date_window",
    "benchmark",
    "request_path",
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
    "status",
    "promoted",
    "kept_heavy_files",
    "experiment_dir",
    "summary_path",
]


# -----------------------------------------------------------------------------
# Small helpers
# -----------------------------------------------------------------------------


def slugify(text: str) -> str:
    safe = []
    for ch in text.lower().strip():
        if ch.isalnum():
            safe.append(ch)
        elif ch in {" ", "-", "_"}:
            safe.append("_")
    out = "".join(safe).strip("_")
    return out or "strategy"


def load_json(path: str | Path) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write_json(obj: dict[str, Any], path: str | Path) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2, ensure_ascii=False), encoding="utf-8")


def maybe_delete(path: str | Path | None) -> None:
    if path is None:
        return
    p = Path(path)
    if p.exists():
        p.unlink()


def delete_cache_sidecars(path: str | Path) -> None:
    """Delete small metadata sidecars that may be written beside cache files."""
    p = Path(path)
    candidates = [
        Path(str(p) + ".metadata.json"),
        p.with_suffix(p.suffix + ".metadata.json"),
        p.with_suffix(".metadata.json"),
    ]
    seen: set[Path] = set()
    for c in candidates:
        if c in seen:
            continue
        seen.add(c)
        maybe_delete(c)


def resolve_actual_output(base_path: Path) -> Path:
    if base_path.exists():
        return base_path

    candidates = [
        base_path.with_suffix(".pkl"),
        base_path.with_suffix(".pickle"),
        base_path.with_suffix(".csv"),
        base_path.with_suffix(".parquet"),
    ]
    for c in candidates:
        if c.exists():
            return c

    raise ExperimentRunnerError(f"Expected output not found: {base_path}")


def ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def copy_if_exists(src: str | Path | None, dst: Path) -> None:
    if src is None:
        return
    src_path = Path(src)
    if src_path.exists():
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src_path, dst)


def dataclass_kwargs(cls: type, payload: dict[str, Any]) -> dict[str, Any]:
    allowed = set(getattr(cls, "__dataclass_fields__", {}).keys())
    return {k: v for k, v in payload.items() if k in allowed}


def append_results_row(results_csv: Path, row: dict[str, Any]) -> None:
    results_csv.parent.mkdir(parents=True, exist_ok=True)
    file_exists = results_csv.exists()
    with results_csv.open("a", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=RESULTS_COLUMNS)
        if not file_exists:
            writer.writeheader()
        writer.writerow({k: row.get(k) for k in RESULTS_COLUMNS})


FORWARD_LABEL_COLS = {
    "ret_1d_fwd",
    "ret_5d_fwd",
    "ret_20d_fwd",
    "bench_ret_1d_fwd",
    "bench_ret_5d_fwd",
    "bench_ret_20d_fwd",
    "excess_ret_1d_fwd",
    "excess_ret_5d_fwd",
    "excess_ret_20d_fwd",
}


def make_feature_only_dataset(dataset: Any) -> Any:
    """Remove forward-return labels before passing data to strategy.py."""
    leak_cols = [c for c in dataset.columns if c in FORWARD_LABEL_COLS]
    return dataset.drop(columns=leak_cols, errors="ignore").copy()


# -----------------------------------------------------------------------------
# Protocol resolution
# -----------------------------------------------------------------------------


def load_experiment_protocol(protocol_path: str | Path) -> dict[str, Any]:
    protocol = load_json(protocol_path)

    required = [
        "benchmark",
        "default_evaluation_profile",
        "allowed_evaluation_profiles",
        "primary_date_window",
        "date_windows",
        "backtest_defaults",
        "evaluation_defaults",
    ]
    missing = [k for k in required if k not in protocol]
    if missing:
        raise ExperimentRunnerError(f"experiment_protocol.json missing required fields: {missing}")

    benchmark = protocol.get("benchmark", {})
    if not isinstance(benchmark, dict) or not benchmark.get("ts_code"):
        raise ExperimentRunnerError("experiment_protocol.json must define benchmark.ts_code.")

    return protocol


def resolve_profile_and_window(
    *,
    request: dict[str, Any],
    protocol: dict[str, Any],
    date_window_name: str | None,
) -> dict[str, Any]:
    profile_name = request.get("evaluation_profile") or protocol["default_evaluation_profile"]
    profiles = protocol["allowed_evaluation_profiles"]
    if profile_name not in profiles:
        raise ExperimentRunnerError(
            f"Unknown evaluation_profile={profile_name!r}. Allowed: {sorted(profiles)}"
        )

    window_name = date_window_name or protocol["primary_date_window"]
    windows = protocol["date_windows"]
    if window_name not in windows:
        raise ExperimentRunnerError(f"Unknown date_window={window_name!r}. Allowed: {sorted(windows)}")

    return {
        "profile_name": profile_name,
        "profile": dict(profiles[profile_name]),
        "date_window_name": window_name,
        "date_window": dict(windows[window_name]),
        "benchmark": protocol["benchmark"]["ts_code"],
        "benchmark_config": dict(protocol["benchmark"]),
    }


def make_backtest_config(
    *,
    protocol: dict[str, Any],
    profile: dict[str, Any],
    backtest_config_path: str | Path | None = None,
) -> BacktestConfig:
    payload: dict[str, Any] = {}

    # Human-controlled protocol defaults.
    payload.update(protocol.get("backtest_defaults", {}))

    # Profile-controlled horizon mechanics.
    payload["label_col"] = profile["label_col"]
    payload["rebalance_frequency"] = profile["rebalance_frequency"]

    # Optional human override file, if explicitly supplied.
    # This is not agent-controlled unless the human gives the agent access to this file.
    if backtest_config_path is not None:
        payload.update(load_json(backtest_config_path))

    return BacktestConfig(**dataclass_kwargs(BacktestConfig, payload))


def make_evaluation_config(
    *,
    protocol: dict[str, Any],
    profile: dict[str, Any],
    evaluation_config_path: str | Path | None = None,
) -> EvaluationConfig:
    payload: dict[str, Any] = {}

    # Human-controlled evaluation defaults.
    payload.update(protocol.get("evaluation_defaults", {}))

    # Profile-controlled annualization.
    payload["periods_per_year"] = profile["periods_per_year"]

    # Optional human override file, if explicitly supplied.
    if evaluation_config_path is not None:
        payload.update(load_json(evaluation_config_path))

    return EvaluationConfig(**dataclass_kwargs(EvaluationConfig, payload))


# -----------------------------------------------------------------------------
# Layout / retention helpers
# -----------------------------------------------------------------------------


def build_layout(base_dir: Path, stem: str) -> dict[str, Path]:
    cache_dir = ensure_dir(base_dir / "cache")
    experiments_dir = ensure_dir(base_dir / "experiments")
    logs_dir = ensure_dir(base_dir / "logs")

    cache_datasets = ensure_dir(cache_dir / "datasets")
    cache_signals = ensure_dir(cache_dir / "signals")
    cache_books = ensure_dir(cache_dir / "books")
    cache_returns = ensure_dir(cache_dir / "returns")

    experiment_dir = ensure_dir(experiments_dir / stem)

    return {
        "base_dir": base_dir,
        "cache_dir": cache_dir,
        "experiments_dir": experiments_dir,
        "logs_dir": logs_dir,
        "experiment_dir": experiment_dir,
        "results_csv": logs_dir / "results.csv",
        "dataset": cache_datasets / f"{stem}.parquet",
        "features": cache_datasets / f"{stem}.features.parquet",
        "signals": cache_signals / f"{stem}.parquet",
        "book": cache_books / f"{stem}.parquet",
        "returns": cache_returns / f"{stem}.parquet",
        "evaluation": experiment_dir / "evaluation.json",
        "summary": experiment_dir / "summary.json",
        "dataset_meta": experiment_dir / "dataset.metadata.json",
        "strategy_meta": experiment_dir / "signals.metadata.json",
        "backtest_meta": experiment_dir / "backtest.metadata.json",
        "notes": experiment_dir / "notes.md",
        "request_snapshot": experiment_dir / "request.json",
        "strategy_snapshot": experiment_dir / "strategy.py",
        "request_schema_snapshot": experiment_dir / "strategy_request.schema.json",
        "protocol_snapshot": experiment_dir / "experiment_protocol.json",
        "backtest_config_snapshot": experiment_dir / "backtest_config.json",
        "evaluation_config_snapshot": experiment_dir / "evaluation_config.json",
    }


def maybe_promote_heavy_files(
    *,
    keep_heavy_files: bool,
    promote_score: float | None,
    score: float | None,
    actual_dataset_path: Path,
    actual_features_path: Path,
    actual_signals_path: Path,
    actual_book_path: Path,
    actual_returns_path: Path,
    experiment_dir: Path,
) -> tuple[bool, list[str]]:
    promoted = False
    copied: list[str] = []

    if keep_heavy_files:
        promoted = True
    elif promote_score is not None and score is not None and score >= promote_score:
        promoted = True

    if not promoted:
        return promoted, copied

    targets = {
        actual_dataset_path: experiment_dir / actual_dataset_path.name,
        actual_features_path: experiment_dir / actual_features_path.name,
        actual_signals_path: experiment_dir / actual_signals_path.name,
        actual_book_path: experiment_dir / actual_book_path.name,
        actual_returns_path: experiment_dir / actual_returns_path.name,
    }
    for src, dst in targets.items():
        if src.exists():
            shutil.copy2(src, dst)
            copied.append(str(dst.resolve()))

    return promoted, copied


def create_notes_placeholder(path: Path, *, strategy_name: str, run_id: str) -> None:
    if path.exists():
        return
    path.write_text(
        "\n".join(
            [
                f"# {strategy_name} / {run_id}",
                "",
                "- hypothesis:",
                "- requested fields:",
                "- evaluation profile:",
                "- horizon rationale:",
                "- main score formula:",
                "- expected benefit:",
                "- actual score:",
                "- actual weaknesses:",
                "- status: keep / discard / crash",
                "- next change:",
                "",
            ]
        ),
        encoding="utf-8",
    )


# -----------------------------------------------------------------------------
# Main run
# -----------------------------------------------------------------------------


def run_experiment(
    *,
    db_path: str | Path,
    request_path: str | Path,
    base_dir: str | Path,
    request_schema_path: str | Path | None = None,
    protocol_path: str | Path = "config/experiment_protocol.json",
    backtest_config_path: str | Path | None = None,
    evaluation_config_path: str | Path | None = None,
    date_window_name: str | None = None,
    run_id: str | None = None,
    cleanup_cache: bool = True,
    keep_heavy_files: bool = False,
    promote_score: float | None = None,
    status: str = "unknown",
) -> dict[str, Any]:
    request = load_json(request_path)
    protocol = load_experiment_protocol(protocol_path)
    resolved = resolve_profile_and_window(
        request=request,
        protocol=protocol,
        date_window_name=date_window_name,
    )

    strategy_name = request.get("strategy_name", "strategy")
    run_id = run_id or datetime.now().strftime("%Y%m%d_%H%M%S")
    stem = f"{slugify(strategy_name)}__{run_id}"

    base_dir = Path(base_dir)
    ensure_dir(base_dir)
    layout = build_layout(base_dir, stem)

    copy_if_exists(request_path, layout["request_snapshot"])
    local_strategy_file = Path(__file__).resolve().parent / "strategy.py"
    copy_if_exists(local_strategy_file, layout["strategy_snapshot"])
    copy_if_exists(request_schema_path, layout["request_schema_snapshot"])
    copy_if_exists(protocol_path, layout["protocol_snapshot"])
    copy_if_exists(backtest_config_path, layout["backtest_config_snapshot"])
    copy_if_exists(evaluation_config_path, layout["evaluation_config_snapshot"])
    create_notes_placeholder(layout["notes"], strategy_name=strategy_name, run_id=run_id)

    # Stage 1: dataset build. The builder also resolves the protocol and
    # records the selected profile/date window in dataset metadata.
    dataset_df, dataset_meta = build_strategy_dataset(
        db_path=db_path,
        request_path=request_path,
        output_path=layout["dataset"],
        schema_path=request_schema_path,
        protocol_path=protocol_path,
        date_window_name=resolved["date_window_name"],
    )
    actual_dataset_path = Path(dataset_meta["output_path"])
    write_json(dataset_meta, layout["dataset_meta"])

    # Strategy.py receives a feature-only dataset so it cannot use future
    # returns as signal inputs. The full dataset is still used by backtest.py.
    feature_dataset_df = make_feature_only_dataset(dataset_df)
    save_bt_dataframe(feature_dataset_df, layout["features"])
    actual_features_path = resolve_actual_output(layout["features"])

    # Reuse the builder-resolved profile if available, because it is the
    # exact protocol actually used to create the dataset.
    profile = dict(dataset_meta.get("profile") or resolved["profile"])
    profile_name = dataset_meta.get("evaluation_profile") or resolved["profile_name"]
    date_window = dataset_meta.get("date_window") or resolved["date_window_name"]
    benchmark = dataset_meta.get("benchmark") or resolved["benchmark"]

    # Stage 2: strategy.
    signals_df, strategy_meta = run_strategy(actual_features_path, layout["signals"])
    actual_signals_path = resolve_actual_output(layout["signals"])
    write_json(strategy_meta, layout["strategy_meta"])

    # Stage 3: fixed backtest configured by protocol profile.
    bt_cfg = make_backtest_config(
        protocol=protocol,
        profile=profile,
        backtest_config_path=backtest_config_path,
    )
    strategy_dataset = load_bt_dataframe(actual_dataset_path)
    signals = load_bt_dataframe(actual_signals_path)
    book_df, returns_df = run_backtest(strategy_dataset, signals, bt_cfg)

    save_bt_dataframe(book_df, layout["book"])
    save_bt_dataframe(returns_df, layout["returns"])
    actual_book_path = resolve_actual_output(layout["book"])
    actual_returns_path = resolve_actual_output(layout["returns"])
    save_backtest_metadata(layout["backtest_meta"], bt_cfg, book_df, returns_df)

    # Stage 4: fixed evaluation configured by protocol profile.
    eval_cfg = make_evaluation_config(
        protocol=protocol,
        profile=profile,
        evaluation_config_path=evaluation_config_path,
    )
    metrics = evaluate_returns(returns_df, eval_cfg)
    save_evaluation_json(metrics, layout["evaluation"])

    score = metrics.get(eval_cfg.score_name)
    promoted, heavy_files_copied = maybe_promote_heavy_files(
        keep_heavy_files=keep_heavy_files,
        promote_score=promote_score,
        score=score,
        actual_dataset_path=actual_dataset_path,
        actual_features_path=actual_features_path,
        actual_signals_path=actual_signals_path,
        actual_book_path=actual_book_path,
        actual_returns_path=actual_returns_path,
        experiment_dir=layout["experiment_dir"],
    )

    summary = {
        "run_id": run_id,
        "strategy_name": strategy_name,
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "status": status,
        "request_path": str(Path(request_path).resolve()),
        "db_path": str(Path(db_path).resolve()),
        "protocol_path": str(Path(protocol_path).resolve()),
        "evaluation_profile": profile_name,
        "date_window": date_window,
        "benchmark": benchmark,
        "profile": profile,
        "promoted": promoted,
        "kept_heavy_files": bool(promoted),
        "heavy_files_copied": heavy_files_copied,
        "folders": {
            "base_dir": str(layout["base_dir"].resolve()),
            "cache_dir": str(layout["cache_dir"].resolve()),
            "experiment_dir": str(layout["experiment_dir"].resolve()),
            "logs_dir": str(layout["logs_dir"].resolve()),
        },
        "artifacts": {
            "dataset_cache": str(actual_dataset_path.resolve()),
            "features_cache": str(actual_features_path.resolve()),
            "signals_cache": str(actual_signals_path.resolve()),
            "book_cache": str(actual_book_path.resolve()),
            "returns_cache": str(actual_returns_path.resolve()),
            "evaluation": str(layout["evaluation"].resolve()),
            "summary": str(layout["summary"].resolve()),
            "dataset_metadata": str(layout["dataset_meta"].resolve()),
            "strategy_metadata": str(layout["strategy_meta"].resolve()),
            "backtest_metadata": str(layout["backtest_meta"].resolve()),
            "request_snapshot": str(layout["request_snapshot"].resolve()),
            "strategy_snapshot": str(layout["strategy_snapshot"].resolve()),
            "protocol_snapshot": str(layout["protocol_snapshot"].resolve()),
            "notes": str(layout["notes"].resolve()),
        },
        "dataset": {
            "row_count": int(len(dataset_df)),
            "column_count": int(len(dataset_df.columns)),
            "columns": list(dataset_df.columns),
        },
        "feature_dataset": {
            "row_count": int(len(feature_dataset_df)),
            "column_count": int(len(feature_dataset_df.columns)),
            "columns": list(feature_dataset_df.columns),
        },
        "signals": {
            "row_count": int(len(signals_df)),
        },
        "backtest": {
            "book_rows": int(len(book_df)),
            "return_rows": int(len(returns_df)),
            "config": getattr(bt_cfg, "__dict__", {}),
        },
        "evaluation": metrics,
    }
    write_json(summary, layout["summary"])

    append_results_row(
        layout["results_csv"],
        {
            "run_id": run_id,
            "timestamp": summary["timestamp"],
            "strategy_name": strategy_name,
            "evaluation_profile": profile_name,
            "date_window": date_window,
            "benchmark": benchmark,
            "request_path": str(Path(request_path).resolve()),
            "score": metrics.get("score"),
            "annualized_return": metrics.get("annualized_return"),
            "annualized_volatility": metrics.get("annualized_volatility"),
            "sharpe": metrics.get("sharpe"),
            "sortino": metrics.get("sortino"),
            "max_drawdown": metrics.get("max_drawdown"),
            "avg_turnover": metrics.get("avg_turnover"),
            "n_periods": metrics.get("n_periods"),
            "dataset_rows": int(len(dataset_df)),
            "dataset_cols": int(len(dataset_df.columns)),
            "signals_rows": int(len(signals_df)),
            "book_rows": int(len(book_df)),
            "return_rows": int(len(returns_df)),
            "status": status,
            "promoted": promoted,
            "kept_heavy_files": bool(promoted),
            "experiment_dir": str(layout["experiment_dir"].resolve()),
            "summary_path": str(layout["summary"].resolve()),
        },
    )

    if cleanup_cache:
        maybe_delete(actual_dataset_path)
        maybe_delete(actual_features_path)
        maybe_delete(actual_signals_path)
        maybe_delete(actual_book_path)
        maybe_delete(actual_returns_path)
        delete_cache_sidecars(actual_dataset_path)
        delete_cache_sidecars(actual_features_path)
        delete_cache_sidecars(actual_signals_path)
        delete_cache_sidecars(actual_book_path)
        delete_cache_sidecars(actual_returns_path)

    return summary


# -----------------------------------------------------------------------------
# CLI
# -----------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the full quant experiment pipeline end-to-end.")
    parser.add_argument("--db", required=True, help="Path to DuckDB database file")
    parser.add_argument("--request", required=True, help="Path to strategy_request.json")
    parser.add_argument("--base-dir", required=True, help="Base directory that will contain cache/, experiments/, logs/")
    parser.add_argument("--request-schema", default=None, help="Optional strategy_request.schema.json path")
    parser.add_argument("--protocol", default="config/experiment_protocol.json", help="Path to fixed experiment_protocol.json")
    parser.add_argument("--date-window", default=None, help="Optional fixed date window from experiment_protocol.json")
    parser.add_argument("--backtest-config", default=None, help="Optional human override backtest config JSON")
    parser.add_argument("--evaluation-config", default=None, help="Optional human override evaluation config JSON")
    parser.add_argument("--run-id", default=None, help="Optional fixed run id")
    parser.add_argument("--no-cleanup-cache", action="store_true", help="Keep cached dataset/signals/book/returns after scoring")
    parser.add_argument("--keep-heavy-files", action="store_true", help="Copy heavy files into the experiment folder")
    parser.add_argument("--promote-score", type=float, default=None, help="If score >= threshold, copy heavy files into the experiment folder")
    parser.add_argument(
        "--status",
        default="unknown",
        choices=["unknown", "keep", "discard", "crash", "baseline", "robustness"],
        help="Optional manual run status",
    )
    args = parser.parse_args()

    summary = run_experiment(
        db_path=args.db,
        request_path=args.request,
        base_dir=args.base_dir,
        request_schema_path=args.request_schema,
        protocol_path=args.protocol,
        backtest_config_path=args.backtest_config,
        evaluation_config_path=args.evaluation_config,
        date_window_name=args.date_window,
        run_id=args.run_id,
        cleanup_cache=not bool(args.no_cleanup_cache),
        keep_heavy_files=bool(args.keep_heavy_files),
        promote_score=args.promote_score,
        status=args.status,
    )

    score = summary["evaluation"].get("score")
    print(f"[PASS] Experiment completed. score={score}")
    print(f"[INFO] Profile: {summary['evaluation_profile']}")
    print(f"[INFO] Date window: {summary['date_window']}")
    print(f"[INFO] Benchmark: {summary['benchmark']}")
    print(f"[INFO] Experiment dir: {summary['folders']['experiment_dir']}")
    print(f"[INFO] Summary: {summary['artifacts']['summary']}")
    print(f"[INFO] Results log: {Path(summary['folders']['logs_dir']) / 'results.csv'}")


if __name__ == "__main__":
    main()
