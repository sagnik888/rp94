"""Offline production replay of UT SMART EXIT (audit 2026-09-26; based on
scripts/verify_offline_engine_replay.py).

Latest result 2026-09-27 (15 sessions to 2026-09-25, archived option prices, 1 lot,
operator timeframe policy INCLUDE_5MIN): 63 trades, +Rs.93,531; both 15m (+Rs.44,289)
and 5m (+Rs.49,242) were positive, as was every index. The replay made no network calls.


Real Scanner / SignalProcessor / TradeManager in HISTORICAL mode on a COPY of the local
candle store and archived option contracts. Network blocked, separate DB, no change to the
live app's settings or state. Usage:
python reports/ut_smart_exit_replay_20260926.py <days> <out.json> [key=value ...]
"""
import json
import os
import shutil
import socket
import sys
from collections import Counter, defaultdict
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[1]
DAYS = int(sys.argv[1]) if len(sys.argv) > 1 else 15
OUT = (Path(sys.argv[2]) if len(sys.argv) > 2 else Path("uts_replay.json")).resolve()
OVERRIDES = dict(arg.split("=", 1) for arg in sys.argv[3:])
WS = OUT.with_suffix(".workspace")

sys.path.insert(0, str(REPO))
os.chdir(REPO)
import config.settings as settings_module  # noqa: E402

base = settings_module.Settings()                # the operator's .env, read in the repo
if WS.exists():
    shutil.rmtree(WS)
(WS / "data_store").mkdir(parents=True)
shutil.copytree(REPO / "data_store" / "candles", WS / "data_store" / "candles")
for name in ("kite_instruments.json", "instruments.json", "option_history_bundle.parquet",
             "option_history_bundle.parquet.manifest.json", "option_history_misses.json", "instrument_iv_history.json"):
    if (REPO / "data_store" / name).exists():
        shutil.copy2(REPO / "data_store" / name, WS / "data_store" / name)
(WS / "config").mkdir()
shutil.copy2(REPO / "config" / "instruments.json", WS / "config" / "instruments.json")
os.chdir(WS)
os.environ["UTBOT_DB_PATH"] = str(WS / "data_store" / "replay.db")
os.environ["UTBOT_TEST_DB_PATH"] = str(WS / "data_store" / "replay_test.db")

attempts = []


def block(*a, **k):
    attempts.append("blocked")
    raise RuntimeError("offline replay: no network")


socket.socket.connect = block
socket.create_connection = block

import pandas as pd  # noqa: E402
from loguru import logger  # noqa: E402

logger.remove()
logger.add(WS / "replay.log", level="INFO")
settings_module._settings = base
settings = settings_module.get_settings()
object.__setattr__(settings, "scanner_engine", "ut_smart")
object.__setattr__(settings, "trading_mode", "HISTORICAL")
for key, value in OVERRIDES.items():
    current = getattr(settings, key)
    cast = type(current) if current is not None else str
    object.__setattr__(settings, key, (value.lower() in {"1", "true", "yes"}) if cast is bool else cast(value))
if hasattr(settings, "telegram_enabled"):
    object.__setattr__(settings, "telegram_enabled", False)

from data.candle_builder import CandleBuilder  # noqa: E402
from engine.expiry_manager import expiry_manager  # noqa: E402
from engine.intelligence_score import aggregate_intelligence_score  # noqa: E402
from engine.multi_timeframe import MultiTimeframeEngine  # noqa: E402
from engine.signal_manager import SignalManager  # noqa: E402
from intelligence.intelligence_aggregator import IntelligenceAggregator  # noqa: E402
from scanner import Scanner  # noqa: E402
from trading.trade_manager import TradeManager  # noqa: E402

for master_name in ("kite_instruments.json", "instruments.json"):
    master_path = WS / "data_store" / master_name
    if master_path.exists():
        expiry_manager.df = pd.DataFrame(json.loads(master_path.read_text()))
        expiry_manager._ensure_expiry_dates()
        expiry_manager.analyze_expiries()
        break


class LocalData:
    def get_option_token(self, name, strike, option_type, trade_date=None, now_dt=None):
        expiry_type = "active_monthly" if name in {"BANKNIFTY", "MIDCPNIFTY"} else "active_weekly"
        if trade_date is None:
            return None
        return expiry_manager.get_token_for_date(name, strike, option_type, trade_date, expiry_type, now_dt=now_dt)

    def get_option_chain(self, *a, **k): return None
    def get_derivative_participation(self, *a, **k): return {}
    def get_recorded_option_chain(self, *a, **k): return pd.DataFrame(), {"available": False}
    def get_latest_price(self, *a, **k): return 0.0
    def get_ltp(self, *a, **k): return 0.0
    def is_market_open(self, *a, **k): return False
    def get_historical_candles(self, *a, **k): return pd.DataFrame()


indices = ["NIFTY", "BANKNIFTY", "SENSEX", "MIDCPNIFTY"]
config = json.loads((WS / "config" / "instruments.json").read_text())
candles = CandleBuilder(max_candles=100000)
mtf = MultiTimeframeEngine(settings.get_ut_engine_params())
frames = {}
for name in indices:
    for tf in ("1min", "5min", "15min"):
        frame = pd.read_csv(WS / f"data_store/candles/{name}_{tf}.csv", index_col=0, parse_dates=True).sort_index()
        frame = frame[~frame.index.duplicated(keep="last")]
        frames[(name, tf)] = frame
        getattr(candles, "_candles_" + tf)[name] = frame
with patch.object(TradeManager, "load_state", lambda self: None), patch("threading.Thread.start", lambda self: None):
    trades = TradeManager(broker=None)
trades._sync_active = False
with patch.object(expiry_manager, "pre_market_check", return_value={}):
    scanner = Scanner(LocalData(), candles, mtf, SignalManager(), IntelligenceAggregator(), trades,
                      SimpleNamespace(), config, trading_mode="HISTORICAL")
scanner.backtest_days = DAYS
scanner.active_indices = indices
scanner._diagnostics.setdefault("simulation", {})["backtest_days"] = DAYS
scanner.log_event = lambda *a, **k: None
scanner._restore_warm_memory = lambda: None
for name in indices:
    cfg = config["indices"][name]
    lot_size = int(cfg["lot_size"])
    lots = int(scanner.user_lots.get(name, 1))
    for tf in ("1min", "5min", "15min"):
        mtf.update_data(name, tf, scanner._prepare_live_signal_data(name, tf, frames[name, tf]))
    result = mtf.process_instrument(name, lot_size=lot_size, lots=lots,
                                    capital=scanner.capital_fut + scanner.capital_opt, risk_pct=scanner.risk_fut_pct)
    for tf in ("1min", "5min", "15min"):
        r = getattr(result, "results_" + tf, None)
        if r:
            scanner._cached_results[f"{name}_{tf}"] = r
    df = frames[(name, "5min")]
    spot = float(df["close"].iloc[-1])
    step = int(cfg["strike_interval"])
    intel = scanner.intel.analyze(name, "5min", df, options_chain=None, spot_price=spot)
    score = aggregate_intelligence_score(intel.get("aggregate", {}))
    regime = intel.get("regime", {}).get("regime", "UNKNOWN")
    scanner.intel.regime.reset_history(name)
    scanner.signal_processor.process_best_signal(name, result, intel, score, regime, lots, lot_size, cfg, spot,
                                                 round(spot / step) * step)
    print(json.dumps(dict(index=name, closed=len(trades.closed_trades))), flush=True)

rows = [t.to_dict() for t in trades.closed_trades]
summary = dict(
    trades=len(rows), net=round(sum(float(r.get("pnl") or 0) for r in rows), 1),
    wins=sum(float(r.get("pnl") or 0) > 0 for r in rows),
    by_tf={k: round(sum(float(r.get("pnl") or 0) for r in rows if r["timeframe"] == k), 1) for k in {r["timeframe"] for r in rows}},
    by_reason=Counter(str(r.get("exit_reason")) for r in rows),
    by_lane=Counter(str(r.get("source_lane")) for r in rows),
    by_index={k: round(sum(float(r.get("pnl") or 0) for r in rows if str(r["instrument"]).startswith(k)), 1) for k in indices},
    network_attempts=len(attempts),
    diagnostics_option_history=scanner._diagnostics.get("option_history", {}),
    rejects=scanner._diagnostics.get("rejects", {}),
    rejected_rows=sorted(
        (str(getattr(c, "instrument", "")), str(getattr(c, "signal_timestamp", "")), str(getattr(c, "status", "")),
         "; ".join(map(str, getattr(c, "reasons", []) or []))[:160])
        for inst in list((getattr(scanner, "_session_trade_candidates", {}) or {}).keys())
        for c in scanner._get_session_trade_candidates(inst)
        if str(getattr(c, "status", "")).upper().startswith(("REJECTED", "NO ENTRY", "SKIP"))
    ),
)
OUT.write_text(json.dumps(dict(summary=summary, trades=rows), indent=1, default=str), encoding="utf-8")
print(json.dumps(summary, default=str), flush=True)
for key in ("_scanner_live_executor", "_engine_lab_executor"):
    ex = getattr(scanner, key, None)
    if ex is not None:
        ex.shutdown(wait=False)
