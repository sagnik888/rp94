"""Pure broker-source health and latency snapshot calculations."""

from __future__ import annotations

import time
from datetime import datetime
from typing import Any, Dict, Mapping, Optional


def build_source_health(
    provider: Any,
    now_ts: Optional[float] = None,
    freshness_threshold: float = 45.0,
) -> Dict[str, Any]:
    now = time.time() if now_ts is None else float(now_ts)
    is_live = provider.is_market_open()
    angel_tick = float(getattr(provider, "_last_angel_ws_tick", 0.0) or 0.0)
    fyers_tick = float(getattr(provider, "_last_fyers_ws_tick", 0.0) or 0.0)
    angel_age = None if angel_tick <= 0 else max(0.0, now - angel_tick)
    fyers_age = None if fyers_tick <= 0 else max(0.0, now - fyers_tick)

    angel_fresh = angel_age is not None and angel_age < freshness_threshold
    fyers_fresh = fyers_age is not None and fyers_age < freshness_threshold
    angel_health = bool(provider.is_connected and (angel_fresh if is_live else True))
    fyers_available = provider.fyers is not None and not provider._fyers_auth_required
    fyers_health = bool(fyers_available and (fyers_fresh if is_live else True))
    all_brokers_unavailable = bool(is_live and not angel_health and not fyers_health)
    yahoo_mode = (
        "emergency_used"
        if all_brokers_unavailable and provider._yahoo_request_count
        else "idle_standby"
    )

    return {
        "angel": angel_health,
        "angel_tick_age_seconds": round(angel_age, 2) if angel_age is not None else None,
        "fyers": fyers_health,
        "fyers_tick_age_seconds": round(fyers_age, 2) if fyers_age is not None else None,
        "broker_degraded": bool(is_live and not (angel_health and fyers_health)),
        "all_brokers_unavailable": all_brokers_unavailable,
        "fyers_auth_required": provider._fyers_auth_required,
        "fyers_auth_reason": provider._fyers_auth_reason,
        "fyers_refresh_disabled": provider._fyers_refresh_disabled,
        "yahoo": True,
        "yahoo_mode": yahoo_mode,
        "yahoo_request_count": int(provider._yahoo_request_count),
        "yahoo_last_used_at": (
            datetime.fromtimestamp(provider._yahoo_last_used_at).isoformat()
            if provider._yahoo_last_used_at
            else None
        ),
    }


def _age_seconds(value: Any, now_ts: float) -> Optional[float]:
    try:
        timestamp = value.get("time") if isinstance(value, dict) else None
        if isinstance(timestamp, datetime):
            return max(0.0, (datetime.now() - timestamp).total_seconds())
        if timestamp:
            return max(0.0, now_ts - float(timestamp))
    except Exception:
        return None
    return None


def _source_record(
    name: str,
    value: Any,
    latency_class: str,
    entry_eligible: bool,
    now_ts: float,
) -> Dict[str, Any]:
    age = _age_seconds(value, now_ts)
    has_order_authority = str(name or "").startswith("angel")
    return {
        "source": name,
        "latency_class": latency_class,
        "age_seconds": round(age, 2) if age is not None else None,
        "order_authority": bool(has_order_authority),
        "entry_eligible": bool(entry_eligible and has_order_authority),
        "ltp": float(value.get("ltp", 0.0)) if isinstance(value, dict) else 0.0,
    }


def build_market_source_snapshot(
    provider: Any,
    symbol: str,
    index_spot_tokens: Mapping[str, Any],
    fyers_index_symbols: Mapping[str, str],
    now_ts: Optional[float] = None,
) -> Dict[str, Any]:
    now = time.time() if now_ts is None else float(now_ts)
    records = []
    token_info = index_spot_tokens.get(symbol)
    if token_info:
        token = token_info[0]
        short_token = token[3:] if token.startswith("999") else token
        for key in (token, short_token):
            cached = provider._ws_cache.get(key)
            if isinstance(cached, dict):
                source = str(cached.get("source") or "angel_ws")
                age = _age_seconds(cached, now)
                records.append(
                    _source_record(
                        source,
                        cached,
                        "REALTIME",
                        age is not None and age <= 6.0,
                        now,
                    )
                )

    fyers_symbol = fyers_index_symbols.get(symbol)
    if fyers_symbol and isinstance(provider._ws_cache.get(fyers_symbol), dict):
        cached = provider._ws_cache[fyers_symbol]
        records.append(_source_record("fyers_ws", cached, "REALTIME", False, now))

    cached_ltp = provider._ltp_cache.get(symbol)
    if isinstance(cached_ltp, dict):
        source = str(cached_ltp.get("source") or "unknown")
        if source == "yahoo":
            records.append(_source_record("yahoo", cached_ltp, "DELAYED", False, now))
        elif source in {"angel", "fyers"}:
            age = _age_seconds(cached_ltp, now)
            records.append(
                _source_record(
                    f"{source}_rest",
                    cached_ltp,
                    "NEAR_REALTIME",
                    age is not None and age <= 15.0,
                    now,
                )
            )

    if not any(record["source"] == "yahoo" for record in records):
        records.append(
            {
                "source": "yahoo",
                "latency_class": "DELAYED",
                "age_seconds": None,
                "order_authority": False,
                "entry_eligible": False,
                "ltp": 0.0,
            }
        )

    entry_sources = [record for record in records if record.get("entry_eligible")]
    best = (
        min(
            records,
            key=lambda record: (
                0 if record.get("entry_eligible") else 1,
                999999.0
                if record.get("age_seconds") is None
                else float(record.get("age_seconds") or 0.0),
            ),
        )
        if records
        else {}
    )
    return {
        "instrument": symbol,
        "sources": records,
        "freshest": best,
        "entry_eligible": bool(entry_sources),
        "context_only_sources": [
            record for record in records if not record.get("entry_eligible")
        ],
    }
