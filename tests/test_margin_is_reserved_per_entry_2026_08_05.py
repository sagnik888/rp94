"""Two concurrent entries must not spend the same rupees.

/user/margins does not reflect an order placed moments ago, so two entry workers reading
free cash at the same time both saw the full balance and both passed. On a Rs.51k account
where one NIFTY lot costs Rs.13k that is survivable; on four concurrent signals it is not.

The contract for this already existed and was never implemented: `reservation_id` in the
return shape and `release_entry_margin_reservation` are both declared on the base Broker
(trading/broker.py:173-183), KiteBroker returned "" for the id, and
`entry_margin_buffer_pct` (config/settings.py:423, with a 0-100 validator) plus
`requires_entry_margin_preflight` (trading/kite_broker.py:103) had no readers at all.

Fail-open on an UNANSWERABLE margin question is deliberately preserved: a quiet
/margins endpoint outside market hours must not halt trading. Only a definite
"not enough money" blocks, which is what the existing docstring promises.
"""
import threading

import pytest

from trading.kite_broker import KiteBroker


class _Api:
    pass


def _broker(available, required, buffer_pct=10.0):
    b = KiteBroker.__new__(KiteBroker)
    b.api = _Api()
    b._margin_lock = threading.Lock()
    b._margin_reservations = {}
    b._margin_reservation_ttl_seconds = 90.0
    b.available_funds = lambda: available
    b.required_entry_margin = lambda *a, **k: required
    b._entry_margin_buffer_pct = lambda: buffer_pct
    return b


def _preflight(broker, qty=65):
    return broker.preflight_entry_margin(
        symbol="NIFTY2680524500CE", qty=qty, side="BUY", product_type="CARRYFORWARD",
    )


def test_a_single_affordable_entry_is_approved_and_reserves_its_margin():
    broker = _broker(available=51_284.0, required=13_240.0)
    verdict = _preflight(broker)
    assert verdict["ok"] is True
    assert verdict["reservation_id"], "approved without reserving anything"
    assert broker._margin_reservations[verdict["reservation_id"]]["amount"] == pytest.approx(
        13_240.0 * 1.10
    )


def test_an_early_affordability_probe_does_not_reserve_margin():
    broker = _broker(available=51_284.0, required=13_240.0)

    verdict = broker.preflight_entry_margin(
        symbol="NIFTY2680524500CE",
        qty=65,
        side="BUY",
        product_type="CARRYFORWARD",
        reserve=False,
    )

    assert verdict["ok"] is True
    assert verdict["reservation_id"] == ""
    assert broker._margin_reservations == {}


def test_concurrent_entries_cannot_all_spend_the_same_cash():
    """The actual bug: free cash is read per worker and does not move between reads."""
    broker = _broker(available=51_284.0, required=13_240.0)
    verdicts = [_preflight(broker) for _ in range(5)]
    approved = [v for v in verdicts if v["ok"]]
    committed = 13_240.0 * 1.10
    assert len(approved) == int(51_284.0 // committed) == 3, (
        f"approved {len(approved)} entries against funds for "
        f"{int(51_284.0 // committed)} — the account over-commits"
    )
    assert "already reserved" in verdicts[-1]["limitation"]


def test_reservation_ids_stay_unique_when_wall_clock_is_frozen(monkeypatch):
    """Clock resolution must not let one reservation overwrite another."""
    broker = _broker(available=51_284.0, required=13_240.0)
    monkeypatch.setattr("trading.kite_broker.time.time", lambda: 1_000.0)

    verdicts = [_preflight(broker) for _ in range(4)]
    approved = [verdict for verdict in verdicts if verdict["ok"]]

    assert len(approved) == 3
    assert len({verdict["reservation_id"] for verdict in approved}) == 3
    assert len(broker._margin_reservations) == 3
    assert verdicts[-1]["ok"] is False


def test_the_buffer_is_applied_and_configurable():
    """A margin figure that is exactly the balance leaves nothing for charges."""
    assert _preflight(_broker(14_000.0, 13_240.0, buffer_pct=10.0))["ok"] is False
    assert _preflight(_broker(14_000.0, 13_240.0, buffer_pct=0.0))["ok"] is True


def test_releasing_gives_the_cash_back():
    broker = _broker(available=51_284.0, required=13_240.0)
    ids = [_preflight(broker)["reservation_id"] for _ in range(3)]
    assert _preflight(broker)["ok"] is False
    broker.release_entry_margin_reservation(ids[0])
    assert _preflight(broker)["ok"] is True, "released margin was not usable again"


def test_releasing_an_unknown_id_is_harmless():
    broker = _broker(available=51_284.0, required=13_240.0)
    broker.release_entry_margin_reservation("")
    broker.release_entry_margin_reservation("nope")
    assert _preflight(broker)["ok"] is True


def test_a_stale_reservation_expires_rather_than_halting_trading():
    """An entry that died without releasing must not hold cash for the session."""
    broker = _broker(available=51_284.0, required=13_240.0)
    for _ in range(3):
        _preflight(broker)
    assert _preflight(broker)["ok"] is False
    for record in broker._margin_reservations.values():
        record["at"] -= 91.0
    assert _preflight(broker)["ok"] is True, (
        "a leaked reservation blocked entries indefinitely — a self-inflicted halt"
    )


def test_an_unanswerable_margin_question_still_fails_open():
    """Preserved on purpose: /margins is routinely quiet outside market hours.

    The clock is pinned. preflight_entry_margin reads the wall clock
    (trading/kite_broker.py:1719-1721) and returns a DIFFERENT basis inside market
    hours, so an unpinned version of this test passes overnight and fails between
    09:15 and 15:30 — it failed at 09:41 on 2026-08-21 for exactly that reason.
    """
    from freezegun import freeze_time

    with freeze_time("2026-08-20 18:00:00"):   # after the close, IST-safe
        no_requirement = _broker(available=51_284.0, required=None)
        assert no_requirement.required_entry_margin() is None
        verdict = _preflight(no_requirement)
        assert verdict["ok"] is True and verdict["basis"] == "MARGIN_UNVERIFIED"

        no_funds = _broker(available=None, required=13_240.0)
        verdict = _preflight(no_funds)
        assert verdict["ok"] is True and verdict["basis"] == "REQUIREMENT_ONLY"
        assert verdict["reservation_id"] == "", "reserved against an unknown balance"


def test_silence_during_market_hours_is_named_differently():
    """The same silence at 11:00 is not a quirk — the funds gate is inert.

    It still fails OPEN, deliberately, but the basis says so rather than pretending
    this is the ordinary out-of-hours quiet.
    """
    from freezegun import freeze_time

    with freeze_time("2026-08-20 05:30:00"):   # 11:00 IST, a Thursday
        no_funds = _broker(available=None, required=13_240.0)
        verdict = _preflight(no_funds)

    assert verdict["ok"] is True
    assert verdict["basis"] == "FUNDS_UNCHECKED_IN_SESSION"
    assert "not checking anything" in verdict["limitation"]
    assert verdict["reservation_id"] == "", "reserved against an unknown balance"


def test_a_definite_shortfall_still_blocks():
    verdict = _preflight(_broker(available=5_000.0, required=13_240.0))
    assert verdict["ok"] is False
    assert "insufficient funds" in verdict["limitation"]


def test_a_recent_exact_shortfall_can_be_read_without_another_broker_request():
    broker = _broker(available=5_000.0, required=13_240.0)
    assert _preflight(broker)["ok"] is False

    cached = broker.cached_entry_margin_refusal(
        symbol="NIFTY2680524500CE", qty=65, side="BUY", product_type="CARRYFORWARD",
    )
    assert cached and cached["ok"] is False


def test_reservations_are_thread_safe():
    """preflight runs on the entry workers, which is where the race lives.

    Funds are sized so the cap BITES well before the thread count — 8 workers against
    room for 5. Sizing it the other way round proves nothing: every worker passes and
    the assertion holds whether or not the lock exists.
    """
    committed = 13_240.0 * 1.10
    broker = _broker(available=committed * 5, required=13_240.0)
    results = []
    barrier = threading.Barrier(8)

    def worker():
        barrier.wait()
        results.append(_preflight(broker)["ok"])

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sum(results) == 5, f"{sum(results)} of 8 concurrent entries approved, room for 5"
