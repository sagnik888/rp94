import io
import json
import sys
import time
from datetime import datetime, timedelta

import pandas as pd
import requests
from kiteconnect import KiteConnect

from config.settings import get_settings
from data.constituent_volume import ConstituentVolumeService

IST = "Asia/Kolkata"
settings = get_settings()
token = json.load(open("kite_token.json", encoding="utf-8"))["access_token"]
kite = KiteConnect(api_key=settings.kite_api_key)
kite.set_access_token(token)

# Mirror load_instruments(): public dump, by_symbol[(tradingsymbol.upper(), exchange)]
by_symbol = {}
for exchange in ("NSE", "BSE"):
    txt = requests.get(f"https://api.kite.trade/instruments/{exchange}", timeout=30,
                       headers={"User-Agent": "Mozilla/5.0"}).text
    df = pd.read_csv(io.StringIO(txt))
    for _, row in df.iterrows():
        by_symbol[(str(row["tradingsymbol"]).upper(), exchange)] = row
    if exchange == "NSE":
        idx_rows = df[df["segment"] == "INDICES"]
        print("index rows:\n", idx_rows[["tradingsymbol", "instrument_token"]].to_string())


def instrument_token(sym, exch):
    row = by_symbol.get((str(sym or "").upper(), str(exch or "").upper()))
    t = int(row["instrument_token"]) if row is not None else 0
    return t or None


service = ConstituentVolumeService("data_store/index_constituents.json")
print("membership last_error:", service.last_error)

index = "NIFTY"
day = datetime.now().date()
member_objs = service.membership(index, day)
print("today membership:", member_objs.version if member_objs else None)

# index 1min history (NIFTY 50 = 256265)
now = datetime.now()
target = now
frm = now - timedelta(days=8)
base = pd.DataFrame(kite.historical_data(256265, frm, target, "minute"))
base["date"] = pd.to_datetime(base["date"])
base = base.set_index("date").sort_index()
base = ConstituentVolumeService.local_frame(base)
base = base.loc[(base.index + pd.Timedelta(minutes=1) <= pd.Timestamp(target))].between_time("09:15", "15:29")
print("base bars:", len(base), base.index.min(), base.index.max())

# stock frames
frames = {}
resolution = {}
for exch, sym in member_objs.members:
    t = instrument_token(sym, exch) or instrument_token(sym + "-EQ", exch)
    resolution[(exch, sym)] = t
    if not t:
        frames[(exch, sym)] = pd.DataFrame()
        continue
    rows = kite.historical_data(t, frm, target, "minute")
    frame = pd.DataFrame(rows)
    if "date" in frame.columns:
        frame["date"] = pd.to_datetime(frame["date"])
        frame = frame.set_index("date").sort_index()
    for col in ("open", "high", "low", "close", "volume"):
        if col in frame.columns:
            frame[col] = pd.to_numeric(frame[col], errors="coerce")
    frames[(exch, sym)] = frame
    time.sleep(0.4)

missing_token = [k for k, v in resolution.items() if not v]
empty_frame = [k for k, f in frames.items() if f is None or f.empty]
no_vol_col = [k for k, f in frames.items() if f is not None and not f.empty and "volume" not in f.columns]

vol = service.aggregate(index, base.index, frames, "kite", now=pd.Timestamp(target))
print("\naggregate observed/expected value counts:")
print(vol[["volume_members_observed", "volume_members_expected"]].value_counts())

# per-member contribution on a dated bar
positions = pd.DatetimeIndex(vol.index)
sample = positions[(positions.date == day)]
contributors = []
for exch, sym in member_objs.members:
    frame = frames[(exch, sym)]
    if frame is None or frame.empty or "volume" not in frame.columns:
        contributors.append((sym, "NO_FRAME/COL", 0))
        continue
    local = ConstituentVolumeService.local_frame(frame)
    series = pd.to_numeric(local["volume"], errors="coerce").reindex(base.index)
    valid = series.notna() & (series >= 0)
    n = int(valid.loc[sample].sum())
    contributors.append((sym, f"tok={resolution[(exch,sym)]}", n))
print("\nper-member valid-bar count over today's base bars:")
for sym, tag, n in contributors:
    if n != len(sample):
        print(f"  SHORT {sym:12s} {tag:12s} valid={n}/{len(sample)}")
print("missing_token:", missing_token)
print("empty_frame:", empty_frame)
print("no_vol_col:", no_vol_col)
