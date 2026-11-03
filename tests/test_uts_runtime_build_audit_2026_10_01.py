"""Source updates in the audited UTS execution path require a runtime reload."""
import pytest
from engine.runtime_build import capture_build, build_status


@pytest.mark.parametrize("name", [
    "engine/uts_lane.py", "engine/uts_repaint.py", "engine/execution_service.py",
    "engine/exit_cycle.py", "engine/trade_row_adapter.py", "engine/ut_smart_engine.py",
    "engine/s1_lane.py", "engine/candidate_selection.py", "engine/alt_scanners.py",
    "engine/risk_manager.py", "engine/market_regime.py", "engine/scanner_chart_mixin.py",
    "engine/scanner_dashboard.py", "engine/engine_lab.py", "data/sqlite_db.py", "main.py",
])
def test_uts_source_change_invalidates_loaded_build(tmp_path, name):
    source = tmp_path / name
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text("before")
    original = capture_build(tmp_path)
    source.write_text("changed contents")
    status = build_status(original, tmp_path)
    assert status["reload_required"]
    assert status["source_files_changed_since_import"] == [name]
