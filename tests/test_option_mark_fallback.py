from types import SimpleNamespace
from datetime import timedelta
import datetime as dt

from scanner import Scanner, IST
from config.settings import get_settings


def _mark(trade, spot):
    # Include the mode-aware expiry adapter used by the actual Scanner.
    stub = SimpleNamespace(mode="REAL", expiry=SimpleNamespace(get_dte=lambda inst, d: 5))
    stub._days_to_expiry_for = Scanner._days_to_expiry_for.__get__(stub, Scanner)
    return Scanner._synthetic_option_mark.__get__(stub, Scanner)(trade, spot)


def test_synthetic_put_premium_rises_when_spot_falls():
    # No entry_time -> theta skipped -> delta-only behaviour (unchanged).
    trade = SimpleNamespace(entry_spot=57500.0, entry_price=800.0, instrument_multiplier=-0.5)
    assert _mark(trade, 57400.0) == 850.0


def test_synthetic_call_premium_rises_when_spot_rises():
    trade = SimpleNamespace(entry_spot=24000.0, entry_price=200.0, instrument_multiplier=0.5)
    assert _mark(trade, 24100.0) == 250.0


def test_synthetic_mark_applies_theta_decay_over_time():
    # With an entry_time, premium now erodes over the holding period (C1 fix):
    # flat spot -> mark falls below entry; held longer -> falls further.
    def trade(hours):
        return SimpleNamespace(entry_spot=24000.0, entry_price=140.0, instrument_multiplier=0.5,
                               instrument="NIFTY", entry_time=dt.datetime.now(IST) - timedelta(hours=hours))

    flat_3h = _mark(trade(3), 24000.0)
    flat_6h = _mark(trade(6), 24000.0)
    assert flat_3h < 140.0          # theta decayed (was exactly 140 before the fix)
    assert flat_6h < flat_3h        # longer hold -> more decay


def test_synthetic_mark_theta_halved_when_in_profit():
    # Decay is gentler when the premium is moving in our favour.
    def trade(hours):
        return SimpleNamespace(entry_spot=24000.0, entry_price=140.0, instrument_multiplier=0.5,
                               instrument="NIFTY", entry_time=dt.datetime.now(IST) - timedelta(hours=hours))
    # +100 spot * 0.5 delta = +50 gross; minus only half the theta.
    favour = _mark(trade(3), 24100.0)
    assert 188.0 < favour < 190.0


def _decision_scanner(quote_age):
    stub = SimpleNamespace(
        mode="REAL",
        expiry=SimpleNamespace(get_dte=lambda inst, d: 5),
        candles=SimpleNamespace(get_latest_price=lambda _instrument: 24100.0),
        log_event=lambda *_args, **_kwargs: None,
    )
    stub._option_premium_quote_plausible = Scanner._option_premium_quote_plausible.__get__(stub, Scanner)
    stub._days_to_expiry_for = Scanner._days_to_expiry_for.__get__(stub, Scanner)
    stub._sanitize_option_mark = Scanner._sanitize_option_mark.__get__(stub, Scanner)
    stub._synthetic_option_mark = Scanner._synthetic_option_mark.__get__(stub, Scanner)
    stub._active_option_decision_mark = Scanner._active_option_decision_mark.__get__(stub, Scanner)
    stub._contract_quote_age_seconds = lambda _trade: quote_age
    stub._setting_enabled = Scanner._setting_enabled
    return stub


def _decision_trade():
    return SimpleNamespace(
        id="T-OPT",
        instrument="NIFTY",
        timeframe="5min",
        inst_type="OPT",
        trading_symbol="NIFTY30JUN24100CE",
        symbol_token="12345",
        option_type="CE",
        atm_strike=24100.0,
        entry_spot=24000.0,
        entry_price=200.0,
        price=200.0,
        target=300.0,
        instrument_multiplier=0.5,
        entry_time=None,
    )


def test_active_option_decision_mark_uses_fresh_contract_ltp():
    settings = get_settings()
    old_enabled = settings.option_internal_mark_on_stale_quote
    old_age = settings.option_internal_mark_quote_age_seconds
    settings.option_internal_mark_on_stale_quote = True
    settings.option_internal_mark_quote_age_seconds = 3.0
    try:
        mark, source, verified = _decision_scanner(quote_age=1.0)._active_option_decision_mark(
            _decision_trade(),
            24100.0,
            real_premium=210.0,
        )
        assert mark == 210.0
        assert source == "broker_contract_ltp"
        assert verified is True
    finally:
        settings.option_internal_mark_on_stale_quote = old_enabled
        settings.option_internal_mark_quote_age_seconds = old_age


def test_active_option_decision_mark_uses_synthetic_when_contract_ltp_is_stale():
    settings = get_settings()
    old_enabled = settings.option_internal_mark_on_stale_quote
    old_age = settings.option_internal_mark_quote_age_seconds
    old_points = settings.option_internal_mark_min_diff_points
    old_pct = settings.option_internal_mark_min_diff_pct
    settings.option_internal_mark_on_stale_quote = True
    settings.option_internal_mark_quote_age_seconds = 3.0
    settings.option_internal_mark_min_diff_points = 1.0
    settings.option_internal_mark_min_diff_pct = 0.5
    try:
        mark, source, verified = _decision_scanner(quote_age=8.0)._active_option_decision_mark(
            _decision_trade(),
            24100.0,
            real_premium=210.0,
        )
        assert mark == 250.0
        assert source == "synthetic_option_mark_stale_contract_ltp"
        assert verified is False
    finally:
        settings.option_internal_mark_on_stale_quote = old_enabled
        settings.option_internal_mark_quote_age_seconds = old_age
        settings.option_internal_mark_min_diff_points = old_points
        settings.option_internal_mark_min_diff_pct = old_pct


def test_active_option_decision_mark_keeps_stale_contract_ltp_when_close_to_synthetic():
    settings = get_settings()
    old_enabled = settings.option_internal_mark_on_stale_quote
    old_age = settings.option_internal_mark_quote_age_seconds
    old_points = settings.option_internal_mark_min_diff_points
    old_pct = settings.option_internal_mark_min_diff_pct
    settings.option_internal_mark_on_stale_quote = True
    settings.option_internal_mark_quote_age_seconds = 3.0
    settings.option_internal_mark_min_diff_points = 2.0
    settings.option_internal_mark_min_diff_pct = 0.5
    try:
        mark, source, verified = _decision_scanner(quote_age=8.0)._active_option_decision_mark(
            _decision_trade(),
            24100.0,
            real_premium=249.25,
        )
        assert mark == 249.25
        assert source == "broker_contract_ltp_stale_but_close"
        assert verified is True
    finally:
        settings.option_internal_mark_on_stale_quote = old_enabled
        settings.option_internal_mark_quote_age_seconds = old_age
        settings.option_internal_mark_min_diff_points = old_points
        settings.option_internal_mark_min_diff_pct = old_pct
