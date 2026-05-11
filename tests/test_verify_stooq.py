"""Tests for StooqVerifier (yfinance_bigquery.verify.stooq)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pandas as pd
import pytest

import yfinance_bigquery.verify.stooq as stooq_mod
from yfinance_bigquery.verify.stooq import STOOQ_TOLERANCES, StooqVerifier

FIXTURE = Path(__file__).parent / "fixtures" / "stooq_aapl_2024.parquet"

# ---------------------------------------------------------------------------
# Structural tests
# ---------------------------------------------------------------------------


def test_supported_metrics() -> None:
    assert set(StooqVerifier.SUPPORTED_METRICS) == {
        "bar_count",
        "close_mean",
        "volume_sum",
        "close_corr",
    }


def test_default_tolerances() -> None:
    assert STOOQ_TOLERANCES["bar_count"] == 2.0
    assert STOOQ_TOLERANCES["close_mean"] == 0.005
    assert STOOQ_TOLERANCES["volume_sum"] == 0.05
    assert STOOQ_TOLERANCES["close_corr"] == 0.001  # abs(corr - 1.0) <= 0.001


# ---------------------------------------------------------------------------
# bar_count
# ---------------------------------------------------------------------------


def test_run_bar_count_within_tolerance(monkeypatch: pytest.MonkeyPatch) -> None:
    """ours=252 bars, Stooq=250 bars; diff=2 == tolerance → within."""
    fake_client = MagicMock()
    # _run_aggregation_per_symbol returns {sym: (value, sample_size)}
    monkeypatch.setattr(
        stooq_mod,
        "_run_aggregation_per_symbol",
        lambda *a, **kw: {"AAPL": (252.0, 252)},
    )
    # _fetch_stooq returns DataFrame with 250 rows (within ±2)
    monkeypatch.setattr(
        StooqVerifier,
        "_fetch_stooq",
        lambda self, symbol, season: pd.DataFrame(
            {"close": [100.0] * 250, "volume": [1_000_000] * 250}
        ),
    )

    v = StooqVerifier(
        client=fake_client,
        table="p.d.ohlcv_1d",
        season=2024,
        metric="bar_count",
        symbols=["AAPL"],
    )
    result = v.run()
    assert result.total_compared == 1
    assert result.within_tolerance_count == 1
    assert result.deltas[0].within_tolerance is True
    assert result.deltas[0].diff == 2.0  # 252 - 250


def test_run_bar_count_outside_tolerance(monkeypatch: pytest.MonkeyPatch) -> None:
    """ours=252, Stooq=245; diff=7 > 2 → outside tolerance."""
    monkeypatch.setattr(
        stooq_mod,
        "_run_aggregation_per_symbol",
        lambda *a, **kw: {"AAPL": (252.0, 252)},
    )
    monkeypatch.setattr(
        StooqVerifier,
        "_fetch_stooq",
        lambda self, symbol, season: pd.DataFrame(
            {"close": [100.0] * 245, "volume": [1_000_000] * 245}
        ),
    )

    v = StooqVerifier(
        client=MagicMock(),
        table="p.d.ohlcv_1d",
        season=2024,
        metric="bar_count",
        symbols=["AAPL"],
    )
    result = v.run()
    assert result.within_tolerance_count == 0
    assert result.deltas[0].within_tolerance is False


# ---------------------------------------------------------------------------
# close_mean (multiplicative tolerance)
# ---------------------------------------------------------------------------


def test_run_close_mean_within_multiplicative_tolerance(monkeypatch: pytest.MonkeyPatch) -> None:
    """ours=175.0, Stooq mean=174.5; diff=0.5, abs_tol=174.5*0.005=0.8725 → within."""
    monkeypatch.setattr(
        stooq_mod,
        "_run_aggregation_per_symbol",
        lambda *a, **kw: {"AAPL": (175.0, 252)},
    )
    # Stooq close prices average to 174.5
    monkeypatch.setattr(
        StooqVerifier,
        "_fetch_stooq",
        lambda self, symbol, season: pd.DataFrame(
            {"close": [174.5] * 252, "volume": [1_000_000] * 252}
        ),
    )

    v = StooqVerifier(
        client=MagicMock(),
        table="p.d.ohlcv_1d",
        season=2024,
        metric="close_mean",
        symbols=["AAPL"],
    )
    result = v.run()
    assert result.total_compared == 1
    assert result.within_tolerance_count == 1
    delta = result.deltas[0]
    assert delta.ours == pytest.approx(175.0)
    assert delta.expected == pytest.approx(174.5)
    assert delta.within_tolerance is True


def test_run_close_mean_outside_multiplicative_tolerance(monkeypatch: pytest.MonkeyPatch) -> None:
    """ours=180.0, Stooq mean=174.5; diff=5.5, abs_tol=174.5*0.005=0.8725 → outside."""
    monkeypatch.setattr(
        stooq_mod,
        "_run_aggregation_per_symbol",
        lambda *a, **kw: {"AAPL": (180.0, 252)},
    )
    monkeypatch.setattr(
        StooqVerifier,
        "_fetch_stooq",
        lambda self, symbol, season: pd.DataFrame(
            {"close": [174.5] * 252, "volume": [1_000_000] * 252}
        ),
    )

    v = StooqVerifier(
        client=MagicMock(),
        table="p.d.ohlcv_1d",
        season=2024,
        metric="close_mean",
        symbols=["AAPL"],
    )
    result = v.run()
    assert result.within_tolerance_count == 0
    delta = result.deltas[0]
    assert delta.within_tolerance is False
    assert delta.diff == pytest.approx(5.5)


# ---------------------------------------------------------------------------
# close_corr
# ---------------------------------------------------------------------------


def _make_close_corr_verifier(symbols: list[str] | None = None) -> StooqVerifier:
    return StooqVerifier(
        client=MagicMock(),
        table="p.d.ohlcv_1d",
        season=2024,
        metric="close_corr",
        symbols=symbols or ["AAPL"],
    )


def test_run_close_corr_perfect(monkeypatch: pytest.MonkeyPatch) -> None:
    """When our close == Stooq close exactly, correlation = 1.0 → within tolerance."""
    prices = list(range(100, 352))  # 252 prices
    dates = pd.date_range("2024-01-02", periods=252, freq="B")

    # _fetch_ours_daily returns trading_date + close columns
    monkeypatch.setattr(
        StooqVerifier,
        "_fetch_ours_daily",
        lambda self, symbol: pd.DataFrame(
            {"trading_date": dates, "close": prices}
        ),
    )
    # _fetch_stooq returns same prices indexed by date
    monkeypatch.setattr(
        StooqVerifier,
        "_fetch_stooq",
        lambda self, symbol, season: pd.DataFrame(
            {"close": prices, "volume": [1_000_000] * 252},
            index=dates,
        ),
    )

    v = _make_close_corr_verifier()
    result = v.run()
    assert result.metric == "close_corr"
    assert result.total_compared == 1
    assert result.within_tolerance_count == 1
    delta = result.deltas[0]
    assert delta.ours == pytest.approx(1.0, abs=1e-10)
    assert delta.within_tolerance is True


def test_run_close_corr_imperfect_outside_tolerance(monkeypatch: pytest.MonkeyPatch) -> None:
    """Low correlation (far from 1.0) → outside tolerance."""
    n = 50
    dates = pd.date_range("2024-01-02", periods=n, freq="B")
    ours_prices = list(range(100, 100 + n))
    # Stooq has opposite trend — negative correlation
    stooq_prices = list(range(100 + n, 100, -1))

    monkeypatch.setattr(
        StooqVerifier,
        "_fetch_ours_daily",
        lambda self, symbol: pd.DataFrame(
            {"trading_date": dates, "close": ours_prices}
        ),
    )
    monkeypatch.setattr(
        StooqVerifier,
        "_fetch_stooq",
        lambda self, symbol, season: pd.DataFrame(
            {"close": stooq_prices, "volume": [1_000_000] * n},
            index=dates,
        ),
    )

    v = _make_close_corr_verifier()
    result = v.run()
    assert result.total_compared == 1
    assert result.within_tolerance_count == 0
    delta = result.deltas[0]
    assert delta.within_tolerance is False
    # Correlation should be strongly negative
    assert delta.ours < 0.0


def test_run_close_corr_skips_insufficient_data(monkeypatch: pytest.MonkeyPatch) -> None:
    """Symbols with fewer than 10 joined rows are skipped (not counted)."""
    dates = pd.date_range("2024-01-02", periods=5, freq="B")

    monkeypatch.setattr(
        StooqVerifier,
        "_fetch_ours_daily",
        lambda self, symbol: pd.DataFrame(
            {"trading_date": dates, "close": [100.0] * 5}
        ),
    )
    monkeypatch.setattr(
        StooqVerifier,
        "_fetch_stooq",
        lambda self, symbol, season: pd.DataFrame(
            {"close": [100.0] * 5, "volume": [1_000_000] * 5},
            index=dates,
        ),
    )

    v = _make_close_corr_verifier()
    result = v.run()
    # Symbol skipped entirely — total_compared = 0
    assert result.total_compared == 0


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def test_unsupported_metric_raises() -> None:
    with pytest.raises(ValueError, match="unsupported stooq metric"):
        StooqVerifier(
            client=MagicMock(),
            table="p.d.ohlcv_1d",
            season=2024,
            metric="batting_avg",
            symbols=["AAPL"],
        )


# ---------------------------------------------------------------------------
# Fixture smoke test (reads the parquet file — no network, no BQ)
# ---------------------------------------------------------------------------


def test_fixture_file_exists_and_has_expected_shape() -> None:
    """The pre-generated Stooq fixture should have ~251 rows for AAPL 2024."""
    assert FIXTURE.exists(), f"Missing fixture: {FIXTURE}"
    df = pd.read_parquet(FIXTURE)
    assert len(df) >= 240, f"Expected ~251 trading days, got {len(df)}"
    assert "Close" in df.columns or "close" in df.columns, "Fixture missing Close column"
    assert "Volume" in df.columns or "volume" in df.columns, "Fixture missing Volume column"
