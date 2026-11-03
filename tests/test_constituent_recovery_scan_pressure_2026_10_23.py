"""Constituent recovery must not parse uncertifiable deep stock history repeatedly."""
from dataclasses import replace
from datetime import date
from types import SimpleNamespace

import pandas as pd
import pytest

from tests.test_kite_angel_replacement_2026_10_23 import provider


class Running:
    def is_set(self): return False
    def wait(self, seconds): return False


def setup_source(tmp_path, *, first_day=date(2026, 10, 23), missing_stock=False):
    source = provider(tmp_path)
    source._replacement_stop = Running()
    source._constituent_volume.memberships = tuple(replace(m, effective_from=first_day)
        for m in source._constituent_volume.memberships)
    bars = pd.to_datetime(["2026-10-08 10:03", "2026-10-22 10:00", "2026-10-23 10:00"])
    frame = pd.DataFrame(dict(open=100., high=101., low=99., close=100., volume=10.), index=bars)
    requests = []
    def history(token, interval, **kwargs):
        requests.append((token, kwargs["days_back"]))
        return pd.DataFrame() if token != 100 and missing_stock else frame.copy()
    source.kite = SimpleNamespace(get_historical=history, instrument_token=lambda *args: 1)
    angel_calls = []
    source.angel_index = SimpleNamespace(resolve_market_instruments=lambda members:
        [dict(exchange=e, token=s, canonical_symbol=s) for e, s in members],
        get_history=lambda exchange, token, interval, start, target:
            angel_calls.append(start) or frame.copy())
    return source, requests, angel_calls


@pytest.mark.parametrize("first_day,expected_days", [(date(2026,10,23),1),
    (date(2026,10,22),2), (date(2026,10,8),15)])
def test_stock_window_follows_known_membership_without_truncating_index_prices(tmp_path, first_day, expected_days):
    source, requests, _ = setup_source(tmp_path, first_day=first_day)
    key = ("NIFTY", 15, "latest", False)
    source._build_replacement_history(key, pd.Timestamp("2026-10-23 10:02"))
    result = source._replacement_history[key][1]
    assert requests[0] == (100, 15)
    assert requests[1:] == [(1, expected_days)]*2
    assert len(result) == 3
    assert result.loc["2026-10-23 10:00", "volume_complete"]
    if first_day > date(2026,10,8):
        assert not result.loc["2026-10-08 10:03", "volume_complete"]


def test_angel_recovers_only_certifiable_window(tmp_path):
    source, _, calls = setup_source(tmp_path, missing_stock=True)
    key = ("NIFTY", 15, "latest", False)
    source._build_replacement_history(key, pd.Timestamp("2026-10-23 10:02"))
    assert calls == [pd.Timestamp("2026-10-23 10:00")]*2
    result = source._replacement_history[key][1]
    assert result.loc["2026-10-23 10:00", "volume_provider"] == "angelone"
    assert not result.loc["2026-10-22 10:00", "volume_complete"]


class Refusing:
    """A broker whose own verdict for the request is a refusal, not an answer."""
    def instrument_token(self, *args): return 1
    def get_historical(self, token, interval, **kwargs):
        outcome = kwargs.get("call_outcome")
        if outcome is not None:
            outcome.update(ok=False, reason="historical REST throttled")
        return pd.DataFrame()


class Answering:
    """A broker that answers cleanly with a genuinely empty frame."""
    def instrument_token(self, *args): return 1
    def get_historical(self, token, interval, **kwargs):
        outcome = kwargs.get("call_outcome")
        if outcome is not None:
            outcome["ok"] = True
        return pd.DataFrame()


def test_refused_member_fetch_is_named_never_cached_as_absent(tmp_path):
    source, _, _ = setup_source(tmp_path)
    source.kite = Refusing()
    key = ("NSE", "BEL", 1, "2026-10-23")
    frame = source._replacement_member_history(key, "BEL", "NSE", 1, pd.Timestamp("2026-10-23 10:02"))
    assert frame is None
    assert source._replacement_stock_cache == {}


def test_genuinely_empty_member_answer_is_still_cached(tmp_path):
    source, _, _ = setup_source(tmp_path)
    source.kite = Answering()
    key = ("NSE", "SUSPENDED", 1, "2026-10-23")
    frame = source._replacement_member_history(key, "SUSPENDED", "NSE", 1, pd.Timestamp("2026-10-23 10:02"))
    assert frame is not None and frame.empty
    assert key in source._replacement_stock_cache


def test_unknown_membership_never_downloads_stock_history(tmp_path):
    source, requests, calls = setup_source(tmp_path, first_day=date(2026,10,24))
    key = ("NIFTY", 15, "latest", False)
    source._build_replacement_history(key, pd.Timestamp("2026-10-23 10:02"))
    assert requests == [(100, 15)] and calls == []
    assert not source._replacement_history[key][1]["volume_complete"].any()


def test_disk_bootstrap_runs_once_but_refresh_still_fetches_new_index_history(tmp_path, monkeypatch):
    source, requests, _ = setup_source(tmp_path)
    key = ("NIFTY", 15, "latest", True)
    target = pd.Timestamp("2026-10-23 10:02")
    source._build_replacement_history(key, target)
    path = tmp_path/"candidate"/"NIFTY_1min.csv"
    assert path.exists()
    source._replacement_history.clear()  # simulate first post-restart recovery
    original = pd.read_csv
    loads = []
    def read(*args, **kwargs): loads.append(args[0]); return original(*args, **kwargs)
    monkeypatch.setattr(pd, "read_csv", read)
    source._build_replacement_history(key, target)
    source._build_replacement_history(key, target+pd.Timedelta(minutes=1))
    assert loads == [path]
    assert sum(token == 100 for token, _ in requests) == 3
