"""Internal consistency verifier — checks OHLCV data quality within BigQuery."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Final

from google.cloud import bigquery

from yfinance_bigquery.verify.base import VerificationResult
from yfinance_bigquery.verify.compare import compare_series

if TYPE_CHECKING:
    from yfinance_bigquery.intervals import Interval

log = logging.getLogger(__name__)

# INTERNAL_AGG_SQL has 6 entries: the user-facing "trading_day_alignment" metric
# is split into two SQL variants by interval (_1d and _intraday).  The external-facing
# SUPPORTED_METRICS set keeps the single user-visible name (5 entries total).
INTERNAL_AGG_SQL: Final[dict[str, str]] = {
    "ohlc_monotonic": (
        "WITH violations AS (\n"
        "  SELECT symbol,\n"
        "         COUNTIF(NOT (high >= GREATEST(open, close)\n"
        "                      AND low <= LEAST(open, close)\n"
        "                      AND high >= low)) AS bad,\n"
        "         COUNT(*) AS total\n"
        "  FROM `{table}`\n"
        "  WHERE EXTRACT(YEAR FROM trading_date) = @season\n"
        "  GROUP BY symbol\n"
        ")\n"
        "SELECT symbol AS id,\n"
        "       SAFE_DIVIDE(bad, total) AS value,\n"
        "       total AS sample_size\n"
        "FROM violations"
    ),
    "volume_non_negative": (
        "WITH violations AS (\n"
        "  SELECT symbol,\n"
        "         COUNTIF(NOT (volume IS NULL OR volume >= 0)) AS bad,\n"
        "         COUNT(*) AS total\n"
        "  FROM `{table}`\n"
        "  WHERE EXTRACT(YEAR FROM trading_date) = @season\n"
        "  GROUP BY symbol\n"
        ")\n"
        "SELECT symbol AS id,\n"
        "       SAFE_DIVIDE(bad, total) AS value,\n"
        "       total AS sample_size\n"
        "FROM violations"
    ),
    "no_future_bars": (
        "WITH violations AS (\n"
        "  SELECT symbol,\n"
        "         COUNTIF(trading_date > CURRENT_DATE()) AS bad,\n"
        "         COUNT(*) AS total\n"
        "  FROM `{table}`\n"
        "  WHERE EXTRACT(YEAR FROM trading_date) = @season\n"
        "  GROUP BY symbol\n"
        ")\n"
        "SELECT symbol AS id,\n"
        "       SAFE_DIVIDE(bad, total) AS value,\n"
        "       total AS sample_size\n"
        "FROM violations"
    ),
    # 1d-only: weekend bars are violations (Sun=1, Sat=7 in BigQuery DAYOFWEEK).
    "trading_day_alignment_1d": (
        "WITH violations AS (\n"
        "  SELECT symbol,\n"
        "         COUNTIF(EXTRACT(DAYOFWEEK FROM trading_date) IN (1, 7)) AS bad,\n"
        "         COUNT(*) AS total\n"
        "  FROM `{table}`\n"
        "  WHERE EXTRACT(YEAR FROM trading_date) = @season\n"
        "  GROUP BY symbol\n"
        ")\n"
        "SELECT symbol AS id,\n"
        "       SAFE_DIVIDE(bad, total) AS value,\n"
        "       total AS sample_size\n"
        "FROM violations"
    ),
    # Intraday-only: bar_start_et hour-of-day check.
    # Market hours are 09:30-16:00 ET; allow ±15 min slack for pre/post bars.
    # Violation if the bar falls outside 09:15–16:15 ET.
    "trading_day_alignment_intraday": (
        "WITH violations AS (\n"
        "  SELECT symbol,\n"
        "         COUNTIF(\n"
        # bar_start_et is stored as TIMESTAMP (UTC internally); EXTRACT
        # without a tz arg defaults to UTC. Use AT TIME ZONE so we actually
        # check market-hour alignment in ET.
        "           EXTRACT(HOUR FROM bar_start_et AT TIME ZONE 'America/New_York') < 9\n"
        "           OR EXTRACT(HOUR FROM bar_start_et AT TIME ZONE 'America/New_York') > 16\n"
        "           OR (EXTRACT(HOUR FROM bar_start_et AT TIME ZONE 'America/New_York') = 9\n"
        "               AND EXTRACT(MINUTE FROM bar_start_et AT TIME ZONE 'America/New_York') < 15)\n"
        "           OR (EXTRACT(HOUR FROM bar_start_et AT TIME ZONE 'America/New_York') = 16\n"
        "               AND EXTRACT(MINUTE FROM bar_start_et AT TIME ZONE 'America/New_York') > 15)\n"
        "         ) AS bad,\n"
        "         COUNT(*) AS total\n"
        "  FROM `{table}`\n"
        "  WHERE EXTRACT(YEAR FROM trading_date) = @season\n"
        "  GROUP BY symbol\n"
        ")\n"
        "SELECT symbol AS id,\n"
        "       SAFE_DIVIDE(bad, total) AS value,\n"
        "       total AS sample_size\n"
        "FROM violations"
    ),
    # Window-function approach: any bar sharing (symbol, bar_start_utc) with another
    # counts as a duplicate.
    "no_duplicate_bars": (
        "WITH dup_check AS (\n"
        "  SELECT symbol,\n"
        "         COUNT(*) OVER (PARTITION BY symbol, bar_start_utc) AS occurrences\n"
        "  FROM `{table}`\n"
        "  WHERE EXTRACT(YEAR FROM trading_date) = @season\n"
        ")\n"
        "SELECT symbol AS id,\n"
        "       SAFE_DIVIDE(SUM(IF(occurrences > 1, 1, 0)), COUNT(*)) AS value,\n"
        "       COUNT(*) AS sample_size\n"
        "FROM dup_check\n"
        "GROUP BY symbol"
    ),
}


def _run_aggregation(
    client: bigquery.Client,
    sql: str,
    table: str,
    season: int,
) -> dict[str, tuple[float, int]]:
    """Execute the SQL against BQ and return {symbol: (value, sample_size)}."""
    job_config = bigquery.QueryJobConfig(
        query_parameters=[
            bigquery.ScalarQueryParameter("season", "INT64", season),
        ]
    )
    rows = client.query_and_wait(
        sql.format(table=table), job_config=job_config
    ).to_dataframe()
    return {
        str(r["id"]): (float(r["value"]), int(r["sample_size"]))
        for _, r in rows.iterrows()
    }


class InternalConsistencyVerifier:
    """Verify internal consistency of OHLCV bars already stored in BigQuery.

    All metrics use zero-tolerance: any violation fraction > 0 is a failure.
    The ``trading_day_alignment`` metric is dispatched to a 1d- or intraday-specific
    SQL variant based on ``interval``.
    """

    # External-facing metric names (5).  The SQL dict has 6 entries because
    # trading_day_alignment splits into _1d / _intraday variants.
    SUPPORTED_METRICS: Final[frozenset[str]] = frozenset([
        "ohlc_monotonic",
        "volume_non_negative",
        "no_future_bars",
        "trading_day_alignment",
        "no_duplicate_bars",
    ])

    def __init__(
        self,
        *,
        client: bigquery.Client,
        table: str,
        season: int,
        metric: str,
        interval: Interval,
        tolerance: float = 0.0,
    ) -> None:
        if metric not in self.SUPPORTED_METRICS:
            raise ValueError(
                f"unsupported metric {metric!r}; "
                f"choices: {sorted(self.SUPPORTED_METRICS)}"
            )
        self.client = client
        self.table = table
        self.season = season
        self.metric = metric
        self.interval = interval
        self.tolerance = tolerance

    def run(self) -> VerificationResult:
        log.info("verify internal %s for %d", self.metric, self.season)
        sql_key = self._resolve_sql_key()
        sql = INTERNAL_AGG_SQL[sql_key]
        ours_with_n = _run_aggregation(self.client, sql, self.table, self.season)
        expected: dict[int | str, float] = {sym: 0.0 for sym in ours_with_n}
        ours: dict[int | str, float] = {sym: v[0] for sym, v in ours_with_n.items()}
        sample_sizes: dict[int | str, int] = {sym: v[1] for sym, v in ours_with_n.items()}
        names: dict[int | str, str] = {sym: sym for sym in ours_with_n}
        deltas = compare_series(
            ours=ours,
            expected=expected,
            sample_sizes=sample_sizes,
            entity_names=names,
            tolerance=self.tolerance,
        )
        within = sum(1 for d in deltas if d.within_tolerance)
        return VerificationResult(
            metric=self.metric,
            season=self.season,
            aggregation="symbol-season",
            source="internal",
            tolerance=self.tolerance,
            total_compared=len(deltas),
            within_tolerance_count=within,
            deltas=deltas,
        )

    def _resolve_sql_key(self) -> str:
        """Map user-facing metric name to the correct INTERNAL_AGG_SQL key."""
        if self.metric == "trading_day_alignment":
            from yfinance_bigquery.intervals import Interval

            return (
                "trading_day_alignment_1d"
                if self.interval is Interval.D1
                else "trading_day_alignment_intraday"
            )
        return self.metric
