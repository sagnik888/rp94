"""Typed scanner settings views over the legacy flat Settings model.

Flat fields remain authoritative during migration, so existing .env names, dashboard
payloads, and callers keep working while subsystem code receives small typed objects.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
import os
from pathlib import Path
from typing import Any

import dotenv


@lru_cache(maxsize=1)
def _legacy_env() -> dict[str, Any]:
    """Read retired flat names during the compatibility window."""
    path = Path(".env")
    return dict(dotenv.dotenv_values(path)) if path.exists() else {}


def _value(settings: Any, name: str, default: Any) -> Any:
    if hasattr(settings, name):
        return getattr(settings, name)
    env_name = name.upper()
    return os.environ.get(env_name, _legacy_env().get(env_name, default))


def _number(settings: Any, name: str, default: float, minimum: float) -> float:
    try:
        raw = _value(settings, name, default)
        value = default if raw is None or raw == "" else raw
        return max(minimum, float(value))
    except (TypeError, ValueError):
        return max(minimum, float(default))


def _integer(settings: Any, name: str, default: int, minimum: int) -> int:
    try:
        raw = _value(settings, name, default)
        value = default if raw is None or raw == "" else raw
        return max(minimum, int(value))
    except (TypeError, ValueError):
        return max(minimum, int(default))


def _boolean(settings: Any, name: str, default: bool) -> bool:
    raw = _value(settings, name, default)
    if isinstance(raw, bool):
        return raw
    text = str(raw or "").strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off", ""}:
        return False
    return bool(default)


@dataclass(frozen=True)
class LiveScanSettings:
    index_deadline_seconds: float
    event_driven: bool
    intracandle_min_interval_seconds: float


@dataclass(frozen=True)
class ExternalCallSettings:
    deadline_seconds: float
    failure_threshold: int
    recovery_seconds: float
    max_inflight_per_route: int


@dataclass(frozen=True)
class RobustnessSettings:
    max_drawdown_pct: float
    max_single_day_profit_concentration: float
    min_positive_unseen_period_ratio: float
    min_index_profit_factor: float
    min_pair_coverage: float


@dataclass(frozen=True)
class ScannerRuntimeSettings:
    live_scan: LiveScanSettings
    external_calls: ExternalCallSettings
    robustness: RobustnessSettings

    @classmethod
    def from_flat(cls, settings: Any) -> "ScannerRuntimeSettings":
        return cls(
            live_scan=LiveScanSettings(
                index_deadline_seconds=_number(settings, "live_index_timeout_seconds", 9.0, 0.05),
                event_driven=_boolean(settings, "event_driven_scan_enabled", True),
                intracandle_min_interval_seconds=_number(
                    settings, "intracandle_analysis_min_interval_seconds", 2.0, 0.0
                ),
            ),
            external_calls=ExternalCallSettings(
                deadline_seconds=_number(settings, "external_call_timeout_seconds", 8.0, 0.05),
                failure_threshold=_integer(settings, "external_circuit_breaker_failures", 2, 1),
                recovery_seconds=_number(
                    settings, "external_circuit_breaker_recovery_seconds", 60.0, 0.1
                ),
                max_inflight_per_route=_integer(
                    settings, "external_call_max_inflight_per_route", 1, 1
                ),
            ),
            robustness=RobustnessSettings(
                max_drawdown_pct=_number(settings, "evaluation_max_drawdown_pct", 20.0, 0.0),
                max_single_day_profit_concentration=_number(
                    settings, "evaluation_max_single_day_profit_concentration", 0.35, 0.0
                ),
                min_positive_unseen_period_ratio=_number(
                    settings, "evaluation_min_positive_unseen_period_ratio", 0.55, 0.0
                ),
                min_index_profit_factor=_number(
                    settings, "evaluation_min_index_profit_factor", 1.0, 0.0
                ),
                min_pair_coverage=_number(settings, "evaluation_min_pair_coverage", 0.95, 0.0),
            ),
        )
