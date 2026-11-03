from engine.live_session_metrics import LiveSessionMetrics


def test_live_session_metrics_reports_required_tail_and_skip_evidence():
    metrics = LiveSessionMetrics()
    for value in (10, 20, 30, 40, 50):
        metrics.record_cycle(
            value,
            analysis_recomputed=value >= 40,
            skip_reason="inputs unchanged",
            timeout_count=2,
            bulkheads={"option_chain:NIFTY": {"state": "OPEN", "inflight": 1}},
        )
    for value in (1, 2, 3):
        metrics.record_exit_tick(value)

    result = metrics.snapshot()

    assert result["cycle_ms"]["p50"] == 30
    assert result["cycle_ms"]["p95"] == 48
    assert result["analysis"]["skip_ratio"] == 0.6
    assert result["exit_tick_ms"]["p95"] == 2.9
    assert result["timeout_count"] == 2
    assert result["saturated_bulkheads"] == 1
    assert result["full_session_evidence"] is False
