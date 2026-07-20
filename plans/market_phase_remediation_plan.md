# Market Phase Remediation Plan

## Goal

Make regime detection, entry filtering, position sizing, exits, session accounting,
historical replay, and dashboard reporting use one deterministic policy without
overfitting thresholds to the June 15 session.

## Phase 0 - Baseline and Rollback

- Record the current commit, dirty-file inventory, runtime settings, complete test
  result, localhost health, and June 15 API/session snapshot.
- Hash critical main-system and restore-point files.
- Keep `restore_point_26` unchanged until the main system passes every gate.
- Preserve one-day startup hydration. Multi-day history remains user-triggered.

Gate: stop if the current state cannot be reproduced or restored.

## Phase 1 - Canonical Regime Contract

- Define canonical states: `TRENDING_UP`, `TRENDING_DOWN`, `RANGING`,
  `VOLATILE`, `BREAKOUT_UP`, `BREAKOUT_DOWN`, and `UNKNOWN`.
- Centralize helpers for trend direction, choppy/risky states, allowed instrument
  types, and trailing behavior.
- Replace scattered string comparisons in the detector, quality policy, processor,
  scanner, trailing logic, historical logic, and dashboard serialization.
- Add persistence/hysteresis and require prior-range confirmation for breakouts.

Files:
- `intelligence/regime_detector.py`
- `engine/signal_quality_policy.py`
- `engine/signal_processor.py`
- `scanner.py`

Gate: every canonical regime must produce the expected entry and trailing policy.

## Phase 2 - Unified Entry Decision

- Introduce one pure decision result containing allow/block, stable reason codes,
  regime, timeframe, closed-candle state, confirmed impulse, market location,
  option quality, and applied limits.
- Use the same policy in manual, auto, live, historical, and shadow paths.
- Fix the unreachable option-quality block.
- Preserve ADX, volume, candle structure, intelligence, and 15-minute alignment on
  the candidate passed to final gating.
- Remove duplicate scanner-side decisions only after parity tests pass.

Gate: live and historical decisions must match for identical closed-candle inputs.

## Phase 3 - Five-Minute Stabilization

- Require closed candles for ordinary five-minute entries.
- Allow forming-candle entry only for a confirmed impulse requiring directional
  candle structure, range expansion, volume participation, confidence, and
  higher-timeframe compatibility.
- Remove ADX-only explosive bypasses.
- Risky regimes must raise requirements; dynamic confidence must never fall to
  30 percent merely because the market is volatile or unknown.
- Run a final repaint check at candle close.

Gate: ADX-only setups are rejected, confirmed impulses remain eligible, and
forming-signal disappearance is deterministic.

## Phase 4 - Location and Anti-Churn

- Change market-location output to `ALLOW`, `REDUCED_RISK`, or `BLOCK`.
- Block clear chase and countertrend-extreme entries. Reduced-risk mode must have
  explicit sizing and exit behavior rather than only a dashboard note.
- Rebuild daily entries, losses, cooldowns, lifecycle IDs, last exits, and
  directional exposure from both TradeManager and session-ledger records.
- Persist and restore counters without double counting.

Gate: restart cannot reset the six-trades-per-index limit or cooldown state.

## Phase 5 - Exit Correctness

Implement and test separately:

1. Correct PE synthetic fallback pricing with signed delta.
2. Route opposite signals to broker-backed open trades.
3. Make broker exits idempotent and retry-safe.
4. Define manual semantics:
   - Watch-only rows are advisory and never become trades.
   - Manually accepted trades follow the selected manual exit policy.
   - Auto trades receive automatic stop, target, repaint, opposite-signal, and EOD
     protection.
5. Make all exit families pass through the same policy boundary.

Gate: no duplicate exit order and no exit path bypasses the selected mode.

## Phase 6 - Chronology and Lifecycle Truth

- Match entries and exits with stable lifecycle IDs.
- Reject or quarantine exit-before-entry rows at creation, restoration, and
  dashboard serialization.
- Exclude invalid rows from P&L until deterministically repaired.
- Repair the June 15 SENSEX row without altering valid broker records.

Gate: zero impossible timestamps, duplicate lifecycles, or inflated P&L rows.

## Phase 7 - Real Risk Sizing

- Convert risk budget and stop distance into actual lots and broker quantity.
- Enforce lot size, capital cap, per-index exposure, and maximum position value.
- If one lot exceeds allowed risk, block the trade instead of silently forcing one
  lot.
- Keep manual quantity overrides explicit and auditable.

Gate: calculated risk, order quantity, dashboard exposure, and realized P&L agree.

## Phase 8 - Replay and Stress Verification

- Test every regime with BUY/SELL, 5m/15m, option/future, manual/auto/historical,
  closed/forming candles, and all broker success/failure states.
- Test restart persistence, session rollover, expiry T-2 rules, weekly/monthly
  expiry, holidays, weekends, PE fallback direction, and lifecycle reconstruction.
- Replay June 15 at least three times with identical settings. Require identical
  candidates, reason codes, lifecycle IDs, exits, and P&L.
- Replay multiple trend, range, volatile, breakout, winning, and losing sessions.
- Use June 15 to expose defects, not to choose profitable thresholds.

## Phase 9 - Shadow Acceptance

- Run the new policy without order authority while the existing policy remains
  authoritative.
- Record old-versus-new decisions and reason codes.
- Require stable scanner latency, no dashboard regression, deterministic replay,
  restart-safe counters, live/historical parity, and uninterrupted active-trade
  protection.

Gate: any failure rolls back only the affected phase.

## Phase 10 - Promotion

1. Verify the main system and restart at the required one-day default.
2. Verify localhost APIs, dashboard, charts, manual mode, auto simulation, and
   broker-failure handling.
3. Run the complete suite and audit scripts.
4. Copy only verified changes into `restore_point_26`.
5. Regenerate manifests and verify normalized relative-path hashes.
6. Test `restore_point_26` independently.
7. Commit phase-sized changes.
8. Push GitHub `main` only after both systems pass.

## Isolation Rules

- Do not combine regime vocabulary with threshold tuning.
- Do not combine five-minute stabilization with risk sizing.
- Do not combine location enforcement with chronology repair.
- Do not combine opposite-signal exits with manual-policy changes.
- Do not combine PE pricing with broad market-data refactoring.
- Do not update restore points or GitHub before shadow acceptance.
