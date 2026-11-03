"""Regime- and trend-aware entry context gate (2026-10-08 user request).

One pure decision used by every lane (QP, S1, UTS) on every timeframe, live and in
replay, so a signal does not fire into the wrong market: against a clear trend, in a
dead or range-bound tape without help from the other indices, or into clearly opposing
PCR / OI intel.

Two independent switches:
  ctx_gate_mode            'shadow' (default) | 'enforce' | 'off'  -- the regime table
  ctx_peer_alignment_mode  'enforce' (default) | 'shadow' | 'off'  -- the user's rule:
      MIDCPNIFTY 15m in a choppy / range-bound context needs >= 2 of the other 3
      indices moving the same way.
Shadow decisions are recorded on the candidate but never refuse an entry. The 2026-10-08
payoff audit found no single filter (volume, ADX, regime) with a robust edge on 20
sessions, so the regime table stays in shadow until replay and live shadow evidence
support enforcing it.

Missing data never silently passes a REQUIRED leg; optional legs (PCR, OI, volume when
the table does not require it) only veto when present and clearly opposing.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

import math

INDICES = ("NIFTY", "BANKNIFTY", "SENSEX", "MIDCPNIFTY")
TREND_UP = {"TRENDING_UP", "BREAKOUT_UP"}
TREND_DOWN = {"TRENDING_DOWN", "BREAKOUT_DOWN"}
RANGE_LIKE = {"RANGING", "CHOPPY", "MEAN_REVERTING", "SIDEWAYS"}
VOLATILE_LIKE = {"VOLATILE", "HIGH_VOLATILITY"}
TF_MINUTES = {"1min": 1, "5min": 5, "15min": 15}


def _g(s, name: str, default: float) -> float:
    try:
        value = getattr(s, name, default) if s is not None else default
        value = float(default if value is None else value)
        return value if math.isfinite(value) else float(default)
    except (TypeError, ValueError):
        return float(default)


def _mode(s, name: str, default: str) -> str:
    value = str(getattr(s, name, default) if s is not None else default or default).strip().lower()
    return value if value in {"shadow", "enforce", "off"} else default


def _csv(s, name: str, default: str) -> set:
    raw = getattr(s, name, default) if s is not None else default
    return {item.strip().upper() if "min" not in item.lower() else item.strip().lower()
            for item in str(raw or "").split(",") if item.strip()}


def _side(direction: str) -> int:
    d = str(direction or "").upper()
    return 1 if d in {"LONG", "BUY"} else -1 if d in {"SHORT", "SELL"} else 0


def _intel_read(value) -> str:
    if isinstance(value, dict):
        if value.get("available") is False:
            return ""
        value = value.get("contrarian_signal") or value.get("signal") or ""
    return str(value or "").upper()


@dataclass
class ContextInputs:
    lane: str
    instrument: str
    timeframe: str
    direction: str
    regime: str = "UNKNOWN"
    adx: Optional[float] = None
    rsi: Optional[float] = None
    volume_ratio: Optional[float] = None
    volume_measured: bool = False
    pcr: Any = None
    oi: Any = None
    atr_pct: Optional[float] = None
    range_bound: Optional[bool] = None
    peers_aligned: Optional[int] = None
    peers_checked: int = 0


@dataclass
class ContextVerdict:
    allowed: bool
    rule_id: str
    reasons: List[str] = field(default_factory=list)
    shadow_reasons: List[str] = field(default_factory=list)
    checks: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {"allowed": self.allowed, "rule_id": self.rule_id, "reasons": list(self.reasons),
                "shadow_reasons": list(self.shadow_reasons), "checks": dict(self.checks)}


def regime_family(regime: str, direction: str) -> str:
    reg = str(regime or "UNKNOWN").upper()
    side = _side(direction)
    if reg in TREND_UP or reg in TREND_DOWN:
        with_trade = (reg in TREND_UP and side > 0) or (reg in TREND_DOWN and side < 0)
        return ("BREAKOUT" if reg.startswith("BREAKOUT") else "TREND") if with_trade else "COUNTER_TREND"
    if reg in RANGE_LIKE:
        return "RANGE"
    if reg in VOLATILE_LIKE:
        return "VOLATILE"
    return "UNKNOWN"


def _thresholds(s, family: str, timeframe: str) -> Dict[str, float]:
    tf = str(timeframe or "5min")
    min_move = {
        "1min": lambda: _g(s, "ctx_min_atr_pct_1min", 0.025),
        "5min": lambda: _g(s, "ctx_min_atr_pct_5min", 0.05),
        "15min": lambda: _g(s, "ctx_min_atr_pct_15min", 0.09),
    }.get(tf, lambda: _g(s, "ctx_min_atr_pct_5min", 0.05))()
    table = {
        "TREND": dict(adx=_g(s, "ctx_trend_min_adx", 18.0), rsi_band=_g(s, "ctx_trend_rsi_band", 2.0),
                      volume=0.0, peers=0, move=min_move),
        "BREAKOUT": dict(adx=_g(s, "ctx_breakout_min_adx", 20.0), rsi_band=_g(s, "ctx_trend_rsi_band", 2.0),
                         volume=_g(s, "ctx_breakout_min_volume", 1.0), peers=0, move=min_move),
        "RANGE": dict(adx=_g(s, "ctx_range_min_adx", 22.0), rsi_band=_g(s, "ctx_range_rsi_band", 5.0),
                      volume=0.0, peers=int(_g(s, "ctx_range_min_peers", 2)), move=min_move * 1.2),
        "VOLATILE": dict(adx=_g(s, "ctx_volatile_min_adx", 20.0), rsi_band=_g(s, "ctx_range_rsi_band", 5.0),
                         volume=0.0, peers=int(_g(s, "ctx_volatile_min_peers", 2)), move=min_move),
        "UNKNOWN": dict(adx=_g(s, "ctx_unknown_min_adx", 20.0), rsi_band=_g(s, "ctx_trend_rsi_band", 2.0),
                        volume=0.0, peers=0, move=min_move),
    }
    return table.get(family, table["UNKNOWN"])


def evaluate(inputs: ContextInputs, settings=None) -> ContextVerdict:
    s = settings
    gate_mode = _mode(s, "ctx_gate_mode", "shadow")
    peer_mode = _mode(s, "ctx_peer_alignment_mode", "enforce")
    side = _side(inputs.direction)
    family = regime_family(inputs.regime, inputs.direction)
    checks: Dict[str, Any] = {
        "lane": str(inputs.lane or ""), "family": family, "regime": str(inputs.regime or ""),
        "adx": inputs.adx, "rsi": inputs.rsi, "volume_ratio": inputs.volume_ratio,
        "volume_measured": bool(inputs.volume_measured), "atr_pct": inputs.atr_pct,
        "range_bound": inputs.range_bound, "peers_aligned": inputs.peers_aligned,
        "peers_checked": inputs.peers_checked, "pcr": _intel_read(inputs.pcr), "oi": _intel_read(inputs.oi),
        "gate_mode": gate_mode, "peer_mode": peer_mode,
    }
    table_fail: List[str] = []
    peer_fail: List[str] = []

    if gate_mode != "off" and side != 0:
        if family == "COUNTER_TREND":
            table_fail.append(f"counter-trend: {inputs.direction} against {inputs.regime}")
        th = _thresholds(s, family if family != "COUNTER_TREND" else "UNKNOWN", inputs.timeframe)
        checks["thresholds"] = th
        if inputs.adx is None or not math.isfinite(float(inputs.adx)):
            table_fail.append("ADX unavailable")
        elif float(inputs.adx) < th["adx"]:
            table_fail.append(f"ADX {float(inputs.adx):.1f} < {th['adx']:.0f} ({family})")
        if inputs.rsi is not None and math.isfinite(float(inputs.rsi)):
            rsi = float(inputs.rsi)
            if (side > 0 and rsi < 50.0 + th["rsi_band"]) or (side < 0 and rsi > 50.0 - th["rsi_band"]):
                table_fail.append(f"RSI {rsi:.1f} not on the trade side (band {th['rsi_band']:.0f})")
        if th["volume"] > 0:
            if not inputs.volume_measured or inputs.volume_ratio is None:
                table_fail.append("volume unmeasured")
            elif float(inputs.volume_ratio) < th["volume"]:
                table_fail.append(f"volume {float(inputs.volume_ratio):.2f}x < {th['volume']:.2f}x")
        if inputs.atr_pct is not None and math.isfinite(float(inputs.atr_pct)) and float(inputs.atr_pct) < th["move"]:
            table_fail.append(f"average move {float(inputs.atr_pct):.3f}% < {th['move']:.3f}% (dead tape)")
        if th["peers"] > 0:
            if inputs.peers_aligned is None:
                table_fail.append("peer alignment unavailable")
            elif int(inputs.peers_aligned) < th["peers"]:
                table_fail.append(f"{int(inputs.peers_aligned)}/{inputs.peers_checked} peers aligned < {int(th['peers'])} ({family})")
        for name, read in (("PCR", checks["pcr"]), ("OI", checks["oi"])):
            opposing = {"BEARISH", "STRONG_BEARISH"} if side > 0 else {"BULLISH", "STRONG_BULLISH"}
            if read in opposing and (family in {"VOLATILE", "RANGE", "COUNTER_TREND"} or read.startswith("STRONG")):
                table_fail.append(f"{name} intel {read} opposes {inputs.direction}")

    if peer_mode != "off" and side != 0:
        required = _csv(s, "ctx_peer_alignment_required_indices", "MIDCPNIFTY")
        timeframes = _csv(s, "ctx_peer_alignment_timeframes", "15min")
        if str(inputs.instrument or "").upper().split()[0] in required and str(inputs.timeframe or "") in timeframes:
            # "Choppy and within range" (user): a range/volatile/counter-trend regime or a
            # weak ADX; an UNKNOWN regime counts only while price is inside its recent
            # range. A clear trend/breakout with ADX at threshold is never choppy, even
            # inside the 12-bar box (true on ~79% of MIDCPNIFTY 15m bars; review 2026-10-09).
            weak_adx = inputs.adx is not None and float(inputs.adx) < _g(s, "ctx_trend_min_adx", 18.0)
            choppy = (family in {"RANGE", "VOLATILE", "COUNTER_TREND"} or weak_adx
                      or (family == "UNKNOWN" and bool(inputs.range_bound)))
            checks["peer_rule_applies"] = choppy
            minimum = int(_g(s, "ctx_peer_alignment_min", 2))
            if choppy:
                if inputs.peers_aligned is None:
                    peer_fail.append("peer alignment unavailable (choppy context)")
                elif int(inputs.peers_aligned) < minimum:
                    peer_fail.append(
                        f"{inputs.instrument} {inputs.timeframe} choppy: {int(inputs.peers_aligned)}/"
                        f"{inputs.peers_checked} other indices aligned < {minimum}")

    enforced = (table_fail if gate_mode == "enforce" else []) + (peer_fail if peer_mode == "enforce" else [])
    shadow = (table_fail if gate_mode == "shadow" else []) + (peer_fail if peer_mode == "shadow" else [])
    rule_id = ("PEER_ALIGNMENT" if peer_fail else "") or (f"CONTEXT_{family}" if table_fail else "CONTEXT_OK")
    checks["table_failures"] = list(table_fail)
    checks["peer_failures"] = list(peer_fail)
    return ContextVerdict(not enforced, rule_id, enforced, shadow, checks)


# --------------------------------------------------------------------------- inputs
CandleProvider = Callable[[str, str], Any]


def _closed_frame(provider: CandleProvider, instrument: str, timeframe: str, ts) -> Any:
    """Bars whose CLOSE is at or before ``ts`` (no look-ahead)."""
    import pandas as pd

    frame = provider(instrument, timeframe)
    if frame is None or len(frame) == 0:
        return None
    minutes = TF_MINUTES.get(str(timeframe), 5)
    index = frame.index.tz_localize(None) if getattr(frame.index, "tz", None) is not None else frame.index
    stamp = pd.Timestamp(ts)
    stamp = stamp.tz_convert("Asia/Kolkata").tz_localize(None) if stamp.tzinfo else stamp
    keep = (index + pd.Timedelta(minutes=minutes)) <= stamp
    tail = frame[keep]
    return tail.iloc[-80:] if len(tail) else None


def market_measures(provider: CandleProvider, instrument: str, timeframe: str, ts, direction: str,
                    range_bars: int = 12) -> Dict[str, Any]:
    """ATR% of price, range-bound flag and peer-index alignment from CLOSED bars."""
    out: Dict[str, Any] = {"atr_pct": None, "range_bound": None, "peers_aligned": None, "peers_checked": 0}
    side = _side(direction)
    try:
        frame = _closed_frame(provider, instrument, timeframe, ts)
        if frame is not None and len(frame) >= 15:
            high, low, close = (frame[c].astype(float) for c in ("high", "low", "close"))
            prev = close.shift(1)
            tr = (high - low).combine((high - prev).abs(), max).combine((low - prev).abs(), max)
            atr = float(tr.iloc[-14:].mean())
            last = float(close.iloc[-1])
            out["atr_pct"] = atr / last * 100.0 if last > 0 else None
            if len(frame) > range_bars + 1:
                box_high = float(high.iloc[-range_bars - 1:-1].max())
                box_low = float(low.iloc[-range_bars - 1:-1].min())
                out["range_bound"] = box_low < last < box_high
    except Exception:
        pass
    if side == 0:
        return out
    base = str(instrument or "").upper().split()[0]
    aligned = checked = 0
    for peer in INDICES:
        if peer == base:
            continue
        try:
            frame = _closed_frame(provider, peer, timeframe, ts)
            if frame is None or len(frame) < 22:
                continue
            close = frame["close"].astype(float)
            ema21 = float(close.ewm(span=21, adjust=False).mean().iloc[-1])
            bar_move = float(close.iloc[-1]) - float(frame["open"].astype(float).iloc[-1])
            checked += 1
            if bar_move * side > 0 and (float(close.iloc[-1]) - ema21) * side > 0:
                aligned += 1
        except Exception:
            continue
    if checked:
        out["peers_aligned"], out["peers_checked"] = aligned, checked
    return out
