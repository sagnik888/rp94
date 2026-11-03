# Resilience claim verification — 5 September 2026

The earlier claim that all six areas were fully fixed is not supported. Direct
execution reproduced five remaining failures. This verification changed tests and
this report only; application code and the running service were not changed.

## Results

The existing focused collection passed 139 tests, including the new
`test_gptastra_audit_verification_2026_09_05.py`. That file contains 22
`inspect.getsource` checks; its description overstates its behavioral coverage.

The independent behavioral collection executed 15 cases: **10 passed, 5 failed**.
Failures below assert the desired behavior and remain executable reproductions.

| Original finding | Result of direct verification |
| --- | --- |
| 1. Slow broker calls | A 250 ms funds preflight runs in the real background queue without delaying the scanner heartbeat beyond 150 ms. A protection check is deferred correctly. However, a full execution queue still blocks the heartbeat for 2.006 seconds. |
| 2. Silent-feed recovery | Passed. Four stalled health passes produce four attempts at simulated times 100, 115, 130, and 145 seconds. This holds for False, an exception, and a successful write without recovered feed health. |
| 3. Intelligence freshness | Passed. Direct execution through `_process_instrument` preserves the old bundle's age of at least 300 seconds when analysis fails, including failure after successful analysis during IV recording. |
| 4. Held-signal retry | Partial. The same held signal reaches grading after intelligence returns in INTRACANDLE mode. CANDLE_CLOSE mode remembers it but does not retry it, even within the allowed deadline. |
| 5. Futures quote protection | Partial. A seeded fresh contract mark allows processing with no spot. Both prices absent skips processing. But an hour-old provider cache quote becomes a new local mark, and the fast path ignores an available fresh websocket contract quote. |
| 6. Uncertain exit | Partial. Existing FULL_EXIT found/absent/cancelled cases pass. After a lost recovery-exit acknowledgement, reconnection can submit a second exit while the original RECOVERY_EXIT is still live. |

## Remaining failures and repair requirements

1. **Duplicate recovery exit (highest priority).** The test submits a recovery SELL,
   simulates loss of its acknowledgement and an unreadable order book, then restores
   the book with that same order still live and position quantity unchanged.
   Reconciliation submits another SELL with the same tag `U1R0T1`.
   `trading/trade_manager.py:5322` releases the ambiguity latch on a position row;
   `:5423` then searches FULL_EXIT, although the uncertain order used RECOVERY_EXIT.
   Reconcile the actual submitted leg and authoritative order state before releasing
   the latch or permitting another exit. Unchanged quantity does not prove no pending
   order exists.

2. **Old provider price becomes fresh futures evidence.** With no broker connection
   and an hour-old provider LTP cache, the real `MarketDataProvider.get_ltp` returns
   25100 via `data/market_data.py:3461`. `scanner.py:11587` puts that price into its
   futures cache with the current time. Preserve and validate source timestamps
   through the provider boundary; the scanner's two-second cache TTL alone is
   insufficient.

3. **Fresh websocket futures quote ignored.** With a fresh provider websocket quote
   of 25200 and an empty scanner-local cache, the production fast lookup returns
   None at `scanner.py:11577`. Both normal scan call sites use this fast mode.
   Read age-qualified provider/websocket quotes without REST and ensure an explicit
   refresh owner. Otherwise missing spot can skip management despite a fresh contract
   quote, and available spot can drive the existing synthetic futures fallback.

4. **Candle-close held signal never reaches grading again.** At 10:05:10 a 10:00
   five-minute BUY is held for missing intelligence. Intelligence is restored at
   10:05:20. INTRACANDLE retries; CANDLE_CLOSE does not. The registration exists, but
   `engine/signal_processor.py:4724` gates its consumer on intrabar mode. Data-readiness
   retries need to work in both modes while retaining candle-close and expiry rules.

5. **Execution queue saturation pauses the shared scan loop.** A full queue makes
   `TradeManager.open_trade` delay a scheduled 10 ms heartbeat by 2.006 seconds.
   `engine/async_queue.py:117` loops over blocking `put(..., timeout=0.1)` attempts
   until a two-second deadline. This remains reachable when slow broker work occupies
   all workers and the backlog fills. Admission to the worker queue must return
   promptly with explicit defer/refusal handling under saturation.

## Volume and runtime evidence

Recounted the final 20 bars specifically for **4 September**, not the latest 20 bars
from whichever session now ends the mutable cache:

| Index | 1min positive | 5min positive | 15min positive |
| --- | ---: | ---: | ---: |
| NIFTY | 7 | 19 | 20 |
| BANKNIFTY | 7 | 19 | 20 |
| MIDCPNIFTY | 11 | 20 | 20 |
| SENSEX | 7 | 19 | 20 |

The quoted 7–11 / 19–20 / 20 ranges are verified. The predictor's message refers to
the current one-minute volume evidence; labelled tick activity and price evidence
can still contribute. It does not establish availability in other timeframes.
Changing a test assertion does not restore missing provider volume.

Local `/api/status` reports HISTORICAL, PID 109504. Its process started at
16:07:36 IST, before the application files edited at 17:45–17:48. This process does
not demonstrate execution of the revised files. No live orders or service restart
were performed during verification. The project's default pytest configuration
excludes integration-marked tests, so the previous 5,642-pass run was not a live
broker end-to-end certification.

## Reproduce

```powershell
python -m pytest -q tests/test_resilience_claims_behavior_2026_09_05.py --tb=short --disable-warnings --junitxml=resilience-claims-verification.xml
```

Machine-readable results: `resilience-claims-verification.xml`.

SHA-256 of the application files checked:

```text
main.py                     E865FE09957146063741C7D6DE16AF76CC9C84BFFC30E30DD10604488D70D4FB
scanner.py                  9DD9C28FA749F56171603E5CD7CD4BB1A7460CAEB347712968485E62251A6EC9
engine/signal_processor.py  28CF4EC4F54AEBAC077A2E3CF28D73ADFECE4B48CC1E90D207262A6EF83CC2D2
engine/async_queue.py        1EFC4014E37C73ED40740B235158CD90D7D505EAD531AC1299C34B72E023F3A4
trading/trade_manager.py     BDC3E27E9877520B1E1AC840578A2BBAD9F0A4DA0B505AB36B3168C94DA564E3
data/market_data.py          393E4DCC2A627B484B9C6C431CA8D703A5EBF3F682800536EBAE9EC42F17912B
```
