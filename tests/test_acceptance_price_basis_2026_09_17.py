"""An accepted row must be priced where it could trade, and its peak must include the fill.

Verified on the 2026-09-16 live session:

* ACCEPTED ``price`` was the contract LTP carried through the 30s stabilization window,
  not the executable quote at acceptance. NIFTY 12:18 was accepted at 193.15 while that
  minute's high was 192.75, the ask 190.00 and the broker fill 188.15; BANKNIFTY 12:14
  at 729.0 against an ask of 733.40 (fill 733.45); NIFTY 13:08 at 210.35 against a fill
  of 216.95. The seven Auto-OFF paper trades marked P&L from that stale price
  (+Rs.1,934.86 against an executable estimate of +Rs.1,298.61).
* NIFTY 13:08's EXIT booked pnl 732.5 (net of Rs.80) while peak_pnl said 607.75: the
  realised gross (229.45 - 216.95) x 65 = 812.5 exceeded the tracked high-water mark.
* pnl_path was NET on the three broker rows (Trade samples its net ``pnl``) and GROSS on
  the seven paper rows. The corpus convention is ``pnl`` net, ``pnl_path``/``peak_pnl``
  gross.

The real-order path must not move: ``_execution_levels`` must return the same levels for
a rebased advisory candidate, and the in-memory peak an exit guard reads is untouched.
"""
from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from engine.signal_processor import TradeCandidate
from intelligence.rtracker import PNL_BASIS_GROSS, PNL_BASIS_NET, RegimeTracker
from trading.trade_manager import IST, Trade, TradeManager


# --- fixtures -----------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _rebase_enabled(monkeypatch):
    """The rebase ships switched OFF (record-only); these tests pin the ON behaviour."""
    from config.settings import get_settings

    monkeypatch.setattr(get_settings(), "accepted_entry_rebase_enabled", True)


def _tracker(tmp_path) -> RegimeTracker:
    return RegimeTracker.from_settings(
        SimpleNamespace(
            rtracker_data_dir=str(tmp_path / "rtracker"),
            rtracker_enabled=True,
            rtracker_report_weeks=8,
            rtracker_retention_days=0,
            strategy_revision="ut1-current",
            strategy_direction_mode="LONG_SHORT",
        )
    )


def _rows(tracker) -> list[dict]:
    rows = []
    for path in sorted(Path(tracker.events_dir).glob("*.jsonl")):
        rows.extend(
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line
        )
    return rows


class _TouchData:
    """Market-data double: one contract touch quote, no network."""

    def __init__(self, quote):
        self.quote = quote
        self.calls = 0

    def get_contract_touch_quote(self, exchange, symbol, token, max_age_seconds=1.5):
        self.calls += 1
        return dict(self.quote) if self.quote is not None else {}


def _scanner(tmp_path, *, mode="REAL", quote=None):
    from scanner import Scanner

    scanner = Scanner.__new__(Scanner)
    scanner.mode = mode
    scanner.rtracker = _tracker(tmp_path)
    scanner.data = _TouchData(quote)
    scanner.trades = SimpleNamespace(open_trades={}, _terminal_callback_lifecycles=set())
    scanner._entry_intel_snapshot = lambda _candidate: {}
    scanner._reset_daily_counters = lambda: None
    scanner._record_signal_forensics = lambda *_args, **_kwargs: None
    scanner._log_opposing_exposure_telemetry = lambda *_args, **_kwargs: None
    scanner._notify_trade_event = lambda: None
    scanner._counted_entry_keys = set()
    scanner._trades_today = {}
    # Contract LTP cache is a non-blocking read in production; no fallback by default.
    scanner._get_live_premium_cached = lambda _c, allow_blocking=True: None
    scanner._get_live_fut_ltp_cached = lambda _c, allow_blocking=True: None
    return scanner


def _nifty_1218(**overrides) -> TradeCandidate:
    """NIFTY 12:18 LONG CE, carried at the stabilization-start LTP 193.15."""
    fields = dict(
        instrument="NIFTY",
        direction="LONG",
        price=193.15,
        stop=180.10788036,
        target=212.71317946000002,
        lots=1,
        lot_size=65,
        grade="A",
        confidence=0.82,
        timeframe="5min",
        inst_type="OPT",
        option_type="CE",
        atm_strike=23200.0,
        multiplier=0.5,
        trading_symbol="NIFTY2692223200CE",
        symbol_token="12345",
        rr=1.5,
        signal_timestamp=datetime(2026, 9, 16, 12, 15),
    )
    fields.update(overrides)
    candidate = TradeCandidate(**fields)
    candidate.status = "TRADE SIGNAL"
    candidate.accepted_by_gate = True
    candidate.initial_stop = float(candidate.stop)
    return candidate


def _accept(scanner, candidate, trade=None):
    """The acceptance seam both lanes share, after the policy touch was stamped."""
    scanner._mark_candidate_accepted_time(candidate, datetime.now(IST))
    scanner._record_accepted_entry(candidate, trade)


FRESH_ASK = {
    "bid": 189.6,
    "ask": 190.0,
    "ltp": 190.05,
    "age_seconds": 0.38,
    "usable": True,
    "source": "kite_ws",
    "reason": "ok",
}


# --- (a) acceptance-time executable entry basis -------------------------------------
def test_touch_quote_stamp_records_ltp_and_observation_clock(tmp_path):
    scanner = _scanner(tmp_path, quote=FRESH_ASK)
    candidate = _nifty_1218()

    before = time.time()
    scanner._candidate_execution_touch_quote(candidate)

    assert candidate.execution_ask_price == 190.0
    assert candidate.execution_bid_price == 189.6
    assert candidate.execution_touch_ltp == 190.05
    assert before <= candidate.execution_touch_checked_epoch <= time.time()
    observed = datetime.fromisoformat(candidate.execution_touch_observed_at)
    assert observed.tzinfo is None
    now_naive = datetime.now(IST).replace(tzinfo=None)
    assert 0.0 <= (now_naive - observed).total_seconds() < 5.0


def test_manual_acceptance_prices_the_advisory_row_at_the_acceptance_ask(tmp_path):
    scanner = _scanner(tmp_path, quote=FRESH_ASK)
    candidate = _nifty_1218()
    scanner._candidate_execution_touch_quote(candidate)  # post_lane_final policy touch

    _accept(scanner, candidate)

    assert candidate.price == 190.0
    assert candidate.stabilization_start_price == 193.15
    assert candidate.entry_price_source == "execution_touch_ask"
    scale = 190.0 / 193.15
    # Stop and target keep the graded premium-fraction geometry around the new basis.
    assert candidate.stop == pytest.approx(180.10788036 * scale)
    assert candidate.target == pytest.approx(212.71317946000002 * scale)
    assert candidate.initial_stop == pytest.approx(180.10788036 * scale)
    assert candidate.display_stop == pytest.approx(candidate.stop)
    assert candidate.display_target == pytest.approx(candidate.target)

    (row,) = [r for r in _rows(scanner.rtracker) if r["decision"] == "ACCEPTED"]
    assert row["price"] == 190.0
    assert row["stabilization_start_price"] == 193.15
    assert row["entry_price_source"] == "execution_touch_ask"
    quote = row["acceptance_quote"]
    assert quote["ltp"] == 190.05
    assert quote["bid"] == 189.6
    assert quote["ask"] == 190.0
    assert quote["source"] == "kite_ws"
    assert quote["observed_at"]
    assert quote["usable"] is True


def test_advisory_pnl_is_marked_from_the_executable_basis(tmp_path):
    scanner = _scanner(tmp_path, quote=FRESH_ASK)
    candidate = _nifty_1218()
    scanner._candidate_execution_touch_quote(candidate)
    _accept(scanner, candidate)

    # 12:20 exit mark 188.6: stale basis booked (188.6-193.15)*65-80 = -375.75.
    assert scanner._candidate_gross_pnl(candidate, 188.6) == pytest.approx((188.6 - 190.0) * 65)


@pytest.mark.parametrize(
    "overrides",
    [
        {},
        {"missed_signal_reentry": True},
        {"volume_spike_entry": True},
    ],
    ids=["graded_option", "missed_reentry_option", "volume_spike_option"],
)
def test_real_order_levels_are_unchanged_by_the_option_rebase(tmp_path, overrides):
    scanner = _scanner(tmp_path, quote=FRESH_ASK)
    candidate = _nifty_1218()
    for name, value in overrides.items():
        setattr(candidate, name, value)
    later_touch = dict(FRESH_ASK, bid=191.2, ask=191.5, age_seconds=0.2)

    scanner._candidate_execution_touch_quote(candidate)
    before = scanner._execution_levels(candidate, execution_touch_quote=later_touch)
    _accept(scanner, candidate)
    after = scanner._execution_levels(candidate, execution_touch_quote=later_touch)

    assert candidate.price == 190.0
    assert after == pytest.approx(before, rel=1e-12, abs=1e-9)


def test_real_order_levels_are_unchanged_by_the_futures_rebase(tmp_path):
    fut_touch = {
        "bid": 23251.0,
        "ask": 23252.0,
        "ltp": 23251.5,
        "age_seconds": 0.2,
        "usable": True,
        "source": "kite_ws",
    }
    scanner = _scanner(tmp_path, quote=fut_touch)
    candidate = _nifty_1218(
        direction="SHORT",
        price=23240.0,
        stop=23286.48,
        target=23170.28,
        inst_type="FUT",
        option_type="",
        atm_strike=0.0,
        multiplier=1.0,
        trading_symbol="NIFTY26SEPFUT",
    )
    later_touch = dict(fut_touch, bid=23260.0, ask=23261.0)

    scanner._candidate_execution_touch_quote(candidate)
    before = scanner._execution_levels(candidate, execution_touch_quote=later_touch)
    _accept(scanner, candidate)
    after = scanner._execution_levels(candidate, execution_touch_quote=later_touch)

    # A futures SHORT is sold at the bid; absolute risk/reward distances are preserved.
    assert candidate.price == 23251.0
    assert candidate.entry_price_source == "execution_touch_bid"
    assert candidate.stop == pytest.approx(23286.48 + 11.0)
    assert candidate.target == pytest.approx(23170.28 + 11.0)
    assert after == pytest.approx(before, rel=1e-12, abs=1e-6)


def test_unusable_touch_falls_back_to_the_contract_ltp(tmp_path):
    scanner = _scanner(tmp_path, quote=dict(FRESH_ASK, usable=False, reason="wide_spread"))
    scanner._get_live_premium_cached = lambda _c, allow_blocking=True: (
        191.1 if allow_blocking is False else pytest.fail("acceptance must not block")
    )
    candidate = _nifty_1218()
    scanner._candidate_execution_touch_quote(candidate)

    _accept(scanner, candidate)

    assert candidate.price == 191.1
    assert candidate.entry_price_source == "contract_ltp"
    assert candidate.stabilization_start_price == 193.15
    (row,) = [r for r in _rows(scanner.rtracker) if r["decision"] == "ACCEPTED"]
    assert row["price"] == 191.1
    assert row["acceptance_quote"]["usable"] is False


def test_a_touch_stamped_too_long_before_acceptance_is_not_the_acceptance_quote(tmp_path):
    scanner = _scanner(tmp_path, quote=FRESH_ASK)
    candidate = _nifty_1218()
    scanner._candidate_execution_touch_quote(candidate)
    candidate.execution_touch_checked_epoch = time.time() - 60.0

    _accept(scanner, candidate)

    # No fresh touch and no cached LTP: keep the carried price, say so.
    assert candidate.price == 193.15
    assert candidate.stabilization_start_price == 193.15
    assert candidate.entry_price_source == "stabilization_start"


def test_broker_acknowledged_entry_uses_the_submitted_trade_price(tmp_path):
    scanner = _scanner(tmp_path, quote=None)
    candidate = _nifty_1218(
        instrument="BANKNIFTY",
        price=729.0,
        stop=698.932276165,
        target=793.04425176855,
        lot_size=30,
        trading_symbol="BANKNIFTY26SEP56200CE",
        signal_timestamp=datetime(2026, 9, 16, 12, 10),
    )
    trade = SimpleNamespace(id="T-BNF-1214", entry_price=733.4, is_ghost=False)

    _accept(scanner, candidate, trade)

    assert candidate.price == 733.4
    assert candidate.stabilization_start_price == 729.0
    assert candidate.entry_price_source == "trade_entry_price"
    (row,) = [r for r in _rows(scanner.rtracker) if r["decision"] == "ACCEPTED"]
    assert row["price"] == 733.4
    assert row["stabilization_start_price"] == 729.0


def test_the_basis_is_set_once_per_acceptance(tmp_path):
    scanner = _scanner(tmp_path, quote=FRESH_ASK)
    candidate = _nifty_1218()
    scanner._candidate_execution_touch_quote(candidate)
    _accept(scanner, candidate)

    scanner.data.quote = dict(FRESH_ASK, bid=199.0, ask=199.5)
    scanner._candidate_execution_touch_quote(candidate)
    scanner._record_accepted_entry(candidate)

    assert candidate.price == 190.0
    assert candidate.stabilization_start_price == 193.15


def test_historical_replay_prices_are_not_rebased(tmp_path):
    scanner = _scanner(tmp_path, mode="HISTORICAL", quote=FRESH_ASK)
    candidate = _nifty_1218()
    scanner._candidate_execution_touch_quote(candidate)

    _accept(scanner, candidate)

    assert candidate.price == 193.15
    assert candidate.stop == 180.10788036
    assert not hasattr(candidate, "stabilization_start_price")


# --- (b) peak includes the realised fill; pnl_path is gross on every lane -------------
@pytest.fixture
def pnl_path_settings(monkeypatch):
    import config.settings as settings_module

    pinned = SimpleNamespace(
        pnl_path_logging_enabled=True,
        pnl_path_min_interval_seconds=0.0,
        pnl_path_min_move_rs=0.0,
        pnl_path_max_samples=400,
    )
    monkeypatch.setattr(settings_module, "get_settings", lambda: pinned)
    return pinned


def _nifty_1308_trade() -> Trade:
    return Trade(
        id="T-NIFTY-1308",
        instrument="NIFTY",
        timeframe="15min",
        direction="LONG",
        entry_price=216.95,
        entry_time=IST.localize(datetime(2026, 9, 16, 13, 8, 58)),
        trailing_stop=203.3,
        current_stop=203.3,
        lots=1,
        lot_size=65,
        grade="A",
        inst_type="OPT",
        atm_strike=23200.0,
        option_type="CE",
        status="OPEN",
        trading_symbol="NIFTY2692223200CE",
        symbol_token="12345",
    )


def test_trade_pnl_path_is_sampled_gross(pnl_path_settings):
    trade = _nifty_1308_trade()
    manager = TradeManager.__new__(TradeManager)

    trade.current_price = 226.30
    trade.pnl = trade.unrealized_pnl  # what update_trade sets just before sampling
    manager._sample_pnl_path(trade)

    assert trade.pnl < (226.30 - 216.95) * 65, "trade.pnl is net of charges"
    assert trade.pnl_path[-1][1] == pytest.approx((226.30 - 216.95) * 65)
    assert trade.pnl_path_basis == PNL_BASIS_GROSS


def test_unrealized_gross_is_the_gross_half_of_unrealized_pnl():
    from config.settings import get_settings
    from engine.trade_accounting import estimate_trade_charges

    trade = _nifty_1308_trade()
    trade.current_price = 226.30
    charges = estimate_trade_charges(
        trade.entry_price, trade.current_price, trade.pnl_quantity, trade.inst_type,
        get_settings(), trade.instrument_multiplier, instrument=trade.instrument,
    )
    assert trade.unrealized_gross_pnl == pytest.approx((226.30 - 216.95) * 65)
    assert trade.unrealized_gross_pnl - charges == pytest.approx(trade.unrealized_pnl)
    trade.status = "CLOSED"
    assert trade.unrealized_gross_pnl == 0.0


def test_broker_exit_row_folds_the_realised_fill_into_peak_and_is_gross(
    tmp_path, pnl_path_settings
):
    scanner = _scanner(tmp_path)
    trade = _nifty_1308_trade()
    manager = TradeManager.__new__(TradeManager)
    for mark in (215.30, 219.65, 226.30):
        trade.current_price = mark
        trade.pnl = trade.unrealized_pnl
        manager._sample_pnl_path(trade)
    # What the scanner's executable high-water tracker left on the Trade.
    trade.peak_pnl = 607.75
    trade.status = "CLOSED"
    trade.exit_price = 229.45
    trade.exit_time = IST.localize(datetime(2026, 9, 16, 13, 9, 27))
    trade.exit_reason = "TARGET_HIT"
    trade.pnl = 732.5
    trade.charges = 80.0
    trade.calibration_outcome_recorded = True

    scanner._record_trade_manager_terminal(trade)

    (row,) = [r for r in _rows(scanner.rtracker) if r["decision"] == "EXIT"]
    assert row["pnl"] == 732.5
    assert row["pnl_basis"] == PNL_BASIS_NET
    assert row["round_trip_cost"] == 80.0
    assert row["realized_gross_pnl"] == pytest.approx(812.5)
    assert row["peak_pnl"] == pytest.approx(812.5)
    assert row["tracked_peak_pnl"] == pytest.approx(607.75)
    assert row["peak_pnl"] >= row["realized_gross_pnl"]
    assert row["pnl_path_basis"] == PNL_BASIS_GROSS
    assert [p for _, p in row["pnl_path"]] == pytest.approx(
        [(215.30 - 216.95) * 65, (219.65 - 216.95) * 65, (226.30 - 216.95) * 65]
    )
    # Recording only: the peak an exit guard reads on the live object is unchanged.
    assert trade.peak_pnl == 607.75


def _paper_exit(**overrides):
    fields = dict(
        action="EXIT",
        status="EXIT SIGNAL",
        instrument="SENSEX",
        timeframe="15min",
        direction="LONG",
        signal_timestamp=datetime(2026, 9, 16, 10, 30),
        entry_timestamp=datetime(2026, 9, 16, 10, 42, 8),
        exit_timestamp=datetime(2026, 9, 16, 10, 50, 38),
        price=760.0,
        exit_price=773.3,
        current_price=773.3,
        pnl=186.0,
        peak_pnl=700.0,
        pnl_path=[(1.04, -257.0), (24.26, 700.0), (510.59, 100.0)],
        inst_type="OPT",
        option_type="CE",
        lots=1,
        lot_size=20,
    )
    fields.update(overrides)
    return SimpleNamespace(**fields)


def test_paper_exit_keeps_a_higher_tracked_peak(tmp_path):
    scanner = _scanner(tmp_path)
    exit_row = _paper_exit()

    scanner._rtracker_record_candidate(exit_row, decision="EXIT", reason="SL", source="session_candidate")

    (row,) = [r for r in _rows(scanner.rtracker) if r["decision"] == "EXIT"]
    assert row["realized_gross_pnl"] == pytest.approx(266.0)
    assert row["peak_pnl"] == 700.0
    assert row["tracked_peak_pnl"] == 700.0
    assert row["pnl_path_basis"] == PNL_BASIS_GROSS
    assert exit_row.peak_pnl == 700.0


def test_a_losing_exit_that_never_went_green_keeps_peak_zero(tmp_path):
    scanner = _scanner(tmp_path)
    exit_row = _paper_exit(
        instrument="MIDCPNIFTY",
        direction="SHORT",
        option_type="PE",
        price=178.45,
        exit_price=167.75,
        current_price=167.75,
        pnl=-1364.0,
        peak_pnl=0.0,
        pnl_path=[(0.24, 0.0), (19.85, -1284.0)],
        lot_size=120,
    )

    scanner._rtracker_record_candidate(exit_row, decision="EXIT", reason="RUPEE_LOSS_BAND_EXIT", source="session_candidate")

    (row,) = [r for r in _rows(scanner.rtracker) if r["decision"] == "EXIT"]
    assert row["realized_gross_pnl"] == pytest.approx(-1284.0)
    assert row["peak_pnl"] == 0.0

# --- record-only default (2026-09-17 review) ----------------------------------------
# Moving the paper row moves the gate inputs paper exits feed (loss streak, cooldown
# clock, reversion window, manual Execute refusal) and offsets spot-mapped trailing
# because entry_spot is not re-anchored. Until that is done the default only records.
def test_default_settings_record_the_executable_basis_without_moving_the_row(
    tmp_path, monkeypatch
):
    from config.settings import Settings, get_settings

    assert Settings.model_fields["accepted_entry_rebase_enabled"].default is False
    monkeypatch.setattr(get_settings(), "accepted_entry_rebase_enabled", False)
    scanner = _scanner(tmp_path, quote=FRESH_ASK)
    candidate = _nifty_1218()
    scanner._candidate_execution_touch_quote(candidate)

    _accept(scanner, candidate)

    assert candidate.price == 193.15
    assert candidate.stop == 180.10788036
    assert candidate.target == 212.71317946000002
    assert candidate.initial_stop == 180.10788036
    assert candidate.stabilization_start_price == 193.15
    assert candidate.entry_price_source == "stabilization_start"
    assert candidate.acceptance_quote["executable_price"] == 190.0
    assert candidate.acceptance_quote["executable_source"] == "execution_touch_ask"

    (row,) = [r for r in _rows(scanner.rtracker) if r["decision"] == "ACCEPTED"]
    assert row["price"] == 193.15
    assert row["entry_price_source"] == "stabilization_start"
    assert row["acceptance_quote"]["executable_price"] == 190.0
    assert row["acceptance_quote"]["executable_source"] == "execution_touch_ask"
    assert row["acceptance_quote"]["ask"] == 190.0


def test_enabled_rebase_also_records_the_executable_basis(tmp_path):
    scanner = _scanner(tmp_path, quote=FRESH_ASK)
    candidate = _nifty_1218()
    scanner._candidate_execution_touch_quote(candidate)

    _accept(scanner, candidate)

    assert candidate.price == 190.0
    assert candidate.acceptance_quote["executable_price"] == 190.0
    assert candidate.acceptance_quote["executable_source"] == "execution_touch_ask"
