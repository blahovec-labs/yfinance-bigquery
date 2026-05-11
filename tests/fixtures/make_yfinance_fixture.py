"""Generate yfinance fixture parquet for offline tests. Run manually."""

import pandas as pd
import yfinance as yf

result: pd.DataFrame = yf.download(  # type: ignore[assignment]
    tickers=["AAPL", "MSFT", "GOOGL"],
    interval="1d",
    start="2024-01-02",
    end="2024-01-10",
    auto_adjust=False,
    group_by="ticker",
    progress=False,
    threads=False,
)
print(f"Downloaded {len(result)} rows, columns: {list(result.columns[:6])}")
result.to_parquet("tests/fixtures/yfinance_aapl_1d.parquet")
print("Saved to tests/fixtures/yfinance_aapl_1d.parquet")
