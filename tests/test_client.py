"""Tests for YFinanceClient."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pandas as pd

from yfinance_bigquery.client import YFinanceClient
from yfinance_bigquery.intervals import Interval

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "yfinance_aapl_1d.parquet"


def test_fetch_reshapes_multiindex_to_long_form():
    """yfinance returns MultiIndex columns when group_by='ticker'. We reshape to long form."""
    raw_df = pd.read_parquet(FIXTURE_PATH)
    with patch("yfinance_bigquery.client.yfinance.download", return_value=raw_df):
        client = YFinanceClient(sleep_seconds=0.0)
        long_df = client.fetch(
            tickers=["AAPL", "MSFT", "GOOGL"],
            interval=Interval.D1,
            start="2024-01-02",
            end="2024-01-10",
        )
    # One row per (symbol, bar_start), all 14 schema columns present
    assert "symbol" in long_df.columns
    assert "bar_start_utc" in long_df.columns
    assert "bar_start_et" in long_df.columns
    assert "trading_date" in long_df.columns
    assert "open" in long_df.columns
    assert "close" in long_df.columns
    assert "interval" in long_df.columns
    # All 3 symbols present
    assert set(long_df["symbol"].unique()) == {"AAPL", "MSFT", "GOOGL"}
    # interval column always equals "1d" for this fetch
    assert (long_df["interval"] == "1d").all()


def test_fetch_normalizes_timezones():
    """For 1d, yfinance returns naive timestamps; we localize to UTC and ET."""
    raw_df = pd.read_parquet(FIXTURE_PATH)
    with patch("yfinance_bigquery.client.yfinance.download", return_value=raw_df):
        long_df = YFinanceClient(sleep_seconds=0.0).fetch(
            ["AAPL"], Interval.D1, "2024-01-02", "2024-01-10",
        )
    # bar_start_utc and bar_start_et should both be tz-aware
    assert long_df["bar_start_utc"].dt.tz is not None
    assert long_df["bar_start_et"].dt.tz is not None


def test_fetch_batches_tickers():
    """500 symbols with batch_size=50 → 10 yfinance.download calls."""
    raw_df = pd.read_parquet(FIXTURE_PATH)
    with patch("yfinance_bigquery.client.yfinance.download", return_value=raw_df) as mock_dl:
        client = YFinanceClient(sleep_seconds=0.0, batch_size=50)
        client.fetch(
            tickers=[f"T{i:03d}" for i in range(500)],
            interval=Interval.D1,
            start="2024-01-02",
            end="2024-01-10",
        )
    assert mock_dl.call_count == 10


def test_fetch_retries_on_429():
    """Retry up to 3 times on HTTPError 429 before giving up on a batch."""
    import requests

    raw_df = pd.read_parquet(FIXTURE_PATH)
    err = requests.HTTPError(response=type("R", (), {"status_code": 429})())  # type: ignore[arg-type]
    with patch(
        "yfinance_bigquery.client.yfinance.download",
        side_effect=[err, err, raw_df],
    ) as mock_dl:
        client = YFinanceClient(sleep_seconds=0.0, max_retries=3)
        long_df = client.fetch(["AAPL"], Interval.D1, "2024-01-02", "2024-01-10")
    assert mock_dl.call_count == 3
    assert not long_df.empty


def test_fetch_empty_batch_returns_empty_df():
    """yfinance returning empty for all symbols in a batch yields an empty long_df."""
    empty = pd.DataFrame()
    with patch("yfinance_bigquery.client.yfinance.download", return_value=empty):
        long_df = YFinanceClient(sleep_seconds=0.0).fetch(
            ["BADTICKER"], Interval.D1, "2024-01-02", "2024-01-10",
        )
    assert long_df.empty


# ---------------------------------------------------------------------------
# Corporate-action capture (actions=True)
# ---------------------------------------------------------------------------
# yfinance.download() only returns the Dividends + Stock Splits columns when
# actions=True is passed. Without it, those columns come back absent and land
# in BigQuery as all-NULL — leaving the data layer blind to splits/dividends
# (the gap that broke the A2 corporate-action work). These tests pin the fix.


def _ohlcv_with_actions() -> pd.DataFrame:
    """Synthetic group_by='ticker' MultiIndex frame WITH Dividends + Stock Splits.

    Mirrors what ``yfinance.download(..., actions=True, group_by='ticker')``
    returns for a 1d request: columns are a [ticker, field] MultiIndex and the
    field set includes 'Dividends' and 'Stock Splits'. AAPL gets a 4:1 split on
    2024-01-03 and a $0.24 dividend on 2024-01-04; MSFT has neither.
    """
    idx = pd.DatetimeIndex(
        pd.to_datetime(["2024-01-02", "2024-01-03", "2024-01-04"]), name="Date"
    )
    fields = ["Open", "High", "Low", "Close", "Adj Close", "Volume",
              "Dividends", "Stock Splits"]
    cols = pd.MultiIndex.from_product(
        [["AAPL", "MSFT"], fields], names=["Ticker", "Price"]
    )
    data = {
        ("AAPL", "Open"): [185.0, 184.0, 46.0],
        ("AAPL", "High"): [186.0, 185.0, 47.0],
        ("AAPL", "Low"): [183.0, 183.0, 45.0],
        ("AAPL", "Close"): [184.0, 46.0, 46.5],
        ("AAPL", "Adj Close"): [183.9, 45.9, 46.4],
        ("AAPL", "Volume"): [50_000_000, 60_000_000, 55_000_000],
        ("AAPL", "Dividends"): [0.0, 0.0, 0.24],
        ("AAPL", "Stock Splits"): [0.0, 4.0, 0.0],
        ("MSFT", "Open"): [370.0, 371.0, 372.0],
        ("MSFT", "High"): [372.0, 373.0, 374.0],
        ("MSFT", "Low"): [369.0, 370.0, 371.0],
        ("MSFT", "Close"): [371.0, 372.0, 373.0],
        ("MSFT", "Adj Close"): [370.9, 371.9, 372.9],
        ("MSFT", "Volume"): [20_000_000, 21_000_000, 22_000_000],
        ("MSFT", "Dividends"): [0.0, 0.0, 0.0],
        ("MSFT", "Stock Splits"): [0.0, 0.0, 0.0],
    }
    return pd.DataFrame(data, index=idx, columns=cols)


def test_fetch_requests_corporate_actions():
    """fetch() must call yfinance.download with actions=True, or splits and
    dividends never come back from Yahoo (they land as all-NULL in BQ)."""
    with patch(
        "yfinance_bigquery.client.yfinance.download",
        return_value=_ohlcv_with_actions(),
    ) as mock_dl:
        YFinanceClient(sleep_seconds=0.0).fetch(
            ["AAPL", "MSFT"], Interval.D1, "2024-01-02", "2024-01-05",
        )
    assert mock_dl.call_count == 1
    assert mock_dl.call_args.kwargs.get("actions") is True


def test_fetch_carries_split_and_dividend_events():
    """When yfinance returns Stock Splits / Dividends, they survive the reshape
    onto the right (symbol, trading_date) rows — not dropped to NULL."""
    with patch(
        "yfinance_bigquery.client.yfinance.download",
        return_value=_ohlcv_with_actions(),
    ):
        long_df = YFinanceClient(sleep_seconds=0.0).fetch(
            ["AAPL", "MSFT"], Interval.D1, "2024-01-02", "2024-01-05",
        )

    def cell(symbol: str, day: str, col: str) -> float:
        row = long_df[
            (long_df["symbol"] == symbol)
            & (long_df["trading_date"] == pd.to_datetime(day).date())
        ]
        assert len(row) == 1, f"expected exactly one {symbol} {day} row"
        return float(row.iloc[0][col])

    # AAPL's 4:1 split on 2024-01-03 and $0.24 dividend on 2024-01-04 survive.
    assert cell("AAPL", "2024-01-03", "stock_splits") == 4.0
    assert cell("AAPL", "2024-01-04", "dividends") == 0.24
    # Non-event cells are 0.0, not NULL.
    assert cell("AAPL", "2024-01-02", "stock_splits") == 0.0
    assert cell("MSFT", "2024-01-03", "stock_splits") == 0.0
