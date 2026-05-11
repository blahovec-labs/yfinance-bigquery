"""Unit tests for RunsTable — the _yfinance_ingest_runs metadata table."""

from __future__ import annotations

import logging
from datetime import date
from unittest.mock import MagicMock

import pandas as pd
from google.cloud.exceptions import NotFound

from yfinance_bigquery.intervals import Interval
from yfinance_bigquery.runs import RunsTable, RunsTableRef

# ---------------------------------------------------------------------------
# RunsTableRef
# ---------------------------------------------------------------------------


def test_runs_table_ref_parse_roundtrip():
    ref = RunsTableRef.parse("proj.ds._yfinance_ingest_runs")
    assert ref.project == "proj"
    assert ref.dataset == "ds"
    assert ref.table == "_yfinance_ingest_runs"
    assert str(ref) == "proj.ds._yfinance_ingest_runs"


def test_runs_table_ref_rejects_bad_format():
    import pytest

    with pytest.raises(ValueError):
        RunsTableRef.parse("only.two")


# ---------------------------------------------------------------------------
# create_table_if_missing
# ---------------------------------------------------------------------------


def test_create_table_if_missing_calls_create_with_interval_schema():
    """assert the schema has 'interval' as the first field."""
    client = MagicMock()
    client.get_table.side_effect = NotFound("not found")
    rt = RunsTable(client=client)
    ref = RunsTableRef.parse("p.d._yfinance_ingest_runs")
    rt.create_table_if_missing(ref)

    assert client.create_table.call_count == 1
    table_arg = client.create_table.call_args[0][0]
    field_names = [f.name for f in table_arg.schema]

    # 'interval' must be first
    assert field_names[0] == "interval"
    # All 9 documented fields present
    for expected in (
        "interval", "chunk_start", "chunk_end", "chunk_kind",
        "rows_written", "status", "started_at", "finished_at", "library_version",
    ):
        assert expected in field_names


def test_create_table_if_missing_skips_when_present():
    client = MagicMock()
    # get_table does NOT raise NotFound → table exists
    rt = RunsTable(client=client)
    ref = RunsTableRef.parse("p.d._yfinance_ingest_runs")
    rt.create_table_if_missing(ref)
    assert client.create_table.call_count == 0


# ---------------------------------------------------------------------------
# record_success
# ---------------------------------------------------------------------------


def test_record_success_inserts_row_with_interval():
    client = MagicMock()
    rt = RunsTable(client=client)
    ref = RunsTableRef.parse("p.d._yfinance_ingest_runs")

    rt.record_success(
        ref=ref,
        interval=Interval.D1,
        chunk_start=date(2026, 5, 1),
        chunk_end=date(2026, 5, 31),
        chunk_kind="month",
        rows_written=12345,
    )

    assert client.query_and_wait.call_count == 1
    sql = client.query_and_wait.call_args[0][0]
    assert f"INSERT INTO `{ref}`" in sql

    params = {
        p.name: p.value
        for p in client.query_and_wait.call_args[1]["job_config"].query_parameters
    }
    assert params["interval"] == "1d"
    assert params["chunk_start"] == date(2026, 5, 1)
    assert params["chunk_end"] == date(2026, 5, 31)
    assert params["chunk_kind"] == "month"
    assert params["rows_written"] == 12345
    assert params["status"] == "success"


# ---------------------------------------------------------------------------
# record_empty
# ---------------------------------------------------------------------------


def test_record_empty_uses_status_empty_and_interval():
    client = MagicMock()
    rt = RunsTable(client=client)
    ref = RunsTableRef.parse("p.d._yfinance_ingest_runs")

    rt.record_empty(
        ref=ref,
        interval=Interval.M5,
        chunk_start=date(2024, 1, 1),
        chunk_end=date(2024, 1, 31),
        chunk_kind="month",
    )

    params = {
        p.name: p.value
        for p in client.query_and_wait.call_args[1]["job_config"].query_parameters
    }
    assert params["status"] == "empty"
    assert params["rows_written"] == 0
    assert params["interval"] == "5m"


# ---------------------------------------------------------------------------
# record_failed
# ---------------------------------------------------------------------------


def test_record_failed_uses_status_failed_and_logs_error(caplog):
    client = MagicMock()
    rt = RunsTable(client=client)
    ref = RunsTableRef.parse("p.d._yfinance_ingest_runs")

    with caplog.at_level(logging.WARNING, logger="yfinance_bigquery.runs"):
        rt.record_failed(
            ref=ref,
            interval=Interval.M1,
            chunk_start=date(2024, 1, 1),
            chunk_end=date(2024, 1, 7),
            chunk_kind="week",
            error="rate limit exceeded",
        )

    params = {
        p.name: p.value
        for p in client.query_and_wait.call_args[1]["job_config"].query_parameters
    }
    assert params["status"] == "failed"
    assert params["interval"] == "1m"
    assert "rate limit exceeded" in caplog.text


# ---------------------------------------------------------------------------
# completed_chunks
# ---------------------------------------------------------------------------


def test_completed_chunks_returns_set_of_3_tuples():
    client = MagicMock()
    fake_df = pd.DataFrame([
        {"interval": "1d", "chunk_start": date(2024, 1, 1), "chunk_end": date(2024, 12, 31)},
        {"interval": "5m", "chunk_start": date(2024, 1, 1), "chunk_end": date(2024, 1, 31)},
    ])
    qjob = MagicMock()
    qjob.to_dataframe.return_value = fake_df
    client.query_and_wait.return_value = qjob

    rt = RunsTable(client=client)
    ref = RunsTableRef.parse("p.d._yfinance_ingest_runs")
    chunks = rt.completed_chunks(ref=ref)

    assert chunks == {
        ("1d", date(2024, 1, 1), date(2024, 12, 31)),
        ("5m", date(2024, 1, 1), date(2024, 1, 31)),
    }
    sql = client.query_and_wait.call_args[0][0]
    assert "status IN ('success', 'empty')" in sql


def test_completed_chunks_returns_empty_set_when_table_missing():
    client = MagicMock()
    client.query_and_wait.side_effect = NotFound("table not found")

    rt = RunsTable(client=client)
    ref = RunsTableRef.parse("p.d._yfinance_ingest_runs")
    chunks = rt.completed_chunks(ref=ref)
    assert chunks == set()


# ---------------------------------------------------------------------------
# completed_chunks_for_interval
# ---------------------------------------------------------------------------


def test_completed_chunks_for_interval_filters():
    client = MagicMock()
    fake_df = pd.DataFrame([
        {"interval": "1d", "chunk_start": date(2024, 1, 1), "chunk_end": date(2024, 12, 31)},
        {"interval": "5m", "chunk_start": date(2024, 1, 1), "chunk_end": date(2024, 1, 31)},
        {"interval": "1d", "chunk_start": date(2023, 1, 1), "chunk_end": date(2023, 12, 31)},
    ])
    qjob = MagicMock()
    qjob.to_dataframe.return_value = fake_df
    client.query_and_wait.return_value = qjob

    rt = RunsTable(client=client)
    ref = RunsTableRef.parse("p.d._yfinance_ingest_runs")
    chunks_1d = rt.completed_chunks_for_interval(ref=ref, interval=Interval.D1)

    assert chunks_1d == {
        (date(2024, 1, 1), date(2024, 12, 31)),
        (date(2023, 1, 1), date(2023, 12, 31)),
    }

    chunks_5m = rt.completed_chunks_for_interval(ref=ref, interval=Interval.M5)
    assert chunks_5m == {(date(2024, 1, 1), date(2024, 1, 31))}


# ---------------------------------------------------------------------------
# _record exception swallowing
# ---------------------------------------------------------------------------


def test_record_failure_does_not_raise(caplog):
    """If recording itself fails (transient BQ error), log but do not raise."""
    client = MagicMock()
    client.query_and_wait.side_effect = RuntimeError("BQ flapping")
    rt = RunsTable(client=client)
    ref = RunsTableRef.parse("p.d._yfinance_ingest_runs")

    with caplog.at_level(logging.ERROR, logger="yfinance_bigquery.runs"):
        # Must not raise
        rt.record_success(
            ref=ref,
            interval=Interval.D1,
            chunk_start=date(2024, 1, 1),
            chunk_end=date(2024, 12, 31),
            chunk_kind="year",
            rows_written=100,
        )
    assert "failed to record run" in caplog.text.lower()
