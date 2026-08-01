"""Write KITE_API_SECRET into .env without it appearing on screen or in history.

    venv/Scripts/python.exe scripts/set_kite_secret.py

The secret is read with getpass, so it is never echoed to the terminal, never lands in
shell history, and never has to be pasted into a chat. Run this again after any
regeneration — it replaces the existing line rather than appending a duplicate.
"""
from __future__ import annotations

import re
import sys
from getpass import getpass
from pathlib import Path

KEY = "KITE_API_SECRET"


def _looks_like_placeholder(value: str) -> bool:
    lowered = value.lower()
    return (
        value.startswith("<")
        or value.endswith(">")
        or any(m in lowered for m in ("paste", "regenerate", "your_", "changeme", "todo"))
    )


def main() -> int:
    env_path = Path(".env")
    if not env_path.exists():
        print("No .env in the current directory. Run this from the project root.")
        return 1

    secret = getpass(f"{KEY} (input hidden, paste and press Enter): ").strip()
    if not secret:
        print("Nothing entered; .env left unchanged.")
        return 1
    if _looks_like_placeholder(secret):
        print("That looks like a placeholder, not a real secret. .env left unchanged.")
        return 1
    if len(secret) < 16:
        print(f"That is only {len(secret)} characters — Kite secrets are longer. Left unchanged.")
        return 1
    # A shell command pasted at this prompt used to sail through: it is not blank, not
    # a placeholder, and comfortably longer than 16 characters. It then failed only at
    # login as "Invalid `checksum`", which looks like a broken integration.
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from data.kite_auth import KiteAuth

    malformed = KiteAuth._malformed_reason(secret)
    if malformed:
        print(f"That {malformed}.")
        print("Paste ONLY the api_secret from developers.kite.trade. .env left unchanged.")
        return 1

    lines = env_path.read_text(encoding="utf-8").splitlines()
    replaced = False
    for index, line in enumerate(lines):
        if re.match(rf"^\s*{KEY}\s*=", line):
            lines[index] = f"{KEY}={secret}"
            replaced = True
            break
    if not replaced:
        lines.append(f"{KEY}={secret}")
    env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    print(f"{KEY} {'updated' if replaced else 'added'} ({len(secret)} chars). Not displayed.")

    # Prove it loads, without revealing the value.
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    try:
        from config.settings import get_settings
        from data.kite_auth import KiteAuth, reset_kite_auth

        reset_kite_auth()
        settings = get_settings()
        auth = KiteAuth(
            api_key=getattr(settings, "kite_api_key", ""),
            api_secret=getattr(settings, "kite_api_secret", ""),
            token_file=getattr(settings, "kite_token_file", "kite_token.json"),
        )
        missing = auth.missing_credentials()
        if missing:
            print(f"Still incomplete: {', '.join(missing)}")
            return 1
        print("Credentials complete. Start the login at:")
        print(f"  {auth.login_url()}")
    except Exception as exc:
        print(f"Wrote the value, but could not verify it loads: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
