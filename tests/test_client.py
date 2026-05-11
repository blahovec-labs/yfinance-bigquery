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
