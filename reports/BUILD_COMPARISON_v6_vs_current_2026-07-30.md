# Build comparison — current (`rp46-3`) vs v6 — 2026-07-30

**Note on path:** `C:\Users\sagnik\Desktop\rp46-3-v6\rp46-3` is **empty**. The v6 build
actually lives at `C:\Users\sagnik\Desktop\rp46-3-v6 - Copy\rp46-3` (a further copy exists
at `rp46-v8\rp46-3`, ~1h newer). This comparison uses the "- Copy" tree.

## How the two builds relate

Both descend from **`87c60a1`** (my stop-out re-entry commit, 25 Jul). They then diverged:

| | Current (`rp46-3`) | v6 |
|---|---|---|
| HEAD | `fa062d7` | `87c60a1` |
| Nature of newer work | **committed** (2ce2b0f + my live fixes) | **uncommitted working tree** (~14,229 insertions / 70 files) |
| `trading/trade_manager.py` | 2,671 lines | **5,422** |
| `scanner.py` | 24,179 | **26,986** |
| `data/market_data.py` | 3,015 | **3,868** |
| `trading/api_rate_limiter.py` | 114 | **346** |
| `trading/broker.py` | 1,220 | **1,453** |
| tests | 132 files | **144** |

**Neither build is a superset of the other.**

---

## What v6 has that the current build LACKS

### 1. Broker order tagging (idempotency)
`_make_order_tag`, `_ensure_order_tag`, `_call_with_order_tag`, `broker.find_order_by_tag`.
A stronger duplicate-order defence than the locking approach currently in place: a tagged
order can be *looked up* at the broker after an ambiguous acknowledgement.

### 2. Real API rate limiting — directly fixes our #1 open issue
`SharedApiRateLimiter` with **per-endpoint minimum intervals** (order book / positions /
trade book capped at 1 req/s) and `AngelHistoricalRateLimiter` with `per_second = 3`,
matching AngelOne's documented history limit.

The current build has a single crude `TokenBucket`. This is the root cause of the
**189 rate-limit hits → 279 Yahoo fallbacks → volume = 0** chain diagnosed on 2026-07-30.

### 3. Event-driven order updates (removes the naked-stop window)
`angel_order_update_ws_enabled`, `angel_postback_enabled/secret`,
`ingest_broker_order_update`, `_wake_position_sync`.
Fills arrive by websocket/postback instead of a 5–30s reconciliation poll — the direct fix
for the "position filled but SL-M not yet armed" gap.

### 4. Execution leasing / concurrency safety
`_acquire_real_execution_lease`, `_release_real_execution_lease`, `_execution_serialized`,
`_protection_lock_for` (per-symbol locks vs one global lock).

### 5. Session + connection hardening
`_refresh_expired_session`, `configure_smartapi_transport_deadline`,
`angel_ws_max_market_connections`, `angel_ws_max_subscriptions_per_session`,
`angel_static_ip_registered/required`, `angel_profile_validation_*`.

### 6. Broker introspection
`get_trade_book`, `get_individual_order_details`, `find_order_by_tag`.

### 7. Misc
Persisted stop-out re-entry state (`_persist_stop_out_reentry_state` — fixes a real defect
in *my* feature, see below), `early_breakout_runner_*`, disk-pressure entry blocking,
`_background_io_worker`, exit-intent latching (`latch_exit_intent`,
`confirm_latched_manual_exit`).

---

## What the CURRENT build has that v6 lacks

These are my post-`87c60a1` live fixes; v6 predates all of them:

| Fix | v6 status |
|---|---|
| Option-chain staleness (`option_chain_intelligence_max_age_seconds`) — the OI/PCR outage | **MISSING** |
| Volume preservation (`_restore_zeroed_volume`) — Yahoo zeros wiping AngelOne volume | **MISSING** |
| Runtime watchdog (dead scanner / auto-mode revert / Fyers expiry) | **MISSING** |
| History pacing settings | **MISSING** |

v6 *does* already contain `update_fyers_token_async` and the `on_notification` parameter,
and does **not** carry the `pd`-shadowing bug (that was introduced later by `2ce2b0f`).

---

## v6's own live audit (2026-07-29) — verdict **NO-GO**

`FULL_SYSTEM_LIVE_E2E_AUDIT_2026-07-29.md` lists 10 critical signal defects and 7 P1
order/stop defects. **Two were verified against the CURRENT build and are present here:**

### 🔴 A. The dashboard "Execute" button places a GHOST trade
`scanner.py:1315` `execute_manual_signal` → `open_trade(..., is_ghost=True)` at
**scanner.py:1388**, then sets `status = "ORDER PENDING"`, logs *"Manual dashboard ENTRY
submitted"* and returns `{"status": "submitted"}`.

**No real broker order is placed**, yet the UI reports submission. The *toast* Execute
(`make_executor`) passes `is_ghost=False` and IS real — so the two manual surfaces behave
completely differently. Anyone running supervised REAL/manual and clicking the dashboard
Execute is placing virtual trades.

### 🔴 B. Closed ghost P&L contaminates the real daily circuit breaker
`engine/risk_manager.py:194-205` sums `t.pnl` for every closed trade **with no `is_ghost`
filter**, while the open-trades loop at `:209` *does* exclude ghosts.

Virtual profits can therefore mask real losses (breaker fails to fire) and virtual losses
can halt real trading. **A and B compound**: ghost trades created by A close, and their
virtual P&L then drives the real breaker.

### Other v6 P1s worth checking here
- Hung SmartAPI call can block the whole account lifecycle (`SmartConnect(api_key=...)` is
  constructed with no timeout at `data/market_data.py:1154`).
- `LONG_ONLY` / `SHORT_ONLY` (`strategy_direction_mode`) is declared but never enforced.
- 15m candidates graded with 5m intelligence/regime.
- My stop-out re-entry per-index cap is ineffective (counter neither daily-reset nor
  persisted, and only checked inside the cooldown branch).
- Regime smoothing counts `detect()` calls rather than distinct candles.

---

## Recommendation

v6 is the more advanced **engineering** line (order tagging, real rate limiting,
event-driven fills, execution leasing) and it fixes the exact issues still open here.
The current line holds four live bug fixes v6 has never seen.

Neither is ready for unattended auto: v6's own audit says **NO-GO**, and the two defects
verified above are live in this build today.

Suggested order:
1. **Fix A and B here immediately** — they affect the supervised manual workflow already in use.
2. Decide a single canonical line rather than maintaining three divergent trees
   (`rp46-3`, `rp46-3-v6 - Copy`, `rp46-v8`). Porting v6's ~14k lines by hand is not
   sensible; merging my four fixes onto the v6 tree is far smaller.
3. Re-audit once consolidated.
