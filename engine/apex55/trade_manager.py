"""
apex_emacross/trade_manager.py
Stateful bar-by-bar trade manager.
Mirrors Pine Script Sections 8–14 exactly (APEX v5.5).

State machine flow:
  Signal detected → pendingEntry on bar N
      ↓
  Trade opens at open of bar N+1  (1-bar delay, realistic execution)
      ↓
  Each bar: update peaks/targets, trail stop, check exits
      ↓
  Exit: SL1 / SL2 / TSL / Momentum → close trade, record PnL
"""

from dataclasses import dataclass, field
from collections import deque
from typing import Optional, List
import math


# ════════════════════════════════════════════════════════════════════════════
# DATA STRUCTURES
# ════════════════════════════════════════════════════════════════════════════

@dataclass
class TradeRecord:
    direction:  str
    entry_px:   float
    exit_px:    float
    pnl_pct:    float
    setup:      str
    exit_reason: str
    bar_in:     int
    bar_out:    int


@dataclass
class LiveTrade:
    direction:  str
    entry_px:   float
    sl1:        float
    sl2:        float
    tsl:        float
    tp1:        float
    tp2:        float
    tp3:        float
    setup:      str
    opt_type:   str
    opt_strike: float
    sl_mode:    str
    bar_in:     int
    t1_hit:     bool = False
    t2_hit:     bool = False
    t3_hit:     bool = False
    prof_locked: bool = False
    peak_px:    float = 0.0
    trough_px:  float = 0.0
    exit_conf_cnt: int = 0
    win_counted:   bool = False
    loss_counted:  bool = False


# ════════════════════════════════════════════════════════════════════════════
# TRADE MANAGER
# ════════════════════════════════════════════════════════════════════════════

class TradeManager:
    """
    Processes one indicator-row at a time and maintains full trade state.

    Usage (streaming / bar-by-bar)
    ───────────────────────────────
        mgr = TradeManager(config)
        for row in indicator_df.itertuples():
            event = mgr.process_bar(row)
            # event: None | {'type': 'SIGNAL', ...} | {'type': 'EXIT', ...}

    Usage (batch scan — called by ApexScanner.scan())
    ──────────────────────────────────────────────────
        See ApexScanner.scan() for the full loop.
    """

    MAX_HISTORY = 10    # rolling trade history kept in memory (mirrors Pine)
    PNL_WINDOW  = 20    # rolling PnL window for signal ratio

    def __init__(self, config):
        self.cfg = config

        # ── Position State ────────────────────────────────────────────────
        self.in_trade:      bool              = False
        self.trade:         Optional[LiveTrade] = None

        # ── Pending Entry (1-bar delay: signal fires N, executes at N+1 open)
        self.pending:       bool              = False
        self.pending_long:  bool              = False
        self.pending_setup: str               = ""
        self.pending_otype: str               = ""
        self.pending_strike: float            = 0.0

        # ── Signal Cooldown / Circuit Breaker ────────────────────────────
        self.last_sig_bar:   int              = -999
        self.last_sig_state: int              = 0    # +1=long, -1=short, 0=none
        self.pause_until:    int              = 0

        # ── Cumulative Statistics ─────────────────────────────────────────
        self.total_trades:     int   = 0
        self.win_trades:       int   = 0
        self.loss_trades:      int   = 0
        self.total_pnl:        float = 0.0
        self.gross_profit_pct: float = 0.0
        self.gross_loss_pct:   float = 0.0
        self.consec_loss:      int   = 0

        # ── Rolling Windows ───────────────────────────────────────────────
        self.history: deque = deque(maxlen=self.MAX_HISTORY)
        self.pnl_roll: deque = deque(maxlen=self.PNL_WINDOW)

        # ── Bar counter ───────────────────────────────────────────────────
        self._bar_idx: int = 0

    # ────────────────────────────────────────────────────────────────────────
    # PUBLIC API
    # ────────────────────────────────────────────────────────────────────────

    def process_bar(self, row: dict) -> Optional[dict]:
        """
        Process one bar's worth of pre-computed indicator data.

        Args:
            row: dict with all fields from compute_all_indicators() /
                 compute_patterns() / compute_confluence() output,
                 plus 'session_ok' (bool), 'bar_idx' (int), and optionally
                 'buy_signal' / 'sell_signal' (pre-computed booleans).

        Returns:
            None           — nothing notable happened.
            {'type': 'SIGNAL', ...}  — new entry signal.
            {'type': 'ENTRY',  ...}  — trade entered (pending → live).
            {'type': 'EXIT',   ...}  — trade closed.
            {'type': 'UPDATE', ...}  — TP milestone hit inside trade.
        """
        self._bar_idx = row.get("bar_idx", self._bar_idx + 1)
        bi = self._bar_idx

        # ── Detect EMA crossover signals ──────────────────────────────────
        ema9  = row["ema9"]
        ema21 = row["ema21"]
        ema9_prev  = row.get("ema9_prev",  ema9)    # ema9  of previous bar
        ema21_prev = row.get("ema21_prev", ema21)   # ema21 of previous bar

        cross_up   = (ema9 > ema21) and (ema9_prev <= ema21_prev)
        cross_down = (ema9 < ema21) and (ema9_prev >= ema21_prev)

        data_ready = row.get("data_ready", bi >= self.cfg.min_bars_required)
        session_ok = row.get("session_ok", True)
        can_signal = ((bi - self.last_sig_bar) >= self.cfg.signal_cooldown
                      and bi > self.pause_until)

        buy_signal  = (cross_up   and session_ok and data_ready and can_signal
                       and not self.in_trade and not self.pending
                       and self.last_sig_state <= 0)
        sell_signal = (cross_down and session_ok and data_ready and can_signal
                       and not self.in_trade and not self.pending
                       and self.last_sig_state >= 0)

        events: List[dict] = []

        # ── Capture new signal (fires at bar N) ───────────────────────────
        if buy_signal or sell_signal:
            self.pending       = True
            self.pending_long  = buy_signal
            self.pending_setup = "EMA9/21 BullX" if buy_signal else "EMA9/21 BearX"
            self.last_sig_bar  = bi
            self.last_sig_state = 1 if buy_signal else -1

            if self.cfg.enable_opt and self.cfg.supports_options:
                from .options_module import get_strike
                is_ce = buy_signal
                conf  = row.get("bull_score", 50.0) if buy_signal else row.get("bear_score", 50.0)
                # Temporarily cache vol/trend state for smart-auto strike
                self.cfg._cached_high_vol    = bool(row.get("high_vol", False))
                self.cfg._cached_is_trending = bool(row.get("is_trending", False))
                self.pending_otype  = "CE" if is_ce else "PE"
                self.pending_strike = get_strike(is_ce, conf, row["close"], self.cfg)
            else:
                self.pending_otype  = ""
                self.pending_strike = 0.0

            events.append({
                "type":        "SIGNAL",
                "direction":   "BUY" if buy_signal else "SELL",
                "bar_idx":     bi,
                "close":       row["close"],
                "bull_score":  row.get("bull_score", 0.0),
                "bear_score":  row.get("bear_score", 0.0),
                "vol_regime":  row.get("vol_regime", "NORMAL"),
                "setup":       self.pending_setup,
                "opt_type":    self.pending_otype,
                "opt_strike":  self.pending_strike,
                "pat_bull":    row.get("pat_bull_name", ""),
                "pat_bear":    row.get("pat_bear_name", ""),
            })

        # ── Execute pending entry at next-bar open ────────────────────────
        if self.pending and not self.in_trade:
            from .risk import calc_sl, calc_targets
            import numpy as np

            is_long   = self.pending_long
            entry_px  = row["open"]   # realistic: execute at next bar's open
            atr_v     = row["atr"]
            high_vol  = bool(row.get("high_vol",   False))
            low_vol   = bool(row.get("low_vol",    False))
            trending  = bool(row.get("is_trending", False))

            # Anchor range: 3-bar lookback (mirrors Pine's ta.lowest/highest(3))
            anchor_low  = min(row["low"],
                              row.get("low_1",  row["low"]),
                              row.get("low_2",  row["low"]))
            anchor_high = max(row["high"],
                              row.get("high_1", row["high"]),
                              row.get("high_2", row["high"]))

            sl1, sl2, mode = calc_sl(is_long, entry_px,
                                     anchor_low, anchor_high,
                                     atr_v, self.cfg,
                                     high_vol, low_vol, trending)
            tp1, tp2, tp3  = calc_targets(is_long, entry_px, sl1, self.cfg)

            self.trade = LiveTrade(
                direction  = "LONG" if is_long else "SHORT",
                entry_px   = entry_px,
                sl1        = sl1,
                sl2        = sl2,
                tsl        = sl1,
                tp1        = tp1, tp2=tp2, tp3=tp3,
                setup      = self.pending_setup,
                opt_type   = self.pending_otype,
                opt_strike = self.pending_strike,
                sl_mode    = mode,
                bar_in     = bi,
                peak_px    = entry_px,
                trough_px  = entry_px,
            )
            self.in_trade   = True
            self.pending    = False
            self.total_trades += 1

            events.append({
                "type":       "ENTRY",
                "direction":  self.trade.direction,
                "bar_idx":    bi,
                "entry_px":   entry_px,
                "sl1":        sl1,  "sl2": sl2,
                "tp1":        tp1,  "tp2": tp2, "tp3": tp3,
                "sl_mode":    mode,
                "opt_type":   self.pending_otype,
                "opt_strike": self.pending_strike,
                "setup":      self.pending_setup,
                "atr":        atr_v,
            })

        # ── In-trade processing ───────────────────────────────────────────
        if self.in_trade and self.trade is not None:
            t = self.trade
            h = row["high"]; l = row["low"]; c = row["close"]
            atr_v = row["atr"]

            # Update peak / trough
            if t.direction == "LONG":
                t.peak_px   = max(t.peak_px, h)
            else:
                t.trough_px = min(t.trough_px, l)

            # ── Target hits ───────────────────────────────────────────────
            milestones = []
            if t.direction == "LONG":
                if not t.t1_hit and h >= t.tp1:
                    t.t1_hit = True; milestones.append("T1")
                if not t.t2_hit and h >= t.tp2:
                    t.t2_hit = True; milestones.append("T2")
                if not t.t3_hit and h >= t.tp3:
                    t.t3_hit = True; milestones.append("T3")
            else:
                if not t.t1_hit and l <= t.tp1:
                    t.t1_hit = True; milestones.append("T1")
                if not t.t2_hit and l <= t.tp2:
                    t.t2_hit = True; milestones.append("T2")
                if not t.t3_hit and l <= t.tp3:
                    t.t3_hit = True; milestones.append("T3")

            if milestones:
                events.append({
                    "type": "UPDATE", "milestones": milestones,
                    "bar_idx": bi, "direction": t.direction,
                })

            # ── Trailing stop ─────────────────────────────────────────────
            if self.cfg.use_trail:
                from .risk import update_trailing_stop
                risk   = abs(t.entry_px - t.sl1)
                curr_rr = ((c - t.entry_px) / risk if t.direction == "LONG"
                           else (t.entry_px - c) / risk) if risk > 0 else 0.0

                t.tsl, t.prof_locked = update_trailing_stop(
                    t.direction, t.tsl, t.entry_px, t.sl1,
                    t.peak_px, t.trough_px, atr_v,
                    t.t1_hit, t.t2_hit, t.prof_locked, curr_rr,
                    bool(row.get("high_vol", False)), self.cfg,
                )

            # ── Momentum exit counter ─────────────────────────────────────
            ema13 = row.get("ema13", row["ema9"])
            rsi_v = row.get("rsi",  50.0)
            rsi_p = row.get("rsi_prev", rsi_v)
            macdL = row.get("macd_line",   0.0)
            macdS = row.get("macd_signal", 0.0)
            maccB = bool(row.get("macd_acc_bull", False))
            maccR = bool(row.get("macd_acc_bear", False))

            threshold = 3 if t.t1_hit else 2

            if t.direction == "LONG":
                cA = (macdL < macdS) and maccR
                cB = c < ema13
                cC = rsi_v < 45.0 and rsi_v < rsi_p
                met = int(cA) + int(cB) + int(cC)
                if met >= threshold:
                    t.exit_conf_cnt += 1
                elif c > row["ema9"]:
                    t.exit_conf_cnt = max(0, t.exit_conf_cnt - 1)
                mom_exit = (t.exit_conf_cnt >= self.cfg.exit_conf_bars
                            and not (h > t.tp2 and not t.t2_hit))
            else:
                cA = (macdL > macdS) and maccB
                cB = c > ema13
                cC = rsi_v > 55.0 and rsi_v > rsi_p
                met = int(cA) + int(cB) + int(cC)
                if met >= threshold:
                    t.exit_conf_cnt += 1
                elif c < row["ema9"]:
                    t.exit_conf_cnt = max(0, t.exit_conf_cnt - 1)
                mom_exit = (t.exit_conf_cnt >= self.cfg.exit_conf_bars
                            and not (l < t.tp2 and not t.t2_hit))

            # ── Exit conditions (priority order mirrors Pine Sec 13) ───────
            exit_reason = None
            exit_px     = c

            if t.direction == "LONG":
                if l < t.sl2:
                    exit_reason = "Hard SL2 (Max Loss)"; exit_px = t.sl2
                elif l < t.sl1:
                    exit_reason = "Hard SL1"; exit_px = t.sl1
                elif self.cfg.use_trail and t.tsl > t.sl1 and l < t.tsl:
                    exit_reason = "TSL (Profit Locked)" if t.prof_locked else "TSL"
                    exit_px = t.tsl
                elif mom_exit:
                    exit_reason = "Momentum Exit"
            else:
                if h > t.sl2:
                    exit_reason = "Hard SL2 (Max Loss)"; exit_px = t.sl2
                elif h > t.sl1:
                    exit_reason = "Hard SL1"; exit_px = t.sl1
                elif self.cfg.use_trail and t.tsl < t.sl1 and h > t.tsl:
                    exit_reason = "TSL (Profit Locked)" if t.prof_locked else "TSL"
                    exit_px = t.tsl
                elif mom_exit:
                    exit_reason = "Momentum Exit"

            if exit_reason:
                # Compute PnL
                if t.direction == "LONG":
                    pnl = (exit_px - t.entry_px) / t.entry_px * 100.0
                else:
                    pnl = (t.entry_px - exit_px) / t.entry_px * 100.0

                self.total_pnl += pnl
                self.pnl_roll.append(pnl)

                if pnl > 0.0 and not t.win_counted:
                    self.win_trades       += 1
                    t.win_counted          = True
                    self.consec_loss       = 0
                    self.gross_profit_pct += pnl
                elif pnl <= 0.0 and not t.loss_counted:
                    self.loss_trades      += 1
                    t.loss_counted         = True
                    self.consec_loss      += 1
                    self.gross_loss_pct   += abs(pnl)
                    if self.consec_loss >= self.cfg.max_consec_loss:
                        self.pause_until = bi + self.cfg.circuit_pause

                rec = TradeRecord(
                    direction  = t.direction,
                    entry_px   = t.entry_px,
                    exit_px    = exit_px,
                    pnl_pct    = pnl,
                    setup      = t.setup + " / " + exit_reason,
                    exit_reason= exit_reason,
                    bar_in     = t.bar_in,
                    bar_out    = bi,
                )
                self.history.append(rec)

                events.append({
                    "type":        "EXIT",
                    "direction":   t.direction,
                    "bar_idx":     bi,
                    "entry_px":    t.entry_px,
                    "exit_px":     exit_px,
                    "pnl_pct":     pnl,
                    "exit_reason": exit_reason,
                    "t1_hit":      t.t1_hit,
                    "t2_hit":      t.t2_hit,
                    "t3_hit":      t.t3_hit,
                    "opt_type":    t.opt_type,
                    "opt_strike":  t.opt_strike,
                    "setup":       t.setup,
                    "bars_held":   bi - t.bar_in,
                })

                # Reset trade state
                self.trade          = None
                self.in_trade       = False
                self.last_sig_state = 0

        self._bar_idx = bi
        return events if events else None

    # ────────────────────────────────────────────────────────────────────────
    # STATISTICS
    # ────────────────────────────────────────────────────────────────────────

    def get_stats(self) -> dict:
        """Return cumulative performance statistics."""
        win_rate = (self.win_trades / self.total_trades * 100.0
                    if self.total_trades > 0 else 0.0)
        avg_win  = (self.gross_profit_pct / self.win_trades
                    if self.win_trades > 0 else 0.0)
        avg_loss = (self.gross_loss_pct  / self.loss_trades
                    if self.loss_trades > 0 else 1.0)

        # Half-Kelly position size recommendation
        half_kelly = 0.0
        if self.total_trades >= 5 and avg_loss > 0.0:
            w     = win_rate / 100.0
            rr_emp = avg_win / avg_loss
            k     = w - (1.0 - w) / rr_emp
            half_kelly = max(0.0, min(k * 50.0, 30.0))

        # Rolling PnL Sharpe-ratio proxy
        pnl_sig_ratio = 0.0
        if len(self.pnl_roll) >= 3:
            import statistics
            mu  = statistics.mean(self.pnl_roll)
            sig = statistics.stdev(self.pnl_roll) or 1e-9
            pnl_sig_ratio = mu / sig

        return {
            "total_trades":   self.total_trades,
            "win_trades":     self.win_trades,
            "loss_trades":    self.loss_trades,
            "win_rate_pct":   round(win_rate, 2),
            "total_pnl_pct":  round(self.total_pnl, 4),
            "avg_win_pct":    round(avg_win, 4),
            "avg_loss_pct":   round(avg_loss, 4),
            "half_kelly_pct": round(half_kelly, 2),
            "pnl_sig_ratio":  round(pnl_sig_ratio, 4),
            "consec_loss":    self.consec_loss,
        }

    def get_live_state(self) -> Optional[dict]:
        """Current live trade state for streaming dashboard."""
        if not self.in_trade or self.trade is None:
            return None
        t   = self.trade
        c   = None   # close not available here; caller should compute
        return {
            "direction":   t.direction,
            "entry_px":    t.entry_px,
            "sl1":         t.sl1,
            "sl2":         t.sl2,
            "tsl":         t.tsl,
            "tp1":         t.tp1,
            "tp2":         t.tp2,
            "tp3":         t.tp3,
            "t1_hit":      t.t1_hit,
            "t2_hit":      t.t2_hit,
            "t3_hit":      t.t3_hit,
            "prof_locked": t.prof_locked,
            "sl_mode":     t.sl_mode,
            "setup":       t.setup,
            "opt_type":    t.opt_type,
            "opt_strike":  t.opt_strike,
            "bars_held":   self._bar_idx - t.bar_in,
        }

    def reset(self):
        """Full state reset — use between instruments in a multi-scan loop."""
        self.__init__(self.cfg)
