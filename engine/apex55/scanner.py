"""
apex_emacross/scanner.py
ApexScanner — main entry point for the APEX Hybrid Pro v5.5 Python package.

Two usage modes
───────────────
  1. BATCH / BACKTEST
     ─────────────────
     signals_df = scanner.scan(ohlcv_df, df_5m=..., df_15m=...)

     Returns a DataFrame of all signal, entry, exit, and update events
     with full SL/TP, scores, patterns, and options columns.

  2. LIVE / STREAMING
     ──────────────────
     # On each new bar from AngelOne / Fyers websocket:
     events = scanner.update(new_bar_dict, history_df)

     Returns list of event dicts (may be empty), same structure as scan().

AngelOne SmartAPI compatibility
────────────────────────────────
  Feed getCandleData() output directly:
    cols = ['timestamp','open','high','low','close','volume']
    df = pd.DataFrame(candle_data, columns=cols)
    df['timestamp'] = pd.to_datetime(df['timestamp'])
    df = df.set_index('timestamp').tz_localize('Asia/Kolkata')
    signals = scanner.scan(df)
"""

import pandas as pd
import numpy as np
from typing import Optional, List, Dict, Any

from .config import ApexConfig
from .instruments import detect_instrument
from .indicators import compute_all_indicators, crossover, crossunder
from .patterns import compute_patterns
from .confluence import compute_confluence
from .session import compute_session_mask
from .trade_manager import TradeManager


class ApexScanner:
    """
    Full APEX Hybrid Pro v5.5 — EMA Cross Edition Python scanner.

    Parameters
    ──────────
    config  : ApexConfig instance (or leave None for defaults).
    symbol  : Ticker string used for auto-detection (e.g. 'BANKNIFTY').
    sym_type: Optional asset class hint ('futures', 'index', 'equity', …).
    """

    def __init__(
        self,
        config:   Optional[ApexConfig] = None,
        symbol:   str = "",
        sym_type: str = "",
    ):
        self.cfg    = config or ApexConfig()
        self.symbol = symbol

        # Auto-detect instrument unless already set
        if self.cfg.instrument_type == "Auto-Detect":
            self.cfg.detected_instrument = detect_instrument(symbol, sym_type)
        else:
            self.cfg.detected_instrument = self.cfg.instrument_type

        self._mgr = TradeManager(self.cfg)

    # ════════════════════════════════════════════════════════════════════════
    # BATCH / BACKTEST MODE
    # ════════════════════════════════════════════════════════════════════════

    def scan(
        self,
        df:       pd.DataFrame,
        df_5m:    Optional[pd.DataFrame] = None,
        df_15m:   Optional[pd.DataFrame] = None,
    ) -> pd.DataFrame:
        """
        Run the full APEX scanner over a historical OHLCV DataFrame.

        Args:
            df:    OHLCV with DatetimeIndex (IST tz recommended).
                   Required columns: open, high, low, close, volume.
            df_5m:  Optional pre-fetched 5m OHLCV (MTF RSI).
            df_15m: Optional pre-fetched 15m OHLCV (HTF EMA gate).

        Returns:
            DataFrame of events:
              type, bar_idx, timestamp, direction, signal,
              entry_px, sl1, sl2, tp1, tp2, tp3, sl_mode,
              bull_score, bear_score, bias, vol_regime,
              setup, exit_reason, pnl_pct, t1_hit, t2_hit, t3_hit,
              opt_type, opt_strike, pat_bull, pat_bear,
              atr, adx, rsi, ema9, ema21, ema_cross_state
        """
        # ── Step 1: Compute all indicators ────────────────────────────────
        ind = compute_all_indicators(df, df_5m, df_15m)

        # ── Step 2: Patterns ──────────────────────────────────────────────
        ind = compute_patterns(ind, self.cfg)

        # ── Step 3: Confluence scores ─────────────────────────────────────
        ind = compute_confluence(ind, self.cfg)

        # ── Step 4: Session mask ──────────────────────────────────────────
        ind["session_ok"] = compute_session_mask(ind, self.cfg)

        # ── Step 5: EMA cross signals ─────────────────────────────────────
        ind["ema_cross_up"]   = crossover( ind["ema9"], ind["ema21"])
        ind["ema_cross_down"] = crossunder(ind["ema9"], ind["ema21"])
        ind["ema_cross_state"] = np.where(ind["ema9"] > ind["ema21"], "BULL", "BEAR")

        # ── Step 6: Bar-by-bar state machine ──────────────────────────────
        self._mgr.reset()
        events: List[Dict[str, Any]] = []

        # Precompute shifted columns needed by TradeManager
        ind["ema9_prev"]  = ind["ema9"].shift(1)
        ind["ema21_prev"] = ind["ema21"].shift(1)
        ind["rsi_prev"]   = ind["rsi"].shift(1)
        ind["low_1"]      = ind["low"].shift(1)
        ind["low_2"]      = ind["low"].shift(2)
        ind["high_1"]     = ind["high"].shift(1)
        ind["high_2"]     = ind["high"].shift(2)

        for i, (ts, row) in enumerate(ind.iterrows()):
            row_dict = row.to_dict()
            row_dict["bar_idx"]     = i
            row_dict["data_ready"]  = i >= self.cfg.min_bars_required
            row_dict["timestamp"]   = ts

            ev_list = self._mgr.process_bar(row_dict)
            if ev_list:
                for ev in ev_list:
                    ev["timestamp"] = ts
                    ev["bar_idx"]   = i
                    # Attach indicator context
                    ev["ema9"]          = row_dict.get("ema9")
                    ev["ema21"]         = row_dict.get("ema21")
                    ev["adx"]           = row_dict.get("adx")
                    ev["rsi"]           = row_dict.get("rsi")
                    ev["atr"]           = row_dict.get("atr")
                    ev["bull_score"]    = row_dict.get("bull_score")
                    ev["bear_score"]    = row_dict.get("bear_score")
                    ev["bias"]          = row_dict.get("bias_text")
                    ev["vol_regime"]    = row_dict.get("vol_regime")
                    ev["pat_bull"]      = row_dict.get("pat_bull_name", "")
                    ev["pat_bear"]      = row_dict.get("pat_bear_name", "")
                    ev["ema_cross"]     = row_dict.get("ema_cross_state")
                    ev["session_ok"]    = row_dict.get("session_ok")
                    ev["close"]         = row_dict.get("close")
                    events.append(ev)

        if not events:
            return pd.DataFrame()

        events_df = pd.DataFrame(events).set_index("timestamp")

        # ── Add stats summary to attrs (access via events_df.attrs) ───────
        events_df.attrs["stats"]   = self._mgr.get_stats()
        events_df.attrs["config"]  = self.cfg
        events_df.attrs["symbol"]  = self.symbol
        events_df.attrs["instrument"] = self.cfg.detected_instrument

        return events_df

    # ════════════════════════════════════════════════════════════════════════
    # LIVE / STREAMING MODE
    # ════════════════════════════════════════════════════════════════════════

    def update(
        self,
        new_bar:    Dict[str, Any],
        history_df: pd.DataFrame,
        df_5m:      Optional[pd.DataFrame] = None,
        df_15m:     Optional[pd.DataFrame] = None,
    ) -> List[Dict[str, Any]]:
        """
        Process a single new bar for live trading integration.

        Args:
            new_bar:    Dict with at minimum:
                        {timestamp, open, high, low, close, volume}
            history_df: Rolling history DataFrame (min 200 bars, same format).
                        The new_bar need NOT be appended — scanner does it.
            df_5m:      Optional recent 5m bars for MTF RSI.
            df_15m:     Optional recent 15m bars for HTF gate.

        Returns:
            List of event dicts (empty list if nothing happened).
            Each dict has the same schema as scan() events.

        Integration tip (AngelOne SmartAPI live feed):
            On every LTP / tick:
                1. Build/maintain rolling_df (last 300+ 1m bars).
                2. Call scanner.update(latest_bar, rolling_df).
                3. React to events: SIGNAL → alert, ENTRY → place order,
                   EXIT → send SL/TP cancels + close order.
        """
        # Append new bar to history and recompute
        ts  = pd.to_datetime(new_bar["timestamp"])
        row = {k: new_bar[k] for k in ["open","high","low","close","volume"]}
        new_row_df = pd.DataFrame([row], index=pd.DatetimeIndex([ts]))

        # Try to preserve timezone
        if hasattr(history_df.index, "tz") and history_df.index.tz is not None:
            new_row_df.index = new_row_df.index.tz_localize(history_df.index.tz)

        full_df = pd.concat([history_df, new_row_df]).sort_index()
        full_df = full_df[~full_df.index.duplicated(keep="last")]

        # Trim to last 500 bars for performance
        full_df = full_df.iloc[-500:]

        # Compute indicators on window
        ind = compute_all_indicators(full_df, df_5m, df_15m)
        ind = compute_patterns(ind, self.cfg)
        ind = compute_confluence(ind, self.cfg)
        ind["session_ok"]     = compute_session_mask(ind, self.cfg)
        ind["ema_cross_up"]   = crossover( ind["ema9"], ind["ema21"])
        ind["ema_cross_down"] = crossunder(ind["ema9"], ind["ema21"])
        ind["ema9_prev"]      = ind["ema9"].shift(1)
        ind["ema21_prev"]     = ind["ema21"].shift(1)
        ind["rsi_prev"]       = ind["rsi"].shift(1)
        ind["low_1"]          = ind["low"].shift(1)
        ind["low_2"]          = ind["low"].shift(2)
        ind["high_1"]         = ind["high"].shift(1)
        ind["high_2"]         = ind["high"].shift(2)

        # Only process the LAST bar (we maintain state externally)
        last_row = ind.iloc[-1].to_dict()
        last_row["bar_idx"]    = self._mgr._bar_idx + 1
        last_row["data_ready"] = len(full_df) >= self.cfg.min_bars_required
        last_row["timestamp"]  = ts

        ev_list = self._mgr.process_bar(last_row) or []

        # Enrich events
        for ev in ev_list:
            ev["timestamp"]  = ts
            ev["ema9"]       = last_row.get("ema9")
            ev["ema21"]      = last_row.get("ema21")
            ev["adx"]        = last_row.get("adx")
            ev["rsi"]        = last_row.get("rsi")
            ev["atr"]        = last_row.get("atr")
            ev["bull_score"] = last_row.get("bull_score")
            ev["bear_score"] = last_row.get("bear_score")
            ev["bias"]       = last_row.get("bias_text")
            ev["vol_regime"] = last_row.get("vol_regime")
            ev["pat_bull"]   = last_row.get("pat_bull_name", "")
            ev["pat_bear"]   = last_row.get("pat_bear_name", "")
            ev["close"]      = last_row.get("close")
            ev["session_ok"] = last_row.get("session_ok")

        return ev_list

    # ════════════════════════════════════════════════════════════════════════
    # HELPERS
    # ════════════════════════════════════════════════════════════════════════

    def get_stats(self) -> dict:
        """Return cumulative performance statistics from the trade manager."""
        return self._mgr.get_stats()

    def get_live_state(self) -> Optional[dict]:
        """Return current open trade state (None if flat)."""
        return self._mgr.get_live_state()

    def get_enriched_df(
        self,
        df:    pd.DataFrame,
        df_5m: Optional[pd.DataFrame] = None,
        df_15m:Optional[pd.DataFrame] = None,
    ) -> pd.DataFrame:
        """
        Return the full indicator-enriched DataFrame without running the
        state machine.  Useful for charting, dashboard, and further analysis.
        """
        ind = compute_all_indicators(df, df_5m, df_15m)
        ind = compute_patterns(ind, self.cfg)
        ind = compute_confluence(ind, self.cfg)
        ind["session_ok"]      = compute_session_mask(ind, self.cfg)
        ind["ema_cross_up"]    = crossover( ind["ema9"], ind["ema21"])
        ind["ema_cross_down"]  = crossunder(ind["ema9"], ind["ema21"])
        ind["ema_cross_state"] = np.where(ind["ema9"] > ind["ema21"], "BULL", "BEAR")
        return ind

    def reset(self, symbol: str = "", sym_type: str = ""):
        """Reset scanner state (call between instruments in a multi-scan loop)."""
        if symbol:
            self.symbol = symbol
            self.cfg.detected_instrument = detect_instrument(symbol, sym_type)
        self._mgr.reset()
