"""Does the fill ledger agree with the broker? The gate for re-enabling stacking.

Step 5 of the same-contract rollout says stacking comes back "only after replay and
paper certification". This is the check that produces that evidence.

The claim being tested is narrow and falsifiable: for every contract the account holds,
the sum of the ledger's per-trade holdings equals the broker's net position. If that
holds across several sessions -- including sessions with partial exits, scale-ins and
stops firing -- then attribution is trustworthy and forbid_concurrent_same_contract_real
can be lifted. If it does not, the ledger is not ready and the containment stays.

READ ONLY. Fetches positions, reads the ledger, prints a report. Places no orders,
writes nothing, and never changes a setting.

    python -m scripts.ledger_certification            # live positions
    python -m scripts.ledger_certification --offline  # ledger only, no broker call

Exit code 0 means agreement (or an explicitly requested offline inspection), 1 means a
discrepancy, and 2 means the requested live broker comparison could not be performed.
"""
from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Tuple


# ``python -m scripts.ledger_certification`` already places the repository on
# sys.path.  Operators and audit runners also invoke this file directly; in that
# form Python exposes only ``scripts/`` and the lazy ``data`` import below fails.
if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def _ledger_holdings() -> Dict[str, Dict[str, int]]:
    """{tradingsymbol: {trade_id: holding}} from the persisted fill ledger."""
    from data.sqlite_db import db

    out: Dict[str, Dict[str, int]] = defaultdict(dict)
    try:
        rows = db.conn.execute(
            "SELECT tradingsymbol, trade_id, COALESCE(SUM(quantity_delta), 0) "
            "FROM order_fills GROUP BY tradingsymbol, trade_id"
        ).fetchall()
    except Exception as exc:
        print(f"! could not read the ledger: {exc}")
        return {}
    for symbol, trade_id, held in rows:
        out[str(symbol or "")][str(trade_id or "")] = int(held or 0)
    return dict(out)


def _broker_positions() -> Tuple[Dict[str, int], str]:
    """{tradingsymbol: netqty} from the live broker, plus a note on how it went."""
    try:
        from config.settings import get_settings
        from data.kite_auth import get_kite_auth
        from trading.kite_broker import KiteBroker
    except Exception as exc:
        return {}, f"broker unavailable ({exc})"

    try:
        settings = get_settings()
        auth = get_kite_auth(settings)
        broker = KiteBroker(
            kite=auth.kite,
            auth=auth,
            account_id=str(getattr(settings, "kite_user_id", "") or "").strip(),
            instance_id="UT1:ledger-certification",
        )
        resp = broker.get_positions() or {}
    except Exception as exc:
        return {}, f"positions unreadable ({exc})"

    rows = resp.get("data", []) if isinstance(resp, dict) else []
    positions: Dict[str, int] = {}
    for row in rows or []:
        symbol = str(row.get("tradingsymbol") or "")
        if not symbol:
            continue
        try:
            positions[symbol] = int(float(row.get("netqty") or 0))
        except (TypeError, ValueError):
            continue
    return positions, "ok"


def _report(ledger: Dict[str, Dict[str, int]], positions: Dict[str, int], offline: bool) -> int:
    symbols = sorted(set(ledger) | set(positions))
    if not symbols:
        print("Nothing to certify: the ledger is empty and the account holds nothing.")
        print()
        print("The ledger only fills from REAL trades. If the account is unfunded or")
        print("every entry is being refused, no evidence can accumulate and stacking")
        print("must stay disabled.")
        return 0

    problems: List[str] = []
    print(f"{'CONTRACT':<28} {'BROKER':>8} {'LEDGER':>8}  ATTRIBUTION")
    print("-" * 78)
    for symbol in symbols:
        holdings = ledger.get(symbol, {})
        led_total = sum(holdings.values())
        broker_qty = abs(positions.get(symbol, 0))
        live = {t: q for t, q in holdings.items() if q > 0}
        detail = ", ".join(f"{t}={q}" for t, q in sorted(live.items())) or "-"

        # A trade whose own orders net NEGATIVE has had more exit quantity recorded
        # against it than entry, which is precisely the mis-attribution this gate exists
        # to catch. db.owned_quantity clamps that away with max(0, ...) for the sizing
        # callers, so this script is the ONLY place the sign is visible -- and summing it
        # into led_total let one trade's excess cancel another's deficit and report
        # agreement. The `q > 0` filter on `detail` hid the offending trade as well.
        negatives = {t: q for t, q in holdings.items() if q < 0}
        if negatives:
            detail += " | NEGATIVE: " + ", ".join(
                f"{t}={q}" for t, q in sorted(negatives.items())
            )

        if offline:
            print(f"{symbol:<28} {'n/a':>8} {led_total:>8}  {detail}")
            if negatives:
                problems.append(
                    f"{symbol}: {len(negatives)} trade(s) attribute negative quantity "
                    f"({', '.join(f'{t}={q}' for t, q in sorted(negatives.items()))})"
                )
            continue

        flag = " " if led_total == broker_qty and not negatives else " <-- MISMATCH"
        print(f"{symbol:<28} {broker_qty:>8} {led_total:>8}  {detail}{flag}")
        if led_total != broker_qty:
            problems.append(
                f"{symbol}: broker holds {broker_qty}, ledger attributes {led_total}"
            )
        if negatives:
            problems.append(
                f"{symbol}: {len(negatives)} trade(s) attribute negative quantity "
                f"({', '.join(f'{t}={q}' for t, q in sorted(negatives.items()))}); the "
                "aggregate can still match while attribution is wrong"
            )
        if len(live) > 1 and broker_qty:
            print(f"{'':<28} {'':>8} {'':>8}  ^ shared contract: the case stacking depends on")

    print()
    if offline:
        print("Offline: ledger contents only, nothing compared. Re-run without --offline")
        print("against a funded account with open positions to certify.")
        return 0

    if problems:
        print("NOT CERTIFIED. The ledger and the broker disagree:")
        for p in problems:
            print(f"  - {p}")
        print()
        print("Keep forbid_concurrent_same_contract_real=True until each is explained.")
        print("A disagreement here is the ledger mis-attributing, which is exactly what")
        print("would mis-size a protective stop on a shared contract.")
        return 1

    print("Ledger and broker agree on every contract.")
    print()
    print("One clean run is not certification. Repeat across sessions that include a")
    print("partial exit, a scale-in and a protective stop firing -- those are the legs")
    print("that shipped wrong before, and the ones stacking depends on.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--offline", action="store_true",
        help="read the ledger only; do not contact the broker",
    )
    args = parser.parse_args()

    ledger = _ledger_holdings()
    positions: Dict[str, int] = {}
    if not args.offline:
        positions, note = _broker_positions()
        if note != "ok":
            print(f"! {note}")
            print("Live broker comparison was requested but could not be performed.\n")
            _report(ledger, {}, offline=True)
            print("\nNOT CERTIFIED: broker state is unavailable.")
            return 2

    return _report(ledger, positions, args.offline)


if __name__ == "__main__":
    sys.exit(main())
