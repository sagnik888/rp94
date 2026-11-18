"""Complete Upstox OAuth flow and persist access token."""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from data.upstox_auth import get_upstox_auth, reset_upstox_auth


def _print_status(auth) -> int:
    st = auth.status()
    print("=" * 64)
    print("UPSTOX SESSION STATE")
    print("=" * 64)
    missing = auth.missing_credentials()
    print(f"credentials     : {'OK' if not missing else 'MISSING ' + ', '.join(missing)}")
    print(f"connected       : {st.get('connected')}")
    if st.get("user_id"):
        print(f"user            : {st['user_id']}")
    if st.get("user_name"):
        print(f"name            : {st['user_name']}")
    if st.get("expires_at"):
        print(f"expires         : {st['expires_at']}  ({st.get('hours_remaining')} h left)")
    print("daily cutoff    : 03:30 IST")
    if st.get("expiring_soon"):
        print("                  ** EXPIRING WITHIN THE HOUR - re-authenticate now **")
    if st.get("auth_required"):
        print(f"ACTION REQUIRED : {st.get('reason')}")
    return 0 if st.get("connected") else 1


def main(argv: list[str]) -> int:
    auth = get_upstox_auth()

    if "--check" in argv:
        return _print_status(auth)

    if "--url" in argv:
        try:
            print(auth.login_url())
            return 0
        except ValueError as exc:
            print(f"Error: {exc}")
            return 1

    positional = [a for a in argv if not a.startswith("-")]
    code = positional[0] if positional else ""

    if not code:
        try:
            url = auth.login_url()
        except ValueError as exc:
            print(f"Error: {exc}")
            return 1
        print("=" * 64)
        print("1. Open this URL in an INCOGNITO/PRIVATE browser window:")
        print()
        print(f"   {url}")
        print()
        print("   NOTE: Do NOT scan QR code. Enter Phone -> OTP -> PIN (173089).")
        print("2. You will be redirected to http://127.0.0.1:8500/api/upstox/callback?code=...")
        print("   Paste the FULL URL, code, or access token below.")
        print("=" * 64)
        try:
            code = input("\ncode, redirect URL, or access token: ")
        except (EOFError, KeyboardInterrupt):
            print("\nAborted.")
            return 1

    extracted = auth.extract_code(code)
    if not extracted:
        print("No code or token supplied.")
        return 1

    print("\nExchanging code / validating token ...")
    try:
        result = auth.complete_login(extracted)
    except Exception as exc:
        print(f"FAILED: {exc}")
        print()
        print("Most common causes:")
        print("  * the code was truncated, already used, or expired (~2 minutes)")
        print("  * logged in via QR instead of Phone number (bypasses OAuth)")
        print("  * existing browser session on pro.upstox.com interfered (use incognito)")
        return 1

    print("Success. Token verified and cached.")
    if result.get("expires_at"):
        print(f"  expires {result['expires_at']}")
    reset_upstox_auth()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
