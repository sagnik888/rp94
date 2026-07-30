# Live-Market Auto-Trading Readiness Audit — 2026-07-30

**Captured:** 2026-07-30 14:54–15:26 IST, market open, app PID 5308 (running since 12:54).
**Suite:** 1308 passed, 12 skipped, **0 failed**.
**Method:** live runtime evidence (logs, rtracker, candles) + targeted code verification.
Multi-agent audit was attempted but all 7 agents hit the session limit; this audit was
completed manually.

---

## Verdict

> **NOT READY for unattended AUTO-mode trading — but for one specific, fixable reason.**
> The money-path safety mechanisms are sound. The blocker is **data integrity**: the
> system is running on a fallback feed that supplies **zero volume**, which silently
> disables every volume-gated entry path. It is *safe* (fails closed) but *degraded* —
> it will not trade the setups it was designed to trade.

Supervised REAL/manual operation (what it does today) is fine.

---

## 1. What is genuinely healthy (verified live)

| Area | Evidence |
|---|---|
| Scanner loop | Cycle **3440**, latency 443–823ms, **zero ERROR/CRITICAL** in 500 lines |
| Auto-exit path | `if not self.auto_mode:` wraps only the *manual latch*; auto mode falls through to automatic close — **auto mode does exit** |
| OI / PCR (fixed) | PCR **0.788 / 0.827 / 0.902 / 0.921**, Call Delta ~0.55 (was 0 before the staleness fix) |
| Fyers chain | 123 strikes, `chains=4/4` every refresh |
| Contract identity (fixed) | **144 of 317** events carry `trading_symbol` (e.g. `MIDCPNIFTY25AUG2614700PE`) |
| WS reconnect storm | **0 occurrences today** (was every ~2s on 07-29) — resolved |
| Regression suite | **1308 passed / 0 failed** |
| Mode safety | REAL armed by premarket worker with **Auto Mode explicitly OFF** |

---

## 2. BLOCKER — Volume pipeline is dead; volume-gated entries silently disabled

**Evidence (today, all four indices):**

| Index | Bars | Volume sum |
|---|---:|---:|
| NIFTY | 334 | **0** |
| BANKNIFTY | 330 | **0** |
| SENSEX | 333 | **0** |
| MIDCPNIFTY | 330 | **0** |

Historically 260M–384M/day through **2026-07-24**, then **0 from 2026-07-27 onward**.

**Root cause chain — each link proven:**

1. **AngelOne history is rate-limited 189 times today** (`history rate limit … cooling down`).
2. That forces **279 Yahoo fallbacks** today (plus 842 local-cache serves).
3. **Yahoo returns volume = 0 for Indian index tickers** — verified directly:
   `^NSEI` and `^NSEBANK`, 75 bars each, `vol_sum=0`, `nonzero=0`.
4. AngelOne's own parser *does* read volume
   (`data/market_data.py:2263`, columns `[timestamp, open, high, low, close, volume]`),
   so the data exists on the path that is being starved.

**Why it matters for auto mode.** Volume gates receive `volume_ratio = 0.0`:

```python
volume_ratio = (float(last["volume"]) / prior_vol) if prior_vol > 0 else 0.0
# scanner.py:9375, 9441
```

This **fails closed** — it blocks rather than falsely permits, so it is not a money-loss
risk. But it means every volume-confirmed path is permanently off: `volume_spike_entry`
(needs ≥2.5×), late-session-reclaim volume votes, one-way-cascade volume votes, and the
volume components of intelligence. Corroborated historically: `volume_spike_entry` fired
only **7 times across all archived sessions**.

**Net effect:** in auto mode the system would run with a large part of its entry logic
disabled and would over-filter — the exact "system ignored the big moves" complaint.

**Recommended fix (not yet applied):**
1. Stop Yahoo zeros overwriting good volume — when the fallback supplies `volume==0` for
   an index, preserve the existing AngelOne volume rather than replacing it.
2. Tag candle provenance so volume gates can *skip* (not silently fail) when volume is
   genuinely unavailable, instead of treating 0 as "no participation".
3. Reduce history pressure: `HISTORY_CACHE_TTL_SECONDS` is 120s and the bucket is
   `TokenBucket(capacity=1, fill_rate=0.7)` (~42 calls/min) — with 4 indices × 3
   timeframes this self-inflicts the 189 rate-limit hits.

---

## 3. HIGH — Fyers token cannot survive unattended overnight

Verified live against the broker: `code -16` —
*"Refresh token API is currently disabled to comply with SEBI regulations."*
Identical response whether the PIN is sent hashed or plaintext, so this cannot be fixed
in code.

The access token expires ~**06:00 daily** and can only be renewed by a **manual OAuth
login** (`GET /api/fyers_login`). Without it the option chain dies, so OI/PCR/Greeks go
blank — the state this system was in yesterday.

**Unattended overnight auto-mode is therefore impossible on the Fyers feed** until Fyers
restores the refresh API. AngelOne (orders + primary data) is unaffected.

---

## 4. MEDIUM findings

| # | Finding | Impact |
|---|---|---|
| 1 | **Auto Mode is not persisted** — resets to `False` on every restart, and the premarket worker deliberately keeps it OFF | A mid-session crash silently reverts to manual: signals accepted, exits latched as *pending*, **nothing fires**. No supervisor or alert. |
| 2 | **NIFTY intermittently returns `PCR=0, Call Delta=0`** while the other three indices are healthy | Intelligence degraded for one index; needs a targeted trace of NIFTY's chain quality/expiry alignment. |
| 3 | **SL-M naked window** — the protective stop is armed only after the position-sync poll observes the fill (`position_sync_interval_seconds` ≈ 5s) | A fresh fill is unprotected at the exchange for several seconds. Acceptable supervised; a real exposure unattended. |
| 4 | **Repaint gate rejects the most decisive moves** — 55 of 222 rejections today were *"Repainted during 30s stabilization window"* | On fast trend days this structurally filters out the strongest candidates. Arguably correct (don't chase), but it is the single largest rejection bucket. |

---

## 5. Today's filtering profile (context, not a defect)

258 rtracker events → **222 REJECTED / 18 ACCEPTED / 18 EXIT**, only **2** manual signals
surfaced (13:37 LONG NIFTY, 14:23 SHORT BANKNIFTY).

| Rejection reason | Count |
|---|---:|
| Repainted during 30s stabilization window | 55 |
| Live trade readiness gate blocked it | 41 |
| C grade blocked | 29 |
| Choppy market blocked | 17 |
| Price-action ADVERSE | 8 |
| Quality gate blocked | 8 |

With volume permanently 0, part of this filtering is an artefact of the data outage rather
than genuine market judgement.

---

## 6. Gate to auto mode

1. **Fix the volume pipeline** (§2) — blocker. Without it auto mode trades a crippled ruleset.
2. **Solve the daily Fyers login** (§3) — either accept a manual morning login (so it is
   *attended* at open) or get the refresh API restored.
3. **Add scanner/auto-mode supervision** (§4.1) — alert if the scanner dies or Auto Mode
   silently reverts to OFF.
4. Then enable Auto Mode for **one supervised session** and compare against the
   backtester before leaving it unattended.

Until then: keep running **REAL/manual**, which is exactly what the system does today.

---

*Read-only audit. No production code changed while producing this report.*
