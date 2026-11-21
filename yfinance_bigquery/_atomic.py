"""Atomic replace-a-slice for the BigQuery writers.

A BigQuery load job cannot run inside a multi-statement transaction, so a plain
"DELETE, then load" is two independent commits: if the load fails, the DELETE has
already happened and the slice is simply gone. That is how statcast-bigquery lost
every 2026-06-02 pitch; this is the same helper as its 0.5.1 fix.

So: load the new rows into a short-lived staging table first (the step that
actually fails), then DELETE + INSERT ... SELECT in ONE transaction, then drop the
staging table. A failed load never touches the target; a failed swap rolls back.
"""

from __future__ import annotations

import datetime as _dt
import logging
import uuid

import pandas as pd
from google.cloud import bigquery

log = logging.getLogger(__name__)

# Safety net: if the process dies between load and drop, BigQuery expires it.
_STAGING_TTL = _dt.timedelta(hours=6)


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
    (still transactional, no staging table). Returns len(df).
    """
    job_config = bigquery.QueryJobConfig(query_parameters=query_parameters or [])
    delete_stmt = f"DELETE FROM `{target}` WHERE {where_sql};"

    if len(df) == 0:
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
