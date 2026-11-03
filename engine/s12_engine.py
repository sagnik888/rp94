"""S1+S2 DUAL engine (s1s2_dual): the host adapter for the user's "S1 + S2 Dual Independent
v1.2" Python port of their Pine v5.4 script, with the documented clock parity fix and
the bot's live-safe R-based runner / early-loss policy in engine/s12dual.

The package runs two independent engines on closed bars. Here they are two lanes:

    D1  the package's S1. A SETUP on a confirmed bar is an order for the NEXT bar's open.
        The host signal is stamped on the SETUP bar and the live entry happens as soon as
        that bar closes, which is the next open in practice. FILL_ESTIMATE is bookkeeping
        only and is never an entry trigger (that would be the one-bar-lag bug class).
        The index stop is re-measured from the actual entry: entry x (1 -/+ stop_pct),
        with stop_pct already clamped by the Intraday hard cap, as the package does.
    D2  the package's S2: an ENTRY at the confirmed close, with the package's own stop and
        targets.

The names "S1"/"S2" never leave this module: the host's "S1" is another lane.

Fixed for this bot (reports/s12_engine_20261010/DESIGN.md):
    - exchange-prefixed symbols with security_type='index' and tick 0.05 (without it S2
      treats an index as a stock: 0.60% burst instead of 0.25%);
    - Intraday modes, no BTST, and the engine's own EOD at 15:15
      (s1_eod_cutoff_ist_min=915, s2_force_exit_ist_decimal=15.25);
    - 'Price proxy' when volume is missing.
Frames carry only CLOSED 09:15-15:15 session bars as float64 OHLCV on a tz-aware
Asia/Kolkata index of bar opens. Volume <= 0 or with an unusable basis becomes NaN, so the
price proxy engages bar by bar, and OHLC-inconsistent rows are repaired or dropped (one bad
row makes the package raise).

update() recomputes every indicator over the whole history (110-320 ms a bar) and holds the
GIL, so the live path never computes on the scan thread. One low-priority worker thread
advances a persistent scanner per (index, timeframe, session day, config hash) when a new
bar closes, and the scan reads the last published snapshot. A new session, a config change,
a restart or a prefix mismatch triggers a full run() instead; the two are event-identical
(tests/test_s12_engine_core.py).
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import threading
import time
from collections import OrderedDict
from dataclasses import asdict, dataclass, field, replace
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from engine.s12dual.config import Config, mode_cap, tf_minutes
from engine.s12dual.engine import DualScanner, hard_stop_price, nearest_stop
from engine.s12dual.engine import targets as ladder_targets

logger = logging.getLogger(__name__)

ENGINE_KEY = "s1s2_dual"
DISPLAY_NAME = "S1+S2 Dual (Pine v5.4)"
PACKAGE_VERSION = "1.2.0"
ADAPTER_VERSION = "s12_engine.v2.dynamic_runner_early_cut"
LANES = ("D1", "D2")
LANE_OF_ENGINE = {"S1": "D1", "S2": "D2"}
EXIT_POLICIES = {"D1": "s12_d1_native", "D2": "s12_d2_native"}

# History the live feed hands the engine ahead of the session (scanner warmup). S1 is
# silent before bar 200 and EMA200 seeds from the frame; ~900 bars lets it converge.
FEED_BARS = 900
FEED_BARS_1MIN = 500

TICK_SIZE = 0.05
SECURITY_TYPE = "index"
TIMEZONE = "Asia/Kolkata"
SESSION_START = "09:15"
SESSION_END = "15:15"
_SESSION_START_MIN = 9 * 60 + 15
_SESSION_END_MIN = 15 * 60 + 15
_EXCHANGE_CLOSE_MIN = 15 * 60 + 30

SYMBOLS = {
    "NIFTY": "NSE:NIFTY",
    "BANKNIFTY": "NSE:BANKNIFTY",
    "MIDCPNIFTY": "NSE:MIDCPNIFTY",
    "SENSEX": "BSE:SENSEX",
}
_INDEX_ALIASES = {"NIFTY50": "NIFTY", "NIFTYBANK": "BANKNIFTY", "MIDCAPNIFTY": "MIDCPNIFTY"}
# Exchange display names whose first word alone would read as another index ("NIFTY BANK").
_INDEX_PHRASES = {"NIFTY BANK": "BANKNIFTY", "NIFTY MID SELECT": "MIDCPNIFTY", "NIFTY MIDCAP SELECT": "MIDCPNIFTY"}

# Host timeframe -> package timeframe; a 15m chart needs the closed 5m bars as its '5' feed.
TIMEFRAMES = {"1min": "1", "5min": "5", "15min": "15"}
AUX_TIMEFRAME = {"15min": "5min"}

# Not exposed as settings: the bot's intraday index contract (DESIGN "Fixed engine
# configuration"). enable_s1/enable_s2 stay on so an open trade keeps its engine exits when
# a lane's admission switch (s12_d1_enabled / s12_d2_enabled) is turned off.
FIXED_CONFIG = {
    "enable_s1": True,
    "enable_s2": True,
    "share_auto_mode": True,
    "block_conflicting_entries": False,
    "round_trip_cost_pct": 0.0,
    "emit_stop_updates": True,
    "no_volume_policy": "Price proxy",
    "s2_confirmed_mtf": True,
    "s1_trade_mode": "Intraday",
    "s2_trade_mode": "Intraday",
    "instrument_type": "Auto-Detect",
    "s1_force_eod": True,
    "s1_eod_cutoff_ist_min": 915,
    "hard_stop_enabled": True,
    "s1_cancel_overnight_cash": True,
    "s2_market_region": "Universal",
    "s2_enable_btst": False,
    "s2_force_exit_ist_decimal": 15.25,
}

# settings field -> (Config field, (kind, *bounds_or_choices)). Bounds follow the package's
# Pine input ranges (engine/s12dual/config.py validate()); defaults below are this bot's
# selected policy and do not alter the vendored package defaults.
SETTING_FIELDS = {
    # D1 (the package's S1)
    "s12_d1_min_score": ("min_score", ("int", 50, 90)),
    "s12_d1_conflict_margin": ("conflict_margin", ("int", 10, 40)),
    "s12_d1_min_adx": ("min_adx", ("float", 15.0, 35.0)),
    "s12_d1_use_15m_gate": ("use_15m_gate", ("bool",)),
    "s12_d1_cooldown_bars": ("s1_cooldown_bars", ("int", 0, 20)),
    "s12_d1_stop_pct": ("s1_stop_pct", ("float", 0.05, 10.0)),
    "s12_d1_exit_at_t3": ("s1_exit_at_t3", ("bool",)),
    "s12_d1_trail": ("s1_trail", ("bool",)),
    "s12_d1_trail_start_r": ("s1_trail_start_r", ("float", 1.0, 3.0)),
    "s12_d1_trail_atr": ("s1_trail_atr", ("float", 1.0, 3.0)),
    "s12_d1_lock_at_t1": ("s1_lock_at_t1", ("bool",)),
    "s12_d1_exit_confirm_bars": ("s1_exit_confirm_bars", ("int", 2, 5)),
    "s12_d1_patterns": ("s1_patterns", ("bool",)),
    "s12_d1_pattern_scoring": ("s1_pattern_scoring", ("bool",)),
    # Shared by both lanes
    "s12_hard_stop_intraday_pct": ("hard_stop_intraday_pct", ("float", 0.10, 1.50)),
    "s12_t1_r": ("t1_r", ("float", 1.5, 5.0)),
    "s12_t2_r": ("t2_r", ("float", 2.0, 6.0)),
    "s12_t3_r": ("t3_r", ("float", 2.5, 10.0)),
    "s12_share_target_ladder": ("share_target_ladder", ("bool",)),
    "s12_consecutive_loss_limit": ("consecutive_loss_limit", ("int", 2, 6)),
    "s12_circuit_pause_bars": ("circuit_pause_bars", ("int", 5, 30)),
    # D2 (the package's S2)
    "s12_d2_settings_mode": ("s2_settings_mode", ("enum", ("Auto", "Manual"))),
    "s12_d2_risk_mode": ("s2_risk_mode", ("enum", ("Dynamic", "ATR", "Percentage", "Swing"))),
    "s12_d2_min_stop_pct": ("s2_min_stop_pct", ("float", 0.0, 5.0)),
    "s12_d2_max_stop_pct": ("s2_max_stop_pct", ("float", 0.0, 15.0)),
    "s12_d2_dynamic_sensitivity": ("s2_dynamic_sensitivity", ("float", 0.5, 2.0)),
    "s12_d2_atr_stop_mult": ("s2_atr_stop_mult", ("float", 0.25, 6.0)),
    "s12_d2_percent_stop_pct": ("s2_percent_stop_pct", ("float", 0.1, 10.0)),
    "s12_d2_t1_manual": ("s2_t1_manual", ("float", 0.5, 3.0)),
    "s12_d2_t2_manual": ("s2_t2_manual", ("float", 1.0, 6.0)),
    "s12_d2_t3_manual": ("s2_t3_manual", ("float", 2.0, 10.0)),
    "s12_d2_circuit": ("s2_circuit", ("bool",)),
    "s12_d2_require_15m_alignment": ("s2_require_15m_alignment", ("bool",)),
    "s12_d2_confidence_floor": ("s2_confidence_floor", ("int", 45, 90)),
    "s12_d2_exit_confirm_input": ("s2_exit_confirm_input", ("int", 0, 5)),
    "s12_d2_gap_skip": ("s2_gap_skip", ("bool",)),
    "s12_d2_daily_trend_lock": ("s2_daily_trend_lock", ("bool",)),
    "s12_d2_max_bars_in_trade": ("s2_max_bars_in_trade", ("int", 50, 500)),
    "s12_d2_macd_entry_filter": ("s2_macd_entry_filter", ("bool",)),
    "s12_d2_signal_gap": ("s2_signal_gap", ("int", 0, 10)),
    "s12_d2_trailing": ("s2_trailing", ("bool",)),
    "s12_d2_trail_start_manual": ("s2_trail_start_manual", ("float", 0.5, 3.0)),
    "s12_d2_trail_atr_manual": ("s2_trail_atr_manual", ("float", 1.0, 5.0)),
    "s12_d2_lock_at_t1": ("s2_lock_at_t1", ("bool",)),
    "s12_d2_profit_lock_r": ("s2_profit_lock_r", ("float", 0.3, 1.5)),
    "s12_d2_aggressive_trail": ("s2_aggressive_trail", ("bool",)),
    "s12_d2_tsl_use_close": ("s2_tsl_use_close", ("bool",)),
    "s12_d2_early_be": ("s2_early_be", ("bool",)),
    "s12_d2_early_be_r": ("s2_early_be_r", ("float", 0.2, 1.4)),
    "s12_d2_exit_mode": ("s2_exit_mode", ("enum", ("Conservative", "Balanced", "Trend-Following"))),
    "s12_d2_smart_exit": ("s2_smart_exit", ("bool",)),
    "s12_d2_exit_weakness": ("s2_exit_weakness", ("bool",)),
    "s12_d2_burst": ("s2_burst", ("bool",)),
    "s12_d2_burst_bars": ("s2_burst_bars", ("int", 0, 10)),
    "s12_d2_burst_index_pct": ("s2_burst_index_pct", ("float", 0.1, 1.0)),
    "s12_d2_burst_volume_mult": ("s2_burst_volume_mult", ("float", 0.8, 2.0)),
    "s12_d2_burst_score_boost": ("s2_burst_score_boost", ("int", 10, 35)),
    "s12_d2_patterns": ("s2_patterns", ("bool",)),
    "s12_d2_orb": ("s2_orb", ("bool",)),
    "s12_d2_orb_minutes": ("s2_orb_minutes", ("int", 5, 90)),
    "s12_d2_divergence": ("s2_divergence", ("bool",)),
    "s12_d2_div_lookback": ("s2_div_lookback", ("int", 5, 30)),
    "s12_d2_div_score_boost": ("s2_div_score_boost", ("int", 5, 25)),
}


APP_CONFIG_DEFAULTS = {
    "s1_stop_pct": 0.1,
    "t1_r": 1.5,
    "t2_r": 2.25,
    "t3_r": 4.0,
    "s2_risk_mode": "Percentage",
    "s2_min_stop_pct": 0.1,
    "s2_percent_stop_pct": 0.1,
    "s2_t1_manual": 1.5,
    "s2_t2_manual": 2.25,
    "s2_t3_manual": 4.0,
}


def _package_default(config_field: str, spec: tuple):
    value = getattr(Config(), config_field)
    return int(value) if spec[0] == "int" else value


# settings field -> default (package defaults with explicit bot-policy overrides).
SETTING_DEFAULTS = {
    name: APP_CONFIG_DEFAULTS.get(config_field, _package_default(config_field, spec))
    for name, (config_field, spec) in SETTING_FIELDS.items()
}


def _settings(settings=None):
    if settings is not None:
        return settings
    try:
        from config.settings import get_settings

        return get_settings()
    except Exception:
        return None


def _coerce(raw, default, spec: tuple):
    """A settings value as the Config field expects it, clamped into the Pine bounds."""
    if raw is None:
        return default
    kind = spec[0]
    try:
        if kind == "bool":
            if isinstance(raw, str):
                return raw.strip().lower() in {"1", "true", "yes", "on"}
            return bool(raw)
        if kind == "enum":
            text = str(raw).strip()
            return text if text in spec[1] else default
        value = float(raw)
        if isinstance(raw, bool) or not math.isfinite(value):
            return default
        value = min(max(value, float(spec[1])), float(spec[2]))
        if kind == "int":
            value = int(round(value))
        return float(value) if isinstance(default, float) else value
    except (TypeError, ValueError):
        return default


def config_from_settings(settings=None) -> Config:
    """The package Config for this bot: s12_* settings plus FIXED_CONFIG."""
    s = _settings(settings)
    base = Config()
    values = asdict(base)
    for name, (config_field, spec) in SETTING_FIELDS.items():
        default = APP_CONFIG_DEFAULTS.get(config_field, getattr(base, config_field))
        raw = getattr(s, name, None) if s is not None else None
        values[config_field] = _coerce(raw, default, spec)
    values.update(FIXED_CONFIG)
    cfg = Config(**values)
    cfg.validate()
    return cfg


def config_hash(cfg: Config) -> str:
    payload = json.dumps({"adapter": ADAPTER_VERSION, "config": asdict(cfg)}, sort_keys=True, default=str)
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:16]


# ─────────────────────────────────────────────────────────────── names and clocks


def index_key(instrument: Any) -> str:
    """"NIFTY" / "BANKNIFTY" / "MIDCPNIFTY" / "SENSEX", or "" for anything else."""
    text = str(instrument or "").strip().upper()
    if ":" in text:
        text = text.split(":", 1)[1]
    text = " ".join(text.replace("-INDEX", "").split())
    for phrase, key in _INDEX_PHRASES.items():
        if text == phrase or text.startswith(phrase + " "):
            return key
    parts = text.split()
    root = parts[0] if parts else ""
    root = _INDEX_ALIASES.get(root, root)
    return root if root in SYMBOLS else ""


def symbol_for(instrument: Any) -> str:
    key = index_key(instrument)
    if not key:
        raise ValueError(f"S1+S2 Dual runs on {', '.join(SYMBOLS)} only, not {instrument!r}")
    return SYMBOLS[key]


def host_timeframe(timeframe: Any) -> str:
    """'5min' for '5min' / '5m' / '5'; '' for an unsupported timeframe."""
    text = str(timeframe or "").strip().lower().replace("min", "").replace("m", "")
    key = f"{text}min" if text.isdigit() else ""
    return key if key in TIMEFRAMES else ""


def warmup_bars(timeframe: Any) -> int:
    return FEED_BARS_1MIN if host_timeframe(timeframe) == "1min" else FEED_BARS


def _tf_minutes(timeframe: Any) -> int:
    return tf_minutes(TIMEFRAMES[host_timeframe(timeframe) or "5min"])


def evaluation_clock(as_of=None) -> pd.Timestamp:
    """The UTS clock: an explicit replay clock, or the live market clock (naive IST)."""
    from engine import ut_smart_engine as uts

    return uts.evaluation_clock(as_of)


def _naive_ist(index) -> pd.DatetimeIndex:
    idx = pd.DatetimeIndex(index)
    return idx.tz_convert(TIMEZONE).tz_localize(None) if idx.tz is not None else idx


def _naive_ts(value) -> pd.Timestamp:
    stamp = pd.Timestamp(value)
    return stamp.tz_convert(TIMEZONE).tz_localize(None) if stamp.tzinfo is not None else stamp


# ─────────────────────────────────────────────────────────────── frame adapter

_OHLC = ("open", "high", "low", "close")
_OHLCV = _OHLC + ("volume",)
# volume_basis labels that are not an exchange volume (uts_lane.setup_values uses the same
# list, plus the candle store's tick-count and empty labels).
UNUSABLE_VOLUME_BASIS = frozenset({
    "", "nan", "<na>", "none", "unknown", "unavailable", "proxy", "ticks", "tick_activity",
    "index_tick_activity", "tick_rolling_avg", "tick_time_of_day", "mixed", "mixed_units",
})


def _empty_frame() -> pd.DataFrame:
    return pd.DataFrame({col: pd.Series(dtype="float64") for col in _OHLCV},
                        index=pd.DatetimeIndex([], tz=TIMEZONE))


def _numeric(df: pd.DataFrame, column: str) -> np.ndarray:
    return pd.to_numeric(df[column], errors="coerce").to_numpy(dtype="float64", na_value=np.nan)


def prepare_frame(df: Optional[pd.DataFrame], timeframe: str, *, as_of=None, closed_only: bool = True,
                  stats: Optional[dict] = None) -> pd.DataFrame:
    """The package's input: closed 09:15-15:15 session bars, float64 OHLCV, tz-aware IST.

    - Closed bars only, judged by the UTS evaluation clock (``as_of`` in replay): a bar is
      closed once open + timeframe <= clock. The forming bar never reaches the engine.
    - Pre-open bars and bars opening at or after 15:15 (frozen prints, then the bulk
      post-close volume) are dropped.
    - Volume <= 0, missing, or with an unusable volume_basis becomes NaN.
    - A row with a missing or non-positive price is dropped; high/low are widened to
      contain open and close. Duplicate stamps keep the last row.
    """
    counters = {"input": 0, "forming": 0, "off_session": 0, "bad_dropped": 0,
                "repaired": 0, "volume_masked": 0, "duplicates": 0}
    if stats is not None:
        stats.update(counters)
    if df is None or len(df) == 0 or not isinstance(df.index, pd.DatetimeIndex):
        return _empty_frame()
    if any(col not in df.columns for col in _OHLC):
        raise ValueError(f"S1+S2 Dual needs {', '.join(_OHLC)} columns")
    counters["input"] = len(df)
    idx = _naive_ist(df.index)
    keep = np.ones(len(df), dtype=bool)
    if closed_only:
        clock = evaluation_clock(as_of)
        closed = np.asarray(idx + pd.Timedelta(minutes=_tf_minutes(timeframe)) <= clock)
        counters["forming"] = int((~closed).sum())
        keep &= closed
    minute = np.asarray(idx.hour * 60 + idx.minute)
    session = (minute >= _SESSION_START_MIN) & (minute < _SESSION_END_MIN)
    counters["off_session"] = int((keep & ~session).sum())
    keep &= session
    pos = np.flatnonzero(keep)
    prices = {col: _numeric(df, col)[pos] for col in _OHLC}
    volume = _numeric(df, "volume")[pos] if "volume" in df.columns else np.full(len(pos), np.nan)
    o, h, l, c = (prices[col] for col in _OHLC)
    with np.errstate(invalid="ignore"):
        valid = np.isfinite(o) & np.isfinite(h) & np.isfinite(l) & np.isfinite(c) & (o > 0) & (h > 0) & (l > 0) & (c > 0)
    counters["bad_dropped"] = int((~valid).sum())
    hi = np.maximum.reduce([o, h, l, c])
    lo = np.minimum.reduce([o, h, l, c])
    counters["repaired"] = int((valid & ((hi != h) | (lo != l))).sum())
    bad_volume = ~np.isfinite(volume) | (volume <= 0)
    if "volume_basis" in df.columns and len(pos):
        basis = df["volume_basis"].iloc[pos].astype("object").to_numpy()
        unusable = np.fromiter((str(b).strip().lower() in UNUSABLE_VOLUME_BASIS if b is not None else True
                                for b in basis), dtype=bool, count=len(basis))
        bad_volume |= unusable
    counters["volume_masked"] = int((valid & bad_volume & np.isfinite(volume)).sum())
    volume = np.where(bad_volume, np.nan, volume)
    out = pd.DataFrame({"open": o, "high": hi, "low": lo, "close": c, "volume": volume},
                       index=idx[pos])[valid]
    duplicated = out.index.duplicated(keep="last")
    if duplicated.any():
        counters["duplicates"] = int(duplicated.sum())
        out = out[~duplicated]
    if not out.index.is_monotonic_increasing:
        out = out.sort_index(kind="stable")
    out.index = out.index.tz_localize(TIMEZONE)
    out.index.name = None
    if stats is not None:
        stats.update(counters)
    return out


def aux_feeds(timeframe: str, df_aux: Optional[pd.DataFrame], *, as_of=None) -> Optional[Dict[str, pd.DataFrame]]:
    """The package's auxiliary feed for a chart: {'5': closed 5m bars} for 15m, else None."""
    aux_tf = AUX_TIMEFRAME.get(host_timeframe(timeframe))
    if aux_tf is None:
        return None
    frame = prepare_frame(df_aux, aux_tf, as_of=as_of)
    if not len(frame):
        raise ValueError(f"S1+S2 Dual {timeframe} needs the closed {aux_tf} feed")
    return {TIMEFRAMES[aux_tf]: frame}


def frame_signature(frame: Optional[pd.DataFrame]) -> str:
    """Hash of the closed bars' stamps and OHLCV (NaN-canonical)."""
    if frame is None:
        return "none"
    digest = hashlib.blake2b(digest_size=16)
    digest.update(np.ascontiguousarray(pd.DatetimeIndex(frame.index).asi8).tobytes())
    for col in _OHLCV:
        values = frame[col].to_numpy(dtype="float64") if col in frame else np.full(len(frame), np.nan)
        digest.update(np.ascontiguousarray(np.where(np.isnan(values), np.nan, values)).tobytes())
    return digest.hexdigest()


def input_signature(frame: pd.DataFrame, aux: Optional[Dict[str, pd.DataFrame]], cfg: Config) -> str:
    parts = [frame_signature(frame), config_hash(cfg)]
    for key in sorted(aux or {}):
        parts.append(f"{key}:{frame_signature(aux[key])}")
    return "|".join(parts)


def _covered_by_aux(frame: pd.DataFrame, minutes: int, aux: Optional[Dict[str, pd.DataFrame]]) -> pd.DataFrame:
    """Chart bars whose close the finer auxiliary feed already covers (else update() raises
    'Stale MTF feed' and a full run would align a missing intrabar)."""
    finer = [(tf_minutes(key), feed) for key, feed in (aux or {}).items() if tf_minutes(key) < minutes]
    if not finer:
        return frame
    if any(not len(feed) for _, feed in finer):
        return frame.iloc[:0]
    covered = min(feed.index[-1] + pd.Timedelta(minutes=m) for m, feed in finer)
    return frame.loc[frame.index + pd.Timedelta(minutes=minutes) <= covered]


# ─────────────────────────────────────────────────────────────── D1 levels


def d1_stop_pct(cfg: Config) -> float:
    """The D1 stop %: s1_stop_pct clamped by the Intraday hard cap (engine _s1_new_trade)."""
    cap = mode_cap(cfg, "Intraday", "NONE")
    return float(min(cfg.s1_stop_pct, cap) if cfg.hard_stop_enabled else cfg.s1_stop_pct)


def d1_ladder(cfg: Config) -> Tuple[float, float, float]:
    t2 = max(cfg.t1_r + .25, cfg.t2_r)
    return float(cfg.t1_r), float(t2), float(max(t2 + .25, cfg.t3_r))


def d1_levels(entry: float, long: bool, stop_pct: float, ladder, tick: float = TICK_SIZE) -> Tuple[float, float, float, float]:
    """(stop, t1, t2, t3) for a D1 entry at ``entry``, exactly as the package fills one."""
    stop = nearest_stop(hard_stop_price(float(entry), bool(long), float(stop_pct)), tick, bool(long))
    t1, t2, t3 = ladder_targets(float(entry), stop, bool(long), *[float(r) for r in ladder])
    return float(stop), float(t1), float(t2), float(t3)


def d1_fill_cancel_reason(signal_open, minutes: int, cfg: Config) -> str:
    """The SETUP_CANCELLED reason the engine will give a D1 SETUP on this bar ("" = fills).

    The engine fills a SETUP at the next bar's open unless that bar is its EOD bar
    (INTRADAY_CUTOFF) or opens a new session (INTRADAY_NEW_DAY). Known at the SETUP bar's
    close, so a live entry the engine would never hold is refused up front.
    """
    stamp = _naive_ts(signal_open)
    next_open = stamp.hour * 60 + stamp.minute + int(minutes)
    if next_open >= _SESSION_END_MIN:
        return "INTRADAY_NEW_DAY"
    close_min = min(next_open + int(minutes), _EXCHANGE_CLOSE_MIN)
    cutoff = int(cfg.s1_eod_cutoff_ist_min)
    eod = 555 <= close_min <= 930 and (close_min >= cutoff or close_min + int(minutes) > cutoff)
    return "INTRADAY_CUTOFF" if eod and cfg.s1_force_eod else ""


# ─────────────────────────────────────────────────────────────── scanner speed-up


class FastDualScanner(DualScanner):
    """DualScanner with its per-bar session clock precomputed; nothing else differs.

    The package boxes a pandas Timestamp per bar in _session (and in the S2 force-exit
    close minute); these arrays replace that work. tests/test_s12_engine_core.py proves the
    events and bar snapshots identical to DualScanner on real NIFTY 5m and 15m frames.
    """

    def _s12_clock(self):
        cache = getattr(self, "_s12_clock_cache", None)
        if (cache is None or cache[0] is not self._local or cache[1] is not self._s2_local
                or cache[2] is not self._bar_closes):
            def minutes(ix):
                return np.asarray(ix.hour * 60 + ix.minute, dtype=np.int64)

            def days(ix):
                return np.asarray(ix.normalize().asi8)

            closes = self._bar_closes.tz_convert(self.s2_tz)
            cache = (self._local, self._s2_local, self._bar_closes,
                     minutes(self._local), days(self._local),
                     minutes(self._s2_local), days(self._s2_local), minutes(closes))
            self._s12_clock_cache = cache
        return cache

    def _session(self, engine='S2'):
        if engine not in {'S1', 'S2'}:
            raise ValueError('engine must be S1 or S2')
        clock = self._s12_clock()
        region = self.native_region if engine == 'S1' else self.region
        mins, days = (clock[3], clock[4]) if engine == 'S1' else (clock[5], clock[6])
        i = self.i
        minute = int(mins[i])
        end = minute + self.minutes
        prev_min = int(mins[i - 1]) if i else None
        commodity = self.s2_is_commodity
        if region == 'India':
            start, stop = (540, 1410) if commodity else (555, 930)
        elif region == 'US':
            start, stop = 570, 960
        elif self.native_session:
            start, stop = self.native_session
        else:
            raise ValueError('Native session hours required; avoid guessing exchange schedule')
        in_session = (start <= minute < stop) if self.minutes < 1440 else True
        prev_in = (prev_min is not None and start <= prev_min < stop) if self.minutes < 1440 else False
        first = bool(not i or days[i - 1] != days[i] or (in_session and not prev_in))
        last = (in_session and end >= stop and self.minutes < 1440)
        return dict(first=first, minute=minute, end=end, market=in_session,
                    session_start=start, session_end=stop, last=last,
                    region=region, is_commodity=commodity if region == 'India' else False)

    def _s2_close_minute(self):
        return int(self._s12_clock()[7][self.i])


USE_FAST_SCANNER = True


def scanner_class():
    return FastDualScanner if USE_FAST_SCANNER else DualScanner


# ─────────────────────────────────────────────────────────────── persistent runner


def _session_day(frame: pd.DataFrame):
    return frame.index[-1].tz_convert(TIMEZONE).date() if len(frame) else None


def _same_prefix(previous: Optional[pd.DataFrame], current: pd.DataFrame, *, trimmed_head_ok: bool = True) -> bool:
    """True when ``current`` extends ``previous``: no earlier history, and every bar the two
    share is identical. A trimmed head is fine unless ``trimmed_head_ok`` is False (the
    scanner already holds that history, anchored at its own first bar)."""
    if previous is None or not len(previous):
        return False
    if not len(current) or current.index[0] < previous.index[0]:
        return False
    if not trimmed_head_ok and current.index[0] != previous.index[0]:
        return False
    overlap = current.loc[current.index <= previous.index[-1]]
    if not len(overlap):
        return False
    reference = previous.loc[previous.index >= overlap.index[0]]
    if len(reference) != len(overlap) or not reference.index.equals(overlap.index):
        return False
    return bool(np.array_equal(reference[list(_OHLCV)].to_numpy(dtype="float64"),
                               overlap[list(_OHLCV)].to_numpy(dtype="float64"), equal_nan=True))


class IncrementalRunner:
    """One persistent DualScanner per (index, timeframe, session day, config hash).

    advance() feeds the latest closed bars: update() for bars after the last fed one, or a
    full run() on a new session day, a config change, a first call, an earlier or changed
    history (prefix mismatch) or an update() failure. Not thread-safe: one owner at a time.

    Live (allow_trimmed_head=True) a frame whose oldest bars were trimmed keeps the state
    anchored at the first run's start. With allow_trimmed_head=False (replay, engine lab) a
    different first bar is a full run, so the result always equals a fresh run() over
    exactly the frame given, whatever was computed before.
    """

    def __init__(self, instrument: str, timeframe: str, *, scanner_cls=None, allow_trimmed_head: bool = True):
        self.index = index_key(instrument)
        self.symbol = symbol_for(instrument)
        self.timeframe = host_timeframe(timeframe)
        if not self.timeframe:
            raise ValueError(f"S1+S2 Dual does not run on timeframe {timeframe!r}")
        self.package_tf = TIMEFRAMES[self.timeframe]
        self.minutes = tf_minutes(self.package_tf)
        self.scanner_cls = scanner_cls or scanner_class()
        self.allow_trimmed_head = bool(allow_trimmed_head)
        self.scanner: Optional[DualScanner] = None
        self.config: Optional[Config] = None
        self.key: Optional[tuple] = None
        self.fed: Optional[pd.DataFrame] = None
        self.aux: Optional[Dict[str, pd.DataFrame]] = None
        self.full_runs = 0
        self.updates = 0
        self.last_mode = ""
        self.last_seconds = 0.0

    def _aux_extends(self, aux: Optional[Dict[str, pd.DataFrame]]) -> bool:
        before, now = self.aux or {}, aux or {}
        if set(before) != set(now):
            return False
        return all(_same_prefix(before[key], now[key], trimmed_head_ok=self.allow_trimmed_head) for key in before)

    def _full(self, frame, aux, cfg, key) -> str:
        scanner = self.scanner_cls(frame, symbol=self.symbol, timeframe=self.package_tf,
                                   mtf=dict(aux) if aux else None, config=replace(cfg),
                                   tick_size=TICK_SIZE, security_type=SECURITY_TYPE)
        scanner.run()
        self.scanner, self.config, self.key = scanner, cfg, key
        self.fed, self.aux = frame, (dict(aux) if aux else None)
        self.full_runs += 1
        return "full"

    def advance(self, frame: pd.DataFrame, aux: Optional[Dict[str, pd.DataFrame]] = None,
                cfg: Optional[Config] = None) -> str:
        """Feed prepared closed bars; returns "full", "update" or "unchanged"."""
        cfg = cfg if cfg is not None else config_from_settings(None)
        frame = _covered_by_aux(frame, self.minutes, aux)
        if not len(frame):
            raise ValueError("S1+S2 Dual has no closed bars to compute")
        key = (self.index, self.timeframe, _session_day(frame), config_hash(cfg))
        started = time.perf_counter()
        if (self.scanner is None or key != self.key
                or not _same_prefix(self.fed, frame, trimmed_head_ok=self.allow_trimmed_head)
                or not self._aux_extends(aux)):
            mode = self._full(frame, aux, cfg, key)
        else:
            new = frame.loc[frame.index > self.fed.index[-1]]
            if not len(new):
                mode = "unchanged"
            else:
                try:
                    self.scanner.update(new, mtf=dict(aux) if aux else None)
                    self.fed = pd.concat([self.fed, new])
                    self.updates += 1
                    mode = "update"
                except Exception as exc:
                    logger.debug(f"S1+S2 Dual update failed for {self.index} {self.timeframe}; full run: {exc}")
                    mode = self._full(frame, aux, cfg, key)
            if aux:
                self.aux = dict(aux)
        self.last_mode = mode
        self.last_seconds = time.perf_counter() - started
        return mode


# ─────────────────────────────────────────────────────────────── published snapshot


@dataclass
class S12Snapshot:
    """What the worker publishes after each computation. Treat as read-only."""
    instrument: str
    timeframe: str
    symbol: str = ""
    signature: str = ""
    config_hash: str = ""
    observed_at: Optional[pd.Timestamp] = None
    computed_at: float = 0.0
    compute_mode: str = ""
    compute_seconds: float = 0.0
    index: pd.DatetimeIndex = field(default_factory=lambda: pd.DatetimeIndex([]))
    events: Tuple[dict, ...] = ()
    signal_records: Tuple[dict, ...] = ()
    trades: Dict[str, dict] = field(default_factory=dict)
    markers: Tuple[dict, ...] = ()
    arrays: Dict[str, np.ndarray] = field(default_factory=dict)
    positions: Dict[str, Optional[dict]] = field(default_factory=dict)
    statistics: Dict[str, dict] = field(default_factory=dict)
    sessions: int = 0
    daily_regime_ready: bool = False
    error: str = ""


def _float(value, default=float("nan")) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def _side(value) -> int:
    return 1 if value == "LONG" else -1 if value == "SHORT" else 0


def _json_value(value):
    if isinstance(value, (np.floating, np.integer, np.bool_)):
        return value.item()
    if isinstance(value, dict):
        return {key: _json_value(item) for key, item in value.items()}
    return value


def build_snapshot(runner: IncrementalRunner, *, signature: str = "", observed_at=None,
                   compute_mode: str = "", compute_seconds: float = 0.0) -> S12Snapshot:
    """Convert the runner's scanner state into lane trades, signal records and bar arrays."""
    sc = runner.scanner
    cfg = runner.config
    data = sc.data
    naive = _naive_ist(data.index)
    n = len(naive)
    minutes = runner.minutes
    step = pd.Timedelta(minutes=minutes)
    snaps = sc.snapshots
    events = tuple(_json_value(e) for e in sc.events)
    if events:
        stamps = _naive_ist(pd.to_datetime([e["bar_open"] for e in events], utc=True))
        positions = naive.get_indexer(stamps)
    else:
        stamps, positions = pd.DatetimeIndex([]), np.array([], dtype=int)

    close = data["close"].to_numpy(dtype="float64")
    arrays = {
        "open": data["open"].to_numpy(dtype="float64"),
        "high": data["high"].to_numpy(dtype="float64"),
        "low": data["low"].to_numpy(dtype="float64"),
        "close": close,
        "atr": data["atr14"].to_numpy(dtype="float64"),
        "adx": data["adx"].to_numpy(dtype="float64"),
        "rsi": data["rsi14"].to_numpy(dtype="float64"),
        "D1_side": np.array([_side(s["s1_side"]) for s in snaps], dtype=np.int64),
        "D2_side": np.array([_side(s["s2_side"]) for s in snaps], dtype=np.int64),
        "D1_stop": np.array([_float(s["s1_stop"]) for s in snaps], dtype="float64"),
        "D2_stop": np.array([_float(s["s2_stop"]) for s in snaps], dtype="float64"),
        "D1_bull": np.array([_float(s["s1_bull"]) for s in snaps], dtype="float64"),
        "D1_bear": np.array([_float(s["s1_bear"]) for s in snaps], dtype="float64"),
        "D2_bull": np.array([_float(s["s2_bull"]) for s in snaps], dtype="float64"),
        "D2_bear": np.array([_float(s["s2_bear"]) for s in snaps], dtype="float64"),
    }
    for lane in LANES:
        for level in ("t1", "t2", "t3"):
            arrays[f"{lane}_{level}"] = np.full(n, np.nan)

    trades: Dict[str, dict] = {}
    records: List[dict] = []
    markers: List[dict] = []
    for event, pos, stamp in zip(events, positions, stamps):
        lane = LANE_OF_ENGINE.get(event.get("engine"), "")
        kind = event.get("event")
        tid = event.get("trade_id")
        pos = int(pos)
        if not lane or pos < 0:
            continue
        stamp = pd.Timestamp(stamp)
        if kind == "SETUP" and lane == "D1":
            long = event.get("signal") == "BUY"
            pct = d1_stop_pct(cfg)
            ladder = d1_ladder(cfg)
            price = _float(event.get("signal_close"))
            stop, t1, t2, t3 = d1_levels(price, long, pct, ladder)
            score = arrays["D1_bull" if long else "D1_bear"][pos]
            record = dict(lane=lane, event_id=event["id"], trade_id=tid, signal_type="BUY" if long else "SELL",
                          direction=1 if long else -1, bar=pos, time=stamp, price=price, stop=stop,
                          stop_pct=pct, t1=t1, t2=t2, t3=t3, r_ladder=list(ladder), score=_float(score, 0.0),
                          setup=str(event.get("setup") or ""), entry_basis="NEXT_BAR_OPEN",
                          fill_cancel=d1_fill_cancel_reason(stamp, minutes, cfg))
            records.append(record)
            trades[tid] = dict(lane=lane, trade_id=tid, side="LONG" if long else "SHORT", signal_time=stamp,
                               signal_bar=pos, event_id=event["id"], setup=record["setup"], score=record["score"],
                               entry_bar=None, entry_time=None, engine_entry=None, initial_stop=None,
                               stop=None, t1=None, t2=None, t3=None, stop_updates=[], milestones={},
                               exit=None, cancelled="")
            markers.append(dict(time=stamp, bar=pos, lane=lane, event="SETUP", side=trades[tid]["side"],
                                price=price, reason=record["setup"], trade_id=tid))
        elif kind == "ENTRY" and lane == "D2":
            long = event.get("action") == "BUY"
            entry, stop = _float(event.get("price")), _float(event.get("stop"))
            reference = _float(event.get("reference_close"))
            risk = abs(entry - stop)
            record = dict(lane=lane, event_id=event["id"], trade_id=tid, signal_type="BUY" if long else "SELL",
                          direction=1 if long else -1, bar=pos, time=stamp, price=reference, stop=stop,
                          stop_pct=(risk / entry * 100.0) if entry > 0 else float("nan"),
                          t1=_float(event.get("t1")), t2=_float(event.get("t2")), t3=_float(event.get("t3")),
                          r_ladder=[(abs(_float(event.get(k)) - entry) / risk) if risk > 0 else float("nan")
                                    for k in ("t1", "t2", "t3")],
                          score=_float(event.get("confidence"), 0.0),
                          setup="BURST" if event.get("burst") else "CONFLUENCE", entry_basis="SIGNAL_CLOSE",
                          engine_entry=entry, burst=bool(event.get("burst")), htf_score=_float(event.get("htf_score"), 0.0),
                          subtype=str(event.get("subtype") or "NONE"), fill_cancel="")
            records.append(record)
            trades[tid] = dict(lane=lane, trade_id=tid, side="LONG" if long else "SHORT", signal_time=stamp,
                               signal_bar=pos, event_id=event["id"], setup=record["setup"], score=record["score"],
                               entry_bar=pos, entry_time=stamp, engine_entry=entry, initial_stop=stop, stop=stop,
                               t1=record["t1"], t2=record["t2"], t3=record["t3"], stop_updates=[], milestones={},
                               exit=None, cancelled="")
            markers.append(dict(time=stamp, bar=pos, lane=lane, event="ENTRY", side=trades[tid]["side"],
                                price=reference, reason=record["setup"], trade_id=tid))
        else:
            trade = trades.get(tid)
            if trade is None:
                continue
            if kind == "FILL_ESTIMATE":
                trade.update(entry_bar=pos, entry_time=stamp, engine_entry=_float(event.get("price")),
                             initial_stop=_float(event.get("stop")), stop=_float(event.get("stop")),
                             t1=_float(event.get("t1")), t2=_float(event.get("t2")), t3=_float(event.get("t3")))
                markers.append(dict(time=stamp, bar=pos, lane=lane, event="FILL", side=trade["side"],
                                    price=trade["engine_entry"], reason="NEXT_BAR_OPEN", trade_id=tid))
            elif kind == "SETUP_CANCELLED":
                reason = str(event.get("reason") or "")
                trade["cancelled"] = reason
                trade["exit"] = dict(reason=f"SETUP_CANCELLED:{reason}", time=stamp, price=None, bar=pos,
                                     net_pct=0.0)
                markers.append(dict(time=stamp, bar=pos, lane=lane, event="CANCEL", side=trade["side"],
                                    price=float(close[pos]), reason=reason, trade_id=tid))
            elif kind in ("T1", "T2", "T3"):
                trade["milestones"][kind] = dict(time=stamp, bar=pos, price=_float(event.get("price")))
                markers.append(dict(time=stamp, bar=pos, lane=lane, event=kind, side=trade["side"],
                                    price=_float(event.get("price")), reason="milestone", trade_id=tid))
            elif kind == "STOP_UPDATED":
                stop = _float(event.get("stop"))
                trade["stop_updates"].append(dict(bar=pos, time=stamp, effective_from=stamp + step, stop=stop))
                trade["stop"] = stop
            elif kind == "EXIT":
                trade["exit"] = dict(reason=str(event.get("reason") or ""), time=stamp,
                                     price=_float(event.get("price")), bar=pos,
                                     net_pct=_float(event.get("net_pct"), 0.0))
                markers.append(dict(time=stamp, bar=pos, lane=lane, event="EXIT", side=trade["side"],
                                    price=_float(event.get("price")), reason=trade["exit"]["reason"], trade_id=tid))

    # Target lines while a trade is open after the bar (the snapshot side), as Pine plots them.
    for trade in trades.values():
        if trade["entry_bar"] is None:
            continue
        lane = trade["lane"]
        end = trade["exit"]["bar"] if trade["exit"] else n
        side = arrays[f"{lane}_side"]
        for level in ("t1", "t2", "t3"):
            line = arrays[f"{lane}_{level}"]
            for j in range(int(trade["entry_bar"]), int(end)):
                if side[j] != 0:
                    line[j] = trade[level]

    lane_positions = {}
    for engine_name, lane in LANE_OF_ENGINE.items():
        details = sc.current_positions().get(engine_name)
        lane_positions[lane] = _json_value(dict(details)) if details else None
    statistics = {}
    for engine_name, lane in LANE_OF_ENGINE.items():
        state = sc.s1 if engine_name == "S1" else sc.s2
        statistics[lane] = dict(total=int(state.total), wins=int(state.wins), flat=int(state.flat),
                                loss_streak=int(state.losses), net_pct=float(state.net_pct))
    for values in arrays.values():
        values.setflags(write=False)
    d_sma = sc.mtf.get("D_sma20") if isinstance(sc.mtf, dict) else None
    return S12Snapshot(
        instrument=runner.index, timeframe=runner.timeframe, symbol=runner.symbol, signature=signature,
        config_hash=config_hash(cfg), observed_at=_naive_ts(observed_at) if observed_at is not None else None,
        computed_at=time.time(), compute_mode=compute_mode, compute_seconds=float(compute_seconds),
        index=naive, events=events, signal_records=tuple(records), trades=trades, markers=tuple(markers),
        arrays=arrays, positions=lane_positions, statistics=statistics,
        sessions=int(len(set(naive.date))) if n else 0,
        daily_regime_ready=bool(d_sma is not None and len(d_sma) and np.isfinite(d_sma[-1])),
    )


# ─────────────────────────────────────────────────────────────── host result


def _to_pydatetime(value):
    if value is None:
        return None
    stamp = pd.Timestamp(value)
    return None if pd.isna(stamp) else stamp.to_pydatetime()


def trade_payload(trade: dict) -> dict:
    """A copy of one engine trade with python datetimes (what lanes and charts read)."""
    out = dict(trade)
    for key in ("signal_time", "entry_time"):
        out[key] = _to_pydatetime(out.get(key))
    out["stop_updates"] = [dict(u, time=_to_pydatetime(u["time"]), effective_from=_to_pydatetime(u["effective_from"]))
                           for u in trade.get("stop_updates") or []]
    out["milestones"] = {k: dict(m, time=_to_pydatetime(m["time"])) for k, m in (trade.get("milestones") or {}).items()}
    if trade.get("exit"):
        out["exit"] = dict(trade["exit"], time=_to_pydatetime(trade["exit"]["time"]))
    return out


def build_signals(snap: S12Snapshot, full_index: Optional[pd.DatetimeIndex], instrument: str,
                  timeframe: str) -> list:
    """One UTBotSignal per package SETUP (D1) / ENTRY (D2) event, with s12_* attributes and
    the exit the engine later gave that trade."""
    from engine.ut_bot_core import UTBotSignal

    records = snap.signal_records
    if not records:
        return []
    times = pd.DatetimeIndex([r["time"] for r in records])
    if full_index is not None and full_index.is_unique:
        locs = full_index.get_indexer(times)
    else:
        locs = np.full(len(records), -1)
    signals = []
    arrays = snap.arrays
    for record, loc in zip(records, locs):
        i = int(record["bar"])
        trade = snap.trades.get(record["trade_id"]) or {}
        exit_info = trade.get("exit") or {}
        stop = float(record["stop"])
        price = float(record["price"])
        sig = UTBotSignal(
            timestamp=record["time"].to_pydatetime(),
            signal_type=record["signal_type"],
            price=price,
            trailing_stop=stop,
            stop_distance=abs(price - stop),
            atr_value=float(np.nan_to_num(arrays["atr"][i])),
            adx_value=float(np.nan_to_num(arrays["adx"][i])),
            position_state=int(record["direction"]),
            bar_index=int(loc) if loc >= 0 else i,
            instrument=instrument,
            timeframe=timeframe,
            raw_candle={key: float(arrays[key][i]) for key in _OHLC},
            source_engine=ENGINE_KEY,
            source_lane=record["lane"],
        )
        updates = trade.get("stop_updates") or []
        for key, value in {
            "source_engines": [ENGINE_KEY],
            "s12_lane": record["lane"],
            "s12_trade_id": record["trade_id"],
            "s12_event_id": record["event_id"],
            "s12_stop": stop,
            "s12_stop_pct": float(record["stop_pct"]),
            "s12_t1": float(record["t1"]),
            "s12_t2": float(record["t2"]),
            "s12_t3": float(record["t3"]),
            "s12_r_ladder": [float(r) for r in record["r_ladder"]],
            "s12_score": float(record["score"]),
            "s12_setup": record["setup"],
            "s12_signal_close": float(arrays["close"][i]),
            "s12_entry_basis": record["entry_basis"],
            "s12_engine_entry": trade.get("engine_entry"),
            "s12_engine_stop": trade.get("initial_stop"),
            "s12_stop_current": trade.get("stop"),
            "s12_stop_updates": [[_to_pydatetime(u["effective_from"]), float(u["stop"])] for u in updates],
            "s12_milestones": {k: _to_pydatetime(m["time"]) for k, m in (trade.get("milestones") or {}).items()},
            "s12_fill_cancel": record.get("fill_cancel", ""),
            "s12_burst": bool(record.get("burst", False)),
            "s12_forming": False,
            "s12_rsi": float(np.nan_to_num(arrays["rsi"][i], nan=50.0)),
            "s12_exit_reason": exit_info.get("reason", "") if exit_info else "",
            "s12_exit_time": _to_pydatetime(exit_info.get("time")) if exit_info else None,
            "s12_exit_price": exit_info.get("price") if exit_info else None,
        }.items():
            setattr(sig, key, value)
        signals.append(sig)
    return signals


# Chart colours per lane: stop, then T1-T3.
OVERLAY_COLORS = {
    "D1": {"stop": "#ff9900", "t1": "#66cc66", "t2": "#33aa33", "t3": "#118811"},
    "D2": {"stop": "#cc66ff", "t1": "#66ccff", "t2": "#3399dd", "t3": "#1166bb"},
}


def _vals(values) -> list:
    arr = np.asarray(values, dtype=float)
    out = arr.astype(object)
    out[~np.isfinite(arr)] = None
    return out.tolist()


def host_result(snap: S12Snapshot, full: pd.DataFrame, instrument: str, timeframe: str, *,
                as_of=None, capital: float = 100000.0, risk_pct: float = 1.0,
                adx_threshold: float = 25.0, current_signature: str = "") -> dict:
    """The AltSignalEngine result for one chart, mapped onto the caller's candles.

    Per-bar values carry forward onto bars the engine never saw (the forming bar, the
    15:15+ bars); bars after the clock are blanked so a replay never plots the future.
    """
    if snap.error:
        return {"error": snap.error, "signals": [], "state": None, "source_engine": ENGINE_KEY}
    clock = evaluation_clock(as_of)
    full_index = _naive_ist(full.index)
    closed = snap.index
    n_closed = len(closed)
    pos = closed.searchsorted(full_index, side="right") - 1 if n_closed else np.full(len(full_index), -1)
    observed = np.asarray(full_index <= clock)
    mapped = (pos >= 0) & observed

    def to_full(values) -> np.ndarray:
        arr = np.asarray(values, dtype=float)
        out = np.full(len(full_index), np.nan)
        if n_closed:
            out[mapped] = arr[pos[mapped]]
        return out

    signals = [sig for sig in build_signals(snap, full_index, instrument, timeframe)
               if _naive_ts(sig.timestamp) <= clock]
    d1_side, d2_side = snap.arrays["D1_side"], snap.arrays["D2_side"]
    combined_side = np.where(d1_side != 0, d1_side, d2_side)
    combined_stop = np.where(d1_side != 0, snap.arrays["D1_stop"], snap.arrays["D2_stop"])
    positions_full = np.nan_to_num(to_full(combined_side)).astype(int)
    stop_full = to_full(combined_stop)
    overlays, lane_overlays = [], {}
    for lane in LANES:
        lane_overlays[lane] = {}
        for level in ("stop", "t1", "t2", "t3"):
            values = _vals(to_full(snap.arrays[f"{lane}_{level}"]))
            lane_overlays[lane][level] = values
            overlays.append({"name": f"{lane} {'Stop' if level == 'stop' else level.upper()}",
                             "color": OVERLAY_COLORS[lane][level], "values": values, "lane": lane})
    markers = []
    visible = [marker for marker in snap.markers if marker["time"] <= clock]
    if visible and full_index.is_unique:
        marker_locs = full_index.get_indexer(pd.DatetimeIndex([marker["time"] for marker in visible]))
    else:
        marker_locs = np.full(len(visible), -1)
    for marker, loc in zip(visible, marker_locs):
        item = dict(marker)
        item["time"] = _to_pydatetime(marker["time"])
        item["bar_index"] = int(loc)
        markers.append(item)
    last = n_closed - 1
    lane_state = {}
    for lane in LANES:
        lane_state[lane] = dict(
            position=int(snap.arrays[f"{lane}_side"][last]) if last >= 0 else 0,
            stop=_float(snap.arrays[f"{lane}_stop"][last]) if last >= 0 else float("nan"),
            bull=_float(snap.arrays[f"{lane}_bull"][last]) if last >= 0 else float("nan"),
            bear=_float(snap.arrays[f"{lane}_bear"][last]) if last >= 0 else float("nan"),
        )
    state_pos = lane_state["D1"]["position"] or lane_state["D2"]["position"]
    state_lane = "D1" if lane_state["D1"]["position"] else "D2" if lane_state["D2"]["position"] else ""
    state_trail = lane_state[state_lane]["stop"] if state_lane else 0.0
    state_trail = state_trail if math.isfinite(state_trail) else 0.0
    observed_close = full.loc[observed, "close"] if observed.any() else full["close"].iloc[:0]
    price_now = float(observed_close.iloc[-1]) if len(observed_close) else (
        float(snap.arrays["close"][last]) if last >= 0 else 0.0)
    distance = abs(price_now - state_trail) if state_pos else 0.0
    exits = [t["exit"] for t in snap.trades.values() if t.get("exit") and t["exit"].get("price") is not None]
    last_exit = max(exits, key=lambda e: e["time"]) if exits else None
    atr_last = float(np.nan_to_num(snap.arrays["atr"][last])) if last >= 0 else 0.0
    adx_last = float(np.nan_to_num(snap.arrays["adx"][last])) if last >= 0 else 0.0
    return {
        "instrument": instrument, "timeframe": timeframe, "candles": len(full),
        "source_engine": ENGINE_KEY, "signals": signals, "positions": positions_full.tolist(),
        "s12_evaluation": {
            "observed_at": clock.to_pydatetime(),
            "computed_for": _to_pydatetime(snap.observed_at),
            "first_bar": closed[0] if n_closed else None,
            "last_closed_bar": closed[-1] if n_closed else None,
            "forming_bar": None,
            "computed_at": snap.computed_at,
            "compute_mode": snap.compute_mode,
            "compute_seconds": snap.compute_seconds,
            "config_hash": snap.config_hash,
            "signature": snap.signature,
            "current": bool(not current_signature or current_signature == snap.signature),
            "sessions": snap.sessions,
            "daily_regime_ready": snap.daily_regime_ready,
            "package_version": PACKAGE_VERSION,
            "adapter_version": ADAPTER_VERSION,
        },
        "trailing_stop": _vals(stop_full),
        "trailing_stop_colors": ["green" if v == 1 else "red" if v == -1 else "gray" for v in positions_full],
        "atr": _vals(to_full(snap.arrays["atr"])),
        "adx": _vals(to_full(snap.arrays["adx"])),
        "indicator_overlays": overlays,
        "s12_overlays": lane_overlays,
        "s12_markers": markers,
        "s12_trades": {tid: trade_payload(t) for tid, t in snap.trades.items()},
        "s12_statistics": {lane: dict(v) for lane, v in snap.statistics.items()},
        "state": {
            "position": state_pos,
            "position_label": "LONG" if state_pos == 1 else "SHORT" if state_pos == -1 else "FLAT",
            "trailing_stop": state_trail,
            "last_entry_price": float(signals[-1].price) if signals else 0.0,
            "last_exit_price": float(last_exit["price"]) if last_exit else 0.0,
            "stop_distance": distance,
            "suggested_qty": int(float(capital) * float(risk_pct) / 100.0 / distance) if distance > 0 else 0,
            "current_price": price_now,
            "atr_value": atr_last,
            "adx_value": adx_last,
            "rsi_value": float(np.nan_to_num(snap.arrays["rsi"][last], nan=50.0)) if last >= 0 else 50.0,
            "is_trending": bool(adx_last >= float(adx_threshold)),
            "s12_state_bar_time": closed[-1] if n_closed else None,
            "s12_positions": {lane: (dict(p) if p else None) for lane, p in snap.positions.items()},
            **{f"s12_{lane.lower()}_{key}": value for lane, values in lane_state.items()
               for key, value in values.items()},
        },
    }


# ─────────────────────────────────────────────────────────────── compute worker


def _lower_current_thread_priority() -> None:
    """Below-normal OS priority for the calling thread (Windows); as scanner.py's lab worker."""
    if os.name != "nt":
        return
    try:
        import ctypes

        handle = ctypes.windll.kernel32.GetCurrentThread()
        ctypes.windll.kernel32.SetThreadPriority(handle, -1)
    except Exception:
        pass


@dataclass
class _Job:
    instrument: str
    timeframe: str
    frame: pd.DataFrame
    aux: Optional[Dict[str, pd.DataFrame]]
    config: Config
    signature: str
    observed_at: Optional[pd.Timestamp]


def compute(runner: IncrementalRunner, frame: pd.DataFrame, aux: Optional[Dict[str, pd.DataFrame]],
            cfg: Config, *, signature: str = "", observed_at=None) -> S12Snapshot:
    """Advance ``runner`` and publish its state; errors become an error snapshot."""
    started = time.perf_counter()
    try:
        mode = runner.advance(frame, aux, cfg)
        return build_snapshot(runner, signature=signature, observed_at=observed_at, compute_mode=mode,
                              compute_seconds=time.perf_counter() - started)
    except Exception as exc:
        logger.warning(f"S1+S2 Dual compute failed for {runner.index} {runner.timeframe}: {exc}")
        return S12Snapshot(instrument=runner.index, timeframe=runner.timeframe, symbol=runner.symbol,
                           signature=signature, observed_at=_naive_ts(observed_at) if observed_at is not None else None,
                           computed_at=time.time(), compute_seconds=time.perf_counter() - started,
                           error=f"S1+S2 Dual compute failed: {exc}")


class ComputeWorker:
    """A single low-priority daemon thread that computes on bar close.

    submit() hands over the latest closed bars (it never waits for a computation and
    coalesces to the newest job per index/timeframe); latest() returns the last published
    snapshot. The worker owns its runners, so no scanner is ever touched by two threads.
    """

    def __init__(self, name: str = "s12-dual-compute", *, lower_priority: bool = True):
        self.name = name
        self.lower_priority = lower_priority
        self._cond = threading.Condition()
        self._pending: "OrderedDict[tuple, _Job]" = OrderedDict()
        self._busy: Optional[_Job] = None
        self._published: Dict[tuple, S12Snapshot] = {}
        self._runners: Dict[tuple, IncrementalRunner] = {}
        self._thread: Optional[threading.Thread] = None
        self._stopping = False
        self.computed = 0
        self.skipped = 0

    @staticmethod
    def key(instrument: str, timeframe: str) -> tuple:
        return index_key(instrument), host_timeframe(timeframe)

    def submit(self, instrument: str, timeframe: str, frame: pd.DataFrame, *,
               aux: Optional[Dict[str, pd.DataFrame]] = None, config: Optional[Config] = None,
               signature: str = "", observed_at=None) -> bool:
        """Queue these closed bars; False when they match the published or queued input."""
        cfg = config if config is not None else config_from_settings(None)
        signature = signature or input_signature(frame, aux, cfg)
        key = self.key(instrument, timeframe)
        with self._cond:
            if self._stopping:
                return False
            published = self._published.get(key)
            queued = self._pending.get(key)
            if ((published is not None and published.signature == signature and not published.error)
                    or (queued is not None and queued.signature == signature)
                    or (self._busy is not None and self.key(self._busy.instrument, self._busy.timeframe) == key
                        and self._busy.signature == signature)):
                self.skipped += 1
                return False
            self._pending.pop(key, None)
            self._pending[key] = _Job(instrument, timeframe, frame, aux, cfg, signature,
                                      _naive_ts(observed_at) if observed_at is not None else None)
            self._ensure_thread()
            self._cond.notify_all()
        return True

    def latest(self, instrument: str, timeframe: str) -> Optional[S12Snapshot]:
        with self._cond:
            return self._published.get(self.key(instrument, timeframe))

    def pending(self) -> int:
        with self._cond:
            return len(self._pending) + (1 if self._busy is not None else 0)

    def wait_idle(self, timeout: float = 30.0) -> bool:
        deadline = time.monotonic() + max(0.0, float(timeout))
        with self._cond:
            while self._pending or self._busy is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._cond.wait(remaining)
        return True

    def stop(self, timeout: float = 5.0) -> None:
        with self._cond:
            self._stopping = True
            self._pending.clear()
            self._cond.notify_all()
            thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout)

    def _ensure_thread(self) -> None:
        if self._thread is None or not self._thread.is_alive():
            self._thread = threading.Thread(target=self._loop, name=self.name, daemon=True)
            self._thread.start()

    def _loop(self) -> None:
        if self.lower_priority:
            _lower_current_thread_priority()
        while True:
            with self._cond:
                while not self._pending and not self._stopping:
                    self._cond.wait(1.0)
                if self._stopping:
                    return
                key, job = self._pending.popitem(last=False)
                self._busy = job
            try:
                runner = self._runners.get(key)
                if runner is None:
                    runner = self._runners[key] = IncrementalRunner(job.instrument, job.timeframe)
                snapshot = compute(runner, job.frame, job.aux, job.config,
                                   signature=job.signature, observed_at=job.observed_at)
            except Exception as exc:  # never let the worker die
                snapshot = S12Snapshot(instrument=key[0], timeframe=key[1], signature=job.signature,
                                       computed_at=time.time(), error=f"S1+S2 Dual worker failed: {exc}")
            with self._cond:
                self._published[key] = snapshot
                self._busy = None
                self.computed += 1
                self._cond.notify_all()


_WORKER: Optional[ComputeWorker] = None
_WORKER_LOCK = threading.Lock()
_SYNC_RUNNERS: Dict[tuple, Tuple[threading.Lock, IncrementalRunner]] = {}
_SYNC_LOCK = threading.Lock()


def default_worker() -> ComputeWorker:
    global _WORKER
    with _WORKER_LOCK:
        if _WORKER is None:
            _WORKER = ComputeWorker()
        return _WORKER


def compute_now(instrument: str, timeframe: str, frame: pd.DataFrame,
                aux: Optional[Dict[str, pd.DataFrame]], cfg: Config, *, signature: str = "",
                observed_at=None) -> S12Snapshot:
    """Synchronous compute (HISTORICAL replay, engine lab, tests) on a persistent runner.

    The runner never reuses state across a different first bar, so the result equals a
    fresh run() over this frame whichever caller computed before.
    """
    key = ComputeWorker.key(instrument, timeframe)
    with _SYNC_LOCK:
        entry = _SYNC_RUNNERS.get(key)
        if entry is None:
            entry = _SYNC_RUNNERS[key] = (threading.Lock(),
                                          IncrementalRunner(instrument, timeframe, allow_trimmed_head=False))
    lock, runner = entry
    with lock:
        return compute(runner, frame, aux, cfg, signature=signature, observed_at=observed_at)


def process(df: pd.DataFrame, instrument: str, timeframe: str, *, df_5min: Optional[pd.DataFrame] = None,
            settings=None, as_of=None, synchronous: bool = False, worker: Optional[ComputeWorker] = None,
            capital: float = 100000.0, risk_pct: float = 1.0, adx_threshold: float = 25.0,
            min_bars: int = 15) -> dict:
    """One chart's S1+S2 Dual result for the host (the body of _process_s1s2_dual).

    Live (synchronous=False): hands the closed bars to the worker and returns the last
    published snapshot; never waits for a computation. "s12_evaluation.current" is False
    while the worker has not yet caught up with these bars. Replay and the engine lab pass
    synchronous=True and an explicit as_of clock.
    """
    def failure(message: str) -> dict:
        return {"error": message, "signals": [], "state": None, "source_engine": ENGINE_KEY}

    tf = host_timeframe(timeframe)
    if not index_key(instrument):
        return failure(f"S1+S2 Dual does not run on {instrument!r}")
    if not tf:
        return failure(f"S1+S2 Dual does not run on timeframe {timeframe!r}")
    try:
        cfg = config_from_settings(settings)
        frame = prepare_frame(df, tf, as_of=as_of)
        aux = aux_feeds(tf, df_5min, as_of=as_of)
    except ValueError as exc:
        return failure(str(exc))
    if len(frame) < max(1, int(min_bars)):
        return failure("Insufficient S1+S2 Dual data")
    signature = input_signature(frame, aux, cfg)
    observed_at = evaluation_clock(as_of)
    if synchronous:
        snap = compute_now(instrument, tf, frame, aux, cfg, signature=signature, observed_at=observed_at)
    else:
        pool = worker or default_worker()
        pool.submit(instrument, tf, frame, aux=aux, config=cfg, signature=signature, observed_at=observed_at)
        snap = pool.latest(instrument, tf)
        if snap is None:
            result = failure("S1+S2 Dual result pending")
            result["s12_pending"] = True
            return result
    return host_result(snap, df, instrument, tf, as_of=as_of, capital=capital, risk_pct=risk_pct,
                       adx_threshold=adx_threshold, current_signature=signature)


def field_names() -> tuple:
    return tuple(SETTING_FIELDS)
