"""Generate Stooq-compatible AAPL 2024 fixture parquet for offline tests. Run manually.

Originally planned to use pandas_datareader Stooq reader, but:
  - pandas_datareader 0.10 is broken on Python 3.13 (uses removed distutils)
  - Stooq now requires an API key for CSV downloads

Workaround: download via yfinance (same underlying data source) and reshape to
Stooq column format (Open/High/Low/Close/Volume, DatetimeIndex named 'Date').
The fixture is used by tests that mock _fetch_stooq; it documents what shape
the real Stooq reader would return so the mock is accurate.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import yfinance as yf

print("Fetching AAPL 2024 via yfinance (Stooq-compatible format)...")
df: pd.DataFrame = yf.download(  # type: ignore[assignment]
    tickers="AAPL",
    interval="1d",
    start="2024-01-01",
    end="2024-12-31",
    auto_adjust=False,
    progress=False,
    threads=False,
)

# Flatten MultiIndex columns (yfinance uses (field, ticker) tuples)
df.columns = [col[0] if isinstance(col, tuple) else col for col in df.columns]  # type: ignore[assignment]

# Shape to match Stooq output: Open/High/Low/Close/Volume, DatetimeIndex named 'Date'
stooq_df = df[["Open", "High", "Low", "Close", "Volume"]].copy()
stooq_df.index.name = "Date"

out = Path(__file__).parent / "stooq_aapl_2024.parquet"
stooq_df.to_parquet(out)
print(f"Saved {len(stooq_df)} rows to {out}")
print(f"Date range: {stooq_df.index.min()} to {stooq_df.index.max()}")
print(f"Columns: {list(stooq_df.columns)}")
