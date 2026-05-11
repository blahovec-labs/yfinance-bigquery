"""Interval enum + per-interval config."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Final


class Interval(StrEnum):
    D1 = "1d"
    M60 = "60m"
    M15 = "15m"
    M5 = "5m"
    M1 = "1m"

    @classmethod
    def from_string(cls, s: str) -> Interval:
        for member in cls:
            if member.value == s:
                return member
        raise ValueError(f"unknown interval {s!r}; choices: {[m.value for m in cls]}")

    def table_name(self, *, prefix: str = "ohlcv") -> str:
        return f"{prefix}_{self.value}"


@dataclass(frozen=True)
class IntervalConfig:
    default_lookback_days: int
    default_chunk_by: str  # "year" | "month" | "week" | "range"
    partition_granularity: str  # "DAY" | "MONTH"
    retention_days: int | None  # None = never trim


INTERVAL_CONFIG: Final[dict[Interval, IntervalConfig]] = {
    Interval.D1: IntervalConfig(
        default_lookback_days=7,
        default_chunk_by="year",
        partition_granularity="DAY",
        retention_days=None,
    ),
    Interval.M60: IntervalConfig(
        default_lookback_days=7,
        default_chunk_by="month",
        partition_granularity="MONTH",
        retention_days=730,
    ),
    Interval.M15: IntervalConfig(
        default_lookback_days=7,
        default_chunk_by="month",
        partition_granularity="MONTH",
        retention_days=60,
    ),
    Interval.M5: IntervalConfig(
        default_lookback_days=7,
        default_chunk_by="month",
        partition_granularity="MONTH",
        retention_days=60,
    ),
    Interval.M1: IntervalConfig(
        default_lookback_days=3,
        default_chunk_by="week",
        partition_granularity="DAY",
        retention_days=7,
    ),
}
