"""Preferred chain recovery is independent of FYERS cache and error cooldown."""
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock

import pandas as pd

from tests.test_kite_angel_replacement_2026_10_23 import provider
from tests.test_kite_angel_replacement_2026_10_23 import chain_provider, chain_frame


def source(tmp_path, monkeypatch):
    result = provider(tmp_path, route="angel_fyers")
    result.chain_matches_execution_expiry = lambda *args: True
    clock = [time.time()]
    monkeypatch.setattr("data.market_data.time.time", lambda: clock[0])
    key = "opt_chain_NIFTY"
    backup = pd.DataFrame({"strike": [50.], "call_ltp": [1.], "put_ltp": [1.]})
    result._opt_chain_cache[key] = backup
    result._option_chain_meta["NIFTY"] = {"source": "fyers", "auto_certified": True}
    result._last_opt_chain_fetch[key] = clock[0]-1000.
    result._last_opt_chain_error[key] = clock[0]-5.
    return result, clock, key, backup


def healthy(result, clock, key, provider_name="kite"):
    chain = pd.DataFrame({"strike": [100.], "call_ltp": [10.], "put_ltp": [10.]})
    result._opt_chain_cache[key] = chain
    result._option_chain_meta["NIFTY"] = {"source": provider_name, "auto_certified": True}
    result._last_opt_chain_fetch[key] = clock[0]
    result._last_opt_chain_error.pop(key, None)
    return chain


def test_healthy_kite_bypasses_fyers_error_cooldown(tmp_path, monkeypatch):
    result, clock, key, backup = source(tmp_path, monkeypatch)
    result._get_replacement_option_chain = Mock(side_effect=lambda *args: healthy(result, clock, key))
    answer = result.get_option_chain("NIFTY")
    assert answer is not backup and answer.iloc[0]["strike"] == 100.
    assert result._option_chain_meta["NIFTY"]["source"] == "kite"
    result._get_replacement_option_chain.assert_called_once()


def test_healthy_angel_bypasses_fresh_fyers_cache(tmp_path, monkeypatch):
    result, clock, key, backup = source(tmp_path, monkeypatch)
    result._last_opt_chain_fetch[key] = clock[0]-1.
    result._last_opt_chain_error.clear()
    result._get_replacement_option_chain = Mock(side_effect=lambda *args: healthy(result, clock, key, "angelone"))
    answer = result.get_option_chain("NIFTY")
    assert answer is not backup
    assert result._option_chain_meta["NIFTY"]["source"] == "angelone"


def test_failed_preferred_probes_do_not_extend_or_retry_fyers_cooldown(tmp_path, monkeypatch):
    result, clock, key, backup = source(tmp_path, monkeypatch)
    original_error, original_fetch = result._last_opt_chain_error[key], result._last_opt_chain_fetch[key]
    result.fyers = object()
    result._call_fyers_bounded = Mock(side_effect=AssertionError("FYERS cooldown retried"))

    def incomplete(*args):
        result._opt_chain_cache[key] = pd.DataFrame({"strike": [100.]})
        result._option_chain_meta["NIFTY"] = {"source": "kite", "auto_certified": False}
        result._last_opt_chain_fetch[key] = result._last_opt_chain_error[key] = clock[0]
        return result._opt_chain_cache[key]

    result._get_replacement_option_chain = Mock(side_effect=incomplete)
    for offset in (0., 1., 2., 4.9, 5.):
        clock[0] = original_error+5.+offset
        assert result.get_option_chain("NIFTY") is backup
        assert result._last_opt_chain_error[key] == original_error
        assert result._last_opt_chain_fetch[key] == original_fetch
        assert result._option_chain_meta["NIFTY"]["source"] == "fyers"
    assert result._get_replacement_option_chain.call_count == 2
    result._call_fyers_bounded.assert_not_called()


def test_concurrent_recovery_reads_share_one_preferred_probe(tmp_path, monkeypatch):
    result, clock, key, backup = source(tmp_path, monkeypatch)
    entered, release = threading.Event(), threading.Event()

    def blocked(*args):
        entered.set()
        assert release.wait(timeout=5.)
        return healthy(result, clock, key)

    result._get_replacement_option_chain = Mock(side_effect=blocked)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(result.get_option_chain, "NIFTY")
        try:
            assert entered.wait(timeout=5.)
            assert result.get_option_chain("NIFTY") is backup
        finally:
            release.set()
        assert first.result(timeout=5.) is not backup
    result._get_replacement_option_chain.assert_called_once()


def test_healthy_preferred_cache_keeps_normal_chain_ttl(tmp_path, monkeypatch):
    result, clock, key, _ = source(tmp_path, monkeypatch)
    preferred = healthy(result, clock, key)
    start = clock[0]
    result._get_replacement_option_chain = Mock(side_effect=AssertionError("Healthy cache refetched"))
    for offset in (0., 6., 89.):
        clock[0] = start+offset
        assert result.get_option_chain("NIFTY") is preferred
    result._get_replacement_option_chain.assert_not_called()


def test_real_preferred_recovery_does_not_probe_cooled_fyers_day_oi(tmp_path):
    result = chain_provider(tmp_path)
    result._market_data_route = "angel_fyers"
    result.chain_matches_execution_expiry = lambda *args: True
    key = "opt_chain_NIFTY"
    result._opt_chain_cache[key] = chain_frame()
    result._option_chain_meta["NIFTY"] = {"source": "fyers", "auto_certified": True}
    result._last_opt_chain_fetch[key] = time.time()-1000.
    result._last_opt_chain_error[key] = time.time()-5.
    result.kite.get_option_chain = Mock(side_effect=lambda *args, **kwargs: chain_frame())
    result.fyers = object()
    result._call_fyers_bounded = Mock(return_value=(False, None))
    answer = result.get_option_chain("NIFTY")
    assert len(answer) == 3
    assert result._option_chain_meta["NIFTY"]["source"] == "kite"
    result._call_fyers_bounded.assert_not_called()
