"""A decision row must say which build decided it, when, and what data quality said.

Verified on the 2026-09-16 live session (546 rtracker rows):

* Restarts at 10:32, 13:07 and 13:32 loaded three different code builds, yet every one of
  the 199 decision rows carried the same quality.configuration_hash, and
  strategy.revision was blank on 10 of them -- both missed_reentry lifecycles
  (ACCEPTED + EXIT) and six filtered_candidate refusals. The per-candidate
  build_fingerprint only existed on the rows whose producer remembered to stamp it.
* REJECTED/STABILIZED rows store ``timestamp`` == the signal bar OPEN. The moment the
  refusal was actually decided survived only by accident, inside
  expected_move.generated_at (NIFTY 15m PA veto: bar 13:00, decided 13:06:20).
* The five ONE_MINUTE_VOLUME_UNAVAILABLE refusals archived a volume block that said
  available=True / basis TRADED_VOLUME. Both readings were true -- the intelligence
  volume and the entry data-quality verdict measure different things -- but only one of
  them reached the row, so the row contradicted its own reason.

Everything here is recording only: no gate, grade or exit path reads these fields.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from engine import build_identity as bi
from engine.entry_data_quality import evaluate_entry_data_quality
from intelligence.rtracker import RegimeTracker
from trading.trade_manager import IST

HEX12 = re.compile(r"^[0-9a-f]{12}$")
STAMP_KEYS = {"git_sha", "git_dirty", "diff_hash", "fingerprint", "process_started_at", "pid"}

needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")


def _now_ist_naive() -> datetime:
    return datetime.now(IST).replace(tzinfo=None)


def _git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        [
            "git", "-C", str(repo),
            "-c", "user.name=test", "-c", "user.email=test@example.invalid",
            "-c", "commit.gpgsign=false", "-c", "core.autocrlf=false",
            *args,
        ],
        check=True,
        capture_output=True,
    )
    return completed.stdout.decode("utf-8", "replace").strip()


@pytest.fixture
def git_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    for rel in bi.FINGERPRINT_SOURCES:
        target = repo / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(f"# {rel}\n", encoding="utf-8")
    (repo / "tests").mkdir(exist_ok=True)
    (repo / "tests" / "test_something.py").write_text("# test\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    return repo


# --- (a) the process build stamp ----------------------------------------------------
@needs_git
def test_clean_checkout_stamp_names_commit_clean_tree_and_process(git_repo):
    stamp = bi.process_build_stamp(git_repo, refresh=True)

    assert set(stamp) == STAMP_KEYS
    assert stamp["git_sha"] == _git(git_repo, "rev-parse", "HEAD")[:12]
    assert stamp["git_dirty"] is False
    assert HEX12.match(stamp["diff_hash"])
    assert stamp["fingerprint"] == bi.build_fingerprint(git_repo, refresh=True)
    assert stamp["pid"] == os.getpid()
    started = datetime.fromisoformat(stamp["process_started_at"])
    assert started <= _now_ist_naive()


@needs_git
def test_an_uncommitted_source_edit_is_dirty_and_moves_the_diff_hash(git_repo):
    """09-16: three builds, one commit. The diff hash is what tells them apart."""
    clean = bi.process_build_stamp(git_repo, refresh=True)
    (git_repo / "engine" / "quality_verdict.py").write_text("# hunk one\n", encoding="utf-8")
    first_edit = bi.process_build_stamp(git_repo, refresh=True)
    (git_repo / "engine" / "quality_verdict.py").write_text("# hunk two\n", encoding="utf-8")
    second_edit = bi.process_build_stamp(git_repo, refresh=True)

    assert first_edit["git_dirty"] is True and second_edit["git_dirty"] is True
    assert first_edit["git_sha"] == clean["git_sha"] == second_edit["git_sha"]
    assert len({clean["diff_hash"], first_edit["diff_hash"], second_edit["diff_hash"]}) == 3


@needs_git
def test_editing_only_tests_does_not_masquerade_as_a_new_build(git_repo):
    clean = bi.process_build_stamp(git_repo, refresh=True)
    (git_repo / "tests" / "test_something.py").write_text("# edited\n", encoding="utf-8")
    after = bi.process_build_stamp(git_repo, refresh=True)

    assert after["git_dirty"] is False
    assert after["diff_hash"] == clean["diff_hash"]


@needs_git
def test_an_untracked_runtime_module_edit_moves_the_diff_hash(git_repo):
    """engine/adaptive_hold.py, engine/build_identity.py, engine/runtime_build.py,
    data/derivative_participation.py and intelligence/derivative_context.py run live
    while untracked and outside FINGERPRINT_SOURCES. ``git diff HEAD`` cannot see them,
    so an edit to one of them alone produced a stamp identical to the build before it."""
    clean = bi.process_build_stamp(git_repo, refresh=True)
    module = git_repo / "engine" / "adaptive_hold.py"
    assert "engine/adaptive_hold.py" not in bi.FINGERPRINT_SOURCES
    module.write_text("# first version\n", encoding="utf-8")
    added = bi.process_build_stamp(git_repo, refresh=True)
    module.write_text("# second version\n", encoding="utf-8")
    edited = bi.process_build_stamp(git_repo, refresh=True)

    assert added["git_dirty"] is True and edited["git_dirty"] is True
    assert clean["fingerprint"] == added["fingerprint"] == edited["fingerprint"]
    assert HEX12.match(added["diff_hash"]) and HEX12.match(edited["diff_hash"])
    assert len({clean["diff_hash"], added["diff_hash"], edited["diff_hash"]}) == 3


@needs_git
def test_untracked_tests_reports_and_ignored_files_do_not_move_the_diff_hash(git_repo):
    clean = bi.process_build_stamp(git_repo, refresh=True)
    (git_repo / "tests" / "test_brand_new.py").write_text("# new test\n", encoding="utf-8")
    (git_repo / "reports" / "replay").mkdir(parents=True)
    (git_repo / "reports" / "replay" / "frozen_scanner.py").write_text("# copy\n", encoding="utf-8")
    (git_repo / ".gitignore").write_text("venv/\n", encoding="utf-8")
    (git_repo / "venv" / "lib").mkdir(parents=True)
    (git_repo / "venv" / "lib" / "site.py").write_text("# ignored\n", encoding="utf-8")
    after = bi.process_build_stamp(git_repo, refresh=True)

    assert after["git_dirty"] is False
    assert after["diff_hash"] == clean["diff_hash"]


@needs_git
def test_an_untracked_listing_failure_is_unknown_not_clean(git_repo, monkeypatch):
    real_run_git = bi._run_git

    def _ls_files_fails(root, *args):
        if args and args[0] == "ls-files":
            return None
        return real_run_git(root, *args)

    monkeypatch.setattr(bi, "_run_git", _ls_files_fails)
    stamp = bi.process_build_stamp(git_repo, refresh=True)

    assert stamp["git_sha"] == _git(git_repo, "rev-parse", "HEAD")[:12]
    assert stamp["diff_hash"] == "unknown"
    assert stamp["git_dirty"] is None


def test_missing_git_degrades_to_unknown_instead_of_raising(tmp_path, monkeypatch):
    def _no_git(*_args, **_kwargs):
        raise FileNotFoundError("git")

    monkeypatch.setattr(subprocess, "run", _no_git)
    stamp = bi.process_build_stamp(tmp_path, refresh=True)

    assert stamp["git_sha"] == "unknown"
    assert stamp["diff_hash"] == "unknown"
    assert stamp["git_dirty"] is None
    assert stamp["fingerprint"]
    assert stamp["pid"] == os.getpid()


def test_a_hung_git_is_bounded_by_a_short_timeout(tmp_path, monkeypatch):
    timeouts = []

    def _hangs(cmd, *_args, **kwargs):
        timeouts.append(kwargs.get("timeout"))
        raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout"))

    monkeypatch.setattr(subprocess, "run", _hangs)
    stamp = bi.process_build_stamp(tmp_path, refresh=True)

    assert stamp["git_sha"] == "unknown" and stamp["diff_hash"] == "unknown"
    assert timeouts and all(t is not None and 0 < t <= 5 for t in timeouts)


def test_the_stamp_is_computed_once_per_process_not_per_row(tmp_path, monkeypatch):
    calls = []
    real_run = subprocess.run

    def _counting(*args, **kwargs):
        calls.append(args)
        return real_run(*args, **kwargs)

    monkeypatch.setattr(subprocess, "run", _counting)
    first = bi.process_build_stamp(tmp_path, refresh=True)
    spawned = len(calls)
    assert spawned >= 1, "the stamp must actually ask git"
    for _ in range(200):
        assert bi.process_build_stamp(tmp_path) == first
    assert len(calls) == spawned


def test_callers_cannot_corrupt_the_cached_stamp(tmp_path):
    stamp = bi.process_build_stamp(tmp_path, refresh=True)
    stamp["git_sha"] = "tampered"
    assert bi.process_build_stamp(tmp_path)["git_sha"] != "tampered"


# --- rows through the production recording path --------------------------------------
def _tracker(tmp_path) -> RegimeTracker:
    """Built the way Scanner.__init__ builds it."""
    return RegimeTracker.from_settings(
        SimpleNamespace(
            rtracker_data_dir=str(tmp_path / "rtracker"),
            rtracker_enabled=True,
            rtracker_report_weeks=8,
            rtracker_retention_days=0,
            strategy_revision="ut1-current",
            strategy_direction_mode="LONG_SHORT",
        )
    )


def _scanner(tracker):
    from scanner import Scanner

    scanner = Scanner.__new__(Scanner)
    scanner.mode = "REAL"
    scanner.rtracker = tracker
    scanner._entry_intel_snapshot = lambda _candidate: {}
    scanner.trades = SimpleNamespace(open_trades={}, _terminal_callback_lifecycles=set())
    return scanner


def _rows(tracker) -> list[dict]:
    rows = []
    for path in sorted(Path(tracker.events_dir).glob("*.jsonl")):
        rows.extend(json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line)
    return rows


def _unstamped_rejection(**overrides):
    fields = dict(
        action="NO_ENTRY",
        status="REJECTED - Price Action",
        instrument="NIFTY",
        timeframe="15min",
        direction="SHORT",
        signal_timestamp=datetime(2026, 9, 16, 13, 0),
        price=25010.0,
        reasons=["Price-action ADVERSE"],
    )
    fields.update(overrides)
    return SimpleNamespace(**fields)


def test_every_decision_lane_is_build_stamped_including_missed_reentry(tmp_path):
    tracker = _tracker(tmp_path)
    scanner = _scanner(tracker)

    # The six 09-16 blanks: a filtered refusal whose producer never stamped it.
    scanner._rtracker_record_candidate(
        _unstamped_rejection(), decision="REJECTED",
        reason="Price-action ADVERSE", source="filtered_candidate",
    )
    # A stabilised signal, also unstamped.
    scanner._rtracker_record_candidate(
        _unstamped_rejection(action="ENTRY", status="STABILIZED", timeframe="5min"),
        decision="STABILIZED", reason="stable", source="signal_stabilizer",
    )
    # The two 09-16 missed_reentry lifecycles: ACCEPTED then EXIT, never stamped.
    reentry = SimpleNamespace(
        action="ENTRY",
        status="ACTIVE",
        accepted_by_gate=True,
        source_lane="missed_reentry",
        instrument="SENSEX",
        timeframe="15min",
        direction="SHORT",
        signal_timestamp=datetime(2026, 9, 16, 11, 0),
        entry_timestamp=datetime(2026, 9, 16, 11, 0, 23),
        price=81000.0,
    )
    scanner._rtracker_record_candidate(
        reentry, decision="ACCEPTED", reason="Missed-signal re-entry",
        source="session_candidate",
    )
    trade = SimpleNamespace(
        id="T-REENTRY",
        status="CLOSED",
        is_ghost=False,
        is_recovery=False,
        calibration_outcome_recorded=True,
        source_lane="missed_reentry",
        instrument="SENSEX",
        timeframe="15min",
        direction="SHORT",
        signal_time=datetime(2026, 9, 16, 11, 0),
        entry_time=datetime(2026, 9, 16, 11, 0, 23),
        exit_time=datetime(2026, 9, 16, 11, 5, 20),
        entry_price=300.0,
        exit_price=320.0,
        exit_reason="SMART_PROFIT_LOCK",
        pnl=1200.0,
    )
    scanner._record_trade_manager_terminal(trade)

    rows = _rows(tracker)
    decisions = sorted(row["decision"] for row in rows)
    assert decisions == ["ACCEPTED", "EXIT", "REJECTED", "STABILIZED"]
    for row in rows:
        label = f"{row['decision']}/{row['source']}"
        assert row["strategy"]["revision"] == "ut1-current", label
        assert row["strategy"]["direction_mode"] == "LONG_SHORT", label
        assert row["strategy"]["data_provenance"] == "LIVE", label
        assert row["strategy"]["build_fingerprint"] == bi.build_fingerprint(), label
    stamp = bi.process_build_stamp()
    for row in rows:
        label = f"{row['decision']}/{row['source']}"
        assert row["build"] == stamp, label
        assert row["build"]["git_sha"] and row["build"]["diff_hash"], label


def test_a_producer_stamp_is_never_overwritten_by_the_writer(tmp_path):
    tracker = _tracker(tmp_path)
    scanner = _scanner(tracker)
    stamped = _unstamped_rejection(
        strategy_revision="rev-from-candidate",
        strategy_direction_mode="LONG_ONLY",
        data_provenance="LIVE",
        build_fingerprint="deadbeef1234+001122334455",
    )
    scanner._rtracker_record_candidate(
        stamped, decision="REJECTED", reason="x", source="live_entry_gate"
    )

    (row,) = _rows(tracker)
    assert row["strategy"] == {
        "revision": "rev-from-candidate",
        "direction_mode": "LONG_ONLY",
        "data_provenance": "LIVE",
        "build_fingerprint": "deadbeef1234+001122334455",
    }


def test_shadow_outcome_rows_carry_the_build_stamp(tmp_path):
    tracker = _tracker(tmp_path)
    tracker.record_event(
        {
            "id": "shadow-1",
            "schema": "rtracker.v1",
            "timestamp": "2026-09-16T10:15:00",
            "date": "2026-09-16",
            "instrument": "NIFTY",
            "timeframe": "5min",
            "direction": "LONG",
            "decision": "SHADOW_OUTCOME",
            "source": "entry_policy_executable_shadow",
        }
    )
    (row,) = _rows(tracker)
    assert row["build"] == bi.process_build_stamp()


# --- (b) decided_at ------------------------------------------------------------------
def test_decided_at_is_the_wall_clock_while_timestamp_keeps_the_bar_open(tmp_path):
    tracker = _tracker(tmp_path)
    scanner = _scanner(tracker)
    before = _now_ist_naive()
    scanner._rtracker_record_candidate(
        _unstamped_rejection(), decision="REJECTED",
        reason="Price-action ADVERSE", source="filtered_candidate",
    )
    after = _now_ist_naive()

    (row,) = _rows(tracker)
    # Joins and dashboards key on the bar clock; that must not move.
    assert row["timestamp"] == "2026-09-16T13:00:00"
    assert row["signal_timestamp"] == "2026-09-16T13:00:00"
    decided = datetime.fromisoformat(row["decided_at"])
    assert decided.tzinfo is None, "same naive-IST convention as every rtracker clock"
    assert before <= decided <= after


def test_decided_at_does_not_split_one_decision_into_two_rows(tmp_path):
    """The row id is keyed on the bar clock. A wall clock in the id would mint a new
    row on every scan that re-evaluates the same refusal."""
    tracker = _tracker(tmp_path)
    scanner = _scanner(tracker)
    for _ in range(3):
        scanner._rtracker_record_candidate(
            _unstamped_rejection(), decision="REJECTED",
            reason="Price-action ADVERSE", source="filtered_candidate",
        )
    assert len(_rows(tracker)) == 1


def test_accepted_and_exit_rows_carry_decided_at(tmp_path):
    tracker = _tracker(tmp_path)
    before = _now_ist_naive()
    tracker.record_candidate(
        SimpleNamespace(
            action="ENTRY", accepted_by_gate=True, instrument="NIFTY", timeframe="5min",
            direction="LONG", signal_timestamp=datetime(2026, 9, 16, 9, 20),
        ),
        decision="ACCEPTED", source="test",
    )
    tracker.record_candidate(
        SimpleNamespace(
            action="EXIT", instrument="NIFTY", timeframe="5min", direction="LONG",
            signal_timestamp=datetime(2026, 9, 16, 9, 20),
            exit_timestamp=datetime(2026, 9, 16, 9, 40),
        ),
        decision="EXIT", source="test",
    )
    rows = _rows(tracker)
    assert {row["decision"] for row in rows} == {"ACCEPTED", "EXIT"}
    for row in rows:
        assert datetime.fromisoformat(row["decided_at"]) >= before


# --- (c) the entry data-quality verdict reaches the row --------------------------------
def _chain():
    return {"intelligence_usable": True, "stale": False, "fallback": False}


def test_volume_block_carries_the_entry_data_quality_verdict_it_was_refused_on(
    tmp_path, monkeypatch
):
    """Reproduce the 09-16 contradiction through the real final gate, then archive."""
    from scanner import Scanner

    monkeypatch.setattr(Scanner, "_auto_entry_resource_blockers", staticmethod(lambda _s: []))
    monkeypatch.setattr(Scanner, "_session_bias_standaside", staticmethod(lambda *_: (False, "")))
    monkeypatch.setattr(Scanner, "_unanimous_book_blocker", staticmethod(lambda *_: ""))
    monkeypatch.setattr(
        Scanner, "_candidate_quality_context",
        staticmethod(lambda *_: ("TRENDING_UP", False, False, False)),
    )
    gate_scanner = SimpleNamespace(
        mode="REAL",
        _live_entry_data_blocks={},
        candles=SimpleNamespace(get_candles=lambda *_: None),
        _prediction_volume_context=lambda *_: {},
        _late_entry_scalp_active=lambda *_: False,
        _setting_enabled=lambda *_: False,
        _gate_block=lambda candidate, message: (
            setattr(candidate, "live_gate_rejection_reason", message) or False
        ),
    )
    candidate = SimpleNamespace(
        action="ENTRY",
        instrument="NIFTY",
        direction="LONG",
        timeframe="5min",
        inst_type="OPT",
        grade="A+",
        confidence=0.8,
        market_ltp_shock_entry=False,
        volume_spike_entry=False,
        chain_quality=_chain(),
        strike_oi_available=True,
        strike_volume_available=True,
        strike_liquidity_available=True,
        reasons=[],
        signal_timestamp=datetime(2026, 9, 16, 10, 5),
        # What the intelligence layer said: exchange traded volume IS available.
        market_intelligence_context={
            "volume": {
                "signal": "NEUTRAL",
                "available": True,
                "source": "EXCHANGE_VOLUME",
                "basis": "TRADED_VOLUME",
                "volume_ratio": 0.86,
            }
        },
    )
    assert Scanner._passes_live_trade_ready_gate(gate_scanner, candidate) is False

    tracker = _tracker(tmp_path)
    _scanner(tracker)._rtracker_record_candidate(
        candidate,
        decision="REJECTED",
        reason=candidate.live_gate_rejection_reason,
        source="live_entry_gate",
    )

    (row,) = _rows(tracker)
    volume = row["volume"]
    # The intelligence reading is recorded exactly as before...
    assert volume["available"] is True and volume["basis"] == "TRADED_VOLUME"
    # ...and the verdict the gate refused on now sits beside it.
    assert volume["entry_data_quality"] == {
        "verdict": "BLOCKED",
        "allowed": False,
        "reason_code": "ONE_MINUTE_VOLUME_UNAVAILABLE",
        "reason": "fresh comparable 1-minute cash volume is unavailable",
        "cash_volume_available": False,
        "version": "entry_data_quality.v1",
    }


def test_an_admitted_row_records_its_ready_verdict(tmp_path):
    tracker = _tracker(tmp_path)
    verdict = evaluate_entry_data_quality(
        live=True,
        special_entry=True,
        one_minute_volume={"available": True, "reliable": True, "comparable": True},
        chain_quality=_chain(),
    )
    tracker.record_candidate(
        SimpleNamespace(
            action="ENTRY", accepted_by_gate=True, instrument="NIFTY", timeframe="5min",
            direction="LONG", signal_timestamp=datetime(2026, 9, 16, 9, 20),
            entry_data_quality=dict(verdict),
        ),
        decision="ACCEPTED", source="test",
    )
    (row,) = _rows(tracker)
    assert row["volume"]["entry_data_quality"]["verdict"] == "ALLOWED"
    assert row["volume"]["entry_data_quality"]["reason_code"] == "READY"
    assert row["volume"]["entry_data_quality"]["cash_volume_available"] is True


def test_a_row_without_a_verdict_keeps_the_old_volume_shape(tmp_path):
    """Exits and early refusals never reached the data-quality gate; inventing an
    'unavailable' verdict for them would be a new contradiction."""
    event = RegimeTracker(tmp_path)._event_from_candidate(
        _unstamped_rejection(), decision="REJECTED", reason="", source="test", extra=None
    )
    assert "entry_data_quality" not in event["volume"]


# --- signal_decisions (sqlite) payload -------------------------------------------------
def test_signal_decisions_payload_carries_build_stamp_and_decided_at(monkeypatch):
    import scanner as scanner_module
    from scanner import Scanner

    saved = []
    monkeypatch.setattr(
        scanner_module, "db", SimpleNamespace(save_signal_decision=saved.append)
    )
    scanner = Scanner.__new__(Scanner)
    scanner.mode = "REAL"
    scanner.is_warmup = False

    before = datetime.now(IST)
    Scanner._persist_rejected_signal_event(
        scanner,
        "Live Gate: blocked NIFTY 5min LONG; Price-action ADVERSE",
        "trade",
    )
    after = datetime.now(IST)

    (payload,) = saved
    assert payload["build"] == bi.process_build_stamp()
    decided = datetime.fromisoformat(payload["decided_at"])
    assert decided.tzinfo is not None, "same aware-IST convention as this table's timestamp"
    assert before <= decided <= after
