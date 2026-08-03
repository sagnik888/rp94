"""Production safety checks for REAL mode startup and switching."""

from __future__ import annotations

import json
from datetime import datetime, time as dtime
from pathlib import Path
from typing import Any, Dict, List

import pytz


IST = pytz.timezone("Asia/Kolkata")


def fyers_token_freshness_warning(now: datetime, token_path: Path | str = "fyers_token.json") -> str:
    """Pre-market checklist alarm: Fyers tokens are daily (no auto-refresh by
    broker policy), so a token not minted TODAY means degraded option-chain/WS
    feeds all session. Returns a warning string, or "" when fresh."""
    path = Path(token_path)
    if not path.exists():
        return "fyers token file missing — Fyers option-chain/WS feeds will be unavailable"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        stamp = str(payload.get("timestamp") or "")
        token_dt = datetime.fromisoformat(stamp)
    except Exception:
        return "fyers token timestamp unreadable — re-auth Fyers before the session"
    if token_dt.tzinfo is not None:
        token_dt = token_dt.astimezone(IST).replace(tzinfo=None)
    session_date = (now.astimezone(IST) if now.tzinfo else IST.localize(now)).date()
    if token_dt.date() < session_date:
        return (
            f"fyers token is from {token_dt.date().isoformat()} (not refreshed today) — "
            "re-auth Fyers before the session or chains/WS degrade"
        )
    return ""


def _parse_time_setting(value: Any, default: str) -> dtime:
    text = str(value or default).strip()
    for fmt in ("%H:%M:%S", "%H:%M"):
        try:
            return datetime.strptime(text, fmt).time()
        except ValueError:
            continue
    return datetime.strptime(default, "%H:%M:%S" if len(default.split(":")) == 3 else "%H:%M").time()


def _format_time(value: dtime) -> str:
    return value.strftime("%H:%M:%S" if value.second else "%H:%M")


from data.kite_auth import most_recent_token_cutoff  # noqa: E402


def validate_real_mode_readiness(
    settings: Any,
    data_provider: Any = None,
    now: datetime | None = None,
    *,
    require_option_chains: bool = False,
) -> Dict[str, Any]:
    """Return a structured readiness decision before REAL mode is allowed."""
    now = now or datetime.now(IST)
    if now.tzinfo is None:
        now = IST.localize(now)
    else:
        now = now.astimezone(IST)

    blockers: List[str] = []
    warnings: List[str] = []

    env_path = Path(".env")
    if not env_path.exists():
        blockers.append(".env missing")

    # Execution is Zerodha Kite. This gate still demanded AngelOne credentials, which
    # no longer place a single order — so with those left in .env it reported REAL mode
    # READY while never once checking the Kite session. The Kite access token expires
    # EVERY day, so "is the daily login done" is the one thing a pre-market gate has to
    # answer, and it was the one thing it did not ask.
    for key in ("kite_api_key", "kite_api_secret"):
        if not str(getattr(settings, key, "") or "").strip():
            blockers.append(f"{key} missing")

    try:
        from data.kite_auth import get_kite_auth

        kite_status = get_kite_auth(settings).status()
    except Exception as exc:
        blockers.append(f"Kite session state unavailable: {exc}")
        kite_status = {}
    if kite_status:
        if kite_status.get("auth_required") or not kite_status.get("connected"):
            blockers.append(
                "Kite daily login required"
                + (f" ({kite_status.get('reason')})" if kite_status.get("reason") else "")
                + " - complete it at /api/kite_login"
            )
        else:
            stamped = str(kite_status.get("token_updated_at") or "")
            issued = None
            if stamped:
                try:
                    issued = datetime.fromisoformat(stamped)
                except ValueError:
                    issued = None
            if issued is not None:
                if issued.tzinfo is None:
                    issued = IST.localize(issued)
                if issued < most_recent_token_cutoff(now):
                    blockers.append(
                        f"Kite token is stale (issued {issued:%Y-%m-%d %H:%M}); "
                        "re-login before the session"
                    )

    risk_summary_fn = getattr(settings, "risk_configuration_summary", None)
    if callable(risk_summary_fn):
        try:
            risk_summary = risk_summary_fn()
            if not risk_summary.get("ok", True):
                blockers.extend(
                    f"risk configuration: {issue}"
                    for issue in (risk_summary.get("issues") or ["invalid"])
                )
            warnings.extend(
                f"risk configuration: {warning}"
                for warning in (risk_summary.get("warnings") or [])
            )
        except Exception as exc:
            warnings.append(f"risk configuration check unavailable: {exc}")

    if data_provider is not None:
        if not bool(getattr(data_provider, "is_connected", False)):
            blockers.append("AngelOne broker not connected")
        try:
            health = data_provider.get_source_health()
            if health.get("all_brokers_unavailable"):
                blockers.append("all broker sources unavailable")
            if health.get("broker_degraded"):
                warnings.append("broker source degraded")
        except Exception as exc:
            warnings.append(f"source health unavailable: {exc}")

        try:
            if data_provider.is_market_holiday(now):
                blockers.append("market holiday")
        except Exception as exc:
            warnings.append(f"holiday calendar check unavailable: {exc}")

    # Free cash vs what one lot actually costs. An account that cannot fund a single
    # lot will have EVERY entry refused by RMS, which surfaces only as a generic
    # rejection per signal. Warn rather than block: margin moves intraday and a stale
    # reading must not veto a session the operator can still fund.
    try:
        from data.kite_auth import get_kite_auth as _auth_for_funds
        from trading.kite_broker import KiteBroker as _KB

        _probe = _KB(kite=_auth_for_funds(settings).kite)
        _cash = _probe.available_funds()
        _floor = float(getattr(settings, "entry_funds_warning_floor", 50000.0) or 0.0)
        if _cash is not None and _floor > 0 and _cash < _floor:
            warnings.append(
                f"broker free cash is {_cash:,.2f}, below the {_floor:,.0f} floor for one "
                "index lot - RMS will refuse entries until the account is funded"
            )
    except Exception:
        pass

    token_warning = fyers_token_freshness_warning(now)
    if token_warning:
        if require_option_chains:
            blockers.append(token_warning)
        else:
            warnings.append(token_warning)

    if require_option_chains:
        if data_provider is None:
            blockers.append("option-chain readiness unavailable")
        else:
            active_indices = [
                str(item or "").strip().upper()
                for item in (getattr(settings, "active_indices", []) or [])
                if str(item or "").strip()
            ]
            min_strikes = max(
                3,
                int(getattr(settings, "auto_option_chain_min_strikes", 3) or 3),
            )
            max_age = max(
                10.0,
                float(
                    getattr(settings, "intelligence_cache_ttl_seconds", 90.0)
                    or 90.0
                ),
            )
            meta_by_index = getattr(data_provider, "_option_chain_meta", {}) or {}
            cache_by_index = getattr(data_provider, "_opt_chain_cache", {}) or {}
            now_epoch = now.timestamp()
            for instrument in active_indices:
                meta = meta_by_index.get(instrument, {})
                chain = cache_by_index.get(f"opt_chain_{instrument}")
                try:
                    count = int(len(chain)) if chain is not None else 0
                except Exception:
                    count = 0
                fetched_at = float(meta.get("fetched_at") or 0.0) if isinstance(meta, dict) else 0.0
                age = now_epoch - fetched_at if fetched_at > 0 else float("inf")
                reasons = []
                if count < min_strikes:
                    reasons.append(f"{count} strikes (need {min_strikes})")
                if not isinstance(meta, dict) or bool(meta.get("fallback", True)):
                    reasons.append("fallback chain")
                if not bool((meta or {}).get("expiry_aligned", False)):
                    reasons.append("active expiry not verified")
                if not str((meta or {}).get("active_expiry") or "").strip():
                    reasons.append("active expiry missing")
                if not (0.0 <= age <= max_age):
                    reasons.append("chain stale or missing")
                if reasons:
                    blockers.append(
                        f"{instrument} option chain not Auto-ready: "
                        + ", ".join(reasons)
                    )

    special_session = False
    if data_provider:
        try:
            special_session = bool(data_provider.is_special_trading_session(now))
        except Exception:
            special_session = False

    if now.weekday() >= 5 and not special_session:
        blockers.append("weekend session")

    arm_start = _parse_time_setting(
        getattr(settings, "real_mode_premarket_arm_start", "09:00:00"),
        "09:00:00",
    )
    premarket_settle_end = _parse_time_setting(
        getattr(settings, "real_mode_premarket_settle_end", "09:08:00"),
        "09:08:00",
    )
    premarket_refresh_end = _parse_time_setting(
        getattr(settings, "real_mode_premarket_refresh_end", "09:14:30"),
        "09:14:30",
    )
    live_start = _parse_time_setting(
        getattr(settings, "real_mode_live_start", "09:15:00"),
        "09:15:00",
    )
    live_end = _parse_time_setting(
        getattr(settings, "real_mode_live_end", "15:30:00"),
        "15:30:00",
    )
    current_time = now.time()
    if not (arm_start <= current_time <= live_end):
        blockers.append(
            f"outside REAL arming window ({_format_time(arm_start)}-{_format_time(live_end)} IST)"
        )
    elif arm_start <= current_time <= premarket_settle_end:
        warnings.append(
            "pre-open settlement window: REAL armed while indicative prices settle; trade execution remains market/session gated"
        )
    elif premarket_settle_end < current_time <= premarket_refresh_end:
        warnings.append(
            "pre-open analysis window: data refresh and daily market context warmup allowed; trade execution remains market/session gated"
        )
    elif premarket_refresh_end < current_time < live_start:
        warnings.append(
            "market-open handoff window: REAL is armed; trade execution remains market/session gated"
        )

    return {
        "ok": not blockers,
        "blockers": blockers,
        "warnings": warnings,
        "checked_at": now.isoformat(),
    }
