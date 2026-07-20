"""Regression: Fyers WebSocket callbacks must accept the SDK's arguments.

The Fyers SDK invokes on_close(message). A previous zero-arg on_close() raised
"takes 0 positional arguments but 1 was given", which crashed BEFORE the cleanup
body ran — so _fyers_ws_connected never reset and the app reported the broker
offline. This captures the real callback and asserts it survives the SDK call.
"""
import data.market_data as md


def _capture_callbacks(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("UT1_FYERS_LOG_DIR", "logs/fyers")
    monkeypatch.setattr(md, "FYERS_LOGS_READY", False)
    captured = {}

    class _FakeSocket:
        def __init__(self, **kw):
            captured.update(kw)
            raise RuntimeError("stop-before-thread")  # abort: no network / no runner thread

    monkeypatch.setattr(md.data_ws, "FyersDataSocket", _FakeSocket)
    prov = md.MarketDataProvider.__new__(md.MarketDataProvider)
    try:
        prov.start_fyers_websocket("dummy-token")
    except Exception:
        pass
    return prov, captured


def test_fyers_logs_are_routed_under_logs_and_root_logs_are_archived(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("UT1_FYERS_LOG_DIR", "logs/fyers")
    monkeypatch.setenv("UT1_FYERS_LOG_ROTATE_BYTES", "4")
    monkeypatch.setattr(md, "FYERS_LOGS_READY", False)

    (tmp_path / "fyersApi.log").write_text("root", encoding="ascii")
    managed_log = tmp_path / "logs" / "fyers" / "fyersRequests.log"
    managed_log.parent.mkdir(parents=True)
    managed_log.write_text("oversized", encoding="ascii")

    log_path = md._fyers_sdk_log_path().replace("\\", "/")

    assert log_path.endswith("logs/fyers/")
    assert not (tmp_path / "fyersApi.log").exists()
    assert list((tmp_path / "logs" / "fyers" / "root_archive").glob("fyersApi.*.log"))
    assert not managed_log.exists()
    assert list((tmp_path / "logs" / "fyers" / "archive").glob("fyersRequests.*.log"))


def test_fyers_on_close_accepts_sdk_argument_and_resets_state(monkeypatch, tmp_path):
    prov, captured = _capture_callbacks(monkeypatch, tmp_path)
    on_close = captured.get("on_close")
    assert callable(on_close)

    prov._fyers_ws_connected = True
    prov._fyers_ws_connected_since = 123.0

    on_close("closed by server: 504")          # SDK-style 1-arg call must not raise
    assert prov._fyers_ws_connected is False    # cleanup ran
    assert prov._fyers_ws_connected_since == 0.0

    on_close()                                  # defensive: 0-arg call also safe


def test_fyers_socket_callbacks_have_compatible_arity(monkeypatch, tmp_path):
    """on_message/on_error/on_close are passed into the constructor and are
    called by the SDK with one argument — all must accept it."""
    _prov, captured = _capture_callbacks(monkeypatch, tmp_path)
    for name in ("on_message", "on_error", "on_close"):
        cb = captured.get(name)
        assert callable(cb), f"{name} not passed to socket"
        # must not raise TypeError on a single positional arg
        try:
            cb(None)
        except TypeError as exc:  # pragma: no cover - the bug we are guarding against
            raise AssertionError(f"{name} rejects the SDK argument: {exc}")
        except Exception:
            pass  # other runtime errors from a None payload are fine here


def test_fyers_ws_tick_stall_detection_and_faster_recovery():
    """Silent-disconnect detection must fire when ticks stall past the threshold,
    stay quiet within it, and (the 2026-07-06 improvement) catch a 50s stall at
    the new 45s threshold that the old hardcoded 60s would have missed."""
    f = md.MarketDataProvider._fyers_ws_tick_stalled
    now = 1000.0

    # stale-tick cases
    assert f(950.0, 800.0, now, 45.0) is True    # 50s stall -> detected at 45s
    assert f(950.0, 800.0, now, 60.0) is False   # ...but the OLD 60s missed it
    assert f(990.0, 800.0, now, 45.0) is False   # 10s fresh -> fine

    # never-a-tick-after-open cases
    assert f(0.0, 900.0, now, 45.0) is True       # connected 100s, no tick
    assert f(0.0, 970.0, now, 45.0) is False      # connected 30s, still within grace

    # a bad/missing threshold must fall back to the conservative 60s, not crash
    assert f(950.0, 800.0, now, None) is False    # 50s < 60s fallback
    assert f(0.0, 800.0, now, "bad") is True       # 200s no-tick still trips fallback


def test_fyers_ws_stall_threshold_setting_is_live_calm_safe():
    """The threshold is settings-driven and defaults to a calm-session-safe 90s."""
    from config.settings import get_settings
    assert float(getattr(get_settings(), "fyers_ws_silent_disconnect_seconds", 90.0)) == 90.0


def test_close_ws_socket_uses_real_sdk_method():
    """Regression (2026-07-08): FyersDataSocket/SmartWebSocketV2 have NO .close()
    — both expose close_connection(). The old teardown raised AttributeError on
    every silent-disconnect/auth/restart (20x that session), so stale sockets
    were never closed before reconnecting; Angel's hasattr-guarded variant was a
    silent no-op. The helper must prefer close_connection() and never raise."""

    calls = []

    class FyersLikeSocket:  # only close_connection, like the real SDKs
        def close_connection(self):
            calls.append("close_connection")

    class LegacySocket:  # hypothetical SDK variant with plain close()
        def close(self):
            calls.append("close")

    class NoCloseSocket:  # nothing closeable — must not raise
        pass

    class RaisingSocket:
        def close_connection(self):
            raise RuntimeError("already dead")

    f = md.MarketDataProvider._close_ws_socket
    assert f(FyersLikeSocket(), "test") is True and calls[-1] == "close_connection"
    assert f(LegacySocket(), "test") is True and calls[-1] == "close"
    assert f(NoCloseSocket(), "test") is False
    assert f(None, "test") is False
    assert f(RaisingSocket(), "test") is False  # tolerated, no raise


def test_no_direct_broken_close_calls_remain():
    """No teardown path may call the nonexistent .close() directly again."""
    src = open("data/market_data.py", encoding="utf-8").read()
    assert "_fyers_ws.close()" not in src
    assert "sws.close()" not in src


def _capture_on_open(monkeypatch, tmp_path):
    """Capture the real on_open callback without spawning the runner thread."""
    import threading as _threading
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("UT1_FYERS_LOG_DIR", "logs/fyers")
    monkeypatch.setattr(md, "FYERS_LOGS_READY", False)
    captured = {}

    class _FakeSocket:
        def __init__(self, **kw):
            captured.update(kw)

        def __setattr__(self, key, value):
            if key == "on_open":
                captured["on_open"] = value
            object.__setattr__(self, key, value)

        def connect(self):
            raise RuntimeError("stop-runner")

        def subscribe(self, **kw):
            captured["subscribed"] = True

    class _NoThread:
        def __init__(self, *a, **k):
            self.daemon = False

        def start(self):
            pass

    monkeypatch.setattr(md.data_ws, "FyersDataSocket", _FakeSocket)
    monkeypatch.setattr(_threading, "Thread", _NoThread)
    prov = md.MarketDataProvider.__new__(md.MarketDataProvider)
    prov.start_fyers_websocket("dummy-token")
    return prov, captured


def test_on_open_resets_stall_clock_on_reconnect(monkeypatch, tmp_path):
    """Regression (2026-07-09 reconnect storm): on_open must zero
    _last_fyers_ws_tick so a reconnected socket gets a fresh grace window
    instead of being instantly re-declared 'silent disconnect' by the stale
    pre-disconnect tick timestamp."""
    import time
    prov, captured = _capture_on_open(monkeypatch, tmp_path)
    on_open = captured.get("on_open")
    assert callable(on_open)

    prov._last_fyers_ws_tick = time.time() - 100.0   # stale tick from prior connection
    on_open()
    assert prov._last_fyers_ws_tick == 0.0            # the fix
    assert prov._fyers_ws_connected is True
    assert prov._fyers_ws_connected_since > 0


def test_stall_detector_does_not_false_fire_right_after_reconnect():
    """The exact storm mechanics: a stale last_tick trips the detector; zeroing
    it on open does not — and a genuine post-reconnect silence still trips."""
    f = md.MarketDataProvider._fyers_ws_tick_stalled
    now = 10_000.0
    # BUG shape: stale tick (100s old) + fresh connected_since -> fires instantly
    assert f(now - 100.0, now, now, 45.0) is True
    # FIX shape: last_tick zeroed on open + fresh connected_since -> grace window
    assert f(0.0, now, now, 45.0) is False
    # ...but a real 46s of silence after reconnect still trips (no_tick_after_open)
    assert f(0.0, now - 46.0, now, 45.0) is True
