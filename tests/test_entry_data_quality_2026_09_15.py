from types import SimpleNamespace

from engine.entry_data_quality import evaluate_entry_data_quality
from scanner import Scanner


def _cash_volume():
    return {
        "volume_available": True,
        "volume_reliable": True,
        "volume_comparable": True,
    }


def _chain():
    return {"intelligence_usable": True, "stale": False, "fallback": False}


def test_special_entry_without_live_cash_volume_is_advisory_only():
    verdict = evaluate_entry_data_quality(
        live=True, special_entry=True, one_minute_volume={}, chain_quality=_chain()
    )

    assert not verdict["allowed"]
    assert verdict["advisory_only"]
    assert verdict["reason_code"] == "ONE_MINUTE_VOLUME_UNAVAILABLE"


def test_option_special_entry_requires_measured_contract_evidence():
    verdict = evaluate_entry_data_quality(
        live=True,
        special_entry=True,
        one_minute_volume=_cash_volume(),
        chain_quality=_chain(),
        option_contract=True,
    )

    assert not verdict["allowed"]
    assert verdict["reason_code"] == "OPTION_CONTRACT_EVIDENCE_UNAVAILABLE"


def test_complete_live_option_evidence_is_entry_ready():
    verdict = evaluate_entry_data_quality(
        live=True,
        special_entry=True,
        one_minute_volume={"available": True, "reliable": True, "comparable": True},
        chain_quality=_chain(),
        option_contract=True,
        strike_oi_available=True,
        strike_volume_available=True,
        strike_liquidity_available=True,
    )

    assert verdict["allowed"]
    assert verdict["reason_code"] == "READY"


def test_ordinary_entry_does_not_turn_a_volume_outage_into_a_global_stand_aside():
    verdict = evaluate_entry_data_quality(
        live=True, special_entry=False, one_minute_volume={}, chain_quality={}
    )

    assert verdict["allowed"]
    assert verdict["reason_code"] == "ORDINARY"


def test_timeout_blocks_every_new_entry_until_a_fresh_pass_lands():
    verdict = evaluate_entry_data_quality(
        live=True,
        special_entry=False,
        scan_block_reason="live index analysis timed out after 9.0s",
    )

    assert not verdict["allowed"]
    assert verdict["reason_code"] == "STALE_SCAN"


def test_scanner_marks_a_plus_option_with_missing_strike_data_unready():
    candidate = SimpleNamespace(
        instrument="NIFTY",
        grade="A+",
        market_ltp_shock_entry=False,
        volume_spike_entry=False,
        chain_quality=_chain(),
        inst_type="OPT",
        strike_oi_available=False,
        strike_volume_available=False,
        strike_liquidity_available=False,
    )
    scanner = SimpleNamespace(
        mode="REAL",
        _live_entry_data_blocks={},
        candles=SimpleNamespace(get_candles=lambda *_: None),
        _prediction_volume_context=lambda *_: _cash_volume(),
    )

    verdict = Scanner._live_entry_data_quality(scanner, candidate)

    assert not verdict["allowed"]
    assert verdict["reason_code"] == "OPTION_CONTRACT_EVIDENCE_UNAVAILABLE"


def test_final_live_gate_blocks_shock_when_current_cash_volume_is_missing(monkeypatch):
    """The final gate must stop an override before any override path can admit it."""
    monkeypatch.setattr(Scanner, "_auto_entry_resource_blockers", staticmethod(lambda _s: []))
    monkeypatch.setattr(Scanner, "_session_bias_standaside", staticmethod(lambda *_: (False, "")))
    monkeypatch.setattr(Scanner, "_unanimous_book_blocker", staticmethod(lambda *_: ""))
    monkeypatch.setattr(
        Scanner, "_candidate_quality_context", staticmethod(lambda *_: ("TRENDING_UP", False, False, False))
    )
    scanner = SimpleNamespace(
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
        timeframe="1min",
        inst_type="OPT",
        grade="B+",
        confidence=0.8,
        market_ltp_shock_entry=True,
        volume_spike_entry=False,
        chain_quality=_chain(),
        strike_oi_available=True,
        strike_volume_available=True,
        strike_liquidity_available=True,
        reasons=[],
    )

    assert Scanner._passes_live_trade_ready_gate(scanner, candidate) is False
    assert candidate.entry_data_quality["reason_code"] == "ONE_MINUTE_VOLUME_UNAVAILABLE"
