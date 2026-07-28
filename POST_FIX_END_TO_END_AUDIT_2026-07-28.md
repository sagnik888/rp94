# UT1 Index Intraday Algo Trading System — Post-Fix End-to-End Audit

**Audit date:** 2026-07-28  
**Codebase:** `rp46-3`  
**Scope:** Trading execution, signal processing, scanner/regime intelligence, market data, dashboard, concurrency, shutdown, and the 28 findings in the 2026-07-27 audit.

## Executive verdict

**CONDITIONAL PASS FOR CONTROLLED PAPER TRADING; NOT YET CERTIFIED FOR UNSUPERVISED REAL-MONEY TRADING.**

All 28 findings in the 2026-07-27 audit have source-level remediations. The safety-critical paths have direct adversarial regressions; several lower-risk findings are supported by code inspection plus broad regression coverage rather than a one-test-per-finding mapping. The follow-up adversarial review also found and repaired additional fail-open paths involving partial fills, late entry acknowledgements, stale lot sizing, stale regimes, stale option chains, cross-process account concurrency, and synchronous broker quotes on the scanner event loop.

The reviewed source and simulated regression suite do not currently expose an unresolved Critical or High defect. That is not proof that the system is bug-free. The running dashboard process has not been restarted into this source state, and this audit deliberately did not submit, cancel, modify, or flatten a real broker order. A controlled restart, replay/paper validation, and a minimum five-session soak remain release gates.

## Annotation 1 — Exact 0.20% range and breakout rule

The rule is implemented as a state machine. It is not a permanent label attached to the whole day.

### Range definition

Let:

- `R` = the regular-session opening price; the first valid session price is used only if the open is invalid.
- `H` = the highest price in the qualifying completed 5-minute candles.
- `L` = the lowest price in the qualifying completed 5-minute candles.
- `range_pct = ((H - L) / R) × 100`.

The box qualifies after at least three **completed** 5-minute candles. The currently forming candle is excluded so that a breakout tick cannot be absorbed into a moving boundary. A box is compressed when `range_pct <= 0.20%`; exactly `0.20%` is included. Once qualified, `H` and `L` are frozen for that session.

If the first three completed 5-minute candles have already expanded beyond `0.20%`, the state becomes `UNRESTRICTED`. The system does not incorrectly keep a one-lot compression cap for the rest of a day that was never compressed.

### State transitions

| State | Trigger | Lot policy |
|---|---|---|
| `FORMING` | Fewer than three completed 5-minute candles | Hard cap: 1 lot |
| `LOCKED` | Qualified box width is at most 0.20% | Hard cap: 1 lot |
| `BREAKOUT_WATCH` | Same-scan LTP breach of a frozen boundary plus buffer | Breach recognized immediately; hard cap remains 1 lot |
| `BREAKOUT_CONFIRMED` | First newly completed 1-minute candle closes and holds beyond the buffered boundary | Scaling can unlock only if the fresh strict regime and trade direction also agree |
| `FAILED_BREAKOUT` | A new close returns materially inside, or a confirmed break records two completed materially-inside closes | Scaling relocked immediately |
| `LOCKED` again | A fresh completed candle is back inside after failure | The original frozen box can arm a later genuine break |
| `UNRESTRICTED` | Initial qualification range was already above 0.20% | No compression cap; scaling still requires a fresh direction-aligned strict trend |

The breach buffer is:

`max(R × 0.0003, 0.01)` — three basis points of the session reference, with a minimum one-paise numerical floor.

An upside watch begins at `LTP >= H + buffer`; a downside watch begins at `LTP <= L - buffer`. This gives immediate detection without allowing one stray tick to authorize multiplied size.

The price state alone never authorizes scaling. Immediately before the broker call, the system recomputes the live box and requires:

1. `BREAKOUT_CONFIRMED` with live price still outside, or `UNRESTRICTED`;
2. the breakout direction to match the intended trade;
3. a current-session regime cache inside the configured freshness TTL;
4. a canonical `TRENDING_UP`/`BREAKOUT_UP` regime for BUY, or `TRENDING_DOWN`/`BREAKOUT_DOWN` for SELL.

If any condition is missing, stale, or contradictory, Heavy-Trend and Cascade multipliers are suppressed. Revalidation may reduce a candidate's lots but may never enlarge them.

### “Immediate” behaviour

The system reacts to a live boundary breach in the same scan by entering `BREAKOUT_WATCH`. A base one-lot trade can still be handled according to the normal entry rules. Multi-lot scaling intentionally waits for the next completed 1-minute close and the final fresh-regime check. The maximum intentional confirmation delay is one completed 1-minute candle; this is the anti-false-breakout safety margin.

## Does the system understand the day's trend regime?

Yes, within the limits of rule-based market inference. The regime detector uses trend strength and direction, moving-average structure, RSI, ATR expansion, Bollinger-band width, breakout location, Kaufman efficiency, session context, range-day guards, and short history smoothing. The detected regime affects signal grading, counter-trend penalties, range-day dampening, trade management, and final lot scaling.

It is important that `RANGING` is not a universal “no trade” switch. A valid base strategy or mean-reversion setup may still trade at base size. What is now prohibited is using a stale or falsely trending label to multiply that trade.

### Why multiple lots were previously possible in chop

The previous implementation had several independent paths that could preserve or create multiplied option lots:

- Heavy-Trend or one-way Cascade flags could multiply lots during product conversion.
- A candidate could wait in stabilization/manual queues and retain sizing from an older regime.
- Range-day scoring penalties were not an execution-time sizing invariant.
- A stale cached trend could still authorize multiplied lots.
- Some conversion and deferred-entry paths did not pass through one canonical lot resolver.

These paths are now converged on a final execution-time resolver. Inside `FORMING`, `LOCKED`, `BREAKOUT_WATCH`, `FAILED_BREAKOUT`, or `REVERSION_LOCKED`, the output is exactly one lot regardless of stale Heavy-Trend/Cascade flags. On a choppy day wider than 0.20%, the multipliers are still suppressed because the strict trend check fails; the configured base lot count remains the operator's base setting. With the default base of one, that is one lot.

## Volume, PCR, and OI truth path

The earlier `Volume 0.0x / NORMAL BALANCED` and `PCR 0.00 / NEUTRAL` presentation combined two different absence cases and displayed them as valid measurements.

### Volume

Cash index candles normally do not carry exchange-traded quantity. The system now:

- uses genuine candle volume only when sufficient non-zero observations exist;
- otherwise uses a clearly labelled relative 1-minute tick-activity proxy after enough live samples exist;
- publishes `available`, `status`, `source`, `metric_kind`, `is_proxy`, confidence, and reason;
- does not replace the latest zero observation with an older positive value;
- prevents unavailable volume from influencing the intelligence score.

The dashboard shows `-- / NO VOLUME DATA` until a valid real-volume or qualified proxy basis exists. A proxy is explicitly labelled “Tick-activity proxy”; it is not represented as exchange volume.

### PCR and OI

The option intelligence path now:

- requests an active-expiry, multi-strike snapshot around ATM (normally ±5 strikes);
- retains only real provider LTP, OI, and volume rather than inventing OI, IV, or Greeks;
- requires positive Call and Put OI and sufficient paired strikes;
- computes Call and Put OI pressure separately rather than summing them into futures-style net OI;
- permits a valid broad-chain PCR basis when a near-money subset is one-sided;
- marks expired, fallback, insufficient, mismatched-expiry, provider-stale, or TTL-stale chains unavailable;
- excludes unavailable PCR/OI from scores, reversal votes, and history;
- publishes the authoritative PCR result to the dashboard instead of only scoring it internally.

The dashboard now renders unavailable PCR/OI as `-- / OPTIONS DATA UNAVAILABLE`, with source/reason details, rather than fabricating `0.00 / NEUTRAL`.

## Original 2026-07-27 audit reconciliation

| ID | Original finding | Post-fix status and verification basis |
|---|---|---|
| C1 | Stray protective stop race | Remediated: protection lifecycle serialized; cancellation is terminally verified; hidden/residual stops and partial fills are reconciled; exit/entry races have adversarial tests. |
| C2 | Reversed options OI interpretation | Remediated: Call and Put OI are evaluated separately; unavailable/one-sided data cannot vote direction. |
| C3 | Scanner/dashboard shared-state race | Remediated: scanner-loop dispatch, copied snapshots, bounded latest-wins broadcasts, connection isolation, and concurrency tests. |
| C4 | IST market-hours mismatch | Remediated: market clock and live ticks are IST-aware; candle storage normalizes aware timestamps; timezone regressions pass. |
| C5 | Blocking telemetry subprocesses | Remediated: telemetry is offloaded to a bounded executor with explicit shutdown. |
| H1 | Ambiguous emergency flatten crash | Remediated: ambiguous acknowledgements enter fail-closed recovery and broker reconciliation; the sync cycle is not allowed to assume flat. |
| H2 | BANKEX routed to NFO | Remediated across broker, scanner, frictionless scanner, and signal conversion; BANKEX resolves to BFO in routing tests. |
| H3 | RiskManager dictionary mutation crash | Remediated through locked/snapshot iteration. |
| H4 | Timezone-unsafe signal deduplication | Remediated using absolute timestamp comparison. |
| H5 | Opposite-direction reversal blocked by dedup | Remediated: same-direction duplicates remain blocked while an opposite-direction signal follows its reversal path. |
| H6 | Blocking expiry refresh on rollover | Remediated: a usable snapshot stays active while refresh runs in a background worker and publishes atomically; a cold master now fails closed and warms off-loop rather than downloading for up to 30 seconds on `ScannerEventLoop`. |
| H7 | Blocking persistence in scan loop | Remediated: warm-memory and rejected-event persistence are offloaded/queued outside latency-sensitive scan work. |
| H8 | No graceful shutdown | Remediated: ingress is gated, scanner and sync work stop/join, broker work drains, trade state persists, streams/executors close, and the account lease is released in order. Shutdown does not blindly flatten a protected position. |
| H9 | Notification thread leak | Remediated with one bounded queue and one worker thread. |
| M1 | Stale fallback instrument CSV | Remediated: stale fallback files are rejected; refresh is staged and atomic. |
| M2 | Score normalized after final calculation | Remediated: the normalized value is used to calculate the published final score. |
| M3 | 30–59 candle regime NaN | Remediated with an adaptive rolling window and partial-readiness confidence cap. |
| M4 | Concurrent Windows file writes | Remediated for the supported single-process scanner with in-process locks and atomic replacement in calibration/R-tracker/warm-memory paths. Cross-process execution is separately blocked by the account lease; running multiple analytics writers against the same files remains unsupported. |
| M5 | No pre-trade margin check | Remediated with fail-closed available-funds checks, exact broker margin when available, conservative fallback, buffer, and concurrent reservations. |
| M6 | Missing dashboard environment template | Remediated: host, port, and auth token are present in `.env.example`. |
| M7 | Dashboard commands unthrottled | Remediated with per-connection command rate limits and bounded queues. |
| M8 | Unbounded scanner caches | Remediated with session cleanup and explicit size caps. |
| L1 | Hardcoded 5× sizing cap | Remediated: the raw sizing multiplier is configurable. |
| L2 | Hardcoded VIX thresholds | Remediated: high/low thresholds are settings-backed. |
| L3 | Silent broad config exception | Remediated: the audited config path catches expected exception classes and records diagnostic context. |
| L4 | List-sliced signal history | Remediated with bounded `deque` storage. |
| L5 | WebSocket token in query string | Remediated: authentication uses the WebSocket subprotocol header; URL query-token handling was removed. |
| L6 | Duplicate log compression | Remediated: one compression owner remains, avoiding the previous race. |

## Additional defects found and fixed in the follow-up audit

- A forming 5-minute candle could absorb the LTP breach into the range boundary.
- A pre-breach completed 1-minute candle could falsely confirm a newly armed breakout.
- A failed breakout could re-arm without a fresh completed close back inside.
- A missing/zero live LTP could be interpreted as a downside boundary breach.
- A delayed/manual candidate could retain stale scaled lots.
- A queued candidate could pass sizing validation and retain those lots after the market regime changed but before broker submission.
- A stale prior-session or over-TTL regime could authorize scaling at the broker gate.
- A cached option chain returned during provider cooldown could be relabelled fresh.
- A valid PCR was scored but omitted from the dashboard payload.
- Zero cash-index volume was presented as a valid balanced measurement.
- Prior-session tick samples or an older positive tick could qualify a current-session volume proxy after the latest sample became zero.
- One-sided/synthetic-looking option rows could satisfy PCR/OI minimum-strike counts without three real CE/PE OI pairs.
- A rejected broker-position payload could be interpreted as a flat account.
- Partial fills could continue while an exit was starting.
- Protection could be cancelled before a failed exit left the residual position safe.
- A negative, empty, or exceptional cancel acknowledgement could be treated as final without polling authoritative order status.
- A queued entry acknowledgement could arrive after emergency-flat logic and overwrite recovery state.
- Recovery reconstruction could enter the normal broker-entry branch and duplicate an already-existing broker position.
- Two sequential or truly concurrent signals could claim the same exact option contract under different strategy identities.
- Separate processes could mutate the same AngelOne account concurrently.
- Concurrent entries could each pass margin checks against the same available balance.
- Cold expiry lookups and direct option-premium helpers could still start a blocking instrument-master refresh.
- Stale-feed fallbacks for index/contract LTP, previous close, Fyers option chain, and executable touch quotes could call synchronous SDK/REST methods on `ScannerEventLoop`.
- Active-trade stop/protection and close paths could wait on the entry execution lock or broker SDK from the 100 ms scanner loop; a second scan-cycle owner could race the HFT owner.
- A toast-confirmed manual exit used broker `pending_exit` as a human-wait flag, which both rejected its own later confirmation and paused exchange-stop maintenance.
- Repeated protection refreshes drained LIFO and could starve an older trade while the newest trade was republished every 100 ms.
- Fyers authorization, dynamic SmartAPI WebSocket subscriptions, and a degraded REAL-mode reconnect attempt could execute blocking SDK calls on `ScannerEventLoop`.
- Dynamic BANKEX/SENSEX derivative subscriptions used SmartAPI exchange type `3` (BSE cash) rather than `4` (BSE F&O).
- A manual-mode ghost exit could wait behind a slow REAL entry's global execution lock.
- A failed dynamic REAL-mode transition could leave persisted `trading_mode=REAL` even though runtime state rolled back.

Each item now has a fail-closed code path. The newest regressions explicitly prove cache-only event-loop quote behavior, same-contract single-flight, bounded daemon refresh/subscription/auth work, prompt exit-intent latching, one-shot manual confirmation, fair protection batches, correct BFO routing, previous-close publication, and preserved synchronous behavior outside the event loop.

## Verification evidence

| Check | Result |
|---|---|
| Regime, compression, intelligence truth, and sizing focused suite | **79 passed** |
| Independent execution-lifecycle re-review suite | **72 passed** |
| Independent final release-blocker lifecycle/concurrency review | **177 passed, 2 skipped; APPROVE, no confirmed unresolved Critical/High blocker** |
| Nonblocking quote, expiry, option-premium, and touch-quote focused suite | **78 passed** |
| Final scanner-loop/broker-boundary adversarial suite | **14 passed** |
| Final combined lifecycle/manual/trailing adjacent suite | **124 passed, 2 skipped** |
| Full regression suite in the available compatibility runtime | **1,419 passed, 12 skipped, 6 deselected, 2 warnings** |
| Integration-marker suite | **4 passed, 1,433 deselected** |
| All changed Python files compile | Passed |
| Dashboard JavaScript syntax | Passed |
| Git whitespace/error check | Passed |
| Project virtual-environment dependency check | Passed before the later sandbox executable restriction |

Four full-suite deselections are the repository's existing intentional quarantine. The other two are option-history tests that require the installed CPython 3.13 PyArrow wheel; the final sandbox-accessible test runtime was CPython 3.12. Those two tests passed in the earlier native 3.13 run, and their production module was unchanged afterward. A final native-runtime rerun is still recommended as an operational release check.

No verification step submitted, modified, cancelled, or flattened a real broker order.

## Remaining limitations and release gates

1. **The running process is not this build yet.** Source edits take effect only after a controlled restart. Existing REAL/AUTO state must not be treated as proof of the fixed behaviour.
2. **No exchange-side certification was performed.** Broker timeouts, partial acknowledgements, RMS responses, WebSocket disconnects, and exchange order-state transitions were tested with deterministic fakes, not live orders.
3. **“No known blocker” is not “bug-free.”** Static review and automated tests cannot prove absence of all defects.
4. **Paper soak remains mandatory.** Run at least five full sessions covering compression, successful upside/downside breaks, failed breaks, range wider than 0.20%, expiry rollover, reconnect/re-authentication, and broker/API degradation.
5. **Verify dashboard truth after restart.** Volume may legitimately remain unavailable until enough tick samples exist, and PCR/OI must remain unavailable when an authoritative option snapshot cannot be obtained. Returning to numerical zero would be a regression.
6. **One application process remains the supported file-writer topology.** The REAL account lease prevents two execution owners, but the calibration and analytics files do not use an operating-system cross-process file lock.
7. **No-timeout SDK calls are contained, not made magically cancellable.** A permanently hung provider quote, authorization, or WebSocket-subscription call can occupy its bounded daemon slot; the scanner remains responsive and duplicate work is suppressed, but persistent unavailability requires provider recovery or a controlled restart.

## Recommended controlled rollout

1. Confirm AUTO is off and retain the current broker/order snapshot.
2. Restart through the normal project virtual environment in PAPER mode.
3. Verify the dashboard build/version and the new availability labels.
4. Replay the range-state tests and observe at least one live `LOCKED → BREAKOUT_WATCH → BREAKOUT_CONFIRMED` or `FAILED_BREAKOUT` transition.
5. Complete the five-session soak and review broker reconciliation, lot-policy, stale-data, background-quote unavailability, and shutdown logs.
6. Only after that review, consider a separately authorized one-lot supervised REAL trial.
