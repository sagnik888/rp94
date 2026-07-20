# Operational safety policy

- Broker entries are confirmed through the order book and position book.
- A stale working entry may be cancelled and replaced once with a market order.
- Every confirmed real position must have an exchange-side SL-M. Local trailing
  stops tighten that order; they never loosen it.
- A recovery-required position is alerted, marked-to-market, protected, and
  automatically flattened if protection cannot be established within the
  configured grace period.
- The entry-order rate kill-switch never blocks exits or protective orders.
- Fresh live entries are blocked when host time and the latest market timestamp
  differ by more than `CLOCK_SKEW_MAX_SECONDS`.
- Python is pinned to 3.13.x and dependencies are exact-version pinned.
- The 13 tests listed in `tests/conftest.py` are intentionally quarantined:
  five cover rolled-back, unwired features and eight cover grade-letter gating
  superseded by confidence-only policy. Any change to that count is tested.
- `.env` is acceptable only on a restricted single-user host. Set
  `SHARED_HOST_MODE=true` and `ALLOW_PLAINTEXT_BROKER_SECRETS=false` to make
  REAL startup fail closed if broker credentials remain in `.env`.
