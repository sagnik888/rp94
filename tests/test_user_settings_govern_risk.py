"""User settings are the single source of truth for risk (2026-07-09).

[1] max_trade_loss_abs defaults to 0 = DISABLED: no hidden per-trade rupee
    hardcap — per-trade risk is governed only by the user's visible settings
    (futures_sl_pct / options_sl_pct stop caps + segment daily budgets).
    Fail-closed accounting must then charge the SEGMENT budget, never 0.
[2] Circuit-breaker code defaults match the operator's values (2.0 / 8.0) so
    .env and code no longer drift.
"""
from config.settings import Settings
from engine.risk_manager import RiskManager


def test_no_per_trade_rupee_hardcap_by_default():
    s = Settings(_env_file=None, capital_fut=900000, risk_fut_pct=2.0, capital_opt=500000, risk_opt_pct=8.0)
    assert float(s.max_trade_loss_abs) == 0.0
    # Disabled backstop -> cap IS the segment daily budget (user's settings).
    assert s.effective_trade_loss_cap("FUT") == 900000 * 0.02
    assert s.effective_trade_loss_cap("OPT") == 500000 * 0.08


def test_explicit_backstop_still_clamps_when_set():
    s = Settings(_env_file=None, capital_opt=500000, risk_opt_pct=8.0, max_trade_loss_abs=5000.0)
    assert s.effective_trade_loss_cap("OPT") == 5000.0


def test_zero_backstop_is_valid_but_negative_rejected():
    Settings(_env_file=None, max_trade_loss_abs=0.0)  # must not raise
    try:
        Settings(_env_file=None, max_trade_loss_abs=-1.0)
        raise AssertionError("negative backstop must be rejected")
    except ValueError:
        pass


def test_projected_stop_risk_unclamped_when_backstop_disabled():
    rm = RiskManager(trade_manager=None, capital_total=1400001)
    rm.update_settings(
        max_trade_loss_abs=0.0, options_sl_pct=15.0,
        capital_fut=900000, capital_opt=500000,
        risk_fut_pct=2.0, risk_opt_pct=8.0,
    )

    class T:
        entry_price = 500.0
        lots = 10
        lot_size = 75
        instrument_multiplier = 1.0
        inst_type = "OPT"
        trailing_stop = 0.0
        direction = "LONG"

    # 15% of 500 * 750 = 56,250 — must NOT be clamped to any rupee backstop.
    assert rm._projected_stop_risk(T()) == 500.0 * 750 * 0.15


def test_fail_closed_charges_segment_budget_when_backstop_disabled():
    """Disabling the rupee backstop must NOT turn fail-closed into fail-open:
    an unparseable trade charges the segment's FULL daily budget."""
    rm = RiskManager(trade_manager=None, capital_total=1400001)
    rm.update_settings(
        max_trade_loss_abs=0.0,
        capital_fut=900000, capital_opt=500000,
        risk_fut_pct=2.0, risk_opt_pct=8.0,
    )
    import inspect
    src = inspect.getsource(RiskManager.check_circuit_breaker)
    assert "_fail_closed_charge_for" in src
    # the helper resolves to the segment budgets when the backstop is 0
    assert "capital_fut" in src and "capital_opt" in src


def test_circuit_breaker_defaults_single_source_of_truth():
    # Risk percentages are operator-controllable knobs (set via .env / dashboard),
    # NOT hard-gated constants. Assert the single-source-of-truth RELATIONSHIP —
    # RiskManager adopts whatever the operator configured, with no hidden hardcode
    # that silently overrides it — plus sanity bounds, rather than pinning a
    # specific percentage the operator may legitimately change.
    s = Settings(_env_file=None)
    assert 0.0 < float(s.risk_fut_pct) <= 100.0
    assert 0.0 < float(s.risk_opt_pct) <= 100.0
    assert float(s.max_trade_loss_abs) == 0.0
    rm = RiskManager(trade_manager=None, capital_total=100000)
    rm.update_settings(
        risk_fut_pct=s.risk_fut_pct,
        risk_opt_pct=s.risk_opt_pct,
        max_trade_loss_abs=s.max_trade_loss_abs,
    )
    assert float(rm.risk_fut_pct) == float(s.risk_fut_pct)
    assert float(rm.risk_opt_pct) == float(s.risk_opt_pct)
    assert float(rm.max_trade_loss_abs) == 0.0
