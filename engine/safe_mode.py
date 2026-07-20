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


def validate_real_mode_readiness(settings: Any, data_provider: Any = None, now: datetime | None = None) -> Dict[str, Any]:
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

    required = {
        "angelone_api_key": getattr(settings, "angelone_api_key", ""),
        "angelone_client_id": getattr(settings, "angelone_client_id", ""),
        "angelone_password": getattr(settings, "angelone_password", ""),
        "angelone_totp_secret": getattr(settings, "angelone_totp_secret", ""),
    }
    for key, value in required.items():
        if not str(value or "").strip():
            blockers.append(f"{key} missing")

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

    token_warning = fyers_token_freshness_warning(now)
    if token_warning:
        warnings.append(token_warning)

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
