"""The dashboard must distinguish estimated pressure from proven manipulation."""
import json
from pathlib import Path
import re
import shutil
import subprocess

import pytest

from intelligence.intelligence_aggregator import IntelligenceAggregator
from tests.test_intelligence_data_truth import candles


def analyze(flow, change=.2):
    aggregator = IntelligenceAggregator()
    aggregator.order_flow.analyze = lambda *args, **kwargs: flow
    return aggregator.analyze(instrument="NIFTY", timeframe="5min", candle_df=candles(),
                              options_chain=None, spot_price=110., price_change_pct=change)


@pytest.mark.parametrize("flow,change", [
    ({"signal": "BEARISH", "ratio": .5, "available": False}, .2),
    ({"signal": "BEARISH", "ratio": .5, "source": "unavailable"}, .2),
    ({"signal": "BULLISH", "ratio": float("inf"), "available": True}, -.2),
    ({"signal": "BEARISH", "ratio": -1., "available": True}, .2),
    ({"signal": "BEARISH", "ratio": None, "available": True}, .2),
])
def test_unavailable_or_invalid_pressure_cannot_trigger_warning_or_vote(flow, change):
    actual = analyze(flow, change)
    assert actual["divergence_alert"] is False
    assert actual["divergence"]["available"] is False
    neutral = analyze({**flow, "ratio": 1.}, change)
    assert actual["aggregate"]["score_before_regime_dampening"] == neutral["aggregate"]["score_before_regime_dampening"]


@pytest.mark.parametrize("ratio,change,signal", [(.5, .2, "BEARISH"), (1.5, -.2, "BULLISH")])
def test_valid_divergence_remains_visible_with_honest_provenance(ratio, change, signal):
    result = analyze(dict(signal=signal, ratio=ratio, available=True, source="kite_depth"), change)
    assert result["divergence_alert"] is True
    assert result["divergence"]["available"] is True
    assert result["divergence"]["pressure_source"] == "kite_depth"
    assert result["candle_pressure"]["source"] == "kite_depth"


def test_frontend_warning_is_conditional_and_never_claims_manipulation():
    node = shutil.which("node")
    assert node, "Node is required for the dashboard rendering regression"
    source = Path("dashboard/static/js/app.js").read_text(encoding="utf-8")
    match = re.search(r"function updateDivergenceAlert\(intel\) \{.*?\n\}", source, re.S)
    assert match, "Expected isolated warning renderer"
    script = r'''
const assert = require('assert');
const element = {style: {}, textContent: '', title: ''};
const document = {getElementById: () => element};
RENDERER
const good = {divergence_alert: true, order_flow: {available: true, ratio: .5, source: 'ohlcv_close_position'}};
updateDivergenceAlert(good);
assert.equal(element.style.display, 'block');
assert.equal(element.textContent, 'PRESSURE DIVERGENCE');
assert(element.title.includes('estimated'));
for (const input of [
 {}, {...good, divergence_alert: false}, {...good, divergence_alert: 'false'},
 {...good, order_flow: {available: false, ratio: .5}},
 {...good, order_flow: {source: 'unavailable', ratio: .5}},
 {...good, order_flow: {available: true, ratio: null}},
 {...good, order_flow: {available: true, ratio: Infinity}},
 {...good, order_flow: {available: true, ratio: -.1}}
]) {
 updateDivergenceAlert(input);
 assert.equal(element.style.display, 'none');
 assert.equal(element.title, '');
}
'''.replace("RENDERER", match.group(0))
    subprocess.run([node, "-e", script], text=True, capture_output=True, check=True)
    assert "updateDivergenceAlert(intel);" in source
    html = Path("dashboard/static/index.html").read_text(encoding="utf-8")
    assert '>⚠️ MANIPULATION</div>' not in html
