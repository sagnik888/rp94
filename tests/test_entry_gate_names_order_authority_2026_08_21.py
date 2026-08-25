"""The entry gate must say WHICH source is missing, not that nothing is fresh.

On 2026-08-21 between 14:35 and 15:02 the gate refused 13 entries with:

    no entry-eligible broker source is fresh; delayed data remains context-only.
    Sources seen: kite_ws[context/1672.49s], fyers_ws[context/0.03s],
                  fyers_rest[context/1517.76s], yahoo[context/Nones]

Fyers WS was 0.01-0.53s fresh on every one of those lines. Nothing was starved of
data. The real condition is narrower and is set in market_source_health.py:167:

    has_order_authority = str(name or "").startswith("kite")
    "entry_eligible": bool(entry_eligible and has_order_authority)

Only the broker we route orders through may authorise an entry, so a sub-second
Fyers tick is deliberately context-only. The gate was RIGHT; its wording was not,
and reading "no source is fresh" against a live sub-second feed sends the reader
hunting a feed outage that is not happening.

The `Sources seen:` suffix stays exactly as it is — it is what made the real cause
diagnosable at all.
"""
from types import SimpleNamespace

import pytest

import scanner as scanner_module
from scanner import Scanner


STALLED_SNAPSHOT = {
    "entry_eligible": False,
    "sources": [
        {"source": "kite_ws", "entry_eligible": False, "age_seconds": 1672.49},
        {"source": "fyers_ws", "entry_eligible": False, "age_seconds": 0.03},
        {"source": "fyers_rest", "entry_eligible": False, "age_seconds": 1517.76},
        {"source": "yahoo", "entry_eligible": False, "age_seconds": None},
    ],
}


@pytest.fixture
def gate(monkeypatch):
    """A REAL-mode scanner with the two side-effects captured."""
    sc = Scanner.__new__(Scanner)
    sc.mode = "REAL"
    sc._diag_inc_unique = lambda *a, **k: None
    recorded = []
    sc._rtracker_record_candidate = lambda candidate, **kw: recorded.append(kw)
    logged = []
    monkeypatch.setattr(
        scanner_module.logger, "warning", lambda msg, *a, **k: logged.append(str(msg))
    )
    return sc, recorded, logged


def _candidate(action="ENTRY"):
    return SimpleNamespace(
        action=action, timeframe="5min", direction="LONG",
        signal_timestamp="2026-08-21T14:35:00",
    )


def test_the_message_names_order_authority_not_general_staleness(gate):
    sc, _recorded, logged = gate

    sc._filter_live_candidates_by_source("BANKNIFTY", [_candidate()], STALLED_SNAPSHOT)

    assert len(logged) == 1
    message = logged[0].lower()
    assert "order authority" in message or "order-authority" in message
    assert "kite" in message
    # The old wording. A sub-second Fyers tick was on screen while it was printed.
    assert "no entry-eligible broker source is fresh" not in message


def test_the_source_ages_are_still_reported(gate):
    """This suffix is what made the 2026-08-21 stall diagnosable — do not lose it."""
    sc, _recorded, logged = gate

    sc._filter_live_candidates_by_source("BANKNIFTY", [_candidate()], STALLED_SNAPSHOT)

    assert "Sources seen:" in logged[0]
    assert "fyers_ws[context/0.03s]" in logged[0]
    assert "kite_ws[context/1672.49s]" in logged[0]


def test_the_instrument_is_still_named(gate):
    sc, _recorded, logged = gate

    sc._filter_live_candidates_by_source("MIDCPNIFTY", [_candidate()], STALLED_SNAPSHOT)

    assert "MIDCPNIFTY" in logged[0]


def test_the_recorded_reason_matches_the_message(gate):
    """rtracker carries the reason into every later analysis; it must not disagree."""
    sc, recorded, _logged = gate

    sc._filter_live_candidates_by_source("NIFTY", [_candidate()], STALLED_SNAPSHOT)

    assert len(recorded) == 1
    reason = recorded[0]["reason"].lower()
    assert "order authority" in reason or "order-authority" in reason
    assert recorded[0]["source"] == "source_freshness_gate"
    assert recorded[0]["decision"] == "REJECTED"


def test_an_entry_eligible_snapshot_passes_everything(gate):
    sc, recorded, logged = gate
    fresh = dict(STALLED_SNAPSHOT, entry_eligible=True)

    out = sc._filter_live_candidates_by_source("NIFTY", [_candidate()], fresh)

    assert len(out) == 1
    assert recorded == [] and logged == []


def test_exits_are_never_blocked(gate):
    """Context-only data may still MANAGE a position — only entries are gated."""
    sc, recorded, logged = gate

    out = sc._filter_live_candidates_by_source(
        "NIFTY", [_candidate(action="EXIT")], STALLED_SNAPSHOT
    )

    assert len(out) == 1
    assert recorded == [] and logged == []
