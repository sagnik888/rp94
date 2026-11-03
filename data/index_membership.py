"""Refresh conservative, current-day membership snapshots from index publishers."""
import csv
from datetime import timedelta
import hashlib
import io
import json
from pathlib import Path
import threading

import pandas as pd
import requests

from data.constituent_volume import ConstituentVolumeService

SOURCES = {
    "NIFTY": ("https://www.niftyindices.com/IndexConstituent/ind_nifty50list.csv", 50),
    "BANKNIFTY": ("https://www.niftyindices.com/IndexConstituent/ind_niftybanklist.csv", 14),
    "MIDCPNIFTY": ("https://www.niftyindices.com/IndexConstituent/ind_niftymidcapselect_list.csv", 25),
    "SENSEX": ("https://www.bseindices.com/AsiaIndexAPI/api/Codewise_Indices/w?code=16", 30),
}
BSE_MASTER = "https://api.kite.trade/instruments/BSE"
_refresh_lock = threading.Lock()


def ensure_current_membership(path, *, day=None, fetch=None):
    """Keep valid curated versions; bootstrap absent indices for today only.

    A downloaded current list never certifies earlier sessions. Publishers' count
    changes fail closed until reviewed. No partially downloaded list is committed.
    The caller runs this on a background data worker, never the scanner loop.
    """
    path = Path(path)
    day = day or pd.Timestamp.now(tz="Asia/Kolkata").date()
    def download(url):
        response = requests.get(url, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
        response.raise_for_status()
        return response.text
    fetch = fetch or download
    with _refresh_lock:
        service = ConstituentVolumeService(path)
        missing = [index for index in SOURCES if service.membership(index, day) is None]
        if not missing:
            return False
        if path.exists() and not service.memberships:
            raise ValueError("existing_membership_invalid")
        payload = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {
            "schema_version": 1, "memberships": []}
        records = []
        for index in missing:
            url, expected = SOURCES[index]
            text = fetch(url)
            if index == "SENSEX":
                rows = json.loads(text)["Table"]
                if any(int(row["index_Code"]) != 16 for row in rows):
                    raise ValueError("wrong_sensex_index")
                codes = [str(row["SCRIP_CODE"]).strip() for row in rows]
                master = list(csv.DictReader(io.StringIO(fetch(BSE_MASTER))))
                identities = {}
                for row in master:
                    if row.get("exchange") == "BSE" and row.get("instrument_type") == "EQ":
                        code, symbol = row["exchange_token"], row["tradingsymbol"]
                        if code in identities and identities[code] != symbol:
                            raise ValueError("ambiguous_bse_identity")
                        identities[code] = symbol
                symbols = [identities[code] for code in codes]
                venue = "BSE"
            else:
                rows = list(csv.DictReader(io.StringIO(text.lstrip("\ufeff"))))
                if any(row.get("Series") != "EQ" or not row.get("ISIN Code", "").startswith("IN") for row in rows):
                    raise ValueError("invalid_equity_constituent")
                symbols = [row["Symbol"].strip().upper() for row in rows]
                venue = "NSE"
            if len(symbols) != expected or len(set(symbols)) != expected or not all(symbols):
                raise ValueError("publisher_membership_count_mismatch")
            # A future curated version remains authoritative; never overlap it.
            until = day + timedelta(days=1)
            if any(m.index == index and day < m.effective_until and m.effective_from < until for m in service.memberships):
                raise ValueError("snapshot_overlaps_curated_membership")
            digest = hashlib.sha256("\n".join(sorted(symbols)).encode()).hexdigest()[:16]
            records.append(dict(index=index, effective_from=day.isoformat(),
                effective_until=until.isoformat(), version=f"observed-{day}-{digest}",
                source=url, expected_count=expected, observed_on=day.isoformat(),
                members=[dict(exchange=venue, symbol=symbol) for symbol in sorted(symbols)]))
        payload["memberships"].extend(records)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".pending")
        try:
            temporary.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            checked = ConstituentVolumeService(temporary)
            if not all(checked.membership(index, day) for index in SOURCES):
                raise ValueError("downloaded_membership_invalid")
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)
        return True
