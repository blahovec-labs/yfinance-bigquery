"""Tests for the 5 docs renderers + apply_data_dictionary."""

from __future__ import annotations

import json
from unittest.mock import MagicMock

from google.cloud import bigquery

from yfinance_bigquery.docs.renderers import (
    apply_data_dictionary,
    render_bq_descriptions,
    render_data_dictionary,
    render_dbt_yaml,
    render_llm_context,
    render_markdown,
)
from yfinance_bigquery.schema import OHLCV_SCHEMA

# Expected column count — must match len(OHLCV_SCHEMA) exactly.
_EXPECTED_COLS = 14

# REQUIRED columns in OHLCV_SCHEMA (used for dbt not_null test assertions).
_REQUIRED_COLS = [c.name for c in OHLCV_SCHEMA if c.mode == "REQUIRED"]


# ---------------------------------------------------------------------------
# render_bq_descriptions
# ---------------------------------------------------------------------------


def test_render_bq_descriptions_returns_schema_fields() -> None:
    fields = render_bq_descriptions()
    assert len(fields) == _EXPECTED_COLS
    for f in fields:
        assert isinstance(f, bigquery.SchemaField)
        assert f.description, f"{f.name} missing description"


def test_render_bq_descriptions_uses_correct_types() -> None:
    fields = {f.name: f for f in render_bq_descriptions()}
    # symbol: STRING REQUIRED
    assert fields["symbol"].field_type == "STRING"
    assert fields["symbol"].mode == "REQUIRED"
    # _ingested_at: TIMESTAMP REQUIRED
    assert fields["_ingested_at"].field_type == "TIMESTAMP"
    assert fields["_ingested_at"].mode == "REQUIRED"
    # volume: INT64 NULLABLE
    assert fields["volume"].field_type == "INT64"
    assert fields["volume"].mode == "NULLABLE"
    # open: FLOAT64 NULLABLE
    assert fields["open"].field_type == "FLOAT64"
    assert fields["open"].mode == "NULLABLE"
    # trading_date: DATE REQUIRED
    assert fields["trading_date"].field_type == "DATE"
    assert fields["trading_date"].mode == "REQUIRED"


# ---------------------------------------------------------------------------
# render_data_dictionary
# ---------------------------------------------------------------------------


def test_render_data_dictionary_shape() -> None:
    rows = render_data_dictionary(dataset="my_dataset", table="ohlcv_1d")
    assert len(rows) == _EXPECTED_COLS
    expected_keys = {
        "dataset",
        "table",
        "column",
        "dtype",
        "description",
        "business_definition",
        "owner",
        "tags",
        "source_system",
        "upstream_lineage_json",
        "created_at",
        "updated_at",
    }
    for r in rows:
        assert expected_keys <= r.keys(), f"Missing keys in row for column {r.get('column')}"
        assert r["dataset"] == "my_dataset"
        assert r["table"] == "ohlcv_1d"
        assert r["source_system"] == "yfinance"


def test_render_data_dictionary_is_valid_json() -> None:
    rows = render_data_dictionary(dataset="d", table="t")
    json.dumps(rows)  # must not raise


def test_render_data_dictionary_upstream_lineage_contains_yfinance_field() -> None:
    rows = render_data_dictionary(dataset="d", table="t")
    # symbol has yfinance_source_field="symbol"
    symbol_row = next(r for r in rows if r["column"] == "symbol")
    lineage = json.loads(symbol_row["upstream_lineage_json"])
    assert lineage["library"] == "yfinance-bigquery"
    assert "yfinance_field" in lineage


# ---------------------------------------------------------------------------
# render_llm_context
# ---------------------------------------------------------------------------


def test_render_llm_context_contains_invariants_section() -> None:
    md = render_llm_context()
    assert "OHLCV invariants" in md


def test_render_llm_context_contains_all_columns() -> None:
    md = render_llm_context()
    for c in OHLCV_SCHEMA:
        assert c.name in md, f"Column {c.name!r} not found in LLM context"


def test_render_llm_context_contains_required_sections() -> None:
    md = render_llm_context()
    assert "# yfinance OHLCV for LLMs" in md
    assert "## Column reference" in md
    assert "## OHLCV invariants" in md
    assert "## Tables by interval" in md
    # all 5 interval tables mentioned
    for iv in ["ohlcv_1d", "ohlcv_60m", "ohlcv_15m", "ohlcv_5m", "ohlcv_1m"]:
        assert iv in md, f"Expected table {iv!r} in LLM context"


# ---------------------------------------------------------------------------
# render_markdown
# ---------------------------------------------------------------------------


def test_render_markdown_lists_all_columns() -> None:
    md = render_markdown()
    for c in OHLCV_SCHEMA:
        assert c.name in md, f"Column {c.name!r} not found in markdown"


def test_render_markdown_has_table_structure() -> None:
    md = render_markdown()
    assert "# ohlcv_* schema" in md
    assert "| Column |" in md


# ---------------------------------------------------------------------------
# render_dbt_yaml
# ---------------------------------------------------------------------------


def test_render_dbt_yaml_includes_model_name() -> None:
    yml = render_dbt_yaml(model_name="ohlcv_1d")
    assert "- name: ohlcv_1d" in yml


def test_render_dbt_yaml_marks_required_columns_with_not_null() -> None:
    yml = render_dbt_yaml(model_name="ohlcv_1d")
    assert "not_null" in yml, "Expected at least one not_null test"
    # Every REQUIRED column should appear before a not_null test block.
    # We verify by checking that each required column name appears in the YAML
    # and that not_null appears at least once per required column.
    for col_name in _REQUIRED_COLS:
        # Find the position of the column name and check not_null follows it
        col_idx = yml.find(f"- name: {col_name}")
        assert col_idx != -1, f"Column {col_name!r} not found in dbt YAML"
        # not_null should appear after the column entry (within the YAML)
        assert "not_null" in yml[col_idx:], (
            f"not_null test missing for REQUIRED column {col_name!r}"
        )


def test_render_dbt_yaml_lists_all_columns() -> None:
    yml = render_dbt_yaml()
    for c in OHLCV_SCHEMA:
        assert f"- name: {c.name}" in yml, f"Column {c.name!r} missing from dbt YAML"


# ---------------------------------------------------------------------------
# apply_data_dictionary
# ---------------------------------------------------------------------------


def test_apply_data_dictionary_runs_delete_then_insert() -> None:
    """apply_data_dictionary should DELETE old rows for (dataset, table) then INSERT
    new ones, wrapped in a single BEGIN TRANSACTION ... COMMIT TRANSACTION."""
    client = MagicMock()
    result = apply_data_dictionary(
        client=client,
        dictionary_table="proj.shared_ops.data_dictionary",
        dataset="my_dataset",
        table="ohlcv_1d",
    )
    # One multi-statement query: BEGIN; DELETE; INSERT; COMMIT
    assert client.query_and_wait.call_count == 1
    sql = client.query_and_wait.call_args[0][0]
    job_config = client.query_and_wait.call_args[1]["job_config"]

    assert "BEGIN TRANSACTION" in sql.upper()
    assert "DELETE FROM `proj.shared_ops.data_dictionary`" in sql
    assert "INSERT INTO `proj.shared_ops.data_dictionary`" in sql
    assert "COMMIT TRANSACTION" in sql.upper()

    # Verify @dataset / @table parameters used (not string interpolation)
    params = {p.name: p.value for p in job_config.query_parameters}
    assert params["dataset"] == "my_dataset"
    assert params["table"] == "ohlcv_1d"

    # Return value is the number of rows inserted
    assert result == _EXPECTED_COLS


def test_apply_data_dictionary_inserts_one_row_per_column() -> None:
    client = MagicMock()
    apply_data_dictionary(
        client=client,
        dictionary_table="proj.shared_ops.data_dictionary",
        dataset="my_dataset",
        table="ohlcv_1d",
    )
    sql = client.query_and_wait.call_args[0][0]
    insert_idx = sql.index("INSERT INTO")
    insert_section = sql[insert_idx:]
    for spec in OHLCV_SCHEMA:
        assert f"'{spec.name}'" in insert_section, (
            f"Column {spec.name!r} missing from INSERT VALUES section"
        )
