# One-minute reversal confirmation cap

The one-minute exit confirmation incorrectly inherited the 30-second minimum and up to 60-second intelligence hold used by slower timeframes. The scanner now caps both boundaries at 15 seconds for 1min only. Larger environment settings cannot extend that cap. The pending notification uses the effective value. At the deadline, an adverse move still cancels the flip; an intelligence-only hold expires under the existing policy. Historical and native closed-candle bypasses retain their existing behavior.

The API configuration exposes opposite_flip_confirm_1min_cap_seconds=15. The current running process was checked and does not yet expose that field, so restart is required for activation. The cap governs the confirmation timer, not broker-fill latency or scanner scheduling: a delayed scan can observe the deadline later than 15 seconds.

Regression tests verify no confirmation at 14.999 seconds and resolution at 15 seconds for confirm, intel_block and adverse verdicts. Existing slower-timeframe, native UT Smart and combined ownership tests are run alongside them. Results are recorded in the adjacent JUnit XML.

Results: the broad run passed 174 tests and failed one combined S1 cache object-reuse assertion. A focused rerun passed 134 tests, including every repaint/UT Smart timing test, and reproduced that same cache assertion failure. The failing assertion checks repeated MultiTimeframeEngine.process_instrument calls, without invoking the changed reversal-confirmation method; it remains unresolved by this timing patch.
