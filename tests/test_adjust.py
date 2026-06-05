"""Tests for the deterministic split-adjustment factor."""

from __future__ import annotations

from datetime import date

import pandas as pd

from yfinance_bigquery.adjust import compute_split_adjustment


def _bars(rows: list[tuple[str, date, float, float]]) -> pd.DataFrame:
    return pd.DataFrame(rows, columns=["symbol", "trading_date", "close", "stock_splits"])


def test_split_makes_adjusted_return_continuous():
    """A 4:1 split: raw close drops 400 -> 100 (spurious -75%). The adjusted
    series must be CONTINUOUS across the split (adjusted return ~ 0)."""
    bars = _bars([
        ("AAPL", date(2020, 8, 28), 400.0, 0.0),   # day before split
        ("AAPL", date(2020, 8, 31), 100.0, 4.0),   # 4:1 split effective
        ("AAPL", date(2020, 9, 1), 105.0, 0.0),    # day after
    ])
    out = compute_split_adjustment(bars).set_index("trading_date")

    # pre-split bar is scaled down by 1/4 so it lines up with post-split level
    assert out.loc[date(2020, 8, 28), "adj_close"] == 100.0
    assert out.loc[date(2020, 8, 31), "adj_close"] == 100.0
    # adjusted return across the split is ~0 (the spurious -75% is gone)
    adj = out["adj_close"]
    ret_across = adj.loc[date(2020, 8, 31)] / adj.loc[date(2020, 8, 28)] - 1.0
    assert abs(ret_across) < 1e-9
    # post-split real move survives: +5%
    ret_after = adj.loc[date(2020, 9, 1)] / adj.loc[date(2020, 8, 31)] - 1.0
    assert abs(ret_after - 0.05) < 1e-9


def test_no_split_is_identity():
    bars = _bars([
        ("MSFT", date(2024, 1, 2), 370.0, 0.0),
        ("MSFT", date(2024, 1, 3), 372.0, 0.0),
    ])
    out = compute_split_adjustment(bars)
    assert (out["cum_split_factor"] == 1.0).all()
    assert (out["adj_close"] == out["close"]).all()


def test_factor_is_deterministic_across_runs():
    """Stability: the factor depends only on raw close + stock_splits, never on
    yfinance's re-adjusted adj_close or run time — two runs must be identical."""
    bars = _bars([
        ("AAPL", date(2020, 8, 28), 400.0, 0.0),
        ("AAPL", date(2020, 8, 31), 100.0, 4.0),
    ])
    a = compute_split_adjustment(bars)["adj_close"].tolist()
    b = compute_split_adjustment(bars)["adj_close"].tolist()
    assert a == b


def test_current_split_bar_not_double_counted():
    """The split bar's OWN ratio is already in its raw price; its factor is 1."""
    bars = _bars([
        ("AAPL", date(2020, 8, 28), 400.0, 0.0),
        ("AAPL", date(2020, 8, 31), 100.0, 4.0),
    ])
    out = compute_split_adjustment(bars).set_index("trading_date")
    assert out.loc[date(2020, 8, 31), "cum_split_factor"] == 1.0
    assert out.loc[date(2020, 8, 28), "cum_split_factor"] == 0.25
