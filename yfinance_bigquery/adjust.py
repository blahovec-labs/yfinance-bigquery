"""Deterministic split factor + raw-price recovery (de-splitting).

Empirical finding (2026-06-04): yfinance's ``close`` (even with
``auto_adjust=False``) is ALREADY split-adjusted — Yahoo always divides historical
prices by every later split ratio. So a 4:1 split shows a CONTINUOUS price series,
not a raw one. Two consequences:

1. ``close`` is the split-adjusted price-return series — use it directly for
   returns. Do NOT re-apply a split factor to it (that would double-adjust).
2. Yahoo RE-derives that adjustment on every new split, so the same historical
   bar drifts run-to-run — useless for a reproducible backtest.

The fix is to recover the immutable RAW (de-split) price: the actual price that
traded on the day, which never changes. From the captured ``stock_splits`` events
we compute, per bar:

  - ``cum_split_factor`` = product of ``1/ratio`` over all splits STRICTLY AFTER
    this bar (latest bar = 1.0; a bar before a single 4:1 split = 0.25). This is
    exactly the factor Yahoo applied to that bar.
  - ``close_raw`` = ``close / cum_split_factor`` — undoes Yahoo's adjustment to
    recover the actual traded price (the 4:1 example: 100 / 0.25 = 400).

``close_raw`` is drift-free (it is the historical fact), so any adjustment can be
reconstructed deterministically from ``close_raw`` + the split events, independent
of Yahoo's run-to-run re-derivation.

``compute_total_return_factor`` adds the dividend leg the same way: a strictly-future
product of ``(1 - dividend/prev_close)`` over ex-dates yields ``adj_close_tr``, the
split- AND dividend-adjusted total-return series — a reproducible replacement for
yfinance's drifting ``adj_close``, built from the captured ``dividends`` events.
"""

from __future__ import annotations

import pandas as pd


def compute_split_factor(bars: pd.DataFrame) -> pd.DataFrame:
    """Add ``cum_split_factor`` and ``close_raw`` to yfinance daily bars.

    Args:
        bars: DataFrame with ``symbol``, ``trading_date``, ``close``, and
            ``stock_splits`` (split ratio on that date; 0/NaN = no split, 4.0 =
            4:1 forward split, 0.5 = 1:2 reverse split). ``close`` is yfinance's
            already-split-adjusted close.

    Returns:
        A new DataFrame (sorted by symbol, trading_date) with two added columns:
          - ``cum_split_factor``: product of ``1/ratio`` over all splits on dates
            STRICTLY AFTER this bar (latest bar = 1.0; a bar before a 4:1 split =
            0.25). The bar's own split is excluded — Yahoo's ``close`` for the
            split date is already the post-split price.
          - ``close_raw``: ``close / cum_split_factor`` — the actual (un-adjusted)
            price that traded on that day; an immutable, drift-free anchor.
    """
    out = bars.sort_values(["symbol", "trading_date"]).reset_index(drop=True).copy()

    # Per-bar multiplier: 1/ratio on split dates, 1.0 otherwise.
    has_split = out["stock_splits"].fillna(0.0) > 0
    ratio = out["stock_splits"].where(has_split, other=1.0).astype(float)
    per_bar = 1.0 / ratio

    out["cum_split_factor"] = per_bar.groupby(out["symbol"]).transform(
        _future_product
    )
    out["close_raw"] = out["close"] / out["cum_split_factor"]
    return out


def compute_total_return_factor(bars: pd.DataFrame) -> pd.DataFrame:
    """Add ``cum_div_factor`` and ``adj_close_tr`` to (split-adjusted) daily bars.

    A reproducible total-return series — the drift-free replacement for yfinance's
    ``adj_close``, derived deterministically from the captured ``dividends`` events.

    Args:
        bars: DataFrame with ``symbol``, ``trading_date``, ``close`` (yfinance's
            split-adjusted close) and ``dividends`` (per-share cash dividend on the
            ex-date, 0 otherwise; on the same split-adjusted basis as ``close``).

    Returns:
        A new DataFrame (sorted by symbol, trading_date) with:
          - ``cum_div_factor``: product of ``(1 - dividend/prev_close)`` over all
            ex-dates STRICTLY AFTER this bar (latest bar = 1.0; the ex-date's own
            dividend is excluded — that bar already trades ex). ``prev_close`` is
            the close on the trading day before the ex-date (standard convention).
          - ``adj_close_tr``: ``close * cum_div_factor`` — split- AND
            dividend-adjusted (total return), anchored so the latest bar equals the
            actual close.
    """
    out = bars.sort_values(["symbol", "trading_date"]).reset_index(drop=True).copy()

    prev_close = out.groupby("symbol")["close"].shift(1)
    div = out["dividends"].fillna(0.0)
    # Per-ex-date reinvestment multiplier: (1 - div/prev_close) on ex-dates, else 1.
    # Require div < prev_close (keeps the multiplier in (0, 1)); a dividend >= the
    # whole share price is pathological and is left unadjusted, mirroring the SQL.
    per_bar = pd.Series(1.0, index=out.index, dtype=float)
    mask = (div > 0) & (prev_close > 0) & (div < prev_close)
    per_bar.loc[mask] = 1.0 - div.loc[mask] / prev_close.loc[mask]

    out["cum_div_factor"] = per_bar.groupby(out["symbol"]).transform(_future_product)
    out["adj_close_tr"] = out["close"] * out["cum_div_factor"]
    return out


def _future_product(multipliers: pd.Series) -> pd.Series:
    """Product of STRICTLY-FUTURE multipliers within one symbol's bar sequence.

    Reverse-cumprod gives the product including the current bar; shifting by one
    excludes the current bar's own event (its split/dividend is already reflected
    in that bar's price — Yahoo pre-splits the close, and an ex-date trades ex).
    """
    including_current = multipliers[::-1].cumprod()[::-1]
    return including_current.shift(-1).fillna(1.0)


def build_adjustment_factor_sql(*, source_table: str) -> str:
    """SELECT producing split + dividend adjustments from a yfinance OHLCV table.

    All factors are products over STRICTLY-FUTURE events (``trading_date`` > the
    bar), computed as ``EXP(SUM(LN(m)))`` over a FOLLOWING window because BigQuery
    has no PRODUCT aggregate. Outputs (deterministic from ``close`` /
    ``stock_splits`` / ``dividends`` only — independent of Yahoo's drifting
    ``adj_close``):

      - ``cum_split_factor`` = ∏ ``1/ratio`` over future splits; ``close_raw`` =
        ``close / cum_split_factor`` undoes Yahoo's split adjustment to recover the
        actual traded price (mirrors ``compute_split_factor``).
      - ``cum_div_factor`` = ∏ ``(1 - dividend/prev_close)`` over future ex-dates
        (``prev_close`` via LAG = the close before the ex-date); ``adj_close_tr`` =
        ``close * cum_div_factor`` is the split- AND dividend-adjusted total-return
        series (mirrors ``compute_total_return_factor``).

    ``close`` is passed through unchanged — it is already the split-adjusted
    price-return series.
    """
    return (
        "WITH base AS (\n"
        "  SELECT\n"
        "    symbol, trading_date, open, high, low, close, volume, dividends,\n"
        "    IF(stock_splits > 0, 1.0 / stock_splits, 1.0) AS _sm,\n"
        "    LAG(close) OVER (PARTITION BY symbol ORDER BY trading_date) AS _prev_close\n"
        f"  FROM `{source_table}`\n"
        "),\n"
        "mult AS (\n"
        "  SELECT\n"
        "    symbol, trading_date, open, high, low, close, volume, _sm,\n"
        # Require dividends < prev_close so _dm stays in (0, 1): a dividend >= the
        # whole share price (a pathological special/liquidating distribution) can't
        # be sensibly reinvestment-adjusted, and would make LN(_dm) hit a <=0 input.
        "    IF(dividends > 0 AND _prev_close > 0 AND dividends < _prev_close,\n"
        "       1.0 - dividends / _prev_close, 1.0) AS _dm\n"
        "  FROM base\n"
        "),\n"
        "factored AS (\n"
        "  SELECT\n"
        "    symbol, trading_date, open, high, low, close, volume,\n"
        "    COALESCE(EXP(SUM(LN(_sm)) OVER w), 1.0) AS cum_split_factor,\n"
        "    COALESCE(EXP(SUM(LN(_dm)) OVER w), 1.0) AS cum_div_factor\n"
        "  FROM mult\n"
        "  WINDOW w AS (\n"
        "    PARTITION BY symbol ORDER BY trading_date\n"
        "    ROWS BETWEEN 1 FOLLOWING AND UNBOUNDED FOLLOWING\n"
        "  )\n"
        ")\n"
        "SELECT\n"
        "  symbol, trading_date, open, high, low, close, volume,\n"
        "  cum_split_factor,\n"
        "  SAFE_DIVIDE(close, cum_split_factor) AS close_raw,\n"
        "  cum_div_factor,\n"
        "  close * cum_div_factor AS adj_close_tr\n"
        "FROM factored"
    )


def build_adjusted_view_ddl(*, source_table: str, view: str) -> str:
    """CREATE OR REPLACE VIEW DDL for the split-factor / de-split view."""
    return (
        f"CREATE OR REPLACE VIEW `{view}` AS\n"
        + build_adjustment_factor_sql(source_table=source_table)
    )


def create_adjusted_view(*, client, source_table: str, view: str) -> None:
    """Create/replace the split-factor / de-split VIEW in BigQuery.

    ``client`` is a ``google.cloud.bigquery.Client`` (duck-typed: needs
    ``query_and_wait``).
    """
    client.query_and_wait(
        build_adjusted_view_ddl(source_table=source_table, view=view)
    )


# ---------------------------------------------------------------------------
# Intraday adjustment: borrow the DAILY factors
# ---------------------------------------------------------------------------
# yfinance does not report dividends/splits at sub-daily resolution, but it DOES
# split-adjust the intraday close exactly like the daily series. A split/dividend
# factor is constant within a trading day, so an intraday bar can borrow its day's
# factor from the daily adjusted view (joined on symbol + trading_date) to get the
# same close_raw + adj_close_tr as 1d — true cross-timeframe parity.


def build_intraday_adjusted_sql(
    *, intraday_table: str, daily_adjusted_view: str
) -> str:
    """SELECT applying the DAILY split/dividend factors to intraday bars.

    Joins each intraday bar to its trading day's ``cum_split_factor`` /
    ``cum_div_factor`` (from ``daily_adjusted_view``) and emits the same
    ``close_raw`` (de-split) and ``adj_close_tr`` (total return) columns as the 1d
    view. LEFT JOIN + COALESCE(..., 1.0) so a bar without a matching daily factor
    is passed through unadjusted rather than dropped.
    """
    return (
        "SELECT\n"
        "  i.symbol, i.bar_start_utc, i.bar_start_et, i.trading_date,\n"
        "  i.open, i.high, i.low, i.close, i.volume, i.interval,\n"
        "  COALESCE(d.cum_split_factor, 1.0) AS cum_split_factor,\n"
        "  SAFE_DIVIDE(i.close, COALESCE(d.cum_split_factor, 1.0)) AS close_raw,\n"
        "  COALESCE(d.cum_div_factor, 1.0) AS cum_div_factor,\n"
        "  i.close * COALESCE(d.cum_div_factor, 1.0) AS adj_close_tr\n"
        f"FROM `{intraday_table}` i\n"
        "LEFT JOIN (\n"
        "  SELECT symbol, trading_date, cum_split_factor, cum_div_factor\n"
        f"  FROM `{daily_adjusted_view}`\n"
        ") d USING (symbol, trading_date)"
    )


def build_intraday_adjusted_view_ddl(
    *, intraday_table: str, daily_adjusted_view: str, view: str
) -> str:
    """CREATE OR REPLACE VIEW DDL for an intraday adjusted view."""
    return (
        f"CREATE OR REPLACE VIEW `{view}` AS\n"
        + build_intraday_adjusted_sql(
            intraday_table=intraday_table, daily_adjusted_view=daily_adjusted_view
        )
    )


def create_intraday_adjusted_view(
    *, client, intraday_table: str, daily_adjusted_view: str, view: str
) -> None:
    """Create/replace an intraday adjusted VIEW in BigQuery (borrows daily factors)."""
    client.query_and_wait(
        build_intraday_adjusted_view_ddl(
            intraday_table=intraday_table,
            daily_adjusted_view=daily_adjusted_view,
            view=view,
        )
    )
