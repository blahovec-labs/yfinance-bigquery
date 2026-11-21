# Changelog

## [0.2.2] - 2026-11-21

### Added
- A shrink guard on every replace. Atomic writes stop a failed load from wiping data, but not a load that succeeds with a short frame (an upstream outage that returns half the slice, or nothing). Before swapping, the writer now counts the rows already in the slice and raises `ShrinkGuardError`, deleting nothing, if the new frame is under 50% of them. Slices under 100 rows are exempt. Set `BIGQUERY_ALLOW_SHRINK=1` for a run where the source really did shrink. Every replace also logs `existing -> incoming` row counts.

## [0.2.1] - 2026-11-21

### Fixed
- OHLCV writes are now atomic. `OHLCVWriter.write` ran `DELETE ... WHERE trading_date
  BETWEEN @start AND @end AND symbol IN UNNEST(@symbols)` as its own query and then a
  separate load job. A load job can't join a transaction, so a failed load left those
  symbols' bars for the window deleted until the next good run (the failure that wiped
  2026-06-02 from statcast-bigquery). It now loads into a short-lived `_staging_*` table
  (6-hour expiry backstop), then `DELETE` + `INSERT ... SELECT` in one multi-statement
  transaction, then drops the staging table. A failed load deletes nothing; a failed swap
  rolls back. Idempotency, the symbol scoping and the empty-frame no-op are unchanged.
  The membership `replace` (a single `WRITE_TRUNCATE` load) and `dim_symbols` `MERGE`
  were already atomic.

## [0.2.0] - 2026-06-05

Bulletproof corporate-action layer + survivorship-free universe + cross-timeframe
adjusted views.

### Added
- **Corporate-action capture.** `sync` now passes `actions=True`, so per-bar
  `stock_splits` and `dividends` are populated (1d) instead of NULL. The data
  layer is no longer blind to splits/dividends.
- **Deterministic adjustment (`adjust.py`) + `ohlcv_1d_adjusted` view.** yfinance's
  `close` is always split-adjusted (and Yahoo re-derives it run-to-run), so the
  view exposes drift-free, reproducible columns derived only from the captured
  events:
  - `cum_split_factor` + `close_raw = close / cum_split_factor` — de-split back to
    the actual historical traded price (an immutable anchor).
  - `cum_div_factor` + `adj_close_tr = close * cum_div_factor` — split- AND
    dividend-adjusted total return (matches Yahoo's `adj_close` to the penny, but
    reproducible).
- **Intraday adjusted views** `ohlcv_{60m,15m,5m,1m}_adjusted` — intraday has no
  native corporate-action events, so they borrow the daily factors (joined on
  symbol + trading_date) for the same `close_raw`/`adj_close_tr` as 1d.
- **`yfinance-bigquery views create`** — idempotently create/replace all 5
  adjusted views.
- **Point-in-time S&P 500 membership** (`universe reconstruct` → `sp500_membership`)
  — survivorship-bias-free membership spells reconstructed from Wikipedia's
  current list + dated changes; recovers since-removed constituents as closed
  spells.
- **`yfinance-bigquery verify-membership`** — `membership_completeness` (no
  inverted/overlapping spells) + `no_survivorship` (table includes closed spells).
- **`corporate_action_continuity`** verify metric — recorded splits stay
  continuous in the adjusted `close` (gated on `stock_splits>0` so legitimate
  crashes are not false-flagged).

### Changed
- Schema docs corrected: the OHLC price columns are **split-adjusted** as returned
  by yfinance (not "raw/unadjusted"); see `close_raw` for the de-split price.
- Intraday `dividends`/`stock_splits` back-fill as `0.0` (not NULL), matching the
  documented "0.0 = no event" contract.

## [0.1.0] - 2026-05-11

### Added
- Initial release.
- `yfinance-bigquery sync` — idempotent OHLCV ingestion for 5 intervals
  (1d, 60m, 15m, 5m, 1m) to 5 per-interval BigQuery tables, mirroring
  the design of statcast-bigquery (per-chunk DELETE-then-INSERT,
  `_yfinance_ingest_runs` metadata for `--resume`, configurable
  rate-limiting for yfinance, post-sync retention trim per interval).
- `yfinance-bigquery universe {init,refresh,list}` — S&P 500 constituent
  management via Wikipedia scrape into a BQ `dim_symbols` table with
  auto `date_removed` tracking for departed constituents.
- `yfinance-bigquery verify --source internal` — zero-tolerance
  internal-consistency checks across all 5 intervals (OHLC monotonicity,
  volume sanity, no future bars, weekday/market-hour alignment, no
  duplicate bars).
- `yfinance-bigquery docs --format {bq-apply, llm, dictionary, markdown, dbt}`
  — 5 documentation renderers, same shape as statcast-bigquery.
- Schema: 14 columns per OHLCV bar (canonical OHLCV + adj_close +
  dividends + splits + tz-normalized timestamps in UTC and ET +
  interval discriminator + ingest_at).
- Auto-applied BQ-native column descriptions at table create time.
- 5-interval ladder with per-interval partitioning + retention
  (1d never trimmed; 60m 730d; 15m 60d; 5m 60d; 1m 7d).

### Deferred to a future release
- Cross-source verification against Stooq (or equivalent). Stooq's CSV
  endpoint now requires an API key obtained via captcha registration,
  and `pandas-datareader` is broken on Python 3.13 (uses removed
  `distutils`). To be revisited when a clean replacement is identified.
