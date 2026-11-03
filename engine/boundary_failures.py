"""Typed fail-closed outcomes at analysis and execution boundaries."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class BoundaryAbstention:
    boundary: str
    reason: str
    failure_type: str
    retryable: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": "ABSTAIN",
            "boundary": self.boundary,
            "reason": self.reason,
            "failure_type": self.failure_type,
            "retryable": self.retryable,
        }


class BoundaryFailure(RuntimeError):
    boundary = "unknown"

    def __init__(self, reason: str, *, cause: Exception | None = None, retryable: bool = False):
        super().__init__(reason)
        self.cause = cause
        self.retryable = retryable

    def abstention(self) -> BoundaryAbstention:
        return BoundaryAbstention(
            boundary=self.boundary,
            reason=str(self),
            failure_type=type(self).__name__,
            retryable=self.retryable,
        )


class AnalysisBoundaryFailure(BoundaryFailure):
    boundary = "entry_analysis"


class ExecutionBoundaryFailure(BoundaryFailure):
    boundary = "execution"
