"""Multi-signal mixed book: 1 FUT (1 lot) + rest OPT on multi-engine/TF waves.

Cascade multi-TF stack (cascade-only):
  - single-index: 1m+5m+15m (+ dual 5/15)
  - multi-index: lead keeps multi-TF; others one best 5/15
  - all indices compete equally for the sole FUT slot
Normal: max 2 concurrent / dual 2+1.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from config.settings import Settings, get_settings
from engine.signal_processor import TradeCandidate, SignalProcessor


@pytest.fixture(autouse=True)
def _native_scanner_mode(monkeypatch):
    """These cases cover native mixed-book policy, regardless of the live mode."""
    monkeypatch.setattr(get_settings(), "scanner_engine", "quad_prime")
    # Fixed-lot multiplier semantics; equal-risk sizing is covered in test_payoff_fixes_2026_10_08.
    monkeypatch.setattr(get_settings(), "risk_based_lots_enabled", False)


def _cand(
    instrument,
    direction="LONG",
    timeframe="5min",
    conf=0.8,
    grade="A",
    inst_type="FUT",
    **kwargs,
):
    c = TradeCandidate(
        instrument=instrument,
        direction=direction,
        price=100.0 if inst_type == "OPT" else 25000.0,
        stop=90.0 if inst_type == "OPT" else 24975.0,
        target=120.0 if inst_type == "OPT" else 25050.0,
        lots=2,
        lot_size=25,
        grade=grade,
        confidence=conf,
        timeframe=timeframe,
        inst_type=inst_type,
        option_type="CE" if direction == "LONG" and inst_type == "OPT" else "",
        atm_strike=25000.0 if inst_type == "OPT" else 0.0,
        multiplier=0.5 if inst_type == "OPT" else 1.0,
        trading_symbol=f"{instrument}-FUT" if inst_type == "FUT" else f"{instrument}25000CE",
        symbol_token="1",
        rr=1.5,
        score=conf,
        reasons=[],
        action="ENTRY",
        status="TRADE SIGNAL",
    )
    for k, v in kwargs.items():
        setattr(c, k, v)
    return c


def _scanner():
    from scanner import Scanner

    sc = Scanner.__new__(Scanner)
    sc.inst_pref = "AUTO"
    sc.mode = "HISTORICAL"
    sc.active_indices = ["NIFTY", "BANKNIFTY", "SENSEX", "MIDCPNIFTY"]
    sc.user_lots = {i: 2 for i in sc.active_indices}
    sc.user_lots_fut = {i: 2 for i in sc.active_indices}
    sc.options_sl_pct = 15.0
    sc.futures_sl_pct = 0.1
    sc.latest_regimes = {i: "TRENDING_UP" for i in sc.active_indices}
    sc.config = {
        "indices": {
            i: {"strike_interval": 50, "lot_size": 25} for i in sc.active_indices
        }
    }
    sc.market_info = {
        i: {"current_fut": f"{i}-I", "current_fut_token": f"t{i}"}
        for i in sc.active_indices
    }
    sc.candles = SimpleNamespace(get_latest_price=lambda name: 25000.0)
    sc.data = MagicMock()
    sc.data.get_option_token.side_effect = lambda inst, strike, ot: {
        "symbol": f"{inst}{int(strike)}{ot}",
        "token": f"tok-{inst}-{strike}-{ot}",
    }
    sc.data.get_ltp.return_value = 150.0
    sc.expiry = SimpleNamespace(get_dte=lambda *a, **k: 3)
    sc.signal_processor = SimpleNamespace(
        _option_signed_delta=lambda *a, **k: 0.5 if a[2] == "CE" else -0.5
    )
    sc.trades = SimpleNamespace(open_trades={})
    sc._setting_enabled = lambda v, d=True: bool(v) if isinstance(v, bool) else d
    sc.log_event = lambda *a, **k: None
    return sc


def test_mixed_book_trigger_on_three_instruments():
    sc = _scanner()
    cands = [
        _cand("NIFTY", conf=0.9),
        _cand("BANKNIFTY", conf=0.85),
        _cand("SENSEX", conf=0.8),
    ]
    assert sc._multi_signal_mixed_book_trigger(cands) is True


def test_mixed_book_one_fut_rest_opt():
    sc = _scanner()
    cands = [
        _cand("NIFTY", conf=0.95, grade="A+", timeframe="15min", inst_type="OPT"),
        _cand("BANKNIFTY", conf=0.88, grade="A", timeframe="5min", inst_type="FUT"),
        _cand("SENSEX", conf=0.82, grade="A", timeframe="5min", inst_type="FUT"),
        _cand("MIDCPNIFTY", conf=0.75, grade="B+", timeframe="15min", inst_type="FUT"),
    ]
    out = sc._apply_multi_signal_mixed_book(cands)
    fut = [c for c in out if c.inst_type == "FUT"]
    opt = [c for c in out if c.inst_type == "OPT"]
    assert len(fut) == 1
    assert fut[0].lots == 1
    assert fut[0].instrument == "NIFTY"  # highest rank
    assert len(opt) >= 2
    assert all(c.lots >= 1 for c in opt)
    # MIDCP may be FUT-blocked → OPT
    assert all(c.option_type in {"CE", "PE"} for c in opt)


def test_verified_option_risk_geometry_survives_conversion():
    """Mixed-book conversion must not replace an ATR-aware option stop with 15%."""
    sc = _scanner()
    candidate = _cand("NIFTY", inst_type="OPT", conf=0.70)
    candidate.quote_verified = True
    candidate.initial_stop = candidate.stop = 96.0
    candidate.target = 110.0

    assert sc._force_candidate_to_options(candidate) is True
    assert candidate.price == pytest.approx(100.0)
    assert candidate.stop == pytest.approx(96.0)
    assert candidate.initial_stop == pytest.approx(96.0)
    assert candidate.target == pytest.approx(110.0)
    assert candidate.risk_geometry_source == "candidate_atr_preserved"
    assert sc._candidate_projected_risk_rs(candidate) == pytest.approx(200.0)


def test_invalid_option_risk_geometry_is_rebuilt_under_configured_cap():
    sc = _scanner()
    candidate = _cand("NIFTY", inst_type="OPT", conf=0.70)
    candidate.quote_verified = True
    candidate.initial_stop = candidate.stop = 80.0  # 20% risk exceeds the 10% ordinary cap
    candidate.target = 120.0

    assert sc._force_candidate_to_options(candidate) is True
    assert candidate.stop == pytest.approx(90.0)
    assert candidate.initial_stop == pytest.approx(90.0)
    assert candidate.target == pytest.approx(115.0)
    assert candidate.risk_geometry_source == "option_cap_rebuilt"


def test_mixed_book_selection_preserves_option_risk_and_order_risk():
    """The preservation survives the actual one-FUT/rest-OPT selection wave."""
    sc = _scanner()
    option = _cand("NIFTY", inst_type="OPT", conf=0.70)
    option.quote_verified = True
    option.initial_stop = option.stop = 96.0
    option.target = 110.0
    futures = [
        _cand("BANKNIFTY", conf=0.95, timeframe="15min"),
        _cand("SENSEX", conf=0.90, timeframe="15min"),
    ]

    out = sc._apply_multi_signal_mixed_book([option, *futures])
    selected = next(c for c in out if c.instrument == "NIFTY")
    assert selected.inst_type == "OPT"
    assert selected.stop == pytest.approx(96.0)
    assert selected.initial_stop == pytest.approx(96.0)
    assert selected.target == pytest.approx(110.0)
    expected_risk = abs(selected.price - selected.initial_stop) * selected.lots * selected.lot_size
    assert sc._candidate_projected_risk_rs(selected) == pytest.approx(expected_risk)


def test_mixed_book_fut_slot_equity_not_nifty_only():
    """BankNifty can take the sole FUT slot when it ranks highest (same rule for all)."""
    sc = _scanner()
    cands = [
        _cand("NIFTY", conf=0.70, grade="A", timeframe="15min"),
        _cand("BANKNIFTY", conf=0.98, grade="A+", timeframe="15min"),
        _cand("SENSEX", conf=0.80, grade="A", timeframe="5min"),
    ]
    out = sc._apply_multi_signal_mixed_book(cands)
    fut = [c for c in out if c.inst_type == "FUT"]
    assert len(fut) == 1
    assert fut[0].instrument == "BANKNIFTY"
    assert fut[0].lots == 1
    assert all(c.inst_type == "OPT" for c in out if c.instrument != "BANKNIFTY")


def test_mixed_book_respects_existing_open_fut():
    sc = _scanner()
    sc.trades.open_trades = {
        "x": SimpleNamespace(inst_type="FUT", is_ghost=False, instrument="NIFTY")
    }
    cands = [
        _cand("NIFTY", conf=0.9),
        _cand("BANKNIFTY", conf=0.85),
        _cand("SENSEX", conf=0.8),
    ]
    out = sc._apply_multi_signal_mixed_book(cands)
    fut = [c for c in out if c.inst_type == "FUT"]
    assert len(fut) == 0
    assert all(c.inst_type == "OPT" for c in out)


def test_mixed_book_enabled_when_inst_pref_locked():
    sc = _scanner()
    sc.inst_pref = "FUT"
    cands = [_cand("NIFTY"), _cand("BANKNIFTY"), _cand("SENSEX")]
    assert sc._multi_signal_mixed_book_trigger(cands) is True


def test_mixed_book_collapses_best_tf_per_index():
    """Non-cascade multi-index still collapses to one best TF per index."""
    sc = _scanner()
    cands = [
        _cand("NIFTY", timeframe="5min", conf=0.70),
        _cand("NIFTY", timeframe="15min", conf=0.92, grade="A+"),
        _cand("BANKNIFTY", timeframe="5min", conf=0.80),
        _cand("SENSEX", timeframe="15min", conf=0.78),
    ]
    out = sc._apply_multi_signal_mixed_book(cands)
    nifty = [c for c in out if c.instrument == "NIFTY"]
    assert len(nifty) == 1
    assert nifty[0].timeframe == "15min"


def test_cascade_multi_index_core_full_stack_midcap_solo_best_two():
    """Cascade: core full multi-TF; MIDCAP solo = best 2 of 1/5/15; FUT blocked on mid."""
    sc = _scanner()
    cands = [
        _cand("NIFTY", timeframe="1min", conf=0.88, grade="A", one_way_cascade_entry=True),
        _cand("NIFTY", timeframe="5min", conf=0.90, grade="A", one_way_cascade_entry=True),
        _cand("NIFTY", timeframe="15min", conf=0.95, grade="A+", one_way_cascade_entry=True),
        _cand("BANKNIFTY", timeframe="1min", conf=0.83, one_way_cascade_entry=True),
        _cand("BANKNIFTY", timeframe="5min", conf=0.84, one_way_cascade_entry=True),
        _cand("BANKNIFTY", timeframe="15min", conf=0.86, one_way_cascade_entry=True),
        _cand("SENSEX", timeframe="5min", conf=0.80, one_way_cascade_entry=True),
        _cand("SENSEX", timeframe="15min", conf=0.82, one_way_cascade_entry=True),
        # MIDCAP solo: 3 TFs → keep best 2 (15min + 5min by conf)
        _cand("MIDCPNIFTY", timeframe="1min", conf=0.70, one_way_cascade_entry=True),
        _cand("MIDCPNIFTY", timeframe="5min", conf=0.74, one_way_cascade_entry=True),
        _cand("MIDCPNIFTY", timeframe="15min", conf=0.75, one_way_cascade_entry=True),
    ]
    out = sc._apply_multi_signal_mixed_book(cands)
    nifty = [c for c in out if c.instrument == "NIFTY"]
    bn = [c for c in out if c.instrument == "BANKNIFTY"]
    sx = [c for c in out if c.instrument == "SENSEX"]
    mid = [c for c in out if c.instrument == "MIDCPNIFTY"]

    assert len(nifty) == 3
    assert {c.timeframe for c in nifty} == {"1min", "5min", "15min"}
    assert len(bn) == 3
    assert {c.timeframe for c in bn} == {"1min", "5min", "15min"}
    assert len(sx) == 2
    # MIDCAP solo: best 2 of 1/5/15 (drop lowest conf 1min)
    assert len(mid) == 2
    assert {c.timeframe for c in mid} == {"5min", "15min"}
    assert all(c.inst_type == "OPT" for c in mid)

    fut = [c for c in out if c.inst_type == "FUT"]
    assert len(fut) == 1
    assert fut[0].lots == 1
    assert fut[0].instrument in {"NIFTY", "BANKNIFTY", "SENSEX"}
    assert fut[0].instrument != "MIDCPNIFTY"


def test_midcap_cascade_dual_is_2_main_plus_1_dual():
    """MIDCAP dual: 2 main + 1 dual when dual is outside clone window (>1m later)."""
    from datetime import datetime, timedelta
    from scanner import Scanner

    sc = _scanner()
    sc._source_lane = Scanner._source_lane
    t0 = datetime(2026, 7, 9, 10, 0, 0)
    t_later = t0 + timedelta(seconds=90)  # > 60s → not a clone
    rows = [
        _cand("MIDCPNIFTY", timeframe="1min", conf=0.70, source_lane="A",
              one_way_cascade_entry=True, signal_timestamp=t0),
        _cand("MIDCPNIFTY", timeframe="5min", conf=0.80, source_lane="A",
              one_way_cascade_entry=True, signal_timestamp=t0),
        _cand("MIDCPNIFTY", timeframe="15min", conf=0.90, source_lane="A",
              one_way_cascade_entry=True, signal_timestamp=t0),
        _cand("MIDCPNIFTY", timeframe="5min", conf=0.85, source_lane="B",
              one_way_cascade_entry=True, signal_timestamp=t_later),
        _cand("MIDCPNIFTY", timeframe="15min", conf=0.88, source_lane="B",
              one_way_cascade_entry=True, signal_timestamp=t_later),
    ]
    picked = sc._pick_cascade_tf_stack_for_midcap(rows)
    assert len(picked) == 3  # 2 main + 1 dual (outside 1m window)
    main = [c for c in picked if (getattr(c, "source_lane", "") or "A") != "B"]
    dual = [c for c in picked if getattr(c, "source_lane", "") == "B"]
    assert len(main) == 2
    assert {c.timeframe for c in main} == {"15min", "5min"}
    assert len(dual) == 1
    assert dual[0].timeframe == "15min"


def test_dual_clone_reduction_same_tf_within_1m_keeps_first_only():
    """Same index+TF dual engines within 1 minute → keep earliest only."""
    from datetime import datetime, timedelta
    from scanner import Scanner

    sc = _scanner()
    sc._source_lane = Scanner._source_lane
    t0 = datetime(2026, 7, 9, 10, 0, 0)
    t30 = t0 + timedelta(seconds=30)
    rows = [
        _cand("NIFTY", timeframe="15min", conf=0.90, source_lane="A",
              signal_timestamp=t0, one_way_cascade_entry=True),
        _cand("NIFTY", timeframe="5min", conf=0.85, source_lane="A",
              signal_timestamp=t0, one_way_cascade_entry=True),
        _cand("NIFTY", timeframe="1min", conf=0.80, source_lane="A",
              signal_timestamp=t0, one_way_cascade_entry=True),
        # Dual same TF within 30s → clone, drop
        _cand("NIFTY", timeframe="15min", conf=0.92, source_lane="B",
              signal_timestamp=t30, one_way_cascade_entry=True),
        _cand("NIFTY", timeframe="5min", conf=0.88, source_lane="B",
              signal_timestamp=t30, one_way_cascade_entry=True),
    ]
    picked = sc._pick_cascade_tf_stack_for_index(rows, primary_max=3, dual_addon_max=2)
    assert len(picked) == 3  # duals dropped as clones
    assert all((getattr(c, "source_lane", "") or "A") != "B" for c in picked)
    assert {c.timeframe for c in picked} == {"1min", "5min", "15min"}
    # First kept is lane A at t0
    for c in picked:
        if c.timeframe == "15min":
            assert getattr(c, "source_lane", "") in {"", "A"}


def test_midcap_cascade_solo_best_two_of_three_tfs():
    sc = _scanner()
    from scanner import Scanner

    sc._source_lane = Scanner._source_lane
    rows = [
        _cand("MIDCPNIFTY", timeframe="1min", conf=0.91, source_lane="A"),
        _cand("MIDCPNIFTY", timeframe="5min", conf=0.80, source_lane="A"),
        _cand("MIDCPNIFTY", timeframe="15min", conf=0.85, source_lane="A"),
    ]
    picked = sc._pick_cascade_tf_stack_for_midcap(rows)
    assert len(picked) == 2
    assert {c.timeframe for c in picked} == {"1min", "15min"}  # top 2 by conf


def test_cascade_opt_lots_scale_2x_to_3x_fut_stays_1x():
    """Mixed-book OPT: base 1 × cascade 3 = 3 lots (not 6). FUT slot stays 1 lot.

    Earlier stress used base=2 → 2×3=6 lots (one scale, not 6× mult).
    Operator base is 1 lot → cascade yields 2–3 lots only.
    """
    sc = _scanner()
    sc.user_lots = {i: 1 for i in sc.active_indices}
    cands = [
        _cand("NIFTY", conf=0.95, grade="A+", timeframe="15min", one_way_cascade_entry=True),
        _cand("BANKNIFTY", conf=0.90, timeframe="15min", one_way_cascade_entry=True),
        _cand("SENSEX", conf=0.85, timeframe="15min", one_way_cascade_entry=True),
    ]
    out = sc._apply_multi_signal_mixed_book(cands)
    fut = [c for c in out if c.inst_type == "FUT"]
    opt = [c for c in out if c.inst_type == "OPT"]
    assert len(fut) == 1
    assert fut[0].lots == 1  # sole FUT slot always 1 lot
    # base OPT lots=1 × cascade mult 3 = 3
    assert all(c.lots == 3 for c in opt)


def test_cascade_opt_base2_is_2x3_not_six_x_mult():
    """If base is 2 lots, cascade 3× → 6 lots total (2×3), still single scale."""
    sc = _scanner()
    sc.user_lots = {i: 2 for i in sc.active_indices}
    cands = [
        _cand("NIFTY", conf=0.95, grade="A+", timeframe="15min", one_way_cascade_entry=True),
        _cand("BANKNIFTY", conf=0.90, timeframe="15min", one_way_cascade_entry=True),
        _cand("SENSEX", conf=0.85, timeframe="15min", one_way_cascade_entry=True),
    ]
    out = sc._apply_multi_signal_mixed_book(cands)
    opt = [c for c in out if c.inst_type == "OPT"]
    assert all(c.lots == 6 for c in opt)


def test_cascade_wave_scored_selects_primary_and_dual():
    """Simultaneous dual same-TF → clone reduction keeps first (primary) only."""
    from datetime import datetime, timedelta

    settings = get_settings()
    t0 = datetime(2026, 7, 9, 10, 0, 0)
    t90 = t0 + timedelta(seconds=90)
    sig = SimpleNamespace(
        signal_type="BUY",
        price=25000.0,
        source_engine="utbot",
        source_engines=["utbot"],
        source_lane="",
        timestamp=t0,
        bar_index=1,
    )
    sig_b5 = SimpleNamespace(
        signal_type="BUY",
        price=25010.0,
        source_engine="apex_hybrid",
        source_engines=["apex_hybrid"],
        source_lane="B",
        timestamp=t90,  # staggered → dual allowed
        bar_index=2,
    )
    sig_b15 = SimpleNamespace(
        signal_type="BUY",
        price=25020.0,
        source_engine="apex_hybrid",
        source_engines=["apex_hybrid"],
        source_lane="B",
        timestamp=t90,
        bar_index=3,
    )
    scored = [
        (0.90, sig, "15min", "A+", 0.90, [], False),
        (0.85, sig, "5min", "A", 0.85, [], False),
        (0.80, sig, "1min", "A", 0.80, [], False),
        (0.88, sig_b5, "5min", "A", 0.88, [], False),
        (0.87, sig_b15, "15min", "A", 0.87, [], False),
    ]
    wave = SignalProcessor._select_cascade_wave_scored_rows(
        scored, settings, "adaptive_dual", instrument="NIFTY"
    )
    tfs_lanes = [
        (str(r[2]), SignalProcessor._signal_source_lane(r[1], "adaptive_dual") or "A")
        for r in wave
    ]
    assert ("15min", "A") in tfs_lanes
    assert ("5min", "A") in tfs_lanes
    assert ("1min", "A") in tfs_lanes
    assert ("5min", "B") in tfs_lanes
    assert ("15min", "B") in tfs_lanes
    assert len(wave) == 5

    # Same timestamps → dual clones dropped
    for s in (sig_b5, sig_b15):
        s.timestamp = t0
    wave2 = SignalProcessor._select_cascade_wave_scored_rows(
        scored, settings, "adaptive_dual", instrument="NIFTY"
    )
    assert len(wave2) == 3


def test_cascade_sibling_emit_from_winner():
    settings = get_settings()
    winner = _cand(
        "NIFTY",
        timeframe="15min",
        conf=0.95,
        grade="A+",
        one_way_cascade_entry=True,
        source_lane="A",
    )
    sig15 = SimpleNamespace(
        signal_type="BUY",
        price=25000.0,
        source_engine="utbot",
        source_engines=["utbot"],
        source_lane="A",
        timestamp=None,
        bar_index=1,
    )
    sig5 = SimpleNamespace(
        signal_type="BUY",
        price=24990.0,
        source_engine="utbot",
        source_engines=["utbot"],
        source_lane="A",
        timestamp=None,
        bar_index=2,
    )
    sig1 = SimpleNamespace(
        signal_type="BUY",
        price=24980.0,
        source_engine="utbot",
        source_engines=["utbot"],
        source_lane="",
        timestamp=None,
        bar_index=3,
        volume_spike_entry=True,
    )
    scored = [
        (0.95, sig15, "15min", "A+", 0.95, ["w"], False),
        (0.90, sig5, "5min", "A", 0.90, ["f"], False),
        (0.85, sig1, "1min", "A", 0.85, ["o"], False),
    ]
    sp = SignalProcessor.__new__(SignalProcessor)
    siblings = sp._emit_cascade_tf_siblings(winner, scored, settings, "utbot")
    assert len(siblings) == 2
    assert {s.timeframe for s in siblings} == {"5min", "1min"}
    assert all(s.one_way_cascade_entry for s in siblings)


def test_cascade_index_cap_allows_multi_tf_stack():
    sc = _scanner()
    cand = _cand("NIFTY", one_way_cascade_entry=True, timeframe="5min")
    cap, basis = sc._active_index_trade_cap(cand, existing_count=2)
    assert cap >= 3
    assert "cascade" in basis.lower()


def test_normal_index_cap_is_two():
    sc = _scanner()
    cand = _cand("NIFTY", one_way_cascade_entry=False, timeframe="5min")
    cap, basis = sc._active_index_trade_cap(cand, existing_count=0)
    assert cap == 2
    assert basis == "normal"


def test_cascade_concurrency_allows_cross_tf_same_book():
    from scanner import Scanner

    sc = _scanner()
    sc._source_lane = Scanner._source_lane
    open_t = SimpleNamespace(
        instrument="NIFTY",
        timeframe="15min",
        inst_type="OPT",
        grade="A",
        confidence=0.9,
        direction="LONG",
        status="OPEN",
        source_lane="A",
        is_ghost=False,
    )
    sc.trades.open_trades = {"t1": open_t}
    sc._active_session_entry_candidates = lambda *a, **k: []
    sc._is_quad_prime_raw_candidate = lambda *a, **k: False
    sc._quad_prime_duplicate_active = lambda *a, **k: False
    sc._dual_lane_concurrency_decision = lambda *a, **k: (None, "")
    sc._candidate_quality_same_or_better = lambda *a, **k: True
    sc._force_candidate_instrument_type = lambda *a, **k: True

    cand = _cand(
        "NIFTY",
        timeframe="5min",
        inst_type="OPT",
        one_way_cascade_entry=True,
        source_lane="A",
    )
    assert sc._prepare_candidate_for_concurrency(cand) is True


def test_heavy_trend_eligible_when_three_indices_align():
    """2026-09-26 user: 3 of 4 indices (was 2 of 4)."""
    sc = _scanner()
    sc._cached_results = {
        "BANKNIFTY_15min": {
            "signals": [SimpleNamespace(signal_type="BUY", price=1.0)]
        }
    }
    sc.trades.open_trades = {}
    # NIFTY firing LONG + BANKNIFTY cached BUY → only 2 aligned
    assert sc._heavy_trend_lot_scale_eligible("NIFTY", "LONG", "15min") is False
    sc._cached_results["SENSEX_15min"] = {"signals": [SimpleNamespace(signal_type="BUY", price=1.0)]}
    # + SENSEX → 3 aligned
    assert sc._heavy_trend_lot_scale_eligible("NIFTY", "LONG", "15min") is True
    # Only self with no peers → not enough
    sc._cached_results = {}
    assert sc._heavy_trend_lot_scale_eligible("NIFTY", "LONG", "15min") is False


def test_normal_concurrency_still_caps_at_two_without_rare_burst(monkeypatch):
    sc = _scanner()
    s = get_settings()
    monkeypatch.setattr(s, "max_concurrent_trades_per_index", 2)
    monkeypatch.setattr(s, "rare_burst_third_trade_enabled", False)
    from scanner import Scanner

    sc._source_lane = Scanner._source_lane
    sc._rare_burst_context = lambda *a, **k: (False, "no rare")
    sc._active_session_entry_candidates = lambda *a, **k: []
    sc._is_quad_prime_raw_candidate = lambda *a, **k: False
    sc._dual_lane_concurrency_decision = lambda *a, **k: (None, "")

    t1 = SimpleNamespace(
        instrument="NIFTY", timeframe="5min", inst_type="OPT",
        grade="A", confidence=0.8, direction="LONG", status="OPEN",
        source_lane="A", is_ghost=False,
    )
    t2 = SimpleNamespace(
        instrument="NIFTY", timeframe="15min", inst_type="FUT",
        grade="A", confidence=0.85, direction="LONG", status="OPEN",
        source_lane="A", is_ghost=False,
    )
    sc.trades.open_trades = {"a": t1, "b": t2}
    cand = _cand("NIFTY", timeframe="1min", one_way_cascade_entry=False)
    assert sc._prepare_candidate_for_concurrency(cand) is False
