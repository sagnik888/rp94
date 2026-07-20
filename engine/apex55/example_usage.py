"""
example_usage.py
────────────────
Demonstrates all three usage patterns for the apex_emacross package.

  A) Batch backtest on a CSV / parquet file
  B) AngelOne SmartAPI historical data scan
  C) Live streaming with AngelOne websocket (pattern)

Run:
    pip install -r requirements.txt
    python example_usage.py
"""

import pandas as pd
import numpy as np
from datetime import datetime, timedelta

from apex_emacross import ApexScanner, ApexConfig


# ════════════════════════════════════════════════════════════════════════════
# SECTION A — Batch backtest with synthetic OHLCV data
# ════════════════════════════════════════════════════════════════════════════

def generate_sample_nifty_data(n_bars: int = 600) -> pd.DataFrame:
    """Generate synthetic 5-minute Nifty 50 OHLCV for quick testing."""
    np.random.seed(42)
    idx = pd.date_range(
        start="2024-01-02 09:15",
        periods=n_bars,
        freq="5min",
        tz="Asia/Kolkata",
    )
    # Remove outside-market bars (rough filter)
    idx = idx[(idx.hour * 100 + idx.minute >= 915) &
              (idx.hour * 100 + idx.minute <= 1530)][:n_bars]

    base   = 21800.0
    close  = base + np.cumsum(np.random.randn(len(idx)) * 25)
    spread = np.abs(np.random.randn(len(idx)) * 30) + 15
    df = pd.DataFrame({
        "open":   close + np.random.randn(len(idx)) * 8,
        "high":   close + spread,
        "low":    close - spread,
        "close":  close,
        "volume": (np.random.randint(50_000, 250_000, size=len(idx))).astype(float),
    }, index=idx)
    return df


def demo_batch_scan():
    print("=" * 60)
    print("DEMO A — Batch backtest (synthetic Nifty 5m)")
    print("=" * 60)

    df = generate_sample_nifty_data(600)
    print(f"Data: {len(df)} bars  |  {df.index[0]}  →  {df.index[-1]}")

    # ── Configure ────────────────────────────────────────────────────────
    cfg = ApexConfig(
        instrument_type = "Nifty 50",
        use_session     = True,
        block_open      = True,
        block_close     = True,
        t1r             = 2.0,
        t2r             = 3.0,
        t3r             = 4.0,
        atr_mult        = 1.5,
        use_trail       = True,
        signal_cooldown = 2,
        enable_opt      = True,
        show_patterns   = True,
    )

    scanner = ApexScanner(cfg, symbol="NIFTY50")
    signals = scanner.scan(df)

    if signals.empty:
        print("No signals fired (need more historical data for warm-up).")
    else:
        print(f"\nEvents captured: {len(signals)}")
        print("\n─── SIGNAL / ENTRY events ───")
        entry_events = signals[signals["type"].isin(["SIGNAL", "ENTRY"])]
        for ts, row in entry_events.iterrows():
            print(
                f"  [{row['type']:6s}] {ts.strftime('%H:%M')}  "
                f"{row.get('direction',''):5s}  "
                f"Bull:{row.get('bull_score', 0.0):.0f}  "
                f"Bear:{row.get('bear_score', 0.0):.0f}  "
                f"ADX:{row.get('adx', 0.0):.1f}  "
                f"Vol:{row.get('vol_regime','')}"
            )

        print("\n─── EXIT events ───")
        exit_events = signals[signals["type"] == "EXIT"]
        for ts, row in exit_events.iterrows():
            pnl = row.get("pnl_pct", 0.0)
            print(
                f"  [EXIT ] {ts.strftime('%H:%M')}  "
                f"{row.get('direction',''):5s}  "
                f"PnL: {pnl:+.2f}%  "
                f"Reason: {row.get('exit_reason','')}"
            )

    print("\n─── Cumulative Stats ───")
    stats = scanner.get_stats()
    for k, v in stats.items():
        print(f"  {k:<22}: {v}")

    return scanner, signals


# ════════════════════════════════════════════════════════════════════════════
# SECTION B — AngelOne SmartAPI historical scan pattern
# ════════════════════════════════════════════════════════════════════════════

def demo_angelone_historical():
    """
    Shows how to wire apex_emacross to AngelOne SmartAPI getCandleData().
    The actual API call is commented out; replace with real credentials.
    """
    print("\n" + "=" * 60)
    print("DEMO B — AngelOne SmartAPI historical pattern (template)")
    print("=" * 60)

    ANGELONE_SCAN_CODE = """
from smartapi import SmartConnect
import pyotp, pandas as pd
from apex_emacross import ApexScanner, ApexConfig

# ── 1. Connect ────────────────────────────────────────────────────────────
obj  = SmartConnect(api_key="YOUR_API_KEY")
data = obj.generateSession("YOUR_CLIENT_ID", "YOUR_PIN",
                            pyotp.TOTP("YOUR_TOTP_SECRET").now())
auth_token = data["data"]["jwtToken"]
obj.setAccessToken(auth_token)

# ── 2. Fetch historical 5m candles (BankNifty spot, last 30 days) ─────────
candle_params = {
    "exchange":    "NSE",
    "symboltoken": "26009",          # BankNifty spot token
    "interval":    "FIVE_MINUTE",
    "fromdate":    "2024-11-01 09:15",
    "todate":      "2024-11-30 15:30",
}
resp = obj.getCandleData(candle_params)
raw  = resp["data"]   # list of [timestamp, open, high, low, close, volume]

df = pd.DataFrame(raw, columns=["timestamp","open","high","low","close","volume"])
df["timestamp"] = pd.to_datetime(df["timestamp"])
df = df.set_index("timestamp").tz_localize("Asia/Kolkata")
df = df.astype(float)

# ── 3. Optional: fetch 5m and 15m for MTF ────────────────────────────────
# df_15m = fetch_candles("FIFTEEN_MINUTE", ...)

# ── 4. Scan ───────────────────────────────────────────────────────────────
cfg     = ApexConfig(instrument_type="Bank Nifty")
scanner = ApexScanner(cfg, symbol="BANKNIFTY")
signals = scanner.scan(df)   # df_15m=df_15m if available

entry_signals = signals[signals["type"] == "SIGNAL"]
print(f"Signals: {len(entry_signals)}  |  Stats: {scanner.get_stats()}")
"""
    print(ANGELONE_SCAN_CODE)


# ════════════════════════════════════════════════════════════════════════════
# SECTION C — Live streaming integration pattern
# ════════════════════════════════════════════════════════════════════════════

def demo_live_streaming():
    """
    Shows the streaming integration pattern for AngelOne WebSocket.
    """
    print("\n" + "=" * 60)
    print("DEMO C — Live streaming / websocket pattern (template)")
    print("=" * 60)

    LIVE_CODE = """
import threading, time, pandas as pd
from smartapi.smartWebSocketV2 import SmartWebSocketV2
from apex_emacross import ApexScanner, ApexConfig

cfg     = ApexConfig(instrument_type="Bank Nifty", use_session=True)
scanner = ApexScanner(cfg, symbol="BANKNIFTY")

# Maintain a rolling window of completed bars
rolling_df = pd.DataFrame(columns=["open","high","low","close","volume"])

def on_new_bar(bar: dict):
    '''Called by your bar-aggregation logic on each completed 5m bar.'''
    global rolling_df

    # ── 1. Append new bar ────────────────────────────────────────────────
    ts  = pd.to_datetime(bar["timestamp"]).tz_localize("Asia/Kolkata")
    new = pd.DataFrame([bar], index=pd.DatetimeIndex([ts]))
    rolling_df = pd.concat([rolling_df, new]).iloc[-500:]

    # Need at least 200 bars for warm-up
    if len(rolling_df) < 200:
        return

    # ── 2. Update scanner ─────────────────────────────────────────────────
    events = scanner.update(bar, rolling_df)

    # ── 3. React to events ────────────────────────────────────────────────
    for ev in events:
        if ev["type"] == "SIGNAL":
            direction = ev["direction"]        # "BUY" or "SELL"
            opt_type  = ev["opt_type"]         # "CE" or "PE"
            strike    = ev["opt_strike"]       # e.g. 50200.0
            bull_s    = ev["bull_score"]
            bear_s    = ev["bear_score"]
            pattern   = ev["pat_bull"] or ev["pat_bear"]

            print(f"⚡ SIGNAL  {direction}  |  {opt_type} {strike:.0f}"
                  f"  |  Bull:{bull_s:.0f}  Bear:{bear_s:.0f}"
                  f"  |  {pattern or 'no pattern'}")
            # → Send alert / notification

        elif ev["type"] == "ENTRY":
            sl1  = ev["sl1"];  sl2 = ev["sl2"]
            tp1  = ev["tp1"];  tp2 = ev["tp2"]
            mode = ev["sl_mode"]
            print(f"✅ ENTRY   {ev['direction']}  @ {ev['entry_px']:.1f}"
                  f"  SL1:{sl1:.1f}  TP1:{tp1:.1f}  ({mode})")
            # → Place bracket order via SmartAPI

        elif ev["type"] == "EXIT":
            print(f"❌ EXIT    {ev['direction']}  @ {ev['exit_px']:.1f}"
                  f"  PnL:{ev['pnl_pct']:+.2f}%  [{ev['exit_reason']}]")
            # → Cancel pending orders, square off position

        elif ev["type"] == "UPDATE":
            print(f"📊 UPDATE  {ev['milestones']} hit — consider scaling out")

# ── WebSocket feed (AngelOne) — bar aggregation is your responsibility ────
# Aggregate ticks into completed OHLCV bars, then call on_new_bar()
"""
    print(LIVE_CODE)


# ════════════════════════════════════════════════════════════════════════════
# SECTION D — Enriched DataFrame for charting / dashboard
# ════════════════════════════════════════════════════════════════════════════

def demo_enriched_df():
    print("\n" + "=" * 60)
    print("DEMO D — Enriched DataFrame (indicators + scores + patterns)")
    print("=" * 60)

    df      = generate_sample_nifty_data(400)
    scanner = ApexScanner(ApexConfig(instrument_type="Nifty 50"), symbol="NIFTY50")
    enriched = scanner.get_enriched_df(df)

    key_cols = [
        "ema9", "ema21", "ema50", "vwap", "atr", "rsi",
        "adx", "di_plus", "di_minus", "macd_line",
        "bull_score", "bear_score", "bias_text",
        "vol_regime", "session_ok",
        "ema_cross_up", "ema_cross_down",
        "pat_valid_bull", "pat_bull_name",
        "pat_valid_bear", "pat_bear_name",
    ]
    available = [c for c in key_cols if c in enriched.columns]
    print(f"Columns available: {len(enriched.columns)}")
    print(f"\nLast 3 rows (key columns):")
    print(enriched[available].tail(3).to_string())


# ════════════════════════════════════════════════════════════════════════════
# MAIN
# ════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    demo_batch_scan()
    demo_angelone_historical()
    demo_live_streaming()
    demo_enriched_df()
