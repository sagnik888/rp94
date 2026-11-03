# UT SMART EXIT integration audit after restart

Audit date: 30 September 2026, Asia/Kolkata. Source fixes and regression results are distinguished from the running process below.

## Runtime evidence

The main process restarted at 14:34:28 IST. The dashboard reported PID 31600, REAL mode, scanner running and Auto enabled. Its import snapshot matched the prior verified scanner, signal processor and trade manager source hashes. New UTS rows carried `uts_native.v1` rather than generic grading.

A new filled MIDCPNIFTY 15-minute option trade recorded entry ₹216.70, initial/current stop ₹184.85 and native 15% cap. The broker stop trigger recorded in SQLite matched ₹184.85. This confirms that the earlier stop/admission changes reached a real execution record. It is not a claim that this trade will profit, or an independent exchange audit.

The earlier MIDCPNIFTY 5-minute session row used a 5% capital stop and later produced `UTS_PROFIT_LOCK`. Older session rows still showed their original generic quality attribution; historical attribution was not rewritten.

Sensitive runtime reads used the dashboard's normal local token mechanism. The saved evidence excludes tokens and credentials. SQLite was opened read-only. No live order, mode, Auto setting or risk setting was changed during this audit.

## Confirmed faults and repairs

1. **Grading remained in replay, recovery and EOD.** Their signal verdict helper still called generic grade/confidence rules. It now calls the native UTS verdict, carries measured RSI/ADX/volume provenance, honours the call-site timeframe and records the selected cap. Generic scanner behavior stays intact.

2. **Old directional signals could fake alignment.** The previous counter could count an older BUY even after a newer SELL, and treated unknown timestamps as fresh. UTS now uses a separate counter that requires the exact timeframe, successful UTS result, observation within 30 seconds and a newly closed bar. Current closed state owns direction. The original flip may be older if the current state is fresh. Unknown, stale, forming or opposite evidence cannot earn the 5% alignment stop. Generic heavy-trend sizing keeps its existing counter.

3. **Reversal replay could exit a candle early.** The production paired-signal path used the next arrow's candle-open timestamp as the fallback exit. It now uses that arrow's confirmed candle close, bounded by session exit. This fixes 1-, 5- and 15-minute replay timing without moving the original source arrow timestamp.

4. **Incomplete metadata and infinite option prices could corrupt replay.** An explicit UTS replay now fills missing lane/timeframe metadata on a copied holder, so generic grade rules cannot silently choose its stop. Nonfinite reached OHLC values reject replay before profit or stop evaluation. Incomplete actual-option paths still abstain.

5. **A rapid peak and pullback could evade the profit lock.** The UTS monitor previously used the latest premium only. Verified contract ticks now run through the lock in received order. For entry ₹100 and initial stop ₹90, ₹120→₹113→₹118 between monitor passes triggers the ₹114 profit floor. A ₹89 initial-stop touch followed by ₹120 exits at the earlier touch; the later peak cannot justify continuing the trade. Pre-fill, future, out-of-order and invalid ticks do not create protection.

6. **Broker widening trusted local stop and request acknowledgement.** The controlled 10%-to-15% extension now verifies the actual pending broker order's original trigger, ownership, contract, side and quantity. After modification, it rereads the actual pending ₹85 stop before committing local widening. Missing or tighter original protection refuses the extension. Touch, quote, quantity, budget or profit-stop changes during broker calls prevent local widening.

7. **Uncertain modifications could undercount risk.** An ambiguous/refused modification keeps the tighter local stop and a persisted uncertainty flag, while risk reserves the possible wider exchange stop. That exposure cannot be hidden by reducing the configured percentage or absolute cap. Restoration must verify the actual original stop before the uncertainty flag and additional risk reservation are released. These changes apply only to uncertain native UTS extensions.

8. **A widening receipt could attach to the wrong session row.** Matching now requires the same source signal time, timeframe, direction, index and valid option contract token. Ghost, pending-exit, unknown-time and missing-token rows refuse receipt matching. A receipt cannot lower an already raised profit stop.

9. **VIX refresh could interrupt intelligence analysis.** The restart log showed `RuntimeError: dictionary changed size during iteration` for NIFTY, BANKNIFTY and SENSEX. A deterministic regression reproduced the same write-during-read mechanism. Percentile calculation now reads a stable VIX dictionary snapshot and retains its prior-session-only semantics.

Missing or invalid RSI is no longer substituted as neutral evidence for rejecting a setup; the joint weakness rejection requires measured RSI.

The extension also requires UTS engine provenance; a cached result from another scanner cannot authorise widening.

The full repository run also reproduced a telemetry contract regression from the earlier patch: ordinary graded signals were being required to expose UTS-only setup fields. Those fields now travel through a separate optional setup list. Ordinary grading telemetry retains its original contract, and native UTS setup still survives the candidate handoff. The repair passed a 90-case focused rerun.

## Policy preserved

For a ₹100 entry, 5% capital risk means a ₹95 stop for filtered 1-minute signals, cascade/alignment trades and strong aligned trends. Ordinary 5-/15-minute trades use ₹90. Choppy/ranging/volatile 15-minute trades use ₹85. Rare strong 5-minute setups may earn one verified extension from ₹90 to ₹85 before ₹90 is touched. A 5% cascade/alignment trade cannot widen.

The option capital stop remains separate from chart ATR. A wider ATR cannot make a new option entry risk 50% premium. Operator ceilings and already tighter stops still bind. Profit protection still arms at one initial risk unit. These rules and the weak-setup thresholds are strategy choices, not demonstrated guarantees of higher expectancy.

UTS entry approval and candidate ranking do not use display grade. AUTO product routing still uses its existing grade-dependent FUT/OPT selection logic; explicit OPT/FUT preference bypasses it. This audit did not redefine that product-selection strategy.

## Validation and remaining limits

**All 7,504 repository cases passed**: 7,500 in the clean default full suite and all four normally excluded integration cases in a separate run. Zero failures, errors or skips in those final runs. The four separately run cases use synthetic dashboard data and fake sockets, not live brokers.

The combined integration gate passed 822 cases before the last additions; the final focused integration gate passed 341. The initial full run exposed one telemetry failure; it was reproduced, repaired and passed in the clean full rerun. Source hashes were unchanged throughout that final run. The suite retains one pre-existing pytest warning about a test returning a value; no failure is hidden by that warning.

Tests use isolated databases and fake brokers. Current-option replay uses minute OHLC and minute closes for profit-lock peaks; it does not reconstruct exact tick order or reproduce the controlled widening and broker acknowledgement workflow. Replay results therefore cannot establish live widening profitability or full tick/broker parity.

The running PID loaded the previous fixes before this audit. **The repairs made during this audit require a further application restart or code reload.** The process was not restarted while managing a live position. A passing source test does not prove these newer repairs are active in that process.

Recorded evidence and proofs:

- `reports/uts_restart_runtime_evidence_20260930.json`
- `reports/uts_restart_audit_combined_20260930.xml`
- `reports/uts_restart_final_integration_20260930.xml`
- `reports/uts_restart_full_verification_20260930.xml` (initial full run)
- `reports/uts_restart_full_final_20260930.xml` (final full run)
- `reports/uts_restart_optional_integration_20260930.xml` (four additional integration cases)
- `reports/uts_restart_integration_audit_20260930.json`

Actual execution can differ from configured stop levels because of gaps, costs and broker processing. The official [Kite order documentation](https://kite.trade/docs/connect/v3/orders/) distinguishes an API response from the final order state; this is why UTS widening and uncertain restoration now verify the order itself.
