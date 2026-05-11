"""Tests for Interval enum and INTERVAL_CONFIG."""

from __future__ import annotations

import pytest

from yfinance_bigquery.intervals import INTERVAL_CONFIG, Interval


def test_interval_values_are_yfinance_strings():
    assert Interval.D1.value == "1d"
    assert Interval.M60.value == "60m"
    assert Interval.M15.value == "15m"
    assert Interval.M5.value == "5m"
    assert Interval.M1.value == "1m"


def test_interval_config_has_5_entries():
    assert set(INTERVAL_CONFIG) == set(Interval)


def test_interval_config_d1_defaults():
    cfg = INTERVAL_CONFIG[Interval.D1]
    assert cfg.default_lookback_days == 7
    assert cfg.default_chunk_by == "year"
    assert cfg.partition_granularity == "DAY"
    assert cfg.retention_days is None  # never trimmed


def test_interval_config_m1_defaults():
    cfg = INTERVAL_CONFIG[Interval.M1]
    assert cfg.default_lookback_days == 3
    assert cfg.default_chunk_by == "week"
    assert cfg.partition_granularity == "DAY"
    assert cfg.retention_days == 7


def test_interval_config_m60_defaults():
    cfg = INTERVAL_CONFIG[Interval.M60]
    assert cfg.partition_granularity == "MONTH"
    assert cfg.retention_days == 730


def test_interval_from_string():
    assert Interval.from_string("1d") is Interval.D1
    assert Interval.from_string("60m") is Interval.M60
    with pytest.raises(ValueError):
        Interval.from_string("4h")


def test_interval_table_name_default_prefix():
    assert Interval.D1.table_name(prefix="ohlcv") == "ohlcv_1d"
    assert Interval.M5.table_name(prefix="ohlcv") == "ohlcv_5m"
