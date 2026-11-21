"""Atomic, shrink-guarded replace-a-slice for the BigQuery writers.

A BigQuery load job cannot run inside a multi-statement transaction, so a plain
"DELETE, then load" is two independent commits: if the load fails, the DELETE has
already happened and the slice is simply gone. That is how statcast-bigquery lost
every 2026-06-02 pitch (a 400 on a new Savant column, 2026-06-09).

So: load the new rows into a short-lived staging table first (the step that
actually fails), then DELETE + INSERT ... SELECT in ONE transaction, then drop the
staging table. A failed load never touches the target; a failed swap rolls back.

Atomicity doesn't help when the load *succeeds* with a short frame (an upstream
outage returning half a week, or nothing). Before swapping, count the slice and
refuse if the new frame is under half of it (slices under 100 rows are exempt).
A refusal deletes nothing. Set BIGQUERY_ALLOW_SHRINK=1 when the source really did
shrink.

This module is copied verbatim into each *-bigquery library; keep the copies identical.
"""

from __future__ import annotations

import datetime as _dt
import logging
import os
import uuid

import pandas as pd
from google.cloud import bigquery

log = logging.getLogger(__name__)

# Safety net: if the process dies between load and drop, BigQuery expires it.
_STAGING_TTL = _dt.timedelta(hours=6)

# Refuse a replace that would leave under this fraction of the slice's rows...
MIN_KEEP_RATIO = 0.5
# ...unless the slice is this small (normal corrections on tiny slices).
MIN_GUARDED_ROWS = 100
ALLOW_SHRINK_ENV = "BIGQUERY_ALLOW_SHRINK"


class ShrinkGuardError(RuntimeError):
    """A replace would have shrunk the slice too far; nothing was deleted."""

    def __init__(self, target: str, where_sql: str, existing: int, incoming: int) -> None:
        self.target = target
        self.where_sql = where_sql
        self.existing = existing
        self.incoming = incoming
        super().__init__(
            f"refusing to replace {existing:,} rows in {target} WHERE {where_sql} with "
            f"{incoming:,} (under {MIN_KEEP_RATIO:.0%}); existing rows kept. If the source "
            f"really shrank, rerun with {ALLOW_SHRINK_ENV}=1."
        )


def _shrink_allowed() -> bool:
    return os.environ.get(ALLOW_SHRINK_ENV, "").strip().lower() in ("1", "true", "yes")


def _count(
    client: bigquery.Client, target: str, where_sql: str, job_config: bigquery.QueryJobConfig
) -> int:
    rows = list(
        client.query_and_wait(
            f"SELECT COUNT(*) AS n FROM `{target}` WHERE {where_sql}", job_config=job_config
        )
    )
    return int(rows[0][0]) if rows else 0


def _guard(
    client: bigquery.Client,
    target: str,
    where_sql: str,
    job_config: bigquery.QueryJobConfig,
    incoming: int,
) -> None:
    existing = _count(client, target, where_sql, job_config)
    log.info(
        "replace %s WHERE %s: %d existing -> %d incoming rows",
        target, where_sql, existing, incoming,
    )
    if existing < MIN_GUARDED_ROWS or incoming >= existing * MIN_KEEP_RATIO:
        return
    if _shrink_allowed():
        log.warning(
            "%s=1: allowing %s to shrink %d -> %d rows",
            ALLOW_SHRINK_ENV, target, existing, incoming,
        )
        return
    raise ShrinkGuardError(target, where_sql, existing, incoming)


def atomic_replace(
    client: bigquery.Client,
    target: str,
    df: pd.DataFrame,
    schema: list[bigquery.SchemaField],
    where_sql: str,
    query_parameters: list | None = None,
) -> int:
    """Replace the rows of `target` matching `where_sql` with `df`, atomically.

    `target` is "project.dataset.table". An empty `df` is a DELETE-only swap
    (still transactional, no staging table). Raises ShrinkGuardError, deleting
    nothing, if `df` would leave under half of a slice of >= 100 rows.
    Returns len(df).
    """
    job_config = bigquery.QueryJobConfig(query_parameters=query_parameters or [])
    delete_stmt = f"DELETE FROM `{target}` WHERE {where_sql};"

    if len(df) == 0:
        _guard(client, target, where_sql, job_config, 0)
        client.query_and_wait(
            f"BEGIN TRANSACTION;\n{delete_stmt}\nCOMMIT TRANSACTION;",
            job_config=job_config,
        )
        return 0

    project, dataset, table = target.split(".")
    staging = f"{project}.{dataset}._staging_{table}_{uuid.uuid4().hex[:12]}"
    staging_table = bigquery.Table(staging, schema=schema)
    staging_table.expires = _dt.datetime.now(_dt.UTC) + _STAGING_TTL

    columns = ", ".join(f"`{f.name}`" for f in schema)
    try:
        client.create_table(staging_table)
        log.info("load %d rows -> staging %s", len(df), staging)
        client.load_table_from_dataframe(
            df,
            staging,
            job_config=bigquery.LoadJobConfig(
                schema=schema,
                write_disposition=bigquery.WriteDisposition.WRITE_APPEND,
                source_format=bigquery.SourceFormat.PARQUET,
            ),
        ).result()

        _guard(client, target, where_sql, job_config, len(df))

        log.info("swap %s WHERE %s (atomic)", target, where_sql)
        client.query_and_wait(
            "BEGIN TRANSACTION;\n"
            f"{delete_stmt}\n"
            f"INSERT INTO `{target}` ({columns}) SELECT {columns} FROM `{staging}`;\n"
            "COMMIT TRANSACTION;",
            job_config=job_config,
        )
    finally:
        try:
            client.delete_table(staging, not_found_ok=True)
        except Exception:  # never mask the real error; TTL cleans up regardless
            log.warning("could not drop staging table %s (expires in %s)", staging, _STAGING_TTL)
    return len(df)
