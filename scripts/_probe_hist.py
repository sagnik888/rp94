import json
import time
from datetime import datetime, timedelta

from kiteconnect import KiteConnect

from config.settings import get_settings

settings = get_settings()
token = json.load(open("kite_token.json", encoding="utf-8"))["access_token"]
kite = KiteConnect(api_key=settings.kite_api_key)
kite.set_access_token(token)

payload = json.load(open("data_store/index_constituents.json", encoding="utf-8"))
members = {}
for m in payload["memberships"]:
    if m["effective_from"] == "2026-10-30":
        members[m["index"]] = [(x["exchange"], x["symbol"]) for x in m["members"]]

now = datetime.now()
frm = now - timedelta(days=8)
to = now

print("profile:", kite.profile().get("user_id"))

for index in ("NIFTY",):
    print(f"\n=== {index} ({len(members[index])} members) ===")
    bad = []
    for exchange, symbol in members[index]:
        inst = kite.lookup_instrument(exchange, symbol) if hasattr(kite, "lookup_instrument") else None
        if inst is None:
            rows = kite.instruments(exchange)
            inst = next((r for r in rows if r["tradingsymbol"] == symbol and r["instrument_type"] == "EQ"), None)
        if not inst:
            bad.append((symbol, "NO_TOKEN"))
            print(f"{symbol:14s} NO_TOKEN")
            continue
        try:
            candles = kite.historical_data(inst["instrument_token"], frm, to, "minute")
        except Exception as exc:
            bad.append((symbol, f"ERR {type(exc).__name__}: {exc}"))
            print(f"{symbol:14s} ERR {type(exc).__name__}: {exc}")
            time.sleep(0.45)
            continue
        good = [c for c in candles if c.get("volume") is not None]
        print(f"{symbol:14s} token={inst['instrument_token']:>8} candles={len(candles):>5} with_volume={len(good)}")
        if not candles:
            bad.append((symbol, "EMPTY"))
        time.sleep(0.45)
    print("\nBAD:", bad)
