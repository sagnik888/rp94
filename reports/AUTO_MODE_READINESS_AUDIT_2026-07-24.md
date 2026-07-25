# Auto-Mode Trading Readiness Audit — UT1 Index Trading System

**Audit date:** 2026-07-24 (Friday, ~01:00 IST)
**Workspace:** `C:\Users\sagnik\Desktop\rp46-3` @ `79c1050`
**Scope:** End-to-end system audit; readiness for **unattended AUTO-mode live trading**
**Nature:** Read-only. No production code was modified.

---

## 1. Bottom line

> **NOT READY for unattended auto-mode trading.**
> Two independently-confirmed **BLOCKER** defects in the protective-stop (SL-M) path can leave a
> **naked, reversed, unprotected position** with real money at risk, with no human in the loop.

Two separate things must be understood, because they are often conflated:

| Question | Answer |
|---|---|
| Will the system auto-trade by itself on the next session? | **No.** It arms REAL/**manual** only. Zero automatic orders. |
| If a human turns Auto Mode ON, is it safe to leave unattended? | **No.** Fix the two BLOCKERs first. |

### Calendar reality
"Tomorrow" = **Saturday 2026-07-25 → market holiday.** The next live session is
**Monday 2026-07-27**. Nothing trades tomorrow regardless of configuration.

---

## 2. The central question: will it auto-trade on its own?

**No — and this is by deliberate design.** Confirmed by direct code trace:

- At `AUTO_REAL_MODE_PREMARKET_SWITCH_TIME` (`09:00:00`) the premarket worker switches
  `HISTORICAL → REAL` but **explicitly forces `auto_mode=False`**, and re-forces it defensively
  three times — [main.py:379,386,389-398](main.py:389). Its own docstring: *"This intentionally
  never enables Auto Mode."*
- `scanner.auto_mode` initialises `False` — [scanner.py:188](scanner.py:188). The boot
  `configure()` call never passes it — [main.py:616-621](main.py:616).
- **`auto_mode` is never persisted.** It is not a `Settings` field, so `save_to_env()` cannot
  write it, and there is no `AUTO_MODE` key in `.env`. Verified by grep of `config/settings.py`
  and `.env`.
- With `auto_mode=False`, the entry gate at [scanner.py:21979](scanner.py:21979)
  (`if not self.auto_mode and not is_warmup`) latches every signal as a **manual candidate** and
  `continue`s — literally commented `# Don't take the trade in manual mode`. Exits are gated the
  same way ([scanner.py:22710](scanner.py:22710), [scanner.py:23587](scanner.py:23587)) and only
  latch `pending_exit` + notify.

**Consequence:** Auto Mode requires a **deliberate human dashboard toggle every single session**,
and it **never survives a restart**. A mid-session crash silently reverts to manual — signals will
be accepted and exits latched as *pending*, but **nothing will fire** until a human notices.

The auto path itself **is** correctly wired: with `auto_mode=True` in REAL,
[scanner.py:22124](scanner.py:22124) calls `trades.open_trade(..., exec_type="A")`, which places a
live broker order and arms an exchange-side SL-M. The machinery works; the defects are in *how* the
stop is armed.

---

## 3. BLOCKERS — must fix before any unattended auto trading

### BLOCKER-1 — `place_protective_stop` swallows ambiguous acks → duplicate SL-M
**`trading/broker.py:511-514`** · *Confirmed by direct code read*

The entry and stop paths handle identical failures **asymmetrically**:

```python
# place_order (broker.py:389-393)  — CORRECT
except Exception as api_err:
    raise BrokerOrderAmbiguous(...)          # caller must NOT retry

# place_protective_stop (broker.py:511-514) — DEFECT
except Exception as exc:
    logger.error(f"Protective stop placement failed for {symbol}: {exc}")
    return None                               # indistinguishable from clean reject
```

A `STOPLOSS_MARKET` order can **reach and be accepted by the exchange** while the HTTP response
times out — precisely the failure class the last commit addressed (AngelOne timeouts / 429s /
websocket drops). `place_protective_stop` returns `None`, which the caller reads as "no stop
exists", so it **places the SL-M again**.

**Impact:** Two identical SL-M orders rest at the exchange for the same lots. On trigger **both
fire** — the first flattens the position, the second executes against nothing and opens an
**equal-and-opposite naked leg** (naked short option / short future) **with no protective stop**.
That is unbounded loss with no human watching.

**Fix:** Raise `BrokerOrderAmbiguous` like `place_order`, and have `ensure_broker_protection` treat
ambiguous as *"do not place again"* — instead scan the order book for a working STOPLOSS on the
symbol before placing.

---

### BLOCKER-2 — `ensure_broker_protection` place-path is unsynchronised & non-idempotent
**`trading/trade_manager.py:770-853`** · *Confirmed by direct code read*

The function holds **no lock** (`self._lock` is used 24× elsewhere in the file — the omission is an
inconsistency, not a design choice), and the throttle

```python
if now - last_sync < 2.0: return True     # line ~818
```

exists **only inside the `if existing:` modify branch**. When `protective_order_id == ""` the code
runs straight to `place_protective_stop` with **no idempotency check and no throttle**.

It is driven concurrently by **two real threads**:

| Thread | Call site |
|---|---|
| `PositionSync` daemon (`threading.Thread`, [trade_manager.py:465](trading/trade_manager.py:465)) | `reconcile_broker_positions` → [:1073](trading/trade_manager.py:1073), [:1092](trading/trade_manager.py:1092) |
| `ScannerEventLoop` thread | `update_trade` → [:1720](trading/trade_manager.py:1720), on every price tick |

Both read `existing = trade.protective_order_id` at line 794. Once reconcile sets
`broker_quantity` but before either records the new order id, **both observe `""` and both place an
SL-M.** Ticks arrive sub-second, so this window opens on essentially every new fill.

**Impact:** Same naked-reversed-leg outcome as BLOCKER-1, but reachable with a **perfectly healthy
broker** — plain thread interleaving is sufficient.

**Fix:** Serialise protection changes under `self._lock` (or a per-`trade.id` lock), re-check
`protective_order_id` after acquiring it, and query the broker order book for an existing working
STOPLOSS before placing.

---

## 4. HIGH-severity risks

### HIGH-1 — Naked-position window: SL-M armed only after the ~5 s reconcile poll
**`trading/trade_manager.py:1073`, `:1505-1594`**

Entry placement (`place_bg`) sets `execution_status='ENTRY_SUBMITTED'` and **never places a stop**.
Protection is first armed inside `reconcile_broker_positions` when the broker *position book* shows
the fill; `broker_quantity` — the precondition for `update_trade` to arm protection — is itself only
set inside reconcile. The sync loop runs at `position_sync_interval_seconds` (**default 5.0 s**).

From fill until the next reconcile poll (**up to ~5 s + AngelOne position-book propagation lag**)
the live position has **no exchange-side stop**, protected only by the in-process software stop. If
the loop stalls or the socket drops in that window, the position is naked.

**Fix:** Place the SL-M synchronously on confirmed fill (or use a bracket/cover order so the stop is
exchange-resident at entry).

### HIGH-2 — Enabling Auto Mode performs **no** broker/warmup/feed re-validation
**`scanner.py:3229-3285`** · *Confirmed*

`validate_real_mode_readiness()` is called **only** on the `HISTORICAL → REAL` transition
(`if requested_mode == "REAL" and current_mode != "REAL"`). Because the premarket worker already put
the system in REAL at 09:00, an operator toggling Auto Mode later sends `mode=None` → `requested_mode`
is empty → **that readiness block is skipped entirely.** The auto_mode gate (3271-3285) then requires
only a client-supplied confirmation string (`YES`/`REAL`).

**Impact:** Auto-execution can be armed at 09:16 while the AngelOne socket is silently disconnected,
warmup is incomplete, or the feed is stale — nothing re-checks.

**Fix:** When `auto_mode` is being enabled and `(requested_mode or current_mode) == "REAL"`, re-run
`validate_real_mode_readiness()` and assert broker connectivity + warmup-complete + feed-fresh.

### HIGH-3 — Fyers access token is expired for the next session
`fyers_token.json` (generated 2026-07-23 08:51):

| Token | Expiry (IST) |
|---|---|
| `access_token` | **2026-07-24 06:00:00** — before market open |
| `refresh_token` | 2026-08-07 06:00:00 |

The access token is **already dead** for any upcoming session. Auto-refresh exists
(`data/market_data.py:864`, `grant_type=refresh_token`) and uses `fyers_password` as the PIN plus a
SHA-256 `app_id:secret` hash ([market_data.py:884-892](data/market_data.py:884)). **Verify the
refresh actually succeeds pre-open** — if the PIN is wrong the Fyers feed will not come up.

---

## 5. MEDIUM / LOW findings

| # | Severity | Finding | Location |
|---|---|---|---|
| 1 | MEDIUM | **Auto Mode non-persistence** — resets to manual on every restart; no supervisor re-enables it. Safety property, but an operational trap for "unattended". | `scanner.py:188` |
| 2 | MEDIUM | **Cancel/replace can double-fill** — if a resting LIMIT fills in the gap between the status read and the cancel reaching the exchange, the market replacement adds a second fill. Largely mitigated (AngelOne rejects cancel of a filled order → `cancel_order` returns False). | `trade_manager.py:973` |
| 3 | MEDIUM | **Confirmation string is not a second factor** — the authorisation for live auto-execution travels in the same payload as the toggle. (Exposure is limited: see §6.) | `dashboard/server.py:1002` |
| 4 | LOW | **Silent `except Exception: pass`** around `getfeedToken()` — a stale feed token can be reused on WS reconnect with no log at all. Real hygiene regression (fails a test). | `data/market_data.py:1356` |
| 5 | LOW | Successful-status response with `data=null` escalates to *ambiguous* rather than *rejected*, creating phantom RECOVERY_REQUIRED trades. Errs safe. | `broker.py:411` |
| 6 | LOW | `DASHBOARD_AUTH_TOKEN` is **empty** in `.env`, so a random per-process token is generated at import. Token rotates on every restart — open browser tabs must reload. | `dashboard/server.py:43,93` |

---

## 6. What is genuinely solid (verified positives)

Several subsystems were probed adversarially and **held up**. These are load-bearing and worth not
regressing:

- **Daily-loss circuit breaker — well built.** `check_circuit_breaker()` runs every scan cycle
  ([scanner.py:5474](scanner.py:5474)), computes realized+unrealized per segment, and explicitly
  **fails closed** on a PnL parse failure (charges the segment's budget so one unparseable trade
  halts that segment). It latches `daily_loss_breached`, **flattens/closes** the breached segment,
  and blocks new entries via `can_trade()` at three gates in
  `engine/signal_processor.py` (2254, 4883, 4912). *(I initially suspected `can_trade` was dead
  code — it is not; it is wired from the signal processor, not the scanner.)*
- **Scanner loop is exception-resilient.** `run()` wraps `_scan_cycle()` in try/except with backoff
  ([scanner.py:4662-4675](scanner.py:4662)), and every background worker self-protects. A single bad
  cycle will **not** kill trading. *(Residual: a true hang inside `_scan_cycle`, or `is_running`
  flipping false, has no supervisor to restart the thread.)*
- **Orphan positions halt trading.** `can_open_trade()` returns
  `False, "Broker-only orphan position requires reconciliation"` whenever a broker-side position has
  no local record ([trade_manager.py:1332](trading/trade_manager.py:1332)) — orphans are **not**
  alert-only.
- **Restart recovery.** `load_state()` reloads open trades from SQLite; previous-session trades are
  marked `RECOVERY_REQUIRED` with CRITICAL logs; stale ghost rows are closed rather than spamming
  alerts ([trade_manager.py:1143-1180](trading/trade_manager.py:1143)).
- **Strict EOD bound.** `check_session_end()` force-exits at `ut_force_exit_time` (default **15:25**)
  every cycle ([trade_manager.py:2075](trading/trade_manager.py:2075)).
- **Dashboard WS is authenticated.** `websocket_endpoint` validates **both** origin and token and
  closes with 1008 otherwise ([server.py:916-921](dashboard/server.py:916)). *This refutes the
  concern that any client reaching port 8503 could arm auto-trading.*
- **Mode switches are exposure-aware.** Switching out of REAL, or resetting P&L, is blocked while
  live broker exposure exists (`scanner.py:3247-3269`).
- Entry ambiguity raises rather than retries (no duplicate **entries**); the entry-rate kill-switch
  correctly does **not** gate exits or protective orders; exits cancel the SL-M before flattening.

---

## 7. Test suite status

`pytest` (venv, Python 3.13.9): **1192 passed · 10 failed · 12 skipped** (49.8 s).
The 12 skips are the intentional quarantine documented in `tests/conftest.py`.

| Failure | Assessment |
|---|---|
| `test_dashboard_port_config` (×2) — `assert 8503 == 8001` | **Stale test.** Port migrated to 8503; tests still assert the retired 8001. Cosmetic but noisy. |
| `test_user_settings_govern_risk` — `risk_opt_pct 7.0 != 8.0`, circuit-breaker single-source-of-truth | **Config drift.** Confirm which value you actually intend to trade with. |
| `test_market_data_hygiene` — silent handler at `market_data.py:1356` | **Real (minor) regression.** See LOW-4. |
| `test_b_grade_entry_rule`, `test_market_calendar_and_expiry` — `assert 'OPT' == 'FUT'` | **Needs a decision.** B-grade / risky-regime routing now prefers OPT where tests expect FUT. Given `INST_PREF='OPT'` this may be intentional, but it is untriaged behaviour drift in the *instrument-selection* path. |
| `scanner.py:722 'Scanner' object has no attribute 'trades'` (×3) | **Test-harness artifact.** `self.trades` is set at `scanner.py:109`; these tests build a partially-constructed Scanner. |

**The `ut index 2` path scare — resolved.** Several tracebacks cite
`C:\Users\sagnik\Desktop\ut index 2\tests\...`. This is **stale bytecode**, not a split runtime:
the `.pyc` `co_filename` literally embeds the old path (the tree was copied from that workspace with
`__pycache__` intact). No junction/symlink exists on `tests` (`fsutil reparsepoint` → *not a reparse
point*), and `main.py` inserts its own directory on `sys.path`. **The audited code is the code that
runs.** Recommend deleting `__pycache__` to stop the confusion (two pytest-version caches coexist).

---

## 8. Pre-session operational checklist

Before enabling Auto Mode on **Monday 2026-07-27**:

1. **Fix BLOCKER-1 and BLOCKER-2** (duplicate SL-M). Non-negotiable for unattended operation.
2. **Refresh the Fyers token** and confirm the feed connects pre-open.
3. Decide `risk_opt_pct` **7 vs 8** and the circuit-breaker source of truth; reconcile the tests.
4. Triage the OPT-vs-FUT routing drift (2 failing tests) — this decides which instrument real money
   goes into.
5. Confirm capital is intended: `CAPITAL_TOTAL=14,00,001` / `FUT 9,00,000` / `OPT 5,00,000`,
   `INST_PREF=OPT`, 4 active indices.
6. Remember Auto Mode must be toggled **manually each session** and **re-toggled after any restart**.
7. Note `ALLOW_PLAINTEXT_BROKER_SECRETS=True` with `SHARED_HOST_MODE=False` — acceptable only on a
   restricted single-user host per `docs/OPERATIONAL_SAFETY.md`.

**Interim recommendation:** the system is in reasonable shape for **REAL/manual supervised** trading
(which is exactly what it arms itself into). Run it that way while the SL-M defects are fixed.

---

## 9. Audit coverage & honest gaps

Coverage was produced by a multi-agent fan-out plus direct verification. **The agent fleet hit a
session/usage limit mid-run: 2 of 10 subsystem finders and all automated verifiers failed.** I
verified the surviving findings and completed the remaining subsystems myself in-session.

| Subsystem | Coverage |
|---|---|
| Auto-mode / mode transition | ✅ Deep (agent + independent verification) |
| Order execution / broker | ✅ Deep (agent; both BLOCKERs personally re-verified) |
| Risk / circuit breaker / safety nets | ✅ Verified directly |
| Scanner loop & exception safety | ✅ Verified directly |
| State recovery / orphans / EOD | ✅ Verified directly |
| Config / `.env` / token freshness | ✅ Verified directly |
| Dashboard auth & control surface | ✅ Verified directly |
| Test-suite triage | ✅ Verified directly |
| **Signal engine (`signal_processor.py`, ~7k lines)** | ⚠️ **NOT deeply audited** — finder died. Entry-*quality* logic, backtest/live parity, and grade→confidence consolidation remain unreviewed. |
| **Websocket / 429 resilience (deep)** | ⚠️ **Partial** — reviewed the silent handler and refresh path; the reconnect/backoff state machine from the last commit was not exhaustively traced. |

Because signal-engine correctness is unaudited, this report speaks to **execution and safety
mechanics**, not to whether the strategy's *entry decisions* are sound. Re-running those two
finders after the usage limit resets (05:30 IST) would close the gap.

---

*Read-only audit. No production code modified.*
