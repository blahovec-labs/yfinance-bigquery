"""Tests for DimSymbolsWriter."""

from __future__ import annotations

from datetime import date
from unittest.mock import MagicMock

import pandas as pd

from yfinance_bigquery.universe.writer import (
    DimSymbolsTableRef,
    DimSymbolsWriter,
)


def test_table_ref_parse():
    ref = DimSymbolsTableRef.parse("p.d.dim_symbols")
    assert (ref.project, ref.dataset, ref.table) == ("p", "d", "dim_symbols")


def test_merge_runs_upsert_and_remove_unseen():
    """merge() should upsert seen symbols and mark date_removed=today() for unseen actives."""
    client = MagicMock()
    writer = DimSymbolsWriter(client=client)
    ref = DimSymbolsTableRef.parse("p.d.dim_symbols")
    new_constituents = pd.DataFrame({
        "symbol": ["AAPL", "NEWCO"],
        "name": ["Apple Inc.", "Newco Holdings"],
        "sector": ["IT", "Industrials"],
        "industry": ["Hardware", "Conglomerates"],
        "date_added": [date(1982, 11, 30), date(2026, 5, 1)],
    })
    writer.merge(ref=ref, constituents=new_constituents)
    # Expect at least 2 query_and_wait calls: MERGE upsert + UPDATE for removals
    assert client.query_and_wait.call_count >= 2


def test_membership_writer_replace_truncate_loads():
    """replace() should full-load (WRITE_TRUNCATE) the membership rows."""
    from yfinance_bigquery.universe.writer import MembershipWriter

    client = MagicMock()
    writer = MembershipWriter(client=client)
    ref = DimSymbolsTableRef.parse("p.d.sp500_membership")
    m = pd.DataFrame({
        "symbol": ["AAPL", "LUMN"],
        "date_added": [date(2020, 1, 1), None],
        "date_removed": [None, date(2023, 6, 20)],
        "source": ["wikipedia", "wikipedia"],
    })
    n = writer.replace(ref=ref, membership=m)
    assert n == 2
    assert client.load_table_from_dataframe.called
    _, kwargs = client.load_table_from_dataframe.call_args
    assert kwargs["job_config"].write_disposition == "WRITE_TRUNCATE"
