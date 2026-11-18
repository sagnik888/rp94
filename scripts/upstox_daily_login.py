"""Daily Upstox re-authentication helper.

Checks token freshness:
  * If valid, prints hours remaining and exits.
  * If expired or missing, opens default browser to Upstox authorization URL.
  * Prompts operator to paste the redirect URL, code, or access token.

Usage:
    python scripts/upstox_daily_login.py
"""
import sys
import webbrowser
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from data.upstox_auth import get_upstox_auth


def main():
    auth = get_upstox_auth()
    st = auth.status()
    print("=" * 64)
    print("  UPSTOX DAILY AUTHENTICATION CHECK & TOKEN REFRESH")
    print("=" * 64)

    if st.get("connected") and not st.get("expiring_soon"):
        print("Token is ACTIVE and healthy.")
        print(f"User ID: {st.get('user_id')}")
        if st.get("user_name"):
            print(f"Name   : {st.get('user_name')}")
        print(f"Hours remaining: {st.get('hours_remaining')}h (expires {st.get('expires_at')})")
        print("No re-authentication needed.")
        return 0

    print(f"Status: {st.get('reason')}")
    try:
        url = auth.login_url()
        print("\nOpening Upstox login dialog in your browser...")
        print(f"URL: {url}\n")
        print("  IMPORTANT:")
        print("  1. Do NOT scan QR code. Enter Phone -> OTP -> PIN (173089).")
        print("  2. If cookies interfere, open the URL in an INCOGNITO window.")
        webbrowser.open(url)
    except Exception as e:
        print(f"Could not open browser: {e}")
        return 1

    print("\nAfter logging in, Upstox redirects with ?code=...")
    print("Paste the FULL redirect URL, authorization code, or access token below:")
    try:
        code_input = input("\nRedirect URL / Code / Token: ").strip()
        if not code_input:
            print("Cancelled.")
            return 1
        res = auth.complete_login(code_input)
        print("\nSUCCESS! Upstox token refreshed.")
        print(f"User   : {res.get('user_id')}")
        if res.get("user_name"):
            print(f"Name   : {res.get('user_name')}")
        print(f"Expires: {res.get('expires_at')}")
        return 0
    except Exception as e:
        print(f"\nAuthentication failed: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
