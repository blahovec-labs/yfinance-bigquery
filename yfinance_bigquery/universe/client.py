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


class WikipediaUniverseClient:
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
