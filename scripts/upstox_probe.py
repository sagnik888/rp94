"""Upstox authorisation + index-volume probe.

Answers ONE question before we build anything: do Upstox index candles carry real
volume, or zero like Yahoo? That decides whether Upstox is worth wiring in as a
volume source or only as a price tier.

Credentials are read from .env / environment and never printed. Run it in three steps:

    python scripts/upstox_probe.py url                 # 1. print the login URL
    python scripts/upstox_probe.py token <CODE>        # 2. exchange the code (do this fast,
                                                       #    the code expires in ~5 minutes)
    python scripts/upstox_probe.py probe               # 3. verify index volume

Step 2 saves the access token to upstox_token.json (gitignored) so step 3 can reuse it.
Upstox tokens die at 03:30 IST daily and there is no refresh token, so steps 1-2 repeat
each morning if we end up depending on this.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.parse
from datetime import datetime
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
TOKEN_FILE = ROOT / "upstox_token.json"

AUTH_URL = "https://api.upstox.com/v2/login/authorization/dialog"
TOKEN_URL = "https://api.upstox.com/v2/login/authorization/token"
# v3 gives real 5/15-minute candles; v2 intraday only had 1minute/30minute.
INTRADAY_V3 = "https://api.upstox.com/v3/historical-candle/intraday/{key}/{unit}/{interval}"
HISTORICAL_V3 = "https://api.upstox.com/v3/historical-candle/{key}/{unit}/{interval}/{to}/{frm}"

# The four indices this system trades, in Upstox instrument-key form.
INDEX_KEYS = {
    "NIFTY": "NSE_INDEX|Nifty 50",
    "BANKNIFTY": "NSE_INDEX|Nifty Bank",
    "MIDCPNIFTY": "NSE_INDEX|NIFTY MID SELECT",
    "SENSEX": "BSE_INDEX|SENSEX",
}
# A traded instrument as a control: if this has volume and the indices do not, the
# difference is real and not a broken request.
CONTROL_KEY = ("SBIN (equity control)", "NSE_EQ|INE062A01020")


def _load_env() -> None:
    env = ROOT / ".env"
    if not env.exists():
        return
    for line in env.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip("'\""))


def _cfg(name: str, required: bool = True) -> str:
    value = os.environ.get(name, "").strip()
    if required and not value:
        sys.exit(
            f"\n{name} is not set.\n"
            f"Add these to {ROOT / '.env'} (no quotes needed):\n"
            "    UPSTOX_API_KEY=your-api-key\n"
            "    UPSTOX_API_SECRET=your-api-secret\n"
            "    UPSTOX_REDIRECT_URI=http://127.0.0.1:8500/\n"
            "The redirect URI must match EXACTLY what is registered on the Upstox app.\n"
        )
    return value


def cmd_url() -> None:
    params = {
        "client_id": _cfg("UPSTOX_API_KEY"),
        "redirect_uri": _cfg("UPSTOX_REDIRECT_URI"),
        "response_type": "code",
    }
    print("\n1. Open this URL in your browser and log in to Upstox:\n")
    print(f"   {AUTH_URL}?{urllib.parse.urlencode(params)}\n")
    print("2. After login it redirects to your redirect URI with ?code=... in the address bar.")
    print("   The page itself may show an error or 404 — that does not matter, only the URL does.")
    print("3. Copy the code value and run, within ~5 minutes:\n")
    print("   python scripts/upstox_probe.py token PASTE_THE_CODE_HERE\n")


def cmd_token(code: str) -> None:
    resp = requests.post(
        TOKEN_URL,
        headers={"accept": "application/json", "Content-Type": "application/x-www-form-urlencoded"},
        data={
            "code": code,
            "client_id": _cfg("UPSTOX_API_KEY"),
            "client_secret": _cfg("UPSTOX_API_SECRET"),
            "redirect_uri": _cfg("UPSTOX_REDIRECT_URI"),
            "grant_type": "authorization_code",
        },
        timeout=20,
    )
    try:
        payload = resp.json()
    except ValueError:
        sys.exit(f"Non-JSON response ({resp.status_code}): {resp.text[:300]}")

    token = payload.get("access_token")
    if not token:
        # Never echo the payload wholesale — it can contain credential echoes.
        sys.exit(
            f"No access_token returned (HTTP {resp.status_code}).\n"
            f"  status : {payload.get('status')}\n"
            f"  errors : {json.dumps(payload.get('errors'))[:400]}\n"
            "Common causes: the code was already used or older than ~5 minutes, or the\n"
            "redirect_uri does not exactly match the one registered on the app."
        )

    TOKEN_FILE.write_text(
        json.dumps({"access_token": token, "saved_at": datetime.now().isoformat()}, indent=2),
        encoding="utf-8",
    )
    print(f"\nAccess token saved to {TOKEN_FILE.name} (expires 03:30 IST).")
    print("Now run:  python scripts/upstox_probe.py probe\n")


def _token() -> str:
    if not TOKEN_FILE.exists():
        sys.exit("No upstox_token.json — run the 'url' then 'token' steps first.")
    return json.loads(TOKEN_FILE.read_text(encoding="utf-8"))["access_token"]


def _fetch(url: str, token: str) -> tuple[str, list]:
    r = requests.get(
        url, headers={"Accept": "application/json", "Authorization": f"Bearer {token}"}, timeout=20
    )
    try:
        p = r.json()
    except ValueError:
        return f"HTTP {r.status_code} non-JSON", []
    if p.get("status") != "success":
        errs = p.get("errors") or p.get("error") or p
        return f"HTTP {r.status_code} {json.dumps(errs)[:120]}", []
    return "ok", (p.get("data") or {}).get("candles") or []


def _report(label: str, key: str, token: str) -> bool:
    quoted = urllib.parse.quote(key, safe="")
    status, candles = _fetch(INTRADAY_V3.format(key=quoted, unit="minutes", interval="5"), token)
    source = "intraday 5m"
    if status != "ok" or not candles:
        # Market may be shut, or intraday may not cover indices — fall back to recent daily.
        today = datetime.now().strftime("%Y-%m-%d")
        status2, candles = _fetch(
            HISTORICAL_V3.format(key=quoted, unit="days", interval="1", to=today, frm=today), token
        )
        source = f"intraday 5m -> {status}; daily fallback"
        status = status2

    if status != "ok":
        print(f"  {label:26s} FAILED   {status}")
        return False
    if not candles:
        print(f"  {label:26s} no candles returned ({source})")
        return False

    vols = [c[5] for c in candles if len(c) > 5 and isinstance(c[5], (int, float))]
    nonzero = sum(1 for v in vols if v > 0)
    verdict = "REAL VOLUME" if nonzero else "ALL ZERO (same as Yahoo)"
    print(
        f"  {label:26s} {len(candles):4d} candles | vol>0: {nonzero:4d} | "
        f"max {max(vols) if vols else 0:>12,.0f} | {verdict}   [{source}]"
    )
    return nonzero > 0


def cmd_probe() -> None:
    token = _token()
    print("\n=== Does Upstox give real volume for INDEX instruments? ===")
    any_index_volume = False
    for label, key in INDEX_KEYS.items():
        if _report(f"{label} ({key.split('|')[0]})", key, token):
            any_index_volume = True

    print("\n=== Control: a genuinely traded instrument ===")
    control_ok = _report(CONTROL_KEY[0], CONTROL_KEY[1], token)
    if not control_ok:
        print("  NOTE: the control has no volume either — the request or the token is the\n"
              "  problem, not index support. Do not conclude anything about indices yet.")

    print("\n--- VERDICT ---")
    if any_index_volume:
        print("  Upstox DOES return index volume -> worth wiring in as a volume source")
        print("  above the Yahoo tertiary. Tell Claude and it will build the tier.")
    else:
        print("  Upstox returns ZERO index volume, same as Yahoo -> not a volume fix.")
        print("  Keep Fyers history as the volume source (fix the app's History")
        print("  permission) and use Upstox only as a price fallback, if at all.")
    print()


def main() -> None:
    _load_env()
    cmd = (sys.argv[1] if len(sys.argv) > 1 else "").lower()
    if cmd == "url":
        cmd_url()
    elif cmd == "token":
        if len(sys.argv) < 3:
            sys.exit("Usage: python scripts/upstox_probe.py token <CODE>")
        cmd_token(sys.argv[2].strip())
    elif cmd == "probe":
        cmd_probe()
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()
