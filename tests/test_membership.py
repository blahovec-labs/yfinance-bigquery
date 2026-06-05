"""Tests for point-in-time S&P 500 membership reconstruction + query."""

from __future__ import annotations

from datetime import date

import pandas as pd

from yfinance_bigquery.universe.membership import (
    members_as_of_sql,
    reconstruct_membership,
)


def test_reconstruct_includes_removed_symbol_as_closed_spell():
    """A symbol removed in the changes log (and not a current member) must be
    recovered as a CLOSED spell — this is the survivorship-bias fix."""
    current = pd.DataFrame(
        {"symbol": ["AAPL", "FICO"], "date_added": [date(1982, 11, 30), date(2023, 6, 20)]}
    )
    changes = pd.DataFrame(
        {
            "date": [date(2023, 6, 20)],
            "added_ticker": ["FICO"],
            "removed_ticker": ["LUMN"],
        }
    )
    m = reconstruct_membership(current=current, changes=changes).set_index("symbol")
    # current members stay open
    assert m.loc["AAPL", "date_removed"] is None or pd.isna(m.loc["AAPL", "date_removed"])
    # the removed symbol is recovered with a close date
    assert m.loc["LUMN", "date_removed"] == date(2023, 6, 20)
    assert m.loc["LUMN", "source"] == "wikipedia"


def test_reconstruct_does_not_drop_current_members():
    current = pd.DataFrame({"symbol": ["AAPL"], "date_added": [date(1982, 11, 30)]})
    changes = pd.DataFrame({"date": [], "added_ticker": [], "removed_ticker": []})
    m = reconstruct_membership(current=current, changes=changes)
    assert set(m["symbol"]) == {"AAPL"}


def test_reconstruct_recovers_added_date_for_removed_symbol():
    """If a removed symbol was also ADDED within the changes window, its
    date_added should be recovered from that prior addition."""
    current = pd.DataFrame({"symbol": ["AAPL"], "date_added": [date(1982, 11, 30)]})
    changes = pd.DataFrame(
        {
            "date": [date(2020, 1, 1), date(2022, 1, 1)],
            "added_ticker": ["TMPCO", None],
            "removed_ticker": [None, "TMPCO"],
        }
    )
    m = reconstruct_membership(current=current, changes=changes).set_index("symbol")
    assert m.loc["TMPCO", "date_added"] == date(2020, 1, 1)
    assert m.loc["TMPCO", "date_removed"] == date(2022, 1, 1)


def test_members_as_of_sql_has_pit_predicate():
    sql = members_as_of_sql(table="p.d.sp500_membership")
    assert "date_added <= @as_of" in sql
    assert "date_removed IS NULL OR date_removed > @as_of" in sql
    assert "p.d.sp500_membership" in sql
