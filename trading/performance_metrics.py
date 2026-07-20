"""Shared performance metric helpers."""

from __future__ import annotations

import math
from typing import Any


TRADING_DAYS_PER_YEAR = 252
SHARPE_YEARLY_FACTOR = math.sqrt(TRADING_DAYS_PER_YEAR)


def finite_float(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return result if math.isfinite(result) else default


def annualize_sharpe(daily_sharpe: Any) -> float:
    return finite_float(daily_sharpe) * SHARPE_YEARLY_FACTOR


def format_sharpe_pair(daily_sharpe: Any, yearly_sharpe: Any | None = None) -> str:
    daily = finite_float(daily_sharpe)
    yearly = annualize_sharpe(daily) if yearly_sharpe is None else finite_float(yearly_sharpe)
    return f"{daily:.2f} / {yearly:.2f}"
