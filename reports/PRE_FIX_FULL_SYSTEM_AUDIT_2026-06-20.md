# Pre-fix full-system audit

**Audit date:** 2026-06-20 (IST)  
**Audited revision at start:** `1420e8f`  
**Scope:** restored pre-repair baseline; analysis and report only. No application source or settings change was made as part of this audit.

## Executive assessment

The restored baseline can start and retrieve market data, but it is **not release-ready**. The most immediate issues are a security boundary that can be bypassed by any page script running locally, exposed credentials in connection logging, serial WebSocket fanout that can delay scanner work, and a regression suite whose tests no longer match the restored implementation.

The one-day historical snapshot is not enough to evaluate trading performance: it completed with four total trades and ₹21.70 daily P&L while the market was closed. There is no frozen data/configuration manifest, so a repeated multi-day result cannot currently be treated as deterministic evidence.

## What was exercised

| Area | Result |
| --- | --- |
| Full repository `pytest` collection | **Blocked**: root-level `test_ws.py` executes a live request at import time and aborts collection on HTTP 401. |
| Maintained `tests/` suite | **231 passed, 20 failed, 4 warnings** in 10.56 seconds. |
| Temporary local instance | Started in `HISTORICAL` mode on `127.0.0.1:8028`; stopped and removed after auditing. |
| Dashboard/API/WebSocket | Four indices loaded; read endpoints and WebSocket behavior verified. |
| Seven-day market/provider audit | Both broker connections available; 4/4 historical feeds, 4/4 Fyers volume checks, 4/4 quotes, and 4/4 option chains succeeded; no timeout or critical-price error. |
| Read-path load probe | Twelve concurrent requests per endpoint: all HTTP 200. State median 24.61 ms / max 31.12 ms; status 10.66 / 34.20 ms; diagnostics 5.25 / 6.79 ms; trades 7.59 / 9.95 ms. |

The direct browser UI pass could not be run because both available browser-control runtimes were blocked by the local sandbox. Endpoint and WebSocket checks covered the same dashboard service surface without modifying it.

## Findings

### P0 — dashboard token is exposed to JavaScript and read APIs are open

Verified end-to-end:

- `GET /api/client_config` returned a dashboard token to an unauthenticated browser client.
- That token successfully opened the WebSocket and retrieved a `full_update`.
- Unauthenticated `GET /api/status`, `/api/state`, `/api/diagnostics`, and `/api/trades` all returned HTTP 200.

This defeats the intended command boundary if any dashboard JavaScript is compromised, and it exposes operational/trade data to local untrusted processes. The local binding reduces external reachability but is not a substitute for a browser-safe session boundary.

### P0 — broker API key is written to application logs

`MarketDataProvider.connect()` formats the AngelOne API key in an INFO log message. This is a direct credential disclosure path. The audit deliberately redacted the value and deleted temporary raw runtime logs.

### P1 — XSS and supply-chain exposure in dashboard rendering

The dashboard has many dynamic `innerHTML` assignments and inline `onclick` handlers, including trade-table and event-log rendering. Dynamic dashboard data can therefore reach HTML/attribute contexts without a single enforced escaping layer.

The base page also loads Google Fonts from a third party and returns no Content-Security-Policy, `X-Content-Type-Options`, or `Referrer-Policy` header. The live probe confirmed CSP was absent on `/`, `/api/state`, `/api/diagnostics`, and `/api/trades`.

### P1 — scanner progress is coupled to sequential WebSocket delivery

`Scanner` awaits its update callback. `broadcast()` then holds the dashboard state lock and sends to each WebSocket client sequentially, without a per-client timeout. One slow or stalled client can therefore delay all following clients and scanner progress. The measured read API latency is healthy for a zero-client run, but it does not prove fanout remains healthy under a slow client.

### P1 — dashboard command validation is partial and ad hoc

The WebSocket validator is a hand-written allow-list with checks only for selected fields. It is not a strict request schema: unknown fields are accepted, most command-specific types/ranges are not centrally validated, and the scanner receives raw values. The malformed `set_power` state was correctly rejected, and malformed `bars`/`limit` query values returned framework-level HTTP 422; these are useful partial protections, not full command validation.

### P1 — regression suite cannot certify this baseline

`pytest -q` cannot collect because `test_ws.py` immediately calls a localhost endpoint during import. The maintained test suite then reports 20 failures:

- chart warm-up tests expect `_prepare_live_signal_data`, which is absent;
- manual exit/intelligence tests expect `_soft_intel_profit_protect_floor` and `_get_timeframe_intel`, which are absent;
- market-location and signal-quality tests expect settings such as `live_15min_min_confidence` and option-IV limits that are absent;
- option/choppy live-gate behavior differs from the tests' asserted policy;
- two repaint tests cannot run because `pytest-asyncio` is missing/not loaded (`asyncio_mode` is an unknown config option).

This is a test/code contract mismatch after rollback. It prevents using the current suite as a reliable safety gate until the expected baseline is chosen and tests are aligned to it.

### P2 — data-source resilience is only partially observable

The provider audit was positive: AngelOne, Fyers, and Yahoo were all reported available, and all four indices returned seven-day candles, valid volume, quotes, and option chains. However, the visible dashboard instrument rows had no price, quote age, signal, or regime values in the closed-session snapshot, despite recording `fyers_rest` as quote source. The state payload lacks a clear operator-facing reason such as “market closed”, freshness age, retry state, or next retry time.

The pre-fix baseline has some caching and fallback code, but no verified global request-spacing/backoff telemetry contract. It also has no frozen candle/options manifest for reproducible historical runs.

### P2 — historical result interpretation remains weak

The completed default one-day simulation reported four total trades, while the default response page returned only three closed rows. Pagination explains the difference, but a headline view needs explicit total/page context to prevent operators reading a partial list as the portfolio total. The observed ₹21.70 P&L is a single closed-session snapshot, not a validated strategy result.

## Runtime evidence

| Measure | Observed value |
| --- | --- |
| Historical scanner | running; 131 scans observed; completed one-day simulation |
| Loaded indices | NIFTY, BANKNIFTY, SENSEX, MIDCPNIFTY |
| Provider audit | 4 historical feeds, 4 volume checks, 4 quotes, 4 option chains succeeded |
| `/api/state` payload | about 95 KB; unauthenticated HTTP 200 |
| State read latency | 24.61 ms median, 31.12 ms max across 12 concurrent reads |
| WebSocket without token | rejected |
| WebSocket with token obtained from client config | accepted; returned `full_update` |
| CSP on sampled responses | absent |

## Audit limitations and workspace integrity

- This audit did not place orders, mutate dashboard settings, request recalibration, reset P&L, or run real-mode trading.
- The provider check used live data reads only.
- Temporary service logs were deleted after redacting the outcome because the baseline contains a credential-logging path.
- After the tests completed, uncommitted edits appeared in `dashboard/server.py`, `scanner.py`, and `pytest.ini`. They were not made or reverted by this audit and were not included in the tested revision; they must be reviewed separately before relying on a later run.

## Recommended decision order (no changes applied)

1. Re-establish the security boundary: safe credential logging, a loopback-only HttpOnly session, authenticated read APIs, DOM-safe rendering, and CSP.
2. Decide which code revision is authoritative, then align/remove incompatible tests and restore async-test support so the suite can serve as a gate.
3. Decouple and time-bound WebSocket fanout before evaluating scanner throughput under multiple clients.
4. Add deterministic data/config manifests and source-health/backoff telemetry before using historical P&L for strategy decisions.
5. Re-run at least two frozen, non-overlapping historical windows before making any performance claim.
