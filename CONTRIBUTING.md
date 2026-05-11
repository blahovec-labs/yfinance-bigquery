# Contributing

Bug reports + small PRs welcome. Please open an issue before large changes.

This is a hobbyist project; review cadence is best-effort.

## Dev setup

    uv venv
    uv sync --extra dev
    pytest

## Style

Ruff + pyright. Run `ruff check yfinance_bigquery tests && pyright yfinance_bigquery tests` before opening a PR.
