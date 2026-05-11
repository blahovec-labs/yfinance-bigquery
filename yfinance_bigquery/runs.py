"""RunsTable: records (interval, chunk_start, chunk_end) sync runs for --resume support."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date

from google.cloud import bigquery
from google.cloud.exceptions import NotFound

from yfinance_bigquery._version import __version__
from yfinance_bigquery.intervals import Interval

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class RunsTableRef:
    project: str
    dataset: str
    table: str

    @classmethod
    def parse(cls, fq: str) -> RunsTableRef:
        parts = fq.split(".")
        if len(parts) != 3:
            raise ValueError(f"expected project.dataset.table, got {fq!r}")
        return cls(*parts)

    def __str__(self) -> str:
        return f"{self.project}.{self.dataset}.{self.table}"


_SCHEMA: list[bigquery.SchemaField] = [
    bigquery.SchemaField("interval", "STRING", mode="REQUIRED"),
    bigquery.SchemaField("chunk_start", "DATE", mode="REQUIRED"),
    bigquery.SchemaField("chunk_end", "DATE", mode="REQUIRED"),
    bigquery.SchemaField("chunk_kind", "STRING", mode="REQUIRED"),
    bigquery.SchemaField("rows_written", "INT64"),
    bigquery.SchemaField("status", "STRING", mode="REQUIRED"),
    bigquery.SchemaField("started_at", "TIMESTAMP", mode="REQUIRED"),
    bigquery.SchemaField("finished_at", "TIMESTAMP"),
    bigquery.SchemaField("library_version", "STRING"),
]


class RunsTable:
    def __init__(self, client: bigquery.Client) -> None:
        self.client = client

    def create_table_if_missing(self, ref: RunsTableRef) -> None:
        try:
            self.client.get_table(str(ref))
            return
        except NotFound:
            pass
        table = bigquery.Table(str(ref), schema=_SCHEMA)
        table.description = (
            "yfinance-bigquery sync run log — used by --resume to skip already-"
            "completed chunks. Underscore-prefixed so BQ console hides by default."
        )
        self.client.create_table(table)
        log.info("created runs table %s", ref)

    def completed_chunks(self, *, ref: RunsTableRef) -> set[tuple[str, date, date]]:
        """Return the set of (interval_value, chunk_start, chunk_end) triples already
        recorded as success or empty. If the table doesn't exist yet, returns empty set."""
        try:
            sql = (
                f"SELECT interval, chunk_start, chunk_end FROM `{ref}` "
                "WHERE status IN ('success', 'empty')"
            )
            rows = self.client.query_and_wait(sql).to_dataframe()
        except NotFound:
            return set()
        return {
            (r["interval"], r["chunk_start"], r["chunk_end"])
            for _, r in rows.iterrows()
        }

    def completed_chunks_for_interval(
        self, *, ref: RunsTableRef, interval: Interval
    ) -> set[tuple[date, date]]:
        """Return (chunk_start, chunk_end) pairs completed for a specific interval."""
        all_chunks = self.completed_chunks(ref=ref)
        interval_value = interval.value
        return {(cs, ce) for iv, cs, ce in all_chunks if iv == interval_value}

    def record_success(
        self,
        *,
        ref: RunsTableRef,
        interval: Interval,
        chunk_start: date,
        chunk_end: date,
        chunk_kind: str,
        rows_written: int,
    ) -> None:
        self._record(
            ref, interval, chunk_start, chunk_end, chunk_kind,
            status="success", rows_written=rows_written,
        )

    def record_empty(
        self,
        *,
        ref: RunsTableRef,
        interval: Interval,
        chunk_start: date,
        chunk_end: date,
        chunk_kind: str,
    ) -> None:
        self._record(
            ref, interval, chunk_start, chunk_end, chunk_kind,
            status="empty", rows_written=0,
        )

    def record_failed(
        self,
        *,
        ref: RunsTableRef,
        interval: Interval,
        chunk_start: date,
        chunk_end: date,
        chunk_kind: str,
        error: str,
    ) -> None:
        log.warning(
            "chunk %s..%s (interval=%s) failed: %s",
            chunk_start, chunk_end, interval.value, error,
        )
        self._record(
            ref, interval, chunk_start, chunk_end, chunk_kind,
            status="failed", rows_written=0,
        )

    def _record(
        self,
        ref: RunsTableRef,
        interval: Interval,
        chunk_start: date,
        chunk_end: date,
        chunk_kind: str,
        *,
        status: str,
        rows_written: int,
    ) -> None:
        sql = (
            f"INSERT INTO `{ref}` "
            "(interval, chunk_start, chunk_end, chunk_kind, rows_written, status, "
            "started_at, finished_at, library_version) "
            "VALUES (@interval, @chunk_start, @chunk_end, @chunk_kind, @rows_written, "
            "@status, CURRENT_TIMESTAMP(), CURRENT_TIMESTAMP(), @library_version)"
        )
        job_config = bigquery.QueryJobConfig(
            query_parameters=[
                bigquery.ScalarQueryParameter("interval", "STRING", interval.value),
                bigquery.ScalarQueryParameter("chunk_start", "DATE", chunk_start),
                bigquery.ScalarQueryParameter("chunk_end", "DATE", chunk_end),
                bigquery.ScalarQueryParameter("chunk_kind", "STRING", chunk_kind),
                bigquery.ScalarQueryParameter("rows_written", "INT64", rows_written),
                bigquery.ScalarQueryParameter("status", "STRING", status),
                bigquery.ScalarQueryParameter(
                    "library_version", "STRING", __version__
                ),
            ]
        )
        try:
            self.client.query_and_wait(sql, job_config=job_config)
        except Exception:
            log.exception(
                "failed to record run (status=%s) for interval=%s %s..%s; sync continues",
                status, interval.value, chunk_start, chunk_end,
            )
