"""CLI entrypoint: yfinance-bigquery {sync,universe,verify,docs}."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import date, timedelta

from google.cloud import bigquery

from yfinance_bigquery._version import __version__
from yfinance_bigquery.docs.renderers import (
    apply_data_dictionary,
    render_bq_descriptions,
    render_data_dictionary,
    render_dbt_yaml,
    render_llm_context,
    render_markdown,
)
from yfinance_bigquery.intervals import INTERVAL_CONFIG, Interval
from yfinance_bigquery.verify.internal import InternalConsistencyVerifier

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")
log = logging.getLogger("yfinance-bigquery")

ALL_INTERVALS: list[Interval] = [
    Interval.D1,
    Interval.M60,
    Interval.M15,
    Interval.M5,
    Interval.M1,
]
ALL_METRICS: list[str] = sorted(InternalConsistencyVerifier.SUPPORTED_METRICS)
DOC_FORMATS: list[str] = ["bq-apply", "llm", "dictionary", "markdown", "dbt"]


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="yfinance-bigquery")
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command", required=True)

    # ---------------------------------------------------------------------- #
    # sync
    # ---------------------------------------------------------------------- #
    p_sync = sub.add_parser("sync", help="Fetch Yahoo Finance OHLCV and write to BigQuery")
    p_sync.add_argument(
        "--interval",
        required=True,
        choices=["all", *[iv.value for iv in Interval]],
        help="Interval to sync; 'all' syncs all 5 intervals.",
    )
    p_sync.add_argument(
        "--start",
        help="YYYY-MM-DD start (inclusive); omit for daily-mode lookback",
    )
    p_sync.add_argument(
        "--end",
        help="YYYY-MM-DD end (inclusive); omit for daily-mode lookback",
    )
    p_sync.add_argument(
        "--dataset",
        required=True,
        help="Fully-qualified BigQuery dataset: project.dataset.yfinance_v2_analytics",
    )
    p_sync.add_argument(
        "--table-prefix",
        default="ohlcv",
        help="Prefix for OHLCV table names (default: ohlcv → ohlcv_1d, ohlcv_60m, …)",
    )
    p_sync.add_argument(
        "--dim-symbols",
        help=(
            "project.dataset.dim_symbols — read active tickers from here "
            "(required unless --symbols given)"
        ),
    )
    p_sync.add_argument(
        "--runs-table",
        help=(
            "project.dataset.table for the sync run log. "
            "Defaults to <dataset>._yfinance_ingest_runs."
        ),
    )
    p_sync.add_argument("--resume", action="store_true",
        help="Skip chunks already recorded as success/empty in --runs-table.")
    p_sync.add_argument(
        "--symbols",
        help="Comma-separated tickers (overrides --dim-symbols).",
    )
    p_sync.add_argument(
        "--chunk-by",
        choices=["year", "month", "week", "range"],
        help="Chunk strategy (default: per-interval default from INTERVAL_CONFIG).",
    )
    p_sync.add_argument("--batch-size", type=int, default=50,
        help="Tickers per yfinance.download call (default 50).")
    p_sync.add_argument("--sleep-seconds", type=float, default=3.0,
        help="Sleep between batches (default 3.0).")
    p_sync.add_argument("--skip-trim", action="store_true",
        help="Skip post-sync retention trim.")
    p_sync.add_argument("--dry-run", action="store_true",
        help="Parse + resolve tickers/chunks but skip BQ writes.")

    # ---------------------------------------------------------------------- #
    # universe
    # ---------------------------------------------------------------------- #
    p_uni = sub.add_parser("universe", help="Manage the dim_symbols universe table")
    uni_sub = p_uni.add_subparsers(dest="action", required=True)

    _add_universe_common = lambda p: (  # noqa: E731
        p.add_argument("--dim-symbols", required=True,
                       help="project.dataset.dim_symbols"),
        p.add_argument("--source", default="wikipedia", choices=["wikipedia"],
                       help="Source for constituents (default: wikipedia)"),
    )

    p_init = uni_sub.add_parser("init", help="Initialize dim_symbols from scratch")
    _add_universe_common(p_init)
    p_init.add_argument(
        "--create-if-missing",
        action="store_true",
        help="Create the dim_symbols table if it doesn't exist.",
    )

    p_refresh = uni_sub.add_parser("refresh", help="Refresh existing dim_symbols")
    _add_universe_common(p_refresh)

    p_list = uni_sub.add_parser("list", help="Print active tickers from dim_symbols")
    p_list.add_argument("--dim-symbols", required=True,
                        help="project.dataset.dim_symbols")

    # ---------------------------------------------------------------------- #
    # verify
    # ---------------------------------------------------------------------- #
    p_v = sub.add_parser("verify", help="Run internal consistency checks on OHLCV tables")
    p_v.add_argument(
        "--source",
        default="internal",
        choices=["internal"],
        help="Verification source (only 'internal' supported in v0.1.0).",
    )
    p_v.add_argument(
        "--interval",
        required=True,
        choices=["all", *[iv.value for iv in Interval]],
        help="Interval to verify; 'all' runs all 5.",
    )
    p_v.add_argument(
        "--aggregation",
        required=True,
        choices=["symbol-season"],
        help="Aggregation level (only 'symbol-season' in v0.1.0).",
    )
    p_v.add_argument(
        "--metric",
        required=True,
        choices=[*ALL_METRICS, "all"],
        help="Metric to check; 'all' runs all supported metrics.",
    )
    p_v.add_argument("--season", required=True, type=int, help="Calendar year to verify.")
    p_v.add_argument(
        "--table",
        help="project.dataset.ohlcv_1d — required for single-interval verify.",
    )
    p_v.add_argument(
        "--table-prefix",
        help="project.dataset.ohlcv — required when --interval all.",
    )
    p_v.add_argument("--threshold", type=float, default=1.00,
        help="Pass threshold (default 1.00 — zero tolerance for internal checks).")

    # ---------------------------------------------------------------------- #
    # docs
    # ---------------------------------------------------------------------- #
    p_docs = sub.add_parser("docs", help="Render documentation in various formats")
    p_docs.add_argument("--format", required=True, choices=DOC_FORMATS)
    p_docs.add_argument(
        "--table",
        help="project.dataset.table (required for bq-apply, dictionary).",
    )
    p_docs.add_argument("--dataset", help="Dataset name (required for dictionary format).")
    p_docs.add_argument("--apply", action="store_true",
        help="For dictionary format: write directly to --dictionary-table instead of stdout.")
    p_docs.add_argument(
        "--dictionary-table",
        help="project.dataset.table for data_dictionary (required with --apply).",
    )
    p_docs.add_argument("--output", default="-",
        help="Output file path or '-' for stdout (default).")

    return parser


# ---------------------------------------------------------------------------
# cmd_sync helpers
# ---------------------------------------------------------------------------


def _resolve_tickers(ns: argparse.Namespace, client: bigquery.Client) -> list[str]:
    """Return ticker list: --symbols CSV overrides --dim-symbols BQ query."""
    if ns.symbols:
        return [s.strip() for s in ns.symbols.split(",") if s.strip()]
    if not ns.dim_symbols:
        log.error("--dim-symbols or --symbols is required for sync")
        sys.exit(2)
    sql = f"SELECT symbol FROM `{ns.dim_symbols}` WHERE date_removed IS NULL ORDER BY symbol"
    rows = client.query_and_wait(sql).to_dataframe()
    return rows["symbol"].tolist()


def _trim_table(client: bigquery.Client, table_fq: str, retention_days: int) -> None:
    sql = (
        f"DELETE FROM `{table_fq}` "
        f"WHERE trading_date < CURRENT_DATE() - INTERVAL {retention_days} DAY"
    )
    log.info("trim: %s (retention %d days)", table_fq, retention_days)
    client.query_and_wait(sql)


# ---------------------------------------------------------------------------
# cmd_sync
# ---------------------------------------------------------------------------


def cmd_sync(ns: argparse.Namespace) -> int:
    from yfinance_bigquery.client import YFinanceClient
    from yfinance_bigquery.runs import RunsTable, RunsTableRef
    from yfinance_bigquery.writer import OHLCVTableRef, OHLCVWriter, _iter_chunks

    client = bigquery.Client()

    # Resolve tickers
    tickers = _resolve_tickers(ns, client)
    if not tickers:
        log.warning("no active tickers found; nothing to sync")
        return 0
    log.info("syncing %d tickers", len(tickers))

    # Resolve interval list
    if ns.interval == "all":
        intervals = ALL_INTERVALS
    else:
        intervals = [Interval.from_string(ns.interval)]

    # Resolve dataset ref parts (project.dataset.yfinance_v2_analytics)
    dataset_parts = ns.dataset.split(".")
    if len(dataset_parts) != 3:
        log.error("--dataset must be project.dataset.table_prefix format, got %r", ns.dataset)
        return 2
    proj, ds = dataset_parts[0], dataset_parts[1]

    # Runs table
    runs_table_fq = ns.runs_table or f"{proj}.{ds}._yfinance_ingest_runs"
    runs_ref = RunsTableRef.parse(runs_table_fq)
    runs = RunsTable(client=client)

    yf_client = YFinanceClient(
        batch_size=ns.batch_size,
        sleep_seconds=ns.sleep_seconds,
    )

    for interval in intervals:
        cfg = INTERVAL_CONFIG[interval]
        table_name = interval.table_name(prefix=ns.table_prefix)
        table_fq = f"{proj}.{ds}.{table_name}"
        ref = OHLCVTableRef.parse(table_fq)
        writer = OHLCVWriter(client=client, interval=interval)

        # Compute [start, end]
        if ns.start and ns.end:
            start_str, end_str = ns.start, ns.end
        else:
            end_dt = date.today()
            start_dt = end_dt - timedelta(days=cfg.default_lookback_days)
            start_str = start_dt.isoformat()
            end_str = end_dt.isoformat()

        chunk_by = ns.chunk_by or cfg.default_chunk_by
        chunks = _iter_chunks(start_str, end_str, chunk_by)

        if ns.resume and not ns.dry_run:
            completed = runs.completed_chunks_for_interval(ref=runs_ref, interval=interval)
            before = len(chunks)
            chunks = [
                (cs, ce) for cs, ce in chunks
                if (date.fromisoformat(cs), date.fromisoformat(ce)) not in completed
            ]
            skipped = before - len(chunks)
            if skipped:
                log.info("--resume: skipping %d completed chunks for %s", skipped, interval.value)

        if not ns.dry_run:
            writer.create_table_if_missing(ref)
            runs.create_table_if_missing(runs_ref)

        log.info(
            "interval=%s  chunks=%d  start=%s  end=%s  tickers=%d",
            interval.value, len(chunks), start_str, end_str, len(tickers),
        )

        for cs, ce in chunks:
            log.info("chunk %s -> %s  interval=%s", cs, ce, interval.value)
            if ns.dry_run:
                continue
            cs_d = date.fromisoformat(cs)
            ce_d = date.fromisoformat(ce)
            try:
                df = yf_client.fetch(tickers, interval, cs, ce)
                if df.empty:
                    runs.record_empty(
                        ref=runs_ref, interval=interval,
                        chunk_start=cs_d, chunk_end=ce_d, chunk_kind=chunk_by,
                    )
                    log.info("no data for %s chunk %s..%s", interval.value, cs, ce)
                    continue
                n = writer.write(ref, df, cs, ce)
                runs.record_success(
                    ref=runs_ref, interval=interval,
                    chunk_start=cs_d, chunk_end=ce_d, chunk_kind=chunk_by,
                    rows_written=n,
                )
            except Exception as exc:
                runs.record_failed(
                    ref=runs_ref, interval=interval,
                    chunk_start=cs_d, chunk_end=ce_d, chunk_kind=chunk_by,
                    error=str(exc),
                )
                raise

    # Post-sync retention trim
    if not ns.skip_trim and not ns.dry_run:
        for interval in intervals:
            cfg = INTERVAL_CONFIG[interval]
            if cfg.retention_days is not None:
                table_name = interval.table_name(prefix=ns.table_prefix)
                table_fq = f"{proj}.{ds}.{table_name}"
                _trim_table(client, table_fq, cfg.retention_days)

    return 0


# ---------------------------------------------------------------------------
# cmd_universe
# ---------------------------------------------------------------------------


def cmd_universe(ns: argparse.Namespace) -> int:
    from yfinance_bigquery.universe.client import WikipediaUniverseClient
    from yfinance_bigquery.universe.writer import DimSymbolsTableRef, DimSymbolsWriter

    if ns.action == "list":
        client = bigquery.Client()
        sql = (
            f"SELECT symbol FROM `{ns.dim_symbols}` "
            "WHERE date_removed IS NULL ORDER BY symbol"
        )
        rows = client.query_and_wait(sql).to_dataframe()
        for sym in rows["symbol"]:
            print(sym)
        return 0

    # init or refresh
    client = bigquery.Client()
    ref = DimSymbolsTableRef.parse(ns.dim_symbols)
    writer = DimSymbolsWriter(client=client)

    if ns.action == "init" and getattr(ns, "create_if_missing", False):
        writer.create_table_if_missing(ref)

    uni_client = WikipediaUniverseClient()
    constituents = uni_client.fetch_constituents()
    n = writer.merge(ref=ref, constituents=constituents)
    log.info("universe %s: merged %d constituents into %s", ns.action, n, ref)
    return 0


# ---------------------------------------------------------------------------
# cmd_verify
# ---------------------------------------------------------------------------


def cmd_verify(ns: argparse.Namespace) -> int:
    client = bigquery.Client()

    # Resolve intervals
    if ns.interval == "all":
        intervals = ALL_INTERVALS
    else:
        intervals = [Interval.from_string(ns.interval)]

    # Resolve metrics
    if ns.metric == "all":
        metrics = ALL_METRICS
    else:
        metrics = [ns.metric]

    # Validate table refs
    if len(intervals) > 1 and not ns.table_prefix:
        log.error("--table-prefix required when --interval all")
        return 2
    if len(intervals) == 1 and not ns.table and not ns.table_prefix:
        log.error("--table or --table-prefix required")
        return 2

    overall_pass = True
    all_results: list[dict] = []

    for interval in intervals:
        if ns.table_prefix:
            table_fq = f"{ns.table_prefix}_{interval.value}"
        else:
            table_fq = ns.table  # type: ignore[assignment]

        for metric in metrics:
            # Future hook: swap in a different verifier based on ns.source.
            # Currently only "internal" is supported.
            v = InternalConsistencyVerifier(
                client=client,
                table=table_fq,
                season=ns.season,
                metric=metric,
                interval=interval,
            )
            result = v.run()
            passed = result.passed(ns.threshold)
            verdict = "PASS" if passed else "FAIL"
            print(
                f"{metric} / {interval.value}"
                f" / compared={result.total_compared}"
                f" / within_tolerance={result.within_tolerance_count}"
                f" / {verdict}"
            )
            if not passed:
                overall_pass = False
            all_results.append(result.to_json())

    return 0 if overall_pass else 1


# ---------------------------------------------------------------------------
# cmd_docs
# ---------------------------------------------------------------------------


def cmd_docs(ns: argparse.Namespace) -> int:
    if ns.format == "bq-apply":
        if not ns.table:
            log.error("--table required for bq-apply")
            return 2
        client = bigquery.Client()
        table = client.get_table(ns.table)
        table.schema = render_bq_descriptions()
        client.update_table(table, ["schema"])
        log.info("updated schema descriptions on %s", ns.table)
        return 0

    if ns.format == "llm":
        out = render_llm_context()
    elif ns.format == "dictionary":
        if not (ns.dataset and ns.table):
            log.error("--dataset and --table required for dictionary")
            return 2
        if ns.apply:
            if not ns.dictionary_table:
                log.error("--dictionary-table required with --apply")
                return 2
            client = bigquery.Client()
            # Extract table name from the FQ ref (project.dataset.table → table)
            table_name = ns.table.split(".")[-1]
            n = apply_data_dictionary(
                client=client,
                dictionary_table=ns.dictionary_table,
                dataset=ns.dataset,
                table=table_name,
            )
            log.info("applied %d rows to %s", n, ns.dictionary_table)
            return 0
        table_name = ns.table.split(".")[-1]
        out = json.dumps(
            render_data_dictionary(dataset=ns.dataset, table=table_name), indent=2
        )
    elif ns.format == "markdown":
        out = render_markdown()
    elif ns.format == "dbt":
        out = render_dbt_yaml()
    else:
        raise AssertionError(f"unhandled format {ns.format!r}")

    if ns.output == "-":
        sys.stdout.write(out)
    else:
        with open(ns.output, "w", encoding="utf-8") as f:
            f.write(out)
    return 0


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    ns = parser.parse_args(argv)
    if ns.command == "sync":
        return cmd_sync(ns)
    if ns.command == "universe":
        return cmd_universe(ns)
    if ns.command == "verify":
        return cmd_verify(ns)
    if ns.command == "docs":
        return cmd_docs(ns)
    raise AssertionError(f"unhandled command {ns.command!r}")


if __name__ == "__main__":
    sys.exit(main())
