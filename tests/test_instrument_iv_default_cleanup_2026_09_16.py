"""The IV-rank history cleanup removes exactly the fabricated observations.

`_chain_iv_context` substituted 0.15 for a missing leg under a non-default source label,
which surfaced as `instrument_iv = 15.0` and was archived. The recording guard closed the
leak on 2026-07-24, but 44 fabricated entries remain across the four instruments. Real IV
for these indices runs ~8-13, so a 15.0 sits at the top of the distribution and pushes
every genuine reading's percentile DOWN -- measured at 2026-09-16 close: SENSEX 69 -> 92,
NIFTY 82 -> 100, BANKNIFTY 75 -> 93.

The cleanup must be surgical. Anything it removes beyond the fabricated set silently
rewrites the volatility routing history.
"""
import json

import pytest

from scripts.clean_instrument_iv_defaults import (
    FABRICATED_VALUE,
    LEAK_CLOSED_AFTER,
    percentile,
    plan,
)


@pytest.fixture
def history():
    return {
        "NIFTY": {
            "2026-06-29": 15.0,          # fabricated, pre-guard
            "2026-07-01": 10.61,
            "2026-07-24": 15.0,          # fabricated, on the boundary
            "2026-08-05": 15.0,          # a REAL 15.0 after the guard closed
            "2026-09-16": 13.44,
        },
        "SENSEX": {
            "2026-07-02": 12.27,
            "2026-09-16": 14.06,
        },
    }


def test_only_pre_guard_fifteens_are_planned_for_removal(history):
    drops = plan(history)
    assert drops == {"NIFTY": ["2026-06-29", "2026-07-24"]}


def test_the_boundary_date_is_inclusive(history):
    """2026-07-24 is the last day the leak could produce a value."""
    assert LEAK_CLOSED_AFTER == "2026-07-24"
    assert "2026-07-24" in plan(history)["NIFTY"]


def test_a_real_fifteen_after_the_guard_is_kept(history):
    """A genuine 15.0 is possible; only the pre-guard ones are fabricated."""
    assert "2026-08-05" not in plan(history)["NIFTY"]


def test_an_instrument_with_no_fabricated_values_is_untouched(history):
    assert "SENSEX" not in plan(history)


def test_nothing_but_the_exact_value_is_touched():
    near = {"NIFTY": {"2026-07-01": 15.01, "2026-07-02": 14.99, "2026-07-03": 15.0}}
    assert plan(near) == {"NIFTY": ["2026-07-03"]}
    assert FABRICATED_VALUE == 15.0


def test_an_already_clean_history_plans_nothing():
    assert plan({"NIFTY": {"2026-09-16": 13.44}}) == {}
    assert plan({}) == {}


def test_the_cleanup_is_idempotent(history):
    drops = plan(history)
    for instrument, days in drops.items():
        for day in days:
            history[instrument].pop(day)
    assert plan(history) == {}


def test_removal_raises_the_percentile_of_a_current_reading(history):
    """The harm being undone: fabricated highs understate a real reading's rank."""
    series = history["NIFTY"]
    latest = series["2026-09-16"]

    before = percentile([float(v) for v in series.values()], latest)
    drops = set(plan(history)["NIFTY"])
    after = percentile(
        [float(v) for d, v in series.items() if d not in drops], latest
    )

    assert after > before


def test_percentile_handles_an_empty_series():
    assert percentile([], 12.0) == 0.0


def test_the_script_refuses_while_the_scanner_is_running(tmp_path, monkeypatch, capsys):
    """The live process rewrites this file from memory about every 60s, so a clean
    written underneath it is reverted within the minute."""
    import scripts.clean_instrument_iv_defaults as cleanup

    target = tmp_path / "instrument_iv_history.json"
    target.write_text(json.dumps({"NIFTY": {"2026-06-29": 15.0}}), encoding="utf-8")

    monkeypatch.setattr(cleanup, "scanner_is_running", lambda: True)
    monkeypatch.setattr("sys.argv", ["cleanup", "--path", str(target)])

    assert cleanup.main() == 2
    assert "REFUSED" in capsys.readouterr().out
    assert json.loads(target.read_text(encoding="utf-8"))["NIFTY"]["2026-06-29"] == 15.0


def test_a_dry_run_never_writes(tmp_path, monkeypatch, capsys):
    import scripts.clean_instrument_iv_defaults as cleanup

    target = tmp_path / "instrument_iv_history.json"
    payload = {"NIFTY": {"2026-06-29": 15.0, "2026-09-16": 13.44}}
    target.write_text(json.dumps(payload), encoding="utf-8")

    monkeypatch.setattr(cleanup, "scanner_is_running", lambda: False)
    monkeypatch.setattr("sys.argv", ["cleanup", "--path", str(target), "--dry-run"])

    assert cleanup.main() == 0
    assert "dry run" in capsys.readouterr().out
    assert json.loads(target.read_text(encoding="utf-8")) == payload


def test_a_real_run_backs_up_and_cleans(tmp_path, monkeypatch):
    import scripts.clean_instrument_iv_defaults as cleanup

    target = tmp_path / "instrument_iv_history.json"
    payload = {"NIFTY": {"2026-06-29": 15.0, "2026-09-16": 13.44}}
    target.write_text(json.dumps(payload), encoding="utf-8")

    monkeypatch.setattr(cleanup, "scanner_is_running", lambda: False)
    monkeypatch.setattr("sys.argv", ["cleanup", "--path", str(target)])

    assert cleanup.main() == 0

    cleaned = json.loads(target.read_text(encoding="utf-8"))
    assert cleaned == {"NIFTY": {"2026-09-16": 13.44}}

    backups = list(tmp_path.glob("instrument_iv_history.json.bak-*"))
    assert len(backups) == 1
    assert json.loads(backups[0].read_text(encoding="utf-8")) == payload


def test_the_refusal_fails_safe_when_process_state_is_unknowable(monkeypatch):
    """Cannot tell -> assume running. Reverting a clean is harmless; racing a live
    writer and reporting success is not."""
    import scripts.clean_instrument_iv_defaults as cleanup

    def boom(*args, **kwargs):
        raise OSError("no powershell here")

    monkeypatch.setattr(cleanup.subprocess, "run", boom)
    assert cleanup.scanner_is_running() is True
