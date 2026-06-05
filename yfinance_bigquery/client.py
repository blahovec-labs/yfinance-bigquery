"""YFinanceClient: rate-limited wrapper around yfinance.download with normalization."""

from __future__ import annotations

import logging
import random
import time
from typing import Final

import pandas as pd
import yfinance

from yfinance_bigquery.intervals import Interval

log = logging.getLogger(__name__)

DEFAULT_BATCH_SIZE: Final[int] = 50
DEFAULT_SLEEP_SECONDS: Final[float] = 3.0
DEFAULT_MAX_RETRIES: Final[int] = 3
ET_TZ: Final[str] = "America/New_York"


class YFinanceClient:
    """Rate-limited wrapper around ``yfinance.download()`` with batching and retry.

    Parameters
    ----------
    batch_size:
        Number of tickers per ``yfinance.download()`` call.  Default 50.
    sleep_seconds:
        Base sleep between batches (plus random jitter up to ``sleep_seconds``).
        Set to ``0.0`` in tests to skip sleeping.
    max_retries:
        How many times to retry a batch on ``requests.HTTPError`` (429/500/503)
        before giving up on that batch.  Backoff is 1s, 2s, 4s, … (2^attempt seconds).
    """

    def __init__(
        self,
        batch_size: int = DEFAULT_BATCH_SIZE,
        sleep_seconds: float = DEFAULT_SLEEP_SECONDS,
        max_retries: int = DEFAULT_MAX_RETRIES,
    ) -> None:
        self.batch_size = batch_size
        self.sleep_seconds = sleep_seconds
        self.max_retries = max_retries

    def fetch(
        self,
        tickers: list[str],
        interval: Interval,
        start: str,
        end: str,
    ) -> pd.DataFrame:
        """Fetch OHLCV bars for *tickers* × *interval* × ``[start, end)``.

        Returns a long-form DataFrame with one row per (symbol, bar_start).
        Returns an empty DataFrame if every batch comes back empty or fails.
        """
        all_chunks: list[pd.DataFrame] = []
        for i in range(0, len(tickers), self.batch_size):
            batch = tickers[i : i + self.batch_size]
            raw = self._fetch_batch_with_retry(batch, interval, start, end)
            if raw is None or raw.empty:
                continue
            long_df = self._reshape_to_long(raw, interval)
            if not long_df.empty:
                all_chunks.append(long_df)
            # Inter-batch sleep with jitter (skip for the last batch)
            if i + self.batch_size < len(tickers) and self.sleep_seconds > 0:
                jitter = random.uniform(0, self.sleep_seconds)
                time.sleep(self.sleep_seconds + jitter)
        if not all_chunks:
            return pd.DataFrame()
        return pd.concat(all_chunks, ignore_index=True)

    def _fetch_batch_with_retry(
        self,
        batch: list[str],
        interval: Interval,
        start: str,
        end: str,
    ) -> pd.DataFrame | None:
        """Call ``yfinance.download()`` with exponential-backoff retry.

        Returns the raw DataFrame on success, or ``None`` if all retries are
        exhausted.  Per-symbol empty results are NOT retried (they are a normal
        outcome when yfinance simply has no data for a symbol/range).
        """
        attempt = 0
        last_err: Exception | None = None
        while attempt < self.max_retries:
            attempt += 1
            try:
                df: pd.DataFrame = yfinance.download(  # type: ignore[assignment]
                    tickers=batch,
                    interval=interval.value,
                    start=start,
                    end=end,
                    auto_adjust=False,
                    actions=True,  # return Dividends + Stock Splits columns (1d)
                    group_by="ticker",
                    threads=True,
                    progress=False,
                )
                return df
            except Exception as exc:
                last_err = exc
                backoff = 2 ** (attempt - 1)  # 1s, 2s, 4s, …
                log.warning(
                    "yfinance batch attempt %d/%d failed: %s; backoff %.1fs",
                    attempt,
                    self.max_retries,
                    exc,
                    backoff,
                )
                time.sleep(backoff)
        log.error(
            "yfinance batch failed after %d attempts: %s — skipping batch",
            attempt,
            last_err,
        )
        return None

    def _reshape_to_long(self, df: pd.DataFrame, interval: Interval) -> pd.DataFrame:
        """Reshape a MultiIndex (ticker × OHLCV-field) DataFrame to long form.

        yfinance returns a MultiIndex when ``group_by="ticker"`` and multiple
        tickers are requested.  We stack the ticker level, rename columns,
        normalise timezones, and emit a DataFrame that matches ``OHLCV_SCHEMA``
        column order.

        For single-ticker calls yfinance may return flat columns — in that case
        we wrap them into a single-entry MultiIndex so the same code path works.
        """
        # ------------------------------------------------------------------ #
        # 1. Normalise to MultiIndex (ticker × price-field)                   #
        # ------------------------------------------------------------------ #
        if not isinstance(df.columns, pd.MultiIndex):
            # yfinance returned flat columns → this happens when a single
            # ticker is passed.  We cannot infer the ticker from the DataFrame
            # alone, so we raise a clear error.  Callers should always pass ≥2
            # tickers, or handle single-ticker reshaping upstream.
            raise ValueError(
                "YFinanceClient._reshape_to_long expects a MultiIndex-column "
                "DataFrame (returned by yfinance when group_by='ticker' and "
                "≥2 tickers are requested).  Got flat columns: "
                f"{list(df.columns)[:8]}"
            )

        # ------------------------------------------------------------------ #
        # 2. Stack ticker level → long form                                   #
        # ------------------------------------------------------------------ #
        # df.columns has levels [ticker, price-field].  stack(level=0) pivots
        # the ticker level into rows; reset_index() promotes the time index
        # (named "Date" or "Datetime") and Ticker to regular columns.
        #
        # Edge case: when some tickers in the batch error out, yfinance can
        # return a DataFrame with `index.name = None`.  After reset_index(),
        # the time column is then named "level_0" instead of the expected
        # "Date"/"Datetime".  We force a known index name before stacking so
        # the rename below always finds the time column.
        if df.index.name is None:
            df = df.copy()
            df.index.name = "bar_start"
        long: pd.DataFrame = df.stack(level=0, future_stack=True).reset_index()

        # ------------------------------------------------------------------ #
        # 3. Normalise column names to lowercase + snake_case                 #
        # ------------------------------------------------------------------ #
        # After stack the columns are e.g. ['Date', 'Ticker', 'Open', 'High',
        # 'Low', 'Close', 'Adj Close', 'Volume'].  We lower-case and replace
        # spaces with underscores in one pass.
        long.columns = pd.Index(
            [str(c).lower().replace(" ", "_") for c in long.columns]
        )
        # 'adj close' → 'adj_close' (handled above); 'date'/'datetime' and
        # 'ticker' need explicit renames.  'bar_start' is already correct.
        long = long.rename(
            columns={
                "date": "bar_start",
                "datetime": "bar_start",
                "ticker": "symbol",
            }
        )

        # ------------------------------------------------------------------ #
        # 4. Timezone normalisation                                           #
        # ------------------------------------------------------------------ #
        # 1d: yfinance returns tz-naive timestamps (session-aligned, treated
        # as America/New_York midnight).  Localize to ET first, then UTC.
        # Intraday: yfinance already returns tz-aware ET timestamps; just
        # convert to UTC.
        if long["bar_start"].dt.tz is None:
            bar_et = long["bar_start"].dt.tz_localize(
                ET_TZ, nonexistent="shift_forward", ambiguous="NaT"
            )
        else:
            bar_et = long["bar_start"].dt.tz_convert(ET_TZ)

        long["bar_start_et"] = bar_et
        long["bar_start_utc"] = bar_et.dt.tz_convert("UTC")
        long["trading_date"] = bar_et.dt.date

        # ------------------------------------------------------------------ #
        # 5. Add pipeline metadata                                            #
        # ------------------------------------------------------------------ #
        long["interval"] = interval.value
        long["_ingested_at"] = pd.Timestamp.now(tz="UTC")

        # ------------------------------------------------------------------ #
        # 6. Ensure all 14 OHLCV_SCHEMA columns exist                        #
        # ------------------------------------------------------------------ #
        # With actions=True, the 1d download returns Dividends + Stock Splits
        # (and Adj Close); they flow through the stack above. Intraday intervals
        # omit them, so any column still absent here is back-filled with None.
        for col in ("adj_close", "dividends", "stock_splits"):
            if col not in long.columns:
                long[col] = None

        # ------------------------------------------------------------------ #
        # 7. Drop the staging column and reorder to match OHLCV_SCHEMA        #
        # ------------------------------------------------------------------ #
        long = long.drop(columns=["bar_start"])

        schema_order = [
            "symbol",
            "bar_start_utc",
            "bar_start_et",
            "trading_date",
            "open",
            "high",
            "low",
            "close",
            "adj_close",
            "volume",
            "dividends",
            "stock_splits",
            "interval",
            "_ingested_at",
        ]
        return long[schema_order]  # type: ignore[return-value]
