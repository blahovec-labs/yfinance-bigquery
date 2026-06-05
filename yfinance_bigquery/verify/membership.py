"""Membership verifier — integrity checks on the sp500_membership spell table.

Two checks, both zero-tolerance (any violation fraction > 0 fails):

  - ``membership_completeness``: every (symbol, date_added, date_removed) spell is
    well-formed — date_added < date_removed when both present, no OVERLAPPING
    spells for the same symbol, and an open spell (date_removed NULL) is the last
    one. Reported as a per-symbol violation fraction.
  - ``no_survivorship``: the table must contain since-removed symbols (CLOSED
    spells). A table of only open spells is survivorship-biased — it silently
    excludes everything that has left the index. Reported as a single table-level
    indicator (0 = closed spells present, 1 = none → biased).

Unlike the OHLCV verifier these are not season- or interval-scoped: membership is
a set of point-in-time spells, not yearly bars.
"""

from __future__ import annotations

import logging
from typing import Final

from google.cloud import bigquery

from yfinance_bigquery.verify.base import VerificationResult
from yfinance_bigquery.verify.compare import compare_series

log = logging.getLogger(__name__)

MEMBERSHIP_CHECK_SQL: Final[dict[str, str]] = {
    "membership_completeness": (
        "WITH ordered AS (\n"
        "  SELECT symbol, date_added, date_removed,\n"
        "         LEAD(date_added) OVER (\n"
        "           PARTITION BY symbol ORDER BY date_added\n"
        "         ) AS next_added\n"
        "  FROM `{table}`\n"
        "),\n"
        "flags AS (\n"
        "  SELECT symbol,\n"
        "         COUNTIF(\n"
        # inverted / zero-length spell
        "           (date_added IS NOT NULL AND date_removed IS NOT NULL\n"
        "            AND date_added >= date_removed)\n"
        # this spell overlaps the next one for the same symbol
        "           OR (date_removed IS NOT NULL AND next_added IS NOT NULL\n"
        "               AND next_added < date_removed)\n"
        # an OPEN spell that is not the symbol's last spell
        "           OR (date_removed IS NULL AND next_added IS NOT NULL)\n"
        "         ) AS bad,\n"
        "         COUNT(*) AS total\n"
        "  FROM ordered\n"
        "  GROUP BY symbol\n"
        ")\n"
        "SELECT symbol AS id,\n"
        "       SAFE_DIVIDE(bad, total) AS value,\n"
        "       total AS sample_size\n"
        "FROM flags"
    ),
    "no_survivorship": (
        "SELECT '__table__' AS id,\n"
        "       IF(COUNTIF(date_removed IS NOT NULL) > 0, 0.0, 1.0) AS value,\n"
        "       COUNT(*) AS sample_size\n"
        "FROM `{table}`"
    ),
}


def _run_membership_aggregation(
    client: bigquery.Client, sql: str, table: str
) -> dict[str, tuple[float, int]]:
    """Execute a (parameter-free) membership check and return {id: (value, n)}."""
    rows = client.query_and_wait(sql.format(table=table)).to_dataframe()
    return {
        str(r["id"]): (float(r["value"]), int(r["sample_size"]))
        for _, r in rows.iterrows()
    }


class MembershipVerifier:
    """Verify integrity of the sp500_membership spell table. Zero-tolerance."""

    SUPPORTED_METRICS: Final[frozenset[str]] = frozenset([
        "membership_completeness",
        "no_survivorship",
    ])

    def __init__(
        self,
        *,
        client: bigquery.Client,
        table: str,
        metric: str,
        tolerance: float = 0.0,
    ) -> None:
        if metric not in self.SUPPORTED_METRICS:
            raise ValueError(
                f"unsupported metric {metric!r}; "
                f"choices: {sorted(self.SUPPORTED_METRICS)}"
            )
        self.client = client
        self.table = table
        self.metric = metric
        self.tolerance = tolerance

    def run(self) -> VerificationResult:
        log.info("verify membership %s on %s", self.metric, self.table)
        sql = MEMBERSHIP_CHECK_SQL[self.metric]
        ours_with_n = _run_membership_aggregation(self.client, sql, self.table)
        expected: dict[int | str, float] = {k: 0.0 for k in ours_with_n}
        ours: dict[int | str, float] = {k: v[0] for k, v in ours_with_n.items()}
        sample_sizes: dict[int | str, int] = {
            k: v[1] for k, v in ours_with_n.items()
        }
        names: dict[int | str, str] = {k: k for k in ours_with_n}
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
            season=0,  # not season-scoped
            aggregation="membership-spell",
            source="internal",
            tolerance=self.tolerance,
            total_compared=len(deltas),
            within_tolerance_count=within,
            deltas=deltas,
        )
