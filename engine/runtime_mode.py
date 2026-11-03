"""Canonical runtime modes with compatibility aliases for the legacy UI and state."""

from __future__ import annotations

from enum import Enum


class RuntimeMode(str, Enum):
    REPLAY = "REPLAY"
    LIVE_PAPER = "LIVE_PAPER"
    LIVE_REAL = "LIVE_REAL"

    @classmethod
    def parse(cls, value: object) -> "RuntimeMode":
        normalized = str(value or "").strip().upper()
        aliases = {
            "": cls.REPLAY,
            "HISTORICAL": cls.REPLAY,
            "BACKTEST": cls.REPLAY,
            "PAPER": cls.LIVE_PAPER,
            "MANUAL": cls.LIVE_PAPER,
            "REAL": cls.LIVE_REAL,
            "LIVE": cls.LIVE_REAL,
        }
        if normalized in aliases:
            return aliases[normalized]
        try:
            return cls(normalized)
        except ValueError as exc:
            raise ValueError(
                f"Unsupported runtime mode {value!r}; expected REPLAY, LIVE_PAPER, or LIVE_REAL"
            ) from exc

    @property
    def legacy_scanner_mode(self) -> str:
        return {
            RuntimeMode.REPLAY: "HISTORICAL",
            RuntimeMode.LIVE_PAPER: "PAPER",
            RuntimeMode.LIVE_REAL: "REAL",
        }[self]

    @property
    def uses_live_market_data(self) -> bool:
        return self is not RuntimeMode.REPLAY

    @property
    def permits_live_orders(self) -> bool:
        return self is RuntimeMode.LIVE_REAL

