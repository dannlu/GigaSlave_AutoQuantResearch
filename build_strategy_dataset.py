from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Iterable

import numpy as np
import pandas as pd

from data_api import DataAPIError, QuantDataAPI

try:  # optional dependency
    import jsonschema  # type: ignore
except Exception:  # pragma: no cover
    jsonschema = None


KEY_COLS = ["ts_code", "trade_date"]

LABEL_TO_HORIZON = {
    "ret_1d_fwd": 1,
    "ret_5d_fwd": 5,
    "ret_20d_fwd": 20,
    "excess_ret_1d_fwd": 1,
    "excess_ret_5d_fwd": 5,
    "excess_ret_20d_fwd": 20,
}


class RequestValidationError(Exception):
    pass


# -----------------------------------------------------------------------------
# JSON helpers
# -----------------------------------------------------------------------------


def load_json(path: str | Path) -> dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise RequestValidationError("JSON must be an object at the top level.")
    return data


def validate_request_against_schema(
    request: dict[str, Any],
    schema_path: str | Path | None,
) -> None:
    if schema_path is None:
        return

    schema = load_json(schema_path)

    if jsonschema is None:
        # Fallback checks when jsonschema is not installed.
        required = [
            "strategy_name",
            "evaluation_profile",
            "horizon_rationale",
            "tables",
            "universe",
            "derived_features",
        ]
        missing = [k for k in required if k not in request]
        if missing:
            raise RequestValidationError(f"Request missing required top-level fields: {missing}")
        return

    try:
        jsonschema.validate(instance=request, schema=schema)
    except Exception as exc:  # pragma: no cover - depends on optional package
        raise RequestValidationError(f"JSON schema validation failed: {exc}") from exc


# -----------------------------------------------------------------------------
# Protocol helpers
# -----------------------------------------------------------------------------


def load_experiment_protocol(protocol_path: str | Path) -> dict[str, Any]:
    protocol = load_json(protocol_path)

    required = [
        "benchmark",
        "default_evaluation_profile",
        "allowed_evaluation_profiles",
        "primary_date_window",
        "date_windows",
    ]
    missing = [k for k in required if k not in protocol]
    if missing:
        raise RequestValidationError(f"Experiment protocol missing required fields: {missing}")

    benchmark = protocol.get("benchmark", {})
    if not isinstance(benchmark, dict) or not benchmark.get("ts_code"):
        raise RequestValidationError("Experiment protocol must define benchmark.ts_code.")

    return protocol


def resolve_protocol_for_request(
    request: dict[str, Any],
    protocol: dict[str, Any],
    *,
    date_window_name: str | None = None,
) -> dict[str, Any]:
    profile_name = request.get("evaluation_profile") or protocol["default_evaluation_profile"]
    profiles = protocol["allowed_evaluation_profiles"]

    if profile_name not in profiles:
        allowed = sorted(profiles)
        raise RequestValidationError(
            f"Unknown evaluation_profile={profile_name!r}. Allowed profiles: {allowed}"
        )

    profile = dict(profiles[profile_name])
    label = profile["label_col"]
    if label not in LABEL_TO_HORIZON:
        raise RequestValidationError(f"Profile {profile_name!r} uses unsupported label_col: {label!r}")

    window_name = date_window_name or protocol["primary_date_window"]
    windows = protocol["date_windows"]
    if window_name not in windows:
        allowed = sorted(windows)
        raise RequestValidationError(f"Unknown date window={window_name!r}. Allowed windows: {allowed}")

    window = dict(windows[window_name])
    start_date = _norm_date(window["start_date"])
    end_date = _norm_date(window["end_date"])

    benchmark = protocol["benchmark"]["ts_code"]

    return {
        "evaluation_profile": profile_name,
        "profile": profile,
        "date_window": window_name,
        "date_window_config": window,
        "start_date": start_date,
        "end_date": end_date,
        "benchmark": benchmark,
        "benchmark_config": protocol["benchmark"],
        "label": label,
        "horizon": LABEL_TO_HORIZON[label],
        "benchmark_label_col": profile.get("benchmark_label_col"),
        "excess_label_col": profile.get("excess_label_col"),
    }


def assert_benchmark_available(
    api: QuantDataAPI,
    *,
    benchmark: str,
    start_date: str,
    end_date: str,
) -> None:
    try:
        bench_df = api.load_index_data(
            index_codes=benchmark,
            columns=["ts_code", "trade_date"],
            start_date=start_date,
            end_date=end_date,
        )
    except Exception as exc:
        raise RequestValidationError(
            f"Failed to load benchmark {benchmark!r} from index_data."
        ) from exc

    if bench_df.empty:
        raise RequestValidationError(
            f"Benchmark {benchmark!r} was not found in index_data for "
            f"{start_date} to {end_date}."
        )


# -----------------------------------------------------------------------------
# Request planning helpers
# -----------------------------------------------------------------------------


def _norm_date(value: str) -> str:
    return pd.Timestamp(value).strftime("%Y-%m-%d")


def _add_calendar_days(value: str, days: int) -> str:
    return (pd.Timestamp(value) + pd.Timedelta(days=int(days))).strftime("%Y-%m-%d")


def _dedupe_keep_order(values: Iterable[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for v in values:
        if v not in seen:
            seen.add(v)
            out.append(v)
    return out


def _feature_input_names(feature: dict[str, Any]) -> list[str]:
    ftype = feature["type"]
    if ftype == "ratio":
        return [feature["numerator"], feature["denominator"]]
    return [feature["column"]]


def derive_required_columns_from_request(
    request: dict[str, Any],
    *,
    label: str,
    benchmark: str,
) -> dict[str, list[str]]:
    tables = {k: list(v) for k, v in request["tables"].items()}
    tables.setdefault("stock_bar", [])
    tables.setdefault("daily_basic", [])
    tables.setdefault("stock_basic", [])

    universe = request.get("universe", {})

    # Dense daily base always needs these.
    tables["stock_bar"] = _dedupe_keep_order(KEY_COLS + ["close", *tables["stock_bar"]])

    # Universe / convenience columns from stock_basic.
    stock_basic_needed = list(tables["stock_basic"])
    if universe.get("min_days_listed", 0) > 0:
        stock_basic_needed.append("list_date")
    if universe.get("markets"):
        stock_basic_needed.append("market")
    if universe.get("include_st") is False:
        stock_basic_needed.append("name")
    tables["stock_basic"] = _dedupe_keep_order(["ts_code", *stock_basic_needed])

    # Daily_basic columns may be needed for universe or strategy usage.
    daily_basic_needed = list(tables["daily_basic"])
    if universe.get("min_amount") is not None:
        # use stock_bar.amount
        tables["stock_bar"] = _dedupe_keep_order([*tables["stock_bar"], "amount"])
    tables["daily_basic"] = _dedupe_keep_order([*KEY_COLS, *daily_basic_needed])

    # Index table is optional for user-requested raw index columns. Benchmark
    # forward returns are attached by protocol regardless of index_data request.
    if request.get("tables", {}).get("index_data"):
        tables.setdefault("index_data", [])

    # Derived-feature inputs may require source columns.
    for feature in request.get("derived_features", []):
        for raw_name in _feature_input_names(feature):
            if raw_name in {"open", "high", "low", "close", "pre_close", "change", "pct_chg", "vol", "amount"}:
                tables["stock_bar"] = _dedupe_keep_order([*tables["stock_bar"], raw_name])
            elif raw_name in {
                "turnover_rate", "turnover_rate_f", "volume_ratio", "pe", "pe_ttm", "pb", "ps", "ps_ttm",
                "dv_ratio", "dv_ttm", "total_share", "float_share", "free_share", "total_mv", "circ_mv"
            }:
                tables["daily_basic"] = _dedupe_keep_order([*tables["daily_basic"], raw_name])
            elif raw_name in {"name", "industry", "market", "list_date", "area", "symbol"}:
                tables["stock_basic"] = _dedupe_keep_order([*tables["stock_basic"], raw_name])
            # fundamental / SW inputs must be explicitly requested so the agent is intentional.

    return tables


def validate_requested_columns(api: QuantDataAPI, tables: dict[str, list[str]]) -> None:
    for table_name, cols in tables.items():
        api.validate_columns(table_name, cols)


# -----------------------------------------------------------------------------
# Column resolution and feature engineering
# -----------------------------------------------------------------------------


def build_raw_to_panel_column_map(request: dict[str, Any]) -> dict[str, str]:
    """
    Map raw request names to realized panel names.
    Dense tables keep their original names; sparse PIT tables get prefixes.
    """
    mapping: dict[str, str] = {
        "ts_code": "ts_code",
        "trade_date": "trade_date",
    }
    for table_name, cols in request["tables"].items():
        if table_name == "fina_indicator":
            for c in cols:
                mapping.setdefault(c, f"fin_{c}")
                mapping.setdefault(f"fin_{c}", f"fin_{c}")
        elif table_name == "sw_industry":
            for c in cols:
                mapping.setdefault(c, f"sw_{c}")
                mapping.setdefault(f"sw_{c}", f"sw_{c}")
        elif table_name == "index_data":
            for c in cols:
                mapping.setdefault(c, f"index_{c}")
                mapping.setdefault(f"index_{c}", f"index_{c}")
        else:
            for c in cols:
                mapping.setdefault(c, c)

    for c in [
        "days_since_listed", "is_tradable",
        "ret_1d_fwd", "ret_5d_fwd", "ret_20d_fwd",
        "bench_ret_1d_fwd", "bench_ret_5d_fwd", "bench_ret_20d_fwd",
        "excess_ret_1d_fwd", "excess_ret_5d_fwd", "excess_ret_20d_fwd",
    ]:
        mapping.setdefault(c, c)
    return mapping


def resolve_column_name(name: str, panel: pd.DataFrame, colmap: dict[str, str]) -> str:
    if name in panel.columns:
        return name
    mapped = colmap.get(name)
    if mapped and mapped in panel.columns:
        return mapped
    raise RequestValidationError(f"Column {name!r} is not available in the built panel.")


def add_requested_index_columns(
    api: QuantDataAPI,
    panel: pd.DataFrame,
    *,
    benchmark: str,
    requested_cols: list[str],
    start_date: str,
    end_date: str,
) -> pd.DataFrame:
    cols = _dedupe_keep_order(["ts_code", "trade_date", *requested_cols])
    index_df = api.load_index_data(
        index_codes=benchmark,
        columns=cols,
        start_date=start_date,
        end_date=end_date,
    )
    rename_map = {c: f"index_{c}" for c in index_df.columns if c not in {"trade_date", "ts_code"}}
    index_df = index_df.rename(columns=rename_map)
    keep_cols = ["trade_date", *rename_map.values()]
    index_df = index_df[keep_cols].drop_duplicates(subset=["trade_date"], keep="last")
    return panel.merge(index_df, on="trade_date", how="left", validate="many_to_one")


def add_derived_features(
    panel: pd.DataFrame,
    features: list[dict[str, Any]],
    colmap: dict[str, str],
) -> pd.DataFrame:
    out = panel.sort_values(["ts_code", "trade_date"]).copy()

    for feature in features:
        ftype = feature["type"]
        name = feature["name"]

        if name in out.columns:
            raise RequestValidationError(f"Derived feature name already exists: {name!r}")

        if ftype == "pct_change":
            col = resolve_column_name(feature["column"], out, colmap)
            window = int(feature["window"])
            out[name] = out.groupby("ts_code", group_keys=False)[col].pct_change(window)

        elif ftype in {"rolling_mean", "rolling_std"}:
            col = resolve_column_name(feature["column"], out, colmap)
            window = int(feature["window"])
            rolled = out.groupby("ts_code", group_keys=False)[col].rolling(window)
            if ftype == "rolling_mean":
                out[name] = rolled.mean().reset_index(level=0, drop=True)
            else:
                out[name] = rolled.std().reset_index(level=0, drop=True)

        elif ftype == "ratio":
            num = resolve_column_name(feature["numerator"], out, colmap)
            den = resolve_column_name(feature["denominator"], out, colmap)
            denom = out[den].replace({0: np.nan})
            out[name] = out[num] / denom

        elif ftype in {"zscore", "rank", "diff"}:
            col = resolve_column_name(feature["column"], out, colmap)
            groupby = feature.get("groupby")

            if ftype == "diff":
                if groupby is None:
                    groupby = "ts_code"
                if groupby not in out.columns:
                    raise RequestValidationError(f"diff groupby column not found: {groupby!r}")
                out[name] = out.groupby(groupby, group_keys=False)[col].diff()

            elif ftype == "rank":
                if groupby is None:
                    groupby = "trade_date"
                if groupby not in out.columns:
                    raise RequestValidationError(f"rank groupby column not found: {groupby!r}")
                out[name] = out.groupby(groupby, group_keys=False)[col].rank(pct=True, method="average")

            elif ftype == "zscore":
                if groupby is None:
                    groupby = "trade_date"
                if groupby not in out.columns:
                    raise RequestValidationError(f"zscore groupby column not found: {groupby!r}")
                grouped = out.groupby(groupby, group_keys=False)[col]
                mean = grouped.transform("mean")
                std = grouped.transform("std").replace({0: np.nan})
                out[name] = (out[col] - mean) / std

        else:  # pragma: no cover - schema should prevent this
            raise RequestValidationError(f"Unsupported derived feature type: {ftype!r}")

        colmap[name] = name

    return out.sort_values(["trade_date", "ts_code"]).reset_index(drop=True)


# -----------------------------------------------------------------------------
# Build function
# -----------------------------------------------------------------------------


def build_strategy_dataset(
    *,
    db_path: str | Path,
    request_path: str | Path,
    output_path: str | Path,
    schema_path: str | Path | None = None,
    protocol_path: str | Path = "config/experiment_protocol.json",
    date_window_name: str | None = None,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    request = load_json(request_path)
    validate_request_against_schema(request, schema_path)

    protocol = load_experiment_protocol(protocol_path)
    resolved = resolve_protocol_for_request(request, protocol, date_window_name=date_window_name)

    # Official evaluation window comes from the fixed protocol.
    # A forward-return label needs future prices, so raw data is loaded beyond
    # the official end date, labels are computed on the buffered panel, and the
    # final output is filtered back to the official window.
    start_date = resolved["start_date"]
    end_date = resolved["end_date"]
    label_buffer_calendar_days = int(protocol.get("label_buffer_calendar_days", 90))
    load_start_date = start_date
    load_end_date = _add_calendar_days(end_date, label_buffer_calendar_days)

    benchmark = resolved["benchmark"]
    label = resolved["label"]
    horizon = resolved["horizon"]
    tables = derive_required_columns_from_request(request, label=label, benchmark=benchmark)

    with QuantDataAPI(db_path) as api:
        api.assert_core_tables_exist()
        validate_requested_columns(api, tables)
        assert_benchmark_available(api, benchmark=benchmark, start_date=start_date, end_date=end_date)

        panel = api.load_daily_panel(
            start_date=load_start_date,
            end_date=load_end_date,
            stock_bar_columns=tables.get("stock_bar"),
            daily_basic_columns=tables.get("daily_basic"),
            stock_basic_columns=tables.get("stock_basic"),
        )

        # Build all standard horizons so a selected profile and diagnostic columns are available.
        panel = api.add_forward_returns(panel, horizons=(1, 5, 20))
        panel = api.attach_index_forward_returns(panel, index_code=benchmark, horizons=(1, 5, 20))
        panel = api.add_excess_return_columns(panel, horizons=(1, 5, 20))

        if request["tables"].get("index_data"):
            panel = add_requested_index_columns(
                api,
                panel,
                benchmark=benchmark,
                requested_cols=request["tables"]["index_data"],
                start_date=load_start_date,
                end_date=load_end_date,
            )

        if request["tables"].get("fina_indicator"):
            panel = api.attach_fina_indicator_pit(panel, columns=request["tables"]["fina_indicator"])

        if request["tables"].get("sw_industry"):
            panel = api.attach_sw_industry_pit(panel, columns=request["tables"]["sw_industry"])

        panel = api.add_listing_age(panel)
        panel = api.add_tradability_flag(
            panel,
            min_price=float(request["universe"].get("min_price", 2.0)),
            min_amount=float(request["universe"].get("min_amount", 20_000.0)),
            min_days_listed=int(request["universe"].get("min_days_listed", 120)),
            include_st=bool(request["universe"].get("include_st", False)),
        )

        if request["universe"].get("tradable_only", False):
            panel = panel.loc[panel["is_tradable"] == 1].copy()

        markets = request["universe"].get("markets") or []
        if markets:
            if "market" not in panel.columns:
                raise RequestValidationError("Universe requested markets filter, but market is not available.")
            panel = panel.loc[panel["market"].isin(markets)].copy()

    colmap = build_raw_to_panel_column_map(request)
    panel = add_derived_features(panel, request.get("derived_features", []), colmap)

    # Keep only the official protocol window in the final strategy dataset.
    # Buffered rows exist only to compute forward-return labels near the end.
    panel["trade_date"] = pd.to_datetime(panel["trade_date"], errors="coerce")
    panel = panel.loc[
        (panel["trade_date"] >= pd.Timestamp(start_date))
        & (panel["trade_date"] <= pd.Timestamp(end_date))
    ].copy()

    # Final column selection: keep keys, requested cols, system labels, profile labels, and bookkeeping.
    keep_cols: list[str] = [*KEY_COLS]

    for table_name, cols in request["tables"].items():
        if table_name == "fina_indicator":
            keep_cols.extend([f"fin_{c}" for c in cols if f"fin_{c}" in panel.columns])
        elif table_name == "sw_industry":
            keep_cols.extend([f"sw_{c}" for c in cols if f"sw_{c}" in panel.columns])
        elif table_name == "index_data":
            keep_cols.extend([f"index_{c}" for c in cols if f"index_{c}" in panel.columns])
        else:
            keep_cols.extend([c for c in cols if c in panel.columns])

    keep_cols.extend(
        [feature["name"] for feature in request.get("derived_features", []) if feature["name"] in panel.columns]
    )

    # Always keep the selected profile label and useful benchmark/excess columns.
    keep_cols.extend([label])
    for c in [
        resolved.get("benchmark_label_col"),
        resolved.get("excess_label_col"),
        f"bench_ret_{horizon}d_fwd",
        f"excess_ret_{horizon}d_fwd",
        f"ret_{horizon}d_fwd",
        "is_tradable",
        "days_since_listed",
    ]:
        if c:
            keep_cols.append(c)

    keep_cols = _dedupe_keep_order([c for c in keep_cols if c in panel.columns])
    dataset = panel[keep_cols].sort_values(["trade_date", "ts_code"]).reset_index(drop=True)

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    save_format = output_path.suffix.lower()
    if save_format == ".csv":
        dataset.to_csv(output_path, index=False)
        actual_output = output_path
    else:
        try:
            dataset.to_parquet(output_path, index=False)
            actual_output = output_path
        except Exception:
            fallback = output_path.with_suffix(".pkl")
            dataset.to_pickle(fallback)
            actual_output = fallback

    metadata = {
        "strategy_name": request["strategy_name"],
        "evaluation_profile": resolved["evaluation_profile"],
        "horizon_rationale": request.get("horizon_rationale"),
        "request_path": str(Path(request_path).resolve()),
        "protocol_path": str(Path(protocol_path).resolve()),
        "source_db": str(Path(db_path).resolve()),
        "output_path": str(actual_output.resolve()),
        "row_count": int(len(dataset)),
        "column_count": int(len(dataset.columns)),
        "columns": list(dataset.columns),
        "date_window": resolved["date_window"],
        "date_range": {"start_date": start_date, "end_date": end_date},
        "official_date_range": {"start_date": start_date, "end_date": end_date},
        "data_load_range": {"start_date": load_start_date, "end_date": load_end_date},
        "label_buffer_calendar_days": label_buffer_calendar_days,
        "benchmark": benchmark,
        "benchmark_name": resolved["benchmark_config"].get("name"),
        "label": label,
        "horizon": horizon,
        "profile": resolved["profile"],
        "requested_tables": list(request["tables"].keys()),
    }

    metadata_path = actual_output.with_suffix(actual_output.suffix + ".metadata.json")
    with open(metadata_path, "w", encoding="utf-8") as f:
        json.dump(metadata, f, ensure_ascii=False, indent=2)

    return dataset, metadata


# -----------------------------------------------------------------------------
# CLI
# -----------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(description="Build a small strategy dataset from a JSON request.")
    parser.add_argument("--db", required=True, help="Path to DuckDB database file")
    parser.add_argument("--request", required=True, help="Path to strategy_request.json")
    parser.add_argument("--output", required=True, help="Output path (.parquet preferred, .csv supported)")
    parser.add_argument("--schema", default=None, help="Optional path to strategy_request.schema.json for validation")
    parser.add_argument(
        "--protocol",
        default="config/experiment_protocol.json",
        help="Path to fixed experiment_protocol.json",
    )
    parser.add_argument(
        "--date-window",
        default=None,
        help="Optional fixed date window name from experiment_protocol.json. Defaults to protocol primary_date_window.",
    )
    args = parser.parse_args()

    dataset, metadata = build_strategy_dataset(
        db_path=args.db,
        request_path=args.request,
        output_path=args.output,
        schema_path=args.schema,
        protocol_path=args.protocol,
        date_window_name=args.date_window,
    )

    print(f"[PASS] Built strategy dataset: {metadata['output_path']}")
    print(f"[INFO] Rows: {len(dataset):,}")
    print(f"[INFO] Columns: {len(dataset.columns)}")
    print(f"[INFO] Profile: {metadata['evaluation_profile']}")
    print(f"[INFO] Label: {metadata['label']}")
    print(f"[INFO] Benchmark: {metadata['benchmark']}")


if __name__ == "__main__":
    main()
