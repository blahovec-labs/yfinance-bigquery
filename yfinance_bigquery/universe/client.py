"""WikipediaUniverseClient: scrape S&P 500 constituents from Wikipedia."""

from __future__ import annotations

import logging
from datetime import date
from typing import Final

import pandas as pd

log = logging.getLogger(__name__)

WIKI_URL: Final[str] = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"

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
        tables = pd.read_html(WIKI_URL)
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
