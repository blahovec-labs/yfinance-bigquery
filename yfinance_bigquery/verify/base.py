"""Verifier base classes: Comparison, VerificationResult, Verifier protocol."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Protocol


@dataclass(frozen=True)
class Comparison:
    """One row of comparison output: our value vs the external source's value."""

    entity_id: int | str
    entity_name: str
    ours: float
    expected: float
    diff: float                  # ours - expected
    sample_size: int
    within_tolerance: bool


@dataclass(frozen=True)
class VerificationResult:
    """Aggregate result of running a Verifier."""

    metric: str
    season: int
    aggregation: str             # "symbol-season"
    source: str                  # "internal" | "stooq"
    tolerance: float
    total_compared: int
    within_tolerance_count: int
    deltas: list[Comparison] = field(default_factory=list)

    @property
    def pct_within_tolerance(self) -> float:
        if self.total_compared == 0:
            return 0.0
        return self.within_tolerance_count / self.total_compared

    def passed(self, threshold: float = 0.99) -> bool:
        return self.pct_within_tolerance >= threshold

    def summary(self) -> str:
        lines = [
            f"Verifying {self.metric} against {self.source} for {self.season}",
            f"  Aggregation: {self.aggregation}",
            f"  Tolerance: ±{self.tolerance}",
            "",
            f"Compared {self.total_compared} entities",
            f"  Within tolerance: {self.within_tolerance_count}"
            f" ({self.pct_within_tolerance:.1%})",
            f"  Outside tolerance: {self.total_compared - self.within_tolerance_count}",
        ]
        outside = [d for d in self.deltas if not d.within_tolerance]
        if outside:
            lines.append("\nTop deltas (outside tolerance):")
            for d in sorted(outside, key=lambda x: abs(x.diff), reverse=True)[:5]:
                lines.append(
                    f"  {d.entity_name:<25}  ours={d.ours:.3f}  "
                    f"expected={d.expected:.3f}  diff={d.diff:+.3f}  "
                    f"(n={d.sample_size})"
                )
        return "\n".join(lines)

    def to_json(self) -> dict[str, Any]:
        return {
            **{k: v for k, v in asdict(self).items() if k != "deltas"},
            "pct_within_tolerance": self.pct_within_tolerance,
            "deltas": [asdict(d) for d in self.deltas],
        }


class Verifier(Protocol):
    def run(self) -> VerificationResult:
        ...
