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
    fyers_tick = float(getattr(provider, "_last_fyers_ws_tick", 0.0) or 0.0)
    fyers_age = None if fyers_tick <= 0 else max(0.0, now - fyers_tick)
    fyers_fresh = fyers_age is not None and fyers_age < freshness_threshold

    # Kite health is session-based FIRST: a valid access token is what determines
    # whether orders work, and the stream being quiet must never read as "broker
    # down". Tick age is reported alongside so a stalled socket is still visible.
    kite_tick = float(getattr(provider, "_last_kite_ws_tick", 0.0) or 0.0)
    kite_age = None if kite_tick <= 0 else max(0.0, now - kite_tick)
    kite_health = bool(provider.is_connected)
    fyers_available = provider.fyers is not None and not provider._fyers_auth_required
    fyers_health = bool(fyers_available and (fyers_fresh if is_live else True))
    all_brokers_unavailable = bool(is_live and not kite_health and not fyers_health)
    yahoo_mode = (
        "emergency_used"
        if all_brokers_unavailable and provider._yahoo_request_count
        else "idle_standby"
    )

    return {
        "kite": kite_health,
        "kite_tick_age_seconds": round(kite_age, 2) if kite_age is not None else None,
        "fyers": fyers_health,
        "fyers_tick_age_seconds": round(fyers_age, 2) if fyers_age is not None else None,
        "broker_degraded": bool(is_live and not (kite_health and fyers_health)),
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
            return max(0.0, time.time() - timestamp.timestamp())
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
    # Order authority means "this feed comes from the broker we actually trade
    # through", which is Kite now. Leaving it matching "angel" marked every source as
    # non-authoritative once AngelOne was retired.
    has_order_authority = str(name or "").startswith("kite")
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
    ws_keys = []
    token_info = index_spot_tokens.get(symbol)
    if token_info:
        token = token_info[0]
        short_token = token[3:] if token.startswith("999") else token
        ws_keys.extend([token, short_token])

    # index_spot_tokens holds the AngelOne 999-prefixed constants, but the Kite ticker
    # caches under Kite's own instrument_token — as _load_kite_index_tokens puts it, "the
    # AngelOne token constants (26000, 26009...) mean nothing to Kite". That was fixed for
    # index candles and missed here, so this lookup never produced a kite_ws record. Since
    # order authority requires a source named kite* (fyers_ws is passed entry_eligible=
    # False and yahoo is DELAYED), the only realtime feed that can authorise an entry was
    # invisible: entry_eligible could come from nothing but the slower REST path, and
    # _filter_live_candidates_by_source dropped EVERY live entry whenever that path was
    # older than 15s.
    ws_keys.extend(
        str(tok)
        for tok, name in (getattr(provider, "_kite_index_names", None) or {}).items()
        if name == symbol
    )

    seen_keys = set()
    for key in ws_keys:
        if not key or key in seen_keys:
            continue
        seen_keys.add(key)
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
        elif source in {"kite", "fyers"}:
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
