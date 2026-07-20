# Intelligence Data-Truth Audit — 2026-06-22

## Outcome

The five prioritized gaps were traced through live, historical, dashboard, and
calibration paths. Four required code changes; the order-flow item is a broker
feed limitation and is now disclosed explicitly rather than overstated.

## 1. Synthetic IV percentile — fixed

The previous percentile was generated from price velocity and nominal option IV.
It could change from 40 to 90 without any volatility-history ranking.

The routing input is now an empirical **India VIX percentile**:

- one prior-session closing observation per day;
- no same-session data in historical calculations, preventing look-ahead;
- at least 20 prior sessions required;
- neutral percentile 50 with `unavailable_neutral` metadata when history is
  inadequate;
- one-year VIX history refreshed to 246 sessions plus recent intraday data;
- live VIX is cached and appended to the history file;
- live and historical instrument routing use the same percentile semantics.

This is a market-volatility regime percentile, not instrument-specific option IV
Rank. The API fields say so explicitly.

## 2. OI-change dependency — fixed

Audit findings:

- FYERS returns `oich`, which was already mapped to call/put OI change.
- AngelOne REST fallback returned current `opnInterest` but hardcoded both
  absolute OI changes to zero.
- Dynamic AngelOne option subscriptions used QUOTE mode 2 even though the
  official SDK exposes OI only in SNAP_QUOTE mode 3.
- Zero OI change was incorrectly interpreted as covering/unwinding based on
  underlying price.

Changes:

- AngelOne option subscriptions now use SNAP_QUOTE mode 3.
- Current OI is persisted by instrument, expiry, strike, and session.
- When provider absolute OI change is absent/zero, call/put deltas are computed
  from consecutive same-session snapshots.
- Provider-reported non-zero OI change remains authoritative.
- First snapshot/unchanged OI produces a neutral signal and explicit
  `snapshot_warmup`/`snapshot_delta` source.
- OI-change source is published through chain metadata and intelligence output.

## 3. Order flow — accepted limitation, made explicit

The engine remains an OHLCV close-position proxy, optionally replaced by L1
bid/ask depth and confirmed by option volume/OI activity. It is not a
trade-by-trade aggressor footprint. Intelligence output now includes:

- `true_tick_footprint_available=false`
- the exact OHLCV/L1 proxy limitation
- the active source (`ohlcv_close_position`, depth source, or unavailable)

## 4. Greeks heatmap fixed-IV display — fixed

The strike heatmap no longer hardcodes 15% IV. It receives the same call/put IV
context used by the ATM Greeks calculation and publishes the IV and source on
every strike. The dashboard label was also changed from option
“CHEAP/EXPENSIVE” to **LOW/MID/HIGH VOL**, because India VIX percentile measures
volatility regime rather than option richness.

## 5. Calibration / self-learning claim — formalized

Calibration remains advisory and cannot modify live decisions. A validation
status is now published through `/api/status`, `/api/state`, and
`/api/calibration-status`.

Current dataset truth:

- 29 unique trade samples
- minimum required: 200
- NIFTY: 16; BANKNIFTY: 13
- context-complete rows: 0
- state: `COLLECTING_EVIDENCE`
- live adjustments: disabled
- self-learning claim: not allowed

Promotion requires chronological walk-forward validation, costs/slippage,
expectancy and drawdown improvement, a 20-session shadow run, and an explicit
reviewed configuration change.

## Verification

- Focused intelligence/dashboard suite: **36 passed, 3 skipped**
- Complete suite: **277 passed, 13 skipped, 1 third-party warning**
- Compilation: passed
- `pip check`: no broken requirements
- `git diff --check`: passed
- Isolated HISTORICAL runtime on port 8033:
  - real VIX percentile: 61.8, source `india_vix_daily_percentile`
  - OI source published as `angelone_snapshot_delta`
  - zero/unconfirmed OI remained neutral on three instruments
  - flow disclosed `true_tick_footprint_available=false`
  - heatmap IV matched the ATM IV context
  - calibration endpoint returned `COLLECTING_EVIDENCE`
  - no intelligence exceptions
  - process stopped and port released
