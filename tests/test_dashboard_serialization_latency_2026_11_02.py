"""Keep dashboard serialization work proportional to the data actually served."""
import asyncio
import copy
import inspect
import json
from collections import deque
from datetime import datetime
from decimal import Decimal
from types import SimpleNamespace as NS

import numpy as np
import pytest

import dashboard.server as server


def legacy_serializable(obj):
    if hasattr(obj, "item") and callable(getattr(obj, "item", None)):
        try:
            return legacy_serializable(obj.item())
        except Exception:
            pass
    if isinstance(obj, (int, str, bool, type(None))):
        return obj
    if isinstance(obj, Decimal):
        return float(obj)
    if isinstance(obj, float):
        return 0 if not np.isfinite(obj) else obj
    if isinstance(obj, dict):
        return {str(k): legacy_serializable(v) for k, v in list(obj.items())}
    if isinstance(obj, (list, tuple, set, deque)):
        return [legacy_serializable(v) for v in list(obj)]
    if isinstance(obj, datetime):
        return obj.isoformat()
    return str(obj)


def test_builtin_fast_path_preserves_scalar_and_container_contract():
    class Scalar:
        def item(self):
            return np.float64(3.25)

    class ItemDictionary(dict):
        def item(self):
            return "custom item wins"

    payload = {1: [None, True, 2, 2.5, float("nan"), float("inf"), float("-inf"),
                   np.float64(4.25), np.float32(5.5), np.int64(6), np.bool_(True),
                   Decimal("7.125"), datetime(2026, 11, 2, 12, 30), Scalar(), ItemDictionary(x=1)],
               "containers": (deque([1, 2]), {"a", "b"})}
    assert server.make_serializable(payload) == legacy_serializable(payload)
    safe = server.make_serializable(payload)
    safe["1"].append("consumer mutation")
    assert len(payload[1]) + 1 == len(safe["1"])
    json.dumps(safe, allow_nan=False)


@pytest.mark.parametrize("number", [Decimal("NaN"), Decimal("Infinity"), Decimal("-Infinity")])
def test_single_conversion_keeps_nonfinite_decimal_wire_value_zero(number):
    # API state previously converted twice, normalizing Decimal -> float -> 0.
    assert server.make_serializable({"price": number}) == {"price": 0}


@pytest.mark.parametrize("source", ["cache", "latest", "fresh"])
@pytest.mark.parametrize("charts_enabled", [True, False])
def test_state_windows_before_one_conversion_and_detaches_lab(monkeypatch, source, charts_enabled):
    rows = [{"id": f"row-{i}", "price": np.float64(10.25)} for i in range(200)]
    payload = {"mode": "REAL", "instruments": {"NIFTY": {"chart": {
        "5min": {"candles": [{"id": "discarded-chart", "close": 10.}], "state": {"stop": 9.}}
    }}}, "trades": {"open": [], "closed": rows, "signals": [], "summary": {"total_trades": 200}},
        "engine_lab": {"rows": [{"engine": "ut_ema", "active": False}]}}
    original_payload = copy.deepcopy(payload)
    scanner = NS(mode="REAL", chart_stream_enabled=charts_enabled,
                 get_latest_results=lambda **kwargs: payload)

    def sync(state, summary):
        state["engine_lab"]["rows"][0]["active"] = True
        state["engine_lab"]["value"] = np.float64(2.5)
    scanner._sync_engine_lab_active_row = sync
    if source == "cache":
        scanner.dashboard_cache = NS(state=lambda: payload)
    monkeypatch.setattr(server, "scanner_ref", scanner)
    monkeypatch.setattr(server.dashboard_state, "latest_safe_state", payload if source == "latest" else {})
    monkeypatch.setattr(server, "_dashboard_heartbeat", lambda: {"alive": True})
    original = server.make_serializable
    visits = []

    def counted(value):
        if isinstance(value, dict) and "id" in value:
            visits.append(value["id"])
        return original(value)
    monkeypatch.setattr(server, "make_serializable", counted)
    actual = json.loads(asyncio.run(server.api_state()).body)

    expected = legacy_serializable(payload)
    if source == "cache" and not charts_enabled:
        expected["instruments"]["NIFTY"]["chart"] = {}
    sync(expected, expected["trades"]["summary"])
    expected["dashboard_heartbeat"] = {"alive": True}
    expected = legacy_serializable(server._lighten_state_payload(expected))
    assert actual == expected
    assert payload == original_payload
    assert visits == [f"row-{i}" for i in range(server._DASHBOARD_STATE_TRADE_LIMIT)]


def reference_cap(payload):
    """Run unchanged rescue stages with their previous full-frame size checks."""
    source = inspect.getsource(server._cap_ws_payload)
    stages = source[source.index("    data = safe_data"):]
    namespace = dict(vars(server))
    exec("def old_cap(safe_data):\n"
         "    def frame_size(candidate):\n"
         "        return _ws_frame_size(candidate)\n" + stages, namespace)
    return namespace["old_cap"](payload)


@pytest.mark.parametrize("context_bytes, extra_bytes", [(20, 0), (4000, 0), (25000, 0), (4000, 950000)])
def test_capping_preserves_stage_choice_and_exact_cached_frame(monkeypatch, context_bytes, extra_bytes):
    monkeypatch.setattr(server, "orjson", None)
    candidates = [{"id": f"SIG-{i}", "current_stop": 99., "status": "TRADE SIGNAL",
                   "market_intelligence_context": {"history": "x" * context_bytes}} for i in range(150)]
    payload = {"mode": "REAL", "scan_count": 3, "diagnostics": {"note": "é" * extra_bytes},
               "instruments": {name: {"ltp": 22000., "trade_candidates": candidates,
                                      "signals": [{"id": name}], "trade_state": {"current_stop": 99.}}
                               for name in ("NIFTY", "BANKNIFTY", "SENSEX", "MIDCPNIFTY")},
               "trades": {"open": [{"id": "open"}], "closed": [], "signals": [], "summary": {}}}
    cache = {}
    expected = reference_cap(payload)
    actual = server._cap_ws_payload(payload, frame_cache=cache)
    assert actual == expected
    encoded = server._frame_bytes({"type": "full_update", "data": actual})
    assert len(encoded) <= server._WS_MAX_FRAME_BYTES
    assert cache["payload"] is actual
    assert cache["encoded"] == encoded
    assert len(payload["instruments"]["NIFTY"]["trade_candidates"]) == 150


def test_rescue_stages_reuse_large_unchanged_field_encoding(monkeypatch):
    original = server._frame_bytes
    encoded_bytes = []
    def counted(value):
        result = original(value)
        encoded_bytes.append(len(result))
        return result
    monkeypatch.setattr(server, "_frame_bytes", counted)
    blob = "x" * 1_200_000
    actual = server._cap_ws_payload({"blob": blob, "trades": {"summary": {}}})
    assert actual["ws_payload_omitted"] is True
    # Previously three complete oversized frames were encoded (3.6 MB).
    assert sum(encoded_bytes) < len(blob) * 2.1


def test_failed_client_does_not_change_other_clients_delta_baseline(monkeypatch):
    class Socket:
        def __init__(self, fail=False):
            self.fail, self.messages = fail, []
        async def send_text(self, message):
            if self.fail:
                raise ConnectionError("closed")
            self.messages.append(json.loads(message))
        async def close(self, **kwargs):
            pass
    good, failed = Socket(), Socket(True)
    monkeypatch.setattr(server, "active_connections", {good, failed})
    monkeypatch.setattr(server.dashboard_state, "last_sent_states", {})
    monkeypatch.setattr(server, "_dashboard_heartbeat", lambda: {})
    async def exercise():
        await server._deliver_broadcast({"mode": "REAL", "scan_count": 1})
        await server._deliver_broadcast({"mode": "REAL", "scan_count": 2})
    asyncio.run(exercise())
    assert [frame["type"] for frame in good.messages] == ["full_update", "delta_update"]
    assert good.messages[-1]["data"] == {"scan_count": 2}
    assert failed not in server.active_connections
    assert server.dashboard_state.last_sent_states[id(good)]["scan_count"] == 2
