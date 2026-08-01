"""Verify the live Kite integration end to end, after the daily login.

    venv/Scripts/python.exe scripts/check_kite_setup.py

Exercises the real API — contract master, quotes, depth, historical candles, order book,
positions, margins — and prints what it actually got back. Places no orders.

This is where the fake-based unit tests stop and reality begins: real tradingsymbols,
real depth payloads, real margin field names. Nothing about the Kite integration should
be trusted for live trading until this passes.
"""
from __future__ import annotations

import sys
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

OK, WARN, BAD = "  [OK]  ", "  [WARN]", "  [FAIL]"


def main() -> int:
    failures: list[str] = []
    from config.settings import get_settings
    from data.kite_auth import get_kite_auth

    settings = get_settings()
    auth = get_kite_auth(settings)

    print("\n=== 1. Session ===")
    missing = auth.missing_credentials()
    if missing:
        print(f"{BAD} missing credentials: {', '.join(missing)}")
        print("         Run: venv/Scripts/python.exe scripts/set_kite_secret.py")
        return 1
    status = auth.status()
    if not status["connected"]:
        print(f"{BAD} no live Kite session: {status['reason']}")
        try:
            print(f"         Log in here first: {auth.login_url()}")
        except Exception:
            pass
        return 1
    print(f"{OK} session active for {status['user_id'] or 'user'} "
          f"(token issued {status['token_updated_at']})")

    kite = auth.kite

    print("\n=== 2. Profile ===")
    try:
        profile = kite.profile()
        print(f"{OK} {profile.get('user_name', '?')} / {profile.get('user_id', '?')}  "
              f"exchanges={','.join(profile.get('exchanges') or [])}")
        products = profile.get("products") or []
        if "NRML" not in products:
            failures.append("NRML product not enabled — carryforward option orders will be refused")
            print(f"{BAD} NRML not enabled (products: {products})")
    except Exception as exc:
        failures.append(f"profile read failed: {exc}")
        print(f"{BAD} {type(exc).__name__}: {exc}")

    print("\n=== 3. Contract master + resolution ===")
    from data.kite_market_data import KiteDataSource

    source = KiteDataSource(auth=auth)
    try:
        total = source.load_instruments()
        print(f"{OK} {total} instruments cached")
    except Exception as exc:
        failures.append(f"instrument dump failed: {exc}")
        print(f"{BAD} {type(exc).__name__}: {exc}")
        total = 0

    resolved = {}
    if total:
        for index in ("NIFTY", "BANKNIFTY", "MIDCPNIFTY", "SENSEX"):
            expiries = source.expiries_for(index)
            if not expiries:
                failures.append(f"no live expiry found for {index}")
                print(f"{BAD} {index:11} no live expiry in the master")
                continue
            strikes = source.available_strikes(index, "CE", expiry=expiries[0])
            if not strikes:
                print(f"{WARN} {index:11} expiry {expiries[0]} has no CE strikes")
                continue
            mid = strikes[len(strikes) // 2]
            row = source.resolve_option(index, mid, "CE", expiry=expiries[0])
            if not row:
                failures.append(f"{index} resolution failed for strike {mid}")
                print(f"{BAD} {index:11} could not resolve {mid} CE")
                continue
            resolved[index] = row
            print(f"{OK} {index:11} next expiry {expiries[0]} | {len(strikes)} strikes | "
                  f"e.g. {row['tradingsymbol']} token={row['instrument_token']} lot={row['lot_size']}")
            # Every expiry offered must still be alive; a dead one is the AB4046 bug.
            if expiries[0] < date.today():
                failures.append(f"{index} nearest expiry {expiries[0]} is in the past")

    print("\n=== 4. Quotes and depth (the missing_bid_ask fix) ===")
    for index, row in resolved.items():
        exchange = row.get("exchange") or "NFO"
        depth = source.get_depth(exchange, row["tradingsymbol"])
        marker = OK if depth["usable"] else WARN
        print(f"{marker} {index:11} ltp={depth['last_price']:.2f} "
              f"bid={depth['bid']:.2f} ask={depth['ask']:.2f} "
              f"usable={depth['usable']}{' - ' + depth['reason'] if depth['reason'] else ''}")
    if resolved and not any(
        source.get_depth(r.get("exchange") or "NFO", r["tradingsymbol"])["usable"]
        for r in resolved.values()
    ):
        print("         (all books empty — expected outside market hours)")

    print("\n=== 5. Historical candles ===")
    for index, row in list(resolved.items())[:2]:
        frame = source.get_historical(row["instrument_token"], "5min", days_back=5)
        if frame.empty:
            print(f"{WARN} {index:11} no candles returned (illiquid or outside data window)")
        else:
            print(f"{OK} {index:11} {len(frame)} x 5min candles, last close "
                  f"{frame['close'].iloc[-1]:.2f} at {frame.index[-1]}")

    print("\n=== 6. Order book / positions / margins ===")
    try:
        orders = kite.orders()
        print(f"{OK} order book readable ({len(orders)} order(s) today)")
    except Exception as exc:
        failures.append(f"order book read failed: {exc}")
        print(f"{BAD} orders: {type(exc).__name__}: {exc}")

    from trading.kite_broker import KiteBroker

    broker = KiteBroker(kite=kite, auth=auth)
    broker._execution_lease = None
    try:
        positions = broker.get_positions()
        rows = (positions or {}).get("data") or []
        print(f"{OK} positions readable ({len(rows)} open)")
        for row in rows:
            print(f"         {row.get('tradingsymbol')} netqty={row.get('netqty')} pnl={row.get('pnl')}")
    except Exception as exc:
        failures.append(f"positions read failed: {exc}")
        print(f"{BAD} positions: {type(exc).__name__}: {exc}")

    # Requirement first: /margins/orders is the number Kite actually charges, and it
    # answers around the clock. Free cash comes from /user/margins, which returns
    # "UNKNOWN_REQUEST Request not registered" outside market hours because Zerodha
    # does not publish RMS limits until the session initialises — a normal condition,
    # not a broken integration, so it must not read as a failure here.
    sample = next(iter(resolved.values()), None)
    if sample:
        required = broker.required_entry_margin(
            sample["tradingsymbol"], int(sample.get("lot_size") or 1), "BUY"
        )
        if required is None:
            failures.append("order-margin preflight returned nothing")
            print(f"{BAD} order margin: Kite returned no requirement")
        else:
            print(f"{OK} order margin for 1 lot {sample['tradingsymbol']}: {required:,.2f}")

    available = broker.available_funds()
    if available is None:
        print(f"{WARN} free cash unavailable — Zerodha does not publish RMS limits "
              f"outside market hours. Re-check after 09:15 IST.")
    else:
        print(f"{OK} free cash {available:,.2f}")

    print("\n=== 7. Open interest / PCR (replaces the Fyers chain dependency) ===")
    for index in ("NIFTY", "BANKNIFTY"):
        row = resolved.get(index)
        if not row:
            continue
        spot = source.get_ltp(*reversed(
            {"NIFTY": ("NIFTY 50", "NSE"), "BANKNIFTY": ("NIFTY BANK", "NSE")}[index]
        )) or 0.0
        chain = source.get_option_chain(index, spot=spot, strikes_each_side=10)
        if chain.empty:
            failures.append(f"{index} option chain empty")
            print(f"{BAD} {index:11} option chain empty")
            continue
        pcr = source.put_call_ratio(chain)
        marker = OK if pcr > 0 else WARN
        print(f"{marker} {index:11} {len(chain)} strikes | callOI={chain['call_oi'].sum():>13,.0f} "
              f"putOI={chain['put_oi'].sum():>13,.0f} | PCR={pcr}")
    print("         OI change stays 0 by design: Kite reports a LEVEL, not a delta —")
    print("         the snapshot store is what turns levels into changes.")

    print("\n=== 8. GTT (protection that survives this process dying) ===")
    try:
        triggers = kite.get_gtts()
        active = [t for t in triggers if str(t.get("status", "")).lower() == "active"]
        print(f"{OK} GTT API reachable — {len(active)} active trigger(s)")
    except Exception as exc:
        failures.append(f"GTT unavailable: {exc}")
        print(f"{BAD} GTT: {type(exc).__name__}: {exc}")

    print("\n=== Result ===")
    for item in failures:
        print(f"{BAD} {item}")
    if failures:
        print("\n  NOT READY for live trading on Kite.\n")
        return 1
    print("\n  Kite integration verified against the live API. No orders were placed.\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
