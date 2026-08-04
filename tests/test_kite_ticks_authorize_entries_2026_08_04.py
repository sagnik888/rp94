"""The Kite tick feed must be visible to the entry gate.

2026-08-04, live: every entry candidate was refused with "no entry-eligible broker source
is fresh" while the Kite WebSocket was connected and subscribed to all four index tokens.

build_market_source_snapshot looked up INDEX_SPOT_TOKENS -- the AngelOne 999-prefixed
constants -- in provider._ws_cache. The Kite ticker caches under Kite's own
instrument_token, so the lookup always missed. _load_kite_index_tokens already says as
much ("the AngelOne token constants (26000, 26009...) mean nothing to Kite"); that was
fixed for index candles and missed here.

Order authority requires a source named kite* (fyers_ws is recorded with
entry_eligible=False, yahoo is DELAYED), so with no kite_ws record the ONLY thing that
could authorise an entry was the slower kite_rest path -- and whenever that was older
than 15s, Scanner._filter_live_candidates_by_source dropped every ENTRY candidate.
"""
import time

import pytest

from data.market_source_health import build_market_source_snapshot

ANGEL_TOKENS = {"NIFTY": ("99926000", "NSE")}
FYERS_SYMBOLS = {"NIFTY": "NSE:NIFTY50-INDEX"}
KITE_TOKEN = "256265"          # what the Kite ticker actually keys on


class _Provider:
    """Mirrors the live shape: Kite ticks under the Kite token, nothing under 999260xx."""

    def __init__(self, tick_age=1.0, with_kite_names=True, rest=None):
        now = time.time()
        self._ws_cache = {
            KITE_TOKEN: {"ltp": 24600.0, "time": now - tick_age, "source": "kite_ws"},
        }
        self._ltp_cache = dict(rest or {})
        self._kite_index_names = {KITE_TOKEN: "NIFTY"} if with_kite_names else {}


def _snapshot(provider):
    return build_market_source_snapshot(provider, "NIFTY", ANGEL_TOKENS, FYERS_SYMBOLS)


def test_a_live_kite_tick_authorizes_an_entry():
    snap = _snapshot(_Provider(tick_age=1.0))
    assert snap["entry_eligible"] is True, (
        "a 1s-old Kite tick did not authorise an entry — this is the live 2026-08-04 "
        "failure, where every candidate was refused while the ticker was connected"
    )
    assert any(s["source"] == "kite_ws" for s in snap["sources"])


def test_a_stale_kite_tick_still_does_not_authorize_an_entry():
    """The freshness rule must survive the fix — 6s is the realtime bound."""
    assert _snapshot(_Provider(tick_age=30.0))["entry_eligible"] is False


def test_fyers_alone_never_authorizes_an_entry():
    """Only the broker we trade through carries order authority."""
    provider = _Provider(tick_age=1.0, with_kite_names=False)
    provider._ws_cache["NSE:NIFTY50-INDEX"] = {
        "ltp": 24600.0, "time": time.time(), "source": "fyers_ws",
    }
    snap = _snapshot(provider)
    assert snap["entry_eligible"] is False
    assert all(not s["entry_eligible"] for s in snap["sources"])


def test_the_legacy_token_path_still_works():
    """Do not regress a provider that really does cache under the 999 token."""
    provider = _Provider(tick_age=1.0, with_kite_names=False)
    provider._ws_cache["99926000"] = {
        "ltp": 24600.0, "time": time.time(), "source": "kite_ws",
    }
    assert _snapshot(provider)["entry_eligible"] is True


def test_a_source_is_not_counted_twice():
    """The Kite token and a legacy alias must not both emit a record for one tick."""
    provider = _Provider(tick_age=1.0)
    provider._kite_index_names = {KITE_TOKEN: "NIFTY", KITE_TOKEN: "NIFTY"}
    sources = [s for s in _snapshot(provider)["sources"] if s["source"] == "kite_ws"]
    assert len(sources) == 1


def test_the_block_message_names_the_instrument():
    """loguru formats with {}, not %-style args; the old line logged a literal '%s'.

    It is the only line emitted when an entry is refused, so an unnamed instrument and
    no source ages made a live outage undiagnosable.
    """
    import inspect

    from scanner import Scanner

    src = inspect.getsource(Scanner._filter_live_candidates_by_source)
    assert "Blocked live entry for %s" not in src, "still using %-style args with loguru"
    assert "Sources seen:" in src, "the refusal must report the sources and their ages"
