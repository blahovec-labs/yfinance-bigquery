"""Tests for WikipediaUniverseClient."""

from __future__ import annotations

import datetime
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from yfinance_bigquery.universe.client import WikipediaUniverseClient

_FAKE_WIKI_TABLE = pd.DataFrame({
    "Symbol": ["AAPL", "MSFT", "GOOGL"],
    "Security": ["Apple Inc.", "Microsoft Corp.", "Alphabet Inc. Class A"],
    "GICS Sector": ["Information Technology", "Information Technology", "Communication Services"],
    "GICS Sub-Industry": ["Technology Hardware", "Systems Software", "Interactive Media"],
    "Date added": ["1982-11-30", "1994-06-01", "April 3, 2014"],
})


def _mock_requests_get():
    """Mock requests.get to return a fake Response with raise_for_status + .text."""
    fake_resp = MagicMock()
    fake_resp.text = "<html><body>fake wiki page</body></html>"
    fake_resp.raise_for_status = MagicMock()
    return patch("yfinance_bigquery.universe.client.requests.get",
                 return_value=fake_resp)


def test_fetch_constituents_returns_dataframe():
    with _mock_requests_get(), patch(
        "yfinance_bigquery.universe.client.pd.read_html",
        return_value=[_FAKE_WIKI_TABLE, pd.DataFrame()],
    ):
        result = WikipediaUniverseClient().fetch_constituents()
    assert len(result) == 3
    assert list(result.columns) == [
        "symbol", "name", "sector", "industry", "date_added"
    ]
    assert result.iloc[0]["symbol"] == "AAPL"
    assert result.iloc[0]["sector"] == "Information Technology"


def test_fetch_constituents_parses_dates_tolerantly():
    """Both ISO and 'April 3, 2014' should parse."""
    with _mock_requests_get(), patch(
        "yfinance_bigquery.universe.client.pd.read_html",
        return_value=[_FAKE_WIKI_TABLE, pd.DataFrame()],
    ):
        result = WikipediaUniverseClient().fetch_constituents()
    aapl_date = result.loc[result["symbol"] == "AAPL", "date_added"].iloc[0]
    googl_date = result.loc[result["symbol"] == "GOOGL", "date_added"].iloc[0]
    assert aapl_date == datetime.date(1982, 11, 30)
    assert googl_date == datetime.date(2014, 4, 3)


def test_fetch_constituents_unparseable_date_becomes_null():
    bad_table = _FAKE_WIKI_TABLE.copy()
    bad_table.loc[0, "Date added"] = "unknown"
    with _mock_requests_get(), patch(
        "yfinance_bigquery.universe.client.pd.read_html",
        return_value=[bad_table, pd.DataFrame()],
    ):
        result = WikipediaUniverseClient().fetch_constituents()
    assert pd.isna(result.loc[result["symbol"] == "AAPL", "date_added"].iloc[0])


def test_fetch_constituents_raises_on_missing_columns():
    bad_table = pd.DataFrame({"Foo": [1], "Bar": [2]})
    with _mock_requests_get(), patch(
        "yfinance_bigquery.universe.client.pd.read_html",
        return_value=[bad_table],
    ):
        with pytest.raises(ValueError, match="expected column"):
            WikipediaUniverseClient().fetch_constituents()


def test_fetch_constituents_sends_user_agent_header():
    """Wikipedia requires a descriptive User-Agent. Verify we send one."""
    with _mock_requests_get() as mock_get, patch(
        "yfinance_bigquery.universe.client.pd.read_html",
        return_value=[_FAKE_WIKI_TABLE],
    ):
        WikipediaUniverseClient().fetch_constituents()
    called_headers = mock_get.call_args.kwargs.get("headers", {})
    assert "User-Agent" in called_headers
    assert "yfinance-bigquery" in called_headers["User-Agent"]
