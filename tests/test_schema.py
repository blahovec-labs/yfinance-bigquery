"""Tests for ColumnSpec, OHLCV_SCHEMA, and DIM_SYMBOLS_SCHEMA."""

from __future__ import annotations

from yfinance_bigquery.intervals import Interval
from yfinance_bigquery.schema import (
    DIM_SYMBOLS_SCHEMA,
    OHLCV_SCHEMA,
    ColumnSpec,
    Partitioning,
    get_ohlcv_clustering,
    get_ohlcv_partitioning,
)

# ---------------------------------------------------------------------------
# OHLCV_SCHEMA
# ---------------------------------------------------------------------------


def test_ohlcv_schema_has_14_columns():
    assert len(OHLCV_SCHEMA) == 14


def test_ohlcv_schema_column_names():
    names = [col.name for col in OHLCV_SCHEMA]
    expected = [
        "symbol",
        "bar_start_utc",
        "bar_start_et",
        "trading_date",
        "open",
        "high",
        "low",
        "close",
        "adj_close",
        "volume",
        "dividends",
        "stock_splits",
        "interval",
        "_ingested_at",
    ]
    assert names == expected


def test_ohlcv_required_columns():
    required_names = {col.name for col in OHLCV_SCHEMA if col.mode == "REQUIRED"}
    assert required_names >= {
        "symbol",
        "bar_start_utc",
        "bar_start_et",
        "trading_date",
        "interval",
        "_ingested_at",
    }


def test_ohlcv_nullable_price_columns():
    nullable_names = {col.name for col in OHLCV_SCHEMA if col.mode == "NULLABLE"}
    assert nullable_names >= {"open", "high", "low", "close", "adj_close", "volume"}


def test_ohlcv_all_business_definitions_non_null():
    for col in OHLCV_SCHEMA:
        assert col.business_definition and col.business_definition.strip(), (
            f"{col.name}: business_definition must not be empty"
        )


def test_ohlcv_all_semantic_tags_non_empty():
    for col in OHLCV_SCHEMA:
        assert col.semantic_tags, f"{col.name}: semantic_tags must not be empty"


def test_ohlcv_statsapi_equivalent_always_none():
    for col in OHLCV_SCHEMA:
        assert col.statsapi_equivalent is None, (
            f"{col.name}: statsapi_equivalent should always be None for yfinance schema"
        )


def test_ohlcv_column_spec_types():
    type_map = {col.name: col.type for col in OHLCV_SCHEMA}
    assert type_map["symbol"] == "STRING"
    assert type_map["bar_start_utc"] == "TIMESTAMP"
    assert type_map["trading_date"] == "DATE"
    assert type_map["open"] == "FLOAT64"
    assert type_map["volume"] == "INT64"
    assert type_map["interval"] == "STRING"
    assert type_map["_ingested_at"] == "TIMESTAMP"


# ---------------------------------------------------------------------------
# DIM_SYMBOLS_SCHEMA
# ---------------------------------------------------------------------------


def test_dim_symbols_schema_has_7_columns():
    assert len(DIM_SYMBOLS_SCHEMA) == 7


def test_dim_symbols_column_names():
    names = [col.name for col in DIM_SYMBOLS_SCHEMA]
    assert names == [
        "symbol",
        "name",
        "sector",
        "industry",
        "date_added",
        "date_removed",
        "last_refreshed_at",
    ]


def test_dim_symbols_required_columns():
    required_names = {col.name for col in DIM_SYMBOLS_SCHEMA if col.mode == "REQUIRED"}
    assert required_names == {"symbol", "last_refreshed_at"}


def test_dim_symbols_nullable_columns():
    nullable_names = {col.name for col in DIM_SYMBOLS_SCHEMA if col.mode == "NULLABLE"}
    assert nullable_names == {"name", "sector", "industry", "date_added", "date_removed"}


def test_dim_symbols_all_business_definitions_non_null():
    for col in DIM_SYMBOLS_SCHEMA:
        assert col.business_definition and col.business_definition.strip(), (
            f"{col.name}: business_definition must not be empty"
        )


# ---------------------------------------------------------------------------
# ColumnSpec dataclass validation
# ---------------------------------------------------------------------------


def test_column_spec_rejects_empty_business_definition():
    import pytest

    with pytest.raises(ValueError, match="business_definition"):
        ColumnSpec(
            name="bad_col",
            type="STRING",
            mode="REQUIRED",
            short_description="A column.",
            business_definition="   ",
            semantic_tags=["identifier"],
            valid_range=None,
            valid_values=None,
            example_value=None,
            gotchas=[],
            statsapi_equivalent=None,
            yfinance_source_field=None,
            deprecated_in_year=None,
        )


# ---------------------------------------------------------------------------
# Partitioning helpers
# ---------------------------------------------------------------------------


def test_get_ohlcv_partitioning_d1():
    p = get_ohlcv_partitioning(Interval.D1)
    assert isinstance(p, Partitioning)
    assert p.field == "trading_date"
    assert p.type == "DAY"


def test_get_ohlcv_partitioning_m60():
    p = get_ohlcv_partitioning(Interval.M60)
    assert p.type == "MONTH"


def test_get_ohlcv_partitioning_m1():
    p = get_ohlcv_partitioning(Interval.M1)
    assert p.type == "DAY"


def test_get_ohlcv_clustering():
    clustering = get_ohlcv_clustering()
    assert clustering == ["symbol"]
