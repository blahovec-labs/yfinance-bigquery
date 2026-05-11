# yfinance-bigquery

Idempotent Yahoo Finance OHLCV → BigQuery ingestion across 5 intervals, with
first-class documentation for SQL/LLM agents and internal-consistency
verification.

## Install

    pip install yfinance-bigquery

## Quickstart

    gcloud auth application-default login

    # 1. Seed your symbol universe from the S&P 500 Wikipedia page
    yfinance-bigquery universe init \
        --dim-symbols myproject.mydataset.dim_symbols \
        --create-if-missing

    # 2. Sync the last week of daily bars for every active ticker
    yfinance-bigquery sync \
        --interval 1d \
        --dataset myproject.mydataset.yfinance_v2_analytics \
        --dim-symbols myproject.mydataset.dim_symbols

    # 3. Spot-check internal consistency for the current year
    yfinance-bigquery verify \
        --source internal \
        --interval 1d \
        --aggregation symbol-season \
        --metric all \
        --season 2026 \
        --table myproject.mydataset.ohlcv_1d

## Backfill

Backfill all 5 intervals in resumable yearly chunks:

    yfinance-bigquery sync \
        --interval all \
        --start 2020-01-01 --end 2026-05-11 \
        --chunk-by year --resume \
        --dataset myproject.mydataset.yfinance_v2_analytics \
        --dim-symbols myproject.mydataset.dim_symbols

`--resume` skips chunks already recorded as success in
`<dataset>._yfinance_ingest_runs`. Override with `--runs-table` if you
want the run log in a sidecar dataset. Re-running with the same
`--chunk-by` is safe; switching `--chunk-by year` → `month` between
runs will re-process (chunks must match exactly to skip).

## Universe management

    # Initialize dim_symbols (first run)
    yfinance-bigquery universe init \
        --dim-symbols myproject.mydataset.dim_symbols \
        --create-if-missing

    # Refresh constituents (tracks additions and marks removals with date_removed)
    yfinance-bigquery universe refresh \
        --dim-symbols myproject.mydataset.dim_symbols

    # List all active tickers
    yfinance-bigquery universe list \
        --dim-symbols myproject.mydataset.dim_symbols

## Documentation

    yfinance-bigquery docs --format llm > LLM_CONTEXT.md

Five formats are supported: `bq-apply` (push column descriptions to BigQuery),
`llm` (a single Markdown file suitable for stuffing into an LLM context window),
`dictionary` (JSON rows for a data dictionary table), `markdown` (human-readable
column reference), and `dbt` (a dbt YAML schema stub).

## Verification

Internal-consistency checks run entirely inside BigQuery — no external data
source required. All 5 metrics use zero-tolerance: any violation fraction > 0
is a FAIL.

    # Check all metrics across all intervals for 2026
    yfinance-bigquery verify \
        --source internal \
        --interval all \
        --aggregation symbol-season \
        --metric all \
        --season 2026 \
        --table-prefix myproject.mydataset.ohlcv

The 5 metrics are:

- `ohlc_monotonic` — high >= open/close >= low for every bar
- `volume_non_negative` — volume is NULL or >= 0
- `no_future_bars` — no bar has a trading_date after today
- `trading_day_alignment` — no weekend bars (1d) or out-of-hours bars (intraday)
- `no_duplicate_bars` — no two bars share the same (symbol, bar_start_utc)

## Seed your data dictionary

If you maintain a `data_dictionary` table (one row per column with business
definitions, tags, and lineage), you can seed it directly:

    yfinance-bigquery docs --format dictionary --apply \
        --dataset mydataset \
        --table myproject.mydataset.ohlcv_1d \
        --dictionary-table myproject.shared_ops.data_dictionary

Atomically replaces rows for `(dataset, table)` only; other entries in the
dictionary table are untouched. Required target schema:

    dataset, table, column, dtype, description, business_definition,
    owner, tags ARRAY<STRING>, source_system, upstream_lineage_json,
    created_at TIMESTAMP, updated_at TIMESTAMP

MIT licensed. This software does not include or distribute Yahoo Finance data.
