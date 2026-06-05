"""Tests for yfinance_bigquery.verify.internal (InternalConsistencyVerifier)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from yfinance_bigquery.intervals import Interval
from yfinance_bigquery.verify.internal import INTERNAL_AGG_SQL, InternalConsistencyVerifier

# ---------------------------------------------------------------------------
# Structural / SQL template tests
# ---------------------------------------------------------------------------


def test_supported_metrics_user_facing() -> None:
    """SUPPORTED_METRICS must expose exactly 6 user-facing names."""
    assert len(InternalConsistencyVerifier.SUPPORTED_METRICS) == 6
    assert InternalConsistencyVerifier.SUPPORTED_METRICS == frozenset([
        "ohlc_monotonic",
        "volume_non_negative",
        "no_future_bars",
        "trading_day_alignment",
        "no_duplicate_bars",
        "corporate_action_continuity",
    ])


def test_internal_agg_sql_has_7_entries() -> None:
    """INTERNAL_AGG_SQL must have 7 entries (trading_day_alignment split into 2 variants)."""
    assert len(INTERNAL_AGG_SQL) == 7
    assert "trading_day_alignment_1d" in INTERNAL_AGG_SQL
    assert "trading_day_alignment_intraday" in INTERNAL_AGG_SQL
    # The user-facing name must NOT appear as a SQL key
    assert "trading_day_alignment" not in INTERNAL_AGG_SQL


def test_sql_template_corporate_action_continuity_flags_leaked_splits() -> None:
    """continuity SQL flags a large residual move ON a recorded split bar (a leaked
    split), gating on stock_splits>0 so legit crashes are not false-flagged."""
    sql = INTERNAL_AGG_SQL["corporate_action_continuity"]
    assert "LAG(close)" in sql
    assert "stock_splits > 0" in sql
    assert "> 0.25" in sql
    assert "SAFE_DIVIDE(close, prev_close)" in sql


def test_sql_template_ohlc_monotonic_has_check() -> None:
    """ohlc_monotonic SQL must contain the GREATEST/LEAST monotonic check."""
    sql = INTERNAL_AGG_SQL["ohlc_monotonic"]
    assert "GREATEST(open, close)" in sql
    assert "LEAST(open, close)" in sql
    assert "high >= low" in sql


def test_sql_template_no_duplicate_bars_uses_window() -> None:
    """no_duplicate_bars SQL must use a window function (OVER PARTITION BY)."""
    sql = INTERNAL_AGG_SQL["no_duplicate_bars"]
    assert "OVER" in sql
    assert "PARTITION BY" in sql
    assert "bar_start_utc" in sql


def test_sql_template_trading_day_alignment_1d_checks_weekend() -> None:
    """1d alignment SQL must filter Sun=1 / Sat=7."""
    sql = INTERNAL_AGG_SQL["trading_day_alignment_1d"]
    assert "DAYOFWEEK" in sql
    assert "1, 7" in sql or "(1, 7)" in sql


def test_sql_template_trading_day_alignment_intraday_checks_hours() -> None:
    """Intraday alignment SQL must check bar_start_et hours."""
    sql = INTERNAL_AGG_SQL["trading_day_alignment_intraday"]
    assert "bar_start_et" in sql
    assert "HOUR" in sql
    assert "MINUTE" in sql


# ---------------------------------------------------------------------------
# _resolve_sql_key dispatch
# ---------------------------------------------------------------------------


def _make_verifier(metric: str, interval: Interval) -> InternalConsistencyVerifier:
    return InternalConsistencyVerifier(
        client=MagicMock(),
        table="proj.dataset.ohlcv_1d",
        season=2024,
        metric=metric,
        interval=interval,
    )


def test_resolve_sql_key_dispatches_trading_day_1d() -> None:
    v = _make_verifier("trading_day_alignment", Interval.D1)
    assert v._resolve_sql_key() == "trading_day_alignment_1d"


def test_resolve_sql_key_dispatches_trading_day_intraday() -> None:
    v = _make_verifier("trading_day_alignment", Interval.M5)
    assert v._resolve_sql_key() == "trading_day_alignment_intraday"


def test_resolve_sql_key_passthrough() -> None:
    v = _make_verifier("ohlc_monotonic", Interval.D1)
    assert v._resolve_sql_key() == "ohlc_monotonic"


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def test_unsupported_metric_raises() -> None:
    with pytest.raises(ValueError, match="unsupported metric"):
        InternalConsistencyVerifier(
            client=MagicMock(),
            table="proj.dataset.ohlcv_1d",
            season=2024,
            metric="batting_avg",
            interval=Interval.D1,
        )


# ---------------------------------------------------------------------------
# run() — fake _run_aggregation
# ---------------------------------------------------------------------------

_FAKE_ZERO: dict[str, tuple[float, int]] = {
    "AAPL": (0.0, 252),
    "MSFT": (0.0, 252),
}

_FAKE_WITH_VIOLATION: dict[str, tuple[float, int]] = {
    "AAPL": (0.0, 252),
    "MSFT": (0.05, 252),   # 5 % violation rate — fails zero-tolerance
}


def test_run_zero_violations() -> None:
    """All symbols at 0.0 violation rate should all pass with tolerance=0.0."""
    v = _make_verifier("ohlc_monotonic", Interval.D1)
    with patch(
        "yfinance_bigquery.verify.internal._run_aggregation",
        return_value=_FAKE_ZERO,
    ):
        result = v.run()
    assert result.total_compared == 2
    assert result.within_tolerance_count == 2
    assert result.passed()


def test_run_some_violations() -> None:
    """Symbol with violation fraction > 0 must fail with zero tolerance."""
    v = _make_verifier("ohlc_monotonic", Interval.D1)
    with patch(
        "yfinance_bigquery.verify.internal._run_aggregation",
        return_value=_FAKE_WITH_VIOLATION,
    ):
        result = v.run()
    assert result.total_compared == 2
    assert result.within_tolerance_count == 1          # AAPL passes, MSFT fails
    failing = [d for d in result.deltas if not d.within_tolerance]
    assert len(failing) == 1
    assert failing[0].entity_id == "MSFT"


def test_entity_id_is_symbol_string() -> None:
    """entity_id in Comparison deltas must be the ticker string (str), not int."""
    v = _make_verifier("no_future_bars", Interval.D1)
    with patch(
        "yfinance_bigquery.verify.internal._run_aggregation",
        return_value=_FAKE_ZERO,
    ):
        result = v.run()
    for delta in result.deltas:
        assert isinstance(delta.entity_id, str), (
            f"entity_id {delta.entity_id!r} should be str, got {type(delta.entity_id)}"
        )
