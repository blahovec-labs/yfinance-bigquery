"""Tests for CLI argument parsing and command dispatch.

Does NOT invoke real BigQuery or Yahoo Finance.
"""

from __future__ import annotations

import argparse
import logging
from datetime import date
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from yfinance_bigquery.cli import (
    build_parser,
    cmd_docs,
    cmd_sync,
    cmd_universe,
    cmd_verify,
    main,
)
from yfinance_bigquery.verify.base import VerificationResult

# ===========================================================================
# Parser shape tests
# ===========================================================================


class TestSyncParser:
    def test_accepts_basic_args(self):
        parser = build_parser()
        ns = parser.parse_args([
            "sync",
            "--interval", "1d",
            "--dataset", "p.ds",
            "--symbols", "AAPL,MSFT",
        ])
        assert ns.command == "sync"
        assert ns.interval == "1d"
        assert ns.dataset == "p.ds"
        assert ns.symbols == "AAPL,MSFT"

    def test_defaults(self):
        parser = build_parser()
        ns = parser.parse_args([
            "sync",
            "--interval", "all",
            "--dataset", "p.ds",
            "--symbols", "AAPL",
        ])
        assert ns.table_prefix == "ohlcv"
        assert ns.batch_size == 50
        assert ns.sleep_seconds == 3.0
        assert ns.resume is False
        assert ns.dry_run is False
        assert ns.skip_trim is False
        assert ns.runs_table is None
        assert ns.chunk_by is None

    def test_all_interval_accepted(self):
        parser = build_parser()
        ns = parser.parse_args([
            "sync",
            "--interval", "all",
            "--dataset", "p.ds",
            "--symbols", "AAPL",
        ])
        assert ns.interval == "all"

    def test_rejects_bad_interval(self):
        parser = build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args([
                "sync",
                "--interval", "2d",
                "--dataset", "p.ds",
                "--symbols", "AAPL",
            ])

    def test_runs_table_override(self):
        parser = build_parser()
        ns = parser.parse_args([
            "sync",
            "--interval", "1d",
            "--dataset", "p.ds",
            "--symbols", "AAPL",
            "--runs-table", "p.ds.custom_runs",
        ])
        assert ns.runs_table == "p.ds.custom_runs"

    def test_resume_flag(self):
        parser = build_parser()
        ns = parser.parse_args([
            "sync",
            "--interval", "1d",
            "--dataset", "p.ds",
            "--symbols", "AAPL",
            "--resume",
        ])
        assert ns.resume is True

    def test_dry_run_flag(self):
        parser = build_parser()
        ns = parser.parse_args([
            "sync",
            "--interval", "1d",
            "--dataset", "p.ds",
            "--symbols", "AAPL",
            "--dry-run",
        ])
        assert ns.dry_run is True


class TestUniverseParser:
    def test_init_parses(self):
        parser = build_parser()
        ns = parser.parse_args([
            "universe", "init",
            "--dim-symbols", "p.ds.dim_symbols",
            "--source", "wikipedia",
            "--create-if-missing",
        ])
        assert ns.command == "universe"
        assert ns.action == "init"
        assert ns.dim_symbols == "p.ds.dim_symbols"
        assert ns.create_if_missing is True

    def test_refresh_parses(self):
        parser = build_parser()
        ns = parser.parse_args([
            "universe", "refresh",
            "--dim-symbols", "p.ds.dim_symbols",
        ])
        assert ns.action == "refresh"
        assert ns.dim_symbols == "p.ds.dim_symbols"

    def test_list_parses(self):
        parser = build_parser()
        ns = parser.parse_args([
            "universe", "list",
            "--dim-symbols", "p.ds.dim_symbols",
        ])
        assert ns.action == "list"
        assert ns.dim_symbols == "p.ds.dim_symbols"

    def test_reconstruct_parses(self):
        parser = build_parser()
        ns = parser.parse_args([
            "universe", "reconstruct",
            "--membership-table", "p.ds.sp500_membership",
        ])
        assert ns.action == "reconstruct"
        assert ns.membership_table == "p.ds.sp500_membership"

    def test_unknown_source_rejected(self):
        parser = build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args([
                "universe", "init",
                "--dim-symbols", "p.ds.dim_symbols",
                "--source", "quandl",
            ])


class TestVerifyParser:
    def test_accepts_internal_source(self):
        parser = build_parser()
        ns = parser.parse_args([
            "verify",
            "--source", "internal",
            "--interval", "1d",
            "--aggregation", "symbol-season",
            "--metric", "ohlc_monotonic",
            "--season", "2024",
            "--table", "p.ds.ohlcv_1d",
        ])
        assert ns.command == "verify"
        assert ns.source == "internal"
        assert ns.season == 2024
        assert ns.threshold == 1.00

    def test_stooq_source_rejected(self):
        """--source stooq must NOT be accepted in v0.1.0."""
        parser = build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args([
                "verify",
                "--source", "stooq",
                "--interval", "1d",
                "--aggregation", "symbol-season",
                "--metric", "ohlc_monotonic",
                "--season", "2024",
                "--table", "p.ds.ohlcv_1d",
            ])

    def test_metric_all_accepted(self):
        parser = build_parser()
        ns = parser.parse_args([
            "verify",
            "--interval", "all",
            "--aggregation", "symbol-season",
            "--metric", "all",
            "--season", "2024",
            "--table-prefix", "p.ds.ohlcv",
        ])
        assert ns.metric == "all"
        assert ns.interval == "all"

    def test_custom_threshold(self):
        parser = build_parser()
        ns = parser.parse_args([
            "verify",
            "--interval", "1d",
            "--aggregation", "symbol-season",
            "--metric", "ohlc_monotonic",
            "--season", "2024",
            "--table", "p.ds.ohlcv_1d",
            "--threshold", "0.99",
        ])
        assert ns.threshold == 0.99


class TestDocsParser:
    def test_format_llm(self):
        parser = build_parser()
        ns = parser.parse_args(["docs", "--format", "llm"])
        assert ns.command == "docs"
        assert ns.format == "llm"
        assert ns.output == "-"

    def test_format_dictionary_with_apply(self):
        parser = build_parser()
        ns = parser.parse_args([
            "docs", "--format", "dictionary",
            "--dataset", "my_ds",
            "--table", "p.ds.ohlcv_1d",
            "--apply",
            "--dictionary-table", "p.shared.data_dictionary",
        ])
        assert ns.apply is True
        assert ns.dictionary_table == "p.shared.data_dictionary"

    def test_invalid_format_rejected(self):
        parser = build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["docs", "--format", "xml"])


class TestVersionFlag:
    def test_version_flag(self, capsys):
        from yfinance_bigquery import __version__
        with pytest.raises(SystemExit) as exc:
            main(["--version"])
        captured = capsys.readouterr()
        assert __version__ in captured.out
        assert exc.value.code == 0


# ===========================================================================
# cmd_universe integration tests (mocked)
# ===========================================================================


class TestCmdUniverseList:
    def test_list_prints_active_tickers(self, capsys, monkeypatch):
        fake_rows = pd.DataFrame({"symbol": ["AAPL", "MSFT", "GOOG"]})
        fake_result = MagicMock()
        fake_result.to_dataframe.return_value = fake_rows

        fake_client = MagicMock()
        fake_client.query_and_wait.return_value = fake_result

        monkeypatch.setattr("yfinance_bigquery.cli.bigquery.Client", lambda: fake_client)

        ns = argparse.Namespace(
            command="universe",
            action="list",
            dim_symbols="p.ds.dim_symbols",
        )
        rc = cmd_universe(ns)
        captured = capsys.readouterr()
        assert rc == 0
        assert "AAPL" in captured.out
        assert "MSFT" in captured.out
        assert "GOOG" in captured.out


class TestCmdUniverseInit:
    def test_init_happy_path(self, monkeypatch):
        fake_client = MagicMock()
        fake_writer = MagicMock()
        fake_writer.merge.return_value = 503
        fake_uni_client = MagicMock()
        fake_uni_client.fetch_constituents.return_value = pd.DataFrame(
            {"symbol": ["AAPL"], "name": ["Apple"], "sector": ["Tech"],
             "industry": ["IT"], "date_added": [None]}
        )

        ns = argparse.Namespace(
            command="universe",
            action="init",
            dim_symbols="p.ds.dim_symbols",
            source="wikipedia",
            create_if_missing=True,
        )

        # Deferred imports bind at call time; patch at the source modules.
        with patch("yfinance_bigquery.cli.bigquery.Client", return_value=fake_client), \
             patch("yfinance_bigquery.universe.writer.DimSymbolsWriter",
                   return_value=fake_writer), \
             patch("yfinance_bigquery.universe.client.WikipediaUniverseClient",
                   return_value=fake_uni_client):
            rc = cmd_universe(ns)

        assert rc == 0
        fake_writer.create_table_if_missing.assert_called_once()
        fake_writer.merge.assert_called_once()

    def test_refresh_skips_create(self, monkeypatch):
        fake_client = MagicMock()
        fake_writer = MagicMock()
        fake_writer.merge.return_value = 503
        fake_uni_client = MagicMock()
        fake_uni_client.fetch_constituents.return_value = pd.DataFrame(
            {"symbol": ["AAPL"], "name": ["Apple"], "sector": ["Tech"],
             "industry": ["IT"], "date_added": [None]}
        )

        ns = argparse.Namespace(
            command="universe",
            action="refresh",
            dim_symbols="p.ds.dim_symbols",
            source="wikipedia",
        )

        # Deferred imports bind at call time; patch at the source modules.
        with patch("yfinance_bigquery.cli.bigquery.Client", return_value=fake_client), \
             patch("yfinance_bigquery.universe.writer.DimSymbolsWriter",
                   return_value=fake_writer), \
             patch("yfinance_bigquery.universe.client.WikipediaUniverseClient",
                   return_value=fake_uni_client):
            rc = cmd_universe(ns)

        assert rc == 0
        fake_writer.create_table_if_missing.assert_not_called()
        fake_writer.merge.assert_called_once()


class TestCmdUniverseReconstruct:
    def test_reconstruct_happy_path(self):
        fake_client = MagicMock()
        fake_mwriter = MagicMock()
        fake_mwriter.replace.return_value = 510
        fake_uni_client = MagicMock()
        fake_uni_client.fetch_constituents.return_value = pd.DataFrame(
            {"symbol": ["AAPL"], "date_added": [date(1982, 11, 30)]}
        )
        fake_uni_client.fetch_changes.return_value = pd.DataFrame(
            {"date": [date(2023, 6, 20)],
             "added_ticker": ["FICO"], "removed_ticker": ["LUMN"]}
        )

        ns = argparse.Namespace(
            command="universe",
            action="reconstruct",
            membership_table="p.ds.sp500_membership",
        )

        # Deferred imports bind at call time; patch at the source modules.
        with patch("yfinance_bigquery.cli.bigquery.Client", return_value=fake_client), \
             patch("yfinance_bigquery.universe.writer.MembershipWriter",
                   return_value=fake_mwriter), \
             patch("yfinance_bigquery.universe.client.WikipediaUniverseClient",
                   return_value=fake_uni_client):
            rc = cmd_universe(ns)

        assert rc == 0
        fake_uni_client.fetch_constituents.assert_called_once()
        fake_uni_client.fetch_changes.assert_called_once()
        fake_mwriter.replace.assert_called_once()


# ===========================================================================
# cmd_sync integration tests (mocked)
# ===========================================================================


class TestCmdSyncResume:
    def test_resume_filters_completed_chunks(self, caplog):
        """--resume should filter chunks already in completed set."""
        fake_client = MagicMock()
        fake_writer = MagicMock()
        fake_writer.write.return_value = 10

        fake_runs = MagicMock()
        # Only 2024 is completed; 2025 is still pending.
        fake_runs.completed_chunks_for_interval.return_value = {
            (date(2024, 1, 1), date(2024, 12, 31)),
        }

        fake_yf = MagicMock()
        fake_df = pd.DataFrame({"symbol": ["AAPL"], "x": [1]})
        fake_yf.fetch.return_value = fake_df

        ns = argparse.Namespace(
            command="sync",
            interval="1d",
            dataset="p.ds",
            table_prefix="ohlcv",
            dim_symbols=None,
            symbols="AAPL",
            start="2024-01-01",
            end="2025-12-31",
            runs_table=None,
            resume=True,
            chunk_by="year",
            batch_size=50,
            sleep_seconds=0.0,
            skip_trim=True,
            dry_run=False,
        )

        caplog.set_level(logging.INFO)

        # Deferred imports bind at call time in cmd_sync; patch at source modules.
        with patch("yfinance_bigquery.cli.bigquery.Client", return_value=fake_client), \
             patch("yfinance_bigquery.writer.OHLCVWriter", return_value=fake_writer), \
             patch("yfinance_bigquery.runs.RunsTable", return_value=fake_runs), \
             patch("yfinance_bigquery.client.YFinanceClient", return_value=fake_yf):
            rc = cmd_sync(ns)

        assert rc == 0
        assert "skipping 1 completed chunks" in caplog.text
        # fetch should only be called once (for the 2025 chunk)
        assert fake_yf.fetch.call_count == 1

    def test_dry_run_does_not_write(self):
        fake_client = MagicMock()
        fake_writer = MagicMock()
        fake_runs = MagicMock()
        fake_yf = MagicMock()

        ns = argparse.Namespace(
            command="sync",
            interval="1d",
            dataset="p.ds",
            table_prefix="ohlcv",
            dim_symbols=None,
            symbols="AAPL",
            start="2024-01-01",
            end="2024-12-31",
            runs_table=None,
            resume=False,
            chunk_by="year",
            batch_size=50,
            sleep_seconds=0.0,
            skip_trim=True,
            dry_run=True,
        )

        # Deferred imports bind at call time in cmd_sync; patch at source modules.
        with patch("yfinance_bigquery.cli.bigquery.Client", return_value=fake_client), \
             patch("yfinance_bigquery.writer.OHLCVWriter", return_value=fake_writer), \
             patch("yfinance_bigquery.runs.RunsTable", return_value=fake_runs), \
             patch("yfinance_bigquery.client.YFinanceClient", return_value=fake_yf):
            rc = cmd_sync(ns)

        assert rc == 0
        fake_yf.fetch.assert_not_called()
        fake_writer.write.assert_not_called()


# ===========================================================================
# cmd_verify integration tests (mocked)
# ===========================================================================


class TestCmdVerify:
    def _make_result(self, *, total: int, within: int) -> VerificationResult:
        return VerificationResult(
            metric="ohlc_monotonic",
            season=2024,
            aggregation="symbol-season",
            source="internal",
            tolerance=0.0,
            total_compared=total,
            within_tolerance_count=within,
            deltas=[],
        )

    def test_all_pass_returns_0(self, capsys, monkeypatch):
        result = self._make_result(total=503, within=503)
        fake_verifier = MagicMock()
        fake_verifier.run.return_value = result
        fake_client = MagicMock()

        ns = argparse.Namespace(
            command="verify",
            source="internal",
            interval="1d",
            aggregation="symbol-season",
            metric="ohlc_monotonic",
            season=2024,
            table="p.ds.ohlcv_1d",
            table_prefix=None,
            threshold=1.00,
        )

        with patch("yfinance_bigquery.cli.bigquery.Client", return_value=fake_client), \
             patch("yfinance_bigquery.cli.InternalConsistencyVerifier",
                   return_value=fake_verifier):
            rc = cmd_verify(ns)

        assert rc == 0
        captured = capsys.readouterr()
        assert "PASS" in captured.out

    def test_any_fail_returns_1(self, capsys, monkeypatch):
        result = self._make_result(total=503, within=500)
        fake_verifier = MagicMock()
        fake_verifier.run.return_value = result
        fake_client = MagicMock()

        ns = argparse.Namespace(
            command="verify",
            source="internal",
            interval="1d",
            aggregation="symbol-season",
            metric="ohlc_monotonic",
            season=2024,
            table="p.ds.ohlcv_1d",
            table_prefix=None,
            threshold=1.00,  # strict
        )

        with patch("yfinance_bigquery.cli.bigquery.Client", return_value=fake_client), \
             patch("yfinance_bigquery.cli.InternalConsistencyVerifier",
                   return_value=fake_verifier):
            rc = cmd_verify(ns)

        assert rc == 1
        captured = capsys.readouterr()
        assert "FAIL" in captured.out

    def test_table_prefix_required_for_all_intervals(self, monkeypatch):
        fake_client = MagicMock()
        ns = argparse.Namespace(
            command="verify",
            source="internal",
            interval="all",
            aggregation="symbol-season",
            metric="ohlc_monotonic",
            season=2024,
            table=None,
            table_prefix=None,
            threshold=1.00,
        )
        with patch("yfinance_bigquery.cli.bigquery.Client", return_value=fake_client):
            rc = cmd_verify(ns)
        assert rc == 2


# ===========================================================================
# cmd_docs tests (mocked)
# ===========================================================================


class TestCmdDocs:
    def test_dictionary_apply_requires_dictionary_table(self, monkeypatch):
        """--apply without --dictionary-table returns rc=2."""
        parser = build_parser()
        ns = parser.parse_args([
            "docs", "--format", "dictionary",
            "--dataset", "my_dataset",
            "--table", "p.ds.ohlcv_1d",
            "--apply",
        ])
        assert ns.apply is True
        assert ns.dictionary_table is None

        # Ensure BQ is never touched
        monkeypatch.setattr(
            "yfinance_bigquery.cli.bigquery.Client",
            lambda: pytest.fail("bigquery.Client must not be called"),
        )
        rc = cmd_docs(ns)
        assert rc == 2

    def test_dictionary_requires_dataset_and_table(self, monkeypatch):
        """--format dictionary without --dataset errors with rc=2."""
        ns = argparse.Namespace(
            format="dictionary",
            dataset=None,
            table="p.ds.ohlcv_1d",
            apply=False,
            dictionary_table=None,
            output="-",
        )
        monkeypatch.setattr(
            "yfinance_bigquery.cli.bigquery.Client",
            lambda: pytest.fail("bigquery.Client must not be called"),
        )
        rc = cmd_docs(ns)
        assert rc == 2

    def test_bq_apply_requires_table(self, monkeypatch):
        ns = argparse.Namespace(format="bq-apply", table=None, output="-")
        monkeypatch.setattr(
            "yfinance_bigquery.cli.bigquery.Client",
            lambda: pytest.fail("bigquery.Client must not be called"),
        )
        rc = cmd_docs(ns)
        assert rc == 2

    def test_llm_format_writes_to_stdout(self, capsys):
        ns = argparse.Namespace(
            format="llm",
            table=None,
            dataset=None,
            apply=False,
            dictionary_table=None,
            output="-",
        )
        rc = cmd_docs(ns)
        assert rc == 0
        captured = capsys.readouterr()
        assert "yfinance OHLCV" in captured.out

    def test_markdown_format_writes_to_stdout(self, capsys):
        ns = argparse.Namespace(
            format="markdown",
            table=None,
            dataset=None,
            apply=False,
            dictionary_table=None,
            output="-",
        )
        rc = cmd_docs(ns)
        assert rc == 0
        captured = capsys.readouterr()
        assert "ohlcv" in captured.out.lower()

    def test_dbt_format_writes_to_stdout(self, capsys):
        ns = argparse.Namespace(
            format="dbt",
            table=None,
            dataset=None,
            apply=False,
            dictionary_table=None,
            output="-",
        )
        rc = cmd_docs(ns)
        assert rc == 0
        captured = capsys.readouterr()
        assert "models:" in captured.out
