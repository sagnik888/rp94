"""A buffered arrow needs a new, valid pass on its own timeframe.

Regression for the live failure where MultiTimeframeEngine reused its previous result
when a timeframe fetch was missing/short or when engine.process raised.  Scanner then
counted that old arrow as another stable observation and could mature it into AUTO.
"""

import ast
import inspect
import textwrap
import time
from datetime import datetime
from types import SimpleNamespace

import pandas as pd
import pytest

from engine.multi_timeframe import MultiTimeframeEngine
from scanner import Scanner


SIGNAL_TIME = datetime(2026, 8, 21, 10, 5)


def _frame(rows=20, *, last_close=120.0):
    index = pd.date_range("2026-08-21 09:15", periods=rows, freq="5min")
    values = [100.0 + i for i in range(rows)]
    if values:
        values[-1] = last_close
    return pd.DataFrame(
        {
            "open": values,
            "high": [v + 1.0 for v in values],
            "low": [v - 1.0 for v in values],
            "close": values,
            "volume": [1000.0] * rows,
        },
        index=index,
    )


def _signal():
    return SimpleNamespace(
        timestamp=SIGNAL_TIME,
        signal_type="BUY",
        bar_index=19,
    )


class _Engine:
    signal_mode = "realtime"

    def __init__(self):
        self.calls = 0
        self.fail = False

    def process(self, **_kwargs):
        self.calls += 1
        if self.fail:
            raise RuntimeError("indicator failed")
        return {
            "signals": [_signal()],
            "state": {"position": 1, "is_trending": True},
        }


def _mtf():
    engine = _Engine()
    mtf = MultiTimeframeEngine.__new__(MultiTimeframeEngine)
    mtf.engines = {"NIFTY_5min": engine}
    mtf._data_cache = {}
    mtf._result_cache = {}
    mtf._data_hash = {}
    mtf._input_metadata = {}
    mtf._result_metadata = {}
    mtf._fresh_result_required_after = {}
    mtf.tf_weights = {"1min": 0.15, "5min": 0.40, "15min": 0.45}
    return mtf, engine


@pytest.mark.parametrize("bad_input", [None, _frame(10)])
def test_missing_or_underfilled_timeframe_invalidates_the_old_arrow(bad_input):
    mtf, _engine = _mtf()
    mtf.update_data("NIFTY", "5min", _frame())
    first = mtf.process_instrument("NIFTY")
    assert first.results_5min["signals"]

    mtf.update_data("NIFTY", "5min", bad_input)
    second = mtf.process_instrument("NIFTY")

    assert second.results_5min is None
    assert "NIFTY_5min" not in mtf._result_cache
    assert second.timeframe_metadata["5min"]["status"] in {
        "input_missing",
        "input_underfilled",
    }
    assert second.timeframe_metadata["5min"]["input"]["version"] == 2


def test_processing_error_invalidates_instead_of_falling_back_to_old_result():
    mtf, engine = _mtf()
    mtf.update_data("NIFTY", "5min", _frame())
    assert mtf.process_instrument("NIFTY").results_5min is not None

    engine.fail = True
    mtf.update_data("NIFTY", "5min", _frame(last_close=121.0))
    failed = mtf.process_instrument("NIFTY")

    assert failed.results_5min is None
    assert "NIFTY_5min" not in mtf._result_cache
    assert failed.timeframe_metadata["5min"]["status"] == "processing_error"
    assert failed.timeframe_metadata["5min"]["result"]["computed_at_epoch"] == 0.0


def test_post_buffer_freshness_requirement_forces_same_hash_recomputation():
    mtf, engine = _mtf()
    frame = _frame()
    mtf.update_data("NIFTY", "5min", frame)
    first = mtf.process_instrument("NIFTY")
    first_computed = first.timeframe_metadata["5min"]["result"]["computed_at_epoch"]
    assert engine.calls == 1

    # Ordinary unchanged input is a legitimate analytical cache hit.
    mtf.update_data("NIFTY", "5min", frame)
    cached = mtf.process_instrument("NIFTY")
    assert engine.calls == 1
    assert cached.timeframe_metadata["5min"]["status"] == "cached"

    buffered_at_epoch = time.time()
    mtf.require_fresh_result(
        "NIFTY", "5min", newer_than_epoch=buffered_at_epoch
    )
    mtf.update_data("NIFTY", "5min", frame)
    fresh = mtf.process_instrument("NIFTY")

    assert engine.calls == 2
    assert fresh.timeframe_metadata["5min"]["status"] == "recomputed"
    assert fresh.timeframe_metadata["5min"]["result"]["computed_at_epoch"] > buffered_at_epoch
    assert fresh.timeframe_metadata["5min"]["result"]["computed_at_epoch"] > first_computed


def test_repaint_status_requires_provenance_newer_than_the_buffer():
    candidate = SimpleNamespace(
        instrument="NIFTY",
        timeframe="5min",
        direction="LONG",
        source_signal_type="BUY",
    )
    result = {"signals": [_signal()]}
    scanner = Scanner.__new__(Scanner)
    scanner._cached_results = {"NIFTY_5min": result}
    scanner._cached_result_metadata = {
        "NIFTY_5min": {
            "result_object": result,
            "result": {"status": "ready", "computed_at_epoch": 100.0},
        }
    }

    assert scanner._repaint_signal_status(
        candidate, SIGNAL_TIME, result_newer_than_epoch=99.0
    ) == "PRESENT"
    assert scanner._repaint_signal_status(
        candidate, SIGNAL_TIME, result_newer_than_epoch=100.0
    ) == "UNKNOWN"

    # Provenance from a different result object cannot bless a chart-only overwrite.
    scanner._cached_results["NIFTY_5min"] = {"signals": [_signal()]}
    assert scanner._repaint_signal_status(
        candidate, SIGNAL_TIME, result_newer_than_epoch=99.0
    ) == "UNKNOWN"


def _coordinate_tree():
    return ast.parse(textwrap.dedent(inspect.getsource(Scanner._coordinate_and_execute)))


def test_stabilization_passes_the_buffer_cutoff_into_the_repaint_read():
    calls = [
        node
        for node in ast.walk(_coordinate_tree())
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "_repaint_signal_status"
    ]
    assert calls
    assert any(
        keyword.arg == "result_newer_than_epoch"
        for call in calls
        for keyword in call.keywords
    ), "the metadata exists, but stabilization does not require it"


def test_unknown_freshness_waits_without_earning_a_stable_scan():
    unknown_branches = []
    for node in ast.walk(_coordinate_tree()):
        if not isinstance(node, ast.If):
            continue
        rendered = ast.dump(node.test)
        if "repaint_status" in rendered and "UNKNOWN" in rendered:
            unknown_branches.append(node)

    assert unknown_branches, "UNKNOWN has no explicit fail-closed stabilization path"
    assert any(
        isinstance(child, ast.Continue)
        for branch in unknown_branches
        for child in ast.walk(branch)
    ), "UNKNOWN falls through and can increment stable_scans"
