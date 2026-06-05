"""Tests for yfinance_bigquery.verify.membership (MembershipVerifier)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from yfinance_bigquery.verify.membership import (
    MEMBERSHIP_CHECK_SQL,
    MembershipVerifier,
)

# ---------------------------------------------------------------------------
# Structural / SQL template tests
# ---------------------------------------------------------------------------


def test_supported_metrics() -> None:
    assert MembershipVerifier.SUPPORTED_METRICS == frozenset([
        "membership_completeness",
        "no_survivorship",
    ])


def test_completeness_sql_checks_overlap_and_inversion() -> None:
    sql = MEMBERSHIP_CHECK_SQL["membership_completeness"]
    # ordered by date_added per symbol, looking ahead to the next spell
    assert "LEAD(date_added)" in sql
    assert "PARTITION BY symbol" in sql
    # inverted spell + overlap + dangling-open-spell checks
    assert "date_added >= date_removed" in sql
    assert "next_added < date_removed" in sql


def test_no_survivorship_sql_is_table_level_indicator() -> None:
    sql = MEMBERSHIP_CHECK_SQL["no_survivorship"]
    # 0.0 when closed spells exist, 1.0 (violation) when the table is all-open
    assert "COUNTIF(date_removed IS NOT NULL) > 0" in sql
    assert "'__table__'" in sql


def test_unsupported_metric_raises() -> None:
    with pytest.raises(ValueError, match="unsupported metric"):
        MembershipVerifier(
            client=MagicMock(), table="p.d.sp500_membership", metric="nope"
        )


# ---------------------------------------------------------------------------
# run() — fake _run_membership_aggregation
# ---------------------------------------------------------------------------


def _verifier(metric: str) -> MembershipVerifier:
    return MembershipVerifier(
        client=MagicMock(), table="p.d.sp500_membership", metric=metric
    )


def test_completeness_all_clean_passes() -> None:
    fake = {"AAPL": (0.0, 1), "LUMN": (0.0, 2)}
    v = _verifier("membership_completeness")
    with patch(
        "yfinance_bigquery.verify.membership._run_membership_aggregation",
        return_value=fake,
    ):
        result = v.run()
    assert result.total_compared == 2
    assert result.within_tolerance_count == 2
    assert result.passed()


def test_completeness_overlap_fails() -> None:
    fake = {"AAPL": (0.0, 1), "BADX": (0.5, 2)}  # one of two spells malformed
    v = _verifier("membership_completeness")
    with patch(
        "yfinance_bigquery.verify.membership._run_membership_aggregation",
        return_value=fake,
    ):
        result = v.run()
    failing = [d for d in result.deltas if not d.within_tolerance]
    assert len(failing) == 1
    assert failing[0].entity_id == "BADX"
    assert not result.passed()


def test_no_survivorship_present_passes() -> None:
    """Closed spells present -> indicator 0.0 -> passes."""
    v = _verifier("no_survivorship")
    with patch(
        "yfinance_bigquery.verify.membership._run_membership_aggregation",
        return_value={"__table__": (0.0, 850)},
    ):
        result = v.run()
    assert result.total_compared == 1
    assert result.passed()


def test_no_survivorship_absent_fails() -> None:
    """No closed spells -> indicator 1.0 -> survivorship bias -> fails."""
    v = _verifier("no_survivorship")
    with patch(
        "yfinance_bigquery.verify.membership._run_membership_aggregation",
        return_value={"__table__": (1.0, 503)},
    ):
        result = v.run()
    assert result.within_tolerance_count == 0
    assert not result.passed()
