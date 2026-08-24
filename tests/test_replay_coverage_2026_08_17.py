"""Replay coverage must travel with a backtest result, or the result gets over-read.

The incident this exists for: two installs were compared as

    current   87 trades   Rs. 64,358
    old      496 trades   Rs.303,767

and read as "the system got 5x worse". Three distortions, none visible in the output:

  1. The runs covered NON-OVERLAPPING windows — old 20-28 Jul, current 29 Jul-12 Aug.
     Replaying the CURRENT code across both gives +Rs.48,750 for the July window and
     -Rs.64 for the August one, on near-identical trade counts (137 vs 151). The market
     changed, not the code.
  2. The current install had 21 trading days of contract history against the old's 35.
  3. 639 of the old install's 812 contract files are HEADER-ONLY (38 bytes). Counting
     files instead of files-with-data made its coverage look 8x better than it was — and
     that wrong number then anchored the whole analysis for a while.

So the module counts contracts WITH DATA separately from raw files, and warns when the
window is too thin to be read as a verdict.
"""
import os

import pytest

from engine.replay_coverage import (
    THIN_COVERAGE_DAYS,
    coverage_warnings,
    measure_option_history,
)

HEADER = "timestamp,open,high,low,close,volume\n"


def _write(tmp_path, name, rows):
    d = tmp_path / "data_store" / "candles"
    d.mkdir(parents=True, exist_ok=True)
    body = HEADER + "".join(
        f"{day} 09:{15 + i:02d}:00,100,101,99,100,500\n" for i, day in enumerate(rows)
    )
    (d / name).write_text(body, encoding="utf-8")


# ── the counting error that anchored the wrong conclusion ────────────────────

def test_header_only_files_are_not_counted_as_coverage(tmp_path):
    """38-byte files with a header and no candles. Counting them made one install look
    8x better covered than it was."""
    _write(tmp_path, "NIFTY26AUG24350CE_1min.csv", ["2026-08-03", "2026-08-04"])
    d = tmp_path / "data_store" / "candles"
    for n in range(5):
        (d / f"EMPTY{n}_1min.csv").write_text(HEADER, encoding="utf-8")

    stats = measure_option_history(str(tmp_path))
    assert stats["files"] == 6
    assert stats["contracts_with_data"] == 1
    assert stats["header_only_files"] == 5
    assert stats["rows"] == 2


def test_header_only_files_are_reported(tmp_path):
    _write(tmp_path, "A_1min.csv", ["2026-08-03"])
    (tmp_path / "data_store" / "candles" / "B_1min.csv").write_text(HEADER, encoding="utf-8")
    joined = " ".join(coverage_warnings(str(tmp_path)))
    assert "header and no candles" in joined


# ── the thin-window warning ──────────────────────────────────────────────────

def test_a_thin_window_is_called_a_sample_not_a_verdict(tmp_path):
    days = [f"2026-08-{d:02d}" for d in range(3, 8)]      # 5 days, well under the floor
    _write(tmp_path, "NIFTY26AUG24350CE_1min.csv", days)
    joined = " ".join(coverage_warnings(str(tmp_path)))
    assert "SAMPLE" in joined
    assert "5 trading days" in joined
    assert "different window" in joined


def test_a_full_window_raises_no_thinness_warning(tmp_path):
    days = [f"2026-08-{d:02d}" for d in range(1, 29)]     # 28 days, above the floor
    _write(tmp_path, "NIFTY26AUG24350CE_1min.csv", days)
    joined = " ".join(coverage_warnings(str(tmp_path)))
    assert "SAMPLE" not in joined


def test_the_floor_sits_ABOVE_the_window_that_actually_misled():
    """The run that misled had 21 trading days. The first draft of this floor was 20 —
    just BELOW it — so the guard would have stayed silent on the exact case it exists
    for. A floor has to be above the incident it is meant to catch."""
    assert THIN_COVERAGE_DAYS > 21, "the guard would not fire on the incident it was written for"
    assert THIN_COVERAGE_DAYS <= 40, "so high that every real run is flagged is no signal"


def test_the_real_install_now_trips_the_warning():
    """This install has 21 trading days. It must be called a sample."""
    stats = measure_option_history(".")
    if stats["files"] == 0 or stats["trading_days"] >= THIN_COVERAGE_DAYS:
        pytest.skip("install coverage no longer thin")
    assert any("SAMPLE" in w for w in coverage_warnings("."))


def test_the_span_is_reported_so_two_runs_can_be_compared(tmp_path):
    """The root cause was two runs over windows that shared no days. You cannot see that
    without the span."""
    _write(tmp_path, "X_1min.csv", ["2026-07-20", "2026-07-28"])
    stats = measure_option_history(str(tmp_path))
    assert stats["first_day"] == "2026-07-20"
    assert stats["last_day"] == "2026-07-28"


# ── it must never break a session ────────────────────────────────────────────

def test_no_history_at_all_is_silent(tmp_path):
    """Not staging replay data is a different problem with its own handling."""
    assert coverage_warnings(str(tmp_path)) == []
    assert measure_option_history(str(tmp_path))["files"] == 0


def test_unreadable_content_does_not_raise(tmp_path):
    d = tmp_path / "data_store" / "candles"
    d.mkdir(parents=True, exist_ok=True)
    (d / "JUNK_1min.csv").write_bytes(b"\xff\xfe\x00not,a,csv\x00\x00")
    measure_option_history(str(tmp_path))          # must not raise
    coverage_warnings(str(tmp_path))


def test_a_probe_failure_is_swallowed(monkeypatch):
    import engine.replay_coverage as rc

    monkeypatch.setattr(rc, "measure_option_history",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    assert rc.coverage_warnings() == []


# ── wiring ───────────────────────────────────────────────────────────────────

def test_readiness_surfaces_it_next_to_the_calendar_warning():
    """Same family as the exchange-calendar warning: tell the operator while there is
    still time to act, rather than after the number has been believed."""
    import inspect

    import engine.safe_mode as sm

    src = inspect.getsource(sm)
    assert "from engine.replay_coverage import coverage_warnings" in src
    assert "warnings.extend(coverage_warnings())" in src


def test_this_install_is_measured():
    """Against the real data_store, not a fixture — the numbers quoted in the report."""
    stats = measure_option_history(".")
    if stats["files"] == 0:
        pytest.skip("no option history staged")
    assert stats["contracts_with_data"] <= stats["files"]
    assert stats["rows"] > 0
    assert stats["first_day"] <= stats["last_day"]
