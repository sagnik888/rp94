"""S1+S2 Dual engine adapter (engine/s12_engine.py): names, settings, frame adapter,
closed-bar feeding, the 15:15 EOD/BTST contract, incremental == full run, the fast scanner,
event -> signal mapping, the host result and the bar-close worker."""
from __future__ import annotations

from dataclasses import fields
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from engine import s12_engine as E
from engine.s12dual import Config, DualScanner
from engine.s12dual.engine import (EngineState, Trade, early_loss_action, hard_stop_price, nearest_stop,
                                   runner_stop_price)
from engine.s12dual.mtf import aggregate
from tests.s12dual.generate_demo import make

IST = "Asia/Kolkata"
DATA = Path(__file__).resolve().parents[1] / "data_store" / "candles"


def host_frame(minute: pd.DataFrame, minutes: int) -> pd.DataFrame:
    """The candle store's shape: naive IST bar opens, OHLCV plus a string volume_basis."""
    df = minute.copy()
    df.index = df.index.tz_convert(IST).tz_localize(None)
    if minutes > 1:
        df = df.resample(f"{minutes}min", label="left", closed="left").agg(
            {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
        ).dropna(subset=["open", "close"])
    df["volume"] = df["volume"].astype("int64")
    df["volume_basis"] = pd.array(["fyers_index"] * len(df), dtype="string")
    df["source"] = pd.array(["fyers_history"] * len(df), dtype="string")
    return df


@pytest.fixture(scope="module")
def minute():
    return make(12)


@pytest.fixture(scope="module")
def m5(minute):
    return host_frame(minute, 5)


@pytest.fixture(scope="module")
def m15(minute):
    return host_frame(minute, 15)


@pytest.fixture(scope="module")
def end_clock(m5):
    return pd.Timestamp(m5.index[-1].date()) + pd.Timedelta(hours=15, minutes=30)


@pytest.fixture(scope="module")
def cfg():
    return E.config_from_settings(SimpleNamespace())


@pytest.fixture(scope="module")
def run5(m5, end_clock, cfg):
    runner = E.IncrementalRunner("NIFTY", "5min")
    runner.advance(E.prepare_frame(m5, "5min", as_of=end_clock), None, cfg)
    return runner


@pytest.fixture(scope="module")
def result5(m5, end_clock):
    return E.process(m5, "NIFTY", "5min", settings=SimpleNamespace(), as_of=end_clock, synchronous=True)


# ─────────────────────────────────────────────────────────────── names and settings


def test_fixed_names_never_reuse_the_s1_lane_tags():
    assert E.ENGINE_KEY == "s1s2_dual"
    assert E.DISPLAY_NAME == "S1+S2 Dual (Pine v5.4)"
    assert E.LANES == ("D1", "D2")
    assert E.EXIT_POLICIES == {"D1": "s12_d1_native", "D2": "s12_d2_native"}
    assert set(E.LANE_OF_ENGINE.values()) == {"D1", "D2"}
    assert E.FEED_BARS >= 900 and E.FEED_BARS_1MIN >= 500
    assert E.warmup_bars("5min") == E.warmup_bars("15min") == 900 and E.warmup_bars("1min") == 500


@pytest.mark.parametrize("instrument,key,symbol", [
    ("NIFTY", "NIFTY", "NSE:NIFTY"), ("BANKNIFTY", "BANKNIFTY", "NSE:BANKNIFTY"),
    ("MIDCPNIFTY", "MIDCPNIFTY", "NSE:MIDCPNIFTY"), ("SENSEX", "SENSEX", "BSE:SENSEX"),
    ("nifty 50", "NIFTY", "NSE:NIFTY"), ("NSE:NIFTY50-INDEX", "NIFTY", "NSE:NIFTY"),
    ("NSE:NIFTYBANK-INDEX", "BANKNIFTY", "NSE:BANKNIFTY"), ("MIDCAPNIFTY", "MIDCPNIFTY", "NSE:MIDCPNIFTY"),
    ("NIFTY BANK", "BANKNIFTY", "NSE:BANKNIFTY"), ("NSE:NIFTY MID SELECT", "MIDCPNIFTY", "NSE:MIDCPNIFTY"),
    ("NIFTY 26OCT 25000 CE", "NIFTY", "NSE:NIFTY"),
])
def test_symbol_mapping_carries_the_exchange_prefix(instrument, key, symbol):
    assert E.index_key(instrument) == key
    assert E.symbol_for(instrument) == symbol


def test_unknown_instrument_and_timeframe_are_refused():
    assert E.index_key("RELIANCE") == ""
    with pytest.raises(ValueError):
        E.symbol_for("RELIANCE")
    assert E.host_timeframe("60min") == ""
    assert (E.host_timeframe("5"), E.host_timeframe("15m"), E.host_timeframe("1min")) == ("5min", "15min", "1min")
    assert "error" in E.process(pd.DataFrame(), "RELIANCE", "5min", synchronous=True)


@pytest.mark.parametrize("symbol", sorted(E.SYMBOLS.values()))
def test_every_index_is_an_index_for_both_package_engines(symbol, m5, end_clock, cfg):
    frame = E.prepare_frame(m5, "5min", as_of=end_clock).iloc[:120]
    sc = E.scanner_class()(frame, symbol=symbol, timeframe="5", config=cfg, tick_size=E.TICK_SIZE,
                           security_type=E.SECURITY_TYPE)
    assert sc.security_type == "index" and sc.instrument_type == "index"
    assert sc.s2_is_index is True and sc.s2_is_commodity is False
    assert sc.native_region == "India" and sc.india_cash is True and sc.tz == IST


def test_setting_fields_are_s12_prefixed_and_use_the_package_pine_bounds():
    config_fields = {f.name for f in fields(Config)}
    for name, (config_field, spec) in E.SETTING_FIELDS.items():
        assert name.startswith("s12_")
        assert not name.startswith(("s1_", "uts_", "dual_lane_"))
        assert config_field in config_fields
        assert config_field not in E.FIXED_CONFIG
        default = E.SETTING_DEFAULTS[name]
        assert default == E.APP_CONFIG_DEFAULTS.get(config_field, getattr(Config(), config_field))
        if spec[0] in {"int", "float"}:
            lo, hi = spec[1], spec[2]
            Config(**{config_field: lo}).validate()
            Config(**{config_field: hi}).validate()
            step = 1 if spec[0] == "int" else 1e-6
            for outside in (lo - step, hi + step):
                with pytest.raises(ValueError):
                    Config(**{config_field: outside}).validate()
        elif spec[0] == "enum":
            for choice in spec[1]:
                Config(**{config_field: choice}).validate()
            with pytest.raises(ValueError):
                Config(**{config_field: "not-a-choice"}).validate()


def test_config_fixes_the_bot_contract_whatever_the_settings_say():
    cfg = E.config_from_settings(SimpleNamespace(s12_d1_stop_pct=0.35, s12_d2_orb_minutes=45))
    assert (cfg.s1_trade_mode, cfg.s2_trade_mode) == ("Intraday", "Intraday")
    assert cfg.s2_enable_btst is False
    assert cfg.s1_eod_cutoff_ist_min == 915 and cfg.s2_force_exit_ist_decimal == 15.25
    assert cfg.no_volume_policy == "Price proxy" and cfg.s2_confirmed_mtf is True
    assert cfg.enable_s1 and cfg.enable_s2 and not cfg.block_conflicting_entries
    assert cfg.s1_stop_pct == 0.35 and cfg.s2_orb_minutes == 45


def test_config_clamps_out_of_bound_settings_into_the_pine_ranges():
    cfg = E.config_from_settings(SimpleNamespace(
        s12_d1_min_score=150, s12_d1_stop_pct=-1, s12_d2_risk_mode="Bogus", s12_d2_burst="false",
        s12_t1_r=float("nan"), s12_d2_confidence_floor=61.6))
    assert cfg.min_score == 90.0 and isinstance(cfg.min_score, float)
    assert cfg.s1_stop_pct == 0.05
    assert cfg.s2_risk_mode == "Percentage"
    assert cfg.s2_burst is False
    assert cfg.t1_r == E.APP_CONFIG_DEFAULTS["t1_r"]
    assert cfg.s2_confidence_floor == 62
    cfg.validate()


def test_selected_policy_defaults_match_across_both_lanes_and_enable_one_minute_bursts():
    from config.settings import Settings

    settings = Settings(_env_file=None)
    cfg = E.config_from_settings(settings)
    assert settings.options_sl_pct == 15.0
    assert settings.s12_options_operator_cap_pct == 10.0
    assert settings.s12_timeframes == "1min,5min,15min"
    assert settings.s12_d1_stop_pct == settings.s12_d2_percent_stop_pct == 0.1
    assert settings.s12_d2_risk_mode == "Percentage"
    assert (cfg.t1_r, cfg.t2_r, cfg.t3_r) == (1.5, 2.25, 4.0)
    assert (cfg.s2_t1_manual, cfg.s2_t2_manual, cfg.s2_t3_manual) == (1.5, 2.25, 4.0)
    assert cfg.share_target_ladder and cfg.s2_burst

    d1 = E.d1_levels(25000.0, True, cfg.s1_stop_pct, (cfg.t1_r, cfg.t2_r, cfg.t3_r))
    assert d1 == pytest.approx((24975.0, 25037.5, 25056.25, 25100.0))

    def lag(key, _offset=0):
        return 24990.0 if key == "low" else 25010.0

    d2_scanner = SimpleNamespace(cfg=cfg, _s2_mode="Intraday", tick=E.TICK_SIZE, lag=lag)
    d2_stop = DualScanner._s2_stop(d2_scanner, {"atrV": 10.0}, True, 25000.0, "NONE")
    assert d2_stop == pytest.approx(24975.0)


def test_config_hash_tracks_every_input():
    a = E.config_from_settings(SimpleNamespace())
    b = E.config_from_settings(SimpleNamespace(s12_d2_signal_gap=2))
    assert E.config_hash(a) == E.config_hash(E.config_from_settings(SimpleNamespace()))
    assert E.config_hash(a) != E.config_hash(b)


@pytest.mark.parametrize("long,peaks,expected", [
    (True, (25037.5, 25056.25, 25100.0, 25200.0), (25015.0, 25031.25, 25075.0, 25175.0)),
    (False, (24962.5, 24943.75, 24900.0, 24800.0), (24985.0, 24968.75, 24925.0, 24825.0)),
])
def test_runner_stop_ratchets_by_peak_r_and_keeps_running_past_t3(long, peaks, expected):
    entry, risk = 25000.0, 25.0
    stop = entry - risk if long else entry + risk
    for peak, wanted in zip(peaks, expected):
        close = peak
        stop = runner_stop_price(entry, long, peak, risk, stop, close, .05, .6)
        assert stop == pytest.approx(wanted)


def test_early_loss_action_tightens_only_a_weak_unproven_trade():
    stop, exit_now = early_loss_action(25000.0, True, 24990.0, 25.0, 25005.0,
                                       24975.0, 2, False)
    assert stop == pytest.approx(24981.25) and not exit_now
    stop, exit_now = early_loss_action(25000.0, True, 24980.0, 25.0, 25005.0,
                                       24981.25, 2, False)
    assert stop == pytest.approx(24981.25) and exit_now
    assert early_loss_action(25000.0, True, 24985.0, 25.0, 25005.0,
                             24975.0, 2, True) == (24975.0, False)
    assert early_loss_action(25000.0, True, 24985.0, 25.0, 25025.0,
                             24975.0, 3, False) == (24975.0, False)


def test_early_loss_action_is_symmetric_for_short_trades():
    stop, exit_now = early_loss_action(25000.0, False, 25010.0, 25.0, 24995.0,
                                       25025.0, 2, False)
    assert stop == pytest.approx(25018.75) and not exit_now


def test_early_loss_exit_applies_at_least_a_15_minute_lane_pause():
    scanner = object.__new__(DualScanner)
    scanner.cfg = Config(circuit_pause_bars=5)
    scanner.minutes = 1
    scanner.i = 100
    scanner.event = lambda *args, **kwargs: None
    trade = Trade("LONG", 25000.0, 99, "loss", 24975.0, 24975.0,
                  25037.5, 25056.25, 25100.0, "probe")
    state = EngineState(active=trade)
    scanner._record_exit(state, "S2", trade, 24987.5, "EARLY LOSS CUT")
    assert state.active is None and state.pause_until == 115


def test_both_lanes_keep_a_t3_winner_open_and_publish_a_dynamic_r_stop(m5, end_clock, cfg):
    frame = E.prepare_frame(m5, "5min", as_of=end_clock)
    scanner = E.scanner_class()(frame, symbol="NSE:NIFTY", timeframe="5", config=cfg,
                                tick_size=E.TICK_SIZE, security_type=E.SECURITY_TYPE)
    scanner.run()
    candidate = None
    for i in range(70, len(scanner.data) - 10):
        scanner.i = i
        x = scanner._features()
        strong_long = (x["close"] > x["ema50"] and x["adx"] > 25 and x["di_plus"] > x["di_minus"]
                       and x["s2_macd"] > x["s2_macd_sig"] and x["s2_macdhist"] > 0)
        if not (strong_long and x["high"] > x["close"] and x["close"] > x["open"]):
            continue
        risk = max((x["high"] - x["low"]) / 4.5, .5)
        entry = x["high"] - 4 * risk
        stop = entry - risk
        if x["low"] > stop + scanner.tick:
            candidate = (i, x, entry, stop, risk)
            break
    assert candidate is not None, "fixture needs a bullish bar that reaches T3 without touching its initial stop"
    i, x, entry, stop, risk = candidate
    d1 = Trade("LONG", entry, i - 1, "D1-probe", stop, stop, entry + 1.5 * risk,
               entry + 2.25 * risk, entry + 4 * risk, "probe", peak=entry, trough=entry)
    d2 = Trade("LONG", entry, i - 1, "D2-probe", stop, stop, entry + 1.5 * risk,
               entry + 2.25 * risk, entry + 4 * risk, "probe", peak=entry, trough=entry)
    scanner.s1.active = d1
    scanner.s2.active = d2
    scanner.i = i
    scanner._s1_bar(x, scanner._session("S1"))
    scanner._s2_bar(x, scanner._session("S2"))
    assert scanner.s1.active is d1 and scanner.s2.active is d2
    assert d1.t3_hit and d2.t3_hit
    assert d1.trailing > entry and d2.trailing > entry
    exits = [e for e in scanner.events if e.get("event") == "EXIT" and e.get("trade_id") in {"D1-probe", "D2-probe"}]
    assert exits == []
    stops = [e for e in scanner.events if e.get("event") == "STOP_UPDATED" and e.get("trade_id") in {"D1-probe", "D2-probe"}]
    assert {e["trade_id"] for e in stops} == {"D1-probe", "D2-probe"}


# ─────────────────────────────────────────────────────────────── frame adapter


def _raw_bars(stamps, rows, **extra):
    df = pd.DataFrame(rows, columns=["open", "high", "low", "close", "volume"], index=pd.DatetimeIndex(stamps))
    for key, values in extra.items():
        df[key] = values
    return df


def test_frame_adapter_keeps_closed_session_bars_as_float64_ist():
    stamps = ["2026-10-09 09:05", "2026-10-09 09:15", "2026-10-09 09:20", "2026-10-09 15:10",
              "2026-10-09 15:15", "2026-10-09 15:25"]
    rows = [[100, 101, 99, 100, 10]] * len(stamps)
    df = _raw_bars(stamps, rows, volume_basis=pd.array(["fyers_index"] * 6, dtype="string"),
                   source=pd.array(["x"] * 6, dtype="string"))
    stats = {}
    out = E.prepare_frame(df, "5min", as_of="2026-10-09 15:30", stats=stats)
    assert list(out.columns) == ["open", "high", "low", "close", "volume"]
    assert all(dtype == np.float64 for dtype in out.dtypes)
    assert str(out.index.tz) == IST
    assert [t.strftime("%H:%M") for t in out.index] == ["09:15", "09:20", "15:10"]
    assert stats["off_session"] == 3 and stats["forming"] == 0


def test_frame_adapter_never_feeds_the_forming_bar():
    stamps = ["2026-10-09 10:00", "2026-10-09 10:05", "2026-10-09 10:10"]
    df = _raw_bars(stamps, [[100, 101, 99, 100, 10]] * 3)
    out = E.prepare_frame(df, "5min", as_of="2026-10-09 10:14:59")
    assert [t.strftime("%H:%M") for t in out.index] == ["10:00", "10:05"]
    assert len(E.prepare_frame(df, "5min", as_of="2026-10-09 10:15")) == 3
    aware = df.copy()
    aware.index = aware.index.tz_localize(IST).tz_convert("UTC")
    assert E.prepare_frame(aware, "5min", as_of="2026-10-09 10:14:59").index.equals(out.index)


def test_frame_adapter_masks_unusable_volume_to_nan():
    stamps = [f"2026-10-09 10:{m:02d}" for m in range(0, 30, 5)]
    rows = [[100, 101, 99, 100, v] for v in (10, 0, -5, 20, 30, 40)]
    basis = pd.array(["fyers_index", "fyers_index", "fyers_index", "", "ticks", pd.NA], dtype="string")
    out = E.prepare_frame(_raw_bars(stamps, rows, volume_basis=basis), "5min", as_of="2026-10-09 12:00")
    assert out["volume"].iloc[0] == 10
    assert out["volume"].iloc[1:].isna().all()
    no_volume = _raw_bars(stamps, rows).drop(columns=["volume"])
    assert E.prepare_frame(no_volume, "5min", as_of="2026-10-09 12:00")["volume"].isna().all()


def test_frame_adapter_repairs_or_drops_inconsistent_ohlc():
    stamps = [f"2026-10-09 10:{m:02d}" for m in range(0, 30, 5)] + ["2026-10-09 10:25"]
    rows = [
        [100, 99, 98, 100.5, 1],     # high below close -> widened
        [100, 101, 102, 100, 1],     # low above high -> narrowed to min(o,h,l,c)
        [np.nan, 101, 99, 100, 1],   # missing open -> dropped
        [100, 101, 0, 100, 1],       # non-positive low -> dropped
        [100, 101, 99, 100, 1],
        [100, 101, 99, 100, 1],      # duplicated stamp below keeps the last row
        [200, 201, 199, 200, 1],
    ]
    stats = {}
    out = E.prepare_frame(_raw_bars(stamps, rows), "5min", as_of="2026-10-09 12:00", stats=stats)
    assert stats["bad_dropped"] == 2 and stats["repaired"] == 2 and stats["duplicates"] == 1
    assert out.loc[out.index[0], "high"] == 100.5 and out.loc[out.index[1], "low"] == 100
    assert out["close"].iloc[-1] == 200
    from engine.s12dual.mtf import require_candles
    require_candles(out, "adapter")


def test_fifteen_minute_chart_requires_and_is_trimmed_to_its_5m_feed(m5, m15, end_clock, cfg):
    assert "needs the closed 5min feed" in E.process(m15, "NIFTY", "15min", as_of=end_clock, synchronous=True)["error"]
    frame = E.prepare_frame(m15, "15min", as_of=end_clock)
    aux = E.aux_feeds("15min", m5, as_of=end_clock)
    assert set(aux) == {"5"}
    lagging = {"5": aux["5"].iloc[:-2]}   # the 5m bar closing with the last 15m bar is missing
    covered = E._covered_by_aux(frame, 15, lagging)
    assert len(covered) == len(frame) - 1
    runner = E.IncrementalRunner("NIFTY", "15min")
    runner.advance(frame, lagging, cfg)
    assert runner.fed.index[-1] == frame.index[-2]
    assert runner.advance(frame, aux, cfg) == "update"
    assert runner.fed.index[-1] == frame.index[-1]


# ─────────────────────────────────────────────────────────────── parity fix and EOD contract


VENDORED = Path(__file__).resolve().parents[1] / "engine" / "s12dual"
# The one local change to the vendored engine (DESIGN "Parity fix"), as (patched, original).
PARITY_FIX = (
    ('    def _s2_close_minute(self):\n'
     '        """Minute of the bar\'s real close on S2\'s clock (Pine s2_istCloseDecimal/s2_sessionCloseDecimal)."""\n'
     '        loc=self._bar_closes[self.i].tz_convert(self.s2_tz)\n'
     '        return loc.hour*60+loc.minute\n\n', ''),
    ("        # Pine 1411/1413 test the bar's real time_close, not open+TF: a session-truncated\n"
     "        # last bar (30m 15:15 closes 15:30) must still force-exit and block entries.\n"
     "        close_min=self._s2_close_minute() if is_intra and ses['market'] and not use_native_last else ses['end']\n",
     ''),
    ("                  (close_min>=last_force or close_min+self.minutes>last_force) and close_min<=ses['session_end'])\n",
     "                  (ses['end']>=last_force or ses['end']+self.minutes>last_force) and ses['end']<=ses['session_end'])\n"),
)


def test_vendored_source_hashes_and_documented_local_exit_policy_are_pinned():
    import hashlib
    import re

    header = (VENDORED / "__init__.py").read_text()
    recorded = dict(re.findall(r"^\s+(?:scanner/)?(\S+\.(?:py|json))\s+([0-9a-f]{64})\s*$", header, re.M))
    assert set(recorded) == {"__init__.py", "config.py", "engine.py", "generated_rules.py", "indicators.py",
                             "mtf.py", "timing.py", "config.defaults.json"}
    assert "1.2.0" in header and "S1_S2_DUAL_PYTHON_SCANNER_v1.2_SINGLE_STOP_PARITY_FIXED" in header

    def sha(text: str) -> str:
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    for name in ("config.py", "generated_rules.py", "indicators.py", "mtf.py", "timing.py", "config.defaults.json"):
        assert hashlib.sha256((VENDORED / name).read_bytes()).hexdigest() == recorded[name], name
    package_init = header[header.index('"""', 3) + 3:].lstrip("\n")
    assert sha(package_init) == recorded["__init__.py"]
    engine_source = (VENDORED / "engine.py").read_bytes().decode("utf-8")
    original = engine_source
    for patched, before in PARITY_FIX:
        assert engine_source.count(patched) == 1
        original = original.replace(patched, before)
    local_policy = re.search(r"SHA256 after removing only the clock parity patch: ([0-9a-f]{64})", header)
    assert local_policy, "vendored header must pin the known R-runner/early-loss policy source"
    assert sha(original) == local_policy.group(1)


def test_s2_force_exit_reads_the_real_bar_close_on_a_truncated_last_bar(minute):
    chart = aggregate(minute, "1", "30", IST, 555)
    sc = DualScanner(chart, symbol="NSE:NIFTY", timeframe="30", mtf={"1": minute}, security_type="index",
                     config=Config(s2_trade_mode="Intraday"))
    i = next(k for k, t in enumerate(sc._local) if (t.hour, t.minute) == (15, 15) and k > 100)
    sc.i = i
    ses = sc._session("S2")
    assert ses["end"] == 945 and sc._s2_close_minute() == 930   # open+TF vs the real 15:30 close
    ep = float(chart["close"].iloc[i - 3])
    sc.s2.active = Trade("LONG", ep, i - 3, "probe", ep * .5, ep * .5, ep * 2, ep * 3, ep * 4, "P", peak=ep, trough=ep)
    sc._s2_bar(sc._features(), ses)
    assert sc.s2.active is None and sc.events[-1]["reason"] == "INTRADAY EOD"


def test_fast_scanner_matches_the_package_on_truncated_bars(minute):
    chart = aggregate(minute, "1", "30", IST, 555)
    kwargs = dict(symbol="NSE:NIFTY", timeframe="30", mtf={"1": minute}, security_type="index",
                  config=Config(s2_trade_mode="Intraday"))
    assert DualScanner(chart, **kwargs).run()["events"] == E.FastDualScanner(chart, **kwargs).run()["events"]


def test_engine_exits_every_trade_by_1515_and_holds_nothing_overnight(run5):
    events = run5.scanner.events
    assert any(e["event"] == "EXIT" for e in events)
    assert all(e.get("subtype", "NONE") == "NONE" for e in events if e["event"] == "ENTRY")
    reasons = {e["reason"] for e in events if e["event"] == "EXIT"}
    assert not reasons & {"ORPHAN INTRADAY NEXT OPEN", "NEW DAY GAP CLOSE"}
    entries = {}
    for e in events:
        stamp = pd.Timestamp(e["bar_open"]).tz_convert(IST)
        assert (stamp.hour, stamp.minute) < (15, 15)
        if e["event"] in {"FILL_ESTIMATE", "ENTRY"}:
            entries[e["trade_id"]] = stamp
        elif e["event"] == "EXIT":
            assert stamp.date() == entries[e["trade_id"]].date()
    positions = run5.scanner.current_positions()
    last = run5.scanner.data.index[-1].tz_convert(IST)
    if (last.hour, last.minute) == (15, 10):
        assert positions == {"S1": None, "S2": None}
    eod = [e for e in events if e["event"] == "EXIT" and "EOD" in e["reason"]]
    assert all(pd.Timestamp(e["bar_open"]).tz_convert(IST).strftime("%H:%M") == "15:10" for e in eod)


# ─────────────────────────────────────────────────────────────── incremental == full


def _scanner_state(sc):
    return sc.events, sc.snapshots, sc.current_positions(), sc.s1.trades + sc.s2.trades


def test_incremental_updates_equal_a_full_run_on_5m(m5, end_clock, cfg):
    frame = E.prepare_frame(m5, "5min", as_of=end_clock)
    last_day = frame.index[-1].date()
    start = int(np.flatnonzero(frame.index.date == last_day)[0]) + 30
    runner = E.IncrementalRunner("NIFTY", "5min")
    modes = [runner.advance(frame.iloc[:k], None, cfg) for k in range(start, len(frame) + 1)]
    assert modes[0] == "full" and set(modes[1:]) == {"update"}
    assert runner.full_runs == 1 and runner.updates == len(frame) - start
    fresh = E.IncrementalRunner("NIFTY", "5min")
    fresh.advance(frame, None, cfg)
    assert _scanner_state(runner.scanner) == _scanner_state(fresh.scanner)
    assert runner.advance(frame, None, cfg) == "unchanged"


def test_incremental_updates_equal_a_full_run_on_15m_with_the_5m_feed(m5, m15, end_clock, cfg):
    frame = E.prepare_frame(m15, "15min", as_of=end_clock)
    five = E.prepare_frame(m5, "5min", as_of=end_clock)
    last_day = frame.index[-1].date()
    start = int(np.flatnonzero(frame.index.date == last_day)[0]) + 8
    runner = E.IncrementalRunner("NIFTY", "15min")
    for k in range(start, len(frame) + 1):
        close = frame.index[k - 1] + pd.Timedelta(minutes=15)
        runner.advance(frame.iloc[:k], {"5": five.loc[five.index + pd.Timedelta(minutes=5) <= close]}, cfg)
    assert runner.full_runs == 1 and runner.updates == len(frame) - start
    fresh = E.IncrementalRunner("NIFTY", "15min")
    fresh.advance(frame, {"5": five}, cfg)
    assert _scanner_state(runner.scanner) == _scanner_state(fresh.scanner)


def test_runner_reruns_in_full_on_a_new_day_a_config_change_or_a_changed_history(m5, end_clock, cfg):
    frame = E.prepare_frame(m5, "5min", as_of=end_clock)
    days = sorted(set(frame.index.date))
    day_end = int(np.flatnonzero(frame.index.date == days[-2])[-1]) + 1
    runner = E.IncrementalRunner("NIFTY", "5min")
    assert runner.advance(frame.iloc[:day_end - 1], None, cfg) == "full"
    assert runner.advance(frame.iloc[:day_end], None, cfg) == "update"
    assert runner.advance(frame.iloc[:day_end + 1], None, cfg) == "full"              # new session day
    assert runner.advance(frame.iloc[:day_end + 2], None, cfg) == "update"
    other = E.config_from_settings(SimpleNamespace(s12_d2_signal_gap=3))
    assert runner.advance(frame.iloc[:day_end + 3], None, other) == "full"            # config change
    changed = frame.iloc[:day_end + 4].copy()
    changed.iloc[day_end + 1, changed.columns.get_loc("close")] += 0.05
    changed.iloc[day_end + 1, changed.columns.get_loc("high")] += 0.05
    assert runner.advance(changed, None, other) == "full"                              # history changed
    assert runner.advance(changed.iloc[5:], None, other) == "unchanged"                # trimmed head only
    assert runner.advance(frame.iloc[5:day_end + 5], None, other) == "full"            # restored correction
    trimmed_next = frame.iloc[6:day_end + 6]
    assert runner.advance(trimmed_next, None, other) == "update"                       # trimmed head + new bar
    fresh = E.IncrementalRunner("NIFTY", "5min")
    fresh.advance(frame.iloc[5:day_end + 6], None, other)
    assert _scanner_state(runner.scanner) == _scanner_state(fresh.scanner)


def test_synchronous_runner_never_carries_state_across_a_different_first_bar(m5, end_clock, cfg):
    frame = E.prepare_frame(m5, "5min", as_of=end_clock)
    runner = E.IncrementalRunner("NIFTY", "5min", allow_trimmed_head=False)
    assert runner.advance(frame.iloc[:-3], None, cfg) == "full"
    assert runner.advance(frame.iloc[:-2], None, cfg) == "update"
    assert runner.advance(frame.iloc[5:-2], None, cfg) == "full"                  # trimmed head
    assert runner.advance(frame.iloc[5:], None, cfg) == "update"
    fresh = E.IncrementalRunner("NIFTY", "5min")
    fresh.advance(frame.iloc[5:], None, cfg)
    assert _scanner_state(runner.scanner) == _scanner_state(fresh.scanner)
    first = E.compute_now("NIFTY", "5min", frame, None, cfg)
    later = E.compute_now("NIFTY", "5min", frame.iloc[5:], None, cfg)
    assert first.compute_mode in {"full", "update", "unchanged"} and later.compute_mode == "full"
    assert later.events == E.build_snapshot(fresh).events


@pytest.mark.parametrize("tf,minutes", [("5", 5), ("15", 15)])
def test_fast_scanner_is_event_identical_to_the_package(minute, tf, minutes, cfg):
    chart = aggregate(minute, "1", tf, IST, 555)
    chart = chart[(chart.index.tz_convert(IST).hour * 60 + chart.index.tz_convert(IST).minute) < 915]
    mtf = {"5": aggregate(minute, "1", "5", IST, 555)} if tf == "15" else None
    kwargs = dict(symbol="NSE:NIFTY", timeframe=tf, mtf=mtf, config=cfg, tick_size=.05, security_type="index")
    base, fast = DualScanner(chart, **kwargs), E.FastDualScanner(chart, **kwargs)
    a, b = base.run(), fast.run()
    assert a["events"] and a["events"] == b["events"] and a["bars"] == b["bars"] and a["ledger"] == b["ledger"]
    cut = len(chart) - 20
    base, fast = DualScanner(chart.iloc[:cut], **kwargs), E.FastDualScanner(chart.iloc[:cut], **kwargs)
    base.run(), fast.run()
    assert base.update(chart.iloc[cut:]) == fast.update(chart.iloc[cut:])
    assert base.snapshots == fast.snapshots


@pytest.mark.skipif(not (DATA / "NIFTY_1min.csv").exists(), reason="no NIFTY candles on disk")
def test_real_nifty_incremental_and_fast_scanner_match_the_package(cfg):
    raw = pd.read_csv(DATA / "NIFTY_1min.csv", index_col=0, parse_dates=True)
    agg = {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum", "volume_basis": "last"}
    m5 = raw.resample("5min", label="left", closed="left").agg(agg).dropna(subset=["open", "close"])
    clock = pd.Timestamp(m5.index[-1].date()) + pd.Timedelta(hours=15, minutes=30)
    frame = E.prepare_frame(m5, "5min", as_of=clock)
    frame = frame.iloc[-972:]
    kwargs = dict(symbol="NSE:NIFTY", timeframe="5", config=cfg, tick_size=.05, security_type="index")
    package = DualScanner(frame, **kwargs).run()
    assert package == E.FastDualScanner(frame, **kwargs).run()
    runner = E.IncrementalRunner("NIFTY", "5min")
    for k in range(len(frame) - 12, len(frame) + 1):
        runner.advance(frame.iloc[:k], None, cfg)
    assert runner.updates == 12
    assert runner.scanner.events == package["events"] and runner.scanner.snapshots == package["bars"]


# ─────────────────────────────────────────────────────────────── event -> signal mapping


def test_one_signal_per_setup_or_entry_event(result5, run5):
    events = run5.scanner.events
    setups = [e for e in events if e["engine"] == "S1" and e["event"] == "SETUP"]
    entries = [e for e in events if e["engine"] == "S2" and e["event"] == "ENTRY"]
    signals = result5["signals"]
    assert setups and entries
    assert len(signals) == len(setups) + len(entries)
    assert len({s.s12_event_id for s in signals}) == len(signals)
    assert {s.s12_event_id for s in signals} == {e["id"] for e in setups + entries}
    assert not any(s.s12_event_id.endswith("FILL_ESTIMATE") for s in signals)
    from engine.ut_bot_core import UTBotSignal
    for sig in signals:
        assert isinstance(sig, UTBotSignal)
        assert sig.source_engine == "s1s2_dual" and sig.source_engines == ["s1s2_dual"]
        assert sig.source_lane == sig.s12_lane and sig.s12_lane in {"D1", "D2"}
        assert sig.signal_type in {"BUY", "SELL"} and sig.position_state == (1 if sig.signal_type == "BUY" else -1)
        assert sig.s12_forming is False and sig.timestamp.tzinfo is None


def test_d1_signal_is_the_setup_bar_with_levels_from_its_stop_pct(result5, run5, cfg, m5):
    setups = {e["id"]: e for e in run5.scanner.events if e["event"] == "SETUP"}
    d1 = [s for s in result5["signals"] if s.s12_lane == "D1"]
    assert d1
    for sig in d1:
        event = setups[sig.s12_event_id]
        assert pd.Timestamp(event["bar_open"]).tz_convert(IST).tz_localize(None) == pd.Timestamp(sig.timestamp)
        assert sig.price == event["signal_close"] == sig.s12_signal_close
        assert m5.index[sig.bar_index] == pd.Timestamp(sig.timestamp)
        long = sig.signal_type == "BUY"
        pct = min(cfg.s1_stop_pct, cfg.hard_stop_intraday_pct)
        assert sig.s12_stop_pct == pct and sig.s12_entry_basis == "NEXT_BAR_OPEN"
        assert sig.s12_stop == nearest_stop(hard_stop_price(sig.price, long, pct), .05, long)
        risk = abs(sig.price - sig.s12_stop)
        assert sig.s12_t1 == pytest.approx(sig.price + (1 if long else -1) * risk * cfg.t1_r)
        assert sig.s12_r_ladder == [1.5, 2.25, 4.0]
        assert sig.trailing_stop == sig.s12_stop and sig.stop_distance == pytest.approx(risk)
        score = run5.scanner.snapshots[run5.scanner.data.index.get_loc(pd.Timestamp(event["bar_open"]))]
        assert sig.s12_score == score["s1_bull" if long else "s1_bear"]


def test_d1_fill_estimate_is_bookkeeping_and_engine_levels_ride_alongside(result5, run5):
    fills = {e["trade_id"]: e for e in run5.scanner.events if e["event"] == "FILL_ESTIMATE"}
    for sig in (s for s in result5["signals"] if s.s12_lane == "D1"):
        fill = fills.get(sig.s12_trade_id)
        if fill is None:
            assert sig.s12_engine_entry is None
            continue
        assert pd.Timestamp(fill["bar_open"]).tz_convert(IST).tz_localize(None) > pd.Timestamp(sig.timestamp)
        assert sig.s12_engine_entry == fill["price"] and sig.s12_engine_stop == fill["stop"]


def test_d2_signal_is_the_entry_bar_with_the_package_stop_and_targets(result5, run5):
    entries = {e["id"]: e for e in run5.scanner.events if e["event"] == "ENTRY"}
    d2 = [s for s in result5["signals"] if s.s12_lane == "D2"]
    assert d2
    for sig in d2:
        event = entries[sig.s12_event_id]
        assert sig.price == event["reference_close"] and sig.s12_engine_entry == event["price"]
        assert (sig.s12_stop, sig.s12_t1, sig.s12_t2, sig.s12_t3) == (event["stop"], event["t1"], event["t2"], event["t3"])
        assert sig.s12_score == event["confidence"] and sig.s12_entry_basis == "SIGNAL_CLOSE"
        assert sig.s12_fill_cancel == ""


def test_signal_carries_the_exit_the_engine_later_gave_that_trade(result5, run5):
    exits = {e["trade_id"]: e for e in run5.scanner.events if e["event"] == "EXIT"}
    updates = {}
    for e in run5.scanner.events:
        if e["event"] == "STOP_UPDATED":
            updates.setdefault(e["trade_id"], []).append(e["stop"])
    checked = 0
    for sig in result5["signals"]:
        event = exits.get(sig.s12_trade_id)
        if event is None:
            assert sig.s12_exit_reason in {"", None} or sig.s12_exit_reason.startswith("SETUP_CANCELLED")
            continue
        checked += 1
        assert sig.s12_exit_reason == event["reason"] and sig.s12_exit_price == event["price"]
        assert pd.Timestamp(sig.s12_exit_time) == pd.Timestamp(event["bar_open"]).tz_convert(IST).tz_localize(None)
        assert [stop for _, stop in sig.s12_stop_updates] == updates.get(sig.s12_trade_id, [])
        for effective, _ in sig.s12_stop_updates:
            assert effective > sig.timestamp
    assert checked


@pytest.mark.parametrize("tf,stamp,expected", [
    (5, "2026-10-09 15:05", "INTRADAY_CUTOFF"), (5, "2026-10-09 15:00", ""),
    (15, "2026-10-09 14:45", "INTRADAY_CUTOFF"), (15, "2026-10-09 14:30", ""),
    (1, "2026-10-09 15:13", "INTRADAY_CUTOFF"), (1, "2026-10-09 15:12", ""),
    (5, "2026-10-09 15:10", "INTRADAY_NEW_DAY"),
])
def test_d1_setups_the_engine_will_cancel_are_flagged_at_signal_time(tf, stamp, expected, cfg):
    assert E.d1_fill_cancel_reason(pd.Timestamp(stamp), tf, cfg) == expected


def test_d1_cancel_flag_agrees_with_the_engine(run5, cfg):
    cancelled = {e["trade_id"]: e["reason"] for e in run5.scanner.events if e["event"] == "SETUP_CANCELLED"}
    for e in run5.scanner.events:
        if e["event"] == "SETUP":
            predicted = E.d1_fill_cancel_reason(pd.Timestamp(e["bar_open"]), 5, cfg)
            assert predicted == cancelled.get(e["trade_id"], "")


# ─────────────────────────────────────────────────────────────── host result


def test_host_result_maps_onto_the_callers_candles_and_hides_the_future(m5, run5):
    snap = E.build_snapshot(run5)
    clock = pd.Timestamp(m5.index[-1].date()) + pd.Timedelta(hours=12)
    full = m5.copy()
    res = E.host_result(snap, full, "NIFTY", "5min", as_of=clock)
    assert len(res["positions"]) == len(res["trailing_stop"]) == len(full)
    future = np.asarray(full.index > clock)
    assert all(v is None for v, f in zip(res["trailing_stop"], future) if f)
    assert all(pd.Timestamp(s.timestamp) <= clock for s in res["signals"])
    assert all(pd.Timestamp(m["time"]) <= clock for m in res["s12_markers"])
    names = [o["name"] for o in res["indicator_overlays"]]
    assert names == ["D1 Stop", "D1 T1", "D1 T2", "D1 T3", "D2 Stop", "D2 T1", "D2 T2", "D2 T3"]
    assert set(res["s12_overlays"]) == {"D1", "D2"}
    assert res["source_engine"] == "s1s2_dual" and res["state"]["s12_state_bar_time"] is not None
    assert {"s12_d1_position", "s12_d2_position", "s12_d1_stop", "s12_d2_stop", "s12_positions"} <= set(res["state"])


def test_off_session_and_forming_bars_carry_the_last_closed_values(m5, run5, end_clock):
    snap = E.build_snapshot(run5)
    res = E.host_result(snap, m5, "NIFTY", "5min", as_of=end_clock)
    idx = m5.index
    late = [k for k, t in enumerate(idx) if t.date() == idx[-1].date() and (t.hour, t.minute) >= (15, 15)]
    assert late
    last_closed = idx.get_loc(snap.index[-1])
    assert all(res["positions"][k] == res["positions"][last_closed] for k in late)
    assert res["state"]["current_price"] == float(m5["close"].iloc[-1])


def test_target_and_stop_lines_follow_open_trades(run5):
    snap = E.build_snapshot(run5)
    for lane in ("D1", "D2"):
        side = snap.arrays[f"{lane}_side"]
        stop = snap.arrays[f"{lane}_stop"]
        t1 = snap.arrays[f"{lane}_t1"]
        assert np.array_equal(np.isfinite(stop), side != 0)
        assert np.array_equal(np.isfinite(t1), side != 0)
        with pytest.raises(ValueError):
            snap.arrays[f"{lane}_t1"][0] = 1.0


# ─────────────────────────────────────────────────────────────── worker


def test_worker_publishes_without_blocking_and_skips_unchanged_bars(m5, end_clock, cfg):
    worker = E.ComputeWorker(name="s12-test", lower_priority=False)
    try:
        frame = E.prepare_frame(m5, "5min", as_of=end_clock)
        first = frame.iloc[:-1]
        assert worker.submit("NIFTY", "5min", first, config=cfg) is True
        assert worker.latest("NIFTY", "5min") is None      # submit returned before computing
        assert worker.submit("NIFTY", "5min", first, config=cfg) is False
        assert worker.wait_idle(120)
        snap = worker.latest("NIFTY", "5min")
        assert snap is not None and not snap.error and snap.compute_mode == "full"
        assert snap.signature == E.input_signature(first, None, cfg)
        assert worker.submit("NIFTY", "5min", first, config=cfg) is False
        assert worker.submit("NIFTY", "5min", frame, config=cfg) is True
        assert worker.wait_idle(120)
        assert worker.latest("NIFTY", "5min").compute_mode == "update"
        assert worker._thread.daemon
    finally:
        worker.stop()


def test_live_process_serves_the_last_published_snapshot(m5, end_clock):
    worker = E.ComputeWorker(name="s12-test-live", lower_priority=False)
    try:
        pending = E.process(m5, "NIFTY", "5min", settings=SimpleNamespace(), as_of=end_clock, worker=worker)
        assert pending.get("s12_pending") is True and pending["signals"] == []
        assert worker.wait_idle(120)
        live = E.process(m5, "NIFTY", "5min", settings=SimpleNamespace(), as_of=end_clock, worker=worker)
        assert not live.get("error") and live["s12_evaluation"]["current"] is True
        sync = E.process(m5, "NIFTY", "5min", settings=SimpleNamespace(), as_of=end_clock, synchronous=True)
        assert [s.s12_event_id for s in live["signals"]] == [s.s12_event_id for s in sync["signals"]]
        newer = E.process(m5, "NIFTY", "5min", settings=SimpleNamespace(s12_d2_signal_gap=4), as_of=end_clock,
                          worker=worker)
        assert newer["s12_evaluation"]["current"] is False     # stale snapshot served while recomputing
    finally:
        worker.stop()


def test_worker_turns_failures_into_an_error_snapshot(cfg):
    worker = E.ComputeWorker(name="s12-test-error", lower_priority=False)
    try:
        bad = pd.DataFrame({"open": [1.0], "high": [1.0], "low": [1.0], "close": [1.0], "volume": [1.0]},
                           index=pd.DatetimeIndex(["2026-10-09 09:15"]))   # naive index: the package raises
        worker.submit("NIFTY", "5min", bad, config=cfg)
        assert worker.wait_idle(60)
        snap = worker.latest("NIFTY", "5min")
        assert snap.error and "S1+S2 Dual" in snap.error
        assert E.host_result(snap, bad, "NIFTY", "5min")["error"] == snap.error
    finally:
        worker.stop()


def test_worker_retries_same_input_after_an_error_snapshot(m5, cfg, monkeypatch):
    worker = E.ComputeWorker(name="s12-test-error-retry", lower_priority=False)
    calls = []

    def compute_once_then_recover(runner, frame, aux, config, *, signature="", observed_at=None):
        calls.append(signature)
        if len(calls) == 1:
            return E.S12Snapshot(instrument="NIFTY", timeframe="5min", signature=signature,
                                 error="temporary compute failure")
        return E.S12Snapshot(instrument="NIFTY", timeframe="5min", signature=signature,
                             compute_mode="full")

    monkeypatch.setattr(E, "compute", compute_once_then_recover)
    try:
        assert worker.submit("NIFTY", "5min", m5, config=cfg, signature="same-input")
        assert worker.wait_idle(10)
        assert worker.latest("NIFTY", "5min").error

        # An errored publication must not permanently dedupe the same input.
        assert worker.submit("NIFTY", "5min", m5, config=cfg, signature="same-input")
        assert worker.wait_idle(10)
        recovered = worker.latest("NIFTY", "5min")
        assert not recovered.error and recovered.compute_mode == "full"
        assert calls == ["same-input", "same-input"]
    finally:
        worker.stop()
