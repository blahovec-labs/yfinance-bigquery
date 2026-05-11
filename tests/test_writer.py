"""Tests for OHLCVWriter and _iter_chunks — uses bigquery.Client mocks."""

from __future__ import annotations

from datetime import date
from unittest.mock import MagicMock

import pandas as pd
import pytest
from google.cloud import bigquery
from google.cloud.exceptions import NotFound

from yfinance_bigquery.intervals import Interval
from yfinance_bigquery.writer import OHLCVTableRef, OHLCVWriter, _iter_chunks


def make_mock_client() -> MagicMock:
    client = MagicMock(spec=bigquery.Client)
    client.project = "test-project"
    return client


# ---------------------------------------------------------------------------
# OHLCVTableRef
# ---------------------------------------------------------------------------


def test_ohlcv_table_ref_parse_roundtrip():
    ref = OHLCVTableRef.parse("myproject.mydataset.ohlcv_1d")
    assert ref.project == "myproject"
    assert ref.dataset == "mydataset"
    assert ref.table == "ohlcv_1d"
    assert str(ref) == "myproject.mydataset.ohlcv_1d"


def test_ohlcv_table_ref_rejects_bad_format():
    with pytest.raises(ValueError):
        OHLCVTableRef.parse("only.two")


# ---------------------------------------------------------------------------
# OHLCVWriter.create_table_if_missing
# ---------------------------------------------------------------------------


def test_create_table_if_missing_calls_create_when_absent():
    client = make_mock_client()
    client.get_table.side_effect = NotFound("missing")
    writer = OHLCVWriter(client=client, interval=Interval.D1)

    writer.create_table_if_missing(OHLCVTableRef.parse("p.d.ohlcv_1d"))

    assert client.create_table.call_count == 1
    table_arg = client.create_table.call_args.args[0]
    # Check partitioning on trading_date
    assert table_arg.time_partitioning is not None
    assert table_arg.time_partitioning.field == "trading_date"
    # 1d uses DAY partitioning
    assert table_arg.time_partitioning.type_ == "DAY"
    # clustering on symbol
    assert table_arg.clustering_fields == ["symbol"]
    # schema includes 'symbol'
    field_names = [f.name for f in table_arg.schema]
    assert "symbol" in field_names
    assert "trading_date" in field_names
    assert "open" in field_names
    assert "volume" in field_names


def test_create_table_if_missing_uses_month_partitioning_for_60m():
    client = make_mock_client()
    client.get_table.side_effect = NotFound("missing")
    writer = OHLCVWriter(client=client, interval=Interval.M60)

    writer.create_table_if_missing(OHLCVTableRef.parse("p.d.ohlcv_60m"))

    table_arg = client.create_table.call_args.args[0]
    assert table_arg.time_partitioning.type_ == "MONTH"


def test_create_table_if_missing_skips_when_table_exists():
    client = make_mock_client()
    client.get_table.return_value = MagicMock()
    writer = OHLCVWriter(client=client, interval=Interval.D1)

    writer.create_table_if_missing(OHLCVTableRef.parse("p.d.ohlcv_1d"))

    assert client.create_table.call_count == 0


# ---------------------------------------------------------------------------
# OHLCVWriter.write
# ---------------------------------------------------------------------------


def _make_ohlcv_df(symbols: list[str]) -> pd.DataFrame:
    """Minimal valid OHLCV dataframe for writer tests."""
    rows = []
    for sym in symbols:
        rows.append({
            "symbol": sym,
            "trading_date": date(2024, 4, 1),
            "bar_start_utc": pd.Timestamp("2024-04-01 13:30:00", tz="UTC"),
            "bar_start_et": pd.Timestamp("2024-04-01 09:30:00", tz="America/New_York"),
            "open": 100.0,
            "high": 101.0,
            "low": 99.0,
            "close": 100.5,
            "adj_close": 100.5,
            "volume": 1_000_000,
            "dividends": 0.0,
            "stock_splits": 0.0,
            "interval": "1d",
            "_ingested_at": pd.Timestamp("2024-04-01 18:00:00", tz="UTC"),
        })
    return pd.DataFrame(rows)


def test_write_empty_df_returns_zero():
    client = make_mock_client()
    writer = OHLCVWriter(client=client, interval=Interval.D1)

    result = writer.write(
        OHLCVTableRef.parse("p.d.ohlcv_1d"), pd.DataFrame(), "2024-04-01", "2024-04-01"
    )

    assert result == 0
    assert client.query_and_wait.call_count == 0
    assert client.load_table_from_dataframe.call_count == 0


def test_write_runs_delete_then_load():
    client = make_mock_client()
    # load_table_from_dataframe returns a job with a .result() method
    load_job = MagicMock()
    client.load_table_from_dataframe.return_value = load_job

    writer = OHLCVWriter(client=client, interval=Interval.D1)
    df = _make_ohlcv_df(["AAPL"])
    ref = OHLCVTableRef.parse("p.d.ohlcv_1d")

    n = writer.write(ref, df, "2024-04-01", "2024-04-01")

    assert n == 1
    # DELETE via query_and_wait
    assert client.query_and_wait.call_count == 1
    delete_sql = client.query_and_wait.call_args.args[0]
    assert "DELETE" in delete_sql
    assert "BETWEEN" in delete_sql
    assert "UNNEST" in delete_sql
    # INSERT via load_table_from_dataframe
    assert client.load_table_from_dataframe.call_count == 1
    load_job.result.assert_called_once()

    # Verify order: DELETE (query_and_wait) must appear before INSERT (load_table_from_dataframe)
    method_names = [str(c) for c in client.method_calls]
    delete_idx = next(i for i, n in enumerate(method_names) if "query_and_wait" in n)
    load_idx = next(i for i, n in enumerate(method_names) if "load_table_from_dataframe" in n)
    assert delete_idx < load_idx


def test_write_delete_scoped_to_unique_symbols():
    client = make_mock_client()
    load_job = MagicMock()
    client.load_table_from_dataframe.return_value = load_job

    writer = OHLCVWriter(client=client, interval=Interval.D1)
    df = _make_ohlcv_df(["AAPL", "MSFT"])
    ref = OHLCVTableRef.parse("p.d.ohlcv_1d")

    writer.write(ref, df, "2024-04-01", "2024-04-01")

    # ArrayQueryParameter uses .values (plural), ScalarQueryParameter uses .value
    params_by_name = {
        p.name: p
        for p in client.query_and_wait.call_args.kwargs["job_config"].query_parameters
    }
    symbols_param = params_by_name["symbols"]
    assert set(symbols_param.values) == {"AAPL", "MSFT"}


def test_write_returns_row_count():
    client = make_mock_client()
    load_job = MagicMock()
    client.load_table_from_dataframe.return_value = load_job

    writer = OHLCVWriter(client=client, interval=Interval.D1)
    df = _make_ohlcv_df(["AAPL", "MSFT", "GOOG"])

    n = writer.write(OHLCVTableRef.parse("p.d.ohlcv_1d"), df, "2024-04-01", "2024-04-30")

    assert n == 3


# ---------------------------------------------------------------------------
# _iter_chunks
# ---------------------------------------------------------------------------


def test_iter_chunks_year():
    chunks = _iter_chunks("2022-06-15", "2024-03-20", "year")
    assert chunks == [
        ("2022-06-15", "2022-12-31"),
        ("2023-01-01", "2023-12-31"),
        ("2024-01-01", "2024-03-20"),
    ]


def test_iter_chunks_month():
    chunks = _iter_chunks("2024-01-15", "2024-03-10", "month")
    assert chunks == [
        ("2024-01-15", "2024-01-31"),
        ("2024-02-01", "2024-02-29"),  # 2024 is a leap year
        ("2024-03-01", "2024-03-10"),
    ]


def test_iter_chunks_week():
    # 2024-02-15 to 2024-03-05
    # Week 1: 2024-02-15 to 2024-02-21 (7 days)
    # Week 2: 2024-02-22 to 2024-02-28 (7 days)
    # Week 3: 2024-02-29 to 2024-03-05 (clipped)
    chunks = _iter_chunks("2024-02-15", "2024-03-05", "week")
    assert chunks == [
        ("2024-02-15", "2024-02-21"),
        ("2024-02-22", "2024-02-28"),
        ("2024-02-29", "2024-03-05"),
    ]


def test_iter_chunks_week_single_chunk():
    # start to end is less than 7 days → single clipped chunk
    chunks = _iter_chunks("2024-01-01", "2024-01-03", "week")
    assert chunks == [("2024-01-01", "2024-01-03")]


def test_iter_chunks_range():
    chunks = _iter_chunks("2024-01-01", "2024-12-31", "range")
    assert chunks == [("2024-01-01", "2024-12-31")]


def test_iter_chunks_single_day():
    for kind in ("year", "month", "week", "range"):
        chunks = _iter_chunks("2024-06-15", "2024-06-15", kind)
        assert len(chunks) == 1
        assert chunks[0] == ("2024-06-15", "2024-06-15")


def test_iter_chunks_unknown_kind():
    with pytest.raises(ValueError, match="unknown chunk kind"):
        _iter_chunks("2024-01-01", "2024-12-31", "decade")
