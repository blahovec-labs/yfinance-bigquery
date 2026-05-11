"""Semantic groupings of OHLCV columns. Used by renderers to organize output.

Every key must correspond to at least one ``semantic_tags[0]`` value in
``OHLCV_SCHEMA``. Keys not present as a first tag are never rendered.
"""

from __future__ import annotations

from typing import Final

from yfinance_bigquery.schema import OHLCV_SCHEMA, ColumnSpec

SEMANTIC_GROUPS: Final[dict[str, str]] = {
    "identifier": "Symbol identifier and bar interval discriminator",
    "temporal": "Time fields (UTC, ET, trading session date, partition key)",
    "price": "OHLC price fields (unadjusted) + split/dividend-adjusted close",
    "volume": "Trading volume",
    "corporate_action": "Dividend payments and stock splits",
    "audit": "Ingestion bookkeeping (pipeline provenance)",
}


def columns_in_group(group: str) -> list[ColumnSpec]:
    """Return all OHLCV_SCHEMA entries whose first semantic tag is ``group``."""
    return [c for c in OHLCV_SCHEMA if c.semantic_tags and c.semantic_tags[0] == group]
