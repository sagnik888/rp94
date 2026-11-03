"""An optional provider must not reset or block the preferred broker lane."""
import threading
from types import SimpleNamespace as NS
from unittest.mock import Mock

import pandas as pd
import pytest


@pytest.mark.parametrize("backup", [True, False])
def test_fyers_token_refresh_only_recalculates_legacy_route(backup):
    from scanner import Scanner
    scanner = Scanner.__new__(Scanner)
    scanner.data = NS(update_fyers_token_from_auth_code=lambda code: {"status": "ok"},
                      _fyers_backup_enabled=lambda: backup)
    scanner.log_event = Mock()
    scanner.queue_full_recalculation = Mock()
    assert scanner.update_fyers_token("not-a-real-code")["status"] == "ok"
    assert scanner.queue_full_recalculation.call_count == (0 if backup else 1)


@pytest.mark.parametrize("code", [-8, -15, -16, -17, -300])
def test_invalid_fyers_session_is_quarantined_without_touching_kite(tmp_path, code):
    from tests.test_three_broker_priority_2026_10_23 import three
    source = three(tmp_path)
    source.kite = kite = NS(is_connected=True)
    source.is_connected = True
    source.fyers = NS(optionchain=Mock())
    source._call_bounded = Mock(return_value=(True, {"s": "error", "code": code, "message": "invalid token"}))
    source._call_fyers_bounded("optionchain", data={}, label="backup test")
    source._call_fyers_bounded("optionchain", data={}, label="backup test")
    assert source._call_bounded.call_count == 1
    assert source._fyers_auth_required
    assert source.kite is kite and source.kite.is_connected
    from data.market_source_health import build_source_health
    import time
    source._last_kite_ws_tick = time.time()
    assert build_source_health(source)["all_brokers_unavailable"] is False


def test_backup_chain_failure_does_not_reload_global_fyers_client(tmp_path):
    from tests.test_three_broker_priority_2026_10_23 import three
    source = three(tmp_path)
    source._load_fyers_token = Mock()
    for _ in range(30):
        source._maybe_self_heal_fyers("backup endpoint error")
    source._load_fyers_token.assert_not_called()


def test_old_request_cannot_quarantine_a_new_fyers_login(tmp_path):
    from tests.test_three_broker_priority_2026_10_23 import three
    source = three(tmp_path)
    source.fyers = NS(optionchain=Mock())
    replacement = NS(optionchain=Mock())
    def late_error(*args, **kwargs):
        source.fyers = replacement
        return True, {"s": "error", "code": -16}
    source._call_bounded = late_error
    source._call_fyers_bounded("optionchain", data={}, label="test")
    assert source.fyers is replacement
    assert source._fyers_auth_required is False


def test_optional_day_oi_never_delays_preferred_chain(tmp_path, monkeypatch):
    from tests.test_kite_angel_replacement_2026_10_23 import chain_provider, chain_frame
    source = chain_provider(tmp_path)
    source._market_data_route = "angel_fyers"
    source.fyers = object()
    source.kite.get_option_chain = lambda *a, **k: chain_frame()
    started, release = threading.Event(), threading.Event()
    archived = []
    source._archive_option_chain = lambda index, chain, timestamp, quality: archived.append((chain.copy(), quality))
    def slow(index, expiry, chain, *, allow_fetch=True):
        result = chain.copy()
        result["call_day_oi_change"] = result["put_day_oi_change"] = float("nan")
        result["day_oi_change_provider"] = "unavailable"
        result["day_oi_change_received_at"] = float("nan")
        if allow_fetch:
            started.set()
            assert release.wait(3.)
            result["call_day_oi_change"] = -5.
            result["put_day_oi_change"] = 2.
            result["day_oi_change_provider"] = "fyers"
            result["day_oi_change_received_at"] = 123.
        return result
    monkeypatch.setattr(source, "_supplement_reported_day_oi", slow)
    result = []
    caller = threading.Thread(target=lambda: result.append(source.get_option_chain("NIFTY")))
    caller.start()
    try:
        assert started.wait(2.)
        caller.join(.2)
        assert not caller.is_alive(), "A healthy preferred chain waits on optional FYERS data"
        assert source._option_chain_meta["NIFTY"]["source"] == "kite"
        assert source._option_chain_meta["NIFTY"]["auto_certified"]
    finally:
        release.set()
        caller.join(3.)
        worker = getattr(source, "_replacement_day_oi_thread", None)
        if worker:
            worker.join(3.)
    cached = source._opt_chain_cache["opt_chain_NIFTY"]
    assert cached["call_day_oi_change"].eq(-5.).all()
    assert cached["call_ltp"].equals(result[0]["call_ltp"])
    assert archived[-1][0]["call_day_oi_change"].eq(-5.).all()
    assert archived[-1][1]["capability_update"] == "reported_day_oi"


def test_fyers_input_error_cooldown_does_not_block_other_index(tmp_path):
    from tests.test_three_broker_priority_2026_10_23 import three
    source = three(tmp_path)
    source.fyers = NS(optionchain=Mock())
    source._call_bounded = Mock(side_effect=[(True, {"s": "error", "code": -99}), (True, {"s": "ok"})])
    first = dict(symbol="invalid")
    source._call_fyers_bounded("optionchain", data=first, label="test")
    assert source._call_fyers_bounded("optionchain", data=first, label="test") == (False, None)
    assert source._call_fyers_bounded("optionchain", data=dict(symbol="NIFTY"), label="test")[0]
    assert source._call_bounded.call_count == 2


def test_fyers_rate_limit_is_shared_without_affecting_preferred_probe(tmp_path):
    from tests.test_kite_angel_replacement_2026_10_23 import chain_provider, chain_frame
    source = chain_provider(tmp_path)
    source._market_data_route = "angel_fyers"
    source.fyers = NS(optionchain=Mock())
    source._call_bounded = Mock(return_value=(True, {"s": "error", "code": -360}))
    source._call_fyers_bounded("optionchain", data=dict(symbol="NIFTY"), label="test")
    assert source._call_fyers_bounded("optionchain", data=dict(symbol="SENSEX"), label="test") == (False, None)
    source.kite.get_option_chain = lambda *args, **kwargs: chain_frame()
    assert not source.get_option_chain("NIFTY").empty
    worker = getattr(source, "_replacement_day_oi_thread", None)
    if worker:
        worker.join(2.)
    assert source._option_chain_meta["NIFTY"]["auto_certified"]
    assert source._call_bounded.call_count == 1


def test_watchdog_backup_auth_failure_is_a_warning_not_a_global_incident():
    from tests.test_volume_pipeline_and_watchdog_2026_07_30 import _watchdog, _scanner
    messages, desktop = [], []
    scanner = _scanner(data=NS(get_fyers_auth_status=lambda: {"auth_required": True},
                              _fyers_backup_enabled=lambda: True),
                       log_event=lambda message, level: messages.append((message, level)))
    _watchdog(scanner, desktop)
    assert any("Optional FYERS backup" in message and level == "warning" for message, level in messages)
    assert not any("Fyers login required" in message for message in desktop)
