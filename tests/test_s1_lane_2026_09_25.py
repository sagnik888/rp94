"""S1 lane, 2026-09-25 user spec.

- 1m trades are volume-burst / spike / snapback (or cascade) trades only, under every
  timeframe policy, S1 included.
- S1 keeps the special trade types (volume-spike, cascade, reclaim, snapback) as labels and
  1m admission; S1 levels, exits, cutoffs and sizing stay S1's.
- The S1 target: 1R, 0.7R on a weak move, runner to 30R (tests/test_s1_target_2026_09_25.py).
- A missed S1 signal (refused or repainted, intra-bar or a few bars later) may re-enter
  while the S1 score is the same or stronger than the missed signal's.
- Lots: the configured lots, 1x even on a cascade, 2x on a heavy trend; an entry the funds
  cannot cover is trimmed to the lots they can cover instead of being refused.
- S1 trades every index/timeframe instead of stopping at the same-direction core limit.
- The S1 loss-streak stop follows "Stop After Consecutive Losses" per index (see
  tests/test_s1_engine_2026_09_23.py).
"""
from datetime import datetime
from types import SimpleNamespace

import pytest

import scanner as scanner_module
from config.settings import Settings, timeframe_entry_timeframes
from engine import s1_lane
from engine.signal_processor import TradeCandidate
from scanner import Scanner

POLICIES = ("PRIMARY_15", "INCLUDE_5MIN", "PRIMARY_5", "INCLUDE_1MIN_5MIN", "INCLUDE_1MIN_5MIN_15MIN",
            "INCLUDE_1MIN_15MIN", "PRIMARY_1")


def _settings(**overrides):
    s = Settings()
    for k, v in overrides.items():
        object.__setattr__(s, k, v)
    return s


# ---------------------------------------------------------------- 1m burst-only


@pytest.mark.parametrize("policy", POLICIES)
def test_an_s1_one_minute_signal_enters_only_on_a_burst_unless_1m_is_selected(monkeypatch, policy):
    """2026-09-26 user: a plain 1m signal enters only when the TF toggle is 1M alone; every
    other policy takes 1m bursts/snapbacks/cascades only (was: burst-only everywhere)."""
    monkeypatch.setattr(scanner_module, "get_settings", lambda: SimpleNamespace(ut_timeframe_entry_policy=policy))
    sc = Scanner.__new__(Scanner)
    sc._gate_block = lambda candidate, message: False
    base = dict(instrument="NIFTY", direction="SHORT", source_engine="s1", source_lane="S1")
    plain = sc._passes_timeframe_entry_policy(SimpleNamespace(timeframe="1min", **base))
    assert plain is (policy == "PRIMARY_1")
    for flag in ("volume_spike_entry", "is_reversion_scalp", "one_way_cascade_entry"):
        assert sc._passes_timeframe_entry_policy(SimpleNamespace(timeframe="1min", **{flag: True}, **base)) is True
    five = sc._passes_timeframe_entry_policy(SimpleNamespace(timeframe="5min", **base))
    assert five is ("5min" in timeframe_entry_timeframes(policy))


# ---------------------------------------------------------------- target (see tests/test_s1_target_2026_09_25.py)


# ---------------------------------------------------------------- re-entry strength


def _seed(**kw):
    base = dict(instrument="NIFTY", timeframe="5min", direction="SHORT", s1_score=75.0,
                signal_timestamp=datetime(2026, 9, 24, 13, 40), source_engine="s1", source_lane="S1",
                underlying_entry=23100.0, underlying_stop=23123.1, action="ENTRY", status="REJECTED",
                confidence=0.0, reasons=[], source_signal_type="SELL", source_signal_price=23100.0)
    base.update(kw)
    return SimpleNamespace(**base)


def _state(bull, bear, at=datetime(2026, 9, 24, 13, 45)):
    return {"s1_bull_score": bull, "s1_bear_score": bear, "s1_state_bar_time": at}


@pytest.mark.parametrize("state,ok", [
    (_state(20, 80), True),                                   # stronger
    (_state(20, 75), True),                                   # same strength
    (_state(20, 70), False),                                  # weaker than the missed signal
    (_state(97, 76), False),                                  # strong enough but trend turned against
    (_state(20, 90, at=datetime(2026, 9, 24, 13, 35)), False),  # state older than the signal
    (None, False),
])
def test_reentry_needs_the_same_or_a_stronger_s1_score(state, ok):
    assert s1_lane.reentry_strength(_seed(), state, _settings())[0] is ok


def test_reentry_window_is_one_bar_on_5m_and_15m_and_three_on_1m():
    """2026-09-25 user: one bar after the signal candle closes on 15m/5m, three on 1m.
    Ages run from the signal timestamp (the candle's open), so the candle itself is added."""
    s = _settings(missed_signal_reentry_max_age_minutes=6.0)
    assert [s1_lane.reentry_bars(tf, s) for tf in ("1min", "5min", "15min")] == [3, 1, 1]
    assert [s1_lane.reentry_max_age_seconds(tf, s) for tf in ("1min", "5min", "15min")] == [240.0, 600.0, 1800.0]


def _reentry_scanner(monkeypatch, state=None, now=datetime(2026, 9, 24, 13, 50)):
    monkeypatch.setattr(scanner_module, "get_settings", lambda: _settings(missed_signal_reentry_min_age_seconds=20.0))
    sc = Scanner.__new__(Scanner)
    sc.mode = "REAL"
    sc._missed_reentry_candidates = {}
    sc._session_squareoff_clock = lambda: now
    sc._diag_inc = lambda *a, **k: None
    sc.mtf = SimpleNamespace(_result_cache={"NIFTY_5min": {"signals": [], "state": state or _state(20, 80)}})
    sc._missed_reentry_chase_check = lambda seed, side, at, cascade_confirmed=False: (True, "", {"moved": 0.0})
    return sc


def test_a_soft_refused_s1_signal_is_seeded_and_a_terminal_one_is_not(monkeypatch):
    sc = _reentry_scanner(monkeypatch)
    sc._remember_missed_reentry_seed(_seed(), reason="Entry policy blocks NIFTY", source="live_gate")
    assert len(sc._missed_reentry_candidates["NIFTY"]) == 1
    sc._missed_reentry_candidates = {}
    sc._remember_missed_reentry_seed(_seed(), reason="NO ENTRY - AFTER 15:10 cutoff", source="live_gate")
    assert sc._missed_reentry_candidates.get("NIFTY", []) == []


def test_a_repainted_s1_signal_stays_re_enterable_but_another_engines_does_not(monkeypatch):
    sc = _reentry_scanner(monkeypatch)
    sc._remember_missed_reentry_seed(_seed(), reason="S1 arrow repainted during stabilization", source="s1_repaint")
    assert len(sc._missed_reentry_candidates["NIFTY"]) == 1
    other = _seed(source_engine="utbot", source_lane="", confidence=0.7)
    sc._missed_reentry_candidates = {}
    sc._remember_missed_reentry_seed(other, reason="Repainted during 30s stabilization window", source="x")
    assert sc._missed_reentry_candidates.get("NIFTY", []) == []


def test_s1_support_is_its_own_strength_within_one_bar_after_the_close(monkeypatch):
    sc = _reentry_scanner(monkeypatch)
    ok, reason, ctx = sc._missed_reentry_support_context(_seed(), datetime(2026, 9, 24, 13, 50))
    assert ok, reason
    assert ctx["type"] == "s1_strength" and ctx["s1_score"] == 80.0
    weak = _reentry_scanner(monkeypatch, state=_state(20, 60))
    assert weak._missed_reentry_support_context(_seed(), datetime(2026, 9, 24, 13, 50))[0] is False
    # 13:40 5m signal closes 13:45; one more bar runs to 13:50, then the window shuts.
    assert sc._missed_reentry_support_context(_seed(), datetime(2026, 9, 24, 13, 51))[1] == "missed signal expired"
    other = _seed(source_engine="utbot", source_lane="", confidence=0.9)
    assert sc._missed_reentry_support_context(other, datetime(2026, 9, 24, 13, 50))[1] == "missed signal expired"


def test_an_s1_reentry_is_rebuilt_on_s1_levels(monkeypatch):
    sc = _reentry_scanner(monkeypatch)
    sc._get_trade_regime = lambda c, default="UNKNOWN": "RANGING"
    cand = TradeCandidate(
        instrument="NIFTY", direction="SHORT", price=150.0, stop=142.5, target=161.25, lots=1, lot_size=65,
        grade="B", confidence=0.0, timeframe="5min", inst_type="OPT", option_type="PE", atm_strike=23050,
        multiplier=-0.5, trading_symbol="NIFTY26SEP23050PE", symbol_token="1", rr=1.5, score=0.0, reasons=[],
        entry_spot=23050.0, spot_stop=23060.0, spot_target=23035.0, source_engine="s1", source_lane="S1",
    )
    sc._apply_s1_reentry_geometry(cand, _seed(), {"s1_score": 85.0}, datetime(2026, 9, 24, 13, 50))
    # 5m guard +1 step at score 85, RANGING cap: 15%; mapped index risk 23.1 x 0.5 = 11.55 is tighter.
    assert cand.s1_guard_pct == 15.0
    assert cand.stop == pytest.approx(150.0 - 11.55)
    assert cand.rr == 1.0 and cand.target == pytest.approx(150.0 + 11.55)    # normal trade: 1R
    assert cand.spot_stop == pytest.approx(23050.0 + 23.1)
    assert cand.spot_target == pytest.approx(23050.0 - 23.1)
    assert s1_lane.is_s1(cand) and cand.s1_score == 85.0 and cand.scalp_lock_mode is False


# ---------------------------------------------------------------- correlation and lots


def test_s1_trades_a_third_core_index_up_to_the_bucket_cap(monkeypatch):
    base = scanner_module.get_settings()
    monkeypatch.setattr(scanner_module, "get_settings", lambda: SimpleNamespace(
        **{**vars(base), "ut_correlated_core_limit": 2, "ut_correlated_allow_aplus_override": False}))

    def scanner_with(cap):
        s = Scanner.__new__(Scanner)
        s.mode = "REAL"
        s.active_indices = ["NIFTY", "BANKNIFTY", "SENSEX", "MIDCPNIFTY"]
        s.trades = SimpleNamespace(open_trades={
            f"T{i}": SimpleNamespace(instrument=n, direction="SHORT", grade="C", timeframe="5min", inst_type="OPT", is_ghost=False)
            for i, n in enumerate(("NIFTY", "BANKNIFTY"))})
        s._active_session_entry_candidates = lambda *a, **k: []
        s._active_bucket_cap = lambda bucket, candidate=None: cap
        s._record_live_reject = lambda *a, **k: None
        return s

    def cand(engine):
        return SimpleNamespace(instrument="SENSEX", direction="SHORT", grade="C", timeframe="5min", inst_type="OPT",
                               signal_timestamp=None, is_ghost=False, source_engine=engine,
                               source_lane="S1" if engine == "s1" else "")

    assert scanner_with(6)._passes_correlated_index_guard(cand("s1"), "SHORT") is True
    assert scanner_with(6)._passes_correlated_index_guard(cand("utbot"), "SHORT") is False
    assert scanner_with(2)._passes_correlated_index_guard(cand("s1"), "SHORT") is False


def _options_scanner():
    from tests.test_multi_signal_mixed_book import _scanner

    sc = _scanner()
    sc.user_lots = {i: 1 for i in sc.active_indices}
    return sc


def _s1_option(**kw):
    from tests.test_multi_signal_mixed_book import _cand

    c = _cand("NIFTY", inst_type="OPT", source_engine="s1", source_lane="S1", s1_guard_pct=10.0, **kw)
    c.lots = 1
    return c


@pytest.mark.parametrize("heavy,expected", [(False, 1), (True, 2)])
def test_s1_options_are_one_lot_on_a_cascade_and_two_on_a_heavy_trend(monkeypatch, heavy, expected):
    from config.settings import get_settings

    # Fixed 1-lot base: equal-risk sizing (2026-10-08) would change the base unit this
    # cascade/heavy-trend test multiplies; it has its own tests.
    monkeypatch.setattr(get_settings(), "risk_based_lots_enabled", False, raising=False)
    sc = _options_scanner()
    sc._heavy_trend_lot_scale_eligible = lambda *a, **k: heavy
    cand = _s1_option(one_way_cascade_entry=True, sizing_compression_state="UNRESTRICTED")
    assert sc._force_candidate_to_options(cand) is True
    assert cand.lots == expected


# ---------------------------------------------------------------- funds


def test_an_unaffordable_multi_lot_entry_is_trimmed_to_the_funded_lots(monkeypatch):
    from tests.test_entry_submission_guard_2026_08_25 import IST, _Broker, _manager

    monkeypatch.setattr("trading.order_retry.time.sleep", lambda _s: None)
    monkeypatch.setattr("engine.async_queue.execution_queue.submit",
                        lambda _name, func, *a, **k: func(*a, **k))

    class _TightBroker(_Broker):
        def cached_entry_margin_refusal(self, **kwargs):     # a cached refusal for 3 lots
            return {"ok": False, "limitation": "insufficient funds"}

        def preflight_entry_margin(self, **kwargs):
            qty = int(kwargs.get("qty") or 0)
            required = qty * 100.0
            if required > 2_500.0:
                return {"ok": False, "reservation_id": "", "basis": "KITE_ORDER_MARGIN",
                        "available": 2_500.0, "required": required, "reserved": 0.0,
                        "limitation": "insufficient funds"}
            return super().preflight_entry_margin(**kwargs)

    broker = _TightBroker()
    manager = _manager(broker)
    trade = manager.open_trade(
        instrument="NIFTY", timeframe="5min", direction="LONG", price=100.0, trailing_stop=95.0,
        lots=3, lot_size=10, grade="A", target=110.0, trading_symbol="NIFTYTESTFUT",
        symbol_token="TOKEN-NIFTY", inst_type="FUT", exec_type="A",
        entry_time=IST.localize(datetime(2026, 8, 25, 10, 0)),
    )
    assert trade is not None and trade.lots == 2
    assert broker.place_payloads[-1]["kwargs"]["qty"] == 20
    assert getattr(trade, "requested_lots") == 3


def test_a_single_lot_the_funds_cannot_cover_is_still_refused(monkeypatch):
    from tests.test_entry_submission_guard_2026_08_25 import IST, _Broker, _manager

    monkeypatch.setattr("trading.order_retry.time.sleep", lambda _s: None)
    monkeypatch.setattr("engine.async_queue.execution_queue.submit",
                        lambda _name, func, *a, **k: func(*a, **k))

    class _BrokeBroker(_Broker):
        def preflight_entry_margin(self, **kwargs):
            qty = int(kwargs.get("qty") or 0)
            return {"ok": False, "reservation_id": "", "basis": "KITE_ORDER_MARGIN",
                    "available": 50.0, "required": qty * 100.0, "reserved": 0.0,
                    "limitation": "insufficient funds"}

    broker = _BrokeBroker()
    manager = _manager(broker)
    trade = manager.open_trade(
        instrument="NIFTY", timeframe="5min", direction="LONG", price=100.0, trailing_stop=95.0,
        lots=2, lot_size=10, grade="A", target=110.0, trading_symbol="NIFTYTESTFUT",
        symbol_token="TOKEN-NIFTY", inst_type="FUT", exec_type="A",
        entry_time=IST.localize(datetime(2026, 8, 25, 10, 0)),
    )
    assert broker.place_calls == []
    assert trade.exit_reason == "ENTRY_MARGIN_PREFLIGHT"


def test_the_reentry_builder_puts_a_missed_s1_signal_on_s1_levels(monkeypatch):
    sc = _options_scanner()
    sc.mode = "REAL"
    sc._get_trade_regime = lambda c, default="UNKNOWN": "RANGING"
    sc.candles = SimpleNamespace(get_latest_price=lambda name: 23050.0)
    seed = _seed(lot_size=65, grade="C", market_regime="RANGING")
    cand = sc._build_missed_reentry_candidate(
        seed, {"type": "s1_strength", "signal_type": "SELL", "s1_score": 85.0, "chase": {}},
        datetime(2026, 9, 24, 13, 50),
    )
    assert cand is not None and s1_lane.is_s1(cand)
    assert cand.geometry_source == "missed_reentry.s1" and cand.s1_score == 85.0
    assert cand.spot_stop == pytest.approx(23050.0 + 23.1)


# ---------------------------------------------------------------- lots persist across restarts


def test_saved_lots_come_back_from_env_after_a_restart(monkeypatch):
    monkeypatch.setenv("USER_LOTS_BY_INDEX", '{"NIFTY": 3, "SENSEX": 2, "BOGUS": 9, "BANKNIFTY": 0}')
    saved = Settings().user_lots_by_index
    lots = {"NIFTY": 1, "BANKNIFTY": 1, "SENSEX": 1, "MIDCPNIFTY": 1}
    Scanner._restore_saved_lots(lots, saved)
    assert lots == {"NIFTY": 3, "BANKNIFTY": 1, "SENSEX": 2, "MIDCPNIFTY": 1}


# ---------------------------------------------------------------- unattended morning start


def test_a_switch_time_after_the_close_is_an_operator_test_and_is_honoured():
    import main
    from tests.test_premarket_real_mode_worker import _FakeScanner, _ist
    from tests.test_premarket_real_mode_worker import _settings as worker_settings

    default = main._auto_real_mode_attempt(_FakeScanner(), worker_settings(), _ist("2026-09-24 16:00:00"))
    assert default == (False, "after_live_end")
    scanner = _FakeScanner(auto_mode=True)
    ok, reason = main._auto_real_mode_attempt(
        scanner, worker_settings(auto_real_mode_premarket_switch_time="20:00:00"), _ist("2026-09-24 20:00:05"))
    assert (ok, reason) == (True, "switched_real_manual")
    assert scanner.mode == "REAL" and scanner.auto_mode is False


def test_the_morning_worker_follows_the_dashboard_toggle_without_a_restart(monkeypatch):
    import main
    from tests.test_premarket_real_mode_worker import _FakeScanner, _ist
    from tests.test_premarket_real_mode_worker import _settings as worker_settings

    class _Stop(Exception):
        pass

    class _Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return _ist("2026-09-25 09:05:00")

    settings = worker_settings(auto_real_mode_premarket_enabled=False)
    started, attempts, sleeps = [], [], []
    monkeypatch.setattr(main.threading, "Thread",
                        lambda target, daemon, name: SimpleNamespace(start=lambda: started.append(target)))
    monkeypatch.setattr(main, "datetime", _Clock)
    monkeypatch.setattr(main, "_auto_real_mode_attempt",
                        lambda sc, st, now: attempts.append(st.auto_real_mode_premarket_enabled)
                        or (True, "switched_real_manual"))

    def fake_sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) == 1:
            settings.auto_real_mode_premarket_enabled = True   # operator turns it on
        if len(sleeps) >= 2:
            raise _Stop

    monkeypatch.setattr(main.time, "sleep", fake_sleep)
    main.start_premarket_real_mode_worker(_FakeScanner(), settings)
    assert len(started) == 1                 # the worker runs even while disabled
    with pytest.raises(_Stop):
        started[0]()
    assert sleeps[0] == 60.0 and attempts == [True] and sleeps[1] <= 60.0


def test_a_funds_refusal_is_not_re_entered(monkeypatch):
    sc = _reentry_scanner(monkeypatch)
    sc._remember_missed_reentry_seed(_seed(status="REJECTED - INSUFFICIENT FUNDS"),
                                     reason="insufficient funds: 27,238 required", source="entry_margin_preflight")
    assert sc._missed_reentry_candidates.get("NIFTY", []) == []


def test_no_reentry_once_the_chart_has_closed_the_missed_signal(monkeypatch):
    sc = _reentry_scanner(monkeypatch)
    chart_signal = SimpleNamespace(source_engine="s1", timestamp=datetime(2026, 9, 24, 13, 40), signal_type="SELL",
                                   s1_exit_reason="Momentum Exit", s1_exit_time=datetime(2026, 9, 24, 13, 45),
                                   s1_exit_price=23080.0)
    sc.mtf._result_cache["NIFTY_5min"]["signals"] = [chart_signal]
    ok, reason, _ = sc._missed_reentry_support_context(_seed(), datetime(2026, 9, 24, 13, 50))
    assert ok is False and reason == "S1 chart already closed this signal"
    chart_signal.s1_exit_reason = ""          # still open on the chart: re-entry allowed
    assert sc._missed_reentry_support_context(_seed(), datetime(2026, 9, 24, 13, 50))[0] is True
