"""Start the postback listener + tunnel, and print the exact URL to register.

    venv/Scripts/python.exe scripts/start_postback_tunnel.py

A free Cloudflare quick-tunnel gets a NEW random hostname every run, so the URL
registered at developers.kite.trade goes stale on every restart. There is no API to
update it — Zerodha only takes it from the portal — so the practical fix is to remove
the friction: this starts both processes, waits for the hostname, checks the endpoint
actually answers through it, and prints the one line to paste.

It also detects when the hostname has NOT changed since the last run and says so, so
you can skip the portal entirely on a plain restart.

For a permanent URL you need a NAMED tunnel on a domain you control
(https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/).
Set KITE_POSTBACK_URL in .env to that hostname and this script will verify it instead
of creating a quick tunnel.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

LISTENER_PORT = 8555
LAST_URL_FILE = Path("data_store") / "kite_postback_tunnel.json"
URL_RE = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")


def _cloudflared() -> str:
    found = shutil.which("cloudflared")
    if found:
        return found
    for candidate in (
        r"C:\Program Files (x86)\cloudflared\cloudflared.exe",
        r"C:\Program Files\cloudflared\cloudflared.exe",
    ):
        if Path(candidate).exists():
            return candidate
    return ""


def _listener_up() -> bool:
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{LISTENER_PORT}/api/kite_postback", timeout=3
        ) as response:
            return response.status == 200
    except Exception:
        return False


def _endpoint_answers(base: str) -> bool:
    try:
        with urllib.request.urlopen(f"{base}/api/kite_postback", timeout=25) as response:
            return response.status == 200
    except Exception:
        return False


def _previous_url() -> str:
    try:
        return str(json.loads(LAST_URL_FILE.read_text(encoding="utf-8")).get("url") or "")
    except Exception:
        return ""


def _remember(url: str) -> None:
    try:
        LAST_URL_FILE.parent.mkdir(parents=True, exist_ok=True)
        LAST_URL_FILE.write_text(json.dumps({"url": url}), encoding="utf-8")
    except Exception:
        pass


def main() -> int:
    from config.settings import get_settings

    settings = get_settings()
    if not str(getattr(settings, "kite_api_secret", "") or "").strip():
        print("KITE_API_SECRET is not set — postbacks cannot be verified. Aborting.")
        return 1

    listener = None
    if _listener_up():
        print(f"[OK]   listener already running on 127.0.0.1:{LISTENER_PORT}")
    else:
        print(f"[..]   starting listener on 127.0.0.1:{LISTENER_PORT}")
        listener = subprocess.Popen(
            [sys.executable, str(Path(__file__).parent / "kite_postback_listener.py")],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        for _ in range(20):
            time.sleep(0.5)
            if _listener_up():
                break
        else:
            print("[FAIL] listener did not come up")
            return 1
        print("[OK]   listener up")

    fixed = str(getattr(settings, "kite_postback_url", "") or "").strip()
    if fixed:
        base = fixed.rstrip("/").removesuffix("/api/kite_postback")
        print(f"[..]   verifying your permanent URL {base}")
        print("[OK]   reachable" if _endpoint_answers(base) else "[WARN] not reachable — is the named tunnel running?")
        print(f"\n  Registered URL stays: {base}/api/kite_postback\n")
        return 0

    binary = _cloudflared()
    if not binary:
        print("[FAIL] cloudflared not found. Install with:  winget install Cloudflare.cloudflared")
        return 1

    print("[..]   opening quick tunnel (free, random hostname)")
    tunnel = subprocess.Popen(
        [binary, "tunnel", "--url", f"http://127.0.0.1:{LISTENER_PORT}"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1,
    )
    url = ""
    deadline = time.time() + 60
    while time.time() < deadline and tunnel.poll() is None:
        line = tunnel.stdout.readline()
        if not line:
            continue
        match = URL_RE.search(line)
        if match:
            url = match.group(0)
            break
    if not url:
        print("[FAIL] tunnel did not report a hostname within 60s")
        return 1
    print(f"[OK]   tunnel up: {url}")

    print("[..]   checking the endpoint answers through the tunnel")
    for _ in range(10):
        if _endpoint_answers(url):
            print("[OK]   endpoint reachable end to end")
            break
        time.sleep(2)
    else:
        print("[WARN] tunnel is up but the endpoint did not answer — check the listener")

    postback_url = f"{url}/api/kite_postback"
    unchanged = _previous_url() == url
    _remember(url)

    print("\n" + "=" * 68)
    if unchanged:
        print("  Hostname is UNCHANGED since the last run — nothing to do at Zerodha.")
    else:
        print("  Register this at developers.kite.trade -> your app -> Postback URL:")
        print(f"\n    {postback_url}\n")
        print("  (The Redirect URL is a DIFFERENT field — do not overwrite it.)")
    print("=" * 68)
    print("\nLeave this window open; closing it drops the tunnel.")
    print("Postbacks are optional — order_history()/trades() still reconcile without them.\n")

    try:
        tunnel.wait()
    except KeyboardInterrupt:
        pass
    finally:
        for process in (tunnel, listener):
            if process is not None and process.poll() is None:
                process.terminate()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
