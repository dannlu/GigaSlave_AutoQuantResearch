# Quant Research Data Dictionary v2

This document describes the raw warehouse schema for the quant research system.  
It is intended to be **read-only** guidance for agents and humans.

## Why this file exists

The raw database already contains the source data. This file exists so an agent does **not** need to rediscover the schema every run.
It should answer three questions quickly:

1. What tables exist?
2. What does each table mean?
3. What are the safe join / point-in-time rules?

## Global notes

- Raw database path from the uploaded summary: `D:\HKU\bigdatainfinance\SLOP\Data\data.db`.
- Date fields are stored as raw strings such as `YYYYMMDD` in the warehouse.
- Daily market tables are keyed by `(ts_code, trade_date)`.
- Fundamental data must be treated as **point-in-time**. For backtesting, fundamental values become visible on `ann_date`, **not** `end_date`.
- Static metadata comes from `stock_basic`.
- Benchmark returns come from `index_data`.

---

## Table: `stock_bar`

**Purpose**: Daily OHLCV-style market data for individual stocks.  
**Granularity**: One row per stock per trading day.  
**Likely key**: `(ts_code, trade_date)`

**Columns**:
- `ts_code` (VARCHAR)
- `trade_date` (VARCHAR)
- `open` (DOUBLE)
- `high` (DOUBLE)
- `low` (DOUBLE)
- `close` (DOUBLE)
- `pre_close` (DOUBLE)
- `change` (DOUBLE)
- `pct_chg` (DOUBLE)
- `vol` (DOUBLE)
- `amount` (DOUBLE)

**Typical uses**:
- returns
- momentum
- rolling volatility
- volume/liquidity filters
- technical indicators

---

## Table: `daily_basic`

**Purpose**: Daily valuation, turnover, and market-cap style fields for individual stocks.  
**Granularity**: One row per stock per trading day.  
**Likely key**: `(ts_code, trade_date)`

**Columns**:
- `ts_code` (VARCHAR)
- `trade_date` (VARCHAR)
- `close` (DOUBLE)
- `turnover_rate` (DOUBLE)
- `turnover_rate_f` (DOUBLE)
- `volume_ratio` (DOUBLE)
- `pe` (DOUBLE)
- `pe_ttm` (DOUBLE)
- `pb` (DOUBLE)
- `ps` (DOUBLE)
- `ps_ttm` (DOUBLE)
- `dv_ratio` (DOUBLE)
- `dv_ttm` (DOUBLE)
- `total_share` (DOUBLE)
- `float_share` (DOUBLE)
- `free_share` (DOUBLE)
- `total_mv` (DOUBLE)
- `circ_mv` (DOUBLE)

**Typical uses**:
- value factors
- liquidity screens
- market-cap segmentation
- turnover constraints

---

## Table: `fina_indicator`

**Purpose**: Fundamental indicators and accounting-derived metrics.  
**Granularity**: One row per stock per financial report / announcement event.  
**Likely key**: sparse event data keyed by stock and announcement/report dates.

**Important date columns**:
- `ann_date`: when the market could observe the data
- `end_date`: reporting period end

**Point-in-time rule**:
- For backtesting, use `ann_date <= trade_date`.
- Do **not** use `end_date` as the visibility date.

**Columns**:
- `ts_code` (VARCHAR)
- `ann_date` (VARCHAR)
- `end_date` (VARCHAR)
- `eps` (DOUBLE)
- `dt_eps` (DOUBLE)
- `total_revenue_ps` (DOUBLE)
- `revenue_ps` (DOUBLE)
- `capital_rese_ps` (DOUBLE)
- `surplus_rese_ps` (DOUBLE)
- `undist_profit_ps` (DOUBLE)
- `extra_item` (DOUBLE)
- `profit_dedt` (DOUBLE)
- `gross_margin` (DOUBLE)
- `current_ratio` (DOUBLE)
- `quick_ratio` (DOUBLE)
- `cash_ratio` (DOUBLE)
- `ar_turn` (DOUBLE)
- `ca_turn` (DOUBLE)
- `fa_turn` (DOUBLE)
- `assets_turn` (DOUBLE)
- `op_income` (DOUBLE)
- `ebit` (DOUBLE)
- `ebitda` (DOUBLE)
- `fcff` (DOUBLE)
- `fcfe` (DOUBLE)
- `current_exint` (DOUBLE)
- `noncurrent_exint` (DOUBLE)
- `interestdebt` (DOUBLE)
- `netdebt` (DOUBLE)
- `tangible_asset` (DOUBLE)
- `working_capital` (DOUBLE)
- `networking_capital` (DOUBLE)
- `invest_capital` (DOUBLE)
- `retained_earnings` (DOUBLE)
- `diluted2_eps` (DOUBLE)
- `bps` (DOUBLE)
- `ocfps` (DOUBLE)
- `retainedps` (DOUBLE)
- `cfps` (DOUBLE)
- `ebit_ps` (DOUBLE)
- `fcff_ps` (DOUBLE)
- `fcfe_ps` (DOUBLE)
- `netprofit_margin` (DOUBLE)
- `grossprofit_margin` (DOUBLE)
- `cogs_of_sales` (DOUBLE)
- `expense_of_sales` (DOUBLE)
- `profit_to_gr` (DOUBLE)
- `saleexp_to_gr` (DOUBLE)
- `adminexp_of_gr` (DOUBLE)
- `finaexp_of_gr` (DOUBLE)
- `impai_ttm` (DOUBLE)
- `gc_of_gr` (DOUBLE)
- `op_of_gr` (DOUBLE)
- `ebit_of_gr` (DOUBLE)
- `roe` (DOUBLE)
- `roe_waa` (DOUBLE)
- `roe_dt` (DOUBLE)
- `roa` (DOUBLE)
- `npta` (DOUBLE)
- `roic` (DOUBLE)
- `roe_yearly` (DOUBLE)
- `roa2_yearly` (DOUBLE)
- `debt_to_assets` (DOUBLE)
- `assets_to_eqt` (DOUBLE)
- `dp_assets_to_eqt` (DOUBLE)
- `ca_to_assets` (DOUBLE)
- `nca_to_assets` (DOUBLE)
- `tbassets_to_totalassets` (DOUBLE)
- `int_to_talcap` (DOUBLE)
- `eqt_to_talcapital` (DOUBLE)
- `currentdebt_to_debt` (DOUBLE)
- `longdeb_to_debt` (DOUBLE)
- `ocf_to_shortdebt` (DOUBLE)
- `debt_to_eqt` (DOUBLE)
- `eqt_to_debt` (DOUBLE)
- `eqt_to_interestdebt` (DOUBLE)
- `tangibleasset_to_debt` (DOUBLE)
- `tangasset_to_intdebt` (DOUBLE)
- `tangibleasset_to_netdebt` (DOUBLE)
- `ocf_to_debt` (DOUBLE)
- `turn_days` (DOUBLE)
- `roa_yearly` (DOUBLE)
- `roa_dp` (DOUBLE)
- `fixed_assets` (DOUBLE)
- `profit_to_op` (DOUBLE)
- `q_saleexp_to_gr` (DOUBLE)
- `q_gc_to_gr` (DOUBLE)
- `q_roe` (DOUBLE)
- `q_dt_roe` (DOUBLE)
- `q_npta` (DOUBLE)
- `q_ocf_to_sales` (DOUBLE)
- `basic_eps_yoy` (DOUBLE)
- `dt_eps_yoy` (DOUBLE)
- `cfps_yoy` (DOUBLE)
- `op_yoy` (DOUBLE)
- `ebt_yoy` (DOUBLE)
- `netprofit_yoy` (DOUBLE)
- `dt_netprofit_yoy` (DOUBLE)
- `ocf_yoy` (DOUBLE)
- `roe_yoy` (DOUBLE)
- `bps_yoy` (DOUBLE)
- `assets_yoy` (DOUBLE)
- `eqt_yoy` (DOUBLE)
- `tr_yoy` (DOUBLE)
- `or_yoy` (DOUBLE)
- `q_sales_yoy` (DOUBLE)
- `q_op_qoq` (DOUBLE)
- `equity_yoy` (DOUBLE)

**Typical uses**:
- value / quality / profitability factors
- growth factors
- leverage and balance-sheet filters
- accounting-based composite signals

---

## Table: `index_data`

**Purpose**: Daily benchmark/index data.  
**Granularity**: One row per index per trading day.  
**Likely key**: `(ts_code, trade_date)`

**Columns**:
- `ts_code` (VARCHAR)
- `trade_date` (VARCHAR)
- `close` (DOUBLE)
- `open` (DOUBLE)
- `high` (DOUBLE)
- `low` (DOUBLE)
- `pre_close` (DOUBLE)
- `change` (DOUBLE)
- `pct_chg` (DOUBLE)
- `vol` (DOUBLE)
- `amount` (DOUBLE)

**Typical uses**:
- benchmark returns
- excess-return labels
- regime filters

---

## Table: `stock_basic`

**Purpose**: Static stock metadata.  
**Granularity**: One row per stock.  
**Likely key**: `ts_code`

**Columns**:
- `ts_code` (VARCHAR)
- `symbol` (VARCHAR)
- `name` (VARCHAR)
- `area` (VARCHAR)
- `industry` (VARCHAR)
- `cnspell` (VARCHAR)
- `market` (VARCHAR)
- `list_date` (VARCHAR)
- `act_name` (VARCHAR)
- `act_ent_type` (VARCHAR)

**Typical uses**:
- listing-age filters
- industry grouping
- exchange/market segmentation
- ST-name filtering via `name` pattern if needed

---

## Table: `sw_industry`

**Purpose**: Shenwan industry classification / membership style table.  
**Granularity**: likely one row per stock per industry-membership record.  
**Likely key**: not a single static key; treat as history keyed by stock plus entry/exit dates.

**Columns**:
- `l1_code` (VARCHAR)
- `l1_name` (VARCHAR)
- `l2_code` (VARCHAR)
- `l2_name` (VARCHAR)
- `l3_code` (VARCHAR)
- `l3_name` (VARCHAR)
- `ts_code` (VARCHAR)
- `name` (VARCHAR)
- `in_date` (VARCHAR)
- `out_date` (INTEGER)
- `is_new` (VARCHAR)

**Interpretation notes**:
- `l1_*`, `l2_*`, `l3_*` are level-1 / level-2 / level-3 Shenwan industry codes and names.
- `ts_code` links the row to a stock.
- `in_date` is the date the stock enters the industry assignment.
- `out_date` is the date the assignment ends.
- `is_new` marks the currently active or newly updated record and should be checked on real rows before relying on it.

**Safe usage hint**:
- For a given `trade_date`, the safe point-in-time industry is the row where:
  - same `ts_code`
  - `in_date <= trade_date`
  - and either `out_date` is null / empty / 0, or `trade_date <= out_date`

---

## Agent-friendly name hints

Many raw names are abbreviated. Agents can still use them, but the schema file should provide a plain-English hint layer.
Use the local schema file as the primary source.
If a field meaning is unclear, check the official Tushare documentation for that specific interface. (https://tushare.pro/document/2)
Do not guess the meaning of ambiguous fields.
Do not change point-in-time rules or join logic based on documentation lookup.

**Recommended rule for future schema versions**:
For each important raw column, document three things:
- raw name
- plain-English meaning
- common research use

Example:
- `pe_ttm` → trailing-twelve-month price-to-earnings → value factor
- `q_op_qoq` → quarter-over-quarter change in operating profit → growth / acceleration factor
- `debt_to_assets` → leverage ratio → risk / balance-sheet filter

---

## Safe join guidance

### Safe daily join
- `stock_bar` + `daily_basic` on `(ts_code, trade_date)`

### Safe static join
- daily panel + `stock_basic` on `ts_code`

### Safe benchmark join
- merge benchmark returns from `index_data` on `trade_date`

### Safe fundamental join
- attach the latest `fina_indicator` row where:
  - same `ts_code`
  - `ann_date <= trade_date`

### Safe SW industry join
- attach the row in `sw_industry` where:
  - same `ts_code`
  - `in_date <= trade_date`
  - `out_date` is missing / open-ended, or `trade_date <= out_date`

---
