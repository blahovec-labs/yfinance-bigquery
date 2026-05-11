# Changelog

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
