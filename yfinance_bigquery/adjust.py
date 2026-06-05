"""Deterministic corporate-action adjustment.

yfinance re-derives ``adj_close`` on every split/dividend event, so the same
bar's adjusted value drifts run-to-run — useless for a reproducible backtest.
``compute_split_adjustment`` instead derives a STABLE split-adjustment factor
purely from the raw ``close`` and the per-bar ``stock_splits`` ratio, so the same
raw inputs always yield the same adjusted series.

Convention: the most recent bar is unadjusted (factor 1.0); historical bars are
scaled down by the product of all STRICTLY-FUTURE split ratios, which removes
the spurious price jump on each split date while preserving real returns.

Dividend total-return adjustment (a smaller, separate effect) is a documented
follow-up — splits are the discontinuity this fixes.
"""

from __future__ import annotations

import pandas as pd


def compute_split_adjustment(bars: pd.DataFrame) -> pd.DataFrame:
    """Add ``cum_split_factor`` and ``adj_close`` to daily bars.

    Args:
        bars: DataFrame with ``symbol``, ``trading_date``, ``close``, and
            ``stock_splits`` (split ratio on that date; 0/NaN = no split, 4.0 =
            4:1 forward split, 0.5 = 1:2 reverse split). The split ratio is
            reported ON the effective date, where ``close`` is already the
            post-split price.

    Returns:
        A new DataFrame (sorted by symbol, trading_date) with two added columns:
          - ``cum_split_factor``: product of ``1/ratio`` over all splits on dates
            STRICTLY AFTER this bar (so the latest bar = 1.0; a bar before a 4:1
            split = 0.25). The bar's own split is excluded — it is already baked
            into that bar's raw ``close``.
          - ``adj_close``: ``close * cum_split_factor`` — a split-continuous
            price series suitable for return calculations.
    """
    out = bars.sort_values(["symbol", "trading_date"]).reset_index(drop=True).copy()

    # Per-bar multiplier: 1/ratio on split dates, 1.0 otherwise.
    has_split = out["stock_splits"].fillna(0.0) > 0
    ratio = out["stock_splits"].where(has_split, other=1.0).astype(float)
    per_bar = 1.0 / ratio

    out["cum_split_factor"] = per_bar.groupby(out["symbol"]).transform(
        _future_product
    )
    out["adj_close"] = out["close"] * out["cum_split_factor"]
    return out


def _future_product(multipliers: pd.Series) -> pd.Series:
    """Product of STRICTLY-FUTURE multipliers within one symbol's bar sequence.

    Reverse-cumprod gives the product including the current bar; shifting by one
    excludes the current bar's own split (already in its raw price).
    """
    including_current = multipliers[::-1].cumprod()[::-1]
    return including_current.shift(-1).fillna(1.0)
