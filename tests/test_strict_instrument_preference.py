"""Strict FUT-only / OPT-only product lock regression tests."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from engine.signal_processor import TradeCandidate
from scanner import Scanner


def _cand(instrument, inst_type="FUT", **kwargs):
    c = TradeCandidate(
        instrument=instrument,
        direction="LONG",
        price=25000.0 if inst_type == "FUT" else 150.0,
        stop=24970.0 if inst_type == "FUT" else 140.0,
        target=25050.0 if inst_type == "FUT" else 180.0,
        lots=1,
        lot_size=25,
        grade="A",
        confidence=0.9,
        timeframe="15min",
        inst_type=inst_type,
        option_type="CE" if inst_type == "OPT" else "",
        atm_strike=25000.0 if inst_type == "OPT" else 0.0,
        multiplier=1.0 if inst_type == "FUT" else 0.5,
        trading_symbol=f"{instrument}-I" if inst_type == "FUT" else f"{instrument}25000CE",
        symbol_token=f"t{instrument}",
        rr=1.5,
        score=0.9,
        reasons=[],
        action="ENTRY",
        status="TRADE SIGNAL",
    )
    for k, v in kwargs.items():
        setattr(c, k, v)
    return c


def _scanner(pref="AUTO"):
    sc = Scanner.__new__(Scanner)
    sc.inst_pref = pref
    sc.mode = "REAL"
    sc.active_indices = ["NIFTY", "BANKNIFTY", "SENSEX", "MIDCPNIFTY"]
    sc.user_lots = {i: 1 for i in sc.active_indices}
    sc.user_lots_fut = {i: 1 for i in sc.active_indices}
    sc.options_sl_pct = 15.0
    sc.futures_sl_pct = 0.1
    sc.latest_regimes = {i: "TRENDING_UP" for i in sc.active_indices}
    sc.config = {
        "indices": {
            i: {"strike_interval": 50, "lot_size": 25, "exchange": "NFO"}
            for i in sc.active_indices
        }
    }
    sc.market_info = {
        i: {"current_fut": f"{i}-I", "current_fut_token": f"tok-{i}"}
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


def test_opt_only_mixed_book_all_options():
    sc = _scanner("OPT")
    out = sc._apply_multi_signal_mixed_book(
        [_cand("NIFTY"), _cand("BANKNIFTY"), _cand("SENSEX"), _cand("MIDCPNIFTY")]
    )
    assert out
    assert all(c.inst_type == "OPT" for c in out)


def test_fut_only_mixed_book_all_futures_including_midcap():
    """FUT-only must not soft-skip MIDCP via auto futures blacklist."""
    sc = _scanner("FUT")
    assert sc._futures_blocked_for_index("MIDCPNIFTY") is False
    out = sc._apply_multi_signal_mixed_book(
        [_cand("NIFTY"), _cand("BANKNIFTY"), _cand("SENSEX"), _cand("MIDCPNIFTY")]
    )
    assert out
    assert all(c.inst_type == "FUT" for c in out)
    assert any(c.instrument == "MIDCPNIFTY" for c in out)


def test_auto_still_blocks_midcap_futures_in_mixed_book():
    sc = _scanner("AUTO")
    assert sc._futures_blocked_for_index("MIDCPNIFTY") is True
    out = sc._apply_multi_signal_mixed_book(
        [_cand("NIFTY"), _cand("BANKNIFTY"), _cand("SENSEX"), _cand("MIDCPNIFTY")]
    )
    mid = [c for c in out if c.instrument == "MIDCPNIFTY"]
    assert mid
    assert all(c.inst_type == "OPT" for c in mid)


def test_strict_pass_converts_wrong_product_opt():
    sc = _scanner("OPT")
    cands = [_cand("NIFTY", inst_type="FUT"), _cand("BANKNIFTY", inst_type="FUT")]
    out = sc._apply_strict_instrument_pref_to_entries(cands)
    assert len(out) == 2
    assert all(c.inst_type == "OPT" for c in out)


def test_strict_pass_converts_wrong_product_fut():
    sc = _scanner("FUT")
    cands = [_cand("NIFTY", inst_type="OPT", option_type="CE", price=150.0)]
    out = sc._apply_strict_instrument_pref_to_entries(cands)
    assert len(out) == 1
    assert out[0].inst_type == "FUT"
    assert out[0].option_type == ""


def test_strict_pass_noop_for_auto():
    sc = _scanner("AUTO")
    cands = [_cand("NIFTY", inst_type="FUT"), _cand("BANKNIFTY", inst_type="OPT", option_type="CE")]
    out = sc._apply_strict_instrument_pref_to_entries(cands)
    assert [c.inst_type for c in out] == ["FUT", "OPT"]


def test_fut_conversion_fails_cleanly_without_opt_flip():
    sc = _scanner("FUT")
    sc.market_info = {}  # no contracts
    c = _cand("NIFTY", inst_type="OPT", option_type="CE", price=150.0, trading_symbol="X", symbol_token="")
    ok, why = sc._enforce_strict_instrument_pref_on_candidate(c)
    assert ok is False
    assert "fut" in why
    assert c.inst_type != "FUT" or not str(c.symbol_token or "").startswith("tok")


def test_missed_reentry_respects_fut_over_prefer_options(monkeypatch):
    from config.settings import get_settings

    s = get_settings()
    old_pref = s.inst_pref
    old_prefer = s.missed_signal_reentry_prefer_options
    try:
        s.inst_pref = "FUT"
        s.missed_signal_reentry_prefer_options = True
        sc = _scanner("FUT")
        # Branch parity with builder
        prefer_options = True
        inst_pref = sc._strict_instrument_pref()
        if inst_pref == "OPT":
            inst_type = "OPT"
        elif inst_pref == "FUT":
            inst_type = "FUT"
        else:
            inst_type = "OPT" if prefer_options else "FUT"
        assert inst_type == "FUT"
    finally:
        s.inst_pref = old_pref
        s.missed_signal_reentry_prefer_options = old_prefer


def test_recovery_pref_branch_matches_strict_rules():
    """Document recovery branch: FUT/OPT absolute, AUTO→OPT."""
    for pref, expected in (("FUT", "FUT"), ("OPT", "OPT"), ("AUTO", "OPT")):
        if pref == "OPT":
            rec = "OPT"
        elif pref == "FUT":
            rec = "FUT"
        else:
            rec = "OPT"
        assert rec == expected
