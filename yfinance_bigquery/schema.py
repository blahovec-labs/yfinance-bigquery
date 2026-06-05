"""ColumnSpec dataclass + OHLCV_SCHEMA + DIM_SYMBOLS_SCHEMA.

Single source of truth for all BigQuery table definitions in yfinance-bigquery.
Every column carries a machine-readable ``ColumnSpec`` that drives five doc
renderers (BQ-native, LLM context, data dictionary, hobbyist Markdown, dbt YAML).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from yfinance_bigquery.intervals import INTERVAL_CONFIG, Interval

SCHEMA_VERSION = "0.1.0"

BqType = Literal["INT64", "FLOAT64", "STRING", "BOOL", "DATE", "TIMESTAMP"]
BqMode = Literal["REQUIRED", "NULLABLE"]

_VALID_TYPES = set(BqType.__args__)  # type: ignore[attr-defined]
_VALID_MODES = set(BqMode.__args__)  # type: ignore[attr-defined]


@dataclass(frozen=True)
class ColumnSpec:
    """Single source of truth for one column in an OHLCV or dim_symbols table."""

    name: str
    type: BqType
    mode: BqMode
    short_description: str
    business_definition: str
    semantic_tags: list[str]
    valid_range: tuple[float, float] | None
    valid_values: list[str] | None
    example_value: object | None
    gotchas: list[str]
    statsapi_equivalent: str | None  # always None for yfinance
    yfinance_source_field: str | None  # the yfinance column name, or None for synthetic fields
    deprecated_in_year: int | None

    def __post_init__(self) -> None:
        if self.type not in _VALID_TYPES:
            raise ValueError(f"{self.name}: invalid type {self.type!r}")
        if self.mode not in _VALID_MODES:
            raise ValueError(f"{self.name}: invalid mode {self.mode!r}")
        if not self.business_definition.strip():
            raise ValueError(f"{self.name}: business_definition required")


@dataclass(frozen=True)
class Partitioning:
    field: str
    type: str  # "DAY" | "MONTH"


def get_ohlcv_partitioning(interval: Interval) -> Partitioning:
    """Return Partitioning for the per-interval OHLCV table.

    Uses INTERVAL_CONFIG[interval].partition_granularity.
    Field is always 'trading_date'.
    """
    cfg = INTERVAL_CONFIG[interval]
    return Partitioning(field="trading_date", type=cfg.partition_granularity)


def get_ohlcv_clustering() -> list[str]:
    """Return clustering fields for any OHLCV table."""
    return ["symbol"]


# ---------------------------------------------------------------------------
# OHLCV_SCHEMA — 14 columns, common across all 5 per-interval tables
# ---------------------------------------------------------------------------

OHLCV_SCHEMA: list[ColumnSpec] = [
    # -------------------------------------------------------------------------
    # Identifiers
    # -------------------------------------------------------------------------
    ColumnSpec(
        name="symbol",
        type="STRING",
        mode="REQUIRED",
        short_description="Ticker symbol (e.g. AAPL).",
        business_definition=(
            "Yahoo Finance ticker symbol identifying the equity; always uppercase and "
            "stable across stock splits and rebrands within Yahoo's system. Joins to "
            "dim_symbols.symbol. May carry dot suffixes for share classes (e.g. BRK.B) "
            "or foreign-listed issues (e.g. ASML.AS) — verify against dim_symbols when "
            "computing universe-level aggregates."
        ),
        semantic_tags=["identifier", "join_key"],
        valid_range=None,
        valid_values=None,
        example_value="AAPL",
        gotchas=[
            "Yahoo tickers differ from CRSP/Bloomberg convention: BRK.B vs BRK/B. "
            "Never use this column as a join key against non-Yahoo sources without a "
            "ticker-mapping step."
        ],
        statsapi_equivalent=None,
        yfinance_source_field="symbol",
        deprecated_in_year=None,
    ),
    # -------------------------------------------------------------------------
    # Timestamps / partition key
    # -------------------------------------------------------------------------
    ColumnSpec(
        name="bar_start_utc",
        type="TIMESTAMP",
        mode="REQUIRED",
        short_description="UTC start of the OHLCV bar window.",
        business_definition=(
            "Coordinated Universal Time (UTC) timestamp marking the beginning of the "
            "price bar. UTC is DST-invariant, so this column is the preferred join key "
            "when correlating bars across instruments or computing inter-bar durations "
            "that span a DST boundary."
        ),
        semantic_tags=["temporal", "bar_boundary"],
        valid_range=None,
        valid_values=None,
        example_value="2024-04-01 13:30:00 UTC",
        gotchas=[
            "yfinance returns intraday timestamps in the exchange's local timezone; "
            "the ingestion pipeline converts to UTC before writing. Always filter on "
            "trading_date (ET) rather than DATE(bar_start_utc) to avoid off-by-one "
            "errors near midnight UTC during US market hours."
        ],
        statsapi_equivalent=None,
        yfinance_source_field="Datetime",
        deprecated_in_year=None,
    ),
    ColumnSpec(
        name="bar_start_et",
        type="TIMESTAMP",
        mode="REQUIRED",
        short_description="America/New_York start of the bar window (denormalized).",
        business_definition=(
            "Bar start timestamp converted to the America/New_York timezone, stored "
            "as a TIMESTAMP with the ET offset baked in. Denormalized from bar_start_utc "
            "for query convenience when filtering by market-session hour (e.g. "
            "EXTRACT(HOUR FROM bar_start_et) BETWEEN 9 AND 16) without timezone math."
        ),
        semantic_tags=["temporal", "bar_boundary", "denormalized"],
        valid_range=None,
        valid_values=None,
        example_value="2024-04-01 09:30:00-04:00",
        gotchas=[
            "This column reflects wall-clock ET including DST offsets (-05:00 in "
            "winter, -04:00 in summer). Do not use for duration arithmetic — use "
            "bar_start_utc instead to avoid DST discontinuities."
        ],
        statsapi_equivalent=None,
        yfinance_source_field="Datetime",
        deprecated_in_year=None,
    ),
    ColumnSpec(
        name="trading_date",
        type="DATE",
        mode="REQUIRED",
        short_description="US equity session date (ET). Partition key.",
        business_definition=(
            "Calendar date (America/New_York) of the US equity trading session in which "
            "this bar falls. Serves as the BigQuery partition key for all OHLCV tables, "
            "so include this column in WHERE clauses to enable partition pruning and "
            "minimize query cost. Pre-market and after-hours bars carry the date of the "
            "nearest regular session, not the wall-clock date."
        ),
        semantic_tags=["temporal", "partition_key", "identifier"],
        valid_range=None,
        valid_values=None,
        example_value="2024-04-01",
        gotchas=[
            "Always filter on trading_date rather than DATE(bar_start_utc) to exploit "
            "partition pruning. Querying without a trading_date filter on large date "
            "ranges will scan every partition and incur significant cost."
        ],
        statsapi_equivalent=None,
        yfinance_source_field=None,
        deprecated_in_year=None,
    ),
    # -------------------------------------------------------------------------
    # OHLCV price columns
    # -------------------------------------------------------------------------
    ColumnSpec(
        name="open",
        type="FLOAT64",
        mode="NULLABLE",
        short_description="Opening price of the bar (unadjusted).",
        business_definition=(
            "First traded price during the bar window, in USD, as reported by Yahoo "
            "Finance without split or dividend adjustment. For the 1d interval this is "
            "the official NYSE/NASDAQ opening auction price; for intraday intervals it "
            "is the first transaction price within the bar."
        ),
        semantic_tags=["price", "ohlc"],
        valid_range=(0.0, 1_000_000.0),
        valid_values=None,
        example_value=189.30,
        gotchas=[
            "NULL indicates yfinance returned no data for this bar (e.g. trading halt "
            "or data gap). A non-NULL open does not guarantee a liquid, tradeable quote."
        ],
        statsapi_equivalent=None,
        yfinance_source_field="Open",
        deprecated_in_year=None,
    ),
    ColumnSpec(
        name="high",
        type="FLOAT64",
        mode="NULLABLE",
        short_description="Highest price traded during the bar (unadjusted).",
        business_definition=(
            "Maximum transaction price recorded within the bar window, in USD, as "
            "reported by Yahoo Finance without adjustment. For intraday bars this "
            "reflects the intrabar high from consolidated tape data. Always satisfies "
            "high >= open, high >= close, and high >= low when all four are non-NULL."
        ),
        semantic_tags=["price", "ohlc"],
        valid_range=(0.0, 1_000_000.0),
        valid_values=None,
        example_value=191.05,
        gotchas=[
            "For very short intraday bars (1m, 5m) during low-liquidity periods, "
            "open == high == low == close is common and does not indicate bad data."
        ],
        statsapi_equivalent=None,
        yfinance_source_field="High",
        deprecated_in_year=None,
    ),
    ColumnSpec(
        name="low",
        type="FLOAT64",
        mode="NULLABLE",
        short_description="Lowest price traded during the bar (unadjusted).",
        business_definition=(
            "Minimum transaction price recorded within the bar window, in USD, as "
            "reported by Yahoo Finance without adjustment. For intraday bars this "
            "reflects the intrabar low from consolidated tape data. Always satisfies "
            "low <= open, low <= close, and low <= high when all four are non-NULL."
        ),
        semantic_tags=["price", "ohlc"],
        valid_range=(0.0, 1_000_000.0),
        valid_values=None,
        example_value=188.45,
        gotchas=[
            "Extreme low spikes (e.g. a single erroneous print) can make a bar's "
            "low appear many percent below the open/close. Cross-validate against "
            "other sources before using low in risk models."
        ],
        statsapi_equivalent=None,
        yfinance_source_field="Low",
        deprecated_in_year=None,
    ),
    ColumnSpec(
        name="close",
        type="FLOAT64",
        mode="NULLABLE",
        short_description="Closing price of the bar (unadjusted, raw).",
        business_definition=(
            "Last traded price during the bar window, in USD, as reported by Yahoo "
            "Finance without split or dividend adjustment (auto_adjust=False). For the "
            "1d interval this is the official closing auction price; for intraday "
            "intervals it is the last transaction price before the bar boundary. "
            "Use adj_close for return calculations on the 1d table."
        ),
        semantic_tags=["price", "ohlc"],
        valid_range=(0.0, 1_000_000.0),
        valid_values=None,
        example_value=189.95,
        gotchas=[
            "Raw close does not account for splits or dividends — do not use for "
            "multi-year return series without first adjusting. Use adj_close (1d) "
            "or the pre-adjusted intraday close for adjusted time-series work."
        ],
        statsapi_equivalent=None,
        yfinance_source_field="Close",
        deprecated_in_year=None,
    ),
    ColumnSpec(
        name="adj_close",
        type="FLOAT64",
        mode="NULLABLE",
        short_description="Split- and dividend-adjusted close (1d only; NULL for intraday).",
        business_definition=(
            "Closing price retroactively adjusted for stock splits and cash dividends "
            "as computed by Yahoo Finance. Available only for the 1d interval — always "
            "NULL for 60m, 15m, 5m, and 1m bars because yfinance pre-adjusts intraday "
            "OHLCV in-place (the raw close IS the adjusted close for those intervals). "
            "Use this column for total-return calculations and multi-year price series."
        ),
        semantic_tags=["price", "adjusted", "return_series"],
        valid_range=(0.0, 1_000_000.0),
        valid_values=None,
        example_value=187.62,
        gotchas=[
            "Yahoo Finance re-adjusts historical adj_close values on split and dividend "
            "events, so the same bar's adj_close can differ across ingestion runs. "
            "Snapshot the adj_close at ingestion time if you need a stable series.",
            "NULL for all intraday intervals — this is expected, not a data gap.",
        ],
        statsapi_equivalent=None,
        yfinance_source_field="Adj Close",
        deprecated_in_year=None,
    ),
    ColumnSpec(
        name="volume",
        type="INT64",
        mode="NULLABLE",
        short_description="Share volume traded during the bar.",
        business_definition=(
            "Total number of shares exchanged during the bar window, as reported by "
            "Yahoo Finance from consolidated tape data. For 1d bars this is the official "
            "daily volume; for intraday bars it is the per-interval tape volume. "
            "Expressed in whole shares (not thousands or millions)."
        ),
        semantic_tags=["volume", "liquidity"],
        valid_range=(0.0, 100_000_000_000.0),
        valid_values=None,
        example_value=62_345_100,
        gotchas=[
            "Zero is a valid volume (e.g. overnight bars, pre-market thinly traded "
            "symbols) and does not indicate a data error. NULL means yfinance returned "
            "no volume data for this bar.",
            "After-hours volume is not included in the official daily volume figure "
            "for most symbols on major US exchanges.",
        ],
        statsapi_equivalent=None,
        yfinance_source_field="Volume",
        deprecated_in_year=None,
    ),
    ColumnSpec(
        name="dividends",
        type="FLOAT64",
        mode="NULLABLE",
        short_description="Cash dividend distributed on this bar date (1d only; 0 for intraday).",
        business_definition=(
            "Per-share cash dividend amount (in USD) declared and distributed on this "
            "bar's trading_date, as reported by Yahoo Finance. Non-zero only on "
            "ex-dividend dates for the 1d interval; always 0.0 for intraday bars "
            "because yfinance does not report dividends at sub-daily resolution. "
            "Use in conjunction with adj_close to reconcile total return."
        ),
        semantic_tags=["corporate_action", "return_series"],
        valid_range=(0.0, 1_000.0),
        valid_values=None,
        example_value=0.24,
        gotchas=[
            "Always 0.0 for intraday intervals — this is expected behavior from "
            "yfinance, not a data gap.",
            "Special dividends and return-of-capital distributions appear here with "
            "the same format as ordinary dividends.",
        ],
        statsapi_equivalent=None,
        yfinance_source_field="Dividends",
        deprecated_in_year=None,
    ),
    ColumnSpec(
        name="stock_splits",
        type="FLOAT64",
        mode="NULLABLE",
        short_description="Split ratio on this bar date (1d only; 0 for intraday).",
        business_definition=(
            "Stock split ratio effective on this bar's trading_date, as reported by "
            "Yahoo Finance; a value of 4.0 represents a 4-for-1 forward split and "
            "0.5 represents a 1-for-2 reverse split. Non-zero only on split effective "
            "dates for the 1d interval; always 0.0 for intraday intervals because "
            "yfinance does not report splits at sub-daily resolution."
        ),
        semantic_tags=["corporate_action"],
        valid_range=(0.0, 100.0),
        valid_values=None,
        example_value=4.0,
        gotchas=[
            "Always 0.0 for intraday intervals — this is expected behavior from "
            "yfinance, not a data gap.",
            "A value of 0.0 (rather than NULL or 1.0) indicates no split on that date. "
            "Filter stock_splits != 0 to find split events.",
        ],
        statsapi_equivalent=None,
        yfinance_source_field="Stock Splits",
        deprecated_in_year=None,
    ),
    # -------------------------------------------------------------------------
    # Interval label (denormalized for cross-table UNION queries)
    # -------------------------------------------------------------------------
    ColumnSpec(
        name="interval",
        type="STRING",
        mode="REQUIRED",
        short_description="Bar duration label (e.g. '1d', '60m').",
        business_definition=(
            "Denormalized label identifying the bar duration. One of '1d', '60m', "
            "'15m', '5m', '1m'. Redundant within a single per-interval table but "
            "essential for cross-table UNION queries that combine multiple intervals "
            "into one result set for multi-resolution analysis."
        ),
        semantic_tags=["identifier", "denormalized"],
        valid_range=None,
        valid_values=["1d", "60m", "15m", "5m", "1m"],
        example_value="1d",
        gotchas=[
            "This value is injected by the ingestion pipeline, not sourced from "
            "yfinance, so it always matches the table's intended interval even if "
            "the upstream yfinance call used a different string alias."
        ],
        statsapi_equivalent=None,
        yfinance_source_field=None,
        deprecated_in_year=None,
    ),
    # -------------------------------------------------------------------------
    # Audit / provenance
    # -------------------------------------------------------------------------
    ColumnSpec(
        name="_ingested_at",
        type="TIMESTAMP",
        mode="REQUIRED",
        short_description="UTC timestamp when this row was written by the pipeline.",
        business_definition=(
            "UTC timestamp recorded by the ingestion pipeline at the moment this row "
            "was loaded into BigQuery. Useful for auditing freshness, debugging "
            "reingestion events, and reconstructing the pipeline's write history. "
            "Leading underscore follows the BQ convention of hiding audit columns from "
            "the BQ Console's default column view."
        ),
        semantic_tags=["audit", "provenance"],
        valid_range=None,
        valid_values=None,
        example_value="2026-05-11 12:05:00 UTC",
        gotchas=[
            "This column reflects ingestion time, not the bar's trading time. Two "
            "rows for the same (symbol, bar_start_utc) from different pipeline runs "
            "will have different _ingested_at values; the idempotent DELETE+INSERT "
            "pattern ensures only the latest write survives."
        ],
        statsapi_equivalent=None,
        yfinance_source_field=None,
        deprecated_in_year=None,
    ),
]


# ---------------------------------------------------------------------------
# DIM_SYMBOLS_SCHEMA — 7 columns, unpartitioned dim table
# ---------------------------------------------------------------------------

DIM_SYMBOLS_SCHEMA: list[ColumnSpec] = [
    ColumnSpec(
        name="symbol",
        type="STRING",
        mode="REQUIRED",
        short_description="Ticker symbol. Primary key.",
        business_definition=(
            "Yahoo Finance ticker symbol for the S&P 500 constituent. Serves as the "
            "primary key of this table and the join key to all OHLCV tables. Always "
            "uppercase. A symbol is present in this table from the date it entered "
            "the S&P 500 index (or the date the universe was first bootstrapped) "
            "through removal, after which date_removed is set."
        ),
        semantic_tags=["identifier", "primary_key", "join_key"],
        valid_range=None,
        valid_values=None,
        example_value="AAPL",
        gotchas=[
            "The same company may have appeared under a different ticker at a prior "
            "point (e.g. FB → META). Historical OHLCV rows carry the ticker that was "
            "active at ingestion time, which may differ from the current symbol.",
        ],
        statsapi_equivalent=None,
        yfinance_source_field="Symbol",
        deprecated_in_year=None,
    ),
    ColumnSpec(
        name="name",
        type="STRING",
        mode="NULLABLE",
        short_description="Company name from Wikipedia 'Security' column.",
        business_definition=(
            "Human-readable company name as listed in the Wikipedia 'List of S&P 500 "
            "companies' table under the 'Security' column. Used for display and "
            "labeling purposes; not guaranteed to match the official legal entity name "
            "or the name reported on SEC filings."
        ),
        semantic_tags=["metadata", "display"],
        valid_range=None,
        valid_values=None,
        example_value="Apple Inc.",
        gotchas=[
            "Wikipedia names may lag corporate rebrands by days to weeks. "
            "NULL if the scraper cannot parse the name from the page."
        ],
        statsapi_equivalent=None,
        yfinance_source_field=None,
        deprecated_in_year=None,
    ),
    ColumnSpec(
        name="sector",
        type="STRING",
        mode="NULLABLE",
        short_description="GICS Sector (e.g. 'Information Technology').",
        business_definition=(
            "Global Industry Classification Standard (GICS) Sector assigned to the "
            "company, as listed on Wikipedia's S&P 500 constituent table. One of the "
            "11 top-level GICS sectors. Use for sector-level aggregation and "
            "factor analysis."
        ),
        semantic_tags=["metadata", "classification", "gics"],
        valid_range=None,
        valid_values=[
            "Communication Services",
            "Consumer Discretionary",
            "Consumer Staples",
            "Energy",
            "Financials",
            "Health Care",
            "Industrials",
            "Information Technology",
            "Materials",
            "Real Estate",
            "Utilities",
        ],
        example_value="Information Technology",
        gotchas=[
            "Wikipedia's sector classification may lag official GICS reclassifications "
            "by weeks. For authoritative sector data use a licensed GICS source.",
            "NULL if the scraper cannot parse the sector from Wikipedia.",
        ],
        statsapi_equivalent=None,
        yfinance_source_field=None,
        deprecated_in_year=None,
    ),
    ColumnSpec(
        name="industry",
        type="STRING",
        mode="NULLABLE",
        short_description="GICS Sub-Industry (e.g. 'Technology Hardware, Storage & Peripherals').",
        business_definition=(
            "Global Industry Classification Standard (GICS) Sub-Industry assigned to "
            "the company, as listed in Wikipedia's 'GICS Sub-Industry' column. More "
            "granular than sector; use for peer-group comparisons and industry-level "
            "factor analysis. There are 158 GICS Sub-Industries as of the 2023 update."
        ),
        semantic_tags=["metadata", "classification", "gics"],
        valid_range=None,
        valid_values=None,
        example_value="Technology Hardware, Storage & Peripherals",
        gotchas=[
            "GICS Sub-Industry names can be long strings; avoid hard-coding them — "
            "query distinct values from this table instead.",
            "NULL if the scraper cannot parse the sub-industry from Wikipedia.",
        ],
        statsapi_equivalent=None,
        yfinance_source_field=None,
        deprecated_in_year=None,
    ),
    ColumnSpec(
        name="date_added",
        type="DATE",
        mode="NULLABLE",
        short_description="Date the symbol was added to the S&P 500 index.",
        business_definition=(
            "Calendar date on which this symbol was added to the S&P 500 index, parsed "
            "from Wikipedia's 'Date added' column. Use to construct point-in-time "
            "universes (i.e. symbols that were S&P 500 constituents as of a given date) "
            "for survivorship-bias-free backtesting."
        ),
        semantic_tags=["temporal", "universe_management"],
        valid_range=None,
        valid_values=None,
        example_value="1982-11-30",
        gotchas=[
            "NULL when Wikipedia's date string cannot be parsed (common for early "
            "constituents where only a year is listed). Do not assume NULL means "
            "the company was never formally added.",
            "This reflects the index inclusion date, not the IPO date.",
        ],
        statsapi_equivalent=None,
        yfinance_source_field=None,
        deprecated_in_year=None,
    ),
    ColumnSpec(
        name="date_removed",
        type="DATE",
        mode="NULLABLE",
        short_description="Date the symbol was removed from S&P 500. NULL = active constituent.",
        business_definition=(
            "Calendar date on which this symbol was removed from the S&P 500 index, "
            "as inferred by the ingestion pipeline when Wikipedia's constituent list "
            "no longer includes the symbol. NULL indicates the symbol is an active "
            "index constituent as of the most recent universe refresh. "
            "Set automatically by DimSymbolsWriter when the symbol drops off Wikipedia."
        ),
        semantic_tags=["temporal", "universe_management"],
        valid_range=None,
        valid_values=None,
        example_value=None,
        gotchas=[
            "This date is the pipeline's detection date (when the Wikipedia scrape "
            "first missed the symbol), not an official index removal date from S&P. "
            "It may lag the actual removal by up to one day (the refresh cadence).",
            "Filter date_removed IS NULL to restrict queries to active constituents.",
        ],
        statsapi_equivalent=None,
        yfinance_source_field=None,
        deprecated_in_year=None,
    ),
    ColumnSpec(
        name="last_refreshed_at",
        type="TIMESTAMP",
        mode="REQUIRED",
        short_description="UTC timestamp of the most recent universe refresh for this row.",
        business_definition=(
            "UTC timestamp set by DimSymbolsWriter each time the MERGE statement "
            "touches this row during a universe refresh. Indicates when the pipeline "
            "last confirmed or updated the metadata for this symbol. Use to detect "
            "staleness — a symbol whose last_refreshed_at is more than two days old "
            "may indicate a failed refresh job."
        ),
        semantic_tags=["audit", "provenance"],
        valid_range=None,
        valid_values=None,
        example_value="2026-05-11 12:10:00 UTC",
        gotchas=[
            "This timestamp updates on every successful MERGE even if no fields "
            "changed, so it reflects the refresh cadence rather than actual data "
            "changes. Use EXCEPT to diff row content if you need change detection."
        ],
        statsapi_equivalent=None,
        yfinance_source_field=None,
        deprecated_in_year=None,
    ),
]


# ---------------------------------------------------------------------------
# SP500_MEMBERSHIP_SCHEMA — point-in-time membership spells (survivorship-free)
# ---------------------------------------------------------------------------

SP500_MEMBERSHIP_SCHEMA: list[ColumnSpec] = [
    ColumnSpec(
        name="symbol",
        type="STRING",
        mode="REQUIRED",
        short_description="Ticker symbol of an S&P 500 member during this spell.",
        business_definition=(
            "Yahoo Finance ticker symbol that was an S&P 500 constituent for the "
            "[date_added, date_removed) interval described by this row. Unlike "
            "dim_symbols, a symbol may appear in MULTIPLE rows here (one per "
            "membership spell), so this column is NOT a primary key — join on "
            "(symbol, date) windows, not on symbol alone."
        ),
        semantic_tags=["identifier", "join_key"],
        valid_range=None,
        valid_values=None,
        example_value="LUMN",
        gotchas=[
            "Not unique: re-additions and historically-removed symbols produce "
            "multiple rows. Use the point-in-time predicate "
            "(date_added <= D AND (date_removed IS NULL OR date_removed > D)).",
        ],
        statsapi_equivalent=None,
        yfinance_source_field=None,
        deprecated_in_year=None,
    ),
    ColumnSpec(
        name="date_added",
        type="DATE",
        mode="NULLABLE",
        short_description="Date this membership spell began.",
        business_definition=(
            "Calendar date the symbol entered the S&P 500 for this spell. Sourced "
            "from Wikipedia's current-constituents 'Date added' column or, for "
            "since-removed symbols, the matching addition in Wikipedia's dated "
            "changes log. NULL when the addition predates the reconstruction window "
            "(~2019) — treat such spells as 'member since at least the window start'."
        ),
        semantic_tags=["temporal", "universe_management"],
        valid_range=None,
        valid_values=None,
        example_value="2020-01-01",
        gotchas=[
            "NULL does NOT mean 'never added' — it means the addition is older than "
            "the reconstructed changes window. Do not run backtests before the "
            "documented window-start date.",
        ],
        statsapi_equivalent=None,
        yfinance_source_field=None,
        deprecated_in_year=None,
    ),
    ColumnSpec(
        name="date_removed",
        type="DATE",
        mode="NULLABLE",
        short_description="Date this spell ended. NULL = still a member.",
        business_definition=(
            "Calendar date the symbol left the S&P 500 for this spell, from "
            "Wikipedia's dated changes log. NULL marks an OPEN spell (a current "
            "constituent). Recovering since-removed symbols as closed spells is the "
            "survivorship-bias fix: a universe built from current members alone "
            "silently excludes everything that has since left the index."
        ),
        semantic_tags=["temporal", "universe_management"],
        valid_range=None,
        valid_values=None,
        example_value="2023-06-20",
        gotchas=[
            "The point-in-time predicate uses a half-open interval: a symbol is a "
            "member on date D iff date_added <= D AND (date_removed IS NULL OR "
            "date_removed > D). The removal date itself is the first non-member day.",
        ],
        statsapi_equivalent=None,
        yfinance_source_field=None,
        deprecated_in_year=None,
    ),
    ColumnSpec(
        name="source",
        type="STRING",
        mode="REQUIRED",
        short_description="Provenance of this membership spell.",
        business_definition=(
            "Identifies how this spell was derived. Currently always 'wikipedia' "
            "(reconstructed from the Wikipedia constituent list + dated changes "
            "table). Reserved for future provenance values if a second source is "
            "added for cross-validation."
        ),
        semantic_tags=["audit", "provenance"],
        valid_range=None,
        valid_values=["wikipedia"],
        example_value="wikipedia",
        gotchas=[
            "Reconstruction reliability degrades before the changes-table window "
            "(~2019); see the table description for the documented confidence bound.",
        ],
        statsapi_equivalent=None,
        yfinance_source_field=None,
        deprecated_in_year=None,
    ),
]
