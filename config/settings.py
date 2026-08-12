"""
UT1 Index Trading System — Configuration
Pydantic-based settings with .env support
"""

import json
import dotenv
from pathlib import Path
from typing import Literal, Optional, Dict, Any, List
from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def _setting_enabled(value, default: bool = True) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return value != 0
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "y", "on"}:
        return True
    if text in {"0", "false", "no", "n", "off", ""}:
        return False
    return bool(value)


PROTECTED_SECRET_ENV_KEYS = {
    "ANGELONE_API_KEY",
    "ANGELONE_PASSWORD",
    "ANGELONE_TOTP_SECRET",
    "ANGELONE_SECRET_KEY",
    "FYERS_SECRET_KEY",
    "FYERS_PASSWORD",
}


def _set_env_key_with_retry(env_key: str, value: str, attempts: int = 4) -> bool:
    """Write one .env key, tolerating a transient Windows file lock.

    python-dotenv writes a temp file and os.replace()s it over .env. On Windows that
    fails outright with "[WinError 5] Access is denied" whenever anything else holds a
    handle — an antivirus scan or an open editor is enough. The lock is momentary, so a
    short backoff clears it, where the single attempt simply dropped the setting.
    """
    import time as _time

    delay = 0.15
    for attempt in range(max(1, attempts)):
        try:
            dotenv.set_key('.env', env_key, value)
            return True
        except (PermissionError, OSError) as exc:
            if attempt == attempts - 1:
                print(
                    f"Could not persist {env_key} to .env after {attempts} attempts: {exc}. "
                    "Close any editor or sync tool holding the file."
                )
                return False
            _time.sleep(delay)
            delay *= 2
    return False


def _clean_env_secret(value: Any) -> str:
    return str(value or "").strip().strip("'").strip('"')


class Settings(BaseSettings):
    """Main application settings loaded from .env"""

    # ─── AngelOne API ────────────────────────────────────────
    # ── Zerodha Kite Connect (primary broker) ────────────────────────────────
    # Kite has no password login: the account password is never stored or used by
    # this system. A browser login issues a request_token, which is exchanged for an
    # access_token that expires every morning (~06:00-07:30 IST), so a manual login
    # is required once per trading day via /api/kite_login.
    kite_api_key: str = ""
    kite_api_secret: str = ""
    # Kite checks the TCP source address against the app's allowed-IP list and refuses
    # EVERY order from an address that is not on it, while READS keep working — so it
    # presents as "the strategy stopped trading", not as a broken connection.
    #
    # This machine has both address families and api.kite.trade resolves to IPv6 first,
    # so orders were leaving on the IPv6 address (2026-08-04, three signals dead). An
    # IPv6 egress is normally a rotating privacy address, so it cannot be whitelisted in
    # any durable way. Pinning Kite to IPv4 leaves exactly one address to manage.
    kite_force_ipv4: bool = True
    # The IPv4 address whitelisted on the Kite developer console. Set it and the readiness
    # gate warns the moment this connection's real egress drifts off it — BEFORE a signal
    # dies. Leave blank to skip the check (correct if the console's IP list is empty,
    # which is the right setup for a dynamic home connection).
    kite_registered_ip: str = ""
    # Optional proxy for Kite REST calls ONLY (orders, margins, order book). Kite checks
    # the source address on order PLACEMENT, so this is the one path that needs a stable
    # address — point it at a host with a fixed IP and whitelist that IP instead of a
    # home connection that keeps moving. The tick WebSocket deliberately stays direct, so
    # market data does not take an extra hop; the added latency lands only on order
    # submission, where a few ms is irrelevant.
    # Any form requests understands: http://host:3128, socks5h://host:1080 (needs PySocks).
    # Blank = direct, unchanged behaviour.
    kite_http_proxy: str = ""
    kite_user_id: str = ""
    # Where the daily access token is cached. Kept out of .env because it rotates.
    kite_token_file: str = "kite_token.json"
    # Kite's redirect target must match the one registered on the app at
    # developers.kite.trade. The dashboard serves the landing route itself.
    kite_redirect_uri: str = ""
    # NRML = overnight/carryforward, MIS = intraday auto-squareoff. The system's
    # product_type strings are broker-neutral and mapped in KiteBroker.
    kite_default_product: str = "NRML"
    # Permanent postback hostname, if you have one (a NAMED Cloudflare tunnel on a
    # domain you control). Set it and scripts/start_postback_tunnel.py verifies that
    # URL instead of minting a throwaway quick-tunnel hostname that changes every
    # restart and has to be re-registered at Zerodha by hand each time.
    kite_postback_url: str = ""
    # Entries go in at the market by default. A LIMIT that does not fill is a missed
    # signal, and the cancel/replace path already converts a stale limit to MARKET
    # after ~12s — so a limit mostly buys a 12-second delay and an extra failure mode.
    # Slippage is bounded instead by market_protection on the order itself.
    #
    # A LIMIT is used only when the signal clears this confidence bar, where waiting
    # for a price is worth the risk of not being filled at all. 100 disables limits
    # entirely; 0 makes every priced entry a limit (the old behaviour).
    # 0-1, like every other confidence floor here (trade.confidence is a fraction and is
    # printed as {confidence:.0%}). This shipped as 95.0, which no confidence could ever
    # reach, so the LIMIT entry path was dead and every order went out MARKET. Values
    # above 1 are still accepted and read as a percentage.
    entry_limit_confidence_threshold: float = 0.95

    # Server-side life span for entry LIMIT orders, in minutes. 0 disables it.
    #
    # This is a BACKSTOP, not the primary mechanism: broker_cancel_replace_enabled
    # already cancels a stale limit after ~12s and re-sends it as MARKET, which
    # actually gets the position taken — TTL only cancels. Set well beyond that
    # window so it never races the replace logic. Its job is the case the replace
    # logic cannot cover: if this process dies while a limit rests, Zerodha expires
    # the order instead of leaving it to fill hours later at a stale price.
    kite_entry_limit_ttl_minutes: int = 5

    angelone_api_key: str = ""
    angelone_client_id: str = ""
    angelone_password: str = ""
    angelone_totp_secret: str = ""
    angelone_secret_key: str = ""
    # The public IP whitelisted on your SmartAPI app (smartapi.angelone.in ->
    # My Profile -> app -> Redirect/Whitelisted IP). AngelOne validates the TCP
    # source IP and refuses EVERY order with AG7002 when it does not match, so a
    # dynamic ISP address silently kills a whole session. Set this and the runtime
    # watchdog warns the moment the machine's real egress IP drifts off it.
    # Leave blank to disable the check.

    # ─── Fyers API ───────────────────────────────────────────
    fyers_app_id: str = "6P37DKRJG6-100"
    fyers_secret_key: str = ""
    fyers_login_id: str = ""
    fyers_password: str = ""
    fyers_redirect_uri: str = "http://127.0.0.1:8001/"

    # ─── Trading Mode ────────────────────────────────────────
    trading_mode: Literal["HISTORICAL", "REAL"] = "HISTORICAL"
    strategy_revision: str = "ut1-current"
    strategy_direction_mode: Literal["LONG_SHORT", "LONG_ONLY", "SHORT_ONLY"] = "LONG_SHORT"
    # Grade each candidate with its OWN timeframe's intelligence/regime. Today a single
    # 5-minute snapshot grades every candidate, so 15-minute signals are scored on
    # 5-minute intelligence. DEFAULT OFF because enabling it shifts grades and
    # confidences system-wide — A/B it against archived sessions before turning it on.
    per_timeframe_grading_enabled: bool = False
    active_indices: list[str] = ["NIFTY", "BANKNIFTY", "SENSEX", "MIDCPNIFTY"]
    # Indices that must NEVER trade futures (route to options instead). Was set in
    # .env but never read by any code — so MIDCPNIFTY was reaching the FUT stop
    # gate and being rejected. Now enforced in _resolve_instrument_type.
    auto_futures_blocked_indices: list[str] = ["MIDCPNIFTY"]
    # Lean instrument routing toward options: send the loss-prone marginal futures
    # cases (choppy chop, ordinary catch-all) to defined-risk options instead.
    # Genuine FUT cases (high IV, high spot vol, 0DTE-late) are preserved.
    lean_routing_to_options: bool = True
    # Slow-grind trend routing: a real but low-thrust directional move (trending
    # regime, modest ADX, compressed per-bar ATR%) earns less on an option's delta
    # than it loses to theta+spread; route it to linear futures instead of letting
    # the lean catch-all send it to options. Aligned-trend FUT passes the regime
    # veto and uses the trend stop tolerance.
    slow_grind_futures_routing_enabled: bool = True
    slow_grind_min_adx: float = 18.0
    slow_grind_max_atr_pct: float = 0.38
    # ── Structural price-action patch (separate, A/B-toggleable) ──
    # Adds S/R + swing-structure + candle-pattern + volume-profile awareness.
    # DEFAULT OFF: enable to A/B-test P&L, keep if it helps, else remove cleanly.
    # block_adverse=False -> advisory (telemetry only); True -> rejects ADVERSE setups.
    price_action_module_enabled: bool = False
    price_action_block_adverse: bool = False
    price_action_adverse_threshold: float = -0.50
    # Day-type-aware premium/discount (2026-07-10): on a CONFIRMED aligned trend
    # day (effective phase, >= min confidence) the "chasing the extreme" penalty
    # is scaled by trend_scale — continuation entries at the top (bottom) of the
    # range on trend days were being blocked blind. Range days keep the full
    # penalty. Set scaled=False to restore day-type-blind behaviour exactly.
    price_action_premium_trend_scaled: bool = True
    price_action_premium_trend_scale: float = 0.5
    price_action_premium_trend_min_confidence: float = 0.70
    post_exit_reversion_enabled: bool = False
    # Reversion scalp window after exit/distress (minutes). Counter-trend 1min
    # signals inside the window trade the snap-back with RR >= min_rr, tight stop,
    # and scalp-lock; the index cooldown is bypassed for them only.
    post_exit_reversion_window_minutes: float = 3.0
    # RR floor for reversion / snapback scalps (aligned with 1m spike scalp RR 1.5).
    post_exit_reversion_min_rr: float = 1.5
    # Also open the window after stop-loss / early-loss style exits (not only wins).
    post_exit_reversion_include_stop_loss: bool = True
    # Open window when exit reason mentions OI/PCR/regime/flow reverse pressure.
    post_exit_reversion_include_oi_pcr: bool = True
    # While an open trade is underwater and tape reverses against it, arm window.
    post_exit_reversion_open_distress_enabled: bool = True
    # Only arm SNAPBACK after 5min/15min trades (not continuous 1min scalps).
    post_exit_reversion_source_timeframes: List[str] = Field(
        default_factory=lambda: ["5min", "15min"]
    )
    # Soft loss band (₹): 0 → -soft (immediate reverse while still shallow).
    post_exit_reversion_loss_band_soft_rs: float = 1000.0
    # Deep loss band (₹): -soft → -deep (fast opposite move + reverse regime).
    post_exit_reversion_loss_band_deep_rs: float = 2000.0
    # Size-of-event gate (OR logic — either band qualifies):
    #   fast: >= 0.40% over ~5 minutes
    #   slow: >= 0.50% over ~10 minutes
    # Micro-chop after tiny exits does not open SNAPBACK.
    post_exit_reversion_fast_move_pct: float = 0.40
    post_exit_reversion_fast_lookback_minutes: float = 5.0
    post_exit_reversion_slow_move_pct: float = 0.50
    post_exit_reversion_slow_lookback_minutes: float = 10.0
    # Legacy aliases (kept for config UI / older saves; dual gate is authoritative).
    post_exit_reversion_min_move_pct: float = 0.40
    post_exit_reversion_move_lookback_minutes: float = 5.0
    # Pure SNAPBACK bare-min data confirm (below full SPIKE floors). Ensures the
    # 1min counter-signal is tape-backed, not a naked UT1 flip.
    post_exit_reversion_min_volume_ratio: float = 1.5
    post_exit_reversion_min_range_atr_ratio: float = 1.0
    post_exit_reversion_min_close_location: float = 0.55
    # Rare dual TF fill: when 1m (spike/snapback) AND 5m/15m are both excellent
    # same-direction, allow up to 2 entries (second usually OPT; never 2x FUT).
    rare_dual_tf_entry_enabled: bool = True
    rare_dual_tf_min_grade: str = "A"
    rare_dual_tf_min_confidence: float = 0.70
    rare_dual_tf_prefer_opt_second: bool = True
    price_action_reversal_relief_enabled: bool = True # Confirmed reversals can override into-S/R vetoes
    # Structure robustness + LuxAlgo-style context filters (only active when the
    # module above is enabled). swing_strength is the confirmed fractal half-width
    # (was a noisy 2). premium_discount penalises chasing the extreme of the swing
    # range; liquidity penalises entering into an equal-highs/lows stop-hunt pool.
    # All CONTEXT-ONLY (adjust the price-action score/verdict), never entry triggers.
    price_action_swing_strength: int = 5
    price_action_premium_discount_enabled: bool = True
    price_action_liquidity_enabled: bool = True

    # ─── Risk Settings ───────────────────────────────────────
    # Absolute backstop for rare burst mode: normal scanner capacity is 4 indices x
    # 2 trades, while a guarded all-index impulse can temporarily stretch to 4 x 3.
    max_concurrent_positions: int = 12
    # Per-bucket concurrent caps. NIFTY/BANKNIFTY/SENSEX share CORE; MIDCPNIFTY is
    # independent. The concurrency toggle now changes strictness, not normal capacity:
    # normal -> CORE 6 + MIDCAP 2 = 8; rare-burst guard-off cap -> CORE 9 + MIDCAP 3.
    core_bucket_max_positions: int = 6
    core_bucket_max_positions_guarded: int = 6
    midcap_bucket_max_positions: int = 2
    midcap_bucket_max_positions_guarded: int = 2
    rare_burst_core_bucket_max_positions: int = 9
    rare_burst_midcap_bucket_max_positions: int = 3
    max_concurrent_trades_per_index: int = 2
    rare_burst_max_concurrent_trades_per_index: int = 3
    rare_burst_third_trade_enabled: bool = True
    rare_burst_min_confidence: float = 0.95
    rare_burst_min_volume_ratio: float = 3.5
    rare_burst_min_adx: float = 35.0
    rare_burst_min_intel_score: float = 0.70
    rare_burst_min_breadth_votes: int = 3
    rare_burst_require_hourly_anchor: bool = True
    rare_burst_require_session_bias: bool = True
    rare_burst_block_range_phase: bool = True
    max_daily_loss_pct: float = 3.0
    capital_total: float = 1100000.0   # Total Demat Capital (Base for Daily Loss)
    
    # Futures Specific
    capital_fut: float = 750000.0   # Shared futures capital across active indices
    risk_fut_pct: float = 2.0        # FUT Daily Max Loss Limit (%) — circuit breaker ONLY (single source of truth with .env)
    # Absolute ceiling for the regime-selected FUT stop tolerance (e.g. 0.10% of LTP).
    # This is the traded stop policy — not a lot-size hardcap.
    futures_sl_pct: float = 0.15
    fut_force_fixed_lots: bool = False # Always trade 1 lot for Futures
    fut_cost: float = 200.0         # Transaction cost per Futures trade
    
    # Options Specific
    capital_opt: float = 300000.0   # Shared options capital across active indices
    risk_opt_pct: float = 8.0        # OPT daily loss budget on option capital (%) — circuit breaker ONLY (single source of truth with .env)
    options_sl_pct: float = 15.0    # OPT per-trade premium stop cap (%)

    # ─── Lot sizing policy ───────────────────────────────────
    # DEFAULT OFF: always trade the configured dashboard/user lot size for both
    # FUT and OPT. Daily risk % and max_trade_loss_abs do NOT shrink lots.
    # Stops remain futures_sl_pct / options_sl_pct from settings.
    auto_risk_sizing_enabled: bool = False
    entry_pre_submit_lot_validation_timeout_seconds: float = 10.0
    # Optional research mode only (when auto_risk_sizing_enabled=True):
    risk_per_trade_fut_pct: float = 0.0
    risk_per_trade_opt_pct: float = 0.0
    risk_per_trade_daily_fraction: float = 0.5
    # ── Lot scale policy ─────────────────────────────────────────────
    # Default: always 1× configured lots (user_lots / user_lots_fut).
    # Cascade: OPT only multiplies 2×–3× max (FUT stays 1×).
    # Heavy trend (non-cascade, ≥2/4 indices same dir + TF align): OPT 2× only.
    # A+/power_move alone never multiplies.
    cascade_lot_scale_enabled: bool = True
    cascade_lot_multiplier: float = 2.0  # legacy fallback
    cascade_lot_multiplier_fut: float = 1.0   # FUT always flat
    cascade_lot_multiplier_opt: float = 3.0   # cascade OPT max (clamped with min below)
    cascade_lot_multiplier_opt_min: float = 2.0  # cascade OPT floor when scaling
    # 0 = no hard max (operator margin responsibility); >0 clamps cascade size (R7).
    cascade_lot_max_lots: int = 0
    cascade_lot_max_lots_fut: int = 0
    cascade_lot_max_lots_opt: int = 0
    cascade_lot_scale_warn: bool = True
    # Non-cascade heavy trend: 2+ of 4 indices align trend+TF → OPT ×2 only
    heavy_trend_lot_scale_enabled: bool = True
    heavy_trend_lot_multiplier_opt: float = 2.0
    heavy_trend_lot_multiplier_fut: float = 1.0
    heavy_trend_min_aligned_indices: int = 2  # 2/4 indices
    # ── Multi-signal mixed book (dual engines × multi-TF × multi-index) ──
    # When several instruments/timeframes/engines fire together, take at most
    # ONE futures (fixed 1 lot) and put the rest on options (2–4 indices).
    # CE/PE, strike, stops still follow TF/engine + existing option logic.
    multi_signal_mixed_book_enabled: bool = True
    multi_signal_min_instruments: int = 3
    multi_signal_min_timeframes: int = 2
    multi_signal_min_engines: int = 2
    multi_signal_max_fut: int = 1
    multi_signal_fut_lots: int = 1
    # Core 3×(3 primary+2 dual) − 1 FUT + midcap 2+1 ≈ up to 17 OPT on full cascade
    multi_signal_max_opt_instruments: int = 18
    multi_signal_collapse_best_tf_per_index: bool = True
    # ── Cascade multi-TF concurrency (CASCADE ONLY; normal stays 2 / dual 2+1) ──
    # NIFTY + BANKNIFTY + SENSEX each get the same multi-TF stack:
    #   primary = 1×1min + 1×5min + 1×15min (cascade_primary_max_tf)
    #   dual    = +1×5min lane-B + +1×15min lane-B (cascade_dual_addon_max)
    # MIDCPNIFTY (FUT blocked → OPT only):
    #   solo engine  = best 2 of {1m, 5m, 15m}
    #   dual engine  = best 2 main-lane + best 1 dual of {5m, 15m}  (= 2+1)
    # Mixed book: at most 1 FUT (1 lot) across the whole wave; rest OPT.
    cascade_multi_tf_stack_enabled: bool = True
    cascade_primary_max_tf: int = 3
    cascade_dual_addon_max: int = 2
    # Indices that receive full cascade multi-TF stack
    cascade_multi_tf_core_indices: list[str] = ["NIFTY", "BANKNIFTY", "SENSEX"]
    # MIDCAP cascade stack (tighter than core)
    cascade_midcap_solo_max_tf: int = 2          # best 2 of 1/5/15 solo engine
    cascade_midcap_dual_main_max: int = 2        # best 2 main-lane
    cascade_midcap_dual_addon_max: int = 1       # + best 1 dual of 5/15 → 2+1
    # Dual modes on normal (non-cascade) bars: base concurrent + 1 dual addon.
    dual_mode_max_per_index: int = 3  # 2 + 1
    # Dual clone reduction: same index + same TF from both engines within this
    # window → keep only the FIRST signal (earlier timestamp); drop the clone.
    dual_clone_reduction_enabled: bool = True
    dual_clone_reduction_window_seconds: int = 60  # 1 minute
    # Heavy-trend peer vote freshness (seconds). Same-wave + recent cache votes
    # within this window count toward 2/4 align without waiting for open fills.
    heavy_trend_vote_max_age_seconds: float = 600.0  # 10 minutes
    # Cascade-aware bucket / global caps (R4): multi-TF stack needs room beyond
    # normal 4×2 core / midcap 2. Prevents late indices being starved mid-wave.
    cascade_core_bucket_max_positions: int = 12   # e.g. 3 indices × up to 4 slots
    cascade_midcap_bucket_max_positions: int = 3  # midcap solo 2 / dual 2+1
    cascade_max_concurrent_positions: int = 18    # soft ceiling during cascade waves
    # A flat options_sl_pct stop is timeframe- and ATR-blind: a 5min scalp gets the
    # same 15% room as a 15min swing. When enabled, derive the premium stop from the
    # signal's ATR natural stop (converted to premium via delta) bounded by the
    # per-timeframe min/max % below, so 5min stops sit tighter and 15min wider —
    # driven by real volatility. options_sl_pct stays the hard ceiling.
    option_stop_tf_atr_aware: bool = True
    option_stop_5min_min_pct: float = 4.0
    option_stop_5min_max_pct: float = 10.0
    option_stop_15min_min_pct: float = 6.0
    option_stop_15min_max_pct: float = 15.0
    opt_cost: float = 80.0          # Transaction cost per Options trade
    # Modeled slippage (H1) so sim/backtest P&L isn't optimistic; applied per entry+exit leg.
    # These are the STATIC defaults; once real fills accumulate, the realized-slippage
    # tracker overrides them with measured values (see slippage_auto_calibration_enabled).
    slippage_opt_pct: float = 0.30  # Options: % of premium per leg (wide bid/ask)
    slippage_fut_bps: float = 2.0   # Futures: bps of notional per leg
    # ─── Execution slippage controls ─────────────────────────
    # Marketable-limit buffer for option entries (% past signal price). This is the
    # worst-case entry slippage CAP: the order crosses the spread and fills at the
    # touch, but can never fill worse than signal ± buffer. ATM index option spreads
    # are typically 1-4 ticks, so 0.40% (with the tick floor below) fills reliably
    # while capping freak fills at ~2.5x less slippage than the old 1.0% buffer.
    option_limit_buffer_pct: float = 0.40
    # Absolute floor on the option limit buffer in ticks (0.05 each) so cheap
    # premiums (e.g. Rs.30 far-week MIDCPNIFTY) still cross the spread and fill.
    option_limit_buffer_min_ticks: int = 2
    # Guarded touch-quote execution. OFF keeps today's LTP/signal anchored limits,
    # while shadow mode still compares the old limit against bid/ask touch quotes
    # whenever the feed provides them. When enabled, option entry limits anchor to
    # ask+ticks for BUY and bid-ticks for SELL, subject to freshness/spread checks.
    execution_use_touch_quote: bool = False
    execution_touch_quote_shadow_enabled: bool = True
    execution_touch_quote_strict: bool = False
    execution_touch_quote_max_age_seconds: float = 1.5
    option_touch_limit_extra_ticks: int = 1
    option_touch_quote_max_spread_pct: float = 1.0
    option_touch_quote_max_spread_ticks: int = 40
    # 2026-08-11 (rtracker, 28 Jul - 11 Aug): one entry cost Rs.10,402 -- ten times the
    # entire 106-trade book. A signal timestamped 09:10:00, before the 09:15 open, was
    # executed at 09:15:34.516 on an entry mark of 131.70 and exited 40 MILLISECONDS
    # later at 52.30. An option cannot round-trip -60% in 40ms of real fills: the mark
    # was the previous close, which is what an option's LTP still reads until its first
    # trade of the day prints. _option_premium_quote_plausible could not catch it -- it
    # only bounds premiums from ABOVE (to spot*0.12 ~ Rs.2,880 on NIFTY), because its
    # job is catching an index price leaking into premium space, not a stale-but-
    # in-range mark. Two independent nets, because the touch quote is not always usable:
    #
    #   (a) refuse an entry whose SIGNAL predates the session open. Across the two-week
    #       window this blocks exactly 2 trades, both losers, together -Rs.10,866.
    #   (b) refuse an entry whose MARK is inconsistent with the live bid/ask. The bad
    #       mark sat 2.5x above the real offer; normal spread noise is far inside 1.5x.
    entry_preopen_signal_guard_enabled: bool = True
    entry_mark_touch_validation_enabled: bool = True
    entry_mark_max_ask_multiple: float = 1.50
    entry_mark_min_bid_multiple: float = 0.50
    # The two guards above live in TradeManager.open_trade, which the MANUAL signal path
    # never calls — it stamps accepted_by_gate on the candidate as-is, and every P&L
    # figure downstream is computed from that recorded price. 2026-08-11 15:09:36: NIFTY
    # 24500PE was accepted at 133.00 with spot at 24453.65, i.e. Rs.87 of time value on a
    # same-day-expiry option 20 minutes from expiry, against a true price of 40.65. The
    # mark was ~2.5 hours stale and the book took -Rs.6,083 the market never charged.
    # Both conditions must hold: a large FRACTIONAL divergence and a material RUPEE gap,
    # so a cheap contract ticking a few paise is never mistaken for a stale mark.
    candidate_entry_mark_validation_enabled: bool = True
    candidate_entry_mark_max_divergence_pct: float = 25.0
    candidate_entry_mark_min_gap_rs: float = 500.0
    # Futures entries were plain MARKET (uncapped in a freak spike). ON places a
    # marketable LIMIT at signal price +/- fut_limit_buffer_bps instead: same
    # immediate fill in normal books, but worst-case entry slippage is hard-capped.
    fut_entry_protection_limit_enabled: bool = True
    fut_limit_buffer_bps: float = 5.0
    # Measure REAL broker fills against signal/decision prices and feed the charge
    # model with the realized EWMA instead of the static assumptions above, once
    # enough samples exist. Bounded to sane ranges so one bad fill can't distort it.
    slippage_auto_calibration_enabled: bool = True
    slippage_calibration_min_samples: int = 5
    # A sample count is not evidence of currency. On 2026-08-11 slippage_stats.json held
    # 12,051 FUT samples at ewma_frac exactly 0.0, every row stamped last_leg "ENTRY",
    # from an integration that has since been retired — and because the measured value
    # REPLACES the static assumption rather than blending with it, that fossil pinned
    # modelled futures slippage to the 0.2bp floor of _FUT_CALIBRATION_BOUNDS. Rows
    # older than this fall back to the static model. 0 disables the age test.
    slippage_calibration_max_age_days: float = 30.0
    broker_fill_confirmation_timeout_seconds: float = 12.0
    broker_cancel_replace_enabled: bool = True
    # Final broker-side affordability preflight.  The requested order value is
    # compared with the freshest available cash/margin snapshot plus this safety
    # buffer; the very short cache avoids duplicating funds API calls for paired
    # checks while preventing a stale balance from authorising later entries.
    entry_margin_buffer_pct: float = 10.0
    entry_margin_cache_seconds: float = 2.0
    # Pre-market warning floor for broker free cash. Index option lot margins run
    # ~37k-56k, so an account below this cannot fund a single lot and every entry will
    # be refused by RMS with a generic rejection. Advisory only: it warns, never blocks,
    # because margin moves intraday and the operator may fund mid-session.
    # 0 = derive it. A flat floor cannot describe an option account: this system BUYS
    # options, so one lot costs premium x lot_size, and the four indices span 6x on lot
    # size alone (SENSEX 20, MIDCPNIFTY 120). The real range is about Rs.2,000 to
    # Rs.48,000. Set this above zero only to override the calculation.
    entry_funds_warning_floor: float = 0.0
    # The ATM premium the funding estimate assumes. Named in the warning so the operator
    # can judge it against the day's actual chain.
    entry_funds_assumed_atm_premium: float = 120.0
    recovery_auto_flatten_seconds: float = 5.0
    # When the order book cannot be read we cannot prove a position is unprotected, so a
    # flatten is withheld — a double exit (stop still resting + our market exit) opens a
    # naked reversed leg, which is worse than a late exit. That is safe intraday but it
    # can carry an NRML position past the close with only an alert.
    #
    # 0 (default) keeps that behaviour: withhold indefinitely and escalate to the operator.
    # Set >0 to accept the double-exit risk after that many seconds of being blocked and
    # flatten anyway; the trade is then flagged for a reversal watch, because a stop that
    # fires afterwards opens a position in the opposite direction. This is a deliberate
    # risk trade-off and is left switched off until an operator chooses it.
    recovery_flatten_block_deadline_seconds: float = 0.0
    max_entry_orders_per_minute: int = 6
    entry_order_rate_window_seconds: float = 60.0
    entry_order_kill_latch_seconds: float = 300.0
    clock_skew_max_seconds: float = 180.0
    shared_host_mode: bool = False
    allow_plaintext_broker_secrets: bool = True
    option_strike_selection: Literal["ATM", "ITM", "BOTH"] = "BOTH"
    inst_pref: Literal["FUT", "OPT", "AUTO"] = "AUTO"
    sl_mode: Literal["NATURAL", "HARDCODED"] = "NATURAL"
    signal_grade_preference: Literal["auto", "B", "B+", "A", "A+"] = "A"
    # ── Grade-gate A/B levers (2026-08-11) ───────────────────────────────────
    # Both default OFF: nothing here changes live behaviour until switched on.
    #
    # Across 12 live sessions / 106 trades the grade the engine computes did not rank
    # outcomes: corr(grade rank, pnl) = +0.096, i.e. no information, and what ordering
    # there was ran the WRONG way — A+ Rs.49/trade and A Rs.48 against B+ Rs.260 and
    # B Rs.407. Meanwhile grade drove 2,468 "C grade blocked" and 424 "Grade rank below
    # required 2" rejections in that window, the second and fifth largest reject reasons.
    #
    # Note what that sample is: .env pins SIGNAL_GRADE_PREFERENCE='B', so options run at
    # min_rank 2 and every B row in the book arrived through the fifteen_min_bplus_ok or
    # power_move exemption. The best-performing grades are therefore precisely the ones
    # the main gate refused and an OR-branch let past — suggestive, but n=15 across B and
    # B+ combined, which is far too thin to justify moving a gate that touches every
    # signal. So these ship off, to be A/B'd once pnl_path data has accumulated.
    #
    # -1 keeps signal_grade_preference in charge; 0-4 forces the floor
    # (C=0, B=1, B+=2, A=3, A+=4).
    # ── Late-trend entry ceiling (2026-08-11) ────────────────────────────────
    # A high ADX on a WITH-trend entry says the leg is MATURE, not that it is strong.
    # Across 47 stored sessions, same direction and same instruments:
    #   TRENDING_DOWN shorts   median ADX 34.4   win rate  7.1%   (1 win in 14)
    #   BREAKOUT_DOWN  shorts  median ADX 21.4   win rate 71.4%
    # and independently the whole ADX>=30 cell runs 32.4%/Rs.41 against 45.8%/Rs.248
    # for ADX<20. Blocking with-trend entries above the ceiling drops 43 of 240
    # historical trades worth -Rs.4,033 and moves the book from 40.0%/Rs.197 to
    # 46.2%/Rs.260. AGAINST-trend entries are deliberately untouched — they are the
    # mean-reversion side of the same reading and measured positive.
    late_trend_entry_guard_enabled: bool = True
    late_trend_entry_adx_ceiling: float = 30.0
    grade_gate_min_rank_override: int = -1
    # Lets a C grade continue to the confidence, choppy and quality gates instead of
    # being refused on grade alone. It does NOT admit the signal — everything downstream
    # still applies.
    grade_gate_allow_c_enabled: bool = False
    ut_timeframe_entry_policy: Literal["PRIMARY_15", "INCLUDE_5MIN"] = "INCLUDE_5MIN"
    ut_5min_option_min_confidence: float = 0.60
    ut_5min_loss_cooldown_minutes: int = 45
    # Enforces the line above. Until 2026-08-10 that value was read by nothing but two
    # broadcast payloads: the real post-loss block was the flat (and regime-scoped)
    # index_cooldown_minutes, so a configured 45 meant an actual 3. Ships False so
    # turning the gate on is an explicit decision — at four indices and a ~360-minute
    # entry window a 45-minute per-index lockout caps each index near 8 entries, which
    # is a material change to how much the system trades.
    ut_5min_loss_cooldown_enabled: bool = False
    # Local quality a candidate must have before a cross-index reversal may carry it
    # past the raw quality floor. Ported back from restore_point_51, where the bypass
    # read `market_wide and market_wide_ok`; this build had lost the guard and the
    # bare flag admitted anything (2026-08-12: grade C at 12% and 13%).
    market_wide_reversal_min_grade: Literal["B", "B+", "A", "A+"] = "B+"
    market_wide_reversal_min_confidence: float = 0.60
    live_b_grade_min_confidence: float = 0.50  # Top-of-band floor for dedicated B-grade entries (trending + 15m align + ADX>=25)

    # ─── Confidence calibration (de-saturation) ──────────────
    # The legacy model was confidence = clamp(0.5 + Σpillars, 0, 1), which pinned
    # at 1.0 for any net-positive setup and destroyed ranking power. The logistic
    # squash keeps the same midpoint (score 0 -> 0.50) and closely tracks the old
    # mapping across the 0.55-0.85 gate band, but asymptotes toward 1.0 instead of
    # clipping, so grades regain discrimination. k≈5 preserves existing gate floors.
    ut_confidence_logistic: bool = True
    ut_confidence_logistic_k: float = 5.0
    # R5: fit confidence to realized win-rate (isotonic) from outcome_calibration.csv.
    # DEFAULT OFF — respects the ADVISORY_ONLY guardrail. Even when enabled it is a
    # no-op until the dataset is ready (>= min_samples AND both win/loss classes).
    ut_confidence_calibration_enabled: bool = False
    ut_confidence_calibration_min_samples: int = 200

    # ─── Regime veto (directional FUT in chop) ───────────────
    # Block directional FUTURES entries when the regime is choppy/ranging/volatile
    # AND ADX is below the floor (no trend to ride). Options and power-moves are
    # exempt. This is a hard veto, not the soft -0.10 regime score penalty.
    regime_veto_enabled: bool = True
    regime_veto_adx_floor: float = 18.0
    # A 60-bar high/low TOUCH is only a real breakout with momentum or volatility
    # expansion. Below this ADX (and without vol expansion) it's noise grazing the
    # band in a dead tape — label it RANGING/VOLATILE honestly, not BREAKOUT.
    regime_breakout_adx_floor: float = 20.0
    # FUT entries are rejected when the natural ATR stop exceeds the regime-selected
    # stop tolerance x this multiplier. futures_sl_pct remains the absolute ceiling.
    # This governs only the REJECT threshold, not the traded stop itself.
    futures_sl_safety_multiplier: float = 2.0
    # Early loss-cut (loss-guard): symmetric for FUT and OPT — cut a failing trade
    # at this R-multiple of initial risk before the full stop is hit. Mirrors the
    # profit-lock / peak-threat path on the winner side. Winners untouched.
    fut_early_loss_cut_enabled: bool = True
    fut_loss_cut_r: float = 0.8
    opt_early_loss_cut_enabled: bool = True
    opt_loss_cut_r: float = 0.8
    # Intel-accelerated loss-guard: when OI/PCR/volume/order-flow + price action
    # confirm a BIG reversal (not a small pullback), cut earlier than base 0.8R.
    # Small pullbacks (trend still supportive, no fast reverse) still wait for 0.8R.
    loss_guard_intel_accelerated_enabled: bool = True
    loss_guard_intel_min_r: float = 0.35   # severe multi-pillar reverse
    loss_guard_soft_intel_r: float = 0.55  # moderate reverse evidence
    loss_guard_big_reversal_severity: int = 5
    loss_guard_soft_reversal_severity: int = 3
    # Gap fixes: rejection wick, multi-bar grind, intel age, OI votes when losing,
    # option premium-melt reverse (without requiring index reverse).
    loss_guard_rejection_wick_enabled: bool = True
    loss_guard_rejection_wick_min_ratio: float = 0.45  # wick / full range
    loss_guard_grind_bars: int = 3
    loss_guard_grind_range_mult: float = 1.50  # cumulative adverse ≥ mult × median range
    loss_guard_intel_max_age_seconds: float = 90.0  # older intel is stale for acceleration
    loss_guard_oi_pcr_votes_winning: int = 3  # hard exit while profitable
    # The reversal exit reads PCR/OI/flow only, though the bundle also carries volume,
    # regime and a divergence alert. Corroboration from those may move the vote bar by
    # ONE, and only the safe way: a losing trade exits a vote sooner when they confirm,
    # a winning trade demands a vote more when they oppose. Neutral extras (the common
    # case) leave the decision byte-identical to the 3-pillar behaviour.
    intel_reversal_corroboration_enabled: bool = True
    intel_reversal_corroboration_votes: int = 2
    loss_guard_oi_pcr_votes_losing: int = 2   # faster OI/PCR exit when already red
    loss_guard_opt_premium_melt_pct: float = 8.0  # premium drop % → melt reverse signal
    loss_guard_opt_premium_melt_severity: int = 2
    # Severity stacking caps (R4): wick+grind+OI+PCR cannot all fully stack.
    loss_guard_severity_max_total: int = 8
    loss_guard_severity_max_price: int = 5
    loss_guard_severity_max_intel: int = 5
    # R5: when intel age is unknown, treat as not fully fresh for acceleration.
    loss_guard_unknown_intel_age_half_weight: bool = True
    # Post-2:59pm sudden-spike capture: a spike LEADS the 15m, so a power-move
    # with big range expansion + hard directional close may enter WITHOUT 15m
    # agreement (otherwise spikes are structurally un-catchable near close).
    power_move_spike_bypass_15m: bool = True
    power_move_spike_range_atr_mult: float = 2.5
    # 5min trend-CONTINUATION entry path: admit a high-grade, 15m-aligned 5min
    # signal in a confirmed trending regime with strong ADX, even if it isn't an
    # impulse reversal and option-premium data is incomplete. Fixes "system misses
    # big directional moves" (A+/95%/ADX48 trend signals were being rejected).
    live_5min_trend_continuation_enabled: bool = True
    live_5min_trend_continuation_adx: float = 25.0
    # Higher-conviction continuation tier: a strongly-confident, 15m-aligned 5min
    # signal in a trending regime may enter at a LOWER ADX, since conviction
    # substitutes for raw ADX. Catches grind trends that never spike ADX to 28
    # (e.g. NIFTY +80 / BANKNIFTY +300 on 2026-06-24, rejected at ADX 24-26 / B+ 69%).
    live_5min_trend_continuation_strong_conf: float = 0.65
    live_5min_trend_continuation_min_adx: float = 22.0
    # Regime: when ADX>=20 confirms a trend but the strict price>sma9>sma21 stack
    # breaks on a pullback within a grind, resolve direction via MA structure
    # (sma9>sma21 AND price>sma21, with macd_hist not reversing) instead of
    # defaulting to VOLATILE. Chop-safe: needs a stacked MA structure, so it cannot
    # fire when the SMAs are intertwined (true chop).
    regime_trend_soft_direction_enabled: bool = True
    # Regime smoothing: the detector computed a raw single-bar opinion and exposed it
    # raw, flipping 17-27x/day (a change every ~3 bars) — gates then saw a noisy regime.
    # Return a majority vote over the last N bars instead. ~50% fewer flips, stabler gates.
    regime_smoothing_enabled: bool = True
    regime_smoothing_window: int = 5
    # Regime hysteresis: ADX>=20 means trend strength IS present, so a bar with strong
    # ADX but ambiguous direction (NEUTRAL) is NOT choppy — hold the recent trend rather
    # than mislabeling it VOLATILE (which routes it to the choppy gates / rejects entries).
    regime_strong_adx_hold_trend: bool = True
    # Confirmed range-day guard: ADX can rise inside a 50-100pt box and manufacture
    # TRENDING/BREAKOUT labels even though the session is still rotating. Demote those
    # phantom directional regimes to RANGING unless the day has clear directional proof.
    regime_range_day_trend_guard_enabled: bool = True
    regime_range_day_trend_guard_min_confidence: float = 0.60
    # ADX-strength escape (2026-07-24 audit): a genuinely strong trend is real even
    # when the session day-type classifier calls the day a RANGE_DAY. Above this ADX
    # the range-day demotion is skipped so strong directional moves are NOT relabelled
    # RANGING (which had cascaded into choppy gating, scalp-lock early exits and dormant
    # trend re-entry — the system missed big moves). Set 0 to disable the escape.
    regime_range_day_trend_guard_adx_override: float = 35.0
    # Kaufman Efficiency Ratio early-trend promotion: ADX lags the first clean
    # directional drive. When ADX is still below the trend floor but price is moving
    # straight enough (ER) with a clean SMA stack, promote RANGING/VOLATILE to an
    # early trend. Confirmed RANGE_DAY context blocks this so wide swing-legs do not
    # manufacture phantom trends.
    regime_early_trend_er_promotion_enabled: bool = True
    regime_early_trend_er_period: int = 10
    regime_early_trend_er_threshold: float = 0.60
    regime_early_trend_er_adx_ceiling: float = 20.0
    regime_early_trend_er_range_day_min_confidence: float = 0.60
    # Relax the VOLATILE 5min confidence floor (0.70 -> this) when the 15m trend
    # agrees with the signal — an aligned pullback-entry in a volatile-but-directional
    # tape is not the same risk as counter-trend chop. RANGING/UNKNOWN keep 0.70.
    live_5min_volatile_aligned_relax_enabled: bool = True
    live_5min_volatile_aligned_min_confidence: float = 0.65

    # ─── Directional census (counter-trend guard) ────────────
    # Track distinct directional signals across the session. When the session is
    # strongly skewed one way (ratio over min sample), require counter-direction
    # entries to clear an elevated confidence floor (B+ floor + penalty).
    directional_census_enabled: bool = True
    directional_census_ratio: float = 1.5
    directional_census_min_signals: int = 12
    directional_census_penalty: float = 0.05

    # ─── Repaint intra-candle lockout ────────────────────────
    # Once a forming-candle signal repaints (vanishes) intra-candle, lock that
    # instrument+timeframe candle from being re-buffered/re-analysed until the
    # candle closes. If a live trade is exited by a repaint abort while the same
    # candle is still forming, re-check that same candle once per minute so a
    # fresh reappearing signal can go through the normal buffer/filter pipeline.
    # ── Stabilization: the system's largest filter (2026-08-11) ──────────────
    # 252 signals entered the 30s buffer that session and 48 matured — 19%. Every
    # rejection read "UT1 arrow vanished", 63 of them inside two seconds. All four
    # indices produced the correct SELL/PE signal within 22 seconds of the open, on the
    # only hour of the day that moved, and the buffer killed them.
    #
    # The latch: a one-way cascade is evidence about a move that ALREADY happened, so
    # re-proving it every scan asks the wrong question and goes false the moment volume
    # ticks under its ratio. Once confirmed it holds for the rest of the window. A
    # FLIPPED arrow still vetoes — that check runs first and is untouched.
    stabilization_cascade_latch_enabled: bool = True
    # Thrust scaling: a flat 30s on a 5-minute candle is 10% of its life, and the arrow
    # is least stable when price moves fastest. A thrusting bar drops to the floor.
    stabilization_thrust_scaling_enabled: bool = True
    stabilization_thrust_min_seconds: float = 10.0
    stabilization_thrust_range_atr: float = 1.50
    stabilization_thrust_volume_ratio: float = 1.50
    # Every buffered signal's outcome is recorded, not just the repaint rejections.
    # rtracker held 887 stabilization rejections across 47 sessions while a single
    # session's log held 203 — the largest gate in the system was effectively unmeasured.
    stabilization_rtracker_logging_enabled: bool = True
    live_repaint_candle_lockout: bool = True
    live_repaint_post_exit_recheck_seconds: float = 60.0

    # ─── Let winners run (anti-"pennies" profit-taking) ──────
    # The profit-lock was snatching tiny gains: it armed at a flat ₹500-1000 peak
    # and gave back on any dip under a flat ₹450, so trades were locked long before
    # reaching target/runner. With let_winners_run, the profit-lock does NOT arm
    # until peak >= profit_lock_min_r_multiple × initial risk (₹) — so every trade
    # gets room to develop to at least that R-multiple before locks engage. The
    # flat give-back floor is also made tunable. Runner mode unlocks earlier so
    # winners flip to trail-and-ride sooner.
    let_winners_run: bool = True
    # 2026-08-11 (rtracker, 12 sessions / 106 trades): 1.0 measured as the reason the
    # non-scalp book gave everything back. The system reached Rs.42,444 of peak profit
    # and banked -Rs.992; 45 trades went green then closed red, 42 of them peaking below
    # every arming floor. Median trade peaks at Rs.274 while the median non-scalp arm
    # computed to Rs.868 -- only 4 of 59 non-scalp trades ever reached their own lock.
    #
    # The Rs.500 base was NOT what bound: it beat risk*1.0 on just 5 of those 59 rows.
    # This R-multiple bound the other 54, so lowering the base alone would have been
    # very nearly a no-op (2 of the 45 give-backs would have armed, against 20 for the
    # pair below).
    #
    # 0.20 mirrors fifteen_min_choppy_scalp_profit_lock_r_multiple, whose cohort is the
    # live control group: scalp_lock trades ran that arm and captured +42.6% of peak
    # against -4.5% for the rest, at a 48.9% vs 20.3% win rate (z=3.11, p=0.002), and
    # the effect holds inside RANGING and outside it. The "let winners run" worry -- that
    # arming early truncates the fat tail -- did not appear in that cohort: it carried
    # the SAME eight peak>=Rs.750 winners as the loose cohort and a HIGHER best peak
    # (Rs.3,582 vs Rs.2,603). Arming is not exiting; the ladder still trails.
    profit_lock_min_r_multiple: float = 0.20
    # True scalp rows still need an R-aware arm; otherwise a Rs.500 blip can cap
    # winners while the configured stop risk is several thousand rupees.
    scalp_profit_lock_min_r_multiple: float = 0.50
    # Absolute arming floors, previously a bare 500.0 literal inside
    # Scanner._profit_lock_min_peak with no way to reach it from the dashboard. Split in
    # two because the branches want different answers: scalp keeps its historical base,
    # while the non-scalp base drops to the same Rs.250 the choppy-scalp path already
    # uses in production (and which .env has pinned at 250.0 since before this change).
    profit_lock_min_peak_rs: float = 500.0
    profit_lock_nonscalp_min_peak_rs: float = 250.0
    profit_lock_abs_floor_rs: float = 450.0
    # The sub-Rs.1,000 rung retains 50% of peak, i.e. it stops a trade that gives back
    # half. Measured heat as a fraction of peak across every winner in 47 sessions:
    # TRENDING_UP 8%, TRENDING_DOWN 28%, RANGING 35%, BREAKOUT_DOWN 43%, BREAKOUT_UP
    # 47%, VOLATILE 49%. In the fast regimes that floor sits ON the pullback those
    # winners routinely make and survive. 0.38 = 62% breathing, clear of the measured
    # median. Applied through a min(), so it can only loosen; TRENDING_UP and RANGING
    # are untouched because their heat is nowhere near the floor.
    first_rung_regime_retention_enabled: bool = True
    first_rung_fast_regime_retention: float = 0.38
    # A closed trade has only ever recorded peak_pnl and max_drawdown, and those cannot
    # answer "would a tighter trail have kept more?" -- a trail exits at f x peak only if
    # P&L reached that level AFTER the peak, and a max carries no ordering. Asked of the
    # 2026-08-11 book the honest answer was "between -Rs.18,114 and +Rs.10,988", which is
    # no answer at all. Sampling the path makes the next such question measurable rather
    # than arguable. Bounded three ways in _sample_pnl_path; at a 1.2-minute median hold
    # a typical trade stores ~36 samples.
    pnl_path_logging_enabled: bool = True
    pnl_path_min_interval_seconds: float = 2.0
    pnl_path_min_move_rs: float = 50.0
    pnl_path_max_samples: int = 400
    # ── Sub-1R give-back guard (Fix 1) ──────────────────────────────
    # Below the full 1R profit-lock arm, a modest peak used to be unprotected and
    # could round-trip into a loss (e.g. a +0.4R peak fading past breakeven). Once
    # a trade shows a meaningful positive peak, don't let that winner become a
    # loser: arm at max(min_peak_rs, arm_r_multiple × risk) and exit at the current
    # (still-positive) price once profit gives back to <= exit_ratio × peak.
    profit_giveback_guard_enabled: bool = True
    profit_giveback_arm_r_multiple: float = 0.40
    profit_giveback_min_peak_rs: float = 500.0
    profit_giveback_exit_ratio: float = 0.50
    # No protective exit may arm below what a round trip costs. 2026-08-11 15:11:
    # PROFIT_GIVEBACK_GUARD closed BANKNIFTY 57400PE to protect a Rs.36 peak and booked
    # -Rs.44, because opening and closing cost Rs.80. Inside the EOD window the arm
    # collapses to eod_protect_min_peak_rs (Rs.1), so any blip armed an exit that could
    # not come out ahead. The LOSS side of peak-threat has refused on this reasoning
    # since it was written (peak_threat_exit_loss_min_cost_multiple); the profit side
    # never had the equivalent. Raises an arm only — it can never lower one.
    profit_protect_cost_floor_enabled: bool = True
    profit_protect_min_cost_multiple: float = 1.0
    # ── Volatility + regime scaling of the profit-side floors (2026-08-11) ────
    # Every profit-side threshold is fixed rupees (Rs.750 peak-threat, Rs.500/250 lock,
    # Rs.500 give-back, ladder at 1k/2k/3k) while the moves they catch scale with
    # volatility. As the median daily range fell 0.97% -> 0.64% across W30-W33, the share
    # of trades ever reaching a Rs.750 peak fell 43% -> 4%; PEAK_THREAT_EXIT, which banks
    # 87% of peak, fired ONCE in a week, and LOSS_THREAT_EXIT went from 30% to 50% of all
    # exits. Win rate followed, 47.8% -> 12.5%.
    #
    # Only the ABSOLUTE floors are scaled — each arm is max(floor, risk x r_multiple) and
    # the r_multiple half already scales, since initial_risk_rs is an ATR-derived stop
    # times quantity. The max() is exactly what lets the blind floor dominate when risk
    # is small, i.e. in the quiet markets where it does the damage.
    #
    # Volatility term: the trade's own risk against this reference (median observed risk
    # is Rs.937). Regime term: measured peak potential, so a trend develops before the
    # lock arms and chop banks sooner. Bounded hard both ways.
    # ── Entry-time intel snapshot (2026-08-12) ───────────────────────────────
    # OBSERVATION ONLY. Nothing reads this to make a decision.
    #
    # The engine computes volume ratio, PCR, cumulative OI, order flow, breadth, VWAP
    # position, the full greeks and a VIX percentile — and the trade record kept almost
    # none of it: volume_ratio survived on 2% of 239 accepted rows, the grading pillars
    # on 33%, PCR/OI/order_flow on none. intelligence_history.json does hold the series
    # per minute, but as a rolling 5,000-point window only 76 of 240 trades were still
    # joinable to it: the evidence expires before anyone can use it.
    #
    # A composite ENTRY GATE built on these features was measured and REJECTED. It looked
    # excellent in-sample (55.6% win rate / Rs.752 at K>=3) and inverted out-of-sample
    # (16.7% / -Rs.336 against a 32.5% baseline), and the adaptive rolling-median variant
    # beat its own baseline win rate in no configuration. This snapshot exists so that
    # question can be re-asked later against complete, permanent data instead of a
    # 6-feature shadow of what the engine actually knows.
    entry_intel_snapshot_enabled: bool = True
    profit_threshold_scaling_enabled: bool = True
    profit_threshold_regime_scaling_enabled: bool = True
    profit_threshold_reference_risk_rs: float = 1000.0
    profit_threshold_scale_min: float = 0.35
    # Capped at 1.0 deliberately: the scale exists to RESTORE PEAK_THREAT_EXIT where the
    # fixed floor has gone unreachable, and letting it exceed 1.0 in trending regimes
    # suppressed the very exit it was meant to rescue — measured as an exact wash, 17
    # trades reaching the arm before and after. Regime still differentiates below 1.0
    # (RANGING 0.85 vs TRENDING_UP 1.00), it just cannot push the arm further away.
    profit_threshold_scale_max: float = 1.00
    # ── Entry budget scaled to the day's measured range ──────────────────────
    # Trades per 1% of median daily range across the stored weeks: 15.5, 13.8, 16.9,
    # 18.8 — activity rose against opportunity as the market quietened, and the median
    # peak fell Rs.533 -> Rs.78 over the same stretch. 0.90% is the reference day;
    # quieter days scale the per-index budget down, never up, and never below the floor.
    range_scaled_budget_enabled: bool = True
    range_scaled_budget_reference_pct: float = 0.90
    range_scaled_budget_min_trades: int = 3
    # Confirmed-live-trend "let winners run" (2026-07-24 audit): a with-trend PROVEN
    # winner (peak >= min_peak_r_multiple x initial risk) in a strong-trend regime
    # rides the runner lock (10% give-back) instead of the tighter give-back guard,
    # even when the session day-type classifier lags or scalp-lock was armed. EOD and
    # unconfirmed-repaint protection are NEVER suppressed. Set enabled False to revert.
    trend_runner_let_winners_run_enabled: bool = True
    # 0.4R crossover (operator design 2026-07-24): BELOW this the current profit/loss
    # guards run unchanged (give-back arms at ~0.40R); AT/ABOVE it, in a confirmed strong
    # trend only, the trade hands off from the 50%-fade give-back to the 85-90% runner
    # lock (protects more AND lets it run). The regime+ADX gate keeps this from engaging
    # on chop days, so it is regime-adaptive rather than a blanket low threshold.
    trend_runner_min_peak_r_multiple: float = 0.4
    # Dynamic runner profit-lock: give a trending premium more room in fast/volatile
    # tape (lock the looser high-vol pct so noise does not shake the runner out) and
    # tighten toward dynamic_rr_runner_lock_pct (0.90) in calm regimes.
    trend_runner_dynamic_lock_enabled: bool = True
    trend_runner_lock_pct_high_vol: float = 0.85
    # Big-move gate: the ride/runner engages EARLY (before the fixed target) only when a
    # proven winner is in a strong trend with ADX above this floor — so small gains are
    # banked normally and the ride is reserved for moves with room to run.
    trend_runner_big_move_min_adx: float = 25.0
    # Runner mode requires a regime for the trade's OWN timeframe. _get_trade_regime
    # falls back to the 5-minute label, which is fine for tightening but must not GRANT
    # the looser runner trail to a 15-minute trade on a 5-minute reading. Set False to
    # restore the previous borrow-and-assume behaviour.
    trend_runner_require_own_timeframe_regime: bool = True
    # A HISTORICAL replay writes the regime it computed from PAST candles into the same
    # intelligence caches live management reads, stamped with the current wall clock — so
    # without this purge the replayed label is served as fresh live intel for a full
    # intelligence_cache_ttl_seconds after switching back to REAL. Set False to restore
    # the old carry-over behaviour.
    purge_intel_cache_on_mode_change: bool = True
    trend_runner_engage_before_target_enabled: bool = True
    # ── Pullback hedge (Phase 3, operator design 2026-07-24) ──────────────────
    # While a proven runner (>= the 0.4R crossover) is riding a confirmed trend, an
    # early pullback arms a FRESH-ATM opposite option leg ("pullback insurance"): it
    # captures the down-move that the runner lock would otherwise give back, and is
    # RELEASED when the move resumes so the runner keeps riding. The hedge is never a
    # standalone directional bet — it closes with its parent and never outlives it.
    # DEFAULT OFF: this opens a second live broker leg (sizing/exposure/accounting),
    # so it stays off until the archived-session backtest justifies enabling it.
    #
    # DORMANT AS OF 2026-08-07, and the distinction matters because this is a toggle in
    # the dashboard UI. _pullback_hedge_arm and _pullback_hedge_release are written and
    # tested, but NOTHING IN THE SYSTEM OPENS A HEDGE LEG — there is no call site that
    # places the second order. Turning this on therefore changes nothing at all.
    #
    # The decision helpers are kept rather than deleted: they encode real thinking about
    # when a hedge is justified (only a proven runner, armed before the lock's give-back)
    # and that work is worth having when the leg is eventually built. What is NOT
    # acceptable is a switch an operator can flip believing it does something, so the
    # REAL-mode preflight warns when this is on. See engine/safe_mode.py.
    pullback_hedge_enabled: bool = False
    # Arm when the premium has retraced this FRACTION of the distance from peak to the
    # runner stop. Relative (not a fixed premium %) because the runner lock sits only
    # ~4-5% below peak: a fixed 5% trigger could never fire — the parent always exited
    # first (proven by the archived-session backtest, 0 hedges armed).
    pullback_hedge_trigger_lock_fraction: float = 0.5
    pullback_hedge_trigger_giveback_pct: float = 0.0   # optional extra floor (0 = off)
    pullback_hedge_resume_recovery_pct: float = 50.0   # % of pullback depth recovered -> release
    pullback_hedge_size_fraction: float = 0.5          # hedge lots vs parent lots
    pullback_hedge_max_per_trade: int = 2              # anti-whipsaw churn cap
    pullback_hedge_max_hold_minutes: float = 15.0      # never hold a hedge indefinitely
    # How old a cached option chain may be and still drive intelligence (OI/PCR/Greeks).
    # In REAL mode the scan loop deliberately serves the cached chain instead of
    # re-fetching every cycle; only genuine age should mark it stale, otherwise the
    # aggregator drops the chain and the panel reports "OPTIONS DATA UNAVAILABLE".
    option_chain_intelligence_max_age_seconds: float = 300.0
    # ── Stop-out re-entry (2026-07-25) ───────────────────────────────────────
    # A trade stopped out early on a trend day, then the move ran without us. When the
    # SAME direction re-validates in a confirmed trend shortly after a STOP-type exit,
    # allow one more attempt by bypassing ONLY the post-exit cooldown. The daily trade
    # budget and the consecutive-loss halt are real risk limits and are NEVER bypassed.
    # Backtest across 8 archived sessions: 20/54 stop-outs qualified, 11 replayable on
    # real premium paths -> 73% win rate, +Rs 3,560 (avg +324/trade). Enabled on that
    # evidence, with conservative caps below; flip to False to revert.
    stop_out_reentry_enabled: bool = True
    stop_out_reentry_window_minutes: float = 30.0   # same move, not a new session phase
    stop_out_reentry_max_per_index: int = 1         # one second chance per index per day
    stop_out_reentry_min_adx: float = 25.0          # only a genuinely strong trend
    # ── Rupee loss ladder (freefall OR confirmed slow bleed) ───────
    # Eligibility floors OPT -1000 / FUT -1500 are NOT auto-exits. Two cut paths:
    # (A) FREEFALL — sudden opposite flow, post-close repaint, spike dump, etc.
    # (B) SLOW BLEED — easier to detect: past arm for N minutes, loss keeps
    #     making new lows / never recovers, other guards missed the grind.
    rupee_loss_band_enabled: bool = True
    rupee_loss_band_opt_only: bool = False
    # Arms/steps/force levels are PER LOT (2026-07-10): a 2-lot book arms at
    # 2× the rupee thresholds so bigger size is not cut on 1-lot noise.
    rupee_loss_band_scale_per_lot: bool = True
    # MIDCPNIFTY carries the largest lot size (120 vs NIFTY 65 / BANKNIFTY 30 /
    # SENSEX 20) — relax its rupee thresholds by this % per lot.
    rupee_loss_band_midcap_relax_pct: float = 20.0
    rupee_loss_band_start_rs: float = 1000.0
    rupee_loss_band_start_rs_opt: float = 1000.0
    rupee_loss_band_start_rs_fut: float = 1500.0
    rupee_loss_band_size_rs: float = 500.0
    rupee_loss_band_size_rs_opt: float = 500.0
    rupee_loss_band_size_rs_fut: float = 500.0
    # Path A: freefall special occasions
    rupee_loss_band_freefall_enabled: bool = True
    rupee_loss_band_min_severity: int = 4
    rupee_loss_band_min_severity_opt: int = 4
    rupee_loss_band_min_severity_fut: int = 4
    rupee_loss_band_min_freefall_flags: int = 2
    # Path B: slow bleed (monotonic grind past arm — easy detector)
    rupee_loss_band_slow_bleed_enabled: bool = True
    rupee_loss_band_slow_bleed_minutes: float = 4.0       # past arm this long
    rupee_loss_band_slow_bleed_worsen_rs: float = 150.0   # new lows beyond first arm touch
    rupee_loss_band_slow_bleed_max_recovery_rs: float = 80.0  # no meaningful bounce
    # Absolute catastrophe force — last resort only.
    rupee_loss_band_force_loss_rs_opt: float = 3000.0
    rupee_loss_band_force_loss_rs_fut: float = 4000.0
    # Spike freefall: adverse PnL velocity Rs/min after entry.
    rupee_loss_band_spike_rs_per_min: float = 400.0
    # Legacy alias (maps to freefall path when true; slow bleed still available).
    rupee_loss_band_freefall_only: bool = False
    # Special-case only: the give-back guard fires ONLY in a protective context
    # (near square-off, or a scalp-lock / late entry). A normal mid-day trade keeps
    # let-winners-run below the 1R arm. Set False for the blanket (every-trade) guard.
    profit_giveback_guard_special_only: bool = True
    # An intracandle entry whose repaint check has not yet passed is structurally
    # unconfirmed — let-winners-run does not apply, so the give-back guard also arms
    # during that window (exit reason REPAINT_GIVEBACK_GUARD). 2026-07-02: a Rs.990
    # peak faded negative before its repaint abort fired.
    repaint_window_giveback_guard_enabled: bool = True
    # ── EOD-proximity profit protection (Fix 2) ─────────────────────
    # Within this many minutes of the square-off, a trade physically cannot run to
    # 1R, so collapse the profit-lock arm toward a small peak so protection engages
    # early instead of letting a winner round-trip until the forced EOD kill.
    eod_protect_enabled: bool = True
    # 14 minutes, set FROM the session schedule rather than independently of it:
    # ut_force_exit_time (15:15) minus this is 15:01, which is exactly
    # ut_5min_no_entry_after. Protection therefore begins the moment the last entry is
    # allowed, so no position can ever be opened inside the window that closes it. At
    # the previous 30 it began at 14:45 and overlapped the entry gate by 20 minutes.
    # Changing either time without the other re-opens that overlap.
    eod_protect_window_minutes: float = 14.0
    eod_protect_arm_r_multiple: float = 0.0
    # Floored at the round-trip cost by _profit_protect_cost_floor_rs — on its own this
    # Rs.1 armed protective exits that banked less than they paid to close.
    eod_protect_min_peak_rs: float = 1.0
    # ── EOD-proximity loss minimizer ───────────────────────────────
    # During the last 15-20 minutes, a losing trade should not casually drift into
    # force-exit/full-stop. Track its best reduced-loss mark and exit if it reaches
    # near-breakeven, starts worsening again, or has only a few minutes left.
    eod_loss_minimize_enabled: bool = True
    eod_loss_minimize_window_minutes: float = 20.0
    eod_loss_minimize_hard_exit_minutes: float = 5.0
    eod_loss_minimize_max_hard_exit_minutes: float = 10.0
    eod_loss_minimize_min_age_minutes: float = 3.0
    eod_loss_minimize_near_breakeven_rs: float = 250.0
    eod_loss_minimize_giveback_rs: float = 250.0
    eod_loss_minimize_giveback_r_multiple: float = 0.12
    eod_loss_minimize_material_loss_r: float = 0.45
    eod_loss_minimize_severe_loss_r: float = 0.75
    # Former-winner recovery watch: when a trade has already shown profit, then
    # slips slightly negative while still inside normal option breathing and the
    # candle/trend context remains supportive, monitor it with a reduced-loss
    # virtual trail instead of passively waiting for the full hard stop.
    loss_recovery_watch_enabled: bool = True
    loss_recovery_watch_min_peak_rs: float = 500.0
    loss_recovery_watch_min_peak_r: float = 0.25
    loss_recovery_watch_max_loss_r: float = 0.45
    # Rupee breathing for former winners: if trend still holds but momentum
    # softens, allow a controlled red slip of -500..-700 before exiting.
    loss_recovery_watch_max_loss_rs: float = 700.0
    loss_recovery_watch_soft_loss_rs: float = 500.0
    loss_recovery_watch_breathing_mult: float = 1.0
    loss_recovery_watch_giveback_rs: float = 250.0
    loss_recovery_watch_giveback_r_multiple: float = 0.10
    loss_recovery_watch_max_minutes: float = 8.0
    loss_recovery_watch_adverse_intel: float = 0.65
    # Winners use the opposite logic: preserve more of peak when late-session
    # context turns adverse/neutral, but give supportive trends a little more room.
    eod_profit_adaptive_peak_enabled: bool = True
    eod_profit_final_window_minutes: float = 10.0
    eod_profit_adverse_exit_ratio: float = 0.75
    eod_profit_neutral_exit_ratio: float = 0.65
    eod_profit_supportive_exit_ratio: float = 0.45
    # ── Live regime-reversal profit-taker (Fix 3) ───────────────────
    # Babysit an open, profitable trade: if the instrument's live regime flips to
    # oppose the position AND profit is fading off its peak, take the profit.
    # Profit-only — never forces a loss (the hard stop still handles downside).
    live_reversal_exit_enabled: bool = True
    live_reversal_exit_fade_ratio: float = 0.85
    # Near-peak threat exit: if a meaningful winner is still holding most of its
    # peak but live evidence flips hard against it (fast reversal, weak volume/OI,
    # adverse intel, cross-index turn), exit at market instead of waiting for the
    # profit-lock/giveback layers to be hit.
    peak_threat_exit_enabled: bool = True
    peak_threat_exit_min_peak_rs: float = 750.0
    peak_threat_exit_min_peak_r: float = 0.35
    peak_threat_exit_min_retention: float = 0.72
    peak_threat_exit_min_score: int = 5
    peak_threat_exit_severe_score: int = 4
    peak_threat_exit_adverse_intel: float = 0.55
    peak_threat_exit_weak_volume_ratio: float = 1.05
    peak_threat_exit_loss_enabled: bool = True
    peak_threat_exit_loss_min_score: int = 6
    peak_threat_exit_loss_max_loss_r: float = 0.35
    # The loss arm used to fire on `current_pnl <= 0.0` — ANY negative, including one
    # rupee of bid-ask spread. Closing costs a round trip (opt_cost/fut_cost), so an exit
    # taken while the price loss is smaller than that cost destroys value by construction:
    # on 2026-08-10 nine trades were cut for price losses of Rs.10-91 at Rs.80 a round
    # trip, and half the day's loss was pure cost. The loss must now be at least this
    # multiple of the round trip before the arm is live. 0 restores the old behaviour.
    peak_threat_exit_loss_min_cost_multiple: float = 1.0
    # No loss-threat exit inside this many seconds of entry. The same day produced exits
    # 4s, 33s and 60s after a fill, on signals that had just spent 31s in stabilization —
    # the position was cut before its own thesis had a chance to print a bar.
    peak_threat_exit_min_hold_seconds: float = 60.0
    # Whether a NEUTRAL/HOLD/MIXED intelligence read counts toward the adverse score.
    # It used to add +1 each for the aggregate and order-flow pillars, so an unclear tape
    # contributed 2 of the 6 points needed to exit — absence of evidence scored as
    # evidence against the trade, the same defect pattern as the repaint and
    # single-pillar-conviction bugs. Off means only genuinely opposing reads score.
    peak_threat_exit_neutral_counts: bool = False
    # Ported from restore_point_51, whose loss arm required BOTH of these before cutting.
    # min_domains: how many distinct evidence kinds must speak (price is mandatory on top
    # of this) — a score alone cannot tell "the tape broke" from "soft intel agrees".
    # confirm_seconds: the same domain signature must be seen twice, that far apart on the
    # MARKET clock. Scanner cycles repeat against one timestamp, so counting calls would
    # manufacture persistence; the clock must actually advance.
    # Days of remaining exchange-calendar coverage below which readiness warns. The
    # bundled NSE holiday list is versioned by year and an uncovered year fails CLOSED —
    # correct, but silent: the system runs, scans and serves a dashboard while placing
    # nothing. Warning early turns a five-month fuse into a routine data update.
    # The unfilled tail of a partially filled ENTRY. The 12s cancel/replace timer stops
    # applying the moment a partial fill lands (it requires position is None AND
    # ENTRY_SUBMITTED), so the remainder used to rest untouched all session — and a late
    # spike could fill it while the protective stop still covered only the first tranche.
    partial_entry_remainder_cancel_enabled: bool = True
    partial_entry_remainder_timeout_seconds: float = 120.0
    calendar_coverage_warn_days: int = 45
    peak_threat_exit_loss_min_domains: int = 2
    peak_threat_exit_loss_confirm_seconds: float = 3.0
    # A same-index loss run used to be measured on the GLOBAL exit tape, so any other
    # index's completed trade cleared it. With four books interleaving, the
    # max_consecutive_losses halt became unreachable: on 2026-08-10 NIFTY lost four
    # times and the streak read 1, 1, 1, 2, so the halt at 3 never fired.
    consecutive_loss_streak_per_index: bool = True
    # Post-entry cross-index correlation read: while a trade is live, if a broad set
    # of the OTHER active indices' live regimes turn against the position (peers need
    # not all correlate — non-directional peers just don't vote), take the profit.
    cross_index_reversal_exit_enabled: bool = True
    cross_index_reversal_min_opposing: int = 2
    # ── Late-entry protect-first-profit (Fix 4) ─────────────────────
    # An entry taken in the final minutes has no runway to run, so mark it
    # scalp-lock (protect first profit) at the live gate.
    late_entry_scalp_enabled: bool = True
    late_entry_scalp_window_minutes: float = 30.0
    trend_option_profit_lock_defer: bool = True
    trend_option_profit_lock_severe_retention: float = 0.50
    # 5m/15m grade-qualified option winners can keep pushing after T1. Do not let
    # the major-win floor ratchet to 99-100% of peak unless adverse evidence exits
    # it first; the peak-threat layer handles true reversal/melt warnings.
    option_major_win_trend_lock_ratio_cap: float = 0.95
    # Broad one-way cascade: when core indices expand in the same direction with
    # range + volume, treat winners as runners, not protected scalps. Entry gates
    # may admit same-side raw signals with tight initial risk, while exit management
    # trails 10-15% off total gains instead of firing ordinary profit locks.
    one_way_cascade_enabled: bool = True
    one_way_cascade_core_votes: int = 3
    one_way_cascade_total_votes: int = 3
    one_way_cascade_volume_votes: int = 3
    one_way_cascade_min_volume_ratio: float = 1.40
    one_way_cascade_min_range_atr_ratio_1min: float = 1.50
    one_way_cascade_min_range_atr_ratio_5min: float = 1.20
    one_way_cascade_min_range_atr_ratio_15min: float = 0.75
    one_way_cascade_min_close_location: float = 0.68
    one_way_cascade_runner_lock_pct: float = 0.85
    one_way_cascade_options_sl_pct: float = 5.0
    one_way_cascade_futures_sl_pct: float = 0.05
    one_way_cascade_rr: float = 1.5
    # Missed signal re-entry: a recent rejected/missed signal can be rebuilt only
    # while the same-side trend/cascade evidence is still fresh and nearby. It is
    # intentionally one-trade-per-index and refuses stale/chase entries.
    # Retrack window: 20s → 360s (6.0 min) after soft reject so 1min + 5min
    # (and 15min) can re-enter if the setup strengthens post-rejection.
    missed_signal_reentry_enabled: bool = True
    # ── Scored re-entry (A/B, 2026-08-12) ────────────────────────────────────
    # The strict path demands FOUR conditions at once — trending regime AND regime
    # aligned AND ADX>=24 AND intel confirmed — on a signal that was already rejected
    # once, with confidence and cascade gating separately. is_trending_regime alone
    # fails on the ~62% of labels that read RANGING, so the other three are rarely even
    # evaluated. Result: 3 re-entries in 19 sessions against 60,425 rejections.
    #
    # Scored mode asks for N of these SIX instead of all of one branch:
    #   confidence, trending_regime, regime_aligns, adx, intel, cascade
    #
    # What it does NOT relax, because these are safety rather than quality: the signal
    # must still be 20s-6min old (a stale seed is a different trade), there must be
    # runway before square-off, the price must not have run past the chase limit, and
    # every repaint rejection still stands. Ships OFF — the strict path is unchanged
    # until this is switched on, which is what makes it an A/B.
    missed_signal_reentry_scored_mode_enabled: bool = False
    missed_signal_reentry_min_conditions: int = 5
    missed_signal_reentry_max_age_minutes: float = 6.0
    missed_signal_reentry_min_age_seconds: float = 20.0
    # 1min + 5min primary retrack; 15min kept for structure flips.
    missed_signal_reentry_timeframes: list[str] = ["1min", "5min", "15min"]
    missed_signal_reentry_max_chase_atr: float = 0.75
    # Cascade chase extension: when a one-way cascade is CONFIRMED (3+ indices
    # synchronized impulse), the 0.75-ATR chase cap is exactly what kept the
    # 2026-07-08 13:35 SENSEX seed out of a -1,185pt crash — by the time the
    # cascade confirms, price has always moved past 0.75 ATR. Cascade-confirmed
    # seeds may chase further (still bounded, still scalp-locked/tight-SL).
    missed_signal_reentry_cascade_max_chase_atr: float = 2.0
    missed_signal_reentry_min_confidence: float = 0.55
    # Exception-route intelligence floors, measured 2026-08-11 against 824 real
    # aggregator reads over the stored index candles. These are the fraction of a
    # fully-informed max-conviction read a candidate must show, and 0.20 admitted 39%
    # of all reads — 2 in 5 is not an exception. 0.35 admits ~15% (1 in 7). Not raised
    # further (0.40 -> 8%, 0.50 -> 2%) because the measurement covers the thin-evidence
    # regime with no option chain; confirm against a live session before tightening.
    #
    # These were 0.20/0.20/0.25 in rp46-2.1 and restore_point_51 too, but the aggregate
    # normaliser changed underneath them: it divided by the COUNT of voting pillars and
    # now divides by their WEIGHT MASS, so the same evidence scores ~3.5x higher and the
    # thresholds were never re-cut to match.
    missed_signal_reentry_min_intel_score: float = 0.35
    missed_signal_reentry_min_trend_adx: float = 24.0
    missed_signal_reentry_min_remaining_minutes: float = 8.0
    missed_signal_reentry_allow_trend_context: bool = True
    missed_signal_reentry_prefer_options: bool = True
    missed_signal_reentry_options_sl_pct: float = 5.0
    missed_signal_reentry_futures_sl_pct: float = 0.05
    missed_signal_reentry_rr: float = 1.5
    # Choppy option scalp protection: once a scalp-lock option trade has already
    # shown a meaningful peak, do not let the generic option breathing room dilute
    # a SMART_PROFIT_LOCK/MAJOR_WIN_GUARD floor. The base arm is Rs.1000; quiet or
    # warning-heavy conditions may lower it to Rs.750/Rs.500 so a low-range day does
    # not need a large absolute move before protecting a premium melt.
    choppy_profit_lock_bypass_peak_rs: float = 1000.0
    choppy_profit_lock_low_move_peak_rs: float = 750.0
    choppy_profit_lock_urgent_peak_rs: float = 500.0
    choppy_profit_lock_low_range_vs_10day: float = 0.65
    choppy_profit_lock_adverse_intel: float = 0.65
    choppy_profit_lock_weak_volume_ratio: float = 1.05
    choppy_profit_lock_premium_melt_ratio: float = 0.45
    choppy_profit_lock_premium_fade_ratio: float = 0.65
    # Trend-day exit hygiene (2026-06-24): the system scalped +Rs.174-694 winners on a
    # +1-1.6% trending day while an older build rode the same names for +Rs.2,400-3,240.
    # These three guards stop premature exits on aligned trend trades. All settings-gated.
    #  - A 15m-aligned entry in a trending regime is a trend pullback, not a scalp, so it
    #    must NOT be forced into scalp_lock_mode (which arms the tightest profit-lock).
    trend_entry_skip_scalp_lock: bool = True
    #  - Do not repaint-abort a position that is currently in profit: the entry is working
    #    regardless of an intra-candle signal flicker on a fast trend.
    repaint_abort_skip_when_profitable: bool = True
    # Options often flicker/repaint during the source candle just before premium expansion.
    # Defer those repaint aborts until candle close unless the premium loss is severe
    # relative to the option breathing room.
    option_repaint_abort_defer_until_close: bool = True
    option_repaint_abort_severe_breathing_mult: float = 2.5
    option_repaint_abort_max_deferred_checks: int = 2
    option_repaint_abort_max_deferred_seconds: float = 55.0
    option_internal_mark_on_stale_quote: bool = True
    option_internal_mark_quote_age_seconds: float = 3.0
    option_internal_mark_min_diff_points: float = 1.0
    option_internal_mark_min_diff_pct: float = 0.5
    option_contract_quote_max_age_seconds: float = 10.0
    option_contract_quote_max_age_next_expiry_seconds: float = 30.0
    #  - PCR is a contrarian indicator; do not let an OI/PCR reversal exit a position that
    #    is aligned with a trending regime (it fires constantly mid-trend).
    oi_pcr_reversal_skip_in_trend: bool = True
    # Tight-trailing timeframe (risky-regime stop trail). Trailing a 15min trade by
    # 1-min candle lows shakes trend trades out on noise — e.g. a +9-pt NIFTY exit
    # right before a +190 move. Use a coarser candle so trend trades breathe. The
    # trade's own timeframe is used when >= this floor; set "1min" for legacy.
    tight_trailing_timeframe: str = "5min"

    # Optional absolute per-trade loss backstop (₹). 0 = DISABLED (default):
    # per-trade risk is governed ONLY by the user's visible settings — the
    # segment stop caps (futures_sl_pct / options_sl_pct) and the daily
    # circuit-breaker budgets (risk_fut_pct / risk_opt_pct). Set >0 only if an
    # extra rupee backstop per trade is explicitly wanted.
    max_trade_loss_abs: float = 0.0
    dynamic_rr_min: float = 1.6 # Minimum initial RR for qualified trades
    dynamic_rr_max: float = 12.0 # Quality-scaled initial RR ceiling; runner mode can capture larger option moves
    dynamic_rr_runner_unlock_ratio: float = 0.85 # T1 progress before removing hard target (lowered so winners flip to trail-and-ride earlier; was 1.00)
    dynamic_rr_runner_lock_pct: float = 0.85 # Runner-mode trailing lock on open gains
    # 5min/1min entries cannot travel the top of the RR ladder within one short bar's
    # horizon before the profit-lock/square-off fires, so cap short-timeframe RR
    # separately from the 15min swing ladder. Set == dynamic_rr_max to disable.
    dynamic_rr_5min_max: float = 2.5
    # RR-scaled position sizing. The legacy `1.0 if rr>=1.5 else rr/1.5` factor was
    # dead (dynamic_rr floors at 1.6), so it never shrank size. When enabled, size is
    # scaled down for low-RR setups toward rr_size_floor and reaches full size once
    # final_rr >= rr_size_reference_rr. Default OFF so live sizing is unchanged until
    # replay-validated.
    rr_size_scaling_enabled: bool = False
    rr_size_reference_rr: float = 2.5
    rr_size_floor: float = 0.5

    # ─── Dashboard & Backtest ────────────────────────────────
    dashboard_port: int = 8001
    dashboard_host: str = "127.0.0.1"
    dashboard_auth_token: str = ""
    dashboard_allowed_origins: list[str] = []
    default_backtest_days: int = 1 # Default backtest window to 1 day as requested
    settings_recalculation_debounce_seconds: float = 8.0
    settings_persistence_debounce_seconds: float = 0.75
    recalculation_worker_enabled: bool = True
    recalculation_worker_timeout_seconds: float = 120.0
    # Keep repeated backtests deterministic by default. Set this true only when
    # a recalculation should also refresh broker candle files before replay.
    backtest_refresh_data_on_recalc: bool = False
    scanner_stale_after_seconds: float = 5.0
    # While historical rebuild / calculation lock is held, use a much longer
    # stale threshold so multi-minute deterministic replays are not "critical".
    scanner_stale_while_busy_seconds: float = 600.0
    api_default_trade_limit: int = 100
    api_max_trade_limit: int = 2000
    api_default_chart_bars: int = 300
    api_max_chart_bars: int = 1000
    outcome_calibration_enabled: bool = True
    production_safe_mode_required: bool = True
    # REAL mode can be armed before the cash-market open so broker feeds,
    # reference quotes, expiries, and option intelligence are warm before 09:15.
    # Order placement remains blocked by market-open and strategy session gates.
    real_mode_premarket_arm_start: str = "09:00:00"
    real_mode_premarket_settle_end: str = "09:08:00"
    real_mode_premarket_refresh_end: str = "09:14:30"
    real_mode_live_start: str = "09:15:00"
    real_mode_live_end: str = "15:30:00"
    real_mode_mid_session_restart_refresh_end: str = "15:00:00"
    # Runtime-only helper: when the app is left running in HISTORICAL mode,
    # arm REAL/manual mode at the premarket window. It never enables Auto Mode.
    auto_real_mode_premarket_enabled: bool = True
    auto_real_mode_premarket_switch_time: str = "09:00:00"
    auto_real_mode_premarket_retry_seconds: float = 30.0

    # ─── Signal engine selector ──────────────────────────────
    # Which raw-signal scanner feeds the pipeline. Default "quad_prime"
    # (all four sources can enter; opposite exits need cross-family confirm).
    # Prior default was "utbot_consensus"; set "utbot" for pure UT1 baseline.
    # "ema": EMA(9/21) crossover. "consensus": Supertrend(10,3) AND EMA(9/21) must
    # agree (holds through disagreement) — far less whippy than UT1 in backtests.
    # Direction comes from the chosen source; the protective stop stays the UT ATR
    # trail, and all downstream grading/gates/exits are untouched. "quad_union"
    # unions UT1 + EMA + Supertrend + Consensus raw arrows after repaint buffering.
    # "utbot_prime": UT1 is the SOLE entry trigger (no union dilution — preserves
    # UTBot's high-frequency options edge); EMA(9/21) + Supertrend(10,3) + their
    # Consensus are used ONLY to RANK each UT1 entry by agreement (more agree ->
    # higher conviction -> small capped confidence boost), never to add their own
    # slower, theta-decayed entries. Exits stay pure UTBot. This is the "rank, don't
    # union" answer to quad_union diluting UTBot's edge.
    # "quad_prime": all four raw sources can enter like quad_union, but opposite exits
    # need cross-family confirmation (UT1 + EMA/Supertrend/Consensus) unless the
    # trade is losing or protected profit is threatened.
    # "adaptive_hybrid": Engine 10 meta-router. It evaluates UTBot/EMA/Supertrend/
    # Consensus flips, then admits the source mix that fits current candle context:
    # trend/opening drive, power thrust, balanced UTBot+Consensus, or strict
    # consensus in chop. It is intentionally not a blind union.
    # "adaptive_dual": Engine 11. Lane A is adaptive_hybrid v2; Lane B runs the
    # uploaded APEX v5.5 five-gate confluence engine (regime, HTF direction,
    # score, conflict margin, volume/body confirmation), then sends both lanes
    # through the same gates, sizing, quote, risk, and exit stack.
    # "quality_hybrid": Engine 12. Independent confluence scanner from the ATR
    # replacement benchmark: Consensus ST+EMA, EMA, Aroon, Donchian, KAMA, and
    # session VWAP vote on direction. UTBot/ATR is retained only for stop/risk
    # arrays and downstream lifecycle compatibility, not as the entry trigger.
    scanner_engine: Literal[
        "utbot",
        "ema",
        "consensus",
        "auto",
        "utbot_ema",
        "utbot_consensus",
        "quad_union",
        "utbot_prime",
        "quad_prime",
        "adaptive_hybrid",
        "adaptive_dual",
        "quality_hybrid",
    ] = "quad_prime"
    # Quad Prime raw-entry hygiene: raw source access is allowed, but weak solo flips
    # must not skip all live quality floors or every winner becomes a tiny scalp while
    # low-confidence losers keep wide option stops.
    quad_prime_raw_min_confidence: float = 0.50
    quad_prime_raw_multi_source_min_confidence: float = 0.55
    quad_prime_raw_runner_min_confidence: float = 0.70
    # ── Engine 10 v2: regime-switched delegation to the proven engines ──
    # v1 re-invented selection from raw arrows and lost 10x to the engines it
    # routed (three 30d backtests Rs.17-21k vs Rs.190k). v2 emits ONLY the two
    # proven engines' flip streams — trend/opening state passes the full
    # utbot_consensus union; chop demands utbot_prime-style cross-source
    # agreement or strict consensus; volume thrust enters fast. v2 signals are
    # NEVER raw-union flagged: they get full normal grading/RR/runner/FUT
    # economics, exactly like utbot_consensus.
    adaptive_hybrid_trend_adx: float = 25.0
    adaptive_hybrid_trend_er: float = 0.68
    adaptive_hybrid_opening_er: float = 0.58
    adaptive_hybrid_chop_min_agreement: int = 2
    adaptive_hybrid_thrust_volume_ratio: float = 1.8
    adaptive_hybrid_thrust_range_atr: float = 1.15
    adaptive_hybrid_thrust_close_loc: float = 0.62
    # Engine 11 dual-lane safety. Selecting adaptive_dual is the main gate; this
    # toggle lets the second APEX lane be disabled without losing Lane A.
    dual_lane_enabled: bool = True
    dual_lane_same_dir_min_bars_gap: int = 3
    dual_lane_same_dir_min_lane_a_r: float = 0.50
    dual_lane_size_fraction: float = 0.65
    dual_lane_max_index_exposure_rs: float = 12000.0
    dual_lane_min_lane_b_confidence: float = 0.70
    dual_lane_jaccard_alert_threshold: float = 0.30
    # Engine 12 independent confluence thresholds.
    quality_hybrid_min_vote_score: float = 3.0
    quality_hybrid_broad_agreement: int = 4
    quality_hybrid_chop_min_agreement: int = 4
    quality_hybrid_trend_adx: float = 22.0
    quality_hybrid_trend_er: float = 0.50
    quality_hybrid_thrust_volume_ratio: float = 1.6
    quality_hybrid_thrust_range_atr: float = 1.05
    quality_hybrid_thrust_close_loc: float = 0.60
    # Macro-event blackout: block fresh ENTRIES during user-listed event windows
    # (data_store/event_blackouts.json: [{date,start,end,label}], e.g. RBI policy,
    # budget). DEFAULT OFF — zero behavior change until enabled. Exits/trailing
    # on open trades are never affected.
    event_blackout_enabled: bool = False
    engine_lab_enabled: bool = True # Shadow-run all 12 engines for comparison; never submits orders
    engine_lab_real_mode_only: bool = False # False also shows lab data during historical/offline scans
    # Live/REAL mode refreshes the lab on a worker thread at most this often; the scan
    # loop always serves the cached snapshot so the 100ms trade monitor never stalls.
    engine_lab_refresh_seconds: float = 10.0
    # ─── UT1 warmup stance seed ────────────────────────────
    # UTBot's flip needs price to CROSS the ATR trail. On the rare window where the
    # ATR warm-up parks the trail on the wrong side of a one-way market from the very
    # first bars (e.g. relentless gap-and-trend phase), no crossover ever happens and
    # UT1 sits FLAT while every bar trends. The seed emits ONE synthetic entry per
    # session+direction when ALL of: zero flips in the window, price pinned on one
    # side of the trail for N bars, ADX >= floor, EMA(9/21) agrees, and net price
    # change over the stance run agrees. Protective stop = the ATR trail (already on
    # the correct protective side). Downstream grading/gates apply unchanged.
    utbot_warmup_seed_enabled: bool = True
    utbot_warmup_seed_min_stance_bars: int = 15
    utbot_warmup_seed_min_adx: float = 20.0
    # auto mode: per-bar, use consensus (low-noise) when ADX confirms a trend, else EMA
    # (more adaptive in chop). Gets consensus's trend precision + EMA's chop-resilience.
    scanner_auto_trend_adx: float = 25.0
    # utbot_prime ranking: confidence boost added PER agreeing partner (EMA / Supertrend /
    # Consensus) at the UT1 entry bar, capped by the max. Opt-in mode only — no effect
    # unless scanner_engine == "utbot_prime". Conservative defaults; raise to weight
    # trend-agreement harder, set per-agreement to 0.0 to disable ranking (pure UTBot).
    utbot_prime_confidence_per_agreement: float = 0.03
    utbot_prime_max_confidence_bonus: float = 0.09
    # When a trend engine (ema/consensus) is active, its flips are already smoothed,
    # confirmed trend signals — not UTBot's noisy single-bar flips. So a 15m-aligned,
    # confidence-qualified trend-engine flip bypasses the impulse-context / choppy
    # confirmation gates (which exist to filter UT1 noise). The confidence floor and
    # 15m-agreement requirement still apply. No effect when scanner_engine == "utbot".
    trend_engine_relaxed_gate: bool = True
    # Combo engines only: a SOLO UT1 opposite-flip must not exit a live trade by itself —
    # UT1 is the jumpy source, so its exits need the partner (ema/consensus) to confirm.
    # The partner emits its own opposite signal when it flips, so the trade simply rides on
    # the partner's (slower, reliable) judgement + the protective stop until then. Entries
    # are unaffected (UTBot's frequency is the combo's entry edge). Toggle off for raw union.
    combo_partner_confirms_exit: bool = True

    # Option rollover now measured in TRADING days (holiday-aware), not calendar days.
    # Roll to next expiry when this many trading sessions (or fewer) remain to expiry.
    # 1 => roll only on the last trading day before expiry (+ expiry day); the system
    # trades the current expiry one session longer than the old T-2 calendar rule.
    option_rollover_working_days: int = 1

    # ─── Expiry-Day Gamma Mode ───────────────────────────────
    # Even with the 1-trading-day rollover above, the system still rolls to next week's
    # options on T-1 + expiry day — exactly when ATM gamma is highest and premiums can
    # 2-3x. When this is ON, on expiry day (and optionally T-1) the system trades the
    # CURRENT expiring ATM (max gamma) instead of rolling — but only
    # in the morning, before the afternoon theta-crush. Default OFF (unchanged rollover).
    expiry_day_trade_current_week: bool = False
    # Cutoff after which the expiry move is usually done and theta decay dominates. Before
    # the cutoff -> current-week gamma; at/after -> see expiry_day_after_cutoff_mode.
    expiry_day_gamma_cutoff: str = "13:00"
    # Also apply gamma mode on T-1 (the day before expiry), not just expiry day itself.
    expiry_day_include_t1: bool = False
    # After the cutoff on expiry day:
    #   "rollover"        -> safest: trade next week's options (the unchanged behaviour)
    #   "momentum_current"-> keep current-week ATM but only the system's momentum-qualified
    #                        entries pass (big, short-tenure moves) — others fall through
    #   "futures"         -> trade the index FUTURE (no theta) instead of options
    expiry_day_after_cutoff_mode: Literal["rollover", "momentum_current", "futures"] = "rollover"
    # ── Expiry-day theta-crush risk controls (apply only to current-expiry gamma trades) ──
    # Expiring ATM options decay to zero fast and can gap hard, so gamma trades get a
    # TIGHTER stop than the normal options_sl_pct (caps the theta-crush loss).
    expiry_day_gamma_sl_pct: float = 10.0
    # Force-square-off gamma trades by this time — never hold an expiring option into the
    # afternoon decay even if SL/target haven't hit. Should be <= expiry_day_gamma_cutoff.
    expiry_day_gamma_time_exit: str = "13:30"
    # Only enter a current-expiry gamma trade on strong momentum (a big short-tenure move);
    # weak/chop entries are skipped so theta doesn't bleed a directionless premium.
    expiry_day_gamma_require_momentum: bool = True
    # Expiry-day LATE window: the big settlement move often comes in the last hour, so on
    # expiry day the system RE-ENGAGES current-expiry gamma between these times (in either
    # direction) — separate from the morning window, skipping the midday lull. Entries are
    # still momentum-gated, and the EOD square-off still flattens everything by force-exit.
    expiry_day_late_window: bool = True
    expiry_day_late_window_start: str = "14:00"
    expiry_day_late_window_end: str = "15:15"
    # Time exit for the late cohort. The comment above said "the EOD square-off still
    # flattens everything by force-exit", and that was literally all that stood between a
    # late-window position and settlement: the 13:30 gamma exit is already past when this
    # window opens, and its entry-time filter drops everything entered after the 13:00
    # cutoff. Defaults ahead of ut_force_exit_time (15:15) so an expiring contract is
    # closed on its own schedule rather than in the final-minutes liquidity crunch,
    # alongside every other force-exited position. Moved 15:20 -> 15:10 with the
    # 2026-08-11 schedule change; at 15:20 it sat AFTER the new force-exit, which
    # inverts the ordering this setting exists to guarantee.
    expiry_day_late_window_time_exit: str = "15:10"

    # ─── UT Bot Core Settings ────────────────────────────────
    ut_preset: str = "AGGRESSIVE"
    ut_atr_period: int = 10
    ut_atr_multiplier: float = 1.0
    ut_use_heikin_ashi: bool = False
    ut_signal_mode: Literal["realtime", "confirmed"] = "realtime"
    ut_adx_filter: bool = True      # ADX is informational, not a signal blocker
    ut_adx_period: int = 14
    ut_adx_threshold: float = 25.0
    ut_sizing_max_base_multiplier: float = 5.0 # Cap raw risk sizing as a multiple of configured base quantity
    ut_strict_adx: bool = False      # Keep UT marker timing aligned with TradingView; ADX is graded later
    ut_smart_trailing: bool = True   # Trailing stop follows the trade's timeframe
    ut_regime_adaptation: bool = True # Enable dynamic regime adaptation for choppy markets
    ut_concurrency_guard: bool = True # Enable concurrency guard to prevent overlapping index trades
    ut_correlated_index_guard: bool = True # Limit same-direction exposure across highly correlated core indices
    ut_correlated_core_limit: int = 2 # Maximum simultaneous same-direction NIFTY/BANKNIFTY/SENSEX exposures
    ut_correlated_allow_aplus_override: bool = True # Permit a third core exposure only when every setup is A+
    ut_fractal_confluence: bool = True # Allow 1m entries if 5m trend aligns
    ut_session_filter: bool = True   # Enable session filtering in the engine
    ut_intel_early_exit: bool = True # Enable intelligence-based early exits
    # OI_PCR_REVERSAL must prove the book turned SINCE entry, not merely that it
    # leans against the trade now. Off = any qualifying reading exits, including the
    # first one an entry made before the day's option chain existed ever saw.
    intel_reversal_requires_new_evidence: bool = True
    ut_dynamic_confidence: bool = True # Enable dynamic confidence threshold
    ut_backtest_more_results: bool = True # Loosen historical-only gates for exploratory backtests
    futures_shadow_enabled: bool = False # Research-only FUT comparison rows for historical AUTO/Hybrid; never places live orders
    ut_session_start: str = "09:18"
    ut_early_session_cutoff: str = "09:20" # Stricter 5min-quality window applies only until this time (aligned to session start, not a trading-start delay)
    ut_session_end: str = "15:25"
    # ─── Operator session schedule (2026-08-11) ──────────────────────────────
    #   14:55  last 15min entry
    #   15:01  last 5min/1min entry, and the moment EOD protection engages
    #   15:15  full exit
    # The three are deliberately consistent: eod_protect_window_minutes is set so that
    # protection begins exactly when the last entry is allowed, rather than 30 minutes
    # before force-exit. Until 2026-08-11 the 5min cutoff (15:15) sat 20 minutes INSIDE
    # the protection window, so fresh positions were opened into a regime whose own EOD
    # logic closed them on the next tick — four such entries on 11 Aug round-tripped in
    # 0.06-0.19s, each paying ~Rs.80 of round-trip charges for a fraction of a second of
    # exposure, and across the stored book that window cost -Rs.8,864 over 13 trades.
    ut_no_entry_after: str = "14:55" # Block fresh 15min entries at/after this IST time
    ut_5min_no_entry_after: str = "15:01" # Qualified 5min/1min entries have a later cutoff
    # Enforces the invariant above: whatever the two cutoffs say, no entry is admitted
    # once EOD protection is active. Off restores the previous behaviour of letting the
    # entry gate and the EOD exit logic contradict each other.
    block_entries_during_eod_protect: bool = True
    ut_force_exit_time: str = "15:15"
    trade_product_type: Literal["CARRYFORWARD"] = "CARRYFORWARD"
    live_filter_leniency_pct: float = 0.0 # Keep live gates strict; loosen only during deliberate diagnostics
    intelligence_cache_ttl_seconds: float = 90.0 # Option-chain cache TTL for live intelligence
    # How long a COMPUTED intelligence bundle stays usable. This used to be the line
    # above, which is a network budget: the option chain is a broker round trip, so
    # re-fetching it every few seconds is wasteful and 90s is right for it. The analysis
    # on top is candle math against an already-cached chain, and it was inheriting the
    # network's budget for no reason — leaving the evidence behind an entry decision up
    # to 90 seconds old, which on a 1min setup is a bar and a half.
    # Measured: 48ms per instrument-timeframe with a 40-strike chain, 17ms without. Only
    # the pair actually being read recomputes, not all twelve. At 30s that is well under
    # 1% duty. Lower it if you want fresher evidence and can spare the CPU; a genuinely
    # per-tick bundle is not on the table (all twelve pairs is ~580ms).
    intelligence_analysis_ttl_seconds: float = 30.0
    # Ceiling on the profit-retention ladder, as a fraction of peak P&L. The major-win
    # rung used to run to 1.0 at a Rs.5000 peak, which places the protective floor ON the
    # peak price — breached by the very next tick, so the "trailing lock" became a fixed
    # take-profit at Rs.5000. Bounded to [0.50, 0.99] in code; 1.0 is not selectable.
    # Matters more as size grows: at 10 NIFTY lots Rs.5000 is under Rs.7 of premium.
    # Breathing space the ladder leaves on a peak, as a band. The operator's rule:
    # 20-25% on the current tiers. The tier progression still decides WHERE in the band a
    # trade sits — more gain, tighter lock — but it can no longer squeeze past 20% or
    # loosen past 25%.
    #
    # Before this the ladder ran 41% breathing at a Rs.800 peak down to 5% at Rs.4,000+,
    # so a big winner was held on a 5% leash and a small one was given nearly half its
    # gain back. Both ends are now inside the band.
    # Outer bounds of the operator's breathing schedule (2026-08-08). These are a CLAMP,
    # not the schedule itself — the tiers in _profit_lock_floor set the shape:
    #   < Rs.1,000/lot   50% breathing   (a young winner needs room)
    #   Rs.1,000-2,000   35% -> 30%
    #   Rs.2,000-3,000   25% -> 20%
    #   Rs.3,000+        15% -> 10%      (protect a big gain)
    # The previous 0.75/0.80 pair flattened every tier to 20-25%, which is far too tight
    # on an index option at a Rs.600 peak.
    profit_lock_max_ratio: float = 0.90      # 10% breathing — the tightest allowed
    profit_lock_min_ratio: float = 0.50      # 50% breathing — the loosest allowed
    # Let the option STOP flex with the intelligence score, the way RR, target and exit
    # already do. Of those four only the stop ignored intelligence entirely, moving with
    # regime and ATR alone. Above the neutral 50 the stop widens slightly (corroborated
    # conviction earns room to be right); below it, it tightens.
    #
    # Default OFF: this changes the risk taken on EVERY option entry, which is a strategy
    # decision, not a defect being repaired. The shift is capped so a stop chosen by ATR
    # and the per-timeframe rules is nudged, never redefined.
    dynamic_stop_from_intelligence: bool = False
    dynamic_stop_intel_max_shift_pct: float = 20.0
    # Let measured market conviction move lot SIZE, not just lot COUNT. Cascade and
    # heavy-trend already scale the number of trades, but size itself was a
    # user-configured constant with only those coarse on/off multipliers.
    # Options only — FUT is hard-capped at fut_lots_hard_cap by operator rule. Only
    # ABOVE-neutral conviction adds size; shrinking is the entry filters' job.
    # Default OFF: sizing up is how a good idea becomes an expensive one.
    market_driven_lot_size: bool = False
    market_lot_size_max_boost_pct: float = 50.0
    market_lot_size_max_lots: int = 4
    # Let MACD acceleration decide how patient an ENTRY bid can be. The broker chooses
    # MARKET vs LIMIT on confidence alone — how good the setup is — and never on how fast
    # the premium is leaving the bid, so a strong signal into an accelerating move rested
    # a limit behind the market. Urgency RAISES the confidence bar a limit must clear; it
    # never lowers it, so this can only make the system less willing to wait, never more.
    # Default OFF: it changes fills, and a missed fill and a paid spread are both real.
    macd_aware_entry_pricing: bool = False
    macd_entry_urgency_max_penalty: float = 0.15
    # Treat intelligence that is DECAYING as a loss-guard signal, not just intelligence
    # that is low. Every pillar the guard reads is a point-in-time value; the snapshot
    # store has held the whole session's history and only a chart overlay ever read it.
    # A trade whose evidence has been sliding for a quarter hour is deteriorating even
    # while price sits still. Default OFF: it makes exits stricter.
    loss_guard_intel_decay_enabled: bool = False
    loss_guard_intel_decay_window_seconds: float = 900.0
    loss_guard_intel_decay_min_slope: float = -1.0   # points per minute
    loss_guard_intel_decay_severity: int = 1
    # ── scale-in (adding lots to a position that is already working) ─────────────────
    # Until now nothing anywhere increased an open trade's quantity: "cascade lot-adding"
    # opens SEPARATE trades across timeframes, which is a different thing.
    #
    # Default OFF. Turning it on changes how much money is at risk per idea, so it is an
    # explicit decision. The four design choices behind it, each made deliberately:
    #   * ONE shared stop, re-armed for the full quantity. Every exit path in this system
    #     assumes one position with one stop; giving a trade two stops would mean teaching
    #     all of them about legs.
    #   * The profit ladder reads the COMBINED position — so run this with
    #     profit_lock_per_lot_basis on, or adding size fakes a bigger win and the ladder
    #     scalps a trade that has barely developed.
    #   * WEIGHTED-AVERAGE entry price, with the original rupee risk FROZEN. Averaging up
    #     moves the stop distance, which would silently redefine 1R and change every
    #     R-based exit rule because of a sizing decision rather than a market move.
    #   * An add-on does NOT consume a fresh position slot (it is not a new trade) but it
    #     IS new money at risk, so it must pass the rupee-risk check and is refused when
    #     it does not fit.
    scale_in_enabled: bool = False
    scale_in_max_adds_per_trade: int = 2
    scale_in_lots_per_add: int = 1
    # An add-on is only allowed on a trade that is ALREADY working: this much of the
    # original risk in open profit, measured in R. Adding to a loser is averaging down.
    scale_in_min_profit_r: float = 0.75
    # ...and only while the regime still points the trade's way.
    scale_in_requires_trend: bool = True
    # Hard ceiling on total lots after adding, whatever the adds-per-trade cap allows.
    scale_in_max_total_lots: int = 6
    auto_option_chain_min_strikes: int = 3 # Minimum fresh active-expiry strikes required for OPT Auto
    vix_percentile_min_sessions: int = 20 # Minimum daily India VIX observations before volatility routing is trusted
    vix_live_cache_seconds: float = 60.0 # Avoid repeated India VIX REST calls across index scans
    signal_vix_high_threshold: float = 20.0 # Configurable high-volatility grading threshold
    signal_vix_low_threshold: float = 12.0 # Configurable low-volatility grading threshold
    option_premium_cache_ttl_seconds: float = 2.0 # Open option premium refresh TTL
    index_ltp_cache_max_age_seconds: float = 6.0 # Scanner-loop index quotes must be this fresh; REST refresh happens off-loop
    contract_ltp_cache_max_age_seconds: float = 10.0 # Scanner-loop derivative quotes must be this fresh
    contract_ltp_background_workers: int = 2 # Bound daemon REST refreshes when live caches miss
    contract_ltp_background_retry_cooldown_seconds: float = 2.0 # Avoid 100ms retry storms after quote failures
    contract_ltp_background_result_limit: int = 256 # Bound completed background quote state
    fyers_chain_background_workers: int = 1 # Bound no-timeout Fyers option-chain SDK refreshes
    option_chain_background_workers: int = 2 # Bound full PCR/OI chain refreshes outside ScannerEventLoop
    option_chain_background_retry_cooldown_seconds: float = 5.0 # Avoid cold-chain retry storms after provider failure
    dynamic_subscription_background_workers: int = 2 # Bound slow WebSocket subscribe sends outside ScannerEventLoop
    history_cache_ttl_seconds: float = 240.0 # Broker history cache TTL for candles/volume
    # Broker history request pacing (requests/second). Too aggressive a rate trips
    # AngelOne's history limit, which forces the Yahoo fallback — and Yahoo reports
    # volume=0 for Indian index tickers, killing every volume-gated entry path.
    history_rate_limit_per_second: float = 0.35
    option_history_miss_ttl_seconds: float = 900.0
    live_index_timeout_seconds: float = 9.0 # Per-index live scan budget before serving a stale fallback that cycle. Raised 6->9 so heavy indices (SENSEX/BANKNIFTY, large option chains) finish during volatile periods. NOTE: this is a CAP, not a wait — fast indices still return early, so the higher cap costs nothing on healthy cycles.
    # Background broker position-reconciliation cadence. Base rate applies while
    # positions are open (need close-reconciliation); idle rate when flat (nothing
    # to reconcile); exponential backoff up to max when the broker API fails
    # (rate-limit/timeout). Stops the every-5s get_positions() from spamming a
    # rate-limited broker and starving the data-API budget.
    position_sync_interval_seconds: float = 5.0
    position_sync_idle_interval_seconds: float = 30.0
    position_sync_max_backoff_seconds: float = 60.0
    live_calculation_lock_timeout_seconds: float = 12.0 # Live lock recovery budget; historical work gets a larger derived budget
    scanner_exception_backoff_seconds: float = 1.0 # Fast recovery after an unexpected scanner-loop exception
    morning_refresh_parallelism: int = 3 # Bound broker pressure while warming independent indices
    # Morning refresh quality gate: only mark the day "complete" when thresholds pass.
    # Incomplete days keep missing_keys and can partial re-warm + 09:15 catch-up.
    morning_refresh_require_history: bool = True
    morning_refresh_require_expiry: bool = True
    morning_refresh_require_all_quotes: bool = True
    morning_refresh_require_all_chains: bool = True
    morning_refresh_require_vix: bool = False  # VIX often flaky pre-open; optional
    morning_refresh_catchup_at_live_open: bool = True  # second pass at/after 09:15 if incomplete
    morning_refresh_max_attempts: int = 3  # full+partial retries per session day
    runtime_cache_prune_interval_seconds: float = 60.0 # Physically evict expired quote/decision cache entries
    trade_state_checkpoint_seconds: float = 1.0 # Coalesce mark-to-market persistence while keeping crash recovery fresh
    trade_stagnation_minutes: float = 20.0 # Minimum profitable hold before stagnation exit can trigger
    trade_stagnation_max_extra_minutes: float = 10.0 # Extra breathing time for volatile indices/regimes
    trade_stagnation_min_peak_r: float = 0.50 # Require meaningful peak profit relative to initial risk
    circuit_breaker_slippage_bps: float = 10.0 # Conservative projected exit slippage for open-position loss checks
    position_sizing_min_atr_fraction: float = 0.10 # Stop-distance floor used for sizing only
    position_sizing_min_price_fraction: float = 0.00005 # Absolute fallback when ATR is unavailable
    live_signal_stabilization_seconds: float = 20.0 # Legacy/default anti-flicker window for non-5m/15m paths
    live_signal_stabilization_seconds_5min: float = 30.0 # Repaint-check buffer before 5min live entry filtering
    live_signal_stabilization_seconds_15min: float = 30.0 # Repaint-check buffer before 15min live entry filtering
    midcap_15min_risky_stabilization_seconds: float = 60.0 # Extra buffer for MIDCPNIFTY 15min risky option signals in chop/weak tape
    midcap_15min_risky_stabilization_min_scans: int = 3 # Require repeated confirmation when MIDCPNIFTY 15min volume/intel is weak
    midcap_15min_risky_stabilization_min_volume_ratio: float = 1.05 # Basic volume floor for MIDCPNIFTY 15min risky stabilization
    midcap_15min_risky_stabilization_min_intel_score: float = 0.35 # Absolute signed-intel floor to skip the extra MIDCPNIFTY buffer
    live_15min_entry_cutoff_grace_seconds: float = 90.0 # Let a 15min signal formed by cutoff finish its live buffer
    live_intrabar_signal_entries: bool = True # Detect forming-candle UT1 arrows before candle close
    # ─── Late-Session Power-Move Override ────────────────────
    # A data-confirmed "big move" may bypass the grade floor (any time) and the
    # 15:00 no-entry cutoff (after 15:00, up to the hard cutoff), with tight scalp-lock risk.
    power_move_override_enabled: bool = True
    power_move_volume_mult: float = 2.5      # candle volume >= this x recent average
    power_move_range_atr_mult: float = 2.0   # candle true range >= this x ATR
    power_move_close_location: float = 0.70  # directional close >= this fraction of candle range
    power_move_adx_threshold: float = 20.0   # momentum: ADX >= this (lower than general 25; ADX lags spikes)
    power_move_momentum_volume_mult: float = 3.5 # momentum substitute: volume >= this x avg counts as momentum
    power_move_volume_lookback: int = 20     # candles for the volume average
    # A power move is the one lane that BYPASSES ut_no_entry_after entirely (see
    # power_move_cutoff_ok in signal_processor), so this is its only cutoff. Held at the
    # 5min entry time: at 15:15 it would have admitted a fresh entry at the very moment
    # of force-exit, which is the instant round-trip the 2026-08-11 schedule change
    # exists to stop — the bypass has to respect the schedule or it re-opens the hole.
    power_move_late_hard_cutoff: str = "15:01" # no power-move entry after this (= the 5min cutoff)
    # Fast 1min/5min volume-spike entry. 1min scalp entries can be disabled
    # separately without disabling 1min tracking/trailing/emergency context.
    # When 1min scalping is OFF, post-exit reversion can still admit 1min rows
    # only inside an open reversion window (decoupled toggles).
    volume_spike_entry_enabled: bool = True
    one_min_scalping_enabled: bool = True
    # 1min continuous scalps require high-burst/spike context (ADX/intel/power)
    # plus volume/range/close floors — not weak 1min noise.
    one_min_scalping_high_burst_only: bool = True
    # A cash index publishes no traded volume in real time: measured 13 of the last 20
    # 1min bars carry volume 0 on every index (5min 1/20, 15min 0/20), because it only
    # arrives with the periodic Fyers backfill. The signal bar of a fresh 1min entry is
    # therefore almost always unmeasurable, and the surge ratio read 0.00x — which is
    # why no 1min trade has ever fired. tick_count is the only live participation
    # measure available. Ships OFF until a session of real tick data exists to cut the
    # threshold against; tick counts are far less dispersed than traded quantity, so the
    # traded floor cannot simply be reused on them.
    surge_tick_proxy_enabled: bool = True
    # Calibrated on the real 1min candles, not guessed: ratio>=2.2 admits 4.73% of
    # measurable traded-volume bars and rank>=97 admits 4.98% — matched strictness in a
    # form that survives the change of measurement basis.
    surge_tick_percentile_floor: float = 97.0
    volume_spike_entry_min_volume_ratio: float = 2.5
    volume_spike_entry_strong_1min_min_volume_ratio: float = 2.5
    volume_spike_entry_strong_1min_adx_threshold: float = 25.0
    volume_spike_entry_strong_1min_intel_threshold: float = 0.70
    volume_spike_entry_min_range_atr_ratio: float = 1.5
    volume_spike_entry_min_close_location: float = 0.65
    volume_spike_entry_futures_sl_pct: float = 0.05
    volume_spike_entry_options_sl_pct: float = 5.0
    volume_spike_entry_min_rr: float = 2.5
    volume_spike_entry_1min_scalp_rr: float = 1.5
    # 1M SPIKE anti-repaint buffer. Aggressive floor = 3s (configurable in advanced
    # settings). 5m/15m use live_signal_stabilization_seconds_* (30–60s) separately.
    volume_spike_entry_stabilization_seconds: float = 3.0
    volume_spike_entry_stabilization_seconds_min: float = 3.0
    volume_spike_entry_stabilization_seconds_max: float = 15.0
    # When L1 depth/OFR is usable, 1min spikes must align with book pressure
    # (BUY needs OFR >= bullish floor; SELL needs OFR <= bearish ceiling).
    # If depth is unavailable, spike still qualifies on candle thrust alone.
    volume_spike_depth_confirm_enabled: bool = True
    volume_spike_depth_ofr_bullish: float = 1.08
    volume_spike_depth_ofr_bearish: float = 0.92
    stabilization_momentum_override_enabled: bool = True # Re-admit flickered signals when thrust keeps confirming direction
    # Drop the weakest branch of that override without disabling the whole mechanism.
    # The override is a chain of ALTERNATIVES (first match wins), and "regime flip" —
    # trending regime pointing the same way as the signal, with no price, volume or
    # structure test — accounted for 653 of 1327 re-admissions across every session log
    # (49%), versus 3 from the strict directional-thrust branch. Turning this ON keeps
    # thrust, cascade, late-reclaim, market-wide-reversal, VWAP and structure while
    # removing the branch that is nearly always true in a trending tape.
    # Default False: the measured edge against clean signals was NOT significant
    # (39% vs 50% win rate, median -151, N=56, p=0.21), so this is a deliberate opt-in.
    stabilization_override_drop_regime_flip: bool = False
    # Refuse a LONG while a SHORT is live on the same index (and vice versa). The rest of
    # the concurrency guard reasons about timeframe, lane, instrument type and count and
    # never once about DIRECTION, so with ut_concurrency_guard off the only limit on
    # holding both sides is max_trades_per_index. Options here are always BOUGHT, so a
    # long CE beside a long PE is two premiums and two spreads with theta against both —
    # a straddle assembled by accident from two signals that each looked fine alone.
    # Default ON: a reversal that arrives AFTER the first trade exits is untouched, and a
    # deliberately armed leg (pullback hedge, dual-lane reversal) is exempt. Turn it off
    # only to deliberately run both sides of an index at once.
    block_opposite_direction_stack: bool = True
    # Read the profit-retention ladder PER LOT instead of in absolute rupees. Its tiers
    # (500 / 1000 / 2000 / 3000) were tuned on single-lot trades, so on 3 lots a Rs.3000
    # peak — Rs.1000 per lot, a modest win — lands on the Rs.3000 MAJOR_WIN_GUARD rung and
    # locks 90%, scalping a position that has barely developed.
    # Default OFF because per-lot is LOOSER, not safer: a multi-lot winner lands on a lower
    # rung and is allowed to give back more before the lock fires. More principled, but a
    # real risk increase on live money, so it stays the operator's decision.
    profit_lock_per_lot_basis: bool = False
    # Refuse an entry that would push the book past its stop-risk budget. The daily
    # circuit breaker measures a book that already exists and halts after the fact; this
    # refuses the entry that would create the breach, using the SAME segment and global
    # budgets so the two cannot drift apart.
    # Off by default because at one or two concurrent positions it duplicates the breaker.
    # It earns its keep once several lots run across several indices with cascade scaling
    # on — which is exactly when the breaker's after-the-fact halt arrives too late.
    entry_risk_budget_enabled: bool = False
    # Hold a MARGINAL profit-taking option exit for a few seconds while the quoted spread
    # is wider than the edge being captured — on an index option, exiting at the bid
    # routinely converts a small win into a small loss.
    # Off by default: it DELAYS an exit, and every deferral is a risk the position moves
    # against you while it waits. Bounded by spread_exit_deferral_max_seconds, restricted
    # to profitable positions and to the reasons in Scanner._MARGINAL_EXIT_REASONS, and
    # bypassed entirely by every protective/loss/forced/expiry/broker/manual exit and by
    # an exchange stop that is already armed and crossed.
    # Have the 15:25 force exit also square off BROKER-ONLY positions this system did not
    # open. Off by default: an orphan may be a position taken manually at the Zerodha
    # terminal, and squaring one off uninvited is worse than the exposure it removes.
    # Left off, the session end escalates instead — the position still carries overnight,
    # which CARRYFORWARD/NRML means the broker will not prevent either.
    # Turn it on only if you never hold manual positions in this account.
    force_exit_flattens_orphans: bool = False
    spread_aware_exit_enabled: bool = False
    spread_exit_deferral_min_spread_pct: float = 0.35   # below this the book is fine
    spread_exit_min_net_profit_rs: float = 250.0        # bid-side profit worth waiting for
    spread_exit_max_cost_to_profit_ratio: float = 0.35  # spread cost vs the edge
    spread_exit_deferral_max_seconds: float = 8.0       # hard ceiling on any hold
    # Tape hygiene: a 1min signal can ONLY enter via the volume-spike/scalp
    # filter, so 1min signals that never cleared it cannot trade. When True, the
    # raw-detection and rejection lines for such no-spike 1min signals are kept off
    # the live tape (downgraded to a debug file log); reject diagnostics still count.
    live_1min_log_only_volume_spike: bool = True
    # Late-session synchronized move: catches broad 1m+5m directional flips after
    # a range/chop tape only when the basket proves reclaim/rollover structure.
    # This is narrow by time, direction, breadth, and scalp-risk settings.
    late_session_reclaim_override_enabled: bool = True
    late_session_reclaim_start: str = "14:25"
    late_session_reclaim_end: str = "15:10"
    late_session_reclaim_core_votes: int = 3
    late_session_reclaim_total_votes: int = 3
    late_session_reclaim_reclaim_votes: int = 2
    late_session_reclaim_max_skew_seconds: float = 240.0
    late_session_reclaim_min_close_location: float = 0.55
    late_session_reclaim_min_volume_ratio: float = 1.10
    late_session_reclaim_volume_votes: int = 3
    late_session_reclaim_options_sl_pct: float = 5.0
    late_session_reclaim_futures_sl_pct: float = 0.05
    late_session_reclaim_rr: float = 1.5

    repaint_check_interval_minutes: int = 2 # (legacy/unused) periodic interval for repaint guard checking
    repaint_check_interval_seconds: float = 20.0 # Intra-candle repaint re-check cadence (5min & 15min) until candle close
    repaint_close_check_grace_seconds: float = 90.0 # Post-close window in which a repaint may still abort; after this the candle is confirmed and the trade is tracked normally
    repaint_recheck_seconds: float = 20.0 # Post-entry repaint re-check cadence until the source candle closes (same for 5min and 15min)
    # ── Opposite-flip confirm window (2026-07-10) ──
    # A flip must persist 30–60s (clamped) AND pass an intel/move sanity read
    # before it exits a live trade and hands the slot to the reverse entry.
    # Multi-index LTP shock force-exit bypasses this window by design.
    opposite_flip_confirm_min_seconds: float = 30.0
    opposite_flip_confirm_max_seconds: float = 60.0
    opposite_flip_intel_filter_enabled: bool = True
    # Hold confirm while the flip's own move has gone adverse by more than this
    # (%) — a flip that immediately mean-reverts is not a real reversal. At the
    # max window the hold expires: adverse ⇒ cancel timer, else confirm.
    opposite_flip_max_adverse_move_pct: float = 0.10
    # Strong opposing intelligence (aligned score <= -threshold) delays confirm
    # until the max window, then price action decides.
    opposite_flip_intel_block_threshold: float = 0.55
    live_signal_recheck_seconds: float = 5.0 # Re-grade a rejected forming arrow at a controlled cadence, plus once at candle close
    last_minute_extension_seconds: int = 60 # Check last minute signal entry extension
    live_restart_recovery_grace_seconds: float = 120.0 # Fallback restart-recovery window (non-5/15min TFs)
    live_restart_recovery_grace_seconds_5min: float = 180.0  # Restart recovery window for 5min last-candle signal
    live_restart_recovery_grace_seconds_15min: float = 360.0 # Restart recovery window for 15min last-candle signal
    live_signal_post_close_grace_seconds_5min: float = 300.0 # Let an already-running scanner finish slow 5min refreshes
    live_signal_post_close_grace_seconds_15min: float = 300.0 # Let an already-running scanner finish slow 15min refreshes
    live_choppy_gate_confidence: float = 0.90 # Choppy/ranging setups need exceptional confidence
    # ─── Live quality-gate confidence floors (promoted from getattr-defaults to real fields) ───
    live_15min_min_confidence: float = 0.62
    live_15min_trending_momentum_adx_threshold: float = 24.0
    live_15min_trending_momentum_intel_threshold: float = 0.60
    live_15min_trending_momentum_min_confidence: float = 0.58
    live_15min_momentum_override_enabled: bool = True
    live_15min_momentum_min_confidence: float = 0.20
    live_15min_momentum_volume_ratio: float = 2.0
    live_15min_momentum_range_atr_ratio: float = 1.25
    live_15min_momentum_close_location: float = 0.65
    live_15min_momentum_adx_threshold: float = 24.0
    live_15min_momentum_intel_threshold: float = 0.65
    fifteen_min_choppy_scalp_guard_enabled: bool = True
    fifteen_min_choppy_scalp_min_intel_score: float = 0.35
    fifteen_min_choppy_scalp_min_volume_ratio: float = 1.05
    fifteen_min_choppy_scalp_micro_max_skew_seconds: float = 240.0
    fifteen_min_choppy_scalp_min_close_location: float = 0.55
    fifteen_min_choppy_scalp_profit_lock_r_multiple: float = 0.20
    fifteen_min_choppy_scalp_min_peak_rs: float = 250.0
    live_5min_bplus_min_confidence: float = 0.70
    live_5min_choppy_min_confidence: float = 0.70
    live_5min_early_min_confidence: float = 0.70
    live_5min_pre_close_min_confidence: float = 0.70
    live_5min_special_adx_threshold: float = 30.0
    live_5min_special_bplus_min_confidence: float = 0.60
    live_5min_special_intel_threshold: float = 0.75
    live_5min_special_max_iv_percentile: float = 70.0
    live_5min_special_min_chain_score: float = 50.0
    reversal_entry_relief_enabled: bool = True
    reversal_entry_min_confidence: float = 0.20
    reversal_entry_thrust_min_range_atr_ratio: float = 1.20
    reversal_entry_thrust_min_close_location: float = 0.62
    reversal_entry_thrust_min_volume_ratio: float = 1.20
    reversal_entry_vwap_min_dev_pct: float = 0.03
    directional_census_reversal_relief_enabled: bool = True
    directional_census_reversal_min_confidence: float = 0.20
    late_session_choppy_entry_guard_enabled: bool = True
    late_session_choppy_min_runway_minutes: float = 30.0
    market_wide_reversal_enabled: bool = True
    market_wide_reversal_core_votes: int = 2
    market_wide_reversal_total_votes: int = 2
    market_wide_reversal_min_range_atr_ratio: float = 1.10
    market_wide_reversal_min_range_atr_ratio_5min: float = 1.10
    market_wide_reversal_min_range_atr_ratio_15min: float = 0.50
    market_wide_reversal_midcap_min_range_atr_ratio_5min: float = 0.65
    market_wide_reversal_midcap_min_range_atr_ratio_15min: float = 0.45
    market_wide_reversal_min_close_location: float = 0.65
    market_wide_reversal_max_skew_seconds_5min: float = 180.0
    market_wide_reversal_max_skew_seconds_15min: float = 900.0
    momentum_override_threshold: float = 25.0 # ADX threshold to bypass choppy market block
    max_trades_per_index: int = 5
    # Dynamic daily trade budget: scale max_trades_per_index by the current
    # intraday phase. A confirmed trend phase earns continuation re-entries;
    # a confirmed range/chop phase trims the budget whether it lasts all day,
    # an hour, or only the current 40-minute pocket.
    dynamic_trade_budget_enabled: bool = True
    dynamic_trade_budget_trend_bonus: int = 3
    dynamic_trade_budget_range_malus: int = 2
    dynamic_trade_budget_min_day_confidence: float = 0.60
    # Multi-entry engines (quad_prime/quad_union/adaptive_hybrid) legitimately accept more entries
    # per day in trend/normal phases. Never offsets the range-phase malus: raw
    # arrow density in chop is churn for every engine. Hard ceiling stays 20.
    dynamic_trade_budget_multi_engine_bonus: int = 4
    # Session trend-day guard: block weak 1min/5min counter-trend scalps while a
    # high-confidence trend day is in force (fast-thrust and reversal-relief
    # entries are exempt; guard disarms once price closes across session VWAP).
    session_context_trend_day_guard_enabled: bool = True
    session_context_trend_day_guard_min_confidence: float = 0.70
    # Census-contradiction yield: on 2026-07-08 the day-type label stayed
    # TREND_DAY_UP and vetoed 6 midcap SELLs (11:19-12:36) while the session's
    # own directional census was already 5L/10S bearish; midcap then fell 1.9%.
    # When the census opposes the label this strongly, the label is stale —
    # the guard yields instead of vetoing.
    session_trend_guard_census_yield_enabled: bool = True
    session_trend_guard_census_min_total: int = 6
    session_trend_guard_census_dominance: float = 1.5
    # Bidirectional late-session market LTP shock (dump AND rally).
    # Multi-index ≥0.20–0.35% move in ≤3–5 min + volume → short stab OPT path;
    # after session_override_start the sticky TREND_DAY_* label yields so the
    # regime panel is not stuck on the full-day open classification.
    market_ltp_shock_enabled: bool = True
    market_ltp_shock_min_move_pct: float = 0.20
    market_ltp_shock_strong_move_pct: float = 0.35
    market_ltp_shock_lookback_minutes: int = 5
    market_ltp_shock_min_lookback_minutes: int = 3
    market_ltp_shock_min_indices: int = 2
    market_ltp_shock_min_core_indices: int = 2
    market_ltp_shock_min_volume_ratio: float = 1.10
    # Shock engine full-sensitivity IST window (normal min_move applies inside).
    market_ltp_shock_session_start: str = "14:00"
    market_ltp_shock_session_end: str = "15:10"
    # Backward-compat alias used by older guards (same as session_start).
    market_ltp_shock_session_override_start: str = "14:00"
    # Outside the window: "strong" = only moves >= outside_strong_move_pct qualify
    # (genuine crash/melt-up bursts); "off" = engine idle outside the window.
    market_ltp_shock_outside_window_mode: str = "strong"
    market_ltp_shock_outside_strong_move_pct: float = 0.35
    market_ltp_shock_opposite_trade_relax: bool = True
    # When True and multi-index LTP shock is confirmed, immediately force-exit
    # open opposite positions (skip ~20s opposite-flip repaint wait) so reverse
    # OPT/FUT can enter the same scan cycle with opposite_trade_relax.
    market_ltp_shock_force_exit_opposite: bool = True
    market_ltp_shock_stabilization_seconds: float = 8.0
    market_ltp_shock_allow_1min: bool = True
    market_ltp_shock_allow_5min: bool = True
    # Rolling mood supersedes sticky full-day day_type for trend-phase guard.
    session_mood_dynamic_enabled: bool = True
    session_mood_min_confidence: float = 0.55
    # Previously referenced via getattr defaults only — declared so the operator
    # can actually toggle/tune them (audit 2026-07-03 found them undeclared).
    ut_market_location_gate: bool = True
    live_5min_special_min_confidence: float = 0.60
    ut_historical_verbose_audit_logs: bool = False
    # Angel REST exponential backoff after throttle/failure responses:
    # delay = min(max_seconds, seconds * 2^(failures-1)).
    angel_rest_backoff_seconds: float = 12.0
    angel_rest_backoff_max_seconds: float = 60.0
    # Kite REST throttle backoff, same ladder. Much shorter than the Angel figures above
    # because Kite's published limits are per-SECOND (historical 3/s, quote 1/s), so a
    # 12s floor would idle the data path for whole scan cycles to clear a sub-second
    # window. Applied per endpoint, and only on a recognised rate-limit refusal.
    kite_rest_backoff_seconds: float = 1.0
    kite_rest_backoff_max_seconds: float = 8.0
    # ── 2026-07-03 session-audit fixes ──
    # Bounded option-quote retry before dropping a live candidate (transient
    # chain hiccups cost 16 tradeable candidates that session).
    option_quote_retry_attempts: int = 2
    option_quote_retry_delay_seconds: float = 0.5
    # Fyers self-heal: re-create the client after a persistent option-chain
    # failure streak (the manual midday restart that cleared ~1600 chain errors
    # was effectively this).
    fyers_chain_self_heal_enabled: bool = True
    fyers_chain_reinit_failure_threshold: int = 8
    fyers_chain_reinit_cooldown_seconds: float = 300.0
    # Fyers WS silent-disconnect detection: reconnect after this many seconds of
    # no ticks while connected. Keep the live default at 90s: calm index sessions
    # can legitimately go quiet long enough that 45s causes false reconnect churn.
    # The detector remains settings-driven for emergency tightening.
    fyers_ws_silent_disconnect_seconds: float = 90.0
    # Divergence warnings: once per 5min bar instead of every intel cycle.
    divergence_warning_throttle_seconds: float = 300.0
    # ── 2026-07-03 lifecycle forensics fixes ──
    # Non-trend give-back scope: on 2026-07-03 four option trades peaked
    # +0.32R..+0.49R and round-tripped to full stops (~Rs.3,900) because normal
    # mid-day trades are excluded from the give-back guard and the Rs.500 floor
    # exceeds 0.40R at 1-lot size. When the session is NOT a confirmed trend day,
    # chop round-trips are the norm — every trade gets give-back protection at a
    # lower arm. Confirmed trend days keep full let-winners-run behavior.
    profit_giveback_nontrend_all_trades: bool = True
    profit_giveback_nontrend_arm_r_multiple: float = 0.30
    profit_giveback_nontrend_min_peak_rs: float = 300.0
    # Exhaustion honesty: ADX>40 entries carried "Risk of exhaustion" in their own
    # reasons yet still graded A+ 0.95 (both 10:25 losers). Cap confidence so an
    # exhaustion-flagged setup cannot wear the top label.
    ut_exhaustion_confidence_cap: float = 0.82
    # Pre-candle flip veto: the predictor projected the 15m flip that stopped the
    # 14:53 A+ short, but is wired to nothing. Default OFF — logs would-veto
    # telemetry so live sessions accumulate evidence before it gates anything.
    pre_candle_flip_veto_enabled: bool = False
    pre_candle_flip_veto_min_confidence: float = 0.70
    # Range-day regime-pillar damp: on 2026-07-03 the 5min regime detector read
    # TRENDING 54-60% of a day the session context correctly classified RANGE_DAY
    # all day; the grading regime pillar's +0.30 "alignment" bonus on those
    # phantom trends manufactured six A+ labels that went 0/6. When the session
    # is a confirmed range day, cap the regime pillar's trend-alignment bonus.
    range_day_regime_pillar_damp_enabled: bool = True
    range_day_regime_pillar_bonus_cap: float = 0.10
    range_day_regime_pillar_min_confidence: float = 0.60
    # Weak-market scalp mode: on a confirmed range day a non-burst entry arms
    # scalp-lock trailing from entry and caps the RR target — book small gains
    # that clear charges instead of holding chop toward runner targets. Bursts
    # (thrust / market-wide reversal / explosive) are exempt and run free.
    weak_market_scalp_mode_enabled: bool = True
    weak_market_scalp_min_day_confidence: float = 0.60
    weak_market_scalp_max_rr: float = 1.5
    # 3 halted an index for the rest of the day on a 3-loss run. Simulated over the
    # measured 59.8% win rate and v7's unconstrained arrival rate (3.61 trades/index/day,
    # 30 days x 4 indices): k=3 blocks ~13 trades and fires ~10 halts, k=4 blocks ~3 and
    # fires ~2.6, k=5 blocks ~1. Raising to 4 recovers ~10 trades (~Rs 7,800 at the
    # measured Rs 784/trade) and still halts an index that is genuinely broken, one trade
    # later. k=5 makes the protection close to vacuous.
    #
    # Per-index remains correct: the old global tape interleaved four indices so the halt
    # never fired at all. This tunes a working guard rather than reverting to a broken one.
    max_consecutive_losses: int = 4
    index_cooldown_minutes: float = 4.0
    # ── Regime-scoped cooldown (2026-07-10) ──
    # UT1 is a flip engine: after an opposite-signal exit the reverse entry IS
    # the trade. A blanket post-exit cooldown blocked those re-entries, so the
    # cooldown now applies ONLY when the index is in a chop/range/no-movement
    # pocket (revenge-churn protection); confirmed trend/normal tape skips it.
    index_cooldown_regime_scoped: bool = True
    # ── FUT position discipline (2026-07-10) ──
    # Futures are ALWAYS 1 lot and at most one concurrent FUT position per
    # index, regardless of user lot settings (options keep user sizing).
    fut_lots_hard_cap: int = 1
    fut_one_position_per_index: bool = True
    ut_option_history_mode: Literal["stored_or_synthetic", "fetch_or_synthetic", "fetch_or_skip"] = "fetch_or_synthetic"
    # REMOVED 2026-08-06: the eleven market_intel_pullback_* / market_intel_loss_* fields
    # that used to sit here had no consumer anywhere in the codebase — a whole guard that
    # was designed, configured (guard_enabled=True in .env) and never built. Keeping them
    # meant eleven knobs an operator could set and watch do nothing.
    #
    # The job they describe is done by the loss-guard family below and in scanner.py:
    # _profit_guard_reversal_context classifies the pullback (noise / small_pullback /
    # reversal / big_reversal / structure_break / premium_melt) from candle structure,
    # _loss_guard_severity scores it with loss_guard_rejection_wick_*, loss_guard_grind_*,
    # loss_guard_opt_premium_melt_* and the OI/PCR vote settings, and _loss_guard_decision
    # turns that severity into the R multiple at which the trade is cut. Those settings
    # are read and tested; these were neither.
    #
    # model_config uses extra='ignore', so the keys may stay in .env harmlessly.
    pcr_near_strikes: int = 5 # Strikes on either side of ATM for near-money PCR

    # ─── Logging ─────────────────────────────────────────────
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    activity_event_dedupe_seconds: float = 2.0 # Suppress identical dashboard-tape events emitted by overlapping callers
    log_path: str = "./logs"
    # rtracker = regime tracker. Shadow-only evidence store for regime conviction,
    # accepted/rejected signals, and weekly study reports. It never changes live
    # entries, exits, stops, or broker behavior.
    rtracker_enabled: bool = True
    rtracker_data_dir: str = "data_store/rtracker"
    rtracker_report_weeks: int = 8
    # Days of raw event day-files to keep on disk. 0 keeps everything, which is the
    # behaviour that let the archive reach 83 MB in 61 days. Any value below
    # rtracker_report_weeks * 7 is raised to it, so pruning can never delete evidence
    # the summary still reports on.
    rtracker_retention_days: int = 0

    model_config = SettingsConfigDict(
        env_file='.env',
        env_file_encoding='utf-8',
        case_sensitive=False,
        extra='ignore'
    )

    @field_validator('max_daily_loss_pct')
    @classmethod
    def validate_max_daily_loss(cls, v: float) -> float:
        if not 0 < v <= 10:
            raise ValueError('max_daily_loss_pct must be between 0 and 10')
        return v

    @field_validator('futures_sl_pct')
    @classmethod
    def validate_futures_stop(cls, v: float) -> float:
        if not 0 < v <= 5:
            raise ValueError('futures_sl_pct must be between 0 and 5')
        return v

    @field_validator('options_sl_pct')
    @classmethod
    def validate_options_stop(cls, v: float) -> float:
        if not 0 < v <= 50:
            raise ValueError('options_sl_pct must be between 0 and 50')
        return v

    @field_validator('max_trade_loss_abs')
    @classmethod
    def validate_max_trade_loss(cls, v: float) -> float:
        if v < 0:
            raise ValueError('max_trade_loss_abs must be >= 0 (0 disables the per-trade rupee backstop)')
        return v

    @field_validator('entry_margin_buffer_pct')
    @classmethod
    def validate_entry_margin_buffer(cls, v: float) -> float:
        if not 0 <= v <= 100:
            raise ValueError('entry_margin_buffer_pct must be between 0 and 100')
        return v

    @field_validator('entry_margin_cache_seconds')
    @classmethod
    def validate_entry_margin_cache(cls, v: float) -> float:
        if not 0 <= v <= 30:
            raise ValueError('entry_margin_cache_seconds must be between 0 and 30')
        return v

    def risk_configuration_summary(self) -> Dict[str, Any]:
        fut_budget = self.capital_fut * self.risk_fut_pct / 100.0
        opt_budget = self.capital_opt * self.risk_opt_pct / 100.0
        issues = []
        if self.capital_total <= 0 or self.capital_fut <= 0 or self.capital_opt <= 0:
            issues.append("capital values must be positive")
        if self.capital_fut + self.capital_opt > self.capital_total:
            issues.append("segment capital exceeds total capital")
        warnings = []
        # max_trade_loss_abs is advisory / circuit-breaker projection only when
        # auto risk sizing is OFF — it does not shrink lots. 0 = disabled.
        if self.auto_risk_sizing_enabled and self.max_trade_loss_abs > 0:
            if self.max_trade_loss_abs > fut_budget:
                warnings.append(
                    f"max_trade_loss_abs {self.max_trade_loss_abs:.0f} exceeds FUT daily budget {fut_budget:.0f}"
                )
            if self.max_trade_loss_abs > opt_budget:
                warnings.append(
                    f"max_trade_loss_abs {self.max_trade_loss_abs:.0f} exceeds OPT daily budget {opt_budget:.0f}"
                )
        return {
            "ok": not issues,
            "issues": issues,
            "warnings": warnings,
            "capital_total": self.capital_total,
            "capital_fut": self.capital_fut,
            "capital_opt": self.capital_opt,
            "futures_sl_pct": self.futures_sl_pct,
            "options_sl_pct": self.options_sl_pct,
            "max_trade_loss_abs": self.max_trade_loss_abs,
            "fut_daily_budget": fut_budget,
            "opt_daily_budget": opt_budget,
            "auto_risk_sizing_enabled": bool(self.auto_risk_sizing_enabled),
            "cascade_lot_scale_enabled": bool(self.cascade_lot_scale_enabled),
            "cascade_lot_multiplier": float(self.cascade_lot_multiplier),
            "cascade_lot_multiplier_fut": float(self.cascade_lot_multiplier_fut),
            "cascade_lot_multiplier_opt": float(self.cascade_lot_multiplier_opt),
            "cascade_lot_multiplier_opt_min": float(self.cascade_lot_multiplier_opt_min),
            "heavy_trend_lot_scale_enabled": bool(self.heavy_trend_lot_scale_enabled),
            "heavy_trend_lot_multiplier_opt": float(self.heavy_trend_lot_multiplier_opt),
            "heavy_trend_min_aligned_indices": int(self.heavy_trend_min_aligned_indices),
            "cascade_multi_tf_stack_enabled": bool(self.cascade_multi_tf_stack_enabled),
            "cascade_midcap_solo_max_tf": int(self.cascade_midcap_solo_max_tf),
            "cascade_midcap_dual_main_max": int(self.cascade_midcap_dual_main_max),
            "cascade_midcap_dual_addon_max": int(self.cascade_midcap_dual_addon_max),
            "cascade_core_bucket_max_positions": int(self.cascade_core_bucket_max_positions),
            "cascade_midcap_bucket_max_positions": int(self.cascade_midcap_bucket_max_positions),
            "cascade_max_concurrent_positions": int(self.cascade_max_concurrent_positions),
            "dual_clone_reduction_enabled": bool(self.dual_clone_reduction_enabled),
            "risk_per_trade_daily_fraction": self.risk_per_trade_daily_fraction,
            "sizing_mode": "fixed_lots" if not self.auto_risk_sizing_enabled else "risk_budget",
        }

    def effective_trade_loss_cap(self, inst_type: str) -> float:
        """Never let one trade consume more than its segment's full daily budget.

        max_trade_loss_abs == 0 disables the extra rupee backstop: the cap is
        then the segment's own daily budget (the user's visible settings)."""
        segment = str(inst_type or "FUT").upper()
        segment_budget = max(0.0, float(
            self.capital_opt * self.risk_opt_pct / 100.0
            if segment == "OPT"
            else self.capital_fut * self.risk_fut_pct / 100.0
        ))
        abs_cap = float(self.max_trade_loss_abs or 0.0)
        if abs_cap <= 0:
            return segment_budget
        return min(abs_cap, segment_budget)

    def per_trade_risk_budget(self, inst_type: str, quality_multiplier: float = 1.0) -> float:
        """Per-trade sizing budget (₹). Daily % knobs stay circuit-breaker only."""
        from engine.position_sizing import per_trade_risk_budget as _budget

        return _budget(self, inst_type, quality_multiplier=quality_multiplier)


    def get_signal_fire_mode(self) -> str:
        """Return the canonical scanner entry-timing mode.

        Both legacy settings remain readable so existing .env files that used
        either switch continue to work. New UI writes synchronize both values.
        """
        intrabar_enabled = _setting_enabled(
            getattr(self, "live_intrabar_signal_entries", True),
            True,
        )
        legacy_mode = str(getattr(self, "ut_signal_mode", "realtime") or "realtime").lower()
        return "INTRACANDLE" if intrabar_enabled and legacy_mode != "confirmed" else "CANDLE_CLOSE"

    def set_signal_fire_mode(self, mode: str) -> str:
        """Synchronize the canonical mode with both legacy engine settings."""
        normalized = str(mode or "INTRACANDLE").strip().upper().replace("-", "_")
        aliases = {
            "REALTIME": "INTRACANDLE",
            "INTRA_CANDLE": "INTRACANDLE",
            "LIVE": "INTRACANDLE",
            "CONFIRMED": "CANDLE_CLOSE",
            "CLOSED_CANDLE": "CANDLE_CLOSE",
            "CLOSE": "CANDLE_CLOSE",
        }
        normalized = aliases.get(normalized, normalized)
        if normalized not in {"INTRACANDLE", "CANDLE_CLOSE"}:
            raise ValueError(f"Unsupported signal fire mode: {mode}")
        intrabar_enabled = normalized == "INTRACANDLE"
        self.live_intrabar_signal_entries = intrabar_enabled
        self.ut_signal_mode = "realtime" if intrabar_enabled else "confirmed"
        return normalized


    def get_ut_engine_params(self) -> dict:
        preset = getattr(self, "ut_preset", "AGGRESSIVE").upper()
        kv = 1.0
        atr = 10
        sig_mode = "realtime" if self.get_signal_fire_mode() == "INTRACANDLE" else "confirmed"
        if preset == "BALANCED":
            kv = 1.3
            atr = 10
        elif preset == "CONSERVATIVE":
            kv = 1.6
            atr = 14
            
        return {
            "key_value": kv,
            "atr_period": atr,
            "regime_adaptation": _setting_enabled(getattr(self, "ut_regime_adaptation", True), True),
            "use_heikin_ashi": self.ut_use_heikin_ashi,
            "signal_mode": sig_mode,
            "adx_filter": self.ut_adx_filter,
            "adx_period": self.ut_adx_period,
            "adx_threshold": self.ut_adx_threshold,
            "strict_adx": self.ut_strict_adx,
            "session_filter": self.ut_session_filter,
            "session_start": self.ut_session_start,
            "session_end": self.ut_session_end
        }
        

    def save_to_env(self):
        """Save current settings to .env file"""
        env_file = Path('.env')
        if not env_file.exists():
            env_file.touch()

        existing_values = dotenv.dotenv_values(env_file)
            
        # Serialize fields using pydantic's model_dump
        data = self.model_dump()
        # Never write back a setting nothing reads. This method dumps EVERY field, so
        # each save resurrected all 32 inert keys as live .env lines — undoing the
        # commenting that documents them as dead and making the preflight shout about
        # them again on the next start. Observed on 2026-08-08: a running scanner
        # persisted settings and every one came back.
        #
        # Their commented lines survive untouched, so the record of what was once
        # configured, and why it does nothing, stays in the file.
        try:
            from config.inert_settings import INERT as _INERT
        except Exception:
            _INERT = {}

        # A value that still equals its dataclass default is NOT a configuration
        # choice, and writing it makes it one. .env wins over the class default
        # (model_config env_file), so the moment a field is persisted at its default it
        # is pinned there for good: changing the default in code afterwards does nothing,
        # because the file shadows it. Measured 2026-08-11 — 681 of 741 keys in .env sat
        # at their default, i.e. 92% of the file was pinning nobody had asked for, and a
        # setting added in code was frozen at its first-ever default the moment the
        # running scanner next persisted (surge_tick_proxy_enabled did exactly that).
        #
        # Keys ALREADY in .env are still refreshed, so nothing an operator has set — or
        # that a previous build pinned — silently changes underneath them. This only
        # stops NEW defaults being written, which is what makes a default upgradeable.
        model_fields = getattr(type(self), "model_fields", {}) or {}

        def _is_untouched_default(field_name: str, value) -> bool:
            field = model_fields.get(field_name)
            if field is None:
                return False
            try:
                default = field.get_default(call_default_factory=True)
            except TypeError:          # older pydantic signature
                try:
                    default = field.get_default()
                except Exception:
                    return False
            except Exception:
                return False
            try:
                return bool(value == default)
            except Exception:
                return False

        for key, val in data.items():
            if key in _INERT:
                continue
            env_key = key.upper()
            if env_key not in existing_values and _is_untouched_default(key, val):
                continue
            # Handle list/dict serialization (e.g. active_indices)
            if isinstance(val, (list, dict)):
                str_val = json.dumps(val)
            else:
                str_val = str(val)
            if env_key in PROTECTED_SECRET_ENV_KEYS and not _clean_env_secret(str_val):
                existing_secret = existing_values.get(env_key)
                if _clean_env_secret(existing_secret):
                    str_val = str(existing_secret)
            _set_env_key_with_retry(env_key, str_val)

    def get_instruments(self) -> Dict[str, Any]:
        """Load instrument configuration from instruments.json"""
        config_path = Path(__file__).parent / "instruments.json"
        with open(config_path, 'r') as f:
            return json.load(f)

    def ensure_directories(self):
        """Create required directories"""
        dirs = [self.log_path, "data_store", "data_store/candles"]
        for d in dirs:
            Path(d).mkdir(parents=True, exist_ok=True)


# Singleton instance
_settings = None


def get_settings() -> Settings:
    """Get or create global settings instance"""
    global _settings
    if _settings is None:
        _settings = Settings()
        _settings.ensure_directories()
    return _settings
