"""Integration-level regression checks for the audit P1 repair paths."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest


pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_websocket_get_state_command_is_hard_capped(monkeypatch):
    import dashboard.server as server

    class Cache:
        def state(self):
            return {"blob": "x" * 1_200_000}

    class Socket:
        def __init__(self):
            self.message = ""

        async def send_text(self, message):
            self.message = message

        async def send_json(self, payload):
            self.message = json.dumps(payload, default=str)

    ws = Socket()
    monkeypatch.setattr(server, "scanner_ref", SimpleNamespace(dashboard_cache=Cache(), latest_results={}))
    await server.handle_command(ws, {"cmd": "get_state", "request_id": "integration"})
    assert len(ws.message.encode("utf-8")) <= server._WS_MAX_FRAME_BYTES
    payload = json.loads(ws.message)
    assert payload["type"] == "full_update"
    assert payload["data"]["ws_truncated"] is True


@pytest.mark.asyncio
async def test_websocket_broadcast_fans_out_concurrently(monkeypatch):
    import dashboard.server as server

    class Socket:
        async def send_text(self, message):
            await asyncio.sleep(0.15)

    clients = {Socket(), Socket(), Socket()}
    monkeypatch.setattr(server, "active_connections", clients)
    server.dashboard_state.last_sent_states.clear()
    started = time.perf_counter()
    await server.broadcast({"scan_count": 1})
    elapsed = time.perf_counter() - started
    assert elapsed < 0.35
    assert len(server.active_connections) == 3
    server.dashboard_state.last_sent_states.clear()


@pytest.mark.asyncio
async def test_long_session_websocket_keeps_useful_instrument_deltas(monkeypatch):
    import dashboard.server as server

    class Socket:
        def __init__(self):
            self.messages = []

        async def send_text(self, message):
            self.messages.append(json.loads(message))

    def state(scan_count, nifty_ltp, current_stop):
        candidates = [
            {
                "id": f"SIG-{i}",
                "status": "TRADE SIGNAL",
                "action": "ENTRY",
                "current_stop": current_stop if i == 0 else 95.0,
                "intelligence_score": 0.74,
                "market_intelligence_context": {"history": "x" * 4_000},
            }
            for i in range(150)
        ]
        return {
            "mode": "REAL",
            "scan_count": scan_count,
            "instruments": {
                name: {
                    "ltp": nifty_ltp if name == "NIFTY" else 50_000.0,
                    "trade_state": {"open_mtm": 250.0, "current_stop": current_stop},
                    "signals": [{"id": f"ACTIVE-{name}", "direction": "LONG"}],
                    "intelligence": {"aggregate": {"score": 0.74, "bias": "BULLISH"}},
                    "chart": {"5min": {"state": {"trailing_stop": current_stop}}},
                    "trade_candidates": candidates,
                }
                for name in ("NIFTY", "BANKNIFTY", "SENSEX", "MIDCPNIFTY")
            },
            "trades": {
                "open": [{"id": "OPEN-1", "current_stop": current_stop}],
                "closed": [],
                "signals": [],
                "summary": {"open_count": 1},
            },
        }

    ws = Socket()
    monkeypatch.setattr(server, "active_connections", {ws})
    server.dashboard_state.last_sent_states.clear()
    try:
        await server.broadcast(state(2_000, 22_000.0, 96.0))
        await server.broadcast(state(2_001, 22_001.0, 97.0))
    finally:
        server.dashboard_state.last_sent_states.clear()

    assert len(ws.messages) == 2
    full, delta = ws.messages
    assert full["type"] == "full_update"
    assert len(json.dumps(full, default=str).encode("utf-8")) <= server._WS_MAX_FRAME_BYTES
    assert full["data"].get("ws_payload_omitted") is not True
    assert set(full["data"]["instruments"]) == {"NIFTY", "BANKNIFTY", "SENSEX", "MIDCPNIFTY"}
    assert len(full["data"]["instruments"]["NIFTY"]["trade_candidates"]) == 20
    assert full["data"]["instruments"]["NIFTY"]["ltp"] == 22_000.0
    assert full["data"]["instruments"]["NIFTY"]["trade_state"]["current_stop"] == 96.0

    assert delta["type"] == "delta_update"
    nifty_delta = delta["data"]["instruments"]["NIFTY"]
    assert nifty_delta["ltp"] == 22_001.0
    assert nifty_delta["trade_state"]["current_stop"] == 97.0
    assert nifty_delta["chart"]["5min"]["state"]["trailing_stop"] == 97.0
    assert nifty_delta["trade_candidates"][0]["current_stop"] == 97.0


def test_missing_ledger_source_fails_closed(tmp_path):
    missing = tmp_path / "missing.db"
    env = dict(os.environ)
    completed = subprocess.run(
        [sys.executable, "-B", "scripts/audit_session_ledger.py", "--db", str(missing)],
        cwd=Path.cwd(),
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    payload = json.loads(completed.stdout)
    assert completed.returncode == 2
    assert payload["ok"] is False
    assert payload["error"] == "source_not_found"
