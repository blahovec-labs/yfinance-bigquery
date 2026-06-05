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

# Expected source columns in the changes table (table index 1), after any
# MultiIndex flattening.  Wikipedia uses a 2-row header that pandas surfaces as
# MultiIndex; we join levels with a space so "('Added', 'Ticker')" becomes
# "Added Ticker".  The flat fixture uses the same names directly.
_CHANGES_REQUIRED_COLS: Final[dict[str, str]] = {
    "Date": "date",
    "Added Ticker": "added_ticker",
    "Added Security": "added_security",
    "Removed Ticker": "removed_ticker",
    "Removed Security": "removed_security",
    "Reason": "reason",
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

        # Flatten MultiIndex columns (e.g. ('Added', 'Ticker') → 'Added Ticker').
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = pd.Index(
                [" ".join(str(level) for level in col).strip() for col in df.columns]
            )

        missing = [c for c in _CHANGES_REQUIRED_COLS if c not in df.columns]
        if missing:
            raise ValueError(
                f"expected column(s) {missing} not found in Wikipedia changes table; "
                f"got {list(df.columns)}. The page structure may have changed."
            )

        out: pd.DataFrame = df[list(_CHANGES_REQUIRED_COLS)].rename(
            columns=_CHANGES_REQUIRED_COLS
        ).copy()
        out["date"] = out["date"].apply(_parse_date)
        return out

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
