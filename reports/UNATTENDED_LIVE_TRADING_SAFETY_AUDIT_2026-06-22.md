# Unattended Live-Trading Safety Audit — 2026-06-22

## Outcome

All 11 reported gaps were validated and addressed in code or explicit operating
policy. The complete regression suite passes: **268 passed, 13 intentionally
skipped, 1 third-party warning**.

## Findings and remediation

1. **No broker-side protective stops — fixed.**
   Confirmed REAL positions now receive an AngelOne `STOPLOSS_MARKET` order with
   `variety=STOPLOSS`. The order is persisted on the trade and tightened when
   the local trailing stop ratchets.

2. **RECOVERY_REQUIRED positions frozen — fixed.**
   Recovery positions continue mark-to-market processing, emit critical UI and
   desktop alerts, attempt broker protection, and auto-flatten after the
   configured grace if protection cannot be established. Protective-order
   cancellation must be confirmed before a market flatten to avoid over-exit.

3. **Fill confirmation only used positions — fixed.**
   Entry orders are polled through the order book. Rejected/cancelled orders are
   closed locally. A stale working limit is cancelled and replaced once with a
   market order; unresolved states escalate to recovery.

4. **Rate-limit headroom unproven — fixed and load-tested.**
   Market-data REST fallbacks, order status, positions, orders, exits, and
   protective stops share one AngelOne REST pacer. Best-effort LTP calls yield
   immediately rather than queue ahead of risk-reducing traffic. Concurrent
   one-to-four-index execution tests pass.

5. **Risk constants not verified against capital — fixed.**
   Startup logs the loaded capital and stop configuration, validates ranges,
   and fails REAL mode on invalid capital allocation. Position sizing uses an
   effective per-trade cap no larger than the segment daily budget. Current
   configuration loads ₹1,100,000 total, ₹600,000 FUT, ₹500,000 OPT,
   FUT SL 0.10%, OPT SL 10%, and configured absolute cap ₹15,000. The effective
   FUT cap is therefore clamped to ₹9,000.

6. **Wall-clock dependence — fixed.**
   During live market hours, fresh entries fail closed if host time differs
   from the latest market timestamp by more than 180 seconds. Session square-off
   uses market time when material skew is detected.

7. **Binary/unpinned requirements — fixed.**
   `requirements.txt` is UTF-8, contains no NUL bytes, and uses exact versions.
   Python is pinned to 3.13.9, with startup guards in batch and service scripts.

8. **No order-rate kill-switch — fixed.**
   Fresh entries are limited to six per rolling minute by default and latch for
   five minutes after a breach. Exits and protective orders bypass this entry
   kill-switch.

9. **13 quarantined tests — confirmed intentional.**
   Five cover rolled-back, unwired features; eight cover grade-letter gating
   superseded by confidence-only policy. A regression test fixes the expected
   quarantine count at 13 so silent additions/removals fail CI.

10. **Plaintext TOTP/credentials — hardened.**
    Startup warns when broker credentials are present in `.env`. Shared-host
    mode can fail REAL startup when plaintext credentials are forbidden.
    `.env.example` contains placeholders only.

11. **Deprecation-warning volume / Python 3.14 risk — fixed operationally.**
    Python is pinned to 3.13.x. The full suite now reports one FYERS
    `pkg_resources` warning; the reported `ast.Str` warning flood is absent.

## Verification

- `python -m pytest tests -q`: **268 passed, 13 skipped, 1 warning**
- Focused max-concurrency and safety suite: **11 passed**
- Python compilation: passed for broker, trade manager, scanner, market data,
  settings, risk manager, signal processor, and startup
- `pip check`: no broken requirements
- `git diff --check`: passed
- Safe runtime boot: HTTP 200 from `/api/status` and `/api/state` on port 8031,
  mode `HISTORICAL`; process stopped and port released

## Production boundary

No live order was sent during verification. AngelOne request shape, persistence,
cancel/replace, protection, tightening, recovery, and flatten behavior are
contract-tested with broker fakes. Before unattended funding, run a controlled
one-lot market-hours drill to confirm the account/product accepts the generated
SL-M and modification requests.
