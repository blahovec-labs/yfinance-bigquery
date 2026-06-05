"""WikipediaUniverseClient: scrape S&P 500 constituents from Wikipedia."""

from __future__ import annotations

import logging
from datetime import date
from io import StringIO
from typing import Final

import pandas as pd
import requests

log = logging.getLogger(__name__)

WIKI_URL: Final[str] = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"

# Wikipedia requires a descriptive User-Agent for programmatic access; default
# urllib UA is blocked with HTTP 403.
_USER_AGENT: Final[str] = (
    "yfinance-bigquery/0.1.0 "
    "(https://github.com/blahovec-labs/yfinance-bigquery)"
)

_REQUIRED_COLS: Final[dict[str, str]] = {
    "Symbol": "symbol",
    "Security": "name",
    "GICS Sector": "sector",
    "GICS Sub-Industry": "industry",
    "Date added": "date_added",
}

# Output column -> accepted source header(s) in the changes table (table index 1),
# after MultiIndex flattening. Wikipedia uses a 2-row header that pandas surfaces
# as a MultiIndex; rowspan'd cells (date + reason) repeat the label on both levels
# ("Effective Date" / "Effective Date"), so we dedupe identical levels before
# matching. The date header has historically been both "Date" and "Effective Date"
# — accept either.
_CHANGES_COL_ALIASES: Final[dict[str, tuple[str, ...]]] = {
    "date": ("Effective Date", "Date"),
    "added_ticker": ("Added Ticker",),
    "added_security": ("Added Security",),
    "removed_ticker": ("Removed Ticker",),
    "removed_security": ("Removed Security",),
    "reason": ("Reason",),
}


class WikipediaUniverseClient:
    def fetch_changes(self) -> pd.DataFrame:
        """Return a DataFrame with columns [date, added_ticker, added_security,
        removed_ticker, removed_security, reason] sourced from Wikipedia's
        "Selected changes to the list of S&P 500 companies" table (table index 1).

        Handles both flat and MultiIndex column headers (pandas.read_html returns
        a MultiIndex when Wikipedia uses a 2-row header row).  Raises ValueError
        if the expected source columns are absent so callers fail loudly rather
        than silently mis-parse a page structure change.
        """
        resp = requests.get(WIKI_URL, headers={"User-Agent": _USER_AGENT}, timeout=30)
        resp.raise_for_status()
        tables = pd.read_html(StringIO(resp.text))
        if len(tables) < 2:
            raise ValueError(
                f"expected at least 2 tables at {WIKI_URL}; got {len(tables)}."
            )
        df: pd.DataFrame = tables[1]
        df.columns = pd.Index(_flatten_columns(df.columns))

        # Resolve each output column to its source header via aliases.
        resolved: dict[str, str] = {}  # source_header -> output_name
        missing: list[str] = []
        for out_name, aliases in _CHANGES_COL_ALIASES.items():
            match = next((a for a in aliases if a in df.columns), None)
            if match is None:
                missing.append(f"{out_name} (one of {list(aliases)})")
            else:
                resolved[match] = out_name
        if missing:
            raise ValueError(
                f"expected column(s) {missing} not found in Wikipedia changes table; "
                f"got {list(df.columns)}. The page structure may have changed."
            )

        out: pd.DataFrame = df[list(resolved)].rename(columns=resolved).copy()
        out["date"] = out["date"].apply(_parse_date)
        return out[
            ["date", "added_ticker", "added_security",
             "removed_ticker", "removed_security", "reason"]
        ]

    def fetch_constituents(self) -> pd.DataFrame:
        """Return a DataFrame with columns [symbol, name, sector, industry, date_added].

        Raises ValueError if Wikipedia's table 0 doesn't have expected columns.
        """
        resp = requests.get(WIKI_URL, headers={"User-Agent": _USER_AGENT}, timeout=30)
        resp.raise_for_status()
        tables = pd.read_html(StringIO(resp.text))
        if not tables:
            raise ValueError(f"no tables found at {WIKI_URL}")
        df: pd.DataFrame = tables[0]
        missing = [c for c in _REQUIRED_COLS if c not in df.columns]
        if missing:
            raise ValueError(
                f"expected column(s) {missing} not found in Wikipedia table 0; "
                f"got {list(df.columns)}. The page structure may have changed."
            )
        out: pd.DataFrame = df[list(_REQUIRED_COLS)].rename(  # type: ignore[assignment]
            columns=_REQUIRED_COLS
        ).copy()
        out["date_added"] = out["date_added"].apply(_parse_date)
        return out


def _flatten_columns(columns: pd.Index) -> list[str]:
    """Flatten (possibly MultiIndex) columns, collapsing identical/empty levels.

    Wikipedia's changes table uses a 2-row header where rowspan'd cells repeat the
    label on both levels (e.g. ('Effective Date', 'Effective Date')); joining all
    levels would yield 'Effective Date Effective Date'. Dedupe identical levels and
    drop empties so ('Added','Ticker')->'Added Ticker' and ('Reason','Reason')->'Reason'.
    """
    if not isinstance(columns, pd.MultiIndex):
        return [str(c) for c in columns]
    flat: list[str] = []
    for col in columns:
        seen: list[str] = []
        for level in col:
            s = str(level).strip()
            if s and s not in seen:
                seen.append(s)
        flat.append(" ".join(seen))
    return flat


def _parse_date(s: object) -> date | None:
    if s is None:
        return None
    try:
        result = pd.isna(s)  # type: ignore[arg-type]
        if bool(result):
            return None
    except (TypeError, ValueError):
        pass
    try:
        return pd.to_datetime(str(s)).date()
    except (ValueError, TypeError):
        return None
