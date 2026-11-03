"""Exercise the shipped RR renderer with real entry geometry and native ratios.

The read-only 2026-10-05 runtime snapshot contains a QP option signal with
entry 276, initial stop 259.44, target 324.852, and RR 2.95. A one-decimal
display collapses that real setup to 1:3.0. Trailing stops must not redefine
the initial RR, because its denominator remains the original entry risk.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "dashboard/static/js/trade-table.js"
NODE = shutil.which("node") or str(Path("C:/Program Files/nodejs/node.exe"))
DRIVER = r"""
const fs = require('fs');
const vm = require('vm');
const script = process.argv[1];
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
const context = {
    window: {},
    document: {addEventListener() {}},
    state: {latestData: {instruments: {}}},
};
vm.createContext(context);
vm.runInContext(fs.readFileSync(script, 'utf8'), context, {filename: script});
const row = input.collect
    ? context.collectSessionSignalRows([], [input.row])[0]
    : input.row;
process.stdout.write(JSON.stringify({
    resolved: context.resolveRiskReward(row),
    formatted: context.formatRiskReward(row),
    row,
}));
"""


def render(row, *, collect=False):
    result = subprocess.run(
        [NODE, "-e", DRIVER, str(SCRIPT)],
        input=json.dumps({"row": row, "collect": collect}),
        text=True, capture_output=True, check=True,
    )
    return json.loads(result.stdout)


@pytest.mark.parametrize("rr", [.7, 1., 1.5, 2., 3., 4.])
def test_renderer_retains_independent_native_ratios(rr):
    result = render({"rr": rr})
    assert result["resolved"] == rr
    assert result["formatted"] == f"1:{rr:.1f}"


@pytest.mark.parametrize("rr, label", [(2.95, "1:2.95"), (3.04, "1:3.04")])
def test_fractional_rr_is_not_displayed_as_three(rr, label):
    result = render({"rr": rr})
    assert result["formatted"] == label


def test_observed_runtime_geometry_is_295_not_fixed_three():
    row = {"price": 276., "initial_stop": 259.44,
           "target": 324.852, "rr": 2.95, "inst_type": "OPT"}
    geometry = (row["target"] - row["price"]) / (row["price"] - row["initial_stop"])
    assert geometry == pytest.approx(2.95)
    result = render(row)
    assert result["resolved"] == pytest.approx(geometry)
    assert result["formatted"] == "1:2.95"


def test_trailing_profit_stop_does_not_redefine_original_entry_risk():
    result = render({"entry_price": 100., "initial_stop": 90.,
                     "trailing_stop": 115., "target": 130., "rr_ratio": 3.})
    assert result["resolved"] == 3.
    assert result["formatted"] == "1:3.0"


def test_missing_metadata_can_resolve_from_original_entry_geometry():
    result = render({"entry_price": 100., "initial_stop": 90.,
                     "trailing_stop": 115., "target": 120.})
    assert result["resolved"] == 2.


def test_signal_row_collection_preserves_planned_rr_after_exit():
    result = render({"instrument": "MIDCPNIFTY", "inst_type": "OPT",
                     "price": 276., "initial_stop": 259.44,
                     "stop": 280., "target": 324.852, "rr": 2.95,
                     "action": "EXIT", "status": "EXIT SIGNAL",
                     "accepted_by_gate": True,
                     "timestamp": "2026-10-05T10:15:02+05:30",
                     "exit_timestamp": "2026-10-05T10:16:00+05:30"}, collect=True)
    assert result["row"]["target"] == 0
    assert result["resolved"] == 2.95
    assert result["formatted"] == "1:2.95"


def test_missing_rr_and_entry_geometry_displays_unavailable():
    result = render({"entry_price": 100.})
    assert result["resolved"] is None
    assert result["formatted"] == "--"
