"""Port of the supplied MANTRA UT + EMA Dual Lite v3.9 Pine indicator.

The two indicator positions follow the uploaded source, including its execution
order. A separate shared-position overlay implements the operator's base+1 rule.
Historical OHLC cannot reconstruct realtime tick paths; the Pine source itself
uses next-bar-open execution for that case.
"""
from __future__ import annotations

import copy
import hashlib
import math
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime
from typing import Optional

import numpy as np
import pandas as pd

from engine.ut_bot_core import UTBotSignal
from engine.ut_smart_engine import _ema, _rma, _true_range

ENGINE_KEY = "ut_ema"
LANE = "UTEMA"
TICK = .05


_OUTPUT_ATOMS = {str, int, float, bool, type(None)}


def _copy_output(value, memo=None):
    """Detach the native output graph without dispatching every scalar to deepcopy.

    Signals and JSON-like containers dominate these snapshots. Preserve memoized
    aliases/cycles and defer extension objects to their normal deepcopy protocol.
    """
    kind = type(value)
    if kind in _OUTPUT_ATOMS:
        return value
    if memo is None:
        memo = {}
    identity = id(value)
    if identity in memo:
        return memo[identity]
    if kind is dict:
        cloned = {}
        memo[identity] = cloned
        for key, item in value.items():
            cloned[key if type(key) in _OUTPUT_ATOMS else _copy_output(key, memo)] = (
                item if type(item) in _OUTPUT_ATOMS else _copy_output(item, memo))
        return cloned
    if kind is list:
        cloned = []
        memo[identity] = cloned
        cloned.extend(item if type(item) in _OUTPUT_ATOMS else _copy_output(item, memo) for item in value)
        return cloned
    if kind is UTBotSignal:
        cloned = object.__new__(UTBotSignal)
        memo[identity] = cloned
        cloned.__dict__ = _copy_output(value.__dict__, memo)
        return cloned
    return copy.deepcopy(value, memo)


def signal_fire_mode(settings=None):
    """Honor the host's canonical entry switch, including legacy settings."""
    if settings is None:
        from config.settings import get_settings
        settings = get_settings()
    reader = getattr(settings, "get_signal_fire_mode", None)
    if callable(reader):
        return reader()
    enabled = getattr(settings, "live_intrabar_signal_entries", True)
    enabled = str(enabled).strip().lower() not in {"false", "0", "off", "no"}
    return "INTRACANDLE" if enabled and str(getattr(settings, "ut_signal_mode", "realtime")).lower() != "confirmed" else "CANDLE_CLOSE"


class StreamDataGap(ValueError):
    """The observed stream cannot advance indicators across missing index bars."""


@dataclass(frozen=True)
class Params:
    ut_factor: float = 1.0
    ut_atr_len: int = 10
    ut_heikin: bool = False
    ema_fast_len: int = 9
    ema_slow_len: int = 21
    execution_mode: str = "Realtime Intrabar"
    intrabar_max_signals: int = 2
    base_stop_pct: float = .2
    ut_target_exit: str = "T2"
    ema_target_exit: str = "T2"
    rr1: float = 1.
    rr2: float = 2.
    rr3: float = 3.
    rr_increment: float = 1.
    ut_smart_stop_on: bool = True
    ut_smart_stop_method: str = "ATR"
    ut_smart_stop_arm: float = .40
    ut_smart_stop_atr: float = 1.50
    ut_smart_stop_pct: float = .50
    ut_peak_exit_on: bool = True
    ut_peak_min_move: float = .60
    ut_peak_pullback: float = .40
    ut_peak_bars: int = 1
    ema_smart_stop_on: bool = True
    ema_smart_stop_method: str = "ATR"
    ema_smart_stop_arm: float = .50
    ema_smart_stop_atr: float = 1.80
    ema_smart_stop_pct: float = .60
    ema_peak_exit_on: bool = True
    ema_peak_min_move: float = .75
    ema_peak_pullback: float = .50
    ema_peak_bars: int = 1
    ema_legacy_trail: bool = False
    ema_trail_factor: float = 1.8


@dataclass
class Position:
    direction: int = 0
    entry: float = math.nan
    stop: float = math.nan
    smart_stop: float = math.nan
    risk: float = math.nan
    extreme: float = math.nan
    extreme_bar: int = -1
    reverse_bars: int = 0


def market_stamp(value=None):
    stamp = pd.Timestamp(value if value is not None else datetime.now().astimezone())
    return stamp.tz_convert("Asia/Kolkata").tz_localize(None) if stamp.tzinfo else stamp


def epoch(value):
    return int(market_stamp(value).tz_localize("Asia/Kolkata").timestamp())


def minutes(timeframe):
    return int(str(timeframe).lower().replace("min", "").replace("m", "") or 5)


def sign(value):
    return 0 if not math.isfinite(value) or value == 0 else (1 if value > 0 else -1)


def crossed(previous, current, side):
    return math.isfinite(previous) and math.isfinite(current) and (
        previous <= 0 < current if side > 0 else previous >= 0 > current)


def initial_stop(entry, side, pct=.2, tick=TICK):
    raw = entry * (1 - side * pct / 100)
    return round((math.floor(raw / tick + 1e-10) if side > 0
                  else math.ceil(raw / tick - 1e-10)) * tick, 10)


def target_multiple(choice, p):
    if choice == "None":
        return math.nan
    level = int(choice[1:])
    return p.rr1 if level == 1 else p.rr2 if level == 2 else p.rr3 + (level - 3) * p.rr_increment


class Timeline:
    """Pine's varip state and the shared execution overlay for one index/frame."""
    def __init__(self, params, instrument, timeframe):
        self.p, self.instrument, self.timeframe = params, instrument, timeframe
        self.tick = .01 if str(instrument).upper() == "SENSEX" else TICK
        self.positions = {name: Position() for name in ("UT", "EMA")}
        self.pending = {name: 0 for name in self.positions}
        self.pending_exit = {name: "" for name in self.positions}
        self.tick_side = {name: 0 for name in self.positions}
        self.rsi_side = {name: 0 for name in self.positions}
        self.previous_tick = {name: math.nan for name in self.positions}
        self.signals_this_bar = {name: 0 for name in self.positions}
        self.marks, self.shared_events, self.records = [], [], {}
        self.basket = None
        self.bar_time = self.observed_at = None
        self.bar_index, self.live = -1, False
        self.position_history = []
        self.level_history = {key: [] for key in ("UT stop", "EMA stop", "UT target", "EMA target")}

    def selected_target(self, name):
        return getattr(self.p, name.lower() + "_target_exit")

    def target(self, name):
        pos = self.positions[name]
        multiple = target_multiple(self.selected_target(name), self.p)
        return pos.entry + pos.direction * pos.risk * multiple if pos.direction else math.nan

    def emit(self, name, side, entry, price, reason):
        event = {"engine": name, "side": side, "entry": entry, "price": float(price),
                 "reason": reason, "bar_index": self.bar_index,
                 "bar_time": self.bar_time.isoformat(), "observed_at": self.observed_at.isoformat(),
                 "realtime": self.live, "sequence": len(self.marks)}
        self.marks.append(event)
        if not entry:
            if self.basket is not None:
                record = self.basket
                record.update(exit_time=event["observed_at"], exit_bar=event["bar_time"],
                              exit_price=float(price), exit_reason=name + " " + reason)
                self.shared_events.append({**event, "action": "EXIT", "lifecycle_id": record["lifecycle_id"],
                                           "side": record["side"]})
                self.basket = None
            return
        if self.basket is None:
            ident = f"UTEMA|{self.instrument}|{self.timeframe}|{event['bar_time']}|{side}|{event['sequence']}"
            stop = initial_stop(price, side, self.p.base_stop_pct, self.tick)
            self.basket = {"lifecycle_id": ident, "side": side, "entry_engine": name,
                           "entry_spot": float(price), "entry_time": event["observed_at"],
                           "entry_bar": event["bar_time"], "initial_stop": stop, "stop": stop,
                           "alignment_lot": 0, "aligned_engine": "", "stop_updates": [],
                           "exit_time": "", "exit_price": 0., "exit_reason": ""}
            self.records[ident] = self.basket
            self.shared_events.append({**event, "action": "ENTRY", "lifecycle_id": ident})
            other = "EMA" if name == "UT" else "UT"
            if self.positions[other].direction == side:
                self.basket.update(alignment_lot=1, aligned_engine=other, alignment_time=event["observed_at"])
                self.shared_events.append({**event, "action": "ADD", "lifecycle_id": ident})
        elif (self.basket["side"] == side and self.basket["entry_engine"] != name
              and not self.basket["alignment_lot"]):
            self.basket.update(alignment_lot=1, aligned_engine=name, alignment_time=event["observed_at"])
            self.shared_events.append({**event, "action": "ADD", "lifecycle_id": self.basket["lifecycle_id"]})

    def enter(self, name, side, price):
        pos = self.positions[name]
        if not side or pos.direction or not math.isfinite(price) or price <= 0:
            return False
        stop = initial_stop(price, side, self.p.base_stop_pct, self.tick)
        if stop <= 0 or side * (price - stop) <= 0:
            return False
        pos.direction, pos.entry, pos.stop = side, price, stop
        pos.smart_stop, pos.risk, pos.extreme = math.nan, abs(price - stop), price
        pos.extreme_bar, pos.reverse_bars = self.bar_index, 0
        self.emit(name, side, True, price, "ENTRY")
        return True

    def exit(self, name, price, reason):
        pos = self.positions[name]
        if not pos.direction:
            return False
        self.emit(name, pos.direction, False, price, reason)
        self.positions[name] = Position()
        return True

    def stop_reason(self, name):
        pos = self.positions[name]
        return "SMART SL" if math.isfinite(pos.smart_stop) and abs(pos.stop - pos.smart_stop) < self.tick * .5 else "STOP"

    def open_reason(self, name, op, flip, pending):
        pos, target = self.positions[name], self.target(name)
        if pos.direction * (op - pos.stop) <= 0:
            return self.stop_reason(name)
        if math.isfinite(target) and pos.direction * (op - target) >= 0:
            return self.selected_target(name)
        return "FLIP" if flip else pending

    def levels(self, name, op, high, low, close, tick=False):
        pos = self.positions[name]
        if not pos.direction:
            return False
        target = self.target(name)
        if tick:
            stop_hit = pos.direction * (close - pos.stop) <= 0
            target_hit = math.isfinite(target) and pos.direction * (close - target) >= 0
            return self.exit(name, close, self.stop_reason(name) if stop_hit else self.selected_target(name)) if stop_hit or target_hit else False
        if pos.direction * (op - pos.stop) <= 0:
            return self.exit(name, op, self.stop_reason(name))
        if math.isfinite(target) and pos.direction * (op - target) >= 0:
            return self.exit(name, op, self.selected_target(name))
        adverse, favorable = (low, high) if pos.direction > 0 else (high, low)
        if pos.direction * (adverse - pos.stop) <= 0:
            return self.exit(name, pos.stop, self.stop_reason(name))
        if math.isfinite(target) and pos.direction * (favorable - target) >= 0:
            return self.exit(name, target, self.selected_target(name))
        return False

    def update_extreme(self, name, high, low, close, tick):
        pos = self.positions[name]
        value = close if tick else high if pos.direction > 0 else low
        if pos.direction and (not math.isfinite(pos.extreme) or pos.direction * (value - pos.extreme) > 0):
            pos.extreme, pos.extreme_bar, pos.reverse_bars = value, self.bar_index, 0

    def smart_reason(self, name, close, previous_close, atr, tick):
        pos, prefix, p = self.positions[name], name.lower(), self.p
        if not pos.direction:
            return ""
        move = 100 * (pos.extreme - pos.entry) * pos.direction / pos.entry
        retrace = 100 * (pos.extreme - close) * pos.direction / pos.extreme
        breach = False
        if getattr(p, prefix + "_smart_stop_on") and move >= getattr(p, prefix + "_smart_stop_arm"):
            distance = (atr * getattr(p, prefix + "_smart_stop_atr")
                if getattr(p, prefix + "_smart_stop_method") == "ATR"
                else pos.extreme * getattr(p, prefix + "_smart_stop_pct") / 100)
            if math.isfinite(distance) and distance > 0:
                proposed = pos.extreme - pos.direction * distance
                if pos.direction * (proposed - pos.stop) > self.tick * .001:
                    breach = pos.direction * (close - proposed) <= 0
                    if not breach:
                        pos.stop = pos.smart_stop = proposed
        enabled = getattr(p, prefix + "_peak_exit_on")
        older = tick or self.bar_index > pos.extreme_bar
        adverse = math.isfinite(previous_close) and pos.direction * (close - previous_close) < 0
        if (enabled and older and move >= getattr(p, prefix + "_peak_min_move")
                and retrace >= getattr(p, prefix + "_peak_pullback") and adverse):
            pos.reverse_bars += 1
        else:
            pos.reverse_bars = 0
        peak = enabled and pos.reverse_bars >= getattr(p, prefix + "_peak_bars")
        return "SMART SL" if breach else "SMART EXIT" if peak else ""

    def trail(self, name, close, ut_trail, atr):
        pos = self.positions[name]
        proposed = math.nan
        if name == "UT" and math.isfinite(ut_trail) and pos.direction * (close - ut_trail) > 0:
            proposed = ut_trail
        elif name == "EMA" and self.p.ema_legacy_trail and math.isfinite(atr):
            proposed = close - pos.direction * self.p.ema_trail_factor * atr
        if pos.direction and math.isfinite(proposed):
            pos.stop = max(pos.stop, proposed) if pos.direction > 0 else min(pos.stop, proposed)

    def sync_shared_stop(self):
        if self.basket is None:
            return
        side = self.basket["side"]
        stops = [p.stop for p in self.positions.values() if p.direction == side]
        if not stops:
            return
        stop = max(stops + [self.basket["stop"]]) if side > 0 else min(stops + [self.basket["stop"]])
        if stop != self.basket["stop"]:
            self.basket["stop"] = stop
            self.basket["stop_updates"].append({"time": self.observed_at.isoformat(), "stop": stop})

    def settle_pending(self, op):
        for name in ("UT", "EMA"):
            pos, pending = self.positions[name], self.pending[name]
            flip = bool(pending and pos.direction and pos.direction != pending)
            if pos.direction and (flip or self.pending_exit[name]):
                self.exit(name, op, self.open_reason(name, op, flip, self.pending_exit[name]))
            if pending and not self.positions[name].direction:
                self.enter(name, pending, op)
            self.pending[name], self.pending_exit[name] = 0, ""

    def step(self, i, frame, indicators, *, tick=False, new_bar=False, observed_at=None):
        current = {key: float(values[i]) for key, values in indicators.items()}
        previous = {key: float(values[i-1]) if i else math.nan for key, values in indicators.items()}
        row = tuple(float(frame[column].iloc[i]) for column in ("open", "high", "low", "close"))
        previous_close = float(frame.close.iloc[i-1]) if i else math.nan
        self.step_values(i, frame.index[i], row, current, previous, previous_close,
                         tick=tick, new_bar=new_bar, observed_at=observed_at)

    def replay(self, frame, indicators, start, count):
        """Read OHLC once; retain exactly the scalar timeline transition order."""
        rows = frame.to_numpy(dtype=float)
        times = frame.index
        closes_at = times + pd.Timedelta(minutes=minutes(self.timeframe))
        for i in range(start, count):
            current = {key: float(values[i]) for key, values in indicators.items()}
            previous = {key: float(values[i-1]) if i else math.nan for key, values in indicators.items()}
            self.step_values(i, times[i], tuple(float(value) for value in rows[i]),
                current, previous, float(rows[i-1, 3]) if i else math.nan,
                observed_at=closes_at[i])

    def step_values(self, i, bar_time, row, current, previous, previous_close,
                    *, tick=False, new_bar=False, observed_at=None):
        """Apply one observation without scanning historical indicator arrays."""
        self.bar_index, self.bar_time = i, bar_time
        self.live = tick
        self.observed_at = market_stamp(observed_at if observed_at is not None else bar_time)
        op, high, low, close = row
        previous_values = previous
        p, next_open = self.p, self.p.execution_mode != "Signal Bar Close"
        raw = {}
        if tick and new_bar:
            for name in ("UT", "EMA"):
                self.signals_this_bar[name] = 0
                self.tick_side[name] = sign(previous[name + "_diff"]) if i else 0
                self.rsi_side[name] = sign(previous[name + "_rsi"] - 50) if i else 0
                self.previous_tick[name] = math.nan
        if (not tick and next_open) or (tick and new_bar):
            saved = self.observed_at
            # Historical pending instructions execute at the next bar open.
            # Realtime settlement occurs on the first actual update: retain its
            # observation clock even though Pine uses this bar's OPEN price.
            if not tick:
                self.observed_at = self.bar_time
            self.settle_pending(op)
            self.observed_at = saved
        exited = {name: self.levels(name, op, high, low, close, tick) for name in ("UT", "EMA")}
        reasons = {}
        for name in ("UT", "EMA"):
            pos = self.positions[name]
            atr = current["ut_atr" if name == "UT" else "ema_atr"]
            self.update_extreme(name, high, low, close, tick)
            if pos.direction:
                self.trail(name, close, current["ut_trail"], atr)
            reasons[name] = self.smart_reason(name, close,
                self.previous_tick[name] if tick else previous_close, atr, tick)
            rsi_diff = current[name + "_rsi"] - 50
            old_rsi = self.rsi_side[name] if tick else previous[name + "_rsi"] - 50 if i else math.nan
            rsi_exit = (old_rsi >= 0 > sign(rsi_diff) if pos.direction > 0 else old_rsi <= 0 < sign(rsi_diff)) if tick else crossed(old_rsi, rsi_diff, -pos.direction)
            if not reasons[name] and pos.direction and rsi_exit:
                reasons[name] = "RSI-50"
            diff = current[name + "_diff"]
            if tick:
                new_side = sign(diff)
                raw[name] = new_side if new_side and self.tick_side[name] and new_side != self.tick_side[name] else 0
                if new_side:
                    self.tick_side[name] = new_side
                self.rsi_side[name], self.previous_tick[name] = sign(rsi_diff), close
            else:
                prior_diff = previous_values[name + "_diff"] if i else math.nan
                raw[name] = 1 if crossed(prior_diff, diff, 1) else -1 if crossed(prior_diff, diff, -1) else 0
        if tick:
            # Both exit checks precede both crossover entries in the supplied Pine.
            for name in ("UT", "EMA"):
                if reasons[name] and self.positions[name].direction:
                    self.exit(name, close, reasons[name])
                    exited[name] = True
            for name in ("UT", "EMA"):
                side = raw[name]
                if side and self.signals_this_bar[name] < p.intrabar_max_signals:
                    self.signals_this_bar[name] += 1
                    if not exited[name]:
                        if self.positions[name].direction and self.positions[name].direction != side:
                            self.exit(name, close, "FLIP")
                        if not self.positions[name].direction:
                            self.enter(name, side, close)
        elif next_open:
            for name in ("UT", "EMA"):
                pos, side = self.positions[name], raw[name]
                self.pending[name] = side
                self.pending_exit[name] = "" if side and pos.direction and side != pos.direction else reasons[name]
        else:
            for name in ("UT", "EMA"):
                pos, side = self.positions[name], raw[name]
                flip = side and pos.direction and side != pos.direction
                if pos.direction and (flip or reasons[name]):
                    self.exit(name, close, "FLIP" if flip else reasons[name])
                if side and not self.positions[name].direction:
                    self.enter(name, side, close)
        self.sync_shared_stop()
        self.snapshot_levels(i)

    def snapshot_levels(self, i):
        """Publish held levels without evaluating a forming candle's signals."""
        while len(self.position_history) <= i:
            self.position_history.append(0)
            for values in self.level_history.values():
                values.append(None)
        self.position_history[i] = self.basket["side"] if self.basket else 0
        for name in ("UT", "EMA"):
            self.level_history[name + " stop"][i] = self.positions[name].stop if self.positions[name].direction else None
            self.level_history[name + " target"][i] = self.target(name) if self.positions[name].direction else None


def _native_rsi(source, length):
    """Pine RSI arithmetic: a flat gain/loss seed is undefined, not 100."""
    change = np.full(len(source), np.nan)
    change[1:] = np.diff(source)
    up, down = np.maximum(change, 0.), np.maximum(-change, 0.)
    average_up, average_down = _rma(up, length), _rma(down, length)
    with np.errstate(divide="ignore", invalid="ignore"):
        return 100. - 100. / (1. + average_up / average_down)


@dataclass
class _RMAState:
    period: int
    count: int = 0
    total: float = 0.
    value: float = math.nan

    def preview(self, value):
        if not math.isfinite(value):
            return self.value
        if math.isfinite(self.value):
            return value / self.period + (1. - 1. / self.period) * self.value
        return (self.total + value) / self.period if self.count + 1 == self.period else math.nan

    def commit(self, value):
        result = self.preview(value)
        if math.isfinite(value) and not math.isfinite(self.value):
            self.count += 1
            self.total += value
        self.value = result
        return result


class _IndicatorState:
    """Committed closed-bar values; previews never change their baseline."""
    def __init__(self, params):
        self.p = params
        self.ema_fast = self.ema_slow = math.nan
        self.previous_close = self.previous_source = self.ut_trail = math.nan
        self.rma = {"ut_atr": _RMAState(params.ut_atr_len), "ema_atr": _RMAState(14),
                    "ut_up": _RMAState(14), "ut_down": _RMAState(14),
                    "ema_up": _RMAState(14), "ema_down": _RMAState(14)}
        self.previous = {name: math.nan for name in (
            "ut_atr", "ema_atr", "ut_trail", "UT_diff", "EMA_diff", "UT_rsi", "EMA_rsi")}

    @staticmethod
    def rsi(gain, loss):
        if not math.isfinite(gain) or not math.isfinite(loss) or gain == loss == 0:
            return math.nan
        return 100. if loss == 0 else 100. - 100. / (1. + gain / loss)

    def calculate(self, row, commit=False):
        op, high, low, close = row
        source = (op + high + low + close) / 4 if self.p.ut_heikin else close
        true_range = max(high-low, abs(high-self.previous_close), abs(low-self.previous_close)) if math.isfinite(self.previous_close) else high-low
        changes = {"ut": source-self.previous_source, "ema": close-self.previous_close}
        inputs = {"ut_atr": true_range, "ema_atr": true_range}
        for name, value in changes.items():
            inputs[name+"_up"] = max(value, 0.) if math.isfinite(value) else math.nan
            inputs[name+"_down"] = max(-value, 0.) if math.isfinite(value) else math.nan
        values = {name: state.commit(inputs[name]) if commit else state.preview(inputs[name]) for name, state in self.rma.items()}
        fast = close if not math.isfinite(self.ema_fast) else 2./(self.p.ema_fast_len+1)*close + (1.-2./(self.p.ema_fast_len+1))*self.ema_fast
        slow = close if not math.isfinite(self.ema_slow) else 2./(self.p.ema_slow_len+1)*close + (1.-2./(self.p.ema_slow_len+1))*self.ema_slow
        loss, prior = self.p.ut_factor*values["ut_atr"], self.ut_trail
        if not math.isfinite(prior):
            trail = source-loss
        elif source > prior and self.previous_source > prior:
            trail = max(prior, source-loss)
        elif source < prior and self.previous_source < prior:
            trail = min(prior, source+loss)
        else:
            trail = source-loss if source > prior else source+loss
        result = {"ut_atr": values["ut_atr"], "ema_atr": values["ema_atr"], "ut_trail": trail,
                  "UT_diff": source-trail, "EMA_diff": fast-slow,
                  "UT_rsi": self.rsi(values["ut_up"], values["ut_down"]),
                  "EMA_rsi": self.rsi(values["ema_up"], values["ema_down"])}
        if commit:
            self.ema_fast, self.ema_slow = fast, slow
            self.previous_close, self.previous_source, self.ut_trail = close, source, trail
            self.previous = result
        return result

    def checkpoint(self):
        return {"ema_fast": self.ema_fast, "ema_slow": self.ema_slow,
                "previous_close": self.previous_close, "previous_source": self.previous_source,
                "ut_trail": self.ut_trail, "previous": self.previous,
                "rma": {name: asdict(state) for name, state in self.rma.items()}}

    @classmethod
    def restore(cls, params, payload):
        state = cls(params)
        for name in ("ema_fast", "ema_slow", "previous_close", "previous_source", "ut_trail"):
            value = payload[name]
            setattr(state, name, math.nan if value is None else float(value))
        state.previous = {name: math.nan if payload["previous"][name] is None else float(payload["previous"][name]) for name in state.previous}
        for name, original in state.rma.items():
            item = payload["rma"][name]
            if int(item["period"]) != original.period or not 0 <= int(item["count"]) <= original.period:
                raise ValueError("Invalid UT/EMA RMA checkpoint")
            state.rma[name] = _RMAState(original.period, int(item["count"]), float(item["total"]), math.nan if item["value"] is None else float(item["value"]))
        return state


def compute_indicators(frame, p):
    op, hi, lo, cl = (frame[c].to_numpy(dtype=float) for c in ("open", "high", "low", "close"))
    src = (op + hi + lo + cl) / 4 if p.ut_heikin else cl
    tr = _true_range(hi, lo, cl)
    atr = _rma(tr, p.ut_atr_len)
    trail = np.full(len(frame), np.nan)
    for i in range(len(frame)):
        loss = p.ut_factor * atr[i]
        prior = trail[i-1] if i else math.nan
        if not math.isfinite(prior):
            trail[i] = src[i] - loss
        elif src[i] > prior and src[i-1] > prior:
            trail[i] = max(prior, src[i] - loss)
        elif src[i] < prior and src[i-1] < prior:
            trail[i] = min(prior, src[i] + loss)
        else:
            trail[i] = src[i] - loss if src[i] > prior else src[i] + loss
    return {"ut_atr": atr, "ema_atr": _rma(tr, 14), "ut_trail": trail,
            "UT_diff": src - trail, "EMA_diff": _ema(cl, p.ema_fast_len) - _ema(cl, p.ema_slow_len),
            "UT_rsi": _native_rsi(src, 14), "EMA_rsi": _native_rsi(cl, 14)}


class UTEmaEngine:
    signal_source, signal_mode, min_bars = ENGINE_KEY, "realtime", 2

    def __init__(self, params=None, **kwargs):
        self.params = params or Params()
        self._timeline = None
        self._processed = 0
        self._live_bar = None
        self._prefix_hash = ""
        self._last_result = None
        self._last_result_key = None
        self._last_result_at = None
        self._last_tick = None
        self._stream_ready = False
        self._stream_dirty = True
        self._stream_times = []
        self._stream_rows = []
        self._stream_values = {}
        self._stream_indices = {}
        self._stream_current = None
        self._stream_seed_context = None
        self._stream_baseline = None
        self._stream_clock = None
        self._stream_gap = ""

    def set_signal_fire_mode(self, mode):
        execution = "Realtime Intrabar" if mode == "INTRACANDLE" else "Signal Bar Close"
        if self.params.execution_mode == execution:
            return False
        self.params = replace(self.params, execution_mode=execution)
        if self._timeline is not None:
            self._timeline.p = self.params
            self._timeline.pending = {name: 0 for name in ("UT", "EMA")}
            self._timeline.pending_exit = {name: "" for name in ("UT", "EMA")}
            if self._stream_times:
                for name in ("UT", "EMA"):
                    self._timeline.tick_side[name] = sign(self._stream_values[name + "_diff"][-1])
                    self._timeline.rsi_side[name] = sign(self._stream_values[name + "_rsi"][-1] - 50)
                    self._timeline.previous_tick[name] = self._stream_rows[-1][3]
        self._stream_dirty = True
        self._last_result = None
        return True

    @staticmethod
    def prepare(df):
        if df is None or len(df) == 0:
            return pd.DataFrame(columns=["open", "high", "low", "close"], index=pd.DatetimeIndex([]))
        if not isinstance(df.index, pd.DatetimeIndex):
            raise ValueError("ut/ema requires timestamp-indexed OHLC")
        out = df.loc[:, ["open", "high", "low", "close"]].astype(float).copy()
        if out.index.tz is not None:
            out.index = out.index.tz_convert("Asia/Kolkata").tz_localize(None)
        if out.index.has_duplicates or not out.index.is_monotonic_increasing:
            raise ValueError("ut/ema requires ordered unique candles")
        if not np.isfinite(out.to_numpy()).all() or (out.to_numpy() <= 0).any():
            raise ValueError("ut/ema requires positive finite OHLC")
        if ((out.high < out[["open", "close", "low"]].max(axis=1)).any()
                or (out.low > out[["open", "close", "high"]].min(axis=1)).any()):
            raise ValueError("ut/ema requires consistent OHLC ranges")
        return out

    @staticmethod
    def fingerprint(frame):
        digest = hashlib.blake2b(digest_size=12)
        digest.update(frame.index.asi8.tobytes())
        digest.update(frame.to_numpy(dtype=float).tobytes())
        return digest.hexdigest()

    def process(self, df, instrument="", timeframe="5min", as_of=None, realtime=None, **kwargs):
        frame = self.prepare(df)
        clock = market_stamp(as_of)
        frame = frame[frame.index <= clock]
        if realtime is None:
            from config.settings import get_settings
            realtime = str(getattr(get_settings(), "trading_mode", "HISTORICAL")).upper() == "REAL"
        live = bool(realtime and self.params.execution_mode == "Realtime Intrabar")
        if not live:
            closed = np.asarray(frame.index + pd.Timedelta(minutes=minutes(timeframe)) <= clock)
            count = int(closed.sum())
            replay_key = (instrument, timeframe, self.params, self.fingerprint(frame.iloc[:count]), count)
            if replay_key == getattr(self, "_historical_cache_key", None):
                return _copy_output(self._historical_cache_result)
            indicators = compute_indicators(frame, self.params)
            prior_key = getattr(self, "_historical_cache_key", None)
            previous_frame = getattr(self, "_historical_closed_frame", None)
            prior_count = len(previous_frame) if previous_frame is not None else 0
            append_only = (prior_key is not None and prior_key[:3] == replay_key[:3]
                and prior_count < count
                and frame.index[:prior_count].equals(previous_frame.index)
                and np.array_equal(frame.iloc[:prior_count].to_numpy(), previous_frame.to_numpy()))
            timeline = self._historical_timeline if append_only else Timeline(self.params, instrument, timeframe)
            timeline.replay(frame, indicators, prior_count if append_only else 0, count)
            result = self.result(timeline, frame, indicators, count)
            self._historical_cache_key, self._historical_cache_result = replay_key, _copy_output(result)
            self._historical_timeline = timeline
            self._historical_closed_frame = frame.iloc[:count].copy()
            return result
        identity = (instrument, timeframe)
        current_identity = (self._timeline.instrument, self._timeline.timeframe) if self._timeline else None
        corrected = False
        if self._stream_ready and current_identity == identity:
            # A rolling window is a suffix of known observations, not a history
            # correction. Compare timestamps rather than absolute array offsets.
            for stamp, row in zip(frame.index, frame.to_numpy(dtype=float)):
                i = self._stream_indices.get(stamp)
                if i is not None and i != self._stream_current:
                    incoming = tuple(float(value) for value in row)
                    if incoming != self._stream_rows[i]:
                        corrected = True
                        break
        if not self._stream_ready or current_identity != identity or corrected:
            self.seed_stream(frame, instrument, timeframe, as_of=clock)
            if corrected:
                self._stream_gap = "closed_history_correction"
        period = pd.Timedelta(minutes=minutes(timeframe))
        for stamp, row in zip(frame.index, frame.to_numpy(dtype=float)):
            values = tuple(float(value) for value in row)
            i = self._stream_indices.get(stamp)
            if i is not None and i != self._stream_current:
                continue
            if self._stream_current is not None and stamp < self._stream_times[self._stream_current]:
                continue
            if stamp + period <= clock:
                if self._stream_current is not None and stamp == self._stream_times[self._stream_current]:
                    if values != self._stream_rows[self._stream_current]:
                        self._stream_gap = "unobserved_bar_close"
                    # An elapsed clock is not another market update. Commit the
                    # final supplied indicators without inventing a native tick.
                    self._stream_rows[self._stream_current] = values
                    self._commit_current()
                elif i is None:
                    self._append_historical(stamp, values)
                    self._stream_gap = "unobserved_closed_bar"
            else:
                self._observe_row(stamp, values, clock)
        self._stream_clock = clock
        return self.stream_result()

    def seed_stream(self, df, instrument="", timeframe="5min", as_of=None, *,
                    session_open=None, session_close=None):
        """Seed closed history without treating an aggregated forming bar as a tick."""
        frame, clock = self.prepare(df), market_stamp(as_of)
        frame = frame[frame.index <= clock]
        closed = frame.index + pd.Timedelta(minutes=minutes(timeframe)) <= clock
        if session_close is not None and clock == market_stamp(session_close):
            # The exchange's closing update belongs to the final realtime bar.
            # Do not commit that bar as history before its first actual tick.
            final_bar = self._tick_bar(clock, timeframe, session_open=session_open,
                                       session_close=session_close)
            closed &= frame.index != final_bar
        historical = frame.loc[closed]
        self._timeline = Timeline(self.params, instrument, timeframe)
        self._stream_baseline = _IndicatorState(self.params)
        self._stream_times, self._stream_rows, self._stream_indices = [], [], {}
        self._stream_values = {key: [] for key in self._stream_baseline.previous}
        self._stream_current = self._stream_seed_context = None
        self._stream_ready, self._stream_dirty, self._stream_gap = True, True, ""
        self._last_tick, self._last_result = None, None
        self._processed, self._live_bar = 0, None
        for stamp, row in zip(historical.index, historical.to_numpy(dtype=float)):
            self._append_historical(stamp, tuple(float(value) for value in row))
        if len(frame) > len(historical):
            row = frame.iloc[-1]
            self._stream_seed_context = {"time": frame.index[-1], "open": float(row.open)}
            if self.params.execution_mode == "Signal Bar Close":
                self._stream_seed_context.update(high=float(row.high), low=float(row.low))
        self._stream_clock = clock
        self._prefix_hash = self.fingerprint(historical)
        return self.stream_result()

    def _append_historical(self, stamp, row):
        if self._stream_current is not None:
            self._commit_current()
        i = len(self._stream_times)
        previous = dict(self._stream_baseline.previous)
        previous_close = self._stream_baseline.previous_close
        current = self._stream_baseline.calculate(row, commit=True)
        self._stream_times.append(stamp)
        self._stream_rows.append(row)
        self._stream_indices[stamp] = i
        for key, value in current.items():
            self._stream_values[key].append(value)
        self._timeline.step_values(i, stamp, row, current, previous, previous_close,
            observed_at=stamp + pd.Timedelta(minutes=minutes(self._timeline.timeframe)))
        self._processed = len(self._stream_times)
        self._stream_dirty = True

    def _commit_current(self, observed_at=None):
        i = self._stream_current
        if i is None:
            return
        previous = dict(self._stream_baseline.previous)
        previous_close = self._stream_baseline.previous_close
        current = self._stream_baseline.calculate(self._stream_rows[i], commit=True)
        if self.params.execution_mode == "Signal Bar Close":
            count = len(self._timeline.marks)
            stamp = self._stream_times[i]
            close_clock = stamp + pd.Timedelta(minutes=minutes(self._timeline.timeframe))
            if observed_at is not None and market_stamp(observed_at).date() == stamp.date():
                close_clock = observed_at
            self._timeline.step_values(i, stamp, self._stream_rows[i], current, previous,
                previous_close, observed_at=close_clock)
            for event in self._timeline.marks[count:]:
                event["confirmed"] = True
            # Shared events are copied by emit; retain the same provenance there.
            for event in self._timeline.shared_events:
                if event["sequence"] >= count:
                    event["confirmed"] = True
        for key, value in current.items():
            self._stream_values[key][i] = value
        self._processed = len(self._stream_times)
        self._stream_current = None
        self._stream_dirty = True

    def _observe_row(self, stamp, row, observed_at):
        count = len(self._timeline.marks)
        if self._stream_current is not None:
            current_time = self._stream_times[self._stream_current]
            if stamp < current_time:
                return False
            if stamp > current_time:
                self._commit_current(observed_at=observed_at)
        new_bar = self._stream_current is None
        if new_bar:
            if self._stream_times and stamp <= self._stream_times[-1]:
                return False
            i = len(self._stream_times)
            self._stream_current = i
            self._stream_times.append(stamp)
            self._stream_rows.append(row)
            self._stream_indices[stamp] = i
            for values in self._stream_values.values():
                values.append(math.nan)
        else:
            i = self._stream_current
        tick_key = (stamp.isoformat(), market_stamp(observed_at).isoformat(), tuple(row))
        if tick_key == self._last_tick:
            return False
        self._stream_rows[i] = row
        current = self._stream_baseline.calculate(row)
        for key, value in current.items():
            self._stream_values[key][i] = value
        previous = self._stream_baseline.previous
        if self.params.execution_mode != "Signal Bar Close":
            self._timeline.step_values(i, stamp, row, current, previous,
                self._stream_baseline.previous_close, tick=True, new_bar=new_bar, observed_at=observed_at)
        else:
            self._timeline.bar_index, self._timeline.bar_time = i, stamp
            self._timeline.observed_at, self._timeline.live = market_stamp(observed_at), False
            self._timeline.snapshot_levels(i)
        self._live_bar, self._last_tick = stamp, tick_key
        self._stream_clock, self._stream_dirty = market_stamp(observed_at), True
        self._stream_seed_context = None
        return len(self._timeline.marks) != count

    @staticmethod
    def _tick_bar(clock, timeframe, session_open=None, session_close=None):
        # Pine gates on timeframe.isintraday, not fixed NSE clock hours. The host
        # validates exchange sessions; an explicit open supports special sessions.
        start = market_stamp(session_open) if session_open is not None else clock.normalize() + pd.Timedelta(hours=9, minutes=15)
        period = pd.Timedelta(minutes=minutes(timeframe))
        if session_close is not None and clock == market_stamp(session_close):
            clock -= pd.Timedelta(nanoseconds=1)
        return start + ((clock-start)//period)*period

    def _expected_stream_bar(self, previous, observed=None, session_open=None, previous_session_close=None):
        period = pd.Timedelta(minutes=minutes(self._timeline.timeframe))
        expected = previous+period
        opening = market_stamp(session_open) if session_open is not None else None
        supplied_close = market_stamp(previous_session_close) if previous_session_close is not None else None
        if observed is None or observed.date() == previous.date():
            if opening is not None and supplied_close is not None and observed == opening and expected == supplied_close <= opening:
                return opening
            return expected
        close = supplied_close if supplied_close is not None else previous.normalize() + pd.Timedelta(hours=15, minutes=30)
        # A host-declared special session can end within its final bar. That
        # partial bar is a complete tail; a missing whole bar remains a gap.
        complete_tail = expected == close or (supplied_close is not None and previous < close < expected)
        if not complete_tail:
            raise StreamDataGap("UT/EMA previous session tail is incomplete")
        from data.market_calendar import load_nse_holidays, load_nse_special_trading_days
        holidays, special = load_nse_holidays(), load_nse_special_trading_days()
        day = previous.normalize() + pd.Timedelta(days=1)
        for _ in range(32):
            label = day.date().isoformat()
            if label in special or (day.weekday() < 5 and label not in holidays):
                if opening is not None:
                    if opening.date() != day.date() or opening > observed:
                        raise StreamDataGap("UT/EMA supplied session open skips an exchange day")
                    return opening
                return day + pd.Timedelta(hours=9, minutes=15)
            day += pd.Timedelta(days=1)
        raise StreamDataGap("UT/EMA next exchange session is unavailable")

    def observe_tick(self, price, observed_at, *, session_open=None, session_close=None, previous_session_close=None):
        """Observe an actual quote in constant history-independent time.

        The current bar previews indicators against immutable closed values. A
        new bar commits the previous last observed values exactly once, without
        a synthetic final-price update. Complete snapshots are materialized only
        for native events or when stream_result is explicitly requested.
        """
        if not self._stream_ready:
            raise ValueError("UT/EMA stream requires seed_stream or live process first")
        price, clock = float(price), market_stamp(observed_at)
        if not math.isfinite(price) or price <= 0 or pd.isna(clock):
            raise ValueError("UT/EMA stream requires a positive price and timestamp")
        if self._stream_clock is not None and clock < self._stream_clock:
            return None
        anchor = session_open
        if anchor is None:
            context = self._stream_seed_context
            if context and context["time"].date() == clock.date() and context["time"] <= clock:
                anchor = context["time"]
            elif self._stream_times and self._stream_times[-1].date() == clock.date():
                anchor = self._stream_times[-1]
        stamp = self._tick_bar(clock, self._timeline.timeframe, session_open=anchor, session_close=session_close)
        if stamp is None:
            return None
        if self._stream_times and stamp > self._stream_times[-1]:
            try:
                expected = self._expected_stream_bar(self._stream_times[-1], stamp, session_open=session_open, previous_session_close=previous_session_close)
                if stamp != expected:
                    raise StreamDataGap(f"UT/EMA index bars missing: expected {expected.isoformat()}, observed {stamp.isoformat()}")
            except StreamDataGap:
                self._stream_gap = "missing_index_bars"
                self._stream_dirty = True
                raise
        if self._stream_current is not None and stamp == self._stream_times[self._stream_current]:
            op, high, low, old_close = self._stream_rows[self._stream_current]
            row = (op, max(high, price), min(low, price), price)
        else:
            context = self._stream_seed_context
            op = float(context["open"]) if context and context["time"] == stamp else price
            row = (op, max(op, price), min(op, price), price)
            if (self.params.execution_mode == "Signal Bar Close" and context
                    and context["time"] == stamp):
                row = (op, max(row[1], context.get("high", op)),
                       min(row[2], context.get("low", op)), price)
        count = len(self._timeline.marks)
        event = self._observe_row(stamp, row, clock)
        if (self.params.execution_mode == "Signal Bar Close" and session_close is not None
                and clock == market_stamp(session_close)):
            self._commit_current(observed_at=clock)
            event = event or len(self._timeline.marks) != count
        return self.stream_result() if event else None

    def _stream_frame(self):
        return pd.DataFrame(self._stream_rows, columns=["open", "high", "low", "close"],
                            index=pd.DatetimeIndex(self._stream_times))

    def stream_result(self, *, copy_result=True):
        """Return the latest native snapshot; reading never consumes a market tick."""
        if not self._stream_ready:
            return _copy_output(self._last_result) if self._last_result is not None else None
        if self._stream_dirty or self._last_result is None:
            frame = self._stream_frame()
            self._last_result = self.result(self._timeline, frame, self._stream_values, len(frame))
            self._last_result.update(utema_stream=True,
                utema_live_path_available=not bool(self._stream_gap), utema_stream_gap=self._stream_gap)
            self._last_result_key = (self._timeline.instrument, self._timeline.timeframe,
                                     self.fingerprint(frame), self._processed)
            self._last_result_at = self._stream_clock
            self._stream_dirty = False
        # The adapter already owns the native lock and makes the public copy.
        return _copy_output(self._last_result) if copy_result else self._last_result

    @staticmethod
    def _checkpoint_json(value):
        if isinstance(value, dict):
            return {str(key): UTEmaEngine._checkpoint_json(item) for key, item in value.items()}
        if isinstance(value, (tuple, list)):
            return [UTEmaEngine._checkpoint_json(item) for item in value]
        if isinstance(value, (datetime, pd.Timestamp)):
            return market_stamp(value).isoformat()
        if isinstance(value, float):
            if math.isnan(value):
                return None
            if not math.isfinite(value):
                raise ValueError("UT/EMA checkpoint contains an infinite value")
        if value is None or isinstance(value, (str, bool, int, float)):
            return value
        raise ValueError("UT/EMA checkpoint contains an unsupported value")

    def stream_checkpoint(self):
        """Serialize native varip and incremental indicators without executable data."""
        if not self._stream_ready:
            return None
        timeline = self._timeline
        fields = ("pending", "pending_exit", "tick_side", "rsi_side", "previous_tick",
                  "signals_this_bar", "marks", "shared_events", "records", "bar_time",
                  "observed_at", "bar_index", "live", "position_history", "level_history")
        payload = {"schema": "utema.stream.v1", "params": asdict(self.params),
                   "instrument": timeline.instrument, "timeframe": timeline.timeframe,
                   "timeline": {key: getattr(timeline, key) for key in fields},
                   "positions": {name: asdict(position) for name, position in timeline.positions.items()},
                   "basket_id": timeline.basket["lifecycle_id"] if timeline.basket else None,
                   "indicator_state": self._stream_baseline.checkpoint(),
                   "times": self._stream_times, "rows": self._stream_rows,
                   "values": self._stream_values, "current": self._stream_current,
                   "seed_context": self._stream_seed_context, "clock": self._stream_clock,
                   "last_tick": self._last_tick, "gap": self._stream_gap}
        return self._checkpoint_json(payload)

    def restore_stream_checkpoint(self, payload):
        """Validate and restore one exact checkpoint. Invalid input never mutates state."""
        if not isinstance(payload, dict) or payload.get("schema") != "utema.stream.v1":
            raise ValueError("Unsupported UT/EMA checkpoint schema")
        if payload.get("params") != asdict(self.params):
            raise ValueError("UT/EMA checkpoint parameters differ")
        def validate_json(value):
            if isinstance(value, dict):
                if any(not isinstance(key, str) for key in value):
                    raise ValueError("UT/EMA checkpoint requires JSON string keys")
                for item in value.values():
                    validate_json(item)
            elif isinstance(value, list):
                for item in value:
                    validate_json(item)
            elif isinstance(value, float):
                if not math.isfinite(value):
                    raise ValueError("UT/EMA checkpoint requires null for undefined numbers")
            elif value is not None and not isinstance(value, (str, bool, int)):
                raise ValueError("UT/EMA checkpoint requires JSON values")
        validate_json(payload)
        try:
            # Reject non-JSON numbers and executable/custom Python objects first.
            encoded = self._checkpoint_json(payload)
            instrument, timeframe = encoded["instrument"], encoded["timeframe"]
            if not isinstance(instrument, str) or not instrument or timeframe not in {"1min", "5min", "15min"}:
                raise ValueError("Invalid UT/EMA checkpoint owner")
            if self._timeline is not None and (self._timeline.instrument, self._timeline.timeframe) != (instrument, timeframe):
                raise ValueError("UT/EMA checkpoint belongs to another owner")
            times = [market_stamp(value) for value in encoded["times"]]
            if len(times) > 100000 or any(pd.isna(value) for value in times) or any(b <= a for a, b in zip(times, times[1:])):
                raise ValueError("Invalid UT/EMA checkpoint times")
            rows = [tuple(float(value) for value in row) for row in encoded["rows"]]
            if len(rows) != len(times) or any(len(row) != 4 or not all(math.isfinite(value) and value > 0 for value in row)
                    or not row[2] <= min(row[0], row[3]) <= max(row[0], row[3]) <= row[1] for row in rows):
                raise ValueError("Invalid UT/EMA checkpoint prices")
            current = encoded["current"]
            if current is not None and (type(current) is not int or current != len(times)-1):
                raise ValueError("Invalid UT/EMA current bar")
            baseline = _IndicatorState.restore(self.params, encoded["indicator_state"])
            values = {key: [math.nan if value is None else float(value) for value in encoded["values"][key]] for key in baseline.previous}
            if any(len(value) != len(times) for value in values.values()):
                raise ValueError("Invalid UT/EMA checkpoint indicator lengths")
            timeline = Timeline(self.params, instrument, timeframe)
            data = encoded["timeline"]
            for name in ("UT", "EMA"):
                position = encoded["positions"][name]
                direction = position["direction"]
                if type(direction) is not int or direction not in {-1, 0, 1}:
                    raise ValueError("Invalid UT/EMA checkpoint position")
                restored = {key: (math.nan if value is None else value) for key, value in position.items()}
                timeline.positions[name] = Position(**restored)
                if direction and any(not math.isfinite(float(restored[key])) or float(restored[key]) <= 0 for key in ("entry", "stop", "risk", "extreme")):
                    raise ValueError("Invalid UT/EMA checkpoint protection")
            for key in ("pending", "pending_exit", "tick_side", "rsi_side", "signals_this_bar"):
                setattr(timeline, key, dict(data[key]))
            timeline.previous_tick = {key: math.nan if value is None else float(value) for key, value in data["previous_tick"].items()}
            if (set(timeline.previous_tick) != {"UT", "EMA"} or set(timeline.pending_exit) != {"UT", "EMA"}
                    or any(not isinstance(value, str) for value in timeline.pending_exit.values())):
                raise ValueError("Invalid UT/EMA checkpoint pending state")
            for key in ("pending", "tick_side", "rsi_side"):
                if set(getattr(timeline, key)) != {"UT", "EMA"} or any(type(value) is not int or value not in {-1, 0, 1} for value in getattr(timeline, key).values()):
                    raise ValueError("Invalid UT/EMA checkpoint side")
            if set(timeline.signals_this_bar) != {"UT", "EMA"} or any(type(value) is not int or not 0 <= value <= self.params.intrabar_max_signals for value in timeline.signals_this_bar.values()):
                raise ValueError("Invalid UT/EMA checkpoint counter")
            for key in ("marks", "shared_events", "records", "position_history", "level_history"):
                setattr(timeline, key, copy.deepcopy(data[key]))
            if (not isinstance(timeline.marks, list) or not isinstance(timeline.shared_events, list)
                    or not isinstance(timeline.records, dict) or len(timeline.marks) > 500000
                    or set(timeline.level_history) != {"UT stop", "EMA stop", "UT target", "EMA target"}):
                raise ValueError("Invalid UT/EMA checkpoint event collections")
            for sequence, mark in enumerate(timeline.marks):
                index = mark["bar_index"]
                if (mark["engine"] not in {"UT", "EMA"} or type(mark["entry"]) is not bool
                        or type(mark["side"]) is not int or mark["side"] not in {-1, 1}
                        or not math.isfinite(float(mark["price"])) or float(mark["price"]) <= 0
                        or type(index) is not int or not 0 <= index < len(times)
                        or mark["sequence"] != sequence or market_stamp(mark["bar_time"]) != times[index]
                        or not isinstance(mark["reason"], str) or type(mark["realtime"]) is not bool):
                    raise ValueError("Invalid UT/EMA checkpoint event")
            for identity, record in timeline.records.items():
                if (record["lifecycle_id"] != identity or not identity.startswith(f"UTEMA|{instrument}|{timeframe}|")
                        or record["side"] not in {-1, 1}
                        or record["entry_engine"] not in {"UT", "EMA"}
                        or any(not math.isfinite(float(record[key])) or float(record[key]) <= 0
                               for key in ("entry_spot", "initial_stop", "stop"))
                        or record["alignment_lot"] not in {0, 1}):
                    raise ValueError("Invalid UT/EMA checkpoint lifecycle")
                if pd.isna(market_stamp(record["entry_time"])) or pd.isna(market_stamp(record["entry_bar"])):
                    raise ValueError("Invalid UT/EMA checkpoint entry time")
                for update in record["stop_updates"]:
                    if pd.isna(market_stamp(update["time"])) or not math.isfinite(float(update["stop"])) or float(update["stop"]) <= 0:
                        raise ValueError("Invalid UT/EMA checkpoint stop history")
                if record.get("exit_time") and (pd.isna(market_stamp(record["exit_time"])) or float(record["exit_price"]) <= 0):
                    raise ValueError("Invalid UT/EMA checkpoint exit")
            for event in timeline.shared_events:
                if event["action"] not in {"ENTRY", "ADD", "EXIT"} or event["lifecycle_id"] not in timeline.records:
                    raise ValueError("Invalid UT/EMA checkpoint shared event")
            if (any(type(value) is not int or value not in {-1, 0, 1} for value in timeline.position_history)
                    or any(value is not None and (not math.isfinite(float(value)) or float(value) <= 0)
                           for values in timeline.level_history.values() for value in values)):
                raise ValueError("Invalid UT/EMA checkpoint plotted protection")
            timeline.bar_time = market_stamp(data["bar_time"]) if data["bar_time"] else None
            timeline.observed_at = market_stamp(data["observed_at"]) if data["observed_at"] else None
            timeline.bar_index, timeline.live = int(data["bar_index"]), bool(data["live"])
            if timeline.bar_index != len(times)-1 or len(timeline.position_history) != len(times) or any(len(value) != len(times) for value in timeline.level_history.values()):
                raise ValueError("Invalid UT/EMA checkpoint timeline lengths")
            basket_id = encoded["basket_id"]
            timeline.basket = timeline.records[basket_id] if basket_id is not None else None
            if timeline.basket and timeline.basket.get("exit_time"):
                raise ValueError("UT/EMA active basket already exited")
            if times and timeline.bar_time != times[-1]:
                raise ValueError("UT/EMA checkpoint current timestamp differs")
            context = encoded["seed_context"]
            if context:
                original_context = context
                context = {"time": market_stamp(context["time"]), "open": float(context["open"])}
                if not math.isfinite(context["open"]) or context["open"] <= 0:
                    raise ValueError("Invalid UT/EMA checkpoint seed context")
                if "high" in original_context or "low" in original_context:
                    high, low = float(original_context["high"]), float(original_context["low"])
                    if not all(math.isfinite(v) for v in (high, low)) or not 0 < low <= context["open"] <= high:
                        raise ValueError("Invalid UT/EMA checkpoint seed range")
                    context.update(high=high, low=low)
            clock = market_stamp(encoded["clock"])
            if pd.isna(clock):
                raise ValueError("Invalid UT/EMA checkpoint clock")
            raw_tick = encoded["last_tick"]
            tick = None
            if raw_tick is not None:
                if (not isinstance(raw_tick, list) or len(raw_tick) != 3
                        or not isinstance(raw_tick[2], list) or len(raw_tick[2]) != 4
                        or not all(math.isfinite(float(value)) and float(value) > 0 for value in raw_tick[2])):
                    raise ValueError("Invalid UT/EMA checkpoint last observation")
                tick_bar, tick_clock = market_stamp(raw_tick[0]), market_stamp(raw_tick[1])
                if tick_bar not in times or pd.isna(tick_clock) or not tick_bar <= tick_clock <= clock:
                    raise ValueError("Invalid UT/EMA checkpoint observation clock")
                tick = (raw_tick[0], raw_tick[1], tuple(float(value) for value in raw_tick[2]))
            if not isinstance(encoded.get("gap", ""), str):
                raise ValueError("Invalid UT/EMA checkpoint gap provenance")
        except (KeyError, TypeError, OverflowError, AttributeError) as exc:
            raise ValueError("Malformed UT/EMA checkpoint") from exc
        self._timeline, self._stream_baseline = timeline, baseline
        self._stream_times, self._stream_rows, self._stream_values = times, rows, values
        self._stream_indices = {stamp: i for i, stamp in enumerate(times)}
        self._stream_current, self._stream_seed_context = current, context
        self._stream_clock, self._stream_gap = clock, str(encoded.get("gap") or "")
        self._processed = current if current is not None else len(times)
        self._live_bar = times[current] if current is not None else None
        self._last_tick = tick
        self._stream_ready, self._stream_dirty = True, True
        self._last_result = None
        return True

    def chart_result(self, df, instrument="", timeframe="5min", **kwargs):
        # Reuse observed tick marks only for the same candles and identity. A
        # corrected history, another chart, or an earlier replay clock must not
        # display events from an unrelated or later live observation.
        clock = market_stamp(kwargs.get("as_of"))
        frame = self.prepare(df)
        frame = frame[frame.index <= clock]
        n_closed = int((frame.index + pd.Timedelta(minutes=minutes(timeframe)) <= clock).sum())
        key = (instrument, timeframe, self.fingerprint(frame), n_closed)
        matches = key == self._last_result_key
        if (not matches and self._stream_ready and self._timeline.instrument == instrument
                and self._timeline.timeframe == timeframe and len(frame) and self._stream_times
                and frame.index[-1] == self._stream_times[-1]):
            indices = [self._stream_indices.get(stamp) for stamp in frame.index]
            matches = (all(i is not None for i in indices) and
                np.array_equal(frame.to_numpy(dtype=float),
                               np.asarray([self._stream_rows[i] for i in indices], dtype=float)))
            matches = matches and n_closed == len(frame) - (self._stream_current is not None)
        if (self._last_result is not None and matches
                and self._last_result_at is not None and clock >= self._last_result_at):
            return _copy_output(self._last_result)
        kwargs.pop("realtime", None)
        return self.process(df, instrument, timeframe, realtime=False, **kwargs)

    def result(self, timeline, frame, indicators, count):
        signals = []
        for event in timeline.shared_events:
            record = _copy_output(timeline.records[event["lifecycle_id"]])
            i = event["bar_index"]
            price, side = event["price"], event["side"]
            signal = UTBotSignal(timestamp=market_stamp(event["bar_time"]).to_pydatetime(),
                signal_type="BUY" if side > 0 else "SELL", price=price,
                trailing_stop=record["initial_stop"], stop_distance=abs(record["entry_spot"] - record["initial_stop"]),
                atr_value=float(indicators["ut_atr"][i]) if math.isfinite(indicators["ut_atr"][i]) else 0.,
                adx_value=0., position_state=side if event["action"] != "EXIT" else 0,
                bar_index=i, instrument=timeline.instrument, timeframe=timeline.timeframe,
                source_engine=ENGINE_KEY, source_lane=LANE)
            signal.utema_state = record
            signal.utema_action, signal.utema_event = event["action"], _copy_output(event)
            signal.utema_event_id = f"{event['lifecycle_id']}|{event['action']}|{event['sequence']}"
            signal.native_exit_only = event["action"] == "EXIT"
            signals.append(signal)
        markers = []
        colors = {("UT", True, 1): "#149b64", ("UT", True, -1): "#c84146",
                  ("EMA", True, 1): "#2a74d7", ("EMA", True, -1): "#e17d1e"}
        for mark in timeline.marks:
            above = mark["side"] < 0 if mark["entry"] else mark["side"] > 0
            text = f"{mark['engine']} {'ENTRY' if mark['entry'] else 'EXIT'} {'LONG' if mark['side'] > 0 else 'SHORT'}"
            label_price = round(mark["price"] / timeline.tick) * timeline.tick
            text += f" {'' if mark['entry'] else mark['reason'] + ' '}@ {label_price:.2f}"
            markers.append({"time": epoch(mark["bar_time"]), "position": "aboveBar" if above else "belowBar",
                "color": colors.get((mark["engine"], mark["entry"], mark["side"]), "#964bc0" if mark["engine"] == "UT" else "#ebc33c"),
                "shape": "arrowDown" if above else "arrowUp", "text": text,
                "price": mark["price"], "engine": mark["engine"], "action": "ENTRY" if mark["entry"] else "EXIT"})
        basket = timeline.basket
        direction = basket["side"] if basket else 0
        current = float(frame.close.iloc[count-1]) if count else 0.
        # Expose the native UT ATR used by this engine.  The dashboard must not
        # borrow a legacy scanner ATR when UT/EMA owns the signal path.
        atr_value = float(indicators["ut_atr"][count-1]) if count and math.isfinite(indicators["ut_atr"][count-1]) else 0.
        return {"instrument": timeline.instrument, "timeframe": timeline.timeframe, "source_engine": ENGINE_KEY,
                "signal_fire_mode": "CANDLE_CLOSE" if timeline.p.execution_mode == "Signal Bar Close" else "INTRACANDLE",
                "signals": signals, "native_markers": markers, "utema_events": _copy_output(timeline.marks),
                "utema_records": _copy_output(timeline.records),
                "indicator_overlays": [{"name": name, "color": color, "values": timeline.level_history[name][:count]}
                    for name, color in (("UT stop", "#ffa500"), ("EMA stop", "#ff00ff"),
                                        ("UT target", "#00dd80"), ("EMA target", "#00bcd4"))],
                "positions": timeline.position_history[:count],
                "state": {"position": direction, "position_label": "LONG" if direction > 0 else "SHORT" if direction < 0 else "FLAT",
                          "current_price": current, "trailing_stop": basket["stop"] if basket else 0.,
                          "atr_value": atr_value, "atr_timeframe": timeline.timeframe,
                          "atr_reason": "native_ut_atr" if atr_value > 0 else "native_atr_unavailable",
                          "bar_count": count, "bar_time": str(frame.index[count-1]) if count else "",
                          "ut_position": timeline.positions["UT"].direction,
                          "ema_position": timeline.positions["EMA"].direction,
                          "ut_stop": timeline.positions["UT"].stop if timeline.positions["UT"].direction else None,
                          "ema_stop": timeline.positions["EMA"].stop if timeline.positions["EMA"].direction else None,
                          "ut_target": timeline.target("UT") if timeline.positions["UT"].direction else None,
                          "ema_target": timeline.target("EMA") if timeline.positions["EMA"].direction else None}}
