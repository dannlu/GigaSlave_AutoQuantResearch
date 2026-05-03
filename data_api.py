from __future__ import annotations

"""
Safe data-access layer for the quant research system.

Design goals
------------
- Provide consistent, typed access to the raw DuckDB warehouse.
- Centralize date parsing and schema validation.
- Expose safe point-in-time (PIT) joins for sparse event data such as
  fundamentals and SW industry membership.
- Keep risky logic (raw SQL, PIT joins, join-key conventions) out of the
  agent-editable strategy layer.

This file is intended to be FIXED / read-only infrastructure.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping, Sequence
import duckdb
import math

import numpy as np
import pandas as pd


# -----------------------------------------------------------------------------
# Constants
# -----------------------------------------------------------------------------

DATE_FMT = "%Y%m%d"

DAILY_KEY = ["ts_code", "trade_date"]

CORE_TABLES = {
    "stock_bar",
    "daily_basic",
    "fina_indicator",
    "index_data",
    "stock_basic",
    "sw_industry",
}

DEFAULT_STOCK_BAR_COLUMNS = [
    "ts_code",
    "trade_date",
    "open",
    "high",
    "low",
    "close",
    "pre_close",
    "change",
    "pct_chg",
    "vol",
    "amount",
]

DEFAULT_DAILY_BASIC_COLUMNS = [
    "ts_code",
    "trade_date",
    "close",
    "turnover_rate",
    "turnover_rate_f",
    "volume_ratio",
    "pe",
    "pe_ttm",
    "pb",
    "ps",
    "ps_ttm",
    "dv_ratio",
    "dv_ttm",
    "total_share",
    "float_share",
    "free_share",
    "total_mv",
    "circ_mv",
]

DEFAULT_FINA_INDICATOR_COLUMNS = [
    "ts_code",
    "ann_date",
    "end_date",
    "eps",
    "dt_eps",
    "total_revenue_ps",
    "revenue_ps",
    "capital_rese_ps",
    "surplus_rese_ps",
    "undist_profit_ps",
    "extra_item",
    "profit_dedt",
    "gross_margin",
    "current_ratio",
    "quick_ratio",
    "cash_ratio",
    "ar_turn",
    "ca_turn",
    "fa_turn",
    "assets_turn",
    "op_income",
    "ebit",
    "ebitda",
    "fcff",
    "fcfe",
    "current_exint",
    "noncurrent_exint",
    "interestdebt",
    "netdebt",
    "tangible_asset",
    "working_capital",
    "networking_capital",
    "invest_capital",
    "retained_earnings",
    "diluted2_eps",
    "bps",
    "ocfps",
    "retainedps",
    "cfps",
    "ebit_ps",
    "fcff_ps",
    "fcfe_ps",
    "netprofit_margin",
    "grossprofit_margin",
    "cogs_of_sales",
    "expense_of_sales",
    "profit_to_gr",
    "saleexp_to_gr",
    "adminexp_of_gr",
    "finaexp_of_gr",
    "impai_ttm",
    "gc_of_gr",
    "op_of_gr",
    "ebit_of_gr",
    "roe",
    "roe_waa",
    "roe_dt",
    "roa",
    "npta",
    "roic",
    "roe_yearly",
    "roa2_yearly",
    "debt_to_assets",
    "assets_to_eqt",
    "dp_assets_to_eqt",
    "ca_to_assets",
    "nca_to_assets",
    "tbassets_to_totalassets",
    "int_to_talcap",
    "eqt_to_talcapital",
    "currentdebt_to_debt",
    "longdeb_to_debt",
    "ocf_to_shortdebt",
    "debt_to_eqt",
    "eqt_to_debt",
    "eqt_to_interestdebt",
    "tangibleasset_to_debt",
    "tangasset_to_intdebt",
    "tangibleasset_to_netdebt",
    "ocf_to_debt",
    "turn_days",
    "roa_yearly",
    "roa_dp",
    "fixed_assets",
    "profit_to_op",
    "q_saleexp_to_gr",
    "q_gc_to_gr",
    "q_roe",
    "q_dt_roe",
    "q_npta",
    "q_ocf_to_sales",
    "basic_eps_yoy",
    "dt_eps_yoy",
    "cfps_yoy",
    "op_yoy",
    "ebt_yoy",
    "netprofit_yoy",
    "dt_netprofit_yoy",
    "ocf_yoy",
    "roe_yoy",
    "bps_yoy",
    "assets_yoy",
    "eqt_yoy",
    "tr_yoy",
    "or_yoy",
    "q_sales_yoy",
    "q_op_qoq",
    "equity_yoy",
]

DEFAULT_INDEX_DATA_COLUMNS = [
    "ts_code",
    "trade_date",
    "close",
    "open",
    "high",
    "low",
    "pre_close",
    "change",
    "pct_chg",
    "vol",
    "amount",
]

DEFAULT_STOCK_BASIC_COLUMNS = [
    "ts_code",
    "symbol",
    "name",
    "area",
    "industry",
    "cnspell",
    "market",
    "list_date",
    "act_name",
    "act_ent_type",
]

DEFAULT_SW_INDUSTRY_COLUMNS = [
    "l1_code",
    "l1_name",
    "l2_code",
    "l2_name",
    "l3_code",
    "l3_name",
    "ts_code",
    "name",
    "in_date",
    "out_date",
    "is_new",
]


@dataclass(frozen=True)
class DateRange:
    start_date: str | None = None
    end_date: str | None = None


class DataAPIError(Exception):
    """Raised when the data API encounters a schema or usage error."""


# -----------------------------------------------------------------------------
# Helper functions
# -----------------------------------------------------------------------------


def _coerce_iterable(values: str | Iterable[str] | None) -> list[str] | None:
    if values is None:
        return None
    if isinstance(values, str):
        return [values]
    result = [str(v) for v in values if v is not None]
    return result or None



def _normalize_date(value: str | int | pd.Timestamp | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, pd.Timestamp):
        return value.strftime(DATE_FMT)
    value = str(value).strip()
    if not value:
        return None
    if len(value) == 8 and value.isdigit():
        return value
    try:
        return pd.Timestamp(value).strftime(DATE_FMT)
    except Exception as exc:  # pragma: no cover - defensive
        raise DataAPIError(f"Could not parse date value: {value!r}") from exc



def _parse_yyyymmdd(series: pd.Series) -> pd.Series:
    return pd.to_datetime(series.astype(str), format=DATE_FMT, errors="coerce")



def _sql_in_clause(values: Sequence[str], params: list[object]) -> str:
    placeholders = ", ".join("?" for _ in values)
    params.extend(values)
    return f"({placeholders})"



def _safe_divide(left: pd.Series, right: pd.Series) -> pd.Series:
    denom = right.replace({0: pd.NA})
    return left / denom


# -----------------------------------------------------------------------------
# Main API class
# -----------------------------------------------------------------------------


class QuantDataAPI:
    """
    Safe access layer over the raw DuckDB warehouse.

    Notes
    -----
    - All date filters accept strings like '20240131', '2024-01-31', or
      pandas Timestamps.
    - Daily table loaders return parsed pandas timestamps in `trade_date`.
    - Fundamental PIT joins use `ann_date <= trade_date`.
    - SW industry PIT joins use `in_date <= trade_date <= out_date` where
      `out_date` is treated as open-ended when missing / 0 / empty.
    """

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        if not self.db_path.exists():
            raise DataAPIError(f"Database file does not exist: {self.db_path}")
        self._conn = duckdb.connect(str(self.db_path), read_only=True)
        self._table_columns_cache: dict[str, list[str]] = {}

    # -- lifecycle -------------------------------------------------------------
    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "QuantDataAPI":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    # -- schema introspection --------------------------------------------------
    def list_tables(self) -> list[str]:
        df = self._conn.execute("SHOW TABLES").fetchdf()
        col = df.columns[0] if len(df.columns) else None
        if col is None:
            return []
        return df[col].astype(str).tolist()

    def assert_core_tables_exist(self) -> None:
        existing = set(self.list_tables())
        missing = sorted(CORE_TABLES - existing)
        if missing:
            raise DataAPIError(f"Missing required tables: {missing}")

    def get_table_columns(self, table_name: str) -> list[str]:
        if table_name in self._table_columns_cache:
            return self._table_columns_cache[table_name]
        info = self._conn.execute(
            """
            SELECT column_name, data_type, ordinal_position
            FROM information_schema.columns
            WHERE table_name = ?
            ORDER BY ordinal_position
            """,
            [table_name],
        ).fetchdf()
        if info.empty:
            raise DataAPIError(f"Table not found: {table_name}")
        cols = info["column_name"].astype(str).tolist()
        self._table_columns_cache[table_name] = cols
        return cols

    def describe_table(self, table_name: str) -> pd.DataFrame:
        info = self._conn.execute(
            """
            SELECT
                ordinal_position AS cid,
                column_name AS name,
                data_type AS type,
                FALSE AS notnull,
                NULL AS dflt_value,
                FALSE AS pk
            FROM information_schema.columns
            WHERE table_name = ?
            ORDER BY ordinal_position
            """,
            [table_name],
        ).fetchdf()
        if info.empty:
            raise DataAPIError(f"Table not found: {table_name}")
        return info

    def validate_columns(self, table_name: str, requested_columns: Sequence[str]) -> list[str]:
        existing = set(self.get_table_columns(table_name))
        missing = [c for c in requested_columns if c not in existing]
        if missing:
            raise DataAPIError(f"Missing columns in {table_name}: {missing}")
        return list(requested_columns)

    def pick_existing_columns(self, table_name: str, requested_columns: Sequence[str]) -> list[str]:
        existing = set(self.get_table_columns(table_name))
        return [c for c in requested_columns if c in existing]

    # -- generic SQL loader ----------------------------------------------------
    def _load_table(
        self,
        table_name: str,
        columns: Sequence[str] | None = None,
        *,
        date_col: str | None = None,
        start_date: str | int | pd.Timestamp | None = None,
        end_date: str | int | pd.Timestamp | None = None,
        ts_codes: str | Iterable[str] | None = None,
        where_sql: str | None = None,
        where_params: Sequence[object] | None = None,
    ) -> pd.DataFrame:
        existing_cols = self.get_table_columns(table_name)
        select_cols = list(columns) if columns else existing_cols
        self.validate_columns(table_name, select_cols)

        params: list[object] = []
        clauses: list[str] = []

        if date_col is not None:
            if date_col not in existing_cols:
                raise DataAPIError(f"{table_name} has no date column named {date_col!r}")
            start_norm = _normalize_date(start_date)
            end_norm = _normalize_date(end_date)
            if start_norm is not None:
                clauses.append(f"{date_col} >= ?")
                params.append(start_norm)
            if end_norm is not None:
                clauses.append(f"{date_col} <= ?")
                params.append(end_norm)

        code_list = _coerce_iterable(ts_codes)
        if code_list is not None:
            if "ts_code" not in existing_cols:
                raise DataAPIError(f"{table_name} has no ts_code column")
            clauses.append(f"ts_code IN {_sql_in_clause(code_list, params)}")

        if where_sql:
            clauses.append(f"({where_sql})")
            if where_params:
                params.extend(where_params)

        where_clause = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        select_clause = ", ".join(select_cols)
        sql = f"SELECT {select_clause} FROM {table_name}{where_clause}"
        return self._conn.execute(sql, params).fetchdf()

    # -- typed table loaders ---------------------------------------------------
    def load_stock_bar(
        self,
        *,
        columns: Sequence[str] | None = None,
        start_date: str | int | pd.Timestamp | None = None,
        end_date: str | int | pd.Timestamp | None = None,
        ts_codes: str | Iterable[str] | None = None,
    ) -> pd.DataFrame:
        cols = list(columns) if columns else DEFAULT_STOCK_BAR_COLUMNS
        df = self._load_table(
            "stock_bar",
            cols,
            date_col="trade_date",
            start_date=start_date,
            end_date=end_date,
            ts_codes=ts_codes,
        )
        return self._clean_daily_df(df, required_cols=["ts_code", "trade_date"])

    def load_daily_basic(
        self,
        *,
        columns: Sequence[str] | None = None,
        start_date: str | int | pd.Timestamp | None = None,
        end_date: str | int | pd.Timestamp | None = None,
        ts_codes: str | Iterable[str] | None = None,
    ) -> pd.DataFrame:
        cols = list(columns) if columns else self.pick_existing_columns(
            "daily_basic", DEFAULT_DAILY_BASIC_COLUMNS
        )
        df = self._load_table(
            "daily_basic",
            cols,
            date_col="trade_date",
            start_date=start_date,
            end_date=end_date,
            ts_codes=ts_codes,
        )
        return self._clean_daily_df(df, required_cols=["ts_code", "trade_date"])

    def load_fina_indicator(
        self,
        *,
        columns: Sequence[str] | None = None,
        ann_start_date: str | int | pd.Timestamp | None = None,
        ann_end_date: str | int | pd.Timestamp | None = None,
        ts_codes: str | Iterable[str] | None = None,
    ) -> pd.DataFrame:
        cols = list(columns) if columns else self.pick_existing_columns(
            "fina_indicator", DEFAULT_FINA_INDICATOR_COLUMNS
        )
        df = self._load_table(
            "fina_indicator",
            cols,
            date_col="ann_date",
            start_date=ann_start_date,
            end_date=ann_end_date,
            ts_codes=ts_codes,
        )
        return self._clean_fundamental_df(df)

    def load_index_data(
        self,
        *,
        index_codes: str | Iterable[str] | None = None,
        columns: Sequence[str] | None = None,
        start_date: str | int | pd.Timestamp | None = None,
        end_date: str | int | pd.Timestamp | None = None,
    ) -> pd.DataFrame:
        cols = list(columns) if columns else self.pick_existing_columns(
            "index_data", DEFAULT_INDEX_DATA_COLUMNS
        )
        df = self._load_table(
            "index_data",
            cols,
            date_col="trade_date",
            start_date=start_date,
            end_date=end_date,
            ts_codes=index_codes,
        )
        return self._clean_daily_df(df, required_cols=["ts_code", "trade_date"])

    def load_stock_basic(self, *, columns: Sequence[str] | None = None) -> pd.DataFrame:
        cols = list(columns) if columns else self.pick_existing_columns(
            "stock_basic", DEFAULT_STOCK_BASIC_COLUMNS
        )
        df = self._load_table("stock_basic", cols)
        if "list_date" in df.columns:
            df["list_date"] = _parse_yyyymmdd(df["list_date"])
        df = df.drop_duplicates(subset=["ts_code"], keep="last").reset_index(drop=True)
        return df

    def load_sw_industry(
        self,
        *,
        columns: Sequence[str] | None = None,
        ts_codes: str | Iterable[str] | None = None,
    ) -> pd.DataFrame:
        cols = list(columns) if columns else self.pick_existing_columns(
            "sw_industry", DEFAULT_SW_INDUSTRY_COLUMNS
        )
        df = self._load_table("sw_industry", cols, ts_codes=ts_codes)
        return self._clean_sw_industry_df(df)

    # -- cleaned join-ready dataframes ----------------------------------------
    @staticmethod
    def _clean_daily_df(df: pd.DataFrame, *, required_cols: Sequence[str]) -> pd.DataFrame:
        missing = [c for c in required_cols if c not in df.columns]
        if missing:
            raise DataAPIError(f"Missing required columns in daily dataframe: {missing}")
        out = df.copy()
        out["ts_code"] = out["ts_code"].astype(str)
        out["trade_date"] = _parse_yyyymmdd(out["trade_date"])
        out = out.dropna(subset=["ts_code", "trade_date"])
        out = out.drop_duplicates(subset=["ts_code", "trade_date"], keep="last")
        out = out.sort_values(["trade_date", "ts_code"]).reset_index(drop=True)
        return out

    @staticmethod
    def _clean_fundamental_df(df: pd.DataFrame) -> pd.DataFrame:
        required = ["ts_code", "ann_date"]
        missing = [c for c in required if c not in df.columns]
        if missing:
            raise DataAPIError(f"Missing required columns in fina_indicator dataframe: {missing}")
        out = df.copy()
        out["ts_code"] = out["ts_code"].astype(str)
        out["ann_date"] = _parse_yyyymmdd(out["ann_date"])
        if "end_date" in out.columns:
            out["end_date"] = _parse_yyyymmdd(out["end_date"])
        out = out.dropna(subset=["ts_code", "ann_date"])
        dedupe_keys = [c for c in ["ts_code", "ann_date", "end_date"] if c in out.columns]
        out = out.drop_duplicates(subset=dedupe_keys, keep="last")
        out = out.sort_values(["ann_date", "ts_code"]).reset_index(drop=True)
        return out

    @staticmethod
    def _clean_sw_industry_df(df: pd.DataFrame) -> pd.DataFrame:
        required = ["ts_code", "in_date"]
        missing = [c for c in required if c not in df.columns]
        if missing:
            raise DataAPIError(f"Missing required columns in sw_industry dataframe: {missing}")
        out = df.copy()
        out["ts_code"] = out["ts_code"].astype(str)
        out["in_date"] = _parse_yyyymmdd(out["in_date"])
        if "out_date" in out.columns:
            # Treat 0 / empty / NaN as open-ended.
            out["out_date"] = (
                out["out_date"]
                .replace({0: pd.NA, "0": pd.NA, "": pd.NA})
                .astype(str)
            )
            out["out_date"] = _parse_yyyymmdd(out["out_date"])
        out = out.dropna(subset=["ts_code", "in_date"])
        dedupe_keys = [c for c in ["ts_code", "l1_code", "l2_code", "l3_code", "in_date", "out_date"] if c in out.columns]
        out = out.drop_duplicates(subset=dedupe_keys, keep="last")
        out = out.sort_values(["in_date", "ts_code"]).reset_index(drop=True)
        return out

    # -- higher-level dataset builders ----------------------------------------
    def load_daily_panel(
        self,
        *,
        start_date: str | int | pd.Timestamp | None = None,
        end_date: str | int | pd.Timestamp | None = None,
        ts_codes: str | Iterable[str] | None = None,
        stock_bar_columns: Sequence[str] | None = None,
        daily_basic_columns: Sequence[str] | None = None,
        stock_basic_columns: Sequence[str] | None = None,
    ) -> pd.DataFrame:
        """
        Build the safe dense daily panel:
            stock_bar + daily_basic + stock_basic

        Notes
        -----
        - The join between stock_bar and daily_basic is on (ts_code, trade_date).
        - `stock_basic` is then attached on ts_code.
        - `close` in daily_basic is dropped if stock_bar already contains it.
        """
        stock_bar = self.load_stock_bar(
            columns=stock_bar_columns,
            start_date=start_date,
            end_date=end_date,
            ts_codes=ts_codes,
        )
        daily_basic = self.load_daily_basic(
            columns=daily_basic_columns,
            start_date=start_date,
            end_date=end_date,
            ts_codes=ts_codes,
        )

        overlap = sorted(set(stock_bar.columns) & set(daily_basic.columns) - set(DAILY_KEY))
        if overlap:
            # Keep stock_bar versions for overlapping daily columns such as `close`.
            daily_basic = daily_basic.drop(columns=overlap)

        panel = stock_bar.merge(
            daily_basic,
            how="left",
            on=DAILY_KEY,
            validate="one_to_one",
        )

        stock_basic = self.load_stock_basic(columns=stock_basic_columns)
        panel = panel.merge(stock_basic, how="left", on="ts_code", validate="many_to_one")
        return panel.sort_values(["trade_date", "ts_code"]).reset_index(drop=True)

    def attach_fina_indicator_pit(
        self,
        panel: pd.DataFrame,
        *,
        columns: Sequence[str] | None = None,
        prefix: str = "fin_",
    ) -> pd.DataFrame:
        """
        Safely attach fundamentals using the rule:
            same ts_code AND ann_date <= trade_date

        The latest announcement observable by each trade_date is used.
        Fundamental columns are prefixed to avoid collisions.
        """
        self._validate_panel_for_pit(panel)
        base_cols = ["ts_code", "ann_date", "end_date"]
        requested = list(columns) if columns else self.pick_existing_columns(
            "fina_indicator", DEFAULT_FINA_INDICATOR_COLUMNS
        )
        requested = [c for c in requested if c not in {"ts_code", "ann_date", "end_date"}]
        use_cols = self.pick_existing_columns("fina_indicator", base_cols + requested)
        fin = self.load_fina_indicator(columns=use_cols, ts_codes=panel["ts_code"].dropna().unique().tolist())
        fin = fin.rename(
            columns={
                c: f"{prefix}{c}"
                for c in fin.columns
                if c not in {"ts_code", "ann_date"}
            }
        )

        left = panel.sort_values(["trade_date", "ts_code"]).reset_index(drop=True)
        right = fin.sort_values(["ann_date", "ts_code"]).reset_index(drop=True)

        merged = pd.merge_asof(
            left,
            right,
            by="ts_code",
            left_on="trade_date",
            right_on="ann_date",
            direction="backward",
            allow_exact_matches=True,
        )
        return merged

    def attach_sw_industry_pit(
        self,
        panel: pd.DataFrame,
        *,
        columns: Sequence[str] | None = None,
        prefix: str = "sw_",
    ) -> pd.DataFrame:
        """
        Safely attach SW industry membership using:
            same ts_code AND in_date <= trade_date <= out_date
        where missing / empty / 0 out_date is treated as open-ended.

        When multiple rows are simultaneously valid, the record with the latest
        `in_date` is used.
        """
        self._validate_panel_for_pit(panel)
        base_cols = ["ts_code", "in_date", "out_date"]
        requested = list(columns) if columns else self.pick_existing_columns(
            "sw_industry", DEFAULT_SW_INDUSTRY_COLUMNS
        )
        requested = [c for c in requested if c not in {"ts_code", "in_date", "out_date"}]
        use_cols = self.pick_existing_columns("sw_industry", base_cols + requested)
        sw = self.load_sw_industry(columns=use_cols, ts_codes=panel["ts_code"].dropna().unique().tolist())
        sw = sw.rename(
            columns={
                c: f"{prefix}{c}"
                for c in sw.columns
                if c not in {"ts_code", "in_date", "out_date"}
            }
        )

        left = panel.sort_values(["trade_date", "ts_code"]).reset_index(drop=True)
        right = sw.sort_values(["in_date", "ts_code"]).reset_index(drop=True)

        merged = pd.merge_asof(
            left,
            right,
            by="ts_code",
            left_on="trade_date",
            right_on="in_date",
            direction="backward",
            allow_exact_matches=True,
        )

        if "out_date" in merged.columns:
            valid = merged["out_date"].isna() | (merged["trade_date"] <= merged["out_date"])
            sw_cols = [c for c in merged.columns if c.startswith(prefix)]
            invalid_mask = ~valid
            if invalid_mask.any():
                merged.loc[invalid_mask, sw_cols] = pd.NA
                merged.loc[invalid_mask, [c for c in ["in_date", "out_date"] if c in merged.columns]] = pd.NaT
        return merged

    @staticmethod
    def _validate_panel_for_pit(panel: pd.DataFrame) -> None:
        missing = [c for c in ["ts_code", "trade_date"] if c not in panel.columns]
        if missing:
            raise DataAPIError(
                f"Panel must contain columns {missing} before PIT joins can be applied."
            )
        if not pd.api.types.is_datetime64_any_dtype(panel["trade_date"]):
            raise DataAPIError("Panel trade_date must be pandas datetime64 before PIT joins.")

    # -- benchmark helpers -----------------------------------------------------
    def compute_index_forward_returns(
        self,
        index_code: str,
        *,
        horizons: Sequence[int] = (1, 5, 20),
        start_date: str | int | pd.Timestamp | None = None,
        end_date: str | int | pd.Timestamp | None = None,
    ) -> pd.DataFrame:
        index_df = self.load_index_data(
            index_codes=index_code,
            columns=["ts_code", "trade_date", "close"],
            start_date=start_date,
            end_date=end_date,
        )
        if index_df.empty:
            raise DataAPIError(f"No index_data rows found for benchmark {index_code!r}")
        index_df = index_df.sort_values(["trade_date"]).reset_index(drop=True)
        for h in horizons:
            index_df[f"bench_ret_{h}d_fwd"] = index_df["close"].shift(-h) / index_df["close"] - 1.0
        return index_df[["trade_date", *[f"bench_ret_{h}d_fwd" for h in horizons]]]

    def attach_index_forward_returns(
        self,
        panel: pd.DataFrame,
        *,
        index_code: str,
        horizons: Sequence[int] = (1, 5, 20),
    ) -> pd.DataFrame:
        if "trade_date" not in panel.columns:
            raise DataAPIError("Panel must contain trade_date to attach benchmark returns")
        bench = self.compute_index_forward_returns(index_code=index_code, horizons=horizons)
        merged = panel.merge(bench, how="left", on="trade_date", validate="many_to_one")
        return merged

    @staticmethod
    def add_excess_return_columns(
        panel: pd.DataFrame,
        *,
        horizons: Sequence[int] = (1, 5, 20),
        stock_return_prefix: str = "ret_",
        bench_return_prefix: str = "bench_ret_",
        excess_return_prefix: str = "excess_ret_",
    ) -> pd.DataFrame:
        out = panel.copy(deep=False)
        for h in horizons:
            stock_col = f"{stock_return_prefix}{h}d_fwd"
            bench_col = f"{bench_return_prefix}{h}d_fwd"
            excess_col = f"{excess_return_prefix}{h}d_fwd"
            if stock_col not in out.columns or bench_col not in out.columns:
                raise DataAPIError(
                    f"Cannot compute excess returns without {stock_col!r} and {bench_col!r}"
                )
            out[excess_col] = out[stock_col] - out[bench_col]
        return out

    # -- reusable market feature helpers --------------------------------------
    @staticmethod
    def add_forward_returns(
        panel: pd.DataFrame,
        *,
        price_col: str = "close",
        horizons: Sequence[int] = (1, 5, 20),
        group_col: str = "ts_code",
    ) -> pd.DataFrame:
        if group_col not in panel.columns or price_col not in panel.columns:
            raise DataAPIError(f"Panel must contain {group_col!r} and {price_col!r}")
        out = panel.sort_values([group_col, "trade_date"]).copy()
        grouped = out.groupby(group_col, group_keys=False)
        for h in horizons:
            out[f"ret_{h}d_fwd"] = grouped[price_col].shift(-h) / out[price_col] - 1.0
        return out.sort_values(["trade_date", group_col]).reset_index(drop=True)

    @staticmethod
    def add_standard_market_features(
        panel: pd.DataFrame,
        *,
        price_col: str = "close",
        volume_col: str = "vol",
        amount_col: str = "amount",
        turnover_col: str = "turnover_rate",
        group_col: str = "ts_code",
        momentum_windows: Sequence[int] = (5, 10, 20, 60, 120),
        volatility_windows: Sequence[int] = (20, 60),
        liquidity_window: int = 20,
    ) -> pd.DataFrame:
        required = [group_col, "trade_date", price_col]
        missing = [c for c in required if c not in panel.columns]
        if missing:
            raise DataAPIError(f"Panel missing required columns for market features: {missing}")

        out = panel.sort_values([group_col, "trade_date"]).copy()
        grouped = out.groupby(group_col, group_keys=False)

        out["ret_1d"] = grouped[price_col].pct_change()
        ratio = grouped[price_col].transform(lambda s: s / s.shift(1))
        ratio = ratio.where(ratio > 0)
        out["logret_1d"] = np.log(ratio)

        for window in momentum_windows:
            out[f"mom_{window}"] = grouped[price_col].pct_change(window)

        for window in volatility_windows:
            out[f"vol_{window}"] = grouped["ret_1d"].rolling(window).std().reset_index(level=0, drop=True)

        if amount_col in out.columns:
            out[f"avg_{amount_col}_{liquidity_window}"] = (
                grouped[amount_col].rolling(liquidity_window).mean().reset_index(level=0, drop=True)
            )
        if volume_col in out.columns:
            out[f"avg_{volume_col}_{liquidity_window}"] = (
                grouped[volume_col].rolling(liquidity_window).mean().reset_index(level=0, drop=True)
            )
        if turnover_col in out.columns:
            out[f"avg_{turnover_col}_{liquidity_window}"] = (
                grouped[turnover_col].rolling(liquidity_window).mean().reset_index(level=0, drop=True)
            )

        return out.sort_values(["trade_date", group_col]).reset_index(drop=True)

    @staticmethod
    def add_listing_age(panel: pd.DataFrame, *, list_date_col: str = "list_date") -> pd.DataFrame:
        if "trade_date" not in panel.columns or list_date_col not in panel.columns:
            raise DataAPIError("Panel must contain trade_date and list_date to compute listing age")
        out = panel.copy(deep=False)
        if not pd.api.types.is_datetime64_any_dtype(out[list_date_col]):
            out[list_date_col] = pd.to_datetime(out[list_date_col], errors="coerce")
        out["days_since_listed"] = (out["trade_date"] - out[list_date_col]).dt.days
        return out

    @staticmethod
    def add_tradability_flag(
        panel: pd.DataFrame,
        *,
        min_price: float = 2.0,
        min_amount: float = 20_000.0,
        min_days_listed: int = 120,
        include_st: bool = False,
        price_col: str = "close",
        amount_col: str = "amount",
        name_col: str = "name",
        days_listed_col: str = "days_since_listed",
        output_col: str = "is_tradable",
    ) -> pd.DataFrame:
        out = panel.copy(deep=False)
        mask = pd.Series(True, index=out.index)

        if price_col in out.columns:
            mask &= out[price_col].fillna(float("-inf")) >= float(min_price)
        if amount_col in out.columns:
            mask &= out[amount_col].fillna(float("-inf")) >= float(min_amount)
        if days_listed_col in out.columns:
            mask &= out[days_listed_col].fillna(-1) >= int(min_days_listed)
        if (not include_st) and name_col in out.columns:
            names = out[name_col].astype(str).str.upper()
            mask &= ~names.fillna("").str.contains("ST", regex=False)

        out[output_col] = mask.astype("int8")
        return out


__all__ = [
    "CORE_TABLES",
    "DAILY_KEY",
    "DataAPIError",
    "DateRange",
    "QuantDataAPI",
]
