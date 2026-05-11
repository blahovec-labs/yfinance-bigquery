"""StooqVerifier: cross-source verification of 1d OHLCV against Stooq."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Final, cast

import pandas as pd
from google.cloud import bigquery

from yfinance_bigquery.verify.base import Comparison, VerificationResult

if TYPE_CHECKING:
    pass

log = logging.getLogger(__name__)

STOOQ_TOLERANCES: Final[dict[str, float]] = {
    "bar_count": 2.0,    # ±2 trading days (additive)
    "close_mean": 0.005,  # 0.5 % of expected value (multiplicative)
    "volume_sum": 0.05,   # 5 % of expected value (multiplicative)
    "close_corr": 0.001,  # abs(corr - 1.0) <= 0.001  (i.e., corr >= 0.999)
}

# Metrics that use multiplicative tolerance: abs(diff) <= abs(expected) * tol
_MULTIPLICATIVE_METRICS: Final[frozenset[str]] = frozenset({"close_mean", "volume_sum"})


class StooqVerifier:
    """Verify 1d OHLCV bars in BigQuery against Stooq data.

    Supported metrics
    -----------------
    bar_count   : number of trading days (additive ±2)
    close_mean  : average closing price per symbol (multiplicative 0.5 %)
    volume_sum  : total volume per symbol (multiplicative 5 %)
    close_corr  : Pearson correlation of close prices joined on trading_date (>= 0.999)
    """

    SUPPORTED_METRICS: Final[frozenset[str]] = frozenset(STOOQ_TOLERANCES.keys())

    def __init__(
        self,
        *,
        client: bigquery.Client,
        table: str,
        season: int,
        metric: str,
        symbols: list[str],
        tolerance: float | None = None,
    ) -> None:
        if metric not in self.SUPPORTED_METRICS:
            raise ValueError(
                f"unsupported stooq metric {metric!r}; "
                f"choices: {sorted(self.SUPPORTED_METRICS)}"
            )
        self.client = client
        self.table = table
        self.season = season
        self.metric = metric
        self.symbols = symbols
        self.tolerance = (
            tolerance if tolerance is not None else STOOQ_TOLERANCES[metric]
        )

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    def run(self) -> VerificationResult:
        """Run verification and return a VerificationResult."""
        log.info("verify stooq %s for %d (%d symbols)", self.metric, self.season, len(self.symbols))
        if self.metric == "close_corr":
            return self._run_close_corr()
        return self._run_aggregated()

    # ------------------------------------------------------------------
    # Aggregated metrics (bar_count, close_mean, volume_sum)
    # ------------------------------------------------------------------

    def _run_aggregated(self) -> VerificationResult:
        """Handle bar_count, close_mean, and volume_sum."""
        ours_raw = _run_aggregation_per_symbol(
            self.client, self.table, self.season, self.metric, self.symbols
        )
        # ours_raw: {symbol: (value, sample_size)}
        ours_values: dict[str, float] = {sym: v[0] for sym, v in ours_raw.items()}
        sample_sizes: dict[str, int] = {sym: v[1] for sym, v in ours_raw.items()}

        # Fetch Stooq expected values per symbol
        expected: dict[str, float] = {}
        stooq_sample_sizes: dict[str, int] = {}
        for sym in self.symbols:
            stooq_df = self._fetch_stooq(sym, self.season)
            exp_val, n = self._compute_metric_from_stooq(stooq_df)
            expected[sym] = exp_val
            stooq_sample_sizes[sym] = n

        # Build Comparison objects, respecting multiplicative vs additive tolerance
        deltas: list[Comparison] = []
        common_syms = sorted(set(ours_values) & set(expected))
        for sym in common_syms:
            ours_v = ours_values[sym]
            expected_v = expected[sym]
            diff = ours_v - expected_v
            if self.metric in _MULTIPLICATIVE_METRICS:
                abs_tol = abs(expected_v) * self.tolerance
            else:
                abs_tol = self.tolerance
            within = abs(diff) <= abs_tol
            deltas.append(
                Comparison(
                    entity_id=sym,
                    entity_name=sym,
                    ours=ours_v,
                    expected=expected_v,
                    diff=diff,
                    sample_size=sample_sizes.get(sym, stooq_sample_sizes.get(sym, 0)),
                    within_tolerance=within,
                )
            )

        within_count = sum(1 for d in deltas if d.within_tolerance)
        return VerificationResult(
            metric=self.metric,
            season=self.season,
            aggregation="symbol-season",
            source="stooq",
            tolerance=self.tolerance,
            total_compared=len(deltas),
            within_tolerance_count=within_count,
            deltas=deltas,
        )

    # ------------------------------------------------------------------
    # close_corr: per-day join + Pearson correlation
    # ------------------------------------------------------------------

    def _run_close_corr(self) -> VerificationResult:
        """Per-symbol Pearson correlation of ours.close vs Stooq.close, joined on trading_date."""
        deltas: list[Comparison] = []
        for sym in self.symbols:
            ours_df = self._fetch_ours_daily(sym)
            stooq_df = self._fetch_stooq(sym, self.season)

            # Normalise stooq index to date only (drop time component if present)
            stooq_df = stooq_df.copy()
            norm_idx = pd.to_datetime(stooq_df.index).normalize()  # type: ignore[union-attr]
            stooq_df.index = norm_idx

            # Prepare stooq close column for merge
            stooq_close = cast(pd.DataFrame, stooq_df[["close"]].copy())
            stooq_close.columns = pd.Index(["stooq_close"])

            # Join on trading_date (ours) vs Date index (stooq)
            merged = ours_df.merge(
                stooq_close,
                left_on="trading_date",
                right_index=True,
                how="inner",
            )
            if len(merged) < 10:
                log.warning("skip close_corr for %s: only %d joined rows", sym, len(merged))
                continue

            corr_cols = cast(pd.DataFrame, merged[["close", "stooq_close"]])
            corr_matrix: pd.DataFrame = corr_cols.corr()  # type: ignore[call-arg]
            corr = float(corr_matrix.iloc[0, 1])
            within = abs(corr - 1.0) <= self.tolerance
            deltas.append(
                Comparison(
                    entity_id=sym,
                    entity_name=sym,
                    ours=corr,
                    expected=1.0,
                    diff=corr - 1.0,
                    sample_size=len(merged),
                    within_tolerance=within,
                )
            )

        within_count = sum(1 for d in deltas if d.within_tolerance)
        return VerificationResult(
            metric="close_corr",
            season=self.season,
            aggregation="symbol-season",
            source="stooq",
            tolerance=self.tolerance,
            total_compared=len(deltas),
            within_tolerance_count=within_count,
            deltas=deltas,
        )

    # ------------------------------------------------------------------
    # Data-fetching helpers
    # ------------------------------------------------------------------

    def _fetch_stooq(self, symbol: str, season: int) -> pd.DataFrame:
        """Fetch 1d OHLCV for *symbol* for the full calendar year *season* from Stooq.

        Returns a DataFrame with lowercase columns (open/high/low/close/volume)
        and a DatetimeIndex named 'Date'.
        """
        try:
            from pandas_datareader import data as pdr  # noqa: PLC0415

            df: pd.DataFrame = pdr.DataReader(
                f"{symbol}.US",
                "stooq",
                f"{season}-01-01",
                f"{season}-12-31",
            )
        except Exception as exc:
            raise RuntimeError(
                f"Failed to fetch Stooq data for {symbol!r} season={season}: {exc}"
            ) from exc
        df.columns = [c.lower() for c in df.columns]
        return df

    def _fetch_ours_daily(self, symbol: str) -> pd.DataFrame:
        """Fetch our close prices from BigQuery for *symbol* in *season*.

        Returns a DataFrame with columns: trading_date (datetime.date), close (float).
        """
        sql = (
            "SELECT trading_date, close "
            f"FROM `{self.table}` "
            "WHERE EXTRACT(YEAR FROM trading_date) = @season "
            "AND symbol = @symbol "
            "ORDER BY trading_date"
        )
        job_config = bigquery.QueryJobConfig(
            query_parameters=[
                bigquery.ScalarQueryParameter("season", "INT64", self.season),
                bigquery.ScalarQueryParameter("symbol", "STRING", symbol),
            ]
        )
        rows: pd.DataFrame = self.client.query_and_wait(sql, job_config=job_config).to_dataframe()
        # Normalise trading_date to pandas Timestamp for join
        rows["trading_date"] = pd.to_datetime(rows["trading_date"]).dt.normalize()
        return cast(pd.DataFrame, rows[["trading_date", "close"]].copy())

    # ------------------------------------------------------------------
    # Internal computation helper
    # ------------------------------------------------------------------

    def _compute_metric_from_stooq(self, df: pd.DataFrame) -> tuple[float, int]:
        """Compute the scalar expected value from a Stooq DataFrame."""
        n = len(df)
        if self.metric == "bar_count":
            return float(n), n
        if self.metric == "close_mean":
            return float(df["close"].mean()), n
        if self.metric == "volume_sum":
            return float(df["volume"].sum()), n
        raise ValueError(f"_compute_metric_from_stooq: unexpected metric {self.metric!r}")


# ---------------------------------------------------------------------------
# BQ aggregation helper (module-level for monkeypatching in tests)
# ---------------------------------------------------------------------------


def _run_aggregation_per_symbol(
    client: bigquery.Client,
    table: str,
    season: int,
    metric: str,
    symbols: list[str],
) -> dict[str, tuple[float, int]]:
    """Query our BigQuery table and return {symbol: (value, sample_size)}.

    For close_corr, returns a placeholder (1.0, n) — the real computation
    is done inline in ``StooqVerifier._run_close_corr``.
    """
    metric_expr: dict[str, str] = {
        "bar_count": "COUNT(*) AS value, COUNT(*) AS sample_size",
        "close_mean": "AVG(close) AS value, COUNT(*) AS sample_size",
        "volume_sum": "SUM(volume) AS value, COUNT(*) AS sample_size",
        "close_corr": "1.0 AS value, COUNT(*) AS sample_size",
    }
    expr = metric_expr[metric]
    symbol_list = ", ".join(f"'{s}'" for s in symbols)
    sql = (
        f"SELECT symbol, {expr} "
        f"FROM `{table}` "
        f"WHERE EXTRACT(YEAR FROM trading_date) = @season "
        f"AND symbol IN ({symbol_list}) "
        f"GROUP BY symbol"
    )
    job_config = bigquery.QueryJobConfig(
        query_parameters=[
            bigquery.ScalarQueryParameter("season", "INT64", season),
        ]
    )
    rows: pd.DataFrame = client.query_and_wait(sql, job_config=job_config).to_dataframe()
    return {
        str(r["symbol"]): (float(r["value"]), int(r["sample_size"]))
        for _, r in rows.iterrows()
    }
