# Dashboard Port Migration - 2026-06-19

## Summary

The active localhost dashboard has been consolidated on `http://127.0.0.1:8001/`.

The migration updates runtime configuration, launch scripts, service messaging, dashboard CORS/proxy fallbacks, notification links, Fyers redirect defaults, audit helpers, browser-ready launch behavior, and websocket verification tooling.

## Safeguards Added

- `scripts/open_dashboard_when_ready.ps1` waits for `/api/state` to return live dashboard state before opening the browser.
- `tests/test_dashboard_port_config.py` verifies the active dashboard port and blocks retired dashboard URLs from active config/launcher files.

## Verification

- `http://127.0.0.1:8001/` returned HTTP 200.
- `/api/state` returned live instruments and scanner heartbeat.
- websocket verification returned a `full_update` payload.
- retired dashboard listeners were confirmed inactive.
- in-app browser loaded `http://127.0.0.1:8001/` with connected status, populated prices, scan count, and chart canvases.
- Focused tests passed: `23 passed`.
