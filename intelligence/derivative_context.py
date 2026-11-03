"""Corroborate existing price conviction with comparable derivative participation.

No standalone directional volume/OI vote, and no entry or exit trigger lives here.
"""
import math
from datetime import datetime, timezone, timedelta


SECONDS = {"1min": 60, "5min": 300, "15min": 900}


def _number(value):
    try:
        parsed = float(value)
        return parsed if math.isfinite(parsed) else None
    except (ValueError, TypeError, OverflowError):
        return None


def _expiry(value):
    text = str(value or "").upper().split(" ")[0]
    for fmt in ("%Y-%m-%d", "%d%b%Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            pass
    return None


def derivative_corroboration(payload, instrument, timeframe, as_of, conviction,
                             *, spot_price=0.0, option_expiry=""):
    result = {"available": False, "multiplier": 1.0, "timeframe": timeframe,
              "instrument": instrument, "families": {}, "reason": "No comparable derivative evidence"}
    seconds = SECONDS.get(timeframe)
    if (not seconds or not isinstance(payload, dict) or payload.get("instrument") != instrument
            or payload.get("timeframe") != timeframe or _number(as_of) is None):
        return result
    query_at = _number(payload.get("as_of"))
    if query_at is None or abs(query_at-float(as_of)) > 1:
        return result
    sign = 1 if conviction > 0 else -1 if conviction < 0 else 0
    max_gap = min(120.0, seconds / 2)
    session_day = datetime.fromtimestamp(float(as_of), timezone(timedelta(hours=5, minutes=30))).date()

    def valid(row, family):
        if not isinstance(row, dict) or not (row.get("available") is True and row.get("entry_eligible") is True):
            return False
        if (row.get("instrument") != instrument or row.get("family") != family
                or row.get("timeframe") != timeframe or row.get("clock") != "exchange"
                or not row.get("provider") or not row.get("contract") or not row.get("unit")):
            return False
        end, start, span = (_number(row.get(k)) for k in ("end_at", "start_at", "coverage_seconds"))
        gap = _number(row.get("max_observation_gap_seconds"))
        ratio, volume, delta, price = (_number(row.get(k)) for k in
                                      ("relative_volume_ratio", "volume_change", "price_change", "price"))
        expiry = _expiry(row.get("expiry"))
        return bool(end is not None and start is not None and span is not None and gap is not None
                    and 0 <= float(as_of)-end <= max_gap and seconds <= span <= seconds+max_gap
                    and abs(end-start-span) <= 1 and 0 <= gap <= max_gap
                    and ratio is not None and ratio >= 0 and volume is not None and volume >= 0
                    and delta is not None and price is not None and price > 0 and price-delta > 0
                    and expiry is not None and expiry >= session_day)

    def evidence(row, direction):
        ratio = float(row["relative_volume_ratio"])
        # 1x is ordinary participation; zero is a real inactive interval, not missing.
        activity = max(-1.0, min(1.0, ratio-1.0))
        if activity <= 0:
            return activity
        return activity if direction == sign else -activity

    futures = [r for r in payload.get("futures", []) if valid(r, "futures")]
    if futures:
        nearest = min(_expiry(r["expiry"]) for r in futures)
        futures = [r for r in futures if _expiry(r["expiry"]) == nearest]
        # Two feeds for one contract are alternatives, never independent votes.
        identities = {(r["provider"], r["contract"], r["unit"]) for r in futures}
        if len(identities) == 1:
            row = max(futures, key=lambda r: r["end_at"])
            delta = float(row["price_change"])
            direction = 1 if delta > 0 else -1 if delta < 0 else 0
            if direction or float(row["relative_volume_ratio"]) < 1:
                result["families"]["futures"] = {"corroboration": evidence(row, direction),
                    "contract": row["contract"], "provider": row["provider"], "expiry": row["expiry"],
                    "relative_volume_ratio": row["relative_volume_ratio"],
                    "oi_change": row.get("oi_change"), "price_change": delta,
                    "oi_interpretation": "position_change_only"}

    groups = {}
    wanted_expiry = _expiry(option_expiry)
    for row in payload.get("options", []):
        if not valid(row, "options") or row.get("option_type") not in {"CE", "PE"}:
            continue
        strike = _number(row.get("strike"))
        if strike is None or strike <= 0 or (wanted_expiry and _expiry(row["expiry"]) != wanted_expiry):
            continue
        key = (row["provider"], _expiry(row["expiry"]), strike, row["unit"])
        groups.setdefault(key, {}).setdefault(row["option_type"], []).append(row)
    pairs = []
    for key, legs in groups.items():
        if len(legs.get("CE", [])) != 1 or len(legs.get("PE", [])) != 1:
            continue
        ce, pe = legs["CE"][0], legs["PE"][0]
        if any(abs(float(ce[k])-float(pe[k])) > min(15, seconds/4) for k in ("start_at", "end_at")):
            continue
        ce_move, pe_move = float(ce["price_change"]), float(pe["price_change"])
        direction = 1 if ce_move > 0 and pe_move < 0 else -1 if ce_move < 0 and pe_move > 0 else 0
        if direction:
            pairs.append((key, ce, pe, direction))
    if pairs and spot_price > 0:
        # Use one near-money pair, not a varying strike-universe sum. A provider
        # ambiguity at that strike abstains rather than cherry-picking its vote.
        nearest = min((key[1], abs(key[2]-spot_price)) for key, *_ in pairs)
        selected = [p for p in pairs if (p[0][1], abs(p[0][2]-spot_price)) == nearest]
        if len(selected) == 1:
            key, ce, pe, direction = selected[0]
            ratio = min(float(ce["relative_volume_ratio"]), float(pe["relative_volume_ratio"]))
            strength = evidence({"relative_volume_ratio": ratio}, direction)
            result["families"]["options"] = {"corroboration": strength, "provider": key[0],
                "expiry": str(key[1]), "strike": key[2], "contracts": [ce["contract"], pe["contract"]],
                "relative_volume_ratio": ratio, "direction_basis": "matched_opposite_premium_moves",
                "call_oi_change": ce.get("oi_change"), "put_oi_change": pe.get("oi_change"),
                "oi_interpretation": "position_change_only"}
    if result["families"]:
        values = [f["corroboration"] for f in result["families"].values()]
        result.update(available=True, reason="", multiplier=1.0 + 0.10*sum(values)/len(values) if sign else 1.0)
    return result
