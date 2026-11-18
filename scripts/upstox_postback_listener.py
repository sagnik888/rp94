"""Upstox OAuth Redirect and Webhook/Postback Listener.

Runs on port 8500 (matching UPSTOX_REDIRECT_URI=http://127.0.0.1:8500):
1. Handles OAuth redirect: captures `?code=...`, exchanges for token, and updates upstox_token.json.
2. Receives Upstox order postbacks/webhooks: relays to the dashboard at /api/upstox_postback.

Usage:
    venv/Scripts/python.exe scripts/upstox_postback_listener.py
"""
from __future__ import annotations

import json
import os
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from loguru import logger

from data.upstox_auth import exchange_code_for_token, get_upstox_auth_status

app = FastAPI(title="Upstox Listener", docs_url=None, redoc_url=None)


@app.get("/")
@app.get("/api/upstox/callback")
@app.get("/callback")
async def oauth_redirect_or_health(request: Request):
    """Handle Upstox OAuth redirect code or health check."""
    params = dict(request.query_params)
    code = params.get("code")

    if code:
        logger.info(f"Received Upstox auth code: {code[:6]}... Exchanging for token.")
        res = exchange_code_for_token(code)
        if res.get("status") == "success":
            html = """
            <!doctype html>
            <html>
            <head><title>Upstox Connected</title>
            <style>body{font-family:sans-serif;background:#0f172a;color:#f8fafc;display:flex;align-items:center;justify-content:center;height:100vh;margin:0;}
            .card{background:#1e293b;padding:32px;border-radius:12px;text-align:center;max-width:400px;border:1px solid #334155;}
            h2{color:#10b981;margin-top:0;}
            p{color:#94a3b8;font-size:14px;}
            a{display:inline-block;margin-top:16px;background:#3b82f6;color:#fff;padding:10px 20px;border-radius:6px;text-decoration:none;font-weight:600;}
            </style></head>
            <body><div class="card">
                <h2>✅ Upstox Connected</h2>
                <p>Access token generated and saved. Daily refresh complete.</p>
                <a href="http://127.0.0.1:8000">Return to Dashboard</a>
            </div></body></html>
            """
            return HTMLResponse(content=html, status_code=200)
        else:
            return HTMLResponse(
                content=f"<h2>❌ Upstox Auth Failed</h2><p>{res.get('message')}</p>",
                status_code=400,
            )

    status = get_upstox_auth_status()
    return JSONResponse({
        "status": "online",
        "service": "Upstox OAuth & Postback Listener",
        "port": 8500,
        "upstox_auth": status
    })


@app.post("/webhook")
@app.post("/api/upstox_postback")
@app.post("/")
async def handle_postback(request: Request):
    """Receive Upstox order webhook and forward to dashboard."""
    try:
        body = await request.body()
        payload = json.loads(body.decode("utf-8")) if body else {}
    except Exception as exc:
        return JSONResponse({"status": "error", "message": f"Malformed payload: {exc}"}, status_code=400)

    logger.info(f"Upstox postback received: order_id={payload.get('order_id')} status={payload.get('status')}")

    # Forward to dashboard on port 8000
    try:
        req = urllib.request.Request(
            "http://127.0.0.1:8000/api/upstox_postback",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=3) as resp:
            pass
    except Exception as fwd_exc:
        logger.debug(f"Could not forward postback to dashboard: {fwd_exc}")

    return JSONResponse({"status": "ok", "received": True})


if __name__ == "__main__":
    logger.info("Starting Upstox Listener on http://127.0.0.1:8500")
    uvicorn.run(app, host="127.0.0.1", port=8500, log_level="warning")
