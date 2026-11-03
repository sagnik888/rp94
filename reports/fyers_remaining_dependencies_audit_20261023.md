# Audit of the remaining FYERS dependencies

Date: 23 October 2026. Scope: inspect the current implementation, search official broker documentation, and assess whether Kite plus Angel One can replace the remaining FYERS capabilities. Application code and configuration were not changed. No authenticated broker requests, application restarts, or orders were made.

Follow-up correction, 23 October 2026: the two defects recorded in section 5 were subsequently fixed. Angel option-pair and touch metadata now read `tradeVolume`. The parity checker accepts finite signed OI changes while retaining nonnegative checks for prices, volume and OI levels. Thirteen focused regression cases and the existing candidate/three-broker tests passed together: 57 passed in 9.94 seconds. The original findings below describe the audited state before those fixes. Other replacement and live-parity limitations remain open.

## Verdict

Most data functions can be rebuilt from Kite and Angel One. The earlier statement that FYERS still supplies these functions describes the current implementation; it does not establish that FYERS is technically indispensable.

Full replacement is **not established today**. Completed cash-volume reconstruction and day-OI reconstruction are feasible with additional state and recovery. Pre-open references have a documented Kite replacement route, but equivalent behavior across all four index symbols remains unverified. Exact intrabar timing, vendor corrections, and behavior during simultaneous preferred-provider failure cannot be guaranteed from documentation alone. Two providers cannot supply a third independent broker failure domain.

The appropriate target remains Kite primary, Angel One supplementary, and FYERS backup. FYERS can become a capability-independent backup only after the gaps below are implemented and validated.

## 1. Cash-index volume: reconstructable, not directly supplied by an index quote

FYERS documents a sum of constituent-stock share volumes for each interval, with membership changes applied from their effective date. This is neither index-futures volume nor turnover. Its documentation also distinguishes genuine non-trading constituents from traded activity. [FYERS volume definition](https://support.fyers.in/portal/en/kb/articles/how-is-the-volume-for-indices-calculated-in-fyers).

Equivalent calculation: `index_volume(interval) = sum(stock_share_volume(member, interval))`, using the membership effective on that trading date. Retrieve stock candles through Kite, or a complete corresponding Angel dataset. Never add duplicate observations from both brokers. A whole-provider interval fallback is easier to reconcile than mixing constituent providers without provenance.

Kite index packets omit traded volume, although tradable-instrument packets include cumulative volume. This makes the constituent service necessary; subscribing to additional index connections does not add the missing statistic. [Kite packet structures](https://kite.trade/docs/connect/v3/websocket/).

Current local evidence:

- `data/constituent_volume.py:101` already aggregates complete stock intervals and distinguishes missing data from explicit zero.
- The default `data_store/index_constituents.json` file does not exist. The service therefore cannot currently certify production membership or constituent volume.
- `data/constituent_volume.py:158` deliberately marks sampled live activity provisional. Sampling cannot certify exact minute boundaries after packet loss or late delivery.
- `data/kite_angel_migration.py:441` still preserves FYERS volume when overlaying preferred OHLC. The new hierarchy has not switched volume ownership.
- Recovery serializes stock-history calls. Fetching a large constituent union can exceed the configured refresh period; request capacity and recovery latency need measurement.

Required: authoritative, effective-dated NSE/BSE membership; persistent stock histories; interval reconciliation; verified non-trading/halting handling; quota-aware recovery; and session-aware volume semantics. A broker instrument master alone does not identify index membership. Under a strict requirement of only two broker APIs with no separately maintained membership, this replacement is not self-contained.

Completed intervals are feasible. Exact real-time equivalence to FYERS is not proven. Futures volume cannot substitute for this feature.

## 2. Native day OI: replace the statistic using a fixed baseline

Kite history supports the `oi=1` parameter. Its documentation warns that the current instrument list excludes expired contracts and that continuous history is restricted to specified futures markets. [Kite historical OI](https://kite.trade/docs/connect/v3/historical/).

Angel documents a separate `getOIData` endpoint for live F&O contracts. The official Python SDK exposes that endpoint. Indexed documentation was available during this audit, but direct access to the documentation page returned an authorization error. Neither account entitlements nor NFO/BFO coverage were tested. [Angel historical API](https://smartapi.angelone.in/docs/Historical), [official SDK](https://raw.githubusercontent.com/angel-one/smartapi-python/main/SmartApi/smartConnect.py).

Reconstruction: `day_oi_change = current_oi - previous_trading_session_closing_oi` for the same exact contract and compatible units. This is an engineering inference from available OI levels and historical observations, not a documented guarantee of equality to FYERS `oich`.

Required: persist a fixed prior-session baseline by exchange, underlying, expiry, strike and side; backfill it on restart; use the trading calendar; preserve broker provenance and units; and define handling for newly listed contracts, missing baselines, expiry, corrections and token reuse. Validate whether the broker's reported baseline is final exchange OI or another observation. An arbitrary previous poll is not a closing baseline. Missing must remain unavailable, not zero.

Current code has no Angel `getOIData` adapter and no fixed day-baseline service. `data/kite_angel_migration.py:194` fetches FYERS day changes even when the main Kite/Angel chain succeeds. Ordinary snapshot deltas cannot replace those day fields at arbitrary startup.

The new `call_day_oi_change` and `put_day_oi_change` fields have no direct strategy consumer outside data construction/metadata in the Python search. `intelligence/oi_tracker.py` separately requires a comparable interval horizon for interval pressure. Preserving a day statistic does not justify feeding it into a five-minute signal.

Conclusion: a comparable day-change feature is feasible. A native FYERS-reported field cannot retain that provenance after reconstruction, and exact numerical equality needs baseline validation.

## 3. Pre-open references: Kite has relevant support; four-index parity remains open

New evidence changes the earlier assessment. A Zerodha staff update dated 5 September 2026 states that the Quote API updates pre-open indicative equilibrium price in `ohlc.open`, alongside imbalance, indicative volume and buy/sell quantities, from 7 September. This supersedes reliance on older discussions about opening values updating only after matching. The announcement concerns eligible equity cash and derivatives instruments; it does not independently prove identical reference behavior for every index used here. [Current Kite pre-open announcement](https://kite.trade/forum/discussion/comment/52719/).

Current application evidence:

- `data/market_data.py:4299` deliberately prefers FYERS REST `lp` during the configured pre-open window. This is an application policy, not proof of a unique FYERS capability.
- The Kite tick callback stores LTP and previous close, but does not retain the pre-open `ohlc.open` as a distinct reference.
- Angel index REST uses `ltpData`, not a dedicated pre-open adapter. Fresh receipt time alone cannot establish that a value is a current indicative index value.
- `config/settings.py:2139` defaults the settlement cutoff to 09:08. Reassess the session contract against the newer auction behavior rather than inheriting this boundary blindly.

Required: a separate reference record with kind, session, exchange timestamp, source and finality; full Quote handling; and captures for NIFTY, BANKNIFTY, MIDCPNIFTY and SENSEX before, during and after matching. Do not relabel prior close as indicative price. Do not treat indicative auction volume as executed share volume or feed pre-open references into regular-session candles.

Conclusion: Kite provides a credible implementation route. Exact four-index equivalence, and Angel's independent ability to supply the same reference during a Kite failure, remain unverified.

## 4. Other unavailable or incomplete preferred data

Full quotes, streaming, history and instrument identities provide the building blocks for prices, exact option premiums, five-level depth, broad option chains, OI levels and previous close. Angel's official market-data announcement describes FULL quotes with price, volume, OI and depth. [Angel market-data capabilities](https://smartapi.angelone.in/smartapi/forum/topic/3661/new-feature-announcement-enhanced-real-time-market-data-with-our-market-data-api/1).

The current adapter already attempts broad exact-contract recovery, historical fallback and resolved India VIX quotes. These paths still need authenticated NSE/BSE/NFO/BFO coverage, active-expiry breadth, clock, unit and latency verification. An endpoint's existence does not certify every symbol or entitlement. Angel Greeks limited to one market cannot silently replace coverage elsewhere; locally derived analytics must keep their own provenance.

The Angel broad-chain route currently obtains its desired universe and contract pairs through Kite resolution. Complete independence during a cold-start Kite master failure requires a canonical contract universe recoverable from Angel's own master, with exact expiry and strike checks.

Two preferred providers can cover individual outages when the other has equivalent data. If both are missing the same interval or contract, extra connections cannot manufacture the observation. FYERS backup retains an additional broker source, though all brokers can still share upstream exchange failures.

## 5. Confirmed implementation and verification blockers

**Angel option volume is dropped.** The documented FULL quote field is `tradeVolume`. `data/angelone_market_data.py:418` instead reads `totTrdQty`; no alias is installed by `get_full_quotes`. A fully offline probe supplied positive volumes of 12,345 and 23,456. The adapter returned 0.0 for both legs. `data/kite_angel_migration.py:401` has the same unsupported field assumption in touch metadata. This is a code/schema mismatch, not an Angel API limitation. [Official field definition](https://smartapi.angelone.in/docs/MarketData).

**Current chain certification misses that error.** Its completeness, clock and OI checks can accept a chain despite lost option volume. Tests must use actual provider field names and distinguish unavailable volume from real zero.

**Parity checker rejects valid negative OI changes.** `scripts/verify_kite_angel_parity.py:50` requires every compared numeric field to be nonnegative, including OI changes. An offline probe with identical baseline/candidate captures and a call OI change of -10 failed all four chain comparisons with `call_oi_change_invalid`. Finite signed deltas are valid inputs. The checker also does not cover native day-change fields, pre-open references, depth or recovery fault scenarios; it cannot alone prove the requested full preservation.

**Current health is deliberately uncertified.** `data/kite_angel_migration.py:596` reports `parity_verified=False` and `cutover_ready=False`. Existing offline tests do not override these missing live observations.

## 6. Webhooks and provider redundancy

Kite postbacks report order-status changes. They do not supply exchange-wide stock volume, OI history, index references or market depth. Angel order updates likewise do not create observations from orders placed at Kite. Use market-data WebSockets and REST/history APIs for this replacement; keep Kite order ownership unchanged. [Kite webhook contract](https://kite.trade/docs/connect/v3/postbacks/).

Retaining FYERS as an authenticated, monitored backup preserves a third configured broker source. Removing it entirely cannot preserve that independence. A dormant backup also has different recovery latency from an already connected feed; its readiness needs evidence.

## Completion conditions

1. Correct provider schema handling and signed-delta verification; cover them with realistic fixtures.
2. Supply validated historical membership and reconcile complete constituent volume within the required latency budget.
3. Implement persistent day-OI baselines and restart recovery, with explicit units and horizons.
4. Implement separate pre-open reference handling and validate every required index and failure route.
5. Validate broad chains, exact premiums, depth, VIX and references using authenticated captures.
6. Replay entries, abstentions, management and exits across opening, ordinary trading, auction/close, expiry and rebalance sessions; inject stale data, disconnects, partial responses, rate limits, restart and individual/both-provider failures.
7. Only then move normal capability ownership away from FYERS while retaining it as the requested backup.

## Audit validation

The existing candidate and three-broker tests passed: **44 passed in 5.89 seconds**. Two additional offline probes reproduced the option-volume and signed-OI-checker defects described above. Passing tests establish current tested routing behavior; they do not establish live equivalence. Only this report was added by this audit.
