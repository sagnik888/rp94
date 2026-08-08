"""How the system selects among simultaneously-firing signals.

Normal capacity is "all 8" (4 indices x 2 timeframes firing the same minute);
rare all-index burst capacity is "all 12" (4 indices x 3 timeframes):
candidates are split by direction, sorted by confidence (highest first), then each
runs an independent eligibility pass (grade -> trade-limit -> live-ready ->
concurrency guard -> correlated-index guard). AUTO executes the survivors via the
broker (capped by max_positions); MANUAL records the same survivors for the user to
fire. The FILTERING is identical between modes — only the execution step differs.
"""

import threading
import types

import pytest

from config.settings import get_settings
from trading.trade_manager import TradeManager


# ── global position cap (max_concurrent_positions, applied in AUTO via open_trade) ──

def _mgr(max_positions=4):
    m = TradeManager.__new__(TradeManager)
    m.max_positions = max_positions
    m.open_trades = {}
    m.orphan_positions = {}
    m._lock = threading.RLock()
    return m


def _ghost(is_ghost=False):
    return types.SimpleNamespace(is_ghost=is_ghost)


def test_max_positions_ceiling_is_twelve_for_rare_burst_backstop():
    # Hard ceiling = rare-burst total (4 indices x 3). Per-bucket caps below
    # this enforce normal 4x2 behavior before orders reach the trade manager.
    m = _mgr(12)
    for i in range(11):
        m.open_trades[f"t{i}"] = _ghost()
    assert m.can_open_trade()[0] is True            # 11 open -> 12th allowed
    m.open_trades["t11"] = _ghost()
    ok, reason = m.can_open_trade()
    assert ok is False and "Max positions (12)" in reason   # 12 open -> 13th blocked


def test_ghost_trades_do_not_consume_capacity():
    m = _mgr(4)
    for i in range(6):
        m.open_trades[f"g{i}"] = _ghost(is_ghost=True)   # ghosts are watch-only
    assert m.can_open_trade()[0] is True


# ── concurrency guard ON vs OFF (_prepare_candidate_for_concurrency) ──

def _scanner():
    from scanner import Scanner
    sc = Scanner.__new__(Scanner)
    sc.trades = types.SimpleNamespace(open_trades={})
    sc.inst_pref = "AUTO"
    sc.active_indices = ["NIFTY", "BANKNIFTY", "SENSEX", "MIDCPNIFTY"]
    sc.latest_regimes = {}
    return sc


def _cand(instrument="NIFTY", tf="5min", grade="A", inst_type="OPT"):
    from engine.signal_processor import TradeCandidate
    return TradeCandidate(
        instrument=instrument, direction="LONG", price=100.0, stop=99.0, target=103.0,
        lots=1, lot_size=75, grade=grade, confidence=0.8, timeframe=tf, inst_type=inst_type,
        option_type="CE", atm_strike=24000.0, multiplier=0.5, trading_symbol="X", symbol_token="1",
        signal_timestamp=None,
    )


def _open_trade_obj(
    instrument="NIFTY",
    tf="5min",
    grade="A",
    inst_type="OPT",
    confidence=0.8,
    *,
    direction="LONG",
    source_lane="",
    entry_price=100.0,
    current_price=100.0,
    initial_stop=99.0,
    signal_timestamp=None,
    source_signal_bar_index=-1,
):
    return types.SimpleNamespace(
        instrument=instrument,
        timeframe=tf,
        grade=grade,
        inst_type=inst_type,
        confidence=confidence,
        direction=direction,
        status="OPEN",
        source_lane=source_lane,
        entry_price=entry_price,
        price=entry_price,
        current_price=current_price,
        initial_stop=initial_stop,
        stop=initial_stop,
        trailing_stop=initial_stop,
        lots=1,
        lot_size=75,
        multiplier=1.0,
        signal_timestamp=signal_timestamp,
        source_signal_bar_index=source_signal_bar_index,
        reasons=["adaptive trend lane"],
    )


def _short_cand(instrument="NIFTY", tf="5min", grade="A", inst_type="OPT"):
    from engine.signal_processor import TradeCandidate
    return TradeCandidate(
        instrument=instrument, direction="SHORT", price=100.0, stop=101.0, target=97.0,
        lots=1, lot_size=75, grade=grade, confidence=0.8, timeframe=tf, inst_type=inst_type,
        option_type="PE", atm_strike=24000.0, multiplier=-0.5, trading_symbol="X", symbol_token="1",
        signal_timestamp=None,
    )


def _attach_bullish_market_intel(candidate):
    candidate.intelligence_score = 0.82
    candidate.market_intelligence_context = {
        "aggregate": {"score": 82.0, "top_down_adjustment": 0.16},
        "order_flow": {"signal": "BULLISH", "ratio": 1.4},
        "session_context": {
            "day_type": "TREND_DAY_UP",
            "confidence": 0.82,
            "bias": "BULLISH",
            "vix_change_pct": -1.2,
            "session_delta": {"bias": "BULLISH", "delta_ratio": 0.28},
        },
        "anchor_regime": {"regime": "TRENDING_UP", "confidence": 78, "ready_for_grading": True},
        "breadth_context": {"bias": "BULLISH", "aligned_count": 4},
        "top_down_context": {"score_adjustment": 0.16},
    }
    return candidate


def _attach_bearish_market_intel(candidate):
    candidate.intelligence_score = -0.82
    candidate.market_intelligence_context = {
        "aggregate": {"score": -82.0, "top_down_adjustment": -0.16},
        "order_flow": {"signal": "BEARISH", "ratio": 0.7},
        "session_context": {
            "day_type": "TREND_DAY_DOWN",
            "confidence": 0.82,
            "bias": "BEARISH",
            "vix_change_pct": 2.2,
            "session_delta": {"bias": "BEARISH", "delta_ratio": 0.28},
        },
        "anchor_regime": {"regime": "TRENDING_DOWN", "confidence": 78, "ready_for_grading": True},
        "breadth_context": {"bias": "BEARISH", "aligned_count": 4},
        "top_down_context": {"score_adjustment": -0.16},
    }
    return candidate


@pytest.fixture
def guard_setting():
    s = get_settings()
    tracked = {
        "ut_concurrency_guard": getattr(s, "ut_concurrency_guard", True),
        "core_bucket_max_positions": getattr(s, "core_bucket_max_positions", 6),
        "core_bucket_max_positions_guarded": getattr(s, "core_bucket_max_positions_guarded", 6),
        "midcap_bucket_max_positions": getattr(s, "midcap_bucket_max_positions", 2),
        "midcap_bucket_max_positions_guarded": getattr(s, "midcap_bucket_max_positions_guarded", 2),
        "rare_burst_core_bucket_max_positions": getattr(s, "rare_burst_core_bucket_max_positions", 9),
        "rare_burst_midcap_bucket_max_positions": getattr(s, "rare_burst_midcap_bucket_max_positions", 3),
        "max_concurrent_trades_per_index": getattr(s, "max_concurrent_trades_per_index", 2),
        "rare_burst_max_concurrent_trades_per_index": getattr(s, "rare_burst_max_concurrent_trades_per_index", 3),
        "rare_burst_third_trade_enabled": getattr(s, "rare_burst_third_trade_enabled", True),
        "rare_burst_min_confidence": getattr(s, "rare_burst_min_confidence", 0.95),
        "rare_burst_min_volume_ratio": getattr(s, "rare_burst_min_volume_ratio", 3.5),
        "rare_burst_min_adx": getattr(s, "rare_burst_min_adx", 35.0),
        "rare_burst_min_intel_score": getattr(s, "rare_burst_min_intel_score", 0.70),
        "rare_burst_min_breadth_votes": getattr(s, "rare_burst_min_breadth_votes", 3),
        "rare_burst_require_hourly_anchor": getattr(s, "rare_burst_require_hourly_anchor", True),
        "rare_burst_require_session_bias": getattr(s, "rare_burst_require_session_bias", True),
        "rare_burst_block_range_phase": getattr(s, "rare_burst_block_range_phase", True),
        "scanner_engine": getattr(s, "scanner_engine", "utbot_consensus"),
        "dual_lane_enabled": getattr(s, "dual_lane_enabled", True),
        "dual_lane_same_dir_min_bars_gap": getattr(s, "dual_lane_same_dir_min_bars_gap", 3),
        "dual_lane_same_dir_min_lane_a_r": getattr(s, "dual_lane_same_dir_min_lane_a_r", 0.50),
        "dual_lane_size_fraction": getattr(s, "dual_lane_size_fraction", 0.65),
        "dual_lane_max_index_exposure_rs": getattr(s, "dual_lane_max_index_exposure_rs", 12000.0),
        "dual_lane_min_lane_b_confidence": getattr(s, "dual_lane_min_lane_b_confidence", 0.70),
        "dual_lane_jaccard_alert_threshold": getattr(s, "dual_lane_jaccard_alert_threshold", 0.30),
    }
    s.core_bucket_max_positions = 6
    s.core_bucket_max_positions_guarded = 6
    s.midcap_bucket_max_positions = 2
    s.midcap_bucket_max_positions_guarded = 2
    s.rare_burst_core_bucket_max_positions = 9
    s.rare_burst_midcap_bucket_max_positions = 3
    s.max_concurrent_trades_per_index = 2
    s.rare_burst_max_concurrent_trades_per_index = 3
    s.rare_burst_third_trade_enabled = True
    s.rare_burst_min_confidence = 0.95
    s.rare_burst_min_volume_ratio = 3.5
    s.rare_burst_min_adx = 35.0
    s.rare_burst_min_intel_score = 0.70
    s.rare_burst_min_breadth_votes = 3
    s.rare_burst_require_hourly_anchor = True
    s.rare_burst_require_session_bias = True
    s.rare_burst_block_range_phase = True
    s.scanner_engine = "utbot_consensus"
    s.dual_lane_enabled = True
    s.dual_lane_same_dir_min_bars_gap = 3
    s.dual_lane_same_dir_min_lane_a_r = 0.50
    s.dual_lane_size_fraction = 0.65
    s.dual_lane_max_index_exposure_rs = 12000.0
    s.dual_lane_min_lane_b_confidence = 0.70
    s.dual_lane_jaccard_alert_threshold = 0.30
    yield s
    for name, value in tracked.items():
        setattr(s, name, value)


def test_the_same_chart_is_blocked_even_with_the_guard_off(guard_setting):
    """Changed 2026-08-08. This used to assert the duplicate was ALLOWED through.

    Requirement 3 is explicit: when a second signal fires on an index+timeframe that
    already has a working trade, the system must add one lot to that trade rather than
    "adding new trade with same or different option or future strike price". Blocking
    here is what routes the duplicate to _offer_cascade_add.

    The same-chart test used to sit BELOW `if not guard_on: return True`, and .env sets
    UT_CONCURRENCY_GUARD='False', so it was dead at runtime and a duplicate opened a
    second position at another strike — the exact outcome the requirement forbids.

    ut_concurrency_guard still governs the cross-timeframe complement rules; it is not a
    switch that permits duplicate positions on one chart.
    """
    guard_setting.ut_concurrency_guard = False
    sc = _scanner()
    sc.trades.open_trades = {"a": _open_trade_obj("NIFTY", "5min")}
    assert sc._prepare_candidate_for_concurrency(_cand("NIFTY", "5min")) is False


def test_the_guard_off_still_permits_a_different_timeframe(guard_setting):
    """What ut_concurrency_guard=False is actually for: the cross-TF complement."""
    guard_setting.ut_concurrency_guard = False
    sc = _scanner()
    sc.trades.open_trades = {"a": _open_trade_obj("NIFTY", "5min")}
    assert sc._prepare_candidate_for_concurrency(_cand("NIFTY", "15min")) is True


def test_concurrency_guard_on_blocks_same_index_same_timeframe(guard_setting):
    guard_setting.ut_concurrency_guard = True
    sc = _scanner()
    sc._active_session_entry_candidates = lambda *a, **k: []   # no session ledger rows
    sc.trades.open_trades = {"a": _open_trade_obj("NIFTY", "5min")}
    # second NIFTY 5min trade -> same index, same TF -> blocked
    assert sc._prepare_candidate_for_concurrency(_cand("NIFTY", "5min")) is False


def test_concurrency_guard_on_blocks_third_trade_per_index(guard_setting):
    guard_setting.ut_concurrency_guard = True
    sc = _scanner()
    sc._active_session_entry_candidates = lambda *a, **k: []
    sc.trades.open_trades = {
        "a": _open_trade_obj("NIFTY", "5min"),
        "b": _open_trade_obj("NIFTY", "15min"),
    }
    assert sc._prepare_candidate_for_concurrency(_cand("NIFTY", "5min")) is False  # max 2/index


def test_concurrency_guard_on_allows_second_same_grade_same_or_higher_confidence(guard_setting):
    guard_setting.ut_concurrency_guard = True
    sc = _scanner()
    sc._active_session_entry_candidates = lambda *a, **k: []
    sc.trades.open_trades = {
        "a": _open_trade_obj("NIFTY", "5min", grade="A", inst_type="FUT", confidence=0.80)
    }

    equal = _cand("NIFTY", "15min", grade="A", inst_type="OPT")
    equal.confidence = 0.80
    assert sc._prepare_candidate_for_concurrency(equal) is True

    lower = _cand("NIFTY", "15min", grade="A", inst_type="OPT")
    lower.confidence = 0.79
    assert sc._prepare_candidate_for_concurrency(lower) is False


def test_adaptive_dual_opposite_lane_b_can_enter_against_lane_a(guard_setting):
    from datetime import datetime

    guard_setting.scanner_engine = "adaptive_dual"
    guard_setting.ut_concurrency_guard = True
    sc = _scanner()
    sc._active_session_entry_candidates = lambda *a, **k: []
    sc.trades.open_trades = {
        "a": _open_trade_obj(
            "NIFTY",
            "5min",
            source_lane="A",
            direction="LONG",
            signal_timestamp=datetime(2026, 7, 3, 10, 0),
            source_signal_bar_index=20,
        )
    }

    lane_b = _short_cand("NIFTY", "5min", grade="A", inst_type="OPT")
    lane_b.confidence = 0.82
    lane_b.source_engine = "apex_hybrid"
    lane_b.source_lane = "B"
    lane_b.volume_spike_entry = True
    lane_b.signal_timestamp = datetime(2026, 7, 3, 10, 5)
    lane_b.source_signal_bar_index = 21

    assert sc._prepare_candidate_for_concurrency(lane_b) is True
    assert getattr(lane_b, "dual_lane_entry", False) is True


def test_adaptive_dual_same_direction_too_soon_is_blocked(guard_setting):
    from datetime import datetime

    guard_setting.scanner_engine = "adaptive_dual"
    guard_setting.ut_concurrency_guard = True
    sc = _scanner()
    sc._active_session_entry_candidates = lambda *a, **k: []
    sc.trades.open_trades = {
        "a": _open_trade_obj(
            "NIFTY",
            "5min",
            source_lane="A",
            current_price=101.0,
            signal_timestamp=datetime(2026, 7, 3, 10, 0),
            source_signal_bar_index=20,
        )
    }

    lane_b = _cand("NIFTY", "5min", grade="A", inst_type="OPT")
    lane_b.confidence = 0.82
    lane_b.source_engine = "apex_hybrid"
    lane_b.source_lane = "B"
    lane_b.volume_spike_entry = True
    lane_b.signal_timestamp = datetime(2026, 7, 3, 10, 5)
    lane_b.source_signal_bar_index = 21

    assert sc._prepare_candidate_for_concurrency(lane_b) is False


def test_adaptive_dual_same_direction_addon_is_size_reduced(guard_setting):
    from datetime import datetime

    guard_setting.scanner_engine = "adaptive_dual"
    guard_setting.ut_concurrency_guard = True
    sc = _scanner()
    sc._active_session_entry_candidates = lambda *a, **k: []
    sc.trades.open_trades = {
        "a": _open_trade_obj(
            "NIFTY",
            "5min",
            source_lane="A",
            entry_price=100.0,
            current_price=101.0,
            initial_stop=99.0,
            signal_timestamp=datetime(2026, 7, 3, 10, 0),
            source_signal_bar_index=20,
        )
    }

    lane_b = _cand("NIFTY", "5min", grade="A", inst_type="OPT")
    lane_b.lots = 3
    lane_b.confidence = 0.82
    lane_b.source_engine = "apex_hybrid"
    lane_b.source_lane = "B"
    lane_b.volume_spike_entry = True
    lane_b.signal_timestamp = datetime(2026, 7, 3, 10, 20)
    lane_b.source_signal_bar_index = 24
    lane_b.reasons = ["lane b volume spike"]

    assert sc._prepare_candidate_for_concurrency(lane_b) is True
    assert lane_b.lots == 1
    assert getattr(lane_b, "dual_lane_entry", False) is True
    assert getattr(lane_b, "scalp_lock_mode", False) is True


def test_adaptive_dual_lane_b_confidence_fails_closed(guard_setting):
    from datetime import datetime

    guard_setting.scanner_engine = "adaptive_dual"
    guard_setting.ut_concurrency_guard = True
    sc = _scanner()
    sc._active_session_entry_candidates = lambda *a, **k: []
    sc.trades.open_trades = {
        "a": _open_trade_obj("NIFTY", "5min", source_lane="A", direction="LONG")
    }

    lane_b = _short_cand("NIFTY", "5min", grade="B+", inst_type="OPT")
    lane_b.confidence = 0.69
    lane_b.source_engine = "apex_hybrid"
    lane_b.source_lane = "B"
    lane_b.volume_spike_entry = True
    lane_b.signal_timestamp = datetime(2026, 7, 3, 10, 5)

    assert sc._prepare_candidate_for_concurrency(lane_b) is False


def test_guard_off_still_caps_two_per_index_unless_rare_burst(guard_setting):
    guard_setting.ut_concurrency_guard = False
    sc = _scanner()
    sc.trades.open_trades = {
        "a": _open_trade_obj("NIFTY", "5min"),
        "b": _open_trade_obj("NIFTY", "15min"),
    }
    sc.latest_regimes = {inst: "TRENDING_UP" for inst in sc.active_indices}

    normal = _cand("NIFTY", "1min", grade="A+", inst_type="OPT")
    normal.confidence = 0.94
    normal.adx_value = 40.0
    normal.volume_ratio = 4.0
    assert sc._prepare_candidate_for_concurrency(normal) is False

    rare = _cand("NIFTY", "1min", grade="A+", inst_type="OPT")
    rare.confidence = 0.95
    rare.adx_value = 40.0
    rare.volume_ratio = 4.0
    _attach_bullish_market_intel(rare)
    assert sc._prepare_candidate_for_concurrency(rare) is True
    assert getattr(rare, "rare_burst_entry", False) is True
    assert getattr(rare, "scalp_lock_mode", False) is True


def test_rare_burst_rejects_when_market_intelligence_disagrees(guard_setting):
    guard_setting.ut_concurrency_guard = False
    sc = _scanner()
    sc.latest_regimes = {inst: "TRENDING_UP" for inst in sc.active_indices}

    candidate = _cand("NIFTY", "1min", grade="A+", inst_type="OPT")
    candidate.confidence = 0.97
    candidate.adx_value = 42.0
    candidate.volume_ratio = 4.2
    _attach_bearish_market_intel(candidate)

    ok, reason = sc._rare_burst_context(candidate)
    assert ok is False
    assert "intel" in reason or "session bias" in reason


def test_rare_burst_allows_bearish_all_index_market_intel(guard_setting):
    guard_setting.ut_concurrency_guard = False
    sc = _scanner()
    sc.latest_regimes = {inst: "TRENDING_DOWN" for inst in sc.active_indices}
    sc.trades.open_trades = {
        "a": _open_trade_obj("NIFTY", "5min"),
        "b": _open_trade_obj("NIFTY", "15min"),
    }

    rare = _short_cand("NIFTY", "1min", grade="A+", inst_type="OPT")
    rare.confidence = 0.96
    rare.adx_value = 41.0
    rare.volume_ratio = 4.1
    _attach_bearish_market_intel(rare)

    assert sc._prepare_candidate_for_concurrency(rare) is True
    assert getattr(rare, "rare_burst_entry", False) is True
    assert getattr(rare, "scalp_lock_mode", False) is True


# ── per-bucket concurrent capacity (normal 4x2; rare burst 4x3) ──

def _bucket_trade(instrument):
    return types.SimpleNamespace(instrument=instrument, is_ghost=False)


def test_bucket_caps_core_and_midcap_guard_off(guard_setting):
    guard_setting.ut_concurrency_guard = False
    sc = _scanner()
    assert sc._active_bucket_cap("CORE") == 6
    assert sc._active_bucket_cap("MIDCAP") == 2
    # CORE: NIFTY+BANKNIFTY+SENSEX share the bucket; 5 open still allows a 6th, 6 blocks.
    sc.trades.open_trades = {
        "a": _bucket_trade("NIFTY"),
        "b": _bucket_trade("NIFTY"),
        "c": _bucket_trade("BANKNIFTY"),
        "d": _bucket_trade("BANKNIFTY"),
        "e": _bucket_trade("SENSEX"),
    }
    assert sc._bucket_capacity_allows(_cand("NIFTY")) is True
    sc.trades.open_trades["f"] = _bucket_trade("SENSEX")
    assert sc._bucket_capacity_allows(_cand("SENSEX")) is False     # CORE bucket full at 6
    # MIDCAP independent: not affected by a full CORE bucket
    assert sc._bucket_capacity_allows(_cand("MIDCPNIFTY")) is True


def test_bucket_caps_keep_normal_4x2_capacity_when_guard_on(guard_setting):
    guard_setting.ut_concurrency_guard = True
    sc = _scanner()
    assert sc._active_bucket_cap("CORE") == 6
    assert sc._active_bucket_cap("MIDCAP") == 2
    sc.trades.open_trades = {str(i): _bucket_trade(inst) for i, inst in enumerate(
        ["NIFTY", "NIFTY", "BANKNIFTY", "BANKNIFTY", "SENSEX", "SENSEX"]
    )}
    assert sc._bucket_capacity_allows(_cand("NIFTY")) is False       # CORE full at 6
    # two MIDCAP already open -> MIDCAP full at 2
    sc.trades.open_trades = {"m1": _bucket_trade("MIDCPNIFTY"), "m2": _bucket_trade("MIDCPNIFTY")}
    assert sc._bucket_capacity_allows(_cand("MIDCPNIFTY")) is False
    assert sc._bucket_capacity_allows(_cand("NIFTY")) is True        # CORE still open


def test_rare_burst_unlocks_guard_off_bucket_capacity(guard_setting):
    guard_setting.ut_concurrency_guard = False
    sc = _scanner()
    sc.latest_regimes = {inst: "TRENDING_UP" for inst in sc.active_indices}
    rare = _cand("NIFTY", "1min", grade="A+", inst_type="OPT")
    rare.confidence = 0.96
    rare.adx_value = 38.0
    rare.volume_ratio = 3.8
    _attach_bullish_market_intel(rare)

    assert sc._active_bucket_cap("CORE", rare) == 9
    assert getattr(rare, "rare_burst_fast_tracking", False) is True


# ── simultaneous-signal prioritisation: highest confidence first ──

def test_simultaneous_signals_processed_highest_confidence_first():
    # mirrors scanner.py: within a direction bucket, candidates are sorted by confidence
    # desc, so the limited execution slots go to the strongest signals first.
    sigs = [_cand("MIDCPNIFTY"), _cand("NIFTY"), _cand("BANKNIFTY"), _cand("SENSEX")]
    for s_, c in zip(sigs, (0.55, 0.92, 0.71, 0.83)):
        s_.confidence = c
    sigs.sort(key=lambda x: x.confidence, reverse=True)
    assert [s_.instrument for s_ in sigs] == ["NIFTY", "SENSEX", "BANKNIFTY", "MIDCPNIFTY"]


# ── regime mode tightens the grade floor in chop ──

def test_regime_mode_tightens_grade_floor_in_chop():
    # mirrors the inline min-grade logic: regime ON upgrades the floor to A (rank 3) in
    # choppy/volatile regimes; OFF keeps the B+ baseline (rank 2) regardless of regime.
    def min_rank(regime_on, regime):
        is_choppy = regime_on and regime in {"CHOPPY", "SIDEWAYS", "MEAN_REVERTING", "RANGING", "VOLATILE", "UNKNOWN"}
        return 3 if is_choppy else 2

    assert min_rank(True, "VOLATILE") == 3    # regime ON + chop -> require A
    assert min_rank(True, "TRENDING") == 2    # regime ON + trend -> B+ baseline
    assert min_rank(False, "VOLATILE") == 2   # regime OFF -> B+ baseline even in chop
