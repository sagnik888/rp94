import io
import json

import pandas as pd
import requests

payload = json.load(open(r"c:\Users\sagnik\Desktop\rp94\rp46-3\data_store\index_constituents.json", encoding="utf-8"))
want = {}
for m in payload["memberships"]:
    if m["effective_from"] == "2026-10-30":
        want[m["index"]] = [(x["exchange"], x["symbol"]) for x in m["members"]]

print("today memberships:", {k: len(v) for k, v in want.items()})

for exchange in ("NSE", "BSE"):
    txt = requests.get(f"https://api.kite.trade/instruments/{exchange}", timeout=30,
                       headers={"User-Agent": "Mozilla/5.0"}).text
    df = pd.read_csv(io.StringIO(txt))
    syms = set(df.loc[df["segment"].isin(["INDICES", "NSE", "BSE"]), "tradingsymbol"].astype(str).str.upper())
    nse = set(df["tradingsymbol"].astype(str).str.upper())
    eq = set(df.loc[df["instrument_type"].astype(str).str.upper() == "EQ", "tradingsymbol"].astype(str).str.upper())
    print(f"\n=== {exchange} master rows={len(df)} ===")
    for index, members in want.items():
        if members and members[0][0] != exchange:
            continue
        missing = [s for e, s in members if s.upper() not in nse]
        print(f"{index}: {len(members)-len(missing)}/{len(members)} resolvable; missing={missing}")
