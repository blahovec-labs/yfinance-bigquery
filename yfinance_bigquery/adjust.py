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
of Yahoo's run-to-run re-derivation. Dividend total-return adjustment is a
separate, documented follow-up.
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


def _future_product(multipliers: pd.Series) -> pd.Series:
    """Product of STRICTLY-FUTURE multipliers within one symbol's bar sequence.

    Reverse-cumprod gives the product including the current bar; shifting by one
    excludes the current bar's own split (already baked into Yahoo's price).
    """
    including_current = multipliers[::-1].cumprod()[::-1]
    return including_current.shift(-1).fillna(1.0)


def build_adjustment_factor_sql(*, source_table: str) -> str:
    """SELECT producing the split factor + de-split raw price from a yfinance OHLCV table.

    ``cum_split_factor`` = product of ``1/ratio`` over all STRICTLY-FUTURE split
    events (``trading_date`` > the bar), computed as ``EXP(SUM(LN(m)))`` over a
    FOLLOWING window because BigQuery has no PRODUCT aggregate. ``close_raw`` =
    ``close / cum_split_factor`` undoes Yahoo's split adjustment to recover the
    actual traded price (mirrors ``compute_split_factor``). ``close`` is passed
    through unchanged — it is already the split-adjusted return series. Both
    outputs are deterministic from ``close`` + ``stock_splits`` only, independent
    of Yahoo's drifting ``adj_close``. Dividend total-return is a follow-up.
    """
    return (
        "WITH mult AS (\n"
        "  SELECT\n"
        "    symbol, trading_date, open, high, low, close, volume,\n"
        "    IF(stock_splits > 0, 1.0 / stock_splits, 1.0) AS _m\n"
        f"  FROM `{source_table}`\n"
        "),\n"
        "factored AS (\n"
        "  SELECT\n"
        "    symbol, trading_date, open, high, low, close, volume,\n"
        "    COALESCE(EXP(SUM(LN(_m)) OVER w), 1.0) AS cum_split_factor\n"
        "  FROM mult\n"
        "  WINDOW w AS (\n"
        "    PARTITION BY symbol ORDER BY trading_date\n"
        "    ROWS BETWEEN 1 FOLLOWING AND UNBOUNDED FOLLOWING\n"
        "  )\n"
        ")\n"
        "SELECT\n"
        "  symbol, trading_date, open, high, low, close, volume,\n"
        "  cum_split_factor,\n"
        "  SAFE_DIVIDE(close, cum_split_factor) AS close_raw\n"
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
