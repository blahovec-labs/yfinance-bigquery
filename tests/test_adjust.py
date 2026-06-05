"""Tests for the deterministic split factor + raw-price recovery.

Empirical finding (2026-06-04): yfinance's ``close`` (auto_adjust=False) is
ALREADY split-adjusted — a 4:1 split shows a continuous price series, not a raw
one. So the job is NOT to re-split ``close`` (that would double-adjust); it is to
recover the immutable RAW (de-split) price as a drift-free anchor, and to expose
the split factor. The split-adjusted return series is ``close`` itself.
"""

from __future__ import annotations

from datetime import date

import pandas as pd

from yfinance_bigquery.adjust import compute_split_factor


def _bars(rows: list[tuple[str, date, float, float]]) -> pd.DataFrame:
    return pd.DataFrame(rows, columns=["symbol", "trading_date", "close", "stock_splits"])


def test_close_raw_recovers_actual_pre_split_price():
    """A 4:1 split. yfinance ``close`` is already split-adjusted (continuous at
    ~100), so close_raw must recover the ACTUAL pre-split traded price (~400)."""
    bars = _bars([
        ("AAPL", date(2020, 8, 28), 100.0, 0.0),   # yfinance-adjusted (raw was 400)
        ("AAPL", date(2020, 8, 31), 100.0, 4.0),   # 4:1 split effective (raw 100)
        ("AAPL", date(2020, 9, 1), 105.0, 0.0),    # day after
    ])
    out = compute_split_factor(bars).set_index("trading_date")

    # The pre-split bar is divided by its 0.25 factor -> recovers the real $400.
    assert out.loc[date(2020, 8, 28), "close_raw"] == 400.0
    assert out.loc[date(2020, 8, 31), "close_raw"] == 100.0
    assert out.loc[date(2020, 9, 1), "close_raw"] == 105.0
    # close itself is the split-CONTINUOUS return series: ~0% across the split.
    adj = out["close"]
    ret_across = adj.loc[date(2020, 8, 31)] / adj.loc[date(2020, 8, 28)] - 1.0
    assert abs(ret_across) < 1e-9


def test_no_split_is_identity():
    bars = _bars([
        ("MSFT", date(2024, 1, 2), 370.0, 0.0),
        ("MSFT", date(2024, 1, 3), 372.0, 0.0),
    ])
    out = compute_split_factor(bars)
    assert (out["cum_split_factor"] == 1.0).all()
    assert (out["close_raw"] == out["close"]).all()


def test_factor_is_deterministic_across_runs():
    """Stability: the factor + raw price depend only on close + stock_splits,
    never on run time — two runs must be identical."""
    bars = _bars([
        ("AAPL", date(2020, 8, 28), 100.0, 0.0),
        ("AAPL", date(2020, 8, 31), 100.0, 4.0),
    ])
    a = compute_split_factor(bars)["close_raw"].tolist()
    b = compute_split_factor(bars)["close_raw"].tolist()
    assert a == b


def test_current_split_bar_factor_is_one():
    """The split bar's OWN close is already post-split; its factor is 1. The
    bar BEFORE a 4:1 split carries 0.25 (one strictly-future split)."""
    bars = _bars([
        ("AAPL", date(2020, 8, 28), 100.0, 0.0),
        ("AAPL", date(2020, 8, 31), 100.0, 4.0),
    ])
    out = compute_split_factor(bars).set_index("trading_date")
    assert out.loc[date(2020, 8, 31), "cum_split_factor"] == 1.0
    assert out.loc[date(2020, 8, 28), "cum_split_factor"] == 0.25


def test_reverse_split_recovers_lower_raw_price():
    """A 1-for-2 reverse split (yfinance ratio 0.5) doubles the price. yfinance
    adjusts the pre-split close UP to ~100; close_raw must recover the real ~50."""
    bars = _bars([
        ("XYZ", date(2023, 1, 2), 100.0, 0.0),   # yfinance-adjusted (raw was 50)
        ("XYZ", date(2023, 1, 3), 100.0, 0.5),   # 1-for-2 reverse effective
    ])
    out = compute_split_factor(bars).set_index("trading_date")
    # 1/0.5 = 2.0 future multiplier -> pre-split factor 2.0 -> raw = 100/2 = 50.
    assert out.loc[date(2023, 1, 2), "cum_split_factor"] == 2.0
    assert out.loc[date(2023, 1, 2), "close_raw"] == 50.0


# ---------------------------------------------------------------------------
# BQ-native plumbing (build_adjustment_factor_sql / view / writer)
# ---------------------------------------------------------------------------


def test_build_adjustment_factor_sql_uses_future_window_product():
    from yfinance_bigquery.adjust import build_adjustment_factor_sql

    sql = build_adjustment_factor_sql(source_table="p.d.ohlcv_1d")
    # Product of STRICTLY-FUTURE split multipliers (current bar excluded).
    assert "ROWS BETWEEN 1 FOLLOWING AND UNBOUNDED FOLLOWING" in sql
    # BigQuery has no PRODUCT aggregate -> EXP(SUM(LN(...))).
    assert "EXP(SUM(LN(" in sql
    assert "1.0 / stock_splits" in sql
    assert "p.d.ohlcv_1d" in sql
    # De-split is DIVISION (close / factor), not multiplication.
    assert "SAFE_DIVIDE(close, cum_split_factor)" in sql
    assert "close_raw" in sql and "cum_split_factor" in sql


def test_build_adjusted_view_ddl_wraps_select():
    from yfinance_bigquery.adjust import build_adjusted_view_ddl

    ddl = build_adjusted_view_ddl(
        source_table="p.d.ohlcv_1d", view="p.d.ohlcv_1d_adjusted"
    )
    assert ddl.startswith("CREATE OR REPLACE VIEW `p.d.ohlcv_1d_adjusted` AS")
    assert "EXP(SUM(LN(" in ddl


def test_create_adjusted_view_runs_ddl():
    from unittest.mock import MagicMock

    from yfinance_bigquery.adjust import create_adjusted_view

    client = MagicMock()
    create_adjusted_view(
        client=client, source_table="p.d.ohlcv_1d", view="p.d.ohlcv_1d_adjusted"
    )
    assert client.query_and_wait.called
    ran_sql = client.query_and_wait.call_args[0][0]
    assert "CREATE OR REPLACE VIEW" in ran_sql
