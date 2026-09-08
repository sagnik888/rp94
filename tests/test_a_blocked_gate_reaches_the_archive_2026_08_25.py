"""A live entry gate's refusal must reach the evidence archive, not just the tape.

scanner._gate_block is the single exit for all 13 live entry gates. It wrote the
refusal to the file log and to the dashboard tape (sqlite signal_decisions) and
stopped there. The rtracker events archive under data_store/rtracker/events/ is
the SOLE input to scripts/gate_edge_report.py, the tool this project uses to
decide whether a gate earns its place — so every one of those 13 gates was
unmeasurable.

2026-08-25 is what that costs. Comparing the two sinks for that session:

    quad-prime raw quality floor   15 refusals on the tape,  0 in the archive
    Market location risk            3                        0
    15min choppy scalp guard        2                        0

20 of the session's 54 refusals, 37%, invisible. Among them the one that refused
BANKNIFTY LONG at 10:43:52 (B+, 66%, 15m trend agreeing) two minutes after the
session low, before the index ran +126 points against -16 of heat. The quad-prime
floor is the most common rejection reason in the decision log and appears nowhere
in the published gate ranking, so its threshold has only ever been argued from
anecdote.

The dedup is part of the contract, not an optimisation: a live gate re-runs every
scan cycle for as long as a refused signal keeps re-presenting itself, and an
unconditional archive write would record the same refusal hundreds of times a
session — inflating the very counts the report takes medians over.
"""
from datetime import datetime

import pytest

from scanner import Scanner


class _FakeTracker:
    def __init__(self):
        self.rows = []

    def record_candidate(self, candidate, *, decision="", reason="", source="", extra=None):
        self.rows.append({
            "decision": decision,
            "reason": reason,
            "source": source,
            "instrument": getattr(candidate, "instrument", ""),
        })


class _Candidate:
    instrument = "BANKNIFTY"
    timeframe = "5min"
    direction = "LONG"
    grade = "B+"
    confidence = 0.66
    signal_timestamp = datetime(2026, 8, 25, 10, 40, 0)


@pytest.fixture
def scanner():
    s = Scanner.__new__(Scanner)
    s.rtracker = _FakeTracker()
    # The production evidence ledger is intentionally REAL-only; historical/paper
    # scans are kept out so restarts cannot inflate rejected-signal counts.
    s.mode = "REAL"
    s.log_event = lambda *a, **k: None
    return s


BANKNIFTY_1043 = (
    "Live Gate: blocked BANKNIFTY 5min LONG; quad-prime raw quality floor failed "
    "(solo_sources=1, grade=B+, conf=66%, adx=13.0, intel=+0.01, trend15=True, risky=True)."
)


def test_the_refusal_reaches_the_archive(scanner):
    """The regression: this gate refused 15 signals and archived none of them."""
    assert scanner._gate_block(_Candidate(), BANKNIFTY_1043) is False

    assert len(scanner.rtracker.rows) == 1
    row = scanner.rtracker.rows[0]
    assert row["decision"] == "REJECTED"
    assert row["instrument"] == "BANKNIFTY"
    assert row["source"] == "live_entry_gate"


def test_the_archived_reason_is_the_gate_not_the_signal(scanner):
    """gate_edge_report groups by reason. The preamble names the signal, so
    leaving it in would mint a family of one for every signal ever refused —
    the exact failure reason_family exists to prevent."""
    scanner._gate_block(_Candidate(), BANKNIFTY_1043)
    reason = scanner.rtracker.rows[0]["reason"]

    assert reason.startswith("quad-prime raw quality floor failed")
    assert "BANKNIFTY" not in reason
    assert "Live Gate" not in reason

    from scripts.gate_edge_report import reason_family
    assert reason_family(reason) == "quad-prime raw quality floor failed"


def test_the_same_refusal_is_archived_once(scanner):
    """A live gate re-runs every scan cycle; the archive must not count it again."""
    for _ in range(50):
        scanner._gate_block(_Candidate(), BANKNIFTY_1043)

    assert len(scanner.rtracker.rows) == 1


def test_a_different_signal_is_archived_separately(scanner):
    later = _Candidate()
    later.signal_timestamp = datetime(2026, 8, 25, 10, 45, 0)

    scanner._gate_block(_Candidate(), BANKNIFTY_1043)
    scanner._gate_block(later, BANKNIFTY_1043)

    assert len(scanner.rtracker.rows) == 2


@pytest.mark.parametrize("message,expected", [
    (BANKNIFTY_1043, "quad-prime raw quality floor failed"),
    ("Live Gate: blocked NIFTY 15min SHORT; UNKNOWN regime requires A/A+ "
     "(confidence >= 0.70).", "UNKNOWN regime requires A/A+"),
    ("Live Gate: blocked SENSEX OPT LONG; range/choppy regime allows only A/A+ "
     "option setups or very high confidence.",
     "range/choppy regime allows only A/A+ option setups o"),
    # No separator at all: keep the whole message rather than losing the refusal.
    ("Live Gate: blocked", "Live Gate"),
])
def test_every_gate_message_shape_yields_its_own_reason(message, expected):
    from scripts.gate_edge_report import reason_family
    assert reason_family(Scanner._gate_block_reason(message)) == expected


def test_telemetry_can_never_veto_the_gate(scanner):
    """The verdict is already decided. A broken archive may not change it."""
    class _Exploding:
        def record_candidate(self, *a, **k):
            raise RuntimeError("archive down")

    scanner.rtracker = _Exploding()

    assert scanner._gate_block(_Candidate(), BANKNIFTY_1043) is False


def test_a_scanner_with_no_tracker_still_blocks():
    s = Scanner.__new__(Scanner)
    s.rtracker = None
    s.mode = "SIGNAL"
    s.log_event = lambda *a, **k: None

    assert s._gate_block(_Candidate(), BANKNIFTY_1043) is False


# ── one row per GATE, not per signal ─────────────────────────────────────────
#
# The archive is deduped per (signal, gate): gate_edge_report.distinct_signals()
# keys on (signal identity, reason family) and every family's n counts signals it
# personally refused. The tape's key carries no gate identity, so hanging the
# archive write on the tape's dedup recorded only the FIRST of the 13 gates to
# refuse a given signal. The gates deepest in the chain would then look rare
# because they are checked last, not because they fire less often -- a ranking
# built on that would understate exactly the late gates it exists to judge.

LOCATION_RISK = (
    "Live Gate: blocked BANKNIFTY 5min LONG; Market location risk LONG: spot "
    "57411.60 below/against EMA structure (EMA9 57406.50, EMA21 57426.30, day-pos 47%)"
)


def test_two_gates_refusing_one_signal_are_two_rows(scanner):
    """The regression: the second gate's refusal vanished."""
    candidate = _Candidate()

    scanner._gate_block(candidate, BANKNIFTY_1043)
    scanner._gate_block(candidate, LOCATION_RISK)

    reasons = [r["reason"] for r in scanner.rtracker.rows]
    assert len(reasons) == 2, reasons
    assert any(r.startswith("quad-prime") for r in reasons)
    assert any(r.startswith("Market location risk") for r in reasons)


def test_one_gate_is_one_row_however_its_readings_moved(scanner):
    """The reason embeds per-cycle measurements. Keying on those would defeat the
    dedup and write the same refusal on every scan cycle."""
    for conf, adx in ((66, 13.0), (67, 13.4), (65, 12.1)):
        scanner._gate_block(_Candidate(), (
            f"Live Gate: blocked BANKNIFTY 5min LONG; quad-prime raw quality floor "
            f"failed (solo_sources=1, grade=B+, conf={conf}%, adx={adx}, trend15=True)."
        ))

    assert len(scanner.rtracker.rows) == 1


# The same gate, on two consecutive scan cycles, with the regime label drifting
# between two members of choppy_regimes -- so the gate keeps firing while the
# label changes underneath it.
CHOPPY_MSG = (
    "Live Gate: blocked NIFTY 5min option; risky regime requires >= 60% plus 15min "
    "agreement or confirmed impulse (CHOPPY, B+, 66%, agree=True, impulse=False)."
)
VOLATILE_MSG = (
    "Live Gate: blocked NIFTY 5min option; risky regime requires >= 60% plus 15min "
    "agreement or confirmed impulse (VOLATILE, B+, 66%, agree=True, impulse=False)."
)


# Every message the 13 live gates actually build, one realistic instance each
# (grep -n "_gate_block(" scanner.py).
GATE_MESSAGES = [
    "Live Gate: blocked NIFTY 5min LONG; 15M MAIN policy treats 5min as timing/exit-only.",
    "Live Gate: blocked NIFTY 5min LONG; late-session entry block: 15:12 past the 15:05 cutoff.",
    "Live Gate: blocked NIFTY 15min LONG; 15min choppy scalp guard: signed intelligence -0.10 not aligned for BUY.",
    BANKNIFTY_1043,
    LOCATION_RISK,
    "Live Gate: blocked NIFTY 5min LONG; UNKNOWN regime requires A/A+ (confidence >= 0.70).",
    "Live Gate: blocked NIFTY 5min LONG; B-grade requires trending regime + 15m alignment + ADX>=25 at confidence>=0.50 (B, 52%).",
    CHOPPY_MSG,
    VOLATILE_MSG,
    "Live Gate: blocked NIFTY 5min LONG; needs A/A+ with >= 70%, or 15min trend agreement (B+, 66%, agree=True).",
    "Live Gate: blocked NIFTY 5min LONG; choppy regime needs A/A+ with >= 70%, or 15min trend agreement.",
    "Live Gate: blocked SENSEX OPT LONG; range/choppy regime allows only A/A+ option setups or very high confidence.",
    "Live Gate: blocked NIFTY LONG; grade is too weak for this regime (B+, CHOPPY).",
]


def test_the_dedup_key_never_merges_two_reporting_families():
    """The invariant, and it is one-directional.

    scanner keys the archive on _gate_block_family; gate_edge_report groups rows
    on reason_family. They are NOT the same string -- reason_family truncates at
    52 characters and preserves case, _gate_block_family does neither -- and they
    do not need to be. What matters is the direction: the dedup key must be at
    least as FINE as the report's family. Finer is harmless, because the report
    dedups again on (signal, family) and collapses the extra rows. Coarser would
    silently drop a gate's rows before the report ever sees them.
    """
    from scripts.gate_edge_report import reason_family

    for i, left in enumerate(GATE_MESSAGES):
        for right in GATE_MESSAGES[i + 1:]:
            families_differ = (
                reason_family(Scanner._gate_block_reason(left))
                != reason_family(Scanner._gate_block_reason(right))
            )
            keys_differ = (
                Scanner._gate_block_family(left) != Scanner._gate_block_family(right)
            )
            if families_differ:
                assert keys_differ, (
                    "two reporting families share one dedup key, so the second "
                    f"gate's rows are dropped:\n  {left}\n  {right}"
                )


def test_a_drifting_regime_label_does_not_split_one_gate_in_two():
    """The other direction, which the pairwise test above cannot see.

    regime is re-derived every scan cycle. While it sat OUTSIDE the brackets it was
    part of the gate's identity, so a label drifting CHOPPY -> VOLATILE mid-signal
    minted a second dedup key AND a second reason_family -- one gate ranked as two
    families with half the n each, which is the fragmentation this whole change set
    exists to remove. Both readings now sit inside the brackets, where every other
    gate already puts them.
    """
    from scripts.gate_edge_report import reason_family

    assert Scanner._gate_block_family(CHOPPY_MSG) == Scanner._gate_block_family(VOLATILE_MSG)
    assert (
        reason_family(Scanner._gate_block_reason(CHOPPY_MSG))
        == reason_family(Scanner._gate_block_reason(VOLATILE_MSG))
    )
    # and the label is still recoverable from the row, just not from its identity
    assert "CHOPPY" in Scanner._gate_block_reason(CHOPPY_MSG)


def test_no_gate_message_produces_an_empty_dedup_key():
    """An empty family would collapse every gate into one key."""
    for message in GATE_MESSAGES:
        assert Scanner._gate_block_family(message).strip()


def test_a_family_is_stable_across_the_readings_that_move_each_cycle(scanner):
    """conf, adx and intel are re-measured every scan. If they reached the key the
    dedup would never hit and the archive would take a row per cycle."""
    seen = set()
    for conf, adx in ((66, 13.0), (67, 13.4), (65, 12.1)):
        seen.add(Scanner._gate_block_family(
            f"Live Gate: blocked BANKNIFTY 5min LONG; quad-prime raw quality floor "
            f"failed (solo_sources=1, grade=B+, conf={conf}%, adx={adx}, trend15=True)."
        ))

    assert len(seen) == 1, seen


# ── the bounded key set must forget the OLDEST ───────────────────────────────

def test_the_key_prune_drops_the_oldest_not_an_arbitrary_half(scanner):
    """It pruned a SET with list(seen)[-500:]. A set has no order, so that evicted
    an arbitrary half. An evicted key re-emits, duplicating the tape row and the
    archive row hung off it -- inflating the very counts the report medians over."""
    scanner.log_event = lambda *a, **k: None
    for n in range(1001):
        assert scanner.log_signal_decision_once(f"k{n}", "m") is True

    # 1001 keys added; the prune fires once and keeps the most recent 500.
    # Asserted on the retained keys directly: probing them re-adds the misses and
    # cascades another prune, so the probe would perturb what it measures.
    assert list(scanner._activity_signal_decision_keys) == [
        f"k{n}" for n in range(501, 1001)
    ]

    # The boundary, behaviourally: newest still remembered, oldest forgotten.
    assert scanner.log_signal_decision_once("k1000", "m") is False
    assert scanner.log_signal_decision_once("k501", "m") is False
    assert scanner.log_signal_decision_once("k0", "m") is True


def test_a_preseeded_set_is_adopted_without_losing_its_keys():
    """Callers that seeded the old set must keep working."""
    s = Scanner.__new__(Scanner)
    s.rtracker = None
    s.mode = "SIGNAL"
    s.log_event = lambda *a, **k: None
    s._activity_signal_decision_keys = {"already-seen"}

    assert s.log_signal_decision_once("already-seen", "m") is False
    assert s.log_signal_decision_once("brand-new", "m") is True


def test_an_object_without_the_dedup_helper_still_blocks():
    """The live-gate parity golden drives these gates through a SimpleNamespace
    carrying only the methods under test. An unguarded telemetry call turned all
    115,200 of its cells into ERR:AttributeError -- a telemetry line deciding a
    gate's verdict, which is exactly what the wrapping forbids."""
    from types import SimpleNamespace

    stub = SimpleNamespace(mode="SIGNAL")
    stub._gate_block = Scanner._gate_block.__get__(stub, Scanner)
    stub.log_signal_decision_once = lambda *a, **k: None   # no _note_gate_archived

    assert stub._gate_block(_Candidate(), BANKNIFTY_1043) is False


# ── the dedup must be atomic ─────────────────────────────────────────────────
#
# _process_instrument runs one task per index on a ThreadPoolExecutor
# (scanner.py:6996) and calls log_signal_decision_once, while the main loop calls
# it through _gate_block. The check and the insert are two bytecodes with a
# thread switch possible between them, so unlocked, two threads can both be told
# "first emission" for one key and both write -- duplicating the tape row and the
# archive row hung off it. The prune is the worse half: list(seen.items()) during
# a concurrent insert raises "dictionary changed size during iteration", and that
# propagates out of 26 call sites that do not wrap it.
#
# Simply running threads at this does NOT reproduce it -- the window is two
# bytecodes wide and eight threads over hundreds of iterations miss it every
# time. So the window is held open deliberately, which is the only way this test
# means anything.

def test_only_one_caller_is_told_it_is_the_first_emission():
    import threading
    import time

    class _HoldsTheWindowOpen(dict):
        """Forces the interleaving the GIL usually hides: the first membership
        test releases the second thread and then stalls, still inside the
        check-and-insert."""

        def __init__(self):
            super().__init__()
            self.second_thread_may_go = threading.Event()
            self.checks = 0

        def __contains__(self, key):
            self.checks += 1
            # The answer must be computed BEFORE the stall. Stalling first and
            # asking afterwards re-reads the dict once the other thread has
            # already inserted, which serialises the two callers by accident and
            # makes this test pass against the very bug it is meant to catch.
            answer = super().__contains__(key)
            if self.checks == 1:
                self.second_thread_may_go.set()
                time.sleep(0.2)
            return answer

    s = Scanner.__new__(Scanner)
    s.rtracker = None
    s.mode = "SIGNAL"
    s.log_event = lambda *a, **k: None
    probe = _HoldsTheWindowOpen()
    s._activity_signal_decision_keys = probe

    results = []

    def first():
        results.append(s.log_signal_decision_once("same-key", "m"))

    def second():
        probe.second_thread_may_go.wait(timeout=5)
        results.append(s.log_signal_decision_once("same-key", "m"))

    threads = [threading.Thread(target=first), threading.Thread(target=second)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert sorted(results) == [False, True], (
        f"{results.count(True)} callers were told they were first; "
        "each one writes a tape row and an archive row"
    )


def test_the_prune_is_inside_the_same_lock():
    """The prune iterates the dict it is pruning. It has to be atomic with the
    insert, not merely adjacent to it."""
    import inspect

    src = inspect.getsource(Scanner.log_signal_decision_once)
    body = src[src.index("with lock:"):]
    prune_at = body.index("[-500:]")
    unlocked = body.index("self.log_event(")

    assert prune_at < unlocked, "the prune escaped the lock"


def test_contended_use_never_raises():
    """A smoke test, not a proof: it passes with or without the lock. It exists to
    catch a future change that makes the common path throw."""
    import threading

    s = Scanner.__new__(Scanner)
    s.rtracker = None
    s.mode = "SIGNAL"
    s.log_event = lambda *a, **k: None

    errors = []
    start = threading.Barrier(6)

    def hammer(worker):
        start.wait()
        try:
            for i in range(500):
                s.log_signal_decision_once(f"w{worker}-k{i}", "m")
        except Exception as exc:      # noqa: BLE001 - the point is to catch any
            errors.append(exc)

    threads = [threading.Thread(target=hammer, args=(w,)) for w in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == [], errors
    assert len(s._activity_signal_decision_keys) <= 1000
