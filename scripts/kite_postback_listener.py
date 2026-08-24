"""Minimal public listener for Zerodha order postbacks.

    venv/Scripts/python.exe scripts/kite_postback_listener.py
    cloudflared tunnel --url http://127.0.0.1:8555

Then register  https://<tunnel-host>/api/kite_postback  on developers.kite.trade.

WHY THIS EXISTS INSTEAD OF TUNNELLING THE DASHBOARD
Zerodha requires a public HTTPS postback URL. Tunnelling the dashboard itself would
put the whole trading UI — mode switches, manual execute, config, the Kite login
route — on a public address. Even token-protected, that is a live brokerage account
reachable from the internet, and a tunnel makes every request arrive from 127.0.0.1
so any loopback-based check silently passes.

So this process exposes exactly ONE path and nothing else:
  * only POST /api/kite_postback is routed; every other path 404s
  * the payload is verified against Zerodha's checksum before it is relayed
  * it holds no session, no token, and cannot place or cancel an order
  * it relays to the dashboard on loopback, which re-verifies the checksum itself

Postbacks remain optional. The system reconciles through order_history() and trades()
regardless; this only removes polling latency.
"""
from __future__ import annotations

import hashlib
import json
import secrets
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from loguru import logger

app = FastAPI(title="UT1 Kite postback listener", docs_url=None, redoc_url=None)

LISTEN_PORT = 8555


def _api_secret() -> str:
    from config.settings import get_settings

    return str(getattr(get_settings(), "kite_api_secret", "") or "")


def _dashboard_url() -> str:
    from config.settings import get_settings

    settings = get_settings()
    return f"http://127.0.0.1:{settings.dashboard_port}/api/kite_postback"


@app.get("/api/kite_postback")
async def postback_probe():
    """Answer a plain GET so a URL validator can see the endpoint is live.

    Zerodha rejects a postback URL it cannot verify, and a 404 on GET looks like a
    dead address. Returns nothing sensitive — no account, order or session data —
    and does not accept any input.
    """
    return {"status": "ok", "endpoint": "kite_postback"}


@app.post("/api/kite_postback")
async def postback(request: Request):
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({"status": "error"}, status_code=400)
    if not isinstance(payload, dict):
        return JSONResponse({"status": "error"}, status_code=400)

    order_id = str(payload.get("order_id") or "")
    stamp = str(payload.get("order_timestamp") or "")
    received = str(payload.get("checksum") or "")
    # Hardened to match the dashboard route (2026-08-16). THIS is the process actually
    # exposed to the internet — the dashboard route sits behind it — so it had the
    # weaker of the two checks, which is backwards.
    secret = _api_secret()
    if not secret:
        # With no secret the expected digest is sha256(order_id + stamp + "") which any
        # caller can compute, so every forgery would verify. Refuse to serve rather than
        # authenticate against a secret we do not have. The __main__ guard below is not
        # enough: it is bypassed entirely when served as `uvicorn scripts...:app`.
        logger.error(
            "Postback refused: no api_secret configured, so no payload can be "
            "authenticated. The route stays closed until a Kite session exists."
        )
        return JSONResponse({"status": "error"}, status_code=503)
    expected = hashlib.sha256(f"{order_id}{stamp}{secret}".encode("utf-8")).hexdigest()
    # compare_digest, not != : a plain comparison short-circuits on the first differing
    # byte and leaks the matched prefix length through timing.
    if not received or not secrets.compare_digest(received, expected):
        # Reject before relaying. This endpoint is public by necessity, and a forged
        # "COMPLETE" reaching the trade manager would invent a position that is not
        # there. The dashboard verifies again — belt and braces.
        logger.warning(f"Rejected unverified postback for order {order_id or '<none>'}")
        return JSONResponse({"status": "error"}, status_code=403)

    try:
        request_body = json.dumps(payload).encode("utf-8")
        relay = urllib.request.Request(
            _dashboard_url(), data=request_body,
            headers={"Content-Type": "application/json"}, method="POST",
        )
        with urllib.request.urlopen(relay, timeout=5) as response:
            response.read()
        logger.info(f"Relayed postback: order {order_id} -> {payload.get('status')}")
    except Exception as exc:
        # Never fail loudly at Zerodha; the system reconciles by polling anyway.
        logger.error(f"Postback relay failed for {order_id}: {exc}")
    return {"status": "ok"}


@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
async def block_everything_else(path: str):
    """Nothing but the postback path is reachable from the public address."""
    return JSONResponse({"status": "not found"}, status_code=404)


if __name__ == "__main__":
    import uvicorn

    from config.settings import get_settings

    if not _api_secret():
        print("KITE_API_SECRET is not set; postbacks cannot be verified. Aborting.")
        raise SystemExit(1)

    # Bind-check first. uvicorn surfaces a taken port as a raw WinError 10048, which
    # reads like a bug rather than "it is already running".
    import socket

    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.bind(("127.0.0.1", LISTEN_PORT))
    except OSError:
        print(f"Port {LISTEN_PORT} is already in use — the listener is probably running")
        print("already in another window. Close that one first, or check with:")
        print(f'  powershell "Get-NetTCPConnection -LocalPort {LISTEN_PORT}"')
        raise SystemExit(1)
    finally:
        probe.close()

    print(f"Postback listener on 127.0.0.1:{LISTEN_PORT}")
    print(f"  relaying to {_dashboard_url()}")
    print(f"  expose with:  cloudflared tunnel --url http://127.0.0.1:{LISTEN_PORT}")
    print(f"  then register https://<tunnel-host>/api/kite_postback at developers.kite.trade")
    uvicorn.run(app, host="127.0.0.1", port=LISTEN_PORT, log_level="warning")
