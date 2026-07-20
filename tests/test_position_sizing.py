from engine.position_sizing import lots_from_risk_budget


def test_lots_from_risk_budget_blocks_when_one_lot_exceeds_budget():
    result = lots_from_risk_budget(
        requested_lots=1,
        lot_size=50,
        risk_budget=1000.0,
        per_unit_risk=25.0,
    )

    assert result.lots == 0
    assert "exceeds budget" in result.reason


def test_lots_from_risk_budget_caps_requested_lots():
    result = lots_from_risk_budget(
        requested_lots=5,
        lot_size=25,
        risk_budget=2500.0,
        per_unit_risk=20.0,
        cap_lots=4,
    )

    assert result.lots == 4
    assert result.risk_per_lot == 500.0


def test_lots_from_risk_budget_respects_position_cap():
    result = lots_from_risk_budget(
        requested_lots=5,
        lot_size=25,
        risk_budget=10000.0,
        per_unit_risk=20.0,
        cap_lots=2,
    )

    assert result.lots == 2
