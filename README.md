# GigaSlave

GigaSlave is my attempt to build an agent-driven quant research framework for A-share strategy mining.

The basic idea is that I want an LLM agent to keep grinding strategy ideas for me. The agent writes a strategy request, edits the strategy logic, runs the fixed pipeline, checks the result, and moves on. Literally eternal slavery.

This repo is mostly AI slop (so do this README. Guess that's why I can't find a job). I am very happy to take advice, criticism, fixes, or better design ideas.

## What this does

The framework is built around a fixed research loop:

```text
strategy request JSON
        ↓
build_strategy_dataset.py
        ↓
strategy.py
        ↓
backtest.py
        ↓
evaluation.py
        ↓
run_experiment_clean.py
        ↓
logs / experiments / robustness reports
```

The agent is supposed to modify only:

```text
requests/runs/<strategy_request>.json
strategy.py
```

The agent is not supposed to modify the infrastructure files.

## Main idea

The project separates three things:

1. **Data access**
   - handled by `data_api.py`
   - reads from a DuckDB database
   - keeps point-in-time rules in one place

2. **Strategy search**
   - controlled by request JSONs
   - signal logic lives in `strategy.py`
   - this is the part the agent can edit

3. **Judging**
   - handled by fixed `backtest.py` and `evaluation.py`
   - agents should not be able to change the judge

The goal is to reduce cheating / accidental leakage while still letting the agent explore ideas.

## Project structure

```text
project/
├─ config/
│  ├─ schema.md
│  ├─ strategy_request.schema.json
│  └─ experiment_protocol.json
│
├─ data/
│  └─ data.db                  # not included in git
│
├─ requests/
│  ├─ templates/
│  │  └─ strategy_request_example.json
│  └─ runs/
│     └─ baseline_value_momentum.json
│
├─ runs/
│  ├─ cache/                   # temporary files, ignored
│  ├─ experiments/             # compact experiment records
│  ├─ logs/
│  │  └─ results.csv
│  └─ robustness/
│
├─ data_api.py
├─ build_strategy_dataset.py
├─ strategy.py
├─ backtest.py
├─ evaluation.py
├─ run_experiment_clean.py
├─ run_robustness.py
└─ program_zh.md
```

## Important files

### `program_zh.md`

This is the instruction file I give to the agent. It explains what the agent can and cannot touch. It is in Chinese since this framework is currently for A-share, and I'm gonna use DS4 (IT IS CHEAP!)

### `config/experiment_protocol.json`

This file defines the fixed experiment protocol:

- benchmark
- evaluation profiles
- date windows
- return horizons
- rebalance frequency
- annualization settings
- backtest defaults
- evaluation defaults

The agent can choose an approved `evaluation_profile`, but cannot invent arbitrary benchmarks, labels, or date ranges.

### `config/strategy_request.schema.json`

This controls the shape of a valid strategy request.

### `build_strategy_dataset.py`

Builds the smaller dataset needed for a specific strategy request. It also applies the protocol date window and computes forward-return labels.

### `strategy.py`

The agent-editable strategy logic. It reads the feature dataset and outputs scores.

### `backtest.py`

Fixed cross-sectional backtester.

### `evaluation.py`

Fixed metric and scoring logic.

### `run_experiment_clean.py`

Runs one full experiment.

### `run_robustness.py`

Runs the same strategy across multiple fixed date windows to check whether the idea is just overfitting one period.

## Example usage

Run one baseline:

```powershell
python run_experiment_clean.py `
  --db "data\data.db" `
  --request "requests/runs/baseline_value_momentum.json" `
  --request-schema "config/strategy_request.schema.json" `
  --protocol "config/experiment_protocol.json" `
  --base-dir "runs" `
  --status baseline
```

Run robustness checks:

```powershell
python run_robustness.py `
  --db "data\data.db" `
  --request "requests/runs/baseline_value_momentum.json" `
  --request-schema "config/strategy_request.schema.json" `
  --protocol "config/experiment_protocol.json" `
  --base-dir "runs"
```

## Notes

This repo does **not** include my raw `data.db` because it is too large.

The data is expected to be a DuckDB database with tables like:

- `stock_bar`
- `daily_basic`
- `fina_indicator`
- `stock_basic`
- `index_data`
- `sw_industry`

A lot of the current design is built around Tushare-style A-share data.

## Current status

The framework can:

- build strategy-specific datasets
- prevent the strategy file from seeing forward-return labels
- run fixed backtests
- evaluate results
- log experiments
- clean heavy cache files
- run robustness checks across fixed windows

It is still rough. The baseline strategy is not impressive. The point of the repo is the framework, not the current alpha.

## Philosophy

I LOVE SLAVERY.

## Contributions / advice

This is mostly AI-generated, duct-taped, semi-chaotic research tooling.

If you have advice on:

- avoiding leakage
- better A-share backtest assumptions
- better factor research workflow
- better evaluation metrics
- better agent instructions
- better file structure
- better anything

I am happy to hear it.

No investment advice. Probably broken in many places. If you used this (why would you do that?), the only thing I can do is to laugh at you. No responsibility will be taken.
