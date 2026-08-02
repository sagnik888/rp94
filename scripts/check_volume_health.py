"""Which feed is actually supplying each index's volume, right now.

    venv/Scripts/python.exe scripts/check_volume_health.py

Answers the question the last outage could not answer: index volume was zero for days
and nothing said so. Every volume gate in this system is a ratio against recent average
— the 2.5x surge that is the ONLY way a 1min signal enters, the 1.6x/1.8x hybrid
thrusts, the 1.40x cascade, and the fast-opposite thrust EXIT guard — so one dead feed
silently disables entries and exits together.

Kite publishes no volume for an index (an index is a computed number, not a traded
instrument), so Fyers cash volume is primary and near-month FUTURES volume + OI is the
fallback. The fallback keeps trading alive, which is exactly why it also hides the
outage. This prints which one is in play and, when it is the fallback, says whether
Fyers is the thing that went quiet.

Hits the live APIs. Places no orders and changes no state.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

OK, WARN, BAD = "  [OK]  ", "  [WARN]", "  [FAIL]"


def main() -> int:
    from config.settings import get_settings
    from data.market_data import MarketDataProvider

    settings = get_settings()
    provider = MarketDataProvider(settings)
    for starter in ("connect", "start", "initialize"):
        fn = getattr(provider, starter, None)
        if callable(fn):
            try:
                fn()
            except Exception as exc:
                print(f"{WARN} provider.{starter}() raised: {exc}")
            break

    names = list(getattr(settings, "active_indices", None) or
                 ["NIFTY", "BANKNIFTY", "SENSEX", "MIDCPNIFTY"])
    print(f"\nIndex volume provenance — {len(names)} instrument(s)\n" + "=" * 62)

    degraded, dead = [], []
    for name in names:
        try:
            activity = provider.get_index_activity(name) or {}
        except Exception as exc:
            print(f"{BAD} {name}: get_index_activity raised: {exc}")
            dead.append(name)
            continue

        source = str(activity.get("source") or "none")
        futures = activity.get("futures") or {}
        age = activity.get("fyers_volume_age_seconds")
        age_text = f"{age:.0f}s ago" if isinstance(age, (int, float)) else "never seen"

        if source == "fyers_index":
            mark = OK
        elif source == "kite_index_futures":
            mark, _ = WARN, degraded.append(name)
        else:
            mark, _ = BAD, dead.append(name)

        print(f"{mark} {name:<12} source={source:<20} unit={activity.get('unit')}")
        print(f"          volume={float(activity.get('volume') or 0.0):,.0f}"
              f"   thresholds_comparable={activity.get('comparable_to_thresholds')}")
        print(f"          fyers_online={activity.get('fyers_online')}  last cash tick: {age_text}")
        if futures:
            print(f"          futures {futures.get('tradingsymbol') or '?'}: "
                  f"vol={float(futures.get('volume') or 0.0):,.0f} "
                  f"oi={float(futures.get('oi') or 0.0):,.0f} "
                  f"available={futures.get('available')}")
        print(f"          confirmation={activity.get('confirmation')}")

    print("=" * 62)
    if dead:
        print(f"{BAD} NO volume source for: {', '.join(dead)}")
        print("        Every volume-gated entry AND the thrust exit guard are dead for these.")
    if degraded:
        print(f"{WARN} On the FUTURES fallback: {', '.join(degraded)}")
        print("        Fyers is the feed that went quiet — that is the thing to fix.")
        print("        Ratio gates still work (a ratio is unit-free), but absolute")
        print("        volume is now contracts, not constituent shares (~1000x apart).")
    if not degraded and not dead:
        print(f"{OK} All indices on primary Fyers cash volume.")
    return 1 if dead else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)
