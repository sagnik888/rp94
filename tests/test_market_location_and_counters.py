from datetime import datetime, timedelta
from types import SimpleNamespace

import pandas as pd

from config.settings import get_settings
from engine.signal_processor import TradeCandidate, directional_census_block
from scanner import Scanner
from trading.trade_manager import IST


def _location_scanner(frame, regime="RANGING"):
    scanner = Scanner.__new__(Scanner)
    scanner.mode = "REAL"
    scanner.candles = SimpleNamespace(get_candles=lambda *_args: frame)
    scanner.latest_regimes = {"BANKNIFTY": regime}
    scanner._candidate_15m_trend_agrees = lambda candidate: bool(
        getattr(candidate, "trend_15m_agrees", False)
    )
    scanner._setting_enabled = lambda value, default=True: bool(default if value is None else value)
    return scanner


def _location_candidate(**overrides):
    values = dict(
        instrument="BANKNIFTY",
        direction="SHORT",
        price=100.0,
        stop=90.0,
        target=120.0,
        lots=1,
        lot_size=30,
        grade="A+",
        confidence=0.9,
        timeframe="5min",
        inst_type="OPT",
        option_type="PE",
        atm_strike=57500.0,
        multiplier=-0.5,
        trading_symbol="BANKNIFTYTESTPE",
        symbol_token="TEST",
        signal_timestamp=datetime(2026, 6, 15, 10, 0),
    )
    init_overrides = {
        key: value
        for key, value in overrides.items()
        if key in TradeCandidate.__dataclass_fields__
    }
    values.update(init_overrides)
    candidate = TradeCandidate(**values)
    for key, value in overrides.items():
        if key not in init_overrides:
            setattr(candidate, key, value)
    return candidate


def _market_wide_frame(signal_ts):
    index = pd.date_range(signal_ts - timedelta(minutes=25), periods=6, freq="5min")
    return pd.DataFrame(
        {
            "open": [100.0, 101.0, 102.0, 103.0, 104.0, 111.0],
            "high": [105.0, 106.0, 107.0, 108.0, 109.0, 112.0],
            "low": [95.0, 96.0, 97.0, 98.0, 99.0, 98.0],
            "close": [101.0, 102.0, 103.0, 104.0, 105.0, 99.0],
            "volume": [1000, 1000, 1000, 1000, 1000, 1200],
        },
        index=index,
    )


def _market_wide_weak_frame(signal_ts):
    frame = _market_wide_frame(signal_ts).copy()
    frame.iloc[-1, frame.columns.get_loc("close")] = 108.0
    return frame


def _market_wide_midcap_sympathy_frame(signal_ts):
    frame = _market_wide_frame(signal_ts).copy()
    frame.iloc[-1, frame.columns.get_loc("open")] = 110.0
    frame.iloc[-1, frame.columns.get_loc("high")] = 111.0
    frame.iloc[-1, frame.columns.get_loc("low")] = 102.0
    frame.iloc[-1, frame.columns.get_loc("close")] = 102.2
    return frame


def _market_wide_scanner(frames, signal_ts, cached_names):
    scanner = Scanner.__new__(Scanner)
    scanner.active_indices = ["NIFTY", "BANKNIFTY", "SENSEX", "MIDCPNIFTY"]
    scanner.candles = SimpleNamespace(
        get_candles=lambda instrument, _tf: frames.get(instrument, frames["NIFTY"])
    )
    scanner._setting_enabled = lambda value, default=True: bool(default if value is None else value)
    scanner._cached_results = {
        f"{name}_5min": {
            "signals": [SimpleNamespace(timestamp=signal_ts, signal_type="SELL")]
        }
        for name in cached_names
    }
    return scanner


def _one_way_cascade_frame(signal_ts, *, signal_type="SELL", freq="5min", volume=1800.0):
    step = pd.Timedelta(freq)
    index = pd.date_range(pd.Timestamp(signal_ts) - (step * 9), periods=10, freq=freq)
    if str(signal_type or "").upper() == "BUY":
        return pd.DataFrame(
            {
                "open": [100.0, 99.7, 99.4, 99.0, 98.7, 98.4, 98.1, 97.8, 97.5, 97.8],
                "high": [104.0, 103.7, 103.4, 103.0, 102.7, 102.4, 102.1, 101.8, 101.5, 119.0],
                "low": [94.0, 93.7, 93.4, 93.0, 92.7, 92.4, 92.1, 91.8, 91.5, 97.0],
                "close": [99.7, 99.4, 99.0, 98.7, 98.4, 98.1, 97.8, 97.5, 97.7, 118.0],
                "volume": [1000.0] * 9 + [volume],
            },
            index=index,
        )
    return pd.DataFrame(
        {
            "open": [100.0, 100.3, 100.6, 101.0, 101.3, 101.6, 101.9, 102.2, 102.5, 102.2],
            "high": [105.0, 105.3, 105.6, 106.0, 106.3, 106.6, 106.9, 107.2, 107.5, 108.0],
            "low": [95.0, 95.3, 95.6, 96.0, 96.3, 96.6, 96.9, 97.2, 97.5, 87.0],
            "close": [100.3, 100.6, 101.0, 101.3, 101.6, 101.9, 102.2, 102.5, 102.3, 88.0],
            "volume": [1000.0] * 9 + [volume],
        },
        index=index,
    )


def _one_way_cascade_scanner(frames):
    scanner = Scanner.__new__(Scanner)
    scanner.active_indices = ["NIFTY", "BANKNIFTY", "SENSEX", "MIDCPNIFTY"]
    scanner.candles = SimpleNamespace(
        get_candles=lambda instrument, tf: frames.get((instrument, tf), frames[("NIFTY", tf)])
    )
    scanner._setting_enabled = lambda value, default=True: bool(default if value is None else value)
    scanner._cached_results = {}
    return scanner


def _late_reclaim_frame(signal_ts, *, freq="min", volume=1100.0, signal_type="BUY"):
    index = pd.date_range(signal_ts - timedelta(minutes=9), periods=10, freq=freq)
    if str(signal_type or "").upper() == "SELL":
        return pd.DataFrame(
            {
                "open": [100.0, 100.2, 100.5, 100.8, 101.0, 101.2, 101.4, 101.5, 101.3, 100.7],
                "high": [100.4, 100.6, 100.9, 101.1, 101.3, 101.5, 101.7, 101.8, 101.6, 100.9],
                "low": [99.8, 100.0, 100.3, 100.6, 100.8, 101.0, 101.2, 101.1, 100.6, 98.8],
                "close": [100.2, 100.5, 100.8, 101.0, 101.2, 101.4, 101.5, 101.3, 100.8, 99.0],
                "volume": [1000.0] * 9 + [volume],
            },
            index=index,
        )
    return pd.DataFrame(
        {
            "open": [100.0, 99.8, 99.5, 99.2, 99.0, 98.8, 98.6, 98.5, 98.7, 99.3],
            "high": [100.2, 100.0, 99.7, 99.4, 99.2, 99.0, 98.8, 98.9, 99.4, 101.2],
            "low": [99.6, 99.4, 99.1, 98.9, 98.7, 98.5, 98.3, 98.2, 98.4, 99.1],
            "close": [99.8, 99.5, 99.2, 99.0, 98.8, 98.6, 98.5, 98.7, 99.2, 101.0],
            "volume": [1000.0] * 9 + [volume],
        },
        index=index,
    )


def _late_reclaim_scanner(frames, signal_ts, *, signal_type="BUY"):
    scanner = Scanner.__new__(Scanner)
    scanner.active_indices = ["NIFTY", "BANKNIFTY", "SENSEX", "MIDCPNIFTY"]
    scanner.candles = SimpleNamespace(
        get_candles=lambda instrument, tf: frames.get((instrument, tf), frames[("NIFTY", tf)])
    )
    scanner._setting_enabled = lambda value, default=True: bool(default if value is None else value)
    scanner._cached_results = {}
    for name in scanner.active_indices:
        scanner._cached_results[f"{name}_1min"] = {
            "signals": [SimpleNamespace(timestamp=signal_ts, signal_type=signal_type)]
        }
        scanner._cached_results[f"{name}_5min"] = {
            "signals": [SimpleNamespace(timestamp=signal_ts, signal_type=signal_type)]
        }
    return scanner


def test_late_session_reclaim_context_catches_basic_volume_basket(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "ut_regime_adaptation", False)
    monkeypatch.setattr(settings, "late_session_reclaim_override_enabled", True)
    monkeypatch.setattr(settings, "late_session_reclaim_start", "14:25")
    monkeypatch.setattr(settings, "late_session_reclaim_end", "15:10")
    monkeypatch.setattr(settings, "late_session_reclaim_core_votes", 2)
    monkeypatch.setattr(settings, "late_session_reclaim_total_votes", 3)
    monkeypatch.setattr(settings, "late_session_reclaim_reclaim_votes", 2)
    monkeypatch.setattr(settings, "late_session_reclaim_min_volume_ratio", 1.10)
    monkeypatch.setattr(settings, "late_session_reclaim_volume_votes", 3)

    signal_ts = datetime(2026, 7, 6, 14, 50)
    frames = {}
    for name in ["NIFTY", "BANKNIFTY", "SENSEX", "MIDCPNIFTY"]:
        frames[(name, "1min")] = _late_reclaim_frame(signal_ts, freq="min", volume=1120.0)
        frames[(name, "5min")] = _late_reclaim_frame(signal_ts, freq="5min", volume=1120.0)
    scanner = _late_reclaim_scanner(frames, signal_ts)

    context = scanner._late_session_reclaim_context("NIFTY", "5min", "BUY", signal_ts)

    assert context is not None
    assert context["type"] == "late_session_reclaim"
    assert context["core_votes"] == 4
    assert context["core_universe"] == ["BANKNIFTY", "MIDCPNIFTY", "NIFTY", "SENSEX"]
    assert context["total_votes"] == 4
    assert context["volume_votes"] == 4
    assert context["allow_regime_veto"] is True


def test_late_session_reclaim_context_catches_sell_rollover_basket(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "late_session_reclaim_override_enabled", True)
    monkeypatch.setattr(settings, "late_session_reclaim_start", "14:25")
    monkeypatch.setattr(settings, "late_session_reclaim_end", "15:10")
    monkeypatch.setattr(settings, "late_session_reclaim_core_votes", 3)
    monkeypatch.setattr(settings, "late_session_reclaim_total_votes", 3)
    monkeypatch.setattr(settings, "late_session_reclaim_reclaim_votes", 2)
    monkeypatch.setattr(settings, "late_session_reclaim_min_volume_ratio", 1.10)
    monkeypatch.setattr(settings, "late_session_reclaim_volume_votes", 3)

    signal_ts = datetime(2026, 7, 6, 14, 50)
    frames = {}
    for name in ["NIFTY", "BANKNIFTY", "SENSEX", "MIDCPNIFTY"]:
        frames[(name, "1min")] = _late_reclaim_frame(
            signal_ts, freq="min", volume=1120.0, signal_type="SELL"
        )
        frames[(name, "5min")] = _late_reclaim_frame(
            signal_ts, freq="5min", volume=1120.0, signal_type="SELL"
        )
    scanner = _late_reclaim_scanner(frames, signal_ts, signal_type="SELL")

    context = scanner._late_session_reclaim_context("NIFTY", "5min", "SELL", signal_ts)

    assert context is not None
    assert context["type"] == "late_session_reclaim"
    assert context["signal_type"] == "SELL"
    assert context["move_label"] == "rollover"
    assert context["core_votes"] == 4
    assert context["core_universe"] == ["BANKNIFTY", "MIDCPNIFTY", "NIFTY", "SENSEX"]
    assert context["volume_votes"] == 4


def test_late_session_reclaim_context_allows_one_core_miss_with_midcap_basket(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "late_session_reclaim_override_enabled", True)
    monkeypatch.setattr(settings, "late_session_reclaim_start", "14:25")
    monkeypatch.setattr(settings, "late_session_reclaim_end", "15:10")
    monkeypatch.setattr(settings, "late_session_reclaim_core_votes", 3)
    monkeypatch.setattr(settings, "late_session_reclaim_total_votes", 3)
    monkeypatch.setattr(settings, "late_session_reclaim_reclaim_votes", 2)
    monkeypatch.setattr(settings, "late_session_reclaim_min_volume_ratio", 1.10)
    monkeypatch.setattr(settings, "late_session_reclaim_volume_votes", 3)

    signal_ts = datetime(2026, 7, 6, 14, 50)
    frames = {}
    for name in ["NIFTY", "BANKNIFTY", "SENSEX", "MIDCPNIFTY"]:
        volume = 1000.0 if name == "SENSEX" else 1120.0
        frames[(name, "1min")] = _late_reclaim_frame(signal_ts, freq="min", volume=volume)
        frames[(name, "5min")] = _late_reclaim_frame(signal_ts, freq="5min", volume=volume)
    scanner = _late_reclaim_scanner(frames, signal_ts)

    context = scanner._late_session_reclaim_context("NIFTY", "5min", "BUY", signal_ts)

    assert context is not None
    assert context["core_votes"] == 3
    assert context["core_indices"] == ["BANKNIFTY", "MIDCPNIFTY", "NIFTY"]
    assert context["core_universe"] == ["BANKNIFTY", "MIDCPNIFTY", "NIFTY", "SENSEX"]
    assert "basket core 3/4" in context["reason"]


def test_late_session_reclaim_context_still_rejects_two_of_four_basket(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "late_session_reclaim_override_enabled", True)
    monkeypatch.setattr(settings, "late_session_reclaim_start", "14:25")
    monkeypatch.setattr(settings, "late_session_reclaim_end", "15:10")
    monkeypatch.setattr(settings, "late_session_reclaim_core_votes", 3)
    monkeypatch.setattr(settings, "late_session_reclaim_total_votes", 3)
    monkeypatch.setattr(settings, "late_session_reclaim_reclaim_votes", 2)
    monkeypatch.setattr(settings, "late_session_reclaim_min_volume_ratio", 1.10)
    monkeypatch.setattr(settings, "late_session_reclaim_volume_votes", 3)

    signal_ts = datetime(2026, 7, 6, 14, 50)
    frames = {}
    for name in ["NIFTY", "BANKNIFTY", "SENSEX", "MIDCPNIFTY"]:
        volume = 1120.0 if name in {"NIFTY", "MIDCPNIFTY"} else 1000.0
        frames[(name, "1min")] = _late_reclaim_frame(signal_ts, freq="min", volume=volume)
        frames[(name, "5min")] = _late_reclaim_frame(signal_ts, freq="5min", volume=volume)
    scanner = _late_reclaim_scanner(frames, signal_ts)

    assert scanner._late_session_reclaim_context("NIFTY", "5min", "BUY", signal_ts) is None


def test_late_session_reclaim_context_is_engine_mode_agnostic(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "late_session_reclaim_override_enabled", True)
    monkeypatch.setattr(settings, "late_session_reclaim_start", "14:25")
    monkeypatch.setattr(settings, "late_session_reclaim_end", "15:10")
    monkeypatch.setattr(settings, "late_session_reclaim_core_votes", 2)
    monkeypatch.setattr(settings, "late_session_reclaim_total_votes", 3)
    monkeypatch.setattr(settings, "late_session_reclaim_reclaim_votes", 2)
    monkeypatch.setattr(settings, "late_session_reclaim_min_volume_ratio", 1.10)
    monkeypatch.setattr(settings, "late_session_reclaim_volume_votes", 3)

    supported_modes = [
        "utbot",
        "ema",
        "consensus",
        "utbot_consensus",
        "quad_union",
        "quad_prime",
        "adaptive_dual",
    ]

    signal_ts = datetime(2026, 7, 6, 14, 50)
    for direction in ["BUY", "SELL"]:
        frames = {}
        for name in ["NIFTY", "BANKNIFTY", "SENSEX", "MIDCPNIFTY"]:
            frames[(name, "1min")] = _late_reclaim_frame(
                signal_ts, freq="min", volume=1120.0, signal_type=direction
            )
            frames[(name, "5min")] = _late_reclaim_frame(
                signal_ts, freq="5min", volume=1120.0, signal_type=direction
            )
        scanner = _late_reclaim_scanner(frames, signal_ts, signal_type=direction)

        for mode in supported_modes:
            monkeypatch.setattr(settings, "scanner_engine", mode)
            context = scanner._entry_reversal_relief_context(
                "NIFTY",
                "5min",
                direction,
                signal_ts,
                regime="RANGING",
                confidence=0.66,
            )

            assert context is not None, f"{mode} {direction}"
            assert context["type"] == "late_session_reclaim"
            assert context["signal_type"] == direction
            assert context["volume_votes"] == 4


def test_late_session_reclaim_context_rejects_quiet_basket(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "late_session_reclaim_override_enabled", True)
    monkeypatch.setattr(settings, "late_session_reclaim_min_volume_ratio", 1.10)
    monkeypatch.setattr(settings, "late_session_reclaim_volume_votes", 3)

    signal_ts = datetime(2026, 7, 6, 14, 50)
    frames = {}
    for name in ["NIFTY", "BANKNIFTY", "SENSEX", "MIDCPNIFTY"]:
        frames[(name, "1min")] = _late_reclaim_frame(signal_ts, freq="min", volume=1000.0)
        frames[(name, "5min")] = _late_reclaim_frame(signal_ts, freq="5min", volume=1000.0)
    scanner = _late_reclaim_scanner(frames, signal_ts)

    assert scanner._late_session_reclaim_context("NIFTY", "5min", "BUY", signal_ts) is None


def test_late_session_reclaim_context_respects_time_box(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "late_session_reclaim_override_enabled", True)
    monkeypatch.setattr(settings, "late_session_reclaim_start", "14:25")
    monkeypatch.setattr(settings, "late_session_reclaim_end", "15:10")

    signal_ts = datetime(2026, 7, 6, 14, 24)
    frames = {}
    for name in ["NIFTY", "BANKNIFTY", "SENSEX", "MIDCPNIFTY"]:
        frames[(name, "1min")] = _late_reclaim_frame(signal_ts, freq="min", volume=1100.0)
        frames[(name, "5min")] = _late_reclaim_frame(signal_ts, freq="5min", volume=1100.0)
    scanner = _late_reclaim_scanner(frames, signal_ts)

    assert scanner._late_session_reclaim_context("NIFTY", "5min", "BUY", signal_ts) is None


def test_late_session_reclaim_location_block_becomes_tight_scalp(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "ut_market_location_gate", True)

    index = pd.date_range("2026-07-06 14:39", periods=12, freq="min")
    frame = pd.DataFrame(
        {
            "open": [105.0, 104.8, 104.4, 104.0, 103.7, 103.3, 103.0, 102.7, 102.4, 102.1, 101.8, 101.6],
            "high": [105.2, 105.0, 104.6, 104.2, 103.9, 103.5, 103.2, 102.9, 102.6, 102.3, 102.0, 101.8],
            "low": [104.6, 104.2, 103.9, 103.5, 103.2, 102.9, 102.6, 102.3, 102.0, 101.7, 101.4, 101.2],
            "close": [104.7, 104.3, 104.0, 103.6, 103.3, 103.0, 102.7, 102.4, 102.1, 101.8, 101.5, 101.3],
        },
        index=index,
    )
    scanner = Scanner.__new__(Scanner)
    scanner.mode = "REAL"
    scanner.candles = SimpleNamespace(get_candles=lambda *_args: frame)
    scanner.latest_regimes = {"NIFTY": "RANGING"}
    scanner._setting_enabled = lambda value, default=True: bool(default if value is None else value)
    scanner._candidate_15m_trend_agrees = lambda candidate: False
    scanner._set_scalp_lock = Scanner._set_scalp_lock.__get__(scanner, Scanner)
    candidate = _location_candidate(
        instrument="NIFTY",
        direction="LONG",
        option_type="CE",
        signal_timestamp=index[-1].to_pydatetime(),
        late_session_reclaim_entry=True,
    )

    allowed, reason = scanner._market_location_entry_gate(candidate)

    assert allowed is True
    assert "late-session synchronized directional move confirmed" in reason
    assert candidate.location_decision == "REDUCED_RISK"
    assert candidate.scalp_lock_mode is True


def test_late_session_reclaim_uses_tight_option_stop(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "late_session_reclaim_options_sl_pct", 4.0)

    scanner = Scanner.__new__(Scanner)
    scanner.expiry = SimpleNamespace(in_gamma_window=lambda *_args: False)
    scanner._session_squareoff_clock = lambda: datetime(2026, 7, 6, 14, 55)
    candidate = _location_candidate(
        instrument="NIFTY",
        direction="LONG",
        option_type="CE",
        late_session_reclaim_entry=True,
        tight_stop_pct=4.0,
    )

    assert scanner._option_premium_stop_distance_for_candidate(100.0, candidate) == 4.0


def test_one_way_cascade_context_catches_all_index_downside_thrust(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "one_way_cascade_enabled", True)
    monkeypatch.setattr(settings, "one_way_cascade_core_votes", 3)
    monkeypatch.setattr(settings, "one_way_cascade_total_votes", 3)
    monkeypatch.setattr(settings, "one_way_cascade_volume_votes", 3)
    monkeypatch.setattr(settings, "one_way_cascade_min_volume_ratio", 1.40)
    monkeypatch.setattr(settings, "one_way_cascade_min_range_atr_ratio_1min", 1.50)
    monkeypatch.setattr(settings, "one_way_cascade_min_range_atr_ratio_5min", 1.20)
    monkeypatch.setattr(settings, "one_way_cascade_min_range_atr_ratio_15min", 0.75)
    monkeypatch.setattr(settings, "one_way_cascade_min_close_location", 0.68)

    signal_ts = datetime(2026, 7, 8, 13, 45)
    frames = {}
    for name in ["NIFTY", "BANKNIFTY", "SENSEX", "MIDCPNIFTY"]:
        frames[(name, "1min")] = _one_way_cascade_frame(signal_ts, freq="1min", signal_type="SELL")
        frames[(name, "5min")] = _one_way_cascade_frame(signal_ts, freq="5min", signal_type="SELL")
        frames[(name, "15min")] = _one_way_cascade_frame(signal_ts, freq="15min", signal_type="SELL")
    scanner = _one_way_cascade_scanner(frames)

    context = scanner._one_way_cascade_context("NIFTY", "5min", "SELL", signal_ts)

    assert context is not None
    assert context["type"] == "one_way_cascade"
    assert context["core_votes"] == 3
    assert context["total_votes"] == 4
    assert context["volume_votes"] == 4
    assert context["force_scalp_lock"] is False
    assert context["suppress_profit_lock"] is True


def test_one_way_cascade_midcap_participates_when_own_data_supports(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "one_way_cascade_enabled", True)
    monkeypatch.setattr(settings, "one_way_cascade_core_votes", 3)
    monkeypatch.setattr(settings, "one_way_cascade_total_votes", 3)
    monkeypatch.setattr(settings, "one_way_cascade_volume_votes", 3)
    monkeypatch.setattr(settings, "one_way_cascade_min_volume_ratio", 1.40)
    monkeypatch.setattr(settings, "one_way_cascade_min_range_atr_ratio_5min", 1.20)
    monkeypatch.setattr(settings, "one_way_cascade_min_close_location", 0.68)

    signal_ts = datetime(2026, 7, 8, 13, 45)
    frames = {}
    for name in ["NIFTY", "BANKNIFTY", "SENSEX", "MIDCPNIFTY"]:
        frames[(name, "1min")] = _one_way_cascade_frame(signal_ts, freq="1min", signal_type="SELL")
        frames[(name, "5min")] = _one_way_cascade_frame(signal_ts, freq="5min", signal_type="SELL")
        frames[(name, "15min")] = _one_way_cascade_frame(signal_ts, freq="15min", signal_type="SELL")
    scanner = _one_way_cascade_scanner(frames)

    context = scanner._one_way_cascade_context("MIDCPNIFTY", "5min", "SELL", signal_ts)

    assert context is not None
    assert context["core_votes"] == 3
    assert "MIDCPNIFTY" in context["indices"]
    assert context["votes"]["MIDCPNIFTY"]["best_timeframe"] in {"1min", "5min", "15min"}


class _ReentryData:
    def is_market_open(self, *_args, **_kwargs):
        return True

    def get_option_token(self, instrument, strike, option_type, *args, **kwargs):
        return {"symbol": f"{instrument}{int(strike)}{option_type}", "token": f"T{instrument}{int(strike)}{option_type}"}

    def get_ltp(self, *_args, **_kwargs):
        return 120.0


def _missed_reentry_scanner(frames, now):
    scanner = _one_way_cascade_scanner(frames)
    scanner.mode = "REAL"
    scanner.data = _ReentryData()
    scanner.config = {
        "indices": {
            "NIFTY": {"strike_interval": 50, "lot_size": 50, "option_exchange": "NFO", "exchange": "NFO"},
            "BANKNIFTY": {"strike_interval": 100, "lot_size": 30, "option_exchange": "NFO", "exchange": "NFO"},
            "SENSEX": {"strike_interval": 100, "lot_size": 20, "option_exchange": "BFO", "exchange": "BFO"},
            "MIDCPNIFTY": {"strike_interval": 25, "lot_size": 75, "option_exchange": "NFO", "exchange": "NFO"},
        }
    }
    scanner.user_lots = {"NIFTY": 1, "BANKNIFTY": 1, "SENSEX": 1, "MIDCPNIFTY": 1}
    scanner.user_lots_fut = {"NIFTY": 1, "BANKNIFTY": 1, "SENSEX": 1, "MIDCPNIFTY": 1}
    scanner.trades = SimpleNamespace(open_trades={})
    scanner.latest_regimes = {"NIFTY": "TRENDING_DOWN"}
    scanner._missed_reentry_candidates = {}
    scanner._session_squareoff_clock = lambda: now
    scanner._configured_session_time = lambda _name, default: datetime.strptime(default, "%H:%M").time()
    scanner._active_session_entry_candidates = lambda *args, **kwargs: []
    scanner.log_signal_decision_once = lambda *args, **kwargs: None
    scanner.is_warmup = False
    return scanner


def _missed_reentry_seed(signal_ts, **overrides):
    seed = _location_candidate(
        instrument="NIFTY",
        direction="SHORT",
        price=92.0,
        stop=98.0,
        target=85.0,
        lots=0,
        lot_size=0,
        grade="A",
        confidence=0.85,
        timeframe="5min",
        inst_type="FUT",
        option_type="",
        atm_strike=0.0,
        multiplier=1.0,
        trading_symbol="",
        symbol_token="",
        action="NO_ENTRY",
        status="REJECTED - Price Action",
        signal_timestamp=signal_ts,
        adx_value=32.0,
    )
    setattr(seed, "source_signal_type", "SELL")
    setattr(seed, "source_signal_price", 92.0)
    setattr(seed, "market_regime", "TRENDING_DOWN")
    setattr(seed, "intelligence_score", -0.45)
    for key, value in overrides.items():
        setattr(seed, key, value)
    return seed


def test_missed_signal_reentry_rebuilds_nearby_soft_reject(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "missed_signal_reentry_enabled", True)
    monkeypatch.setattr(settings, "missed_signal_reentry_max_age_minutes", 6.0)
    monkeypatch.setattr(settings, "missed_signal_reentry_min_age_seconds", 20.0)
    monkeypatch.setattr(settings, "missed_signal_reentry_max_chase_atr", 0.75)
    monkeypatch.setattr(settings, "missed_signal_reentry_min_confidence", 0.55)
    monkeypatch.setattr(settings, "missed_signal_reentry_prefer_options", True)
    monkeypatch.setattr(settings, "missed_signal_reentry_options_sl_pct", 5.0)
    monkeypatch.setattr(settings, "missed_signal_reentry_rr", 1.5)
    monkeypatch.setattr(settings, "one_way_cascade_enabled", True)
    monkeypatch.setattr(settings, "one_way_cascade_core_votes", 3)
    monkeypatch.setattr(settings, "one_way_cascade_total_votes", 3)
    monkeypatch.setattr(settings, "one_way_cascade_volume_votes", 3)
    monkeypatch.setattr(settings, "one_way_cascade_min_volume_ratio", 1.40)
    monkeypatch.setattr(settings, "one_way_cascade_min_range_atr_ratio_5min", 1.20)
    monkeypatch.setattr(settings, "one_way_cascade_min_close_location", 0.68)

    signal_ts = datetime(2026, 7, 8, 13, 45)
    now = datetime(2026, 7, 8, 13, 47)
    frames = {}
    for name in ["NIFTY", "BANKNIFTY", "SENSEX", "MIDCPNIFTY"]:
        frames[(name, "1min")] = _one_way_cascade_frame(signal_ts, freq="1min", signal_type="SELL")
        frames[(name, "5min")] = _one_way_cascade_frame(signal_ts, freq="5min", signal_type="SELL")
        frames[(name, "15min")] = _one_way_cascade_frame(signal_ts, freq="15min", signal_type="SELL")
    scanner = _missed_reentry_scanner(frames, now)
    scanner._remember_missed_reentry_seed(
        _missed_reentry_seed(signal_ts),
        reason="Price-action adverse soft block",
        source="filtered_candidate",
    )

    candidates = scanner._collect_missed_signal_reentries([])

    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate.instrument == "NIFTY"
    assert candidate.direction == "SHORT"
    assert candidate.inst_type == "OPT"
    assert candidate.option_type == "PE"
    assert candidate.price == 120.0
    # This fixture uses an intentionally tiny synthetic index level (88). The final
    # entry policy's 0.15% underlying stop cap therefore tightens the old 5%-premium
    # geometry; assert the invariant instead of the retired uncapped 114/129 tuple.
    underlying_risk_pct = (
        abs(candidate.underlying_stop - candidate.underlying_entry)
        / candidate.underlying_entry
        * 100.0
    )
    actual_rr = (candidate.target - candidate.price) / (candidate.price - candidate.stop)
    assert underlying_risk_pct <= 0.15 + 1e-9
    assert actual_rr >= 1.5 - 1e-9
    assert candidate.premium_stop == candidate.stop
    assert candidate.premium_target == candidate.target
    assert getattr(candidate, "missed_signal_reentry") is True
    assert getattr(candidate, "one_way_cascade_entry") is True
    assert getattr(candidate, "source_signal_timestamp") == signal_ts


def test_missed_signal_reentry_rejects_chase_after_move_extended(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "missed_signal_reentry_enabled", True)
    monkeypatch.setattr(settings, "missed_signal_reentry_max_age_minutes", 6.0)
    monkeypatch.setattr(settings, "missed_signal_reentry_min_age_seconds", 20.0)
    monkeypatch.setattr(settings, "missed_signal_reentry_max_chase_atr", 0.10)
    # Cascade seeds now get an extended chase cap; pin it too so this test still
    # exercises "extended beyond chase limit" under a confirmed cascade context.
    monkeypatch.setattr(settings, "missed_signal_reentry_cascade_max_chase_atr", 0.10)
    monkeypatch.setattr(settings, "missed_signal_reentry_min_confidence", 0.55)
    monkeypatch.setattr(settings, "one_way_cascade_enabled", True)
    monkeypatch.setattr(settings, "one_way_cascade_core_votes", 3)
    monkeypatch.setattr(settings, "one_way_cascade_total_votes", 3)
    monkeypatch.setattr(settings, "one_way_cascade_volume_votes", 3)
    monkeypatch.setattr(settings, "one_way_cascade_min_volume_ratio", 1.40)
    monkeypatch.setattr(settings, "one_way_cascade_min_range_atr_ratio_5min", 1.20)
    monkeypatch.setattr(settings, "one_way_cascade_min_close_location", 0.68)

    signal_ts = datetime(2026, 7, 8, 13, 45)
    now = datetime(2026, 7, 8, 13, 47)
    frames = {}
    for name in ["NIFTY", "BANKNIFTY", "SENSEX", "MIDCPNIFTY"]:
        frames[(name, "1min")] = _one_way_cascade_frame(signal_ts, freq="1min", signal_type="SELL")
        frames[(name, "5min")] = _one_way_cascade_frame(signal_ts, freq="5min", signal_type="SELL")
        frames[(name, "15min")] = _one_way_cascade_frame(signal_ts, freq="15min", signal_type="SELL")
    scanner = _missed_reentry_scanner(frames, now)
    scanner._remember_missed_reentry_seed(
        _missed_reentry_seed(signal_ts, source_signal_price=105.0),
        reason="Price-action adverse soft block",
        source="filtered_candidate",
    )

    assert scanner._collect_missed_signal_reentries([]) == []


def test_missed_signal_reentry_blocks_when_same_index_trade_active(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "missed_signal_reentry_enabled", True)
    signal_ts = datetime(2026, 7, 8, 13, 45)
    now = datetime(2026, 7, 8, 13, 47)
    frames = {}
    for name in ["NIFTY", "BANKNIFTY", "SENSEX", "MIDCPNIFTY"]:
        frames[(name, "1min")] = _one_way_cascade_frame(signal_ts, freq="1min", signal_type="SELL")
        frames[(name, "5min")] = _one_way_cascade_frame(signal_ts, freq="5min", signal_type="SELL")
        frames[(name, "15min")] = _one_way_cascade_frame(signal_ts, freq="15min", signal_type="SELL")
    scanner = _missed_reentry_scanner(frames, now)
    scanner.trades = SimpleNamespace(open_trades={"T1": SimpleNamespace(instrument="NIFTY", status="OPEN")})
    scanner._remember_missed_reentry_seed(
        _missed_reentry_seed(signal_ts),
        reason="Price-action adverse soft block",
        source="filtered_candidate",
    )

    assert scanner._collect_missed_signal_reentries([]) == []


def test_one_way_cascade_location_block_becomes_runner_not_scalp(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "ut_market_location_gate", True)

    index = pd.date_range("2026-07-08 13:34", periods=12, freq="min")
    frame = pd.DataFrame(
        {
            "open": [100 + i for i in range(12)],
            "high": [101 + i for i in range(12)],
            "low": [99 + i for i in range(12)],
            "close": [100.5 + i for i in range(12)],
        },
        index=index,
    )
    scanner = _location_scanner(frame, regime="RANGING")
    scanner._set_scalp_lock = Scanner._set_scalp_lock.__get__(scanner, Scanner)
    candidate = _location_candidate(
        instrument="BANKNIFTY",
        direction="SHORT",
        option_type="PE",
        signal_timestamp=index[-1].to_pydatetime(),
        one_way_cascade_entry=True,
        one_way_cascade_context={"move_label": "one-way downside cascade"},
    )

    allowed, reason = scanner._market_location_entry_gate(candidate)

    assert allowed is True
    assert "one-way downside cascade" in reason
    assert candidate.location_decision == "REDUCED_RISK"
    assert candidate.scalp_lock_mode is False


def test_market_wide_reversal_context_allows_two_of_three_core_sync(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "market_wide_reversal_enabled", True)
    monkeypatch.setattr(settings, "market_wide_reversal_core_votes", 2)
    monkeypatch.setattr(settings, "market_wide_reversal_total_votes", 2)
    monkeypatch.setattr(settings, "market_wide_reversal_min_range_atr_ratio_5min", 1.10)
    monkeypatch.setattr(settings, "market_wide_reversal_min_close_location", 0.65)

    signal_ts = datetime(2026, 6, 29, 10, 45)
    frames = {
        "NIFTY": _market_wide_frame(signal_ts),
        "BANKNIFTY": _market_wide_frame(signal_ts),
        "SENSEX": _market_wide_weak_frame(signal_ts),
    }
    scanner = _market_wide_scanner(frames, signal_ts, ["NIFTY", "BANKNIFTY"])

    context = scanner._market_wide_reversal_context("NIFTY", "5min", "SELL", signal_ts)

    assert context is not None
    assert context["core_votes"] == 2
    assert context["total_votes"] == 2
    assert context["core_indices"] == ["BANKNIFTY", "NIFTY"]


def test_market_wide_reversal_context_requires_candidate_vote(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "market_wide_reversal_enabled", True)
    monkeypatch.setattr(settings, "market_wide_reversal_core_votes", 2)
    monkeypatch.setattr(settings, "market_wide_reversal_total_votes", 2)
    monkeypatch.setattr(settings, "market_wide_reversal_min_range_atr_ratio_5min", 1.10)
    monkeypatch.setattr(settings, "market_wide_reversal_min_close_location", 0.65)

    signal_ts = datetime(2026, 6, 29, 10, 45)
    frames = {
        "NIFTY": _market_wide_frame(signal_ts),
        "BANKNIFTY": _market_wide_frame(signal_ts),
        "SENSEX": _market_wide_weak_frame(signal_ts),
    }
    scanner = _market_wide_scanner(frames, signal_ts, ["NIFTY", "BANKNIFTY", "SENSEX"])

    context = scanner._market_wide_reversal_context("SENSEX", "5min", "SELL", signal_ts)

    assert context is None


def test_market_wide_reversal_context_allows_midcap_with_two_core_votes(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "market_wide_reversal_enabled", True)
    monkeypatch.setattr(settings, "market_wide_reversal_core_votes", 2)
    monkeypatch.setattr(settings, "market_wide_reversal_total_votes", 2)
    monkeypatch.setattr(settings, "market_wide_reversal_min_range_atr_ratio_5min", 1.10)
    monkeypatch.setattr(settings, "market_wide_reversal_midcap_min_range_atr_ratio_5min", 0.65)
    monkeypatch.setattr(settings, "market_wide_reversal_min_close_location", 0.65)

    signal_ts = datetime(2026, 6, 29, 10, 45)
    frames = {
        "NIFTY": _market_wide_frame(signal_ts),
        "BANKNIFTY": _market_wide_frame(signal_ts),
        "SENSEX": _market_wide_weak_frame(signal_ts),
        "MIDCPNIFTY": _market_wide_midcap_sympathy_frame(signal_ts),
    }
    scanner = _market_wide_scanner(frames, signal_ts, ["NIFTY", "BANKNIFTY", "MIDCPNIFTY"])

    context = scanner._market_wide_reversal_context("MIDCPNIFTY", "5min", "SELL", signal_ts)

    assert context is not None
    assert context["core_votes"] == 2
    assert context["total_votes"] == 3
    assert context["core_indices"] == ["BANKNIFTY", "NIFTY"]
    assert context["votes"]["MIDCPNIFTY"]["midcap_core_sympathy"] is True


def test_counter_session_bias_yields_to_confirmed_reversal_context(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "directional_census_enabled", True)
    monkeypatch.setattr(settings, "directional_census_reversal_relief_enabled", True)
    monkeypatch.setattr(settings, "directional_census_reversal_min_confidence", 0.20)
    monkeypatch.setattr(settings, "directional_census_min_signals", 12)
    monkeypatch.setattr(settings, "directional_census_ratio", 1.5)
    monkeypatch.setattr(settings, "directional_census_penalty", 0.05)

    blocked = directional_census_block(
        settings,
        {"LONG": 44, "SHORT": 28},
        signal_direction="SHORT",
        confidence=0.35,
        grade_floor=0.55,
    )
    allowed = directional_census_block(
        settings,
        {"LONG": 44, "SHORT": 28},
        signal_direction="SHORT",
        confidence=0.35,
        grade_floor=0.55,
        reversal_context={"type": "cross_index_rollover", "reason": "peers rolled over"},
    )

    assert "Counter-session-bias" in blocked
    assert allowed == ""


def test_entry_reversal_relief_uses_market_wide_rollover(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "reversal_entry_relief_enabled", True)
    monkeypatch.setattr(settings, "reversal_entry_min_confidence", 0.20)
    monkeypatch.setattr(settings, "market_wide_reversal_enabled", True)
    monkeypatch.setattr(settings, "market_wide_reversal_core_votes", 2)
    monkeypatch.setattr(settings, "market_wide_reversal_total_votes", 2)
    monkeypatch.setattr(settings, "market_wide_reversal_min_range_atr_ratio_5min", 1.10)
    monkeypatch.setattr(settings, "market_wide_reversal_min_close_location", 0.65)

    signal_ts = datetime(2026, 6, 29, 10, 45)
    frames = {
        "NIFTY": _market_wide_frame(signal_ts),
        "BANKNIFTY": _market_wide_frame(signal_ts),
        "SENSEX": _market_wide_weak_frame(signal_ts),
    }
    scanner = _market_wide_scanner(frames, signal_ts, ["NIFTY", "BANKNIFTY"])

    context = scanner._entry_reversal_relief_context(
        "NIFTY",
        "5min",
        "SELL",
        signal_ts,
        regime="RANGING",
        confidence=0.35,
    )

    assert context is not None
    assert context["type"] == "cross_index_rollover"


def test_market_wide_reversal_live_gate_bypasses_single_index_location_block(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "ut_timeframe_entry_policy", "INCLUDE_5MIN")

    scanner = Scanner.__new__(Scanner)
    scanner.mode = "REAL"
    scanner.latest_regimes = {"NIFTY": "TRENDING_UP"}
    scanner._passes_timeframe_entry_policy = lambda candidate: True
    scanner._setting_enabled = lambda value, default=True: bool(default if value is None else value)
    candidate = _location_candidate(
        instrument="NIFTY",
        grade="C",
        confidence=0.25,
        trend_15m_agrees=False,
        market_wide_reversal_entry=True,
    )

    assert scanner._passes_candidate_grade_preference(candidate) == (
        True,
        "market-wide reversal override",
    )
    assert scanner._passes_live_trade_ready_gate(candidate) is True
    assert candidate.scalp_lock_mode is True


def test_location_gate_blocks_unconfirmed_countertrend_extreme(monkeypatch):
    index = pd.date_range("2026-06-15 09:45", periods=16, freq="min")
    frame = pd.DataFrame(
        {
            "open": [100 + i for i in range(16)],
            "high": [101 + i for i in range(16)],
            "low": [99 + i for i in range(16)],
            "close": [100.5 + i for i in range(16)],
        },
        index=index,
    )
    candidate = _location_candidate()
    scanner = _location_scanner(frame)

    allowed, reason = scanner._market_location_entry_gate(candidate)

    assert not allowed
    assert reason
    assert candidate.location_decision == "BLOCK"


def test_location_gate_allows_only_confirmed_aligned_impulse_as_reduced_risk():
    index = pd.date_range("2026-06-15 09:45", periods=16, freq="min")
    frame = pd.DataFrame(
        {
            "open": [100 + i for i in range(16)],
            "high": [101 + i for i in range(16)],
            "low": [99 + i for i in range(16)],
            "close": [100.5 + i for i in range(16)],
        },
        index=index,
    )
    candidate = _location_candidate(
        impulse_confirmed=True,
        trend_15m_agrees=True,
    )
    scanner = _location_scanner(frame, regime="TRENDING_UP")

    allowed, reason = scanner._market_location_entry_gate(candidate)

    assert allowed
    assert reason
    assert candidate.location_decision == "REDUCED_RISK"


def test_choppy_option_location_risk_blocks_without_strict_exception():
    index = pd.date_range("2026-06-15 09:45", periods=16, freq="min")
    frame = pd.DataFrame(
        {
            "open": [100 + i for i in range(16)],
            "high": [101 + i for i in range(16)],
            "low": [99 + i for i in range(16)],
            "close": [100.5 + i for i in range(16)],
        },
        index=index,
    )
    scanner = _location_scanner(frame, regime="RANGING")
    candidate = _location_candidate(
        impulse_confirmed=True,
        trend_15m_agrees=True,
        is_explosive_bypass=False,
        intelligence_score=0.20,
    )

    allowed, reason = scanner._market_location_entry_gate(candidate)

    assert not allowed
    assert reason
    assert candidate.location_decision == "BLOCK"


def test_choppy_option_location_risk_allows_strict_intel_exception():
    index = pd.date_range("2026-06-15 09:45", periods=16, freq="min")
    frame = pd.DataFrame(
        {
            "open": [100 + i for i in range(16)],
            "high": [101 + i for i in range(16)],
            "low": [99 + i for i in range(16)],
            "close": [100.5 + i for i in range(16)],
        },
        index=index,
    )
    scanner = _location_scanner(frame, regime="RANGING")
    candidate = _location_candidate(
        impulse_confirmed=True,
        trend_15m_agrees=True,
        is_explosive_bypass=True,
        intelligence_score=0.75,
    )

    allowed, reason = scanner._market_location_entry_gate(candidate)

    assert allowed
    assert reason
    assert candidate.location_decision == "REDUCED_RISK"


def test_choppy_5min_option_live_gate_requires_trend_and_impulse():
    scanner = Scanner.__new__(Scanner)
    scanner.mode = "REAL"
    scanner.latest_regimes = {"BANKNIFTY": "RANGING"}
    scanner._passes_timeframe_entry_policy = lambda candidate: True
    scanner._market_location_entry_gate = lambda candidate: (True, "")
    scanner._candidate_15m_trend_agrees = lambda candidate: bool(getattr(candidate, "trend_15m_agrees", False))
    scanner._setting_enabled = lambda value, default=True: True
    candidate = _location_candidate(
        grade="B+",
        confidence=0.70,
        timeframe="5min",
        inst_type="OPT",
        impulse_confirmed=False,
        trend_15m_agrees=False,
    )

    assert scanner._passes_live_trade_ready_gate(candidate) is False

    candidate.impulse_confirmed = True
    candidate.is_explosive_bypass = True
    assert scanner._passes_live_trade_ready_gate(candidate) is False

    candidate.trend_15m_agrees = True

    assert scanner._passes_live_trade_ready_gate(candidate) is True


def test_choppy_5min_option_live_gate_allows_special_bplus_a_aplus_sixty_exception():
    scanner = Scanner.__new__(Scanner)
    scanner.mode = "REAL"
    scanner.latest_regimes = {"BANKNIFTY": "RANGING"}
    scanner._passes_timeframe_entry_policy = lambda candidate: True
    scanner._market_location_entry_gate = lambda candidate: (True, "")
    scanner._candidate_15m_trend_agrees = lambda candidate: bool(getattr(candidate, "trend_15m_agrees", False))
    scanner._setting_enabled = lambda value, default=True: True
    for grade in ("B+", "A", "A+"):
        candidate = _location_candidate(
            grade=grade,
            confidence=0.60,
            timeframe="5min",
            inst_type="OPT",
            impulse_confirmed=False,
            is_explosive_bypass=False,
            trend_15m_agrees=True,
            intelligence_score=0.20,
            adx_value=30.0,
        )
        candidate.iv_percentile = 65.0
        candidate.chain_quality = {"score": 60, "stale": False}
        candidate.option_premium_ok = True

        assert scanner._passes_live_trade_ready_gate(candidate) is True


def test_choppy_5min_special_bplus_sixty_blocks_bad_premium_health():
    scanner = Scanner.__new__(Scanner)
    scanner.mode = "REAL"
    scanner.latest_regimes = {"BANKNIFTY": "RANGING"}
    scanner._passes_timeframe_entry_policy = lambda candidate: True
    scanner._market_location_entry_gate = lambda candidate: (True, "")
    scanner._candidate_15m_trend_agrees = lambda candidate: bool(getattr(candidate, "trend_15m_agrees", False))
    scanner._setting_enabled = lambda value, default=True: True
    candidate = _location_candidate(
        grade="B+",
        confidence=0.60,
        timeframe="5min",
        inst_type="OPT",
        impulse_confirmed=False,
        is_explosive_bypass=False,
        trend_15m_agrees=True,
        intelligence_score=0.80,
        adx_value=30.0,
    )
    candidate.iv_percentile = 75.0
    candidate.chain_quality = {"score": 60, "stale": False}
    candidate.option_premium_ok = False

    assert scanner._passes_live_trade_ready_gate(candidate) is False


def test_choppy_5min_special_bplus_sixty_blocks_missing_premium_health():
    scanner = Scanner.__new__(Scanner)
    scanner.mode = "REAL"
    scanner.latest_regimes = {"BANKNIFTY": "RANGING"}
    scanner._passes_timeframe_entry_policy = lambda candidate: True
    scanner._market_location_entry_gate = lambda candidate: (True, "")
    scanner._candidate_15m_trend_agrees = lambda candidate: bool(getattr(candidate, "trend_15m_agrees", False))
    scanner._setting_enabled = lambda value, default=True: True
    candidate = _location_candidate(
        grade="A+",
        confidence=0.60,
        timeframe="5min",
        inst_type="OPT",
        impulse_confirmed=False,
        is_explosive_bypass=False,
        trend_15m_agrees=True,
        intelligence_score=0.80,
        adx_value=30.0,
    )

    assert scanner._passes_live_trade_ready_gate(candidate) is False


def test_choppy_5min_special_sixty_ignores_stale_premium_ok_without_iv():
    scanner = Scanner.__new__(Scanner)
    scanner.mode = "REAL"
    scanner.latest_regimes = {"BANKNIFTY": "RANGING"}
    scanner._passes_timeframe_entry_policy = lambda candidate: True
    scanner._market_location_entry_gate = lambda candidate: (True, "")
    scanner._candidate_15m_trend_agrees = lambda candidate: bool(getattr(candidate, "trend_15m_agrees", False))
    scanner._setting_enabled = lambda value, default=True: True
    candidate = _location_candidate(
        grade="A+",
        confidence=0.60,
        timeframe="5min",
        inst_type="OPT",
        impulse_confirmed=False,
        is_explosive_bypass=False,
        trend_15m_agrees=True,
        intelligence_score=0.80,
        adx_value=30.0,
    )
    candidate.option_premium_ok = True

    assert scanner._passes_live_trade_ready_gate(candidate) is False


def test_choppy_15min_b_uses_15min_floor_not_5min_choppy_gate(monkeypatch):
    from config.settings import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "signal_grade_preference", "auto")
    monkeypatch.setattr(settings, "ut_regime_adaptation", True)
    monkeypatch.setattr(settings, "live_filter_leniency_pct", 0.0)
    monkeypatch.setattr(settings, "live_15min_min_confidence", 0.62)
    scanner = Scanner.__new__(Scanner)
    scanner.mode = "REAL"
    scanner.latest_regimes = {"BANKNIFTY": "RANGING"}
    scanner._passes_timeframe_entry_policy = lambda candidate: True
    scanner._market_location_entry_gate = lambda candidate: (True, "")
    scanner._candidate_15m_trend_agrees = lambda candidate: True
    scanner._setting_enabled = lambda value, default=True: True
    candidate = _location_candidate(
        grade="B",
        confidence=0.62,
        timeframe="15min",
        inst_type="OPT",
    )

    assert scanner._passes_candidate_grade_preference(candidate) == (
        True,
        "15min B-or-better confidence floor override",
    )
    assert scanner._passes_live_trade_ready_gate(candidate) is True


def test_15min_live_gate_blocks_below_independent_floor(monkeypatch):
    from config.settings import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "live_filter_leniency_pct", 0.0)
    monkeypatch.setattr(settings, "live_15min_min_confidence", 0.62)
    monkeypatch.setattr(settings, "live_15min_trending_momentum_min_confidence", 0.58)
    scanner = Scanner.__new__(Scanner)
    scanner.mode = "REAL"
    scanner.latest_regimes = {"BANKNIFTY": "RANGING"}
    scanner._passes_timeframe_entry_policy = lambda candidate: True
    scanner._market_location_entry_gate = lambda candidate: (True, "")
    scanner._candidate_15m_trend_agrees = lambda candidate: True
    scanner._setting_enabled = lambda value, default=True: True
    candidate = _location_candidate(
        grade="B",
        confidence=0.61,
        timeframe="15min",
        inst_type="FUT",
    )

    assert scanner._passes_live_trade_ready_gate(candidate) is False


def test_15min_live_gate_allows_lower_trending_momentum_floor(monkeypatch):
    from config.settings import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "signal_grade_preference", "auto")
    monkeypatch.setattr(settings, "ut_regime_adaptation", True)
    monkeypatch.setattr(settings, "live_filter_leniency_pct", 0.0)
    monkeypatch.setattr(settings, "live_15min_min_confidence", 0.62)
    monkeypatch.setattr(settings, "live_15min_trending_momentum_min_confidence", 0.58)
    monkeypatch.setattr(settings, "live_15min_trending_momentum_adx_threshold", 24.0)
    scanner = Scanner.__new__(Scanner)
    scanner.mode = "REAL"
    scanner.latest_regimes = {"BANKNIFTY": "RANGING"}
    scanner._passes_timeframe_entry_policy = lambda candidate: True
    scanner._market_location_entry_gate = lambda candidate: (True, "")
    scanner._candidate_15m_trend_agrees = lambda candidate: True
    scanner._setting_enabled = lambda value, default=True: True
    candidate = _location_candidate(
        grade="B",
        confidence=0.58,
        timeframe="15min",
        inst_type="OPT",
        adx_value=24.0,
    )

    assert scanner._passes_candidate_grade_preference(candidate) == (
        True,
        "15min B-or-better confidence floor override",
    )
    assert scanner._passes_live_trade_ready_gate(candidate) is True


def test_unknown_regime_allows_only_a_or_aplus_at_75_confidence():
    scanner = Scanner.__new__(Scanner)
    scanner.mode = "REAL"
    scanner.latest_regimes = {"BANKNIFTY": "UNKNOWN"}
    scanner._passes_timeframe_entry_policy = lambda candidate: True
    scanner._market_location_entry_gate = lambda candidate: (True, "")
    scanner._candidate_15m_trend_agrees = lambda candidate: True
    scanner._setting_enabled = lambda value, default=True: True

    allowed = _location_candidate(
        grade="A",
        confidence=0.75,
        timeframe="15min",
        inst_type="FUT",
    )
    low_conf = _location_candidate(
        grade="A",
        confidence=0.74,
        timeframe="15min",
        inst_type="FUT",
    )
    weak_grade = _location_candidate(
        grade="B",
        confidence=0.90,
        timeframe="15min",
        inst_type="FUT",
    )

    assert scanner._passes_live_trade_ready_gate(allowed) is True
    assert scanner._passes_live_trade_ready_gate(low_conf) is False
    assert scanner._passes_live_trade_ready_gate(weak_grade) is False


def test_choppy_option_stabilization_uses_configured_buffer_and_two_scans(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "live_signal_stabilization_seconds_5min", 30.0)
    scanner = Scanner.__new__(Scanner)
    scanner.latest_regimes = {"BANKNIFTY": "VOLATILE"}
    candidate = _location_candidate(timeframe="5min", inst_type="OPT")

    seconds, scans, adaptive = scanner._live_signal_stabilization_requirements(candidate)

    assert seconds == 30.0
    assert scans == 2
    assert adaptive is True


def test_timeframe_stabilization_buffers_are_operator_configured(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "live_signal_stabilization_seconds_5min", 30.0)
    monkeypatch.setattr(settings, "live_signal_stabilization_seconds_15min", 30.0)
    scanner = Scanner.__new__(Scanner)
    scanner.latest_regimes = {"BANKNIFTY": "TRENDING_UP"}

    five = _location_candidate(timeframe="5min", inst_type="FUT")
    fifteen = _location_candidate(timeframe="15min", inst_type="FUT")

    assert scanner._live_signal_stabilization_requirements(five) == (30.0, 1, False)
    assert scanner._live_signal_stabilization_requirements(fifteen) == (30.0, 1, False)


def test_midcap_15min_choppy_weak_tape_uses_longer_stabilization(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "ut_regime_adaptation", False)
    monkeypatch.setattr(settings, "live_signal_stabilization_seconds_15min", 30.0)
    monkeypatch.setattr(settings, "midcap_15min_risky_stabilization_seconds", 60.0)
    monkeypatch.setattr(settings, "midcap_15min_risky_stabilization_min_scans", 3)
    monkeypatch.setattr(settings, "midcap_15min_risky_stabilization_min_volume_ratio", 1.05)
    monkeypatch.setattr(settings, "midcap_15min_risky_stabilization_min_intel_score", 0.25)
    scanner = Scanner.__new__(Scanner)
    scanner.latest_regimes = {"MIDCPNIFTY": "RANGING"}

    candidate = _location_candidate(
        instrument="MIDCPNIFTY",
        timeframe="15min",
        inst_type="OPT",
        volume_ratio=0.0,
        intelligence_score=0.0,
    )

    assert scanner._live_signal_stabilization_requirements(candidate) == (60.0, 3, True)


def test_midcap_15min_choppy_confirmed_tape_keeps_base_stabilization(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "live_signal_stabilization_seconds_15min", 30.0)
    monkeypatch.setattr(settings, "midcap_15min_risky_stabilization_min_volume_ratio", 1.05)
    monkeypatch.setattr(settings, "midcap_15min_risky_stabilization_min_intel_score", 0.25)
    scanner = Scanner.__new__(Scanner)
    scanner.latest_regimes = {"MIDCPNIFTY": "RANGING"}

    candidate = _location_candidate(
        instrument="MIDCPNIFTY",
        timeframe="15min",
        inst_type="OPT",
        volume_ratio=1.20,
        intelligence_score=0.40,
    )

    assert scanner._live_signal_stabilization_requirements(candidate) == (30.0, 1, True)


def test_15min_choppy_scalp_guard_blocks_without_signed_intel(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "fifteen_min_choppy_scalp_guard_enabled", True)
    signal_ts = datetime(2026, 7, 6, 14, 50)
    frames = {
        (name, tf): _late_reclaim_frame(signal_ts, freq="min" if tf == "1min" else "5min", volume=1100.0)
        for name in ["NIFTY", "BANKNIFTY", "SENSEX", "MIDCPNIFTY"]
        for tf in ["1min", "5min"]
    }
    scanner = _late_reclaim_scanner(frames, signal_ts)
    candidate = _location_candidate(
        instrument="MIDCPNIFTY",
        timeframe="15min",
        inst_type="OPT",
        option_type="CE",
        direction="LONG",
        signal_timestamp=signal_ts,
        intelligence_score=0.0,
    )

    allowed, reason = scanner._fifteen_min_choppy_scalp_context(candidate, "RANGING")

    assert allowed is False
    assert "signed intelligence" in reason
    assert getattr(candidate, "fifteen_min_choppy_scalp_entry", False) is False


def test_15min_choppy_scalp_guard_blocks_known_low_volume(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "fifteen_min_choppy_scalp_guard_enabled", True)
    monkeypatch.setattr(settings, "fifteen_min_choppy_scalp_min_volume_ratio", 1.05)
    signal_ts = datetime(2026, 7, 6, 14, 50)
    frames = {
        (name, tf): _late_reclaim_frame(signal_ts, freq="min", volume=1100.0)
        for name in ["NIFTY", "BANKNIFTY", "SENSEX", "MIDCPNIFTY"]
        for tf in ["1min", "5min"]
    }
    scanner = _late_reclaim_scanner(frames, signal_ts)
    candidate = _location_candidate(
        instrument="MIDCPNIFTY",
        timeframe="15min",
        inst_type="OPT",
        option_type="CE",
        direction="LONG",
        signal_timestamp=signal_ts,
        # Above fifteen_min_choppy_scalp_min_intel_score (0.35 since 2026-08-11) so the
        # VOLUME condition under test is what blocks, not the intel floor. Was 0.24 when
        # that floor was 0.20.
        intelligence_score=0.40,
        volume_ratio=1.01,
    )

    allowed, reason = scanner._fifteen_min_choppy_scalp_context(candidate, "RANGING")

    assert allowed is False
    assert "volume 1.01x below 1.05x" in reason
    assert getattr(candidate, "fifteen_min_choppy_scalp_entry", False) is False


def test_15min_choppy_scalp_guard_allows_buy_and_sell_micro_confirmation(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "ut_regime_adaptation", False)
    monkeypatch.setattr(settings, "fifteen_min_choppy_scalp_guard_enabled", True)
    monkeypatch.setattr(settings, "fifteen_min_choppy_scalp_min_intel_score", 0.20)
    monkeypatch.setattr(settings, "fifteen_min_choppy_scalp_min_volume_ratio", 1.05)
    signal_ts = datetime(2026, 7, 6, 14, 50)

    all_indices = ["NIFTY", "BANKNIFTY", "SENSEX", "MIDCPNIFTY"]
    for signal_type, direction, option_type, intel in [
        ("BUY", "LONG", "CE", 0.24),
        ("SELL", "SHORT", "PE", -0.24),
    ]:
        frames = {
            (name, tf): _late_reclaim_frame(
                signal_ts,
                freq="min",
                volume=1100.0,
                signal_type=signal_type,
            )
            for name in all_indices
            for tf in ["1min", "5min"]
        }
        for instrument in all_indices:
            scanner = _late_reclaim_scanner(frames, signal_ts, signal_type=signal_type)
            candidate = _location_candidate(
                instrument=instrument,
                timeframe="15min",
                inst_type="OPT",
                option_type=option_type,
                direction=direction,
                rr=2.5,
                signal_timestamp=signal_ts,
                intelligence_score=intel,
            )

            allowed, reason = scanner._fifteen_min_choppy_scalp_context(candidate, "RANGING")

            assert allowed is True, f"{instrument} {signal_type}"
            assert signal_type in reason
            assert candidate.fifteen_min_choppy_scalp_entry is True
            assert candidate.scalp_lock_mode is True
            assert candidate.rr == 1.5
            assert candidate.fifteen_min_choppy_scalp_context["signal_type"] == signal_type


def test_15min_choppy_scalp_guard_is_engine_mode_agnostic(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "fifteen_min_choppy_scalp_guard_enabled", True)
    signal_ts = datetime(2026, 7, 6, 14, 50)
    all_indices = ["NIFTY", "BANKNIFTY", "SENSEX", "MIDCPNIFTY"]
    frames = {
        (name, tf): _late_reclaim_frame(signal_ts, freq="min", volume=1100.0)
        for name in all_indices
        for tf in ["1min", "5min"]
    }

    for mode in [
        "utbot",
        "ema",
        "consensus",
        "utbot_consensus",
        "quad_union",
        "quad_prime",
        "adaptive_dual",
    ]:
        monkeypatch.setattr(settings, "scanner_engine", mode)
        for instrument in all_indices:
            scanner = _late_reclaim_scanner(frames, signal_ts)
            scanner.mode = "REAL"
            scanner.latest_regimes = {name: "RANGING" for name in all_indices}
            scanner._late_entry_scalp_active = lambda _candidate: False
            scanner._passes_timeframe_entry_policy = lambda _candidate: True
            scanner._late_session_entry_block_reason = lambda _candidate: ""
            scanner._market_location_entry_gate = lambda _candidate: (True, "")
            scanner._candidate_15m_trend_agrees = lambda _candidate: False
            scanner.log_signal_decision_once = lambda *_args, **_kwargs: None
            candidate = _location_candidate(
                instrument=instrument,
                timeframe="15min",
                inst_type="OPT",
                option_type="CE",
                direction="LONG",
                grade="A+",
                confidence=0.90,
                signal_timestamp=signal_ts,
                # Above fifteen_min_choppy_scalp_min_intel_score (0.35 since 2026-08-11) so the
        # VOLUME condition under test is what blocks, not the intel floor. Was 0.24 when
        # that floor was 0.20.
        intelligence_score=0.40,
            )

            assert scanner._passes_live_trade_ready_gate(candidate) is True, f"{mode} {instrument}"
            assert candidate.fifteen_min_choppy_scalp_entry is True
            assert candidate.scalp_lock_mode is True


def test_15min_choppy_scalp_profit_lock_uses_special_floor(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "profit_lock_min_r_multiple", 1.0)
    monkeypatch.setattr(settings, "scalp_profit_lock_min_r_multiple", 0.50)
    monkeypatch.setattr(settings, "fifteen_min_choppy_scalp_profit_lock_r_multiple", 0.20)
    monkeypatch.setattr(settings, "fifteen_min_choppy_scalp_min_peak_rs", 250.0)
    scanner = Scanner.__new__(Scanner)
    special = SimpleNamespace(fifteen_min_choppy_scalp_entry=True)
    normal = SimpleNamespace(fifteen_min_choppy_scalp_entry=False)

    assert scanner._profit_lock_min_peak(True, 1700.0, "15min", trade_like=special) == 340.0
    assert scanner._profit_lock_min_peak(True, 1700.0, "15min", trade_like=normal) == 850.0

    lock_stop, reason, ratio = scanner._profit_lock_floor(
        entry_price=100.0,
        direction="LONG",
        qty=10,
        peak_pnl=400.0,
        scalp_mode=True,
        initial_risk_rs=1700.0,
        timeframe="15min",
        trade_like=special,
    )
    assert reason == "SMART_PROFIT_LOCK"
    assert ratio > 0
    assert lock_stop > 100.0


def test_15min_entry_cutoff_grace_allows_signal_formed_by_cutoff(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "ut_no_entry_after", "15:01")
    monkeypatch.setattr(settings, "live_15min_entry_cutoff_grace_seconds", 90.0)
    scanner = Scanner.__new__(Scanner)
    candidate = _location_candidate(
        timeframe="15min",
        signal_timestamp=datetime(2026, 7, 6, 15, 0),
    )

    assert scanner._entry_cutoff_allows_candidate(candidate, datetime(2026, 7, 6, 15, 1, 30)) is True
    assert scanner._entry_cutoff_allows_candidate(candidate, datetime(2026, 7, 6, 15, 2, 31)) is False


def test_15min_entry_cutoff_grace_blocks_signal_formed_after_cutoff(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "ut_no_entry_after", "15:01")
    monkeypatch.setattr(settings, "live_15min_entry_cutoff_grace_seconds", 90.0)
    scanner = Scanner.__new__(Scanner)
    candidate = _location_candidate(
        timeframe="15min",
        signal_timestamp=datetime(2026, 7, 6, 15, 2),
    )

    assert scanner._entry_cutoff_allows_candidate(candidate, datetime(2026, 7, 6, 15, 2, 10)) is False


def test_stabilization_never_matures_before_configured_time():
    # This is the live bug shape observed in logs: a 5min signal configured for
    # 30s must not mature early because stable scans shortcut the time buffer.
    assert Scanner._stabilization_ready(29.9, 30.0, 5, 2, True) is False
    assert Scanner._stabilization_ready(30.0, 30.0, 1, 2, True) is False
    assert Scanner._stabilization_ready(30.0, 30.0, 2, 2, True) is True


def test_late_session_choppy_option_blocks_without_momentum(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "late_session_choppy_entry_guard_enabled", True)
    monkeypatch.setattr(settings, "late_session_choppy_min_runway_minutes", 30.0)
    monkeypatch.setattr(settings, "ut_force_exit_time", "15:25")

    scanner = Scanner.__new__(Scanner)
    scanner.mode = "REAL"
    scanner.latest_regimes = {"BANKNIFTY": "RANGING"}
    scanner._setting_enabled = lambda value, default=True: bool(default if value is None else value)
    scanner._entry_reversal_relief_context = lambda *args, **kwargs: None
    candidate = _location_candidate(signal_timestamp=datetime(2026, 6, 29, 15, 5))

    reason = scanner._late_session_entry_block_reason(candidate)

    assert "late-session choppy option blocked" in reason


def test_late_session_choppy_option_allows_confirmed_momentum_scalp(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "late_session_choppy_entry_guard_enabled", True)
    monkeypatch.setattr(settings, "late_session_choppy_min_runway_minutes", 30.0)
    monkeypatch.setattr(settings, "ut_force_exit_time", "15:25")

    scanner = Scanner.__new__(Scanner)
    scanner.mode = "REAL"
    scanner.latest_regimes = {"BANKNIFTY": "RANGING"}
    scanner._setting_enabled = lambda value, default=True: bool(default if value is None else value)
    candidate = _location_candidate(
        signal_timestamp=datetime(2026, 6, 29, 15, 5),
        market_wide_reversal_entry=True,
    )

    reason = scanner._late_session_entry_block_reason(candidate)

    assert reason == ""
    assert candidate.scalp_lock_mode is True


def test_nonadaptive_and_spike_stabilization_readiness():
    assert Scanner._stabilization_ready(29.9, 30.0, 10, 1, False) is False
    assert Scanner._stabilization_ready(30.0, 30.0, 1, 1, False) is True

    # Volume-spike entries keep their separate short buffer, but still need the
    # configured time and both confirming scans.
    assert Scanner._stabilization_ready(4.9, 5.0, 2, 2, True) is False
    assert Scanner._stabilization_ready(5.0, 5.0, 1, 2, True) is False
    assert Scanner._stabilization_ready(5.0, 5.0, 2, 2, True) is True


def test_stabilization_preflight_waits_for_last_10_seconds_and_does_not_mutate():
    scanner = Scanner.__new__(Scanner)
    scanner._signal_preflight_cache = {}
    scanner._diagnostics = {"stabilization": {}}
    scanner.latest_regimes = {"BANKNIFTY": "RANGING"}
    scanner.inst_pref = "AUTO"
    scanner._apply_strict_instrument_pref_to_entries = lambda rows: rows
    scanner._subscribe_candidate_quote = lambda *_args, **_kwargs: None

    resolved = []

    def resolve(rows):
        prepared = rows[0]
        prepared.trading_symbol = "BANKNIFTY_PREPARED_PE"
        prepared.symbol_token = "PREPARED_TOKEN"
        prepared.quote_verified = True
        resolved.append(prepared)
        return rows

    scanner._resolve_unbuilt_opt_entries = resolve
    candidate = _location_candidate(
        trading_symbol="",
        symbol_token="",
        quote_verified=False,
    )

    assert scanner._maybe_prepare_stabilization_preflight(
        "BANKNIFTY_5min_SHORT",
        candidate,
        elapsed=19.9,
        stable_seconds=30.0,
    ) is False
    assert scanner._signal_preflight_cache == {}
    assert resolved == []

    assert Scanner._stabilization_ready(20.1, 30.0, 5, 1, False) is False
    assert scanner._maybe_prepare_stabilization_preflight(
        "BANKNIFTY_5min_SHORT",
        candidate,
        elapsed=20.1,
        stable_seconds=30.0,
    ) is True

    assert candidate.trading_symbol == ""
    assert candidate.symbol_token == ""
    assert candidate.quote_verified is False
    assert resolved and resolved[0] is not candidate
    assert scanner._diagnostics["stabilization"]["preflight_started"] == 1
    assert scanner._diagnostics["stabilization"]["preflight_ready"] == 1


def test_stabilization_preflight_applies_only_after_fingerprint_still_matches():
    scanner = Scanner.__new__(Scanner)
    scanner._signal_preflight_cache = {}
    scanner._diagnostics = {"stabilization": {}}
    scanner.latest_regimes = {"BANKNIFTY": "RANGING"}
    scanner.inst_pref = "AUTO"
    scanner._apply_strict_instrument_pref_to_entries = lambda rows: rows
    scanner._subscribe_candidate_quote = lambda *_args, **_kwargs: None

    def resolve(rows):
        prepared = rows[0]
        prepared.trading_symbol = "BANKNIFTY_PREPARED_PE"
        prepared.symbol_token = "PREPARED_TOKEN"
        prepared.quote_verified = True
        prepared.quote_source = "stabilization_preflight"
        prepared.reasons = list(prepared.reasons or []) + ["Prepared during stabilization"]
        return rows

    scanner._resolve_unbuilt_opt_entries = resolve
    key = "BANKNIFTY_5min_SHORT"
    candidate = _location_candidate(trading_symbol="", symbol_token="")

    assert scanner._maybe_prepare_stabilization_preflight(
        key,
        candidate,
        elapsed=25.0,
        stable_seconds=30.0,
    ) is True
    assert scanner._apply_stabilization_preflight(key, candidate) is True

    assert candidate.trading_symbol == "BANKNIFTY_PREPARED_PE"
    assert candidate.symbol_token == "PREPARED_TOKEN"
    assert candidate.quote_verified is True
    assert candidate.quote_source == "stabilization_preflight"
    assert candidate.stabilization_preflight_applied is True
    assert "Prepared during stabilization" in candidate.reasons
    assert scanner._signal_preflight_cache == {}
    assert scanner._diagnostics["stabilization"]["preflight_cache_hit"] == 1


def test_stabilization_preflight_stale_cache_is_ignored():
    scanner = Scanner.__new__(Scanner)
    scanner._signal_preflight_cache = {}
    scanner._diagnostics = {"stabilization": {}}
    scanner.latest_regimes = {"BANKNIFTY": "RANGING"}
    scanner.inst_pref = "AUTO"
    scanner._apply_strict_instrument_pref_to_entries = lambda rows: rows
    scanner._subscribe_candidate_quote = lambda *_args, **_kwargs: None

    def resolve(rows):
        rows[0].trading_symbol = "BANKNIFTY_PREPARED_PE"
        rows[0].symbol_token = "PREPARED_TOKEN"
        return rows

    scanner._resolve_unbuilt_opt_entries = resolve
    key = "BANKNIFTY_5min_SHORT"
    candidate = _location_candidate(trading_symbol="", symbol_token="")

    assert scanner._maybe_prepare_stabilization_preflight(
        key,
        candidate,
        elapsed=25.0,
        stable_seconds=30.0,
    ) is True
    candidate.grade = "B"

    assert scanner._apply_stabilization_preflight(key, candidate) is False
    assert candidate.trading_symbol == ""
    assert candidate.symbol_token == ""
    assert scanner._signal_preflight_cache == {}
    assert scanner._diagnostics["stabilization"]["preflight_cache_stale"] == 1


def test_sell_flicker_repaint_override_is_not_readmitted(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "stabilization_momentum_override_enabled", True)
    scanner = Scanner.__new__(Scanner)
    scanner.latest_regimes = {"BANKNIFTY": "TRENDING_DOWN"}
    scanner._setting_enabled = lambda value, default=True: bool(default if value is None else value)
    calls = []
    scanner._entry_reversal_relief_context = lambda *args, **kwargs: calls.append(args) or {
        "reason": "market-wide SELL rollover"
    }
    candidate = _location_candidate(
        direction="SHORT",
        source_signal_type="SELL",
        signal_timestamp=datetime(2026, 7, 6, 14, 48),
    )

    assert scanner._stabilization_repaint_override_context(candidate, candidate.signal_timestamp) is None
    assert calls == []


def test_sell_late_session_rollover_can_override_repaint_guard(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "stabilization_momentum_override_enabled", True)
    scanner = Scanner.__new__(Scanner)
    scanner._setting_enabled = lambda value, default=True: bool(default if value is None else value)
    scanner._late_session_reclaim_context = lambda *args, **kwargs: {
        "type": "late_session_reclaim",
        "signal_type": "SELL",
        "move_label": "rollover",
        "reason": "late-session synchronized SELL rollover",
    }
    scanner._entry_reversal_relief_context = lambda *args, **kwargs: None
    candidate = _location_candidate(
        instrument="NIFTY",
        direction="SHORT",
        option_type="PE",
        source_signal_type="SELL",
        signal_timestamp=datetime(2026, 7, 6, 14, 50),
    )

    context = scanner._stabilization_repaint_override_context(candidate, candidate.signal_timestamp)

    assert context["type"] == "late_session_reclaim"
    assert context["signal_type"] == "SELL"
    assert context["move_label"] == "rollover"


def test_late_buy_flicker_repaint_override_requires_reclaim_basket(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "stabilization_momentum_override_enabled", True)
    scanner = Scanner.__new__(Scanner)
    scanner.latest_regimes = {"NIFTY": "TRENDING_UP"}
    scanner._setting_enabled = lambda value, default=True: bool(default if value is None else value)
    scanner._late_session_reclaim_context = lambda *args, **kwargs: None
    calls = []
    scanner._entry_reversal_relief_context = lambda *args, **kwargs: calls.append(args) or {
        "reason": "market-wide BUY reversal"
    }
    candidate = _location_candidate(
        instrument="NIFTY",
        direction="LONG",
        option_type="CE",
        source_signal_type="BUY",
        signal_timestamp=datetime(2026, 7, 6, 14, 58),
    )

    context = scanner._stabilization_repaint_override_context(candidate, candidate.signal_timestamp)

    assert context is None
    assert calls == []


def test_late_buy_flicker_repaint_override_accepts_reclaim_basket(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "stabilization_momentum_override_enabled", True)
    scanner = Scanner.__new__(Scanner)
    scanner._setting_enabled = lambda value, default=True: bool(default if value is None else value)
    scanner._late_session_reclaim_context = lambda *args, **kwargs: {
        "type": "late_session_reclaim",
        "signal_type": "BUY",
        "move_label": "reclaim",
        "reason": "late-session synchronized BUY reclaim",
    }
    scanner._entry_reversal_relief_context = lambda *args, **kwargs: None
    candidate = _location_candidate(
        instrument="NIFTY",
        direction="LONG",
        option_type="CE",
        source_signal_type="BUY",
        signal_timestamp=datetime(2026, 7, 6, 14, 58),
    )

    context = scanner._stabilization_repaint_override_context(candidate, candidate.signal_timestamp)

    assert context["type"] == "late_session_reclaim"
    assert context["signal_type"] == "BUY"
    assert context["move_label"] == "reclaim"


def test_midday_buy_flicker_repaint_override_still_uses_confirmed_relief(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "stabilization_momentum_override_enabled", True)
    scanner = Scanner.__new__(Scanner)
    scanner.latest_regimes = {"NIFTY": "TRENDING_UP"}
    scanner._setting_enabled = lambda value, default=True: bool(default if value is None else value)
    scanner._late_session_reclaim_context = lambda *args, **kwargs: None
    scanner._entry_reversal_relief_context = lambda *args, **kwargs: {
        "reason": "market-wide BUY reversal"
    }
    candidate = _location_candidate(
        instrument="NIFTY",
        direction="LONG",
        option_type="CE",
        source_signal_type="BUY",
        signal_timestamp=datetime(2026, 7, 6, 13, 58),
    )

    context = scanner._stabilization_repaint_override_context(candidate, candidate.signal_timestamp)

    assert context == {"reason": "market-wide BUY reversal"}


def test_daily_counters_restore_manual_session_entries_and_losses():
    now = datetime.now(IST).replace(tzinfo=None).replace(hour=12, minute=0, second=0, microsecond=0)
    entry = _location_candidate(
        signal_timestamp=now - timedelta(minutes=10),
        accepted_by_gate=True,
    )
    exit_row = _location_candidate(
        signal_timestamp=entry.signal_timestamp,
        action="EXIT",
        exit_timestamp=now - timedelta(minutes=5),
        pnl=-500.0,
    )
    scanner = Scanner.__new__(Scanner)
    scanner.trades = SimpleNamespace(closed_trades=[], open_trades={})
    scanner._session_trade_candidates = {
        "BANKNIFTY": {
            scanner._candidate_history_key(entry): entry,
            scanner._candidate_history_key(exit_row): exit_row,
        }
    }
    scanner._trades_today = {}
    scanner._losses_today = {}
    scanner._last_exit_time = {}
    scanner._counted_entry_keys = set()
    scanner._last_reset_date = None

    scanner._reset_daily_counters()

    assert scanner._trades_today["BANKNIFTY"] == 1
    assert scanner._losses_today["BANKNIFTY"] == 1
    assert scanner._last_exit_time["BANKNIFTY"] == exit_row.exit_timestamp


def test_consecutive_index_losses_survive_another_index_exiting():
    """CHANGED 2026-08-10. This asserted that ANY other index's completed trade cleared
    NIFTY's run, measured on the global exit tape. With four books interleaving that made
    the max_consecutive_losses halt unreachable: measured live that day, NIFTY lost four
    times and the streak read 1, 1, 1, 2 because SENSEX and BANKNIFTY exits kept clearing
    it, so the halt at 3 never fired and a fourth losing entry was allowed.

    A NIFTY run is now broken only by a NIFTY outcome that is not a loss — what
    "3 consecutive NIFTY losses" means to someone watching one chart."""
    scanner = Scanner.__new__(Scanner)
    scanner._losses_today = {}
    scanner._last_loss_outcome_instrument = ""
    scanner._setting_enabled = lambda v, d=True: bool(v) if v is not None else d

    assert scanner._record_consecutive_loss_outcome("NIFTY", -100.0) == 1
    assert scanner._record_consecutive_loss_outcome("NIFTY", -200.0) == 2

    # Another index losing does NOT clear NIFTY.
    assert scanner._record_consecutive_loss_outcome("MIDCPNIFTY", -50.0) == 1
    assert scanner._losses_today["NIFTY"] == 2
    assert scanner._record_consecutive_loss_outcome("NIFTY", -300.0) == 3

    # Only NIFTY's own non-loss breaks NIFTY's run.
    assert scanner._record_consecutive_loss_outcome("SENSEX", 250.0) == 0
    assert scanner._losses_today["NIFTY"] == 3
    assert scanner._record_consecutive_loss_outcome("NIFTY", 250.0) == 0
    assert scanner._losses_today["NIFTY"] == 0


def test_todays_interleaved_sequence_now_reaches_the_halt():
    """The live 2026-08-10 exit order. Under the old global tape NIFTY peaked at 2."""
    scanner = Scanner.__new__(Scanner)
    scanner._losses_today = {}
    scanner._last_loss_outcome_instrument = ""
    scanner._setting_enabled = lambda v, d=True: bool(v) if v is not None else d

    sequence = ["SENSEX", "NIFTY", "BANKNIFTY", "NIFTY", "SENSEX",
                "BANKNIFTY", "NIFTY", "NIFTY", "SENSEX"]
    peak = {}
    for name in sequence:
        streak = scanner._record_consecutive_loss_outcome(name, -100.0)
        peak[name] = max(peak.get(name, 0), streak)
    assert peak["NIFTY"] == 4, peak
    assert peak["NIFTY"] >= 3, "the max_consecutive_losses halt would still never fire"


def test_the_global_tape_behaviour_is_still_available():
    """Kept behind consecutive_loss_streak_per_index=False."""
    scanner = Scanner.__new__(Scanner)
    scanner._losses_today = {}
    scanner._last_loss_outcome_instrument = ""
    scanner._setting_enabled = lambda v, d=True: False

    assert scanner._record_consecutive_loss_outcome("NIFTY", -100.0) == 1
    assert scanner._record_consecutive_loss_outcome("NIFTY", -200.0) == 2
    assert scanner._record_consecutive_loss_outcome("MIDCPNIFTY", -50.0) == 1
    assert scanner._losses_today["NIFTY"] == 0


def test_daily_counter_rebuild_merges_exit_sources_in_global_chronological_order():
    base = datetime.now(IST).replace(tzinfo=None).replace(
        hour=13, minute=42, second=0, microsecond=0
    )

    def exit_row(instrument, exit_time, pnl):
        return _location_candidate(
            instrument=instrument,
            signal_timestamp=exit_time - timedelta(minutes=2),
            source_signal_timestamp=exit_time - timedelta(minutes=2),
            action="EXIT",
            exit_timestamp=exit_time,
            pnl=pnl,
            accepted_by_gate=False,
        )

    # Exact shape around the reported 14:26 block: a SENSEX win, two NIFTY
    # losses, then a MIDCPNIFTY loss. The MIDCPNIFTY close interrupts NIFTY's
    # same-index run before the next NIFTY signal is evaluated.
    exits = [
        exit_row("SENSEX", base + timedelta(seconds=12), 287.67),
        exit_row("NIFTY", base + timedelta(seconds=15), -223.0),
        exit_row("NIFTY", base + timedelta(minutes=5, seconds=25), -372.5),
        exit_row("MIDCPNIFTY", base + timedelta(minutes=20, seconds=29), -410.0),
    ]
    scanner = Scanner.__new__(Scanner)
    scanner.trades = SimpleNamespace(closed_trades=[], open_trades={})
    scanner._session_trade_candidates = {}
    for row in exits:
        book = scanner._session_trade_candidates.setdefault(row.instrument, {})
        book[scanner._candidate_history_key(row)] = row
    scanner._trades_today = {}
    scanner._losses_today = {}
    scanner._last_exit_time = {}
    scanner._counted_entry_keys = set()
    scanner._counted_outcome_signatures = set()
    scanner._last_loss_outcome_instrument = ""
    scanner._last_reset_date = None

    scanner._reset_daily_counters()

    # CHANGED 2026-08-10 with the per-index streak. The subject of this test is that the
    # rebuild merges both exit sources in global chronological order; the NIFTY figure is
    # incidental. Two NIFTY losses, never broken by a NIFTY win, is now 2 — the
    # MIDCPNIFTY loss that follows no longer clears another index's run.
    assert scanner._losses_today["NIFTY"] == 2
    assert scanner._losses_today["MIDCPNIFTY"] == 1


def test_index_halt_message_matches_uninterrupted_same_index_semantics():
    scanner = Scanner.__new__(Scanner)
    scanner._losses_today = {}
    scanner._last_loss_outcome_instrument = ""
    for _ in range(3):
        scanner._record_consecutive_loss_outcome("NIFTY", -100.0)
    scanner._reset_daily_counters = lambda: None
    scanner._trades_today = {}
    scanner._dynamic_trade_budget = lambda _instrument: (10, "base")
    scanner.max_consecutive_losses = 3
    scanner.index_cooldown_minutes = 0.0
    scanner._last_exit_time = {}
    scanner._index_cooldown_applies = lambda *_a: (False, "")
    scanner._post_loss_cooldown_block = lambda *_a, **_k: (False, "")

    allowed, reason = scanner._can_trade_instrument(
        "NIFTY", datetime.now(IST).replace(tzinfo=None)
    )

    assert allowed is False
    assert reason == (
        "Same-index loss suspension: skipped one NIFTY candidate "
        "after 3 consecutive losses"
    )
    # It is a cooling-off, not a lockout: the streak is consumed by the skip so the
    # next signal is judged on its own merits. A halt that only a win could clear,
    # on an index refused every entry, could never clear at all — on 2026-08-12 that
    # sat NIFTY and SENSEX out of the 14:15 reversal.
    assert scanner._losses_today["NIFTY"] == 0
    assert scanner._can_trade_instrument(
        "NIFTY", datetime.now(IST).replace(tzinfo=None)
    )[0] is True
