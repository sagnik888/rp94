"""The configured cap on correlated same-direction exposure must actually cap.

ut_correlated_core_limit says "Maximum simultaneous same-direction NIFTY/BANKNIFTY/SENSEX
exposures" and .env sets it to 2. _passes_correlated_index_guard took its limit from
_active_bucket_cap('CORE') instead — 6 normally, 9 on a rare burst. Three times the
configured number, silently, on the concentration control that matters most as size grows.

ut_correlated_allow_aplus_override describes the one escape: "permit a third core exposure
only when every setup is A+". It had no consumer either, so neither the limit nor its
escape existed.

NIFTY, BANKNIFTY and SENSEX are one correlated bucket here; MIDCPNIFTY is independent.
"""
from types import SimpleNamespace

import pytest

import scanner as scanner_module
from scanner import Scanner


def _cand(instrument="NIFTY", direction="LONG", grade="A", tf="5min"):
    return SimpleNamespace(
        instrument=instrument, direction=direction, grade=grade, timeframe=tf,
        inst_type="OPT", signal_timestamp=None, is_ghost=False,
    )


def _open(instrument, direction="LONG", grade="A", tf="5min"):
    return SimpleNamespace(
        instrument=instrument, direction=direction, grade=grade, timeframe=tf,
        inst_type="OPT", is_ghost=False,
    )


def _scanner(open_trades, bucket_cap=6):
    s = Scanner.__new__(Scanner)
    s.mode = "REAL"
    s.active_indices = ["NIFTY", "BANKNIFTY", "SENSEX", "MIDCPNIFTY"]
    s.trades = SimpleNamespace(open_trades={f"T{i}": t for i, t in enumerate(open_trades)})
    s._active_session_entry_candidates = lambda *a, **k: []
    s._active_bucket_cap = lambda bucket, candidate=None: bucket_cap
    s._record_live_reject = lambda *a, **k: None
    return s


def _settings(monkeypatch, **over):
    base = scanner_module.get_settings()
    monkeypatch.setattr(
        scanner_module, "get_settings",
        lambda: SimpleNamespace(**{**vars(base), **over}),
    )


# ── the limit binds ──────────────────────────────────────────────────────────

def test_two_same_direction_core_exposures_block_a_third(monkeypatch):
    _settings(monkeypatch, ut_correlated_core_limit=2, ut_correlated_allow_aplus_override=False)
    s = _scanner([_open("NIFTY"), _open("BANKNIFTY")])
    assert s._passes_correlated_index_guard(_cand("SENSEX"), "LONG") is False, (
        "a third same-direction core exposure passed a configured limit of 2 — the "
        "bucket cap of 6 was binding instead"
    )


def test_one_exposure_still_admits_a_second(monkeypatch):
    _settings(monkeypatch, ut_correlated_core_limit=2, ut_correlated_allow_aplus_override=False)
    s = _scanner([_open("NIFTY")])
    assert s._passes_correlated_index_guard(_cand("BANKNIFTY"), "LONG") is True


def test_the_opposite_direction_is_not_counted(monkeypatch):
    """This guard is about CONCENTRATION, not about holding both sides."""
    _settings(monkeypatch, ut_correlated_core_limit=2, ut_correlated_allow_aplus_override=False)
    s = _scanner([_open("NIFTY", "SHORT"), _open("BANKNIFTY", "SHORT")])
    assert s._passes_correlated_index_guard(_cand("SENSEX", "LONG"), "LONG") is True


def test_midcpnifty_is_an_independent_bucket(monkeypatch):
    _settings(monkeypatch, ut_correlated_core_limit=2, ut_correlated_allow_aplus_override=False)
    s = _scanner([_open("NIFTY"), _open("BANKNIFTY")])
    assert s._passes_correlated_index_guard(_cand("MIDCPNIFTY"), "LONG") is True


def test_the_bucket_cap_still_applies_when_it_is_tighter(monkeypatch):
    """The two limits measure different things; the tighter one wins."""
    _settings(monkeypatch, ut_correlated_core_limit=5, ut_correlated_allow_aplus_override=False)
    s = _scanner([_open("NIFTY")], bucket_cap=1)
    assert s._passes_correlated_index_guard(_cand("BANKNIFTY"), "LONG") is False


def test_turning_the_whole_guard_off_still_works(monkeypatch):
    _settings(monkeypatch, ut_correlated_index_guard=False, ut_correlated_core_limit=1)
    s = _scanner([_open("NIFTY"), _open("BANKNIFTY")])
    assert s._passes_correlated_index_guard(_cand("SENSEX"), "LONG") is True


def test_an_unset_limit_falls_back_to_the_bucket_cap(monkeypatch):
    """0 means "not configured", not "block everything"."""
    _settings(monkeypatch, ut_correlated_core_limit=0)
    s = _scanner([_open("NIFTY"), _open("BANKNIFTY")], bucket_cap=6)
    assert s._passes_correlated_index_guard(_cand("SENSEX"), "LONG") is True


# ── the A+ escape ────────────────────────────────────────────────────────────

def test_an_all_aplus_basket_earns_one_extra_exposure(monkeypatch):
    _settings(monkeypatch, ut_correlated_core_limit=2, ut_correlated_allow_aplus_override=True)
    s = _scanner([_open("NIFTY", grade="A+"), _open("BANKNIFTY", grade="A+")])
    assert s._passes_correlated_index_guard(_cand("SENSEX", grade="A+"), "LONG") is True


def test_one_weak_leg_closes_the_escape(monkeypatch):
    """EVERY setup has to be A+, including the ones already on."""
    _settings(monkeypatch, ut_correlated_core_limit=2, ut_correlated_allow_aplus_override=True)
    s = _scanner([_open("NIFTY", grade="A+"), _open("BANKNIFTY", grade="A")])
    assert s._passes_correlated_index_guard(_cand("SENSEX", grade="A+"), "LONG") is False


def test_a_weak_incoming_candidate_closes_it_too(monkeypatch):
    _settings(monkeypatch, ut_correlated_core_limit=2, ut_correlated_allow_aplus_override=True)
    s = _scanner([_open("NIFTY", grade="A+"), _open("BANKNIFTY", grade="A+")])
    assert s._passes_correlated_index_guard(_cand("SENSEX", grade="A"), "LONG") is False


def test_the_escape_grants_exactly_one_extra(monkeypatch):
    _settings(monkeypatch, ut_correlated_core_limit=2, ut_correlated_allow_aplus_override=True)
    s = _scanner([
        _open("NIFTY", grade="A+"),
        _open("BANKNIFTY", grade="A+"),
        _open("SENSEX", grade="A+"),
    ], bucket_cap=9)
    assert s._passes_correlated_index_guard(_cand("NIFTY", grade="A+", tf="15min"), "LONG") is False


# ── the helper itself ────────────────────────────────────────────────────────

def test_the_limit_helper_is_readable_on_a_partial_scanner(monkeypatch):
    """Partial scanners are a real pattern here."""
    _settings(monkeypatch, ut_correlated_core_limit=2, ut_correlated_allow_aplus_override=False)
    assert Scanner._correlated_core_limit(SimpleNamespace(), _cand(), []) == 2


def test_a_junk_limit_does_not_crash_the_guard(monkeypatch):
    _settings(monkeypatch, ut_correlated_core_limit="two")
    assert Scanner._correlated_core_limit(SimpleNamespace(), _cand(), []) == 0
