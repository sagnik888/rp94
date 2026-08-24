"""Settings that exist but are read by nothing.

A setting the operator writes into .env and the code never reads is worse than a missing
feature: it reads as a control that is working. `ut_correlated_core_limit=2` is the sharp
example — its own comment says "Maximum simultaneous same-direction NIFTY/BANKNIFTY/SENSEX
exposures", and the guard that enforces core concentration reads `_active_bucket_cap`
instead, whose live answer is 6. Three times the configured limit on the concentration
control that matters most as size grows.

Two things live here:

  * INERT is the audited list, each entry saying WHY it has no consumer. Anything on it
    that also appears in .env is reported at startup, so a dead knob cannot go on looking
    live.
  * tests/test_every_setting_has_a_consumer_2026_08_06.py fails when a settings field has
    no consumer and is not listed, so this list can only shrink by accident.

Nothing here changes behaviour. Wiring a setting up (or deleting it) is a decision about
live risk, and it belongs to the operator, not to a cleanup pass.
"""

from __future__ import annotations

# name -> why it is inert, and what actually governs the behaviour instead.
INERT: dict[str, str] = {
    # ── risk / sizing ────────────────────────────────────────────────────────
    "fut_force_fixed_lots": "No sizing path consults it; lots come from the risk sizer.",
    "heavy_trend_lot_multiplier_fut": "No sizing path consults it.",
    "api_max_trade_limit": "The trades API clamps with its own literal.",

    # The eleven market_intel_pullback_* / market_intel_loss_* settings that used to be
    # listed here were DELETED from settings.py on 2026-08-06 rather than documented: a
    # guard that was configured and never built is better removed than catalogued. The
    # loss-guard family does that job.

    # ── entry execution pricing ──────────────────────────────────────────────
    "option_limit_buffer_pct": "Entry limit pricing does not read it.",
    "option_limit_buffer_min_ticks": "Entry limit pricing does not read it.",
    "fut_entry_protection_limit_enabled": "No futures entry-protection limit path exists.",
    "fut_limit_buffer_bps": "Named in a settings.py comment describing a path that was never built.",
    "entry_pre_submit_lot_validation_timeout_seconds": "No pre-submit lot validation exists.",
    "entry_margin_cache_seconds": (
        "Has a field_validator but no behavioural consumer: the margin preflight reserves "
        "per entry on its own TTL. A validator checks a value nothing goes on to use."
    ),

    # ── exits ────────────────────────────────────────────────────────────────
    "rupee_loss_band_freefall_only": "The rupee loss band applies regardless of freefall.",
    "tight_trailing_timeframe": "Trailing tightness is chosen per trade, not per timeframe.",

    # ── background worker pools (sized by literals at their call sites) ──────
    "contract_ltp_background_workers": "Pool size is a literal at the call site.",
    "contract_ltp_background_retry_cooldown_seconds": "Retry cooldown is a literal.",
    "contract_ltp_background_result_limit": "The result cap is a literal at the call site.",
    "contract_ltp_cache_max_age_seconds": "Contract LTP freshness uses its own literal.",
    "index_ltp_cache_max_age_seconds": "Index LTP freshness uses its own literal.",
    "fyers_chain_background_workers": "Pool size is a literal at the call site.",
    "option_chain_background_workers": "Pool size is a literal at the call site.",
    "option_chain_background_retry_cooldown_seconds": "Retry cooldown is a literal.",
    "dynamic_subscription_background_workers": "Pool size is a literal at the call site.",

    # ── declared, superseded, or plumbing ────────────────────────────────────
    "outcome_calibration_enabled": (
        "OutcomeCalibrationStore is constructed unconditionally; this cannot switch it "
        "off. Harmless while you want it on."
    ),
    "ut_fractal_confluence": "No fractal-confluence filter exists.",
    "pcr_near_strikes": "Near-money PCR uses its own strike window.",
    "repaint_check_interval_minutes": "Labelled legacy/unused in settings.py itself.",
    "last_minute_extension_seconds": "No consumer; the session end reads ut_force_exit_time.",
    "settings_persistence_debounce_seconds": "Persistence debounce uses its own literal.",
    "kite_default_product": "product_type is passed explicitly on every order.",
    "angelone_secret_key": "AngelOne is retired; nothing constructs a SmartConnect client.",
    "fyers_login_id": "Fyers auth uses the token file.",
}


def configured_but_inert(env_keys) -> list[tuple[str, str]]:
    """Inert settings the operator has actually written into .env, with reasons."""
    present = {str(k or "").strip().lower() for k in env_keys}
    return sorted((name, why) for name, why in INERT.items() if name in present)


def env_keys_from_file(path: str = ".env") -> list[str]:
    keys: list[str] = []
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as handle:
            for line in handle:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    keys.append(line.split("=", 1)[0].strip().lower())
    except OSError:
        return []
    return keys
