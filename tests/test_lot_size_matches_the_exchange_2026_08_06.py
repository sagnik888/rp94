"""Position size comes from a hand-maintained file; the exchange's own figure is ignored.

config/instruments.json holds a lot_size per index (NIFTY 65, BANKNIFTY 30, SENSEX 20,
MIDCPNIFTY 120) and scanner.py reads it — scanner.py:2826 and :9096 — to size every
position. The Kite contract master carries the authoritative figure, refreshed daily, and
_normalise_instrument parses it into row["lot_size"] where nothing reads it.

Exchanges revise index lot sizes periodically. When that happens the JSON goes stale
SILENTLY: every order is sized on the wrong multiple and the broker refuses it as an
invalid quantity — which reaches the operator as an ordinary rejection with no hint of the
cause, and this account has been chasing unexplained rejections.

Reported, never applied. Silently resizing live positions because a file disagrees with a
dump would be a worse failure than the one it fixes — so a mismatch BLOCKS REAL mode and
names both numbers, and a human decides.
"""
import json
import threading
from types import SimpleNamespace

import pytest

from data.kite_market_data import KiteDataSource


def _source(rows_by_exchange):
    src = KiteDataSource.__new__(KiteDataSource)
    src._lock = threading.RLock()
    src._instruments = rows_by_exchange
    src._by_symbol = {}
    src._loaded_on = None
    return src


def _opt(name, lot, itype="CE"):
    return {"name": name, "instrument_type": itype, "lot_size": lot, "tradingsymbol": f"{name}X"}


def test_the_exchange_lot_size_is_readable_at_all():
    src = _source({"NFO": [_opt("NIFTY", 75), _opt("NIFTY", 75, "PE")]})
    assert src.option_lot_size("NIFTY") == 75


def test_an_unknown_underlying_reports_nothing_rather_than_guessing():
    assert _source({"NFO": [_opt("NIFTY", 75)]}).option_lot_size("BANKNIFTY") is None
    assert _source({}).option_lot_size("NIFTY") is None
    assert _source({"NFO": []}).option_lot_size("") is None


def test_futures_rows_do_not_decide_an_option_lot_size():
    src = _source({"NFO": [
        {"name": "NIFTY", "instrument_type": "FUT", "lot_size": 50, "tradingsymbol": "F"},
        _opt("NIFTY", 75),
    ]})
    assert src.option_lot_size("NIFTY") == 75


def test_a_revision_in_progress_takes_the_dominant_size():
    """Mid-revision, two expiries can carry different sizes at once."""
    src = _source({"NFO": [_opt("NIFTY", 75), _opt("NIFTY", 75, "PE"), _opt("NIFTY", 65)]})
    assert src.option_lot_size("NIFTY") == 75


def test_both_exchanges_are_searched():
    """SENSEX options are BFO, not NFO."""
    src = _source({"NFO": [_opt("NIFTY", 75)], "BFO": [_opt("SENSEX", 20)]})
    assert src.option_lot_size("SENSEX") == 20


# ── the comparison ───────────────────────────────────────────────────────────

def test_a_matching_configuration_reports_no_mismatch():
    src = _source({"NFO": [_opt("NIFTY", 75)]})
    assert src.lot_size_mismatches({"NIFTY": 75}) == []


def test_a_stale_configuration_is_reported_with_both_numbers():
    src = _source({"NFO": [_opt("NIFTY", 75)]})
    assert src.lot_size_mismatches({"NIFTY": 65}) == [("NIFTY", 65, 75)]


def test_an_index_the_master_does_not_cover_is_not_accused():
    """No data is not evidence of a mismatch."""
    src = _source({"NFO": [_opt("NIFTY", 75)]})
    assert src.lot_size_mismatches({"BANKNIFTY": 30}) == []


def test_a_junk_configured_value_is_skipped_not_crashed():
    src = _source({"NFO": [_opt("NIFTY", 75)]})
    assert src.lot_size_mismatches({"NIFTY": "sixty-five"}) == []
    assert src.lot_size_mismatches({"NIFTY": 0}) == []


def test_every_index_is_checked_together():
    src = _source({
        "NFO": [_opt("NIFTY", 75), _opt("BANKNIFTY", 35), _opt("MIDCPNIFTY", 140)],
        "BFO": [_opt("SENSEX", 20)],
    })
    found = src.lot_size_mismatches(
        {"NIFTY": 65, "BANKNIFTY": 30, "SENSEX": 20, "MIDCPNIFTY": 120}
    )
    assert found == [("BANKNIFTY", 30, 35), ("MIDCPNIFTY", 120, 140), ("NIFTY", 65, 75)]
    assert not any(row[0] == "SENSEX" for row in found), "SENSEX matches and was accused"


# ── it has to reach the operator ─────────────────────────────────────────────

def test_a_mismatch_blocks_real_mode(tmp_path, monkeypatch):
    """Wrong-sized orders are refused by the broker anyway; better to say why first."""
    from config.settings import get_settings
    from engine.safe_mode import validate_real_mode_readiness

    monkeypatch.chdir(tmp_path)
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "instruments.json").write_text(
        json.dumps({"indices": {"NIFTY": {"lot_size": 65}}}), encoding="utf-8"
    )
    provider = SimpleNamespace(
        kite=_source({"NFO": [_opt("NIFTY", 75)]}),
        is_connected=True,
        get_source_health=lambda: {},
        is_market_holiday=lambda now: False,
    )
    result = validate_real_mode_readiness(get_settings(), data_provider=provider)
    text = " | ".join(result.get("blockers") or [])
    assert "NIFTY lot size is 65" in text
    assert "exchange uses 75" in text
    assert result["ok"] is False


def test_a_matching_configuration_does_not_block(tmp_path, monkeypatch):
    from config.settings import get_settings
    from engine.safe_mode import validate_real_mode_readiness

    monkeypatch.chdir(tmp_path)
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "instruments.json").write_text(
        json.dumps({"indices": {"NIFTY": {"lot_size": 75}}}), encoding="utf-8"
    )
    provider = SimpleNamespace(
        kite=_source({"NFO": [_opt("NIFTY", 75)]}),
        is_connected=True,
        get_source_health=lambda: {},
        is_market_holiday=lambda now: False,
    )
    blockers = validate_real_mode_readiness(get_settings(), data_provider=provider)["blockers"]
    assert not any("lot size" in b for b in blockers)


def test_no_master_yet_does_not_block_startup(tmp_path, monkeypatch):
    """Before the dump loads there is nothing to compare; that is not a fault."""
    from config.settings import get_settings
    from engine.safe_mode import validate_real_mode_readiness

    monkeypatch.chdir(tmp_path)
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "instruments.json").write_text(
        json.dumps({"indices": {"NIFTY": {"lot_size": 65}}}), encoding="utf-8"
    )
    provider = SimpleNamespace(
        kite=_source({}),
        is_connected=True,
        get_source_health=lambda: {},
        is_market_holiday=lambda now: False,
    )
    blockers = validate_real_mode_readiness(get_settings(), data_provider=provider)["blockers"]
    assert not any("lot size" in b for b in blockers)
