"""Position sizing helpers shared by live and replay signal construction.

Default policy (operator-requested):
  * Use the configured lot count from settings/dashboard (user_lots / user_lots_fut).
  * Stops come from settings (futures_sl_pct / options_sl_pct), not lot-budget math.
  * Daily risk_fut_pct / risk_opt_pct are circuit-breaker budgets only.
  * Auto risk-budget lot reduction is OFF unless auto_risk_sizing_enabled=True.
  * Exception: cascade / high-conviction setups may multiply the base lot count.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any


@dataclass(frozen=True)
class PositionSizeResult:
    lots: int
    reason: str = ""
    risk_per_lot: float = 0.0
    risk_budget: float = 0.0
    mode: str = "fixed"  # fixed | cascade | risk_budget


# Margin warnings fire once per (label, inst_type, lots) per process — the
# sizer is evaluated for every candidate row every cycle, which produced 74
# WARNING lines for ~4 accepted trades (2026-07-10 live audit). Repeats log at
# DEBUG; the accepted trade's reasons list still carries the scale note.
_LOT_SCALE_WARNED: set = set()


def _setting_enabled(value, default: bool = True) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return value != 0
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "y", "on"}:
        return True
    if text in {"0", "false", "no", "n", "off", ""}:
        return False
    return bool(value)


def lots_from_risk_budget(
    *,
    requested_lots: int,
    lot_size: int,
    risk_budget: float,
    per_unit_risk: float,
    cap_lots: int | None = None,
) -> PositionSizeResult:
    """Optional risk-budget sizer (only used when auto_risk_sizing_enabled=True)."""
    requested = max(0, int(requested_lots or 0))
    lot_size = max(1, int(lot_size or 1))
    budget = max(0.0, float(risk_budget or 0.0))
    per_unit = max(0.0, float(per_unit_risk or 0.0))
    risk_per_lot = per_unit * lot_size
    if requested <= 0:
        return PositionSizeResult(0, "No lots requested", risk_per_lot, budget, "risk_budget")
    if budget <= 0 or risk_per_lot <= 0:
        return PositionSizeResult(
            0, "Invalid risk budget or stop distance", risk_per_lot, budget, "risk_budget"
        )
    allowed_by_risk = int(math.floor(budget / risk_per_lot))
    if allowed_by_risk < 1:
        return PositionSizeResult(
            0,
            f"One lot risk {risk_per_lot:.2f} exceeds budget {budget:.2f}",
            risk_per_lot,
            budget,
            "risk_budget",
        )
    allowed = min(requested, allowed_by_risk)
    if cap_lots is not None:
        allowed = min(allowed, max(0, int(cap_lots or 0)))
    if allowed < 1:
        return PositionSizeResult(
            0, "Position cap leaves no valid lots", risk_per_lot, budget, "risk_budget"
        )
    return PositionSizeResult(allowed, "", risk_per_lot, budget, "risk_budget")


def per_trade_risk_budget(
    settings: Any,
    inst_type: str,
    *,
    quality_multiplier: float = 1.0,
) -> float:
    """Optional per-trade ₹ budget when auto risk sizing is enabled.

    Daily segment % remains the circuit-breaker ceiling; this is NOT used when
    auto_risk_sizing_enabled is False (the default).
    """
    segment = str(inst_type or "FUT").upper()
    is_opt = segment == "OPT"
    capital = float(
        getattr(settings, "capital_opt" if is_opt else "capital_fut", 0.0) or 0.0
    )
    daily_pct = float(
        getattr(settings, "risk_opt_pct" if is_opt else "risk_fut_pct", 0.0) or 0.0
    )
    daily_budget = max(0.0, capital * daily_pct / 100.0)

    explicit_pct = float(
        getattr(
            settings,
            "risk_per_trade_opt_pct" if is_opt else "risk_per_trade_fut_pct",
            0.0,
        )
        or 0.0
    )
    if explicit_pct > 0:
        budget = capital * explicit_pct / 100.0
    else:
        fraction = float(getattr(settings, "risk_per_trade_daily_fraction", 0.5) or 0.5)
        fraction = min(1.0, max(0.05, fraction))
        budget = daily_budget * fraction

    q = min(1.0, max(0.1, float(quality_multiplier or 1.0)))
    budget = budget * q

    # Soft advisory only when auto-sizing: never force below daily budget
    # unless max_trade_loss_abs is intentionally smaller.
    try:
        hard_cap = float(settings.effective_trade_loss_cap(segment))
    except Exception:
        hard_cap = float(getattr(settings, "max_trade_loss_abs", 0.0) or 0.0)
        if hard_cap <= 0:
            hard_cap = budget
        else:
            hard_cap = min(hard_cap, daily_budget) if daily_budget > 0 else hard_cap
    return min(max(0.0, budget), max(0.0, hard_cap))


def resolve_trade_lots(
    *,
    settings: Any,
    inst_type: str,
    requested_lots: int,
    lot_size: int,
    entry_price: float = 0.0,
    stop_price: float = 0.0,
    quality_multiplier: float = 1.0,
    cap_lots: int | None = None,
    cascade_scale: bool = False,
    heavy_trend_scale: bool = False,
    high_conviction: bool = False,
    compression_state: str = "",
) -> PositionSizeResult:
    """Canonical lot resolution for live + historical.

    Default (auto_risk_sizing_enabled=False):
      1× configured lots always, except:
        - cascade_scale: OPT 2×–3× max, FUT 1×
        - heavy_trend_scale (non-cascade 2/4 align): OPT 2× only, FUT 1×
      high_conviction (A+/power_move) never multiplies.
    Optional auto mode:
      risk-budget clamp of requested lots (legacy research path).
    """
    base = max(0, int(requested_lots or 0))
    lot_size = max(1, int(lot_size or 1))
    per_unit = abs(float(entry_price or 0.0) - float(stop_price or 0.0))
    risk_per_lot = per_unit * lot_size

    # Final safety invariant: a quiet locked session is never enlarged by a
    # caller accidentally carrying cascade/heavy flags.  This intentionally
    # overrides even a user-configured base size until breakout confirmation.
    compression = str(compression_state or "").upper()
    if compression in {"FORMING", "LOCKED", "BREAKOUT_WATCH", "FAILED_BREAKOUT", "REVERSION_LOCKED"}:
        return PositionSizeResult(1 if base > 0 else 0, f"Compression cap: {compression} -> 1 lot", risk_per_lot, 0.0, "compression_cap")

    # Futures discipline (2026-07-10, operator rule): FUT is ALWAYS capped at
    # fut_lots_hard_cap (default 1 lot) regardless of user lot settings.
    # Options keep user sizing. Applies to live AND historical for parity.
    if str(inst_type or "").upper() == "FUT":
        fut_cap = max(1, int(getattr(settings, "fut_lots_hard_cap", 1) or 1))
        if base > fut_cap:
            base = fut_cap

    if base < 1:
        return PositionSizeResult(0, "No lots requested", risk_per_lot, 0.0, "fixed")

    def _apply_scale(
        lots_in: int,
        *,
        mult: float,
        mode: str,
        label: str,
        max_lots: int = 0,
        warn: bool = False,
    ) -> PositionSizeResult:
        from engine.loss_guard_policy import apply_cascade_lot_scale

        scaled, note = apply_cascade_lot_scale(
            lots_in, mult=mult, max_lots=max_lots, inst_type=inst_type
        )
        active = mult > 1.0 + 1e-9 or scaled != lots_in
        out_mode = mode if active else "fixed"
        if active and warn and _setting_enabled(
            getattr(settings, "cascade_lot_scale_warn", True), True
        ):
            try:
                from loguru import logger

                warn_key = (str(label), str(inst_type or "").upper(), int(scaled))
                if warn_key not in _LOT_SCALE_WARNED:
                    _LOT_SCALE_WARNED.add(warn_key)
                    logger.warning(
                        f"{label}: {note} — ensure margin covers {scaled} lots"
                    )
                else:
                    logger.debug(f"{label}: {note} ({scaled} lots)")
            except Exception:
                pass
        return PositionSizeResult(
            scaled,
            f"{label}: {note}" if active else f"Fixed lots={scaled} (1× settings)",
            risk_per_lot,
            0.0,
            out_mode,
        )

    def _maybe_scale(lots_in: int) -> PositionSizeResult:
        # Cascade wins over heavy-trend when both true.
        if cascade_scale and _setting_enabled(
            getattr(settings, "cascade_lot_scale_enabled", True), True
        ):
            from engine.loss_guard_policy import (
                cascade_max_lots_for_inst,
                cascade_multiplier_for_inst,
            )

            mult = cascade_multiplier_for_inst(settings, inst_type)
            max_lots = cascade_max_lots_for_inst(settings, inst_type)
            return _apply_scale(
                lots_in,
                mult=mult,
                mode="cascade",
                label="CASCADE LOT SCALE",
                max_lots=max_lots,
                warn=True,
            )
        if heavy_trend_scale and _setting_enabled(
            getattr(settings, "heavy_trend_lot_scale_enabled", True), True
        ):
            from engine.loss_guard_policy import heavy_trend_multiplier_for_inst

            mult = heavy_trend_multiplier_for_inst(settings, inst_type)
            return _apply_scale(
                lots_in,
                mult=mult,
                mode="heavy_trend",
                label="HEAVY-TREND LOT SCALE",
                max_lots=0,
                warn=True,
            )
        # high_conviction deliberately ignored — always 1× otherwise
        _ = high_conviction
        return PositionSizeResult(
            lots_in,
            f"Fixed lots={lots_in} (settings; mode=fixed 1×)",
            risk_per_lot,
            0.0,
            "fixed",
        )

    # ── Fixed-lot policy (default) ──────────────────────────────────
    if not _setting_enabled(getattr(settings, "auto_risk_sizing_enabled", False), False):
        return _maybe_scale(base)

    # ── Optional auto risk-budget sizing ────────────────────────────
    budget = per_trade_risk_budget(
        settings, inst_type, quality_multiplier=quality_multiplier
    )
    sized = lots_from_risk_budget(
        requested_lots=base,
        lot_size=lot_size,
        risk_budget=budget,
        per_unit_risk=per_unit,
        cap_lots=cap_lots,
    )
    if sized.lots < 1:
        return sized
    scaled = _maybe_scale(sized.lots)
    if scaled.mode != "fixed":
        return PositionSizeResult(
            scaled.lots,
            f"Risk-sized then {scaled.reason}",
            sized.risk_per_lot,
            sized.risk_budget,
            scaled.mode,
        )
    return sized


def size_lots_for_trade(
    *,
    settings: Any,
    inst_type: str,
    requested_lots: int,
    lot_size: int,
    entry_price: float,
    stop_price: float,
    quality_multiplier: float = 1.0,
    cap_lots: int | None = None,
    cascade_scale: bool = False,
    heavy_trend_scale: bool = False,
    high_conviction: bool = False,
    compression_state: str = "",
) -> PositionSizeResult:
    """Back-compat alias for resolve_trade_lots."""
    return resolve_trade_lots(
        settings=settings,
        inst_type=inst_type,
        requested_lots=requested_lots,
        lot_size=lot_size,
        entry_price=entry_price,
        stop_price=stop_price,
        quality_multiplier=quality_multiplier,
        cap_lots=cap_lots,
        cascade_scale=cascade_scale,
        heavy_trend_scale=heavy_trend_scale,
        high_conviction=high_conviction,
        compression_state=compression_state,
    )
