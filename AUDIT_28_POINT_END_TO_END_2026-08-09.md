====================================================================================================
rp46-3  ·  28-POINT END-TO-END SYSTEM AUDIT
branch: fix/auto-mode-safety-and-trend-runner        date: 2026-08-09
====================================================================================================

Every one of the 28 claimed behaviours was audited against the code that implements it.
All 28 came back PARTIAL -- none is fully clean, none is wholly absent.

  points audited .......... 28
  findings ................ 118   (critical 3 · high 42 · medium 57 · low 16)
  baseline test suite ..... 1 failed, 2584 passed, 12 skipped, 4 deselected  (99.86s)

Findings marked [RE-VERIFIED] I confirmed myself by reading the code or reproducing the
behaviour. Everything else is a single-agent finding with a file:line and a stated failure
mode -- credible, but not independently confirmed.

====================================================================================================
VERDICT TABLE
====================================================================================================

  #   status    crit high med low   requirement
  --------------------------------------------------------------------------------------------------
  1   partial   .    1    3   .     Pre-market boot, 09:00 warmup, 09:08 settle, pre-open read
  2   partial   .    1    1   1     Both-broker data -> 3-TF scan -> buffer -> intel filter ->...
  3   partial   .    4    1   .     Simultaneous signals add +1 lot to the running trade, not...
  4   partial   1    1    1   1     Post-entry 20s re-scan to candle close; exit on repaint
  5   partial   .    3    2   .     Per-signal trail, profit-lock ladder, rupee loss slabs
  6   partial   .    1    1   1     Per-tick intelligence per index, same or better for live...
  7   partial   .    1    2   1     Per-index move size, volume and lot size handled correctly
  8   partial   .    2    3   1     Session and per-timeframe regime for all 4 indices
  9   partial   .    2    2   .     Broker states: flapping vs fully lost vs one-of-two down
  10  partial   .    .    3   2     No material delay between backend and frontend
  11  partial   .    3    2   .     MACD histogram per timeframe prices the entry/exit bid
  12  partial   .    2    2   .     Re-entry on winners and losers, pullbacks, hedging
  13  partial   .    .    2   1     Worker engage/release lifecycle per trade
  14  partial   .    2    3   .     Full session day in warm memory with a rolling last hour
  15  partial   .    1    4   .     Daily / weekly / monthly data cleaning
  16  partial   .    2    2   1     Live market vs off-market discrimination
  17  partial   .    2    3   .     Slippage measured and fed back between trades
  18  partial   .    2    2   .     Cascade trade count and lot size driven by market data
  19  partial   .    1    3   .     Cancels, partial fills, missed prices on fast moves
  20  partial   .    .    1   1     First start mid-session, and restart mid-session
  21  partial   .    1    1   1     ATR flip: buy->sell transition through buffer and analysis
  22  partial   1    1    2   .     Index spot vs index futures vs option premium kept apart
  23  partial   .    1    1   2     AngelOne SmartAPI retired, Kite/Zerodha in its place
  24  partial   .    3    2   .     Dynamic RR, target, exit and stop-loss
  25  partial   .    1    2   1     Full chart+scanner read with no signal delay; server...
  26  partial   .    1    2   1     How a generated signal is graded
  27  partial   1    1    2   .     Every intelligence engine alone and combined; track/trail...
  28  partial   .    2    2   1     Regime detection across day / hour / minute and its effect...

====================================================================================================
PART 1 -- FIX THESE THREE FIRST
====================================================================================================

Each is confirmed, each has a concrete trigger, and each costs money rather than tidiness.

  [C1]  An empty scan cache is treated as a repaint, so a cache clear market-exits live positions
        requirement 4  ·  scanner.py:23090  [RE-VERIFIED]

        The tracker's only evidence is `self._live_utbot_signal_still_present(candidate,
        sig_timestamp)` (scanner.py:23090), which is `_repaint_signal_status(...) == 'PRESENT'`
        (5542). `_repaint_signal_status` reads `cache = getattr(self, '_cached_results', None) or
        {}` and returns 'UNKNOWN' when the `{instrument}_{timeframe}` key is absent or its signals
        list is empty (5509-5513). The docstring itself says "Callers treat it like VANISHED". The
        tracker has no freshness/populated-cache guard and no debounce: one False read goes straight
        to the abort branch (23144-23168). `_cached_results.clear()` is invoked while trackers are
        in flight from operator actions - configure/full-refresh on a settings save
        (scanner.py:3926), `perform_system_recalibration` (4027), explicit reset (3863), day
        rollover (883, 5904) and engine/scanner_recalculation.py:374. The session mirror at least
        brackets its close determination with `_repaint_close_check_in_window` (19899); the
        real-money tracker never calls it (grep: call sites are only 14589 and 19899).

        FAILURE
        NIFTY FUT long entered intracandle at 10:31:40 on the 10:30 5min bar, currently -6 points.
        At 10:32:05 the operator saves any setting from the dashboard; the configure path hits
        `self._cached_results.clear()` (3926). At the tracker's next 20s tick (10:32:20) the cache
        is empty, `_repaint_signal_status` returns 'UNKNOWN', `still_present` is False. No
        mitigation applies (no stabilization override, not in profit, not an OPT so the defer
        returns False,''), so `_close_and_record(trade.id, mark, 'REPAINT_ABORT')` fires and
        close_trade sends a real market exit for a signal that never repainted.

  --------------------------------------------------------------------------------------------------

  [C2]  `entry_spot = entry_spot or price` stores the option PREMIUM in the spot field
        requirement 22  ·  trading/trade_manager.py:3603  [RE-VERIFIED]

        `open_trade` defaults `entry_spot` to `price`. For an OPT trade `price` is the option
        premium (~120), not index spot (~24850). All three OPT entry sites pass an unguarded spot
        lookup - scanner.py:1373 `actual_spot = self.candles.get_latest_price(candidate.instrument)`
        -> scanner.py:1402, and the same pattern at 23941->23964 and 24028->24077 - and
        `get_latest_price` returns None/0 when no spot tick is cached for that index. `None or
        price` yields the premium. This also kills the guard at trade_manager.py:3911-3914 whose own
        comment reads 'If entry_spot is missing, do NOT use spot price as option price! Keep it at
        entry_price to avoid massive fake P&L': that else-branch is unreachable whenever price > 0
        because line 3603 guarantees entry_spot > 0. The synthetic branch at 3904-3910 then runs
        with a premium-valued entry_spot and, unlike the scanner path, applies NO plausibility
        sanitisation - only `max(0.05, ...)` at 3917. Every spot<->premium converter keys off the
        same field (scanner.py:25042-25055 prem_to_spot/spot_to_prem), so spot-space stops and
        targets are corrupted too.

        FAILURE
        NIFTY 25000CE bought at premium 120, instrument_multiplier 0.5, 75 qty. The spot tick is
        briefly unavailable at entry so scanner.py:1373 returns None and the trade is created with
        entry_spot = 120. On any later heartbeat where the live option quote is missing
        (real_premium None), trade_manager.py:3905 computes spot_change = 24850 - 120 = 24730,
        premium_move = 12365, current_price = 12485. The position reports roughly Rs 9.2 lakh of
        fabricated profit, fires TARGET_HIT, and books a fictitious exit price into the ledger.

  --------------------------------------------------------------------------------------------------

  [C3]  A single available pillar saturates the aggregate to 100/STRONG_BUY; no minimum-component gate
        requirement 27  ·  intelligence/intelligence_aggregator.py:567  [RE-VERIFIED]

        `averaged_score = score / max_possible` divides by the sum of maximum votes of only the
        pillars that actually voted. Regime is the one pillar that increments `max_possible`
        unconditionally (line 415-416, +0.25). Volume needs `volume_available` (237), OI/PCR need
        `chain_authoritative` (242, 190), order flow needs `flow_available` (450). When the option
        chain is not authoritative and order flow abstains — the exact 09:15-09:19 condition the
        comment at lines 431-434 documents — regime alone owns the denominator: a TRENDING_UP read
        contributes score=+0.25 over max_possible=0.25, giving averaged_score=1.0, final_score=100.0
        and final_signal STRONG_BUY (lines 571-580). Grep across scanner.py and engine/ shows
        `components_analyzed` is only ever written (line 595) and never read as a gate, so nothing
        downstream can tell a one-engine verdict from a seven-engine consensus. The
        weight-normalisation rewrite documented at lines 143-157 fixed the old unreachable-ceiling
        problem by making the denominator adaptive, but did not add a floor.

        FAILURE
        09:16 IST, option chain still fallback/stale so `intelligence_chain` is None, 1-min frame
        has <5 bars so OrderFlowAnalyzer returns available=False, index has no traded volume so
        `volume_available` is False. RegimeDetector returns TRENDING_UP. max_possible=0.25,
        score=0.25 (+0.07 if aroon/structure agree), averaged_score clamps to 1.0. The aggregate
        publishes score=100.0 / STRONG_BUY on the least-informed read of the session, clearing every
        `min_intel_score` gate in scanner.py (0.20 missed-signal reentry at :16803, 0.20 choppy
        scalp at :20403, even the 0.70 rare-burst gate at :20475) on the strength of one engine.

  --------------------------------------------------------------------------------------------------

====================================================================================================
PART 2 -- FOUND OUTSIDE THE AGENT SWEEP
====================================================================================================

These came from reproducing the failing test and tracing the shipped config myself.
No point-scoped agent reported them.

  [HIGH]  Two option trades on one index in different timeframes are refused
        scanner.py:21226

        The cross-TF complement rule computes target_type = "OPT" if existing.inst_type == "FUT"
        else "FUT", so a second OPT candidate is forced to FUTURES; _force_candidate_instrument_type
        then refuses when the futures token is missing (scanner.py:20126). Requirement 3 asks for
        one trade per timeframe per index (3 per index, 12 total). Outside the cascade branch that
        collapses to one trade per index.

        EVIDENCE
        Reproduced directly in a Python session:
          existing=OPT@15min, candidate=OPT@5min, same direction -> False
          existing=FUT@15min, candidate=OPT@5min                 -> True
        Log line: "Concurrency Guard: cannot convert NIFTY to FUT; missing futures token."

  --------------------------------------------------------------------------------------------------

  [MEDIUM]  A stale code comment inverts the premise the guard was built on
        scanner.py:21209

        The comment asserts that .env sets UT_CONCURRENCY_GUARD='False', "so it was dead at
        runtime". Actual: .env:30 sets 'True', and get_settings().ut_concurrency_guard resolves to
        True.

        EVIDENCE
        This matters because everything below "if not guard_on: return True" -- the complement rule,
        the grade comparison, the instrument-preference forcing -- is a LIVE production path, not
        the dormant one the comment describes. The finding above is therefore reachable in
        production.

  --------------------------------------------------------------------------------------------------

  [HIGH]  DELETE + VACUUM runs on every startup, including a mid-session restart
        data/sqlite_db.py:113

        _init_db calls cleanup_old_records() whenever not running under pytest. That deletes from
        three tables and then runs VACUUM, which rewrites the entire database file under an
        exclusive lock.

        EVIDENCE
        Restart at 13:00 with the market open and the system blocks on a full vacuum of the live
        ledger before it can resume trading. Bears on requirement 15 (maintenance scheduling) and
        requirement 20 (restart).

  --------------------------------------------------------------------------------------------------

  [MEDIUM]  Retention cutoffs are UTC while the rows are IST
        data/sqlite_db.py:124

        The cleanup compares against SQLite date('now', ?) and datetime('now', ?). SQLite 'now' is
        UTC; rows are written on IST wall-clock.

        EVIDENCE
        A 5h30m skew at every retention boundary. Bears on requirement 15 and requirement 16.

  --------------------------------------------------------------------------------------------------

  [MEDIUM]  The branch ships one failing test
        tests/test_a_flip_is_not_a_flicker_2026_08_05.py:322

        Commit f70d31e ("Close the last four sweep items") tightened
        _force_candidate_instrument_type to refuse conversion without a genuine index spot. The test
        predates it (commit 690ea22) and stubs a bare Scanner.__new__(Scanner) with no entry_spot,
        no candles and no market_info, so it now returns False where it asserts True.

        EVIDENCE
        Baseline: 1 failed, 2584 passed, 12 skipped. Behaviour changed and the test was not updated
        with it. This is the only red test in the suite.

  --------------------------------------------------------------------------------------------------

====================================================================================================
PART 3 -- THE SWITCHED-OFF PATTERN  (the single most important theme)
====================================================================================================

The most common reason a requirement failed is NOT missing code. It is code that is written,
tested, and disabled in the shipped .env. A code-only read would score these as met.
All values below were resolved live via get_settings().

    Req 3  cascade +1 lot   SCALE_IN_ENABLED='False'          (.env:762)  -> scale_in_enabled = False
    Req 5  per-lot ladder   PROFIT_LOCK_PER_LOT_BASIS='False' (.env:751)  -> ladder is per-TRADE, not per-lot
    Req 8/26 per-TF grade   per_timeframe_grading_enabled = False         -> 15min graded on the 5min regime
    Req 12 hedging          PULLBACK_HEDGE_ENABLED='False'    (.env:690)
    Req 12 pullback route   SCANNER_ENGINE='utbot'            (.env:4)    -> pullback lives in quality_hybrid
    Req 11 MACD pricing     MACD entry-pricing path off in live config

  Requirement 12 deserves special mention -- engine/safe_mode.py:454 says it outright:

      "Hedging is DORMANT: _pullback_hedge_arm/_pullback_hedge_release are written and tested, but
      no code path places the second leg. The setting is a live toggle in the dashboard, so an
      operator can switch it on and reasonably believe hedging is now running."

  So turning PULLBACK_HEDGE_ENABLED on would still not place a hedge leg.

  Also surfaced by running validate_real_mode_readiness(get_settings()) today:
    WARNING  broker free cash is 0.00, below the Rs.2,400 needed for the cheapest one-lot option
             entry (SENSEX 20 x Rs.120 assumed ATM premium) -- every entry will be refused
             until the account is funded
    WARNING  fyers token is from 2026-08-08 (not refreshed today)
    (unreachable_intel_gates(settings) returns [] -- no dead intel gate in the shipped config)

====================================================================================================
PART 4 -- ALL 28 POINTS IN FULL
====================================================================================================

----------------------------------------------------------------------------------------------------
POINT 1  [PARTIAL]  Pre-market boot, 09:00 warmup, 09:08 settle, pre-open read
----------------------------------------------------------------------------------------------------

WHAT THE CODE ACTUALLY DOES
    Boot ordering is correct: `create_system` builds data provider, broker, candle builder, MTF
    engine, signal manager, intelligence, trade manager and scanner from `get_settings()`
    (.env-restored values), `TradeManager.__init__` calls `load_state()`
    (trading/trade_manager.py:612) and `Scanner.__init__` ends with `pre_market_check()` +
    `_restore_warm_memory()` (scanner.py:554-555) — all before `scanner.run()` is launched
    (main.py:953-966). The 09:00 warmup IS wired to a real clock, not hardcoded/dead:
    `start_premarket_real_mode_worker` sleeps to `auto_real_mode_premarket_switch_time`
    (main.py:635-680) and the scanner's `_is_real_mode_premarket_arm_window` (09:00:00 ≤ t <
    09:15:00) drives `_ensure_market_session_refresh`, which schedules a full
    `_morning_market_refresh` warming expiry, 1/5/15min history, index quotes, prev-closes, futures,
    ATM option LTPs, chains, and runs `intel.analyze` for 5min/15min with `price_change_pct` against
    previous close — so the pre-open change-vs-last-session and regime read (claim d) genuinely
    exist before 09:15. The 09:08 settlement boundary is implemented in three places
    (`_dashboard_quote_session`, `_preopen_rest_quote_allowed`, safe_mode phase labels). What is
    broken is claim (b)/(c) at the dashboard: during 09:00-09:15 in REAL mode with no open trades,
    `_scan_cycle` takes the closed-market idle branch and re-broadcasts the *previous* `instruments`
    dict verbatim, so the 4-index spot panel is frozen at its last pre-09:00 snapshot and never gets
    a PRE-OPEN/SETTLE label. The morning-refresh attempt cap can also permanently disable the 09:15
    catch-up it was written to feed.

EVIDENCE IN CODE
    - main.py:692-905 - create_system(): settings -> data_provider.connect() -> readiness/lease
    gates -> broker -> candle builder -> MTF engine -> signal manager -> intelligence ->
    TradeManager -> Scanner -> scanner.configure(capital/risk); scanner.run() only started later at
    main.py:953-966
    - trading/trade_manager.py:612 - self.load_state() inside TradeManager.__init__ restores
    prior-session trades before any scan
    - scanner.py:554-555 - Scanner.__init__ ends with self.market_info =
    self.expiry.pre_market_check(); self._restore_warm_memory()
    - main.py:635-680 - start_premarket_real_mode_worker sleeps until
    auto_real_mode_premarket_switch_time (default 09:00:00), then retries _auto_real_mode_attempt
    every retry_seconds; genuinely clock-driven
    - scanner.py:4294-4299 - _is_real_mode_premarket_arm_window: real_mode_premarket_arm_start
    (09:00:00) <= now < real_mode_live_start (09:15:00)
    - scanner.py:4425-4441 - pre-open branch of _ensure_market_session_refresh labels 'pre-open
    settlement' (<=09:08), 'pre-open analysis' (<=09:14:30), 'market-open handoff' and schedules the
    warm refresh
    - scanner.py:4795-4845 - morning refresh computes previous_close/_price_change_pct_for and runs
    intel.analyze for 5min/15min, caching regime/PCR/OI before 09:15
    - scanner.py:5779-5790 - _closed_market_idle_allowed(): True in REAL once
    _initial_setup_completed and no open trades / no active session candidates (the 09:00-09:15
    state after _rollover_session_if_needed clears candidates at scanner.py:868-883)
    - scanner.py:5993-6015 - `if not is_market_open and self._closed_market_idle_allowed():` copies
    dict(previous['instruments']) and returns before any quote fetch
    - scanner.py:5024 - _background_ltp_worker: `if not self.data.is_market_open() and not
    self.trades.open_trades: sleep(5); continue` -> no pre-open LTP polling either
    - scanner.py:9675 + 4324-4340 - _dashboard_quote_session (the only producer of
    is_preopen/preopen_settlement) is called only inside _process_instrument, which the idle branch
    skips
    - scanner.py:4257-4266 vs 4402-4420 - attempt cap sets _morning_refresh_last_date = session_day,
    and _ensure_market_session_refresh returns False at its first check (4384-4386) before the 09:15
    catch-up branch

FINDINGS (4)

  [HIGH    ] bug      scanner.py:5993
    Pre-open scan cycle short-circuits and replays the previous instruments dict; 4-index panel
    frozen

      `is_market_open` is hard-coded 09:15-15:30 (data/market_data.py:1131-1143), so the whole
      pre-open window counts as CLOSED. In _scan_cycle: `if not is_market_open and
      self._closed_market_idle_allowed():` then `results.update({"instruments":
      dict(previous.get("instruments") or {}), ...}); ... return` (scanner.py:5993-6015).
      `_closed_market_idle_allowed` (scanner.py:5779-5790) returns True whenever mode==REAL, initial
      setup is done, there are no open trades and no active session candidates — precisely the state
      at 09:00 after `_rollover_session_if_needed` clears `_session_trade_candidates`
      (scanner.py:868-883). The only runtime callers of `data.get_index_quote` are
      `_process_instrument` (scanner.py:9275) and the morning refresh (scanner.py:4625, which never
      publishes into `results`), and the panel is fed exclusively from the broadcast/dashboard_cache
      payload (dashboard/server.py:1449-1451; dashboard/static/js/app.js:1085-1096 reads
      instruments[name].ltp/change_pct). `_background_ltp_worker` also idles pre-open
      (scanner.py:5024). Net: nothing refreshes the panel between 09:00 and 09:15.

      FAILURE
      App started 08:30 on a trading day; initial setup completes; premarket worker arms REAL at
      09:00:00. From 09:00 to 09:15 every scan tick hits the idle branch, so
      price_NIFTY/price_BANKNIFTY/price_SENSEX/price_MIDCPNIFTY keep showing the 08:30 snapshot
      (yesterday's close) with market_status CLOSED, while real pre-open ticks and the 09:07
      settlement print are visible in the broker terminal. The operator sees a dead panel for the
      entire 15-minute pre-open and gets no gap read until the first post-09:15 scan.

  [MEDIUM  ] bug      scanner.py:4331
    PRE-OPEN / 09:08 SETTLE labels computed only in _process_instrument, unreachable during pre-open

      `_dashboard_quote_session` (scanner.py:4302-4340) overrides the card label to PRE-OPEN and
      sets `is_settlement = now <= real_mode_premarket_settle_end (09:08:00)`, and the UI has
      dedicated states for it (`normalizeQuoteLabel` returns 'SETTLE' on `preopen_settlement ===
      true`, dashboard/static/js/app.js:1125-1132). Its single call site is scanner.py:9675 inside
      `_process_instrument`, which is only reached on the full scan path — the same path the
      closed-market idle branch at scanner.py:5993 bypasses for the whole 09:00-09:15 window.
      `results['market_status']` is also fixed to 'CLOSED' at scanner.py:5988-5990 before the idle
      return. The 09:08 transition therefore exists in code but is not observable unless an open
      trade or live candidate forces the full path.

      FAILURE
      On a normal session start (no overnight position, no carried candidate), at 09:07:59 the
      dashboard shows CLOSED/REF instead of SETTLE, and at 09:08:01 there is no visible transition
      to pre-open analysis — the tab label only changes after 09:15 when is_market_open flips. An
      operator relying on the SETTLE badge to confirm the settlement print gets no signal at all.

  [MEDIUM  ] bug      scanner.py:4257
    Morning-refresh attempt cap marks the day complete, making the 09:15 catch-up unreachable

      `_schedule_morning_market_refresh` escapes the retry loop with `if used >= max_attempts and
      not force_full:` -> `self._morning_refresh_last_date = session_day` plus
      `summary['accepted_incomplete'] = True` (scanner.py:4257-4266). But
      `_morning_refresh_is_complete_for_day` is exactly `self._morning_refresh_last_date ==
      session_day` (scanner.py:4177-4178), and `_ensure_market_session_refresh` returns False on
      that check at its first line (scanner.py:4384-4386), before the `incomplete and
      catchup_enabled and not catchup_done and now >= live_start` branch at scanner.py:4402-4420 and
      before the mid-session `live_incomplete_partial` branch. Nothing else clears
      `_morning_refresh_last_date` within the day, so the catch-up whose own log line reads
      're-warming missing morning keys after incomplete premarket pass' can never fire once the cap
      has been hit pre-open.

      FAILURE
      Pre-open, NFO/BFO option chains are empty before 09:15, so with
      morning_refresh_require_all_chains=True attempts 1-3 all return INCOMPLETE with missing_keys
      ['chains','chain:NIFTY',...] within the first minute or two after 09:00. The 4th schedule
      (force_full=False) hits the cap, sets last_date=today, and the day is treated as warmed. At
      09:15 the designed catch-up pass is skipped entirely, so chains/intel for the missing indices
      are never re-warmed by the refresh path.

  [MEDIUM  ] partial  scanner.py:3128
    Warm-memory restore runs once in __init__ and only if boot mode is REAL; runtime arm skips it

      `_restore_warm_memory()` has exactly one call site, the last line of Scanner.__init__
      (scanner.py:555), and it no-ops unless `_warm_memory_allowed()` is true, which is `mode ==
      'REAL'` (scanner.py:3127-3128). The documented normal path is to boot in HISTORICAL and flip
      at 09:00 — main.py:567-631 `_auto_real_mode_attempt` ('Runtime-only HISTORICAL -> REAL/manual
      premarket arm') — and create_system itself downgrades `settings.trading_mode` to HISTORICAL on
      risk-config, safe-mode readiness or execution-lease failure (main.py:706-711, 765-772,
      806-812). `scanner.configure(mode='REAL', ...)` does not call `_restore_warm_memory`, so the
      crash-recovery payload (recent candles, session candidates, pending stabilization buffers) is
      never loaded on that path.

      FAILURE
      Process crashes at 10:20 with two pending stabilization buffers and live session candidates.
      Restart at 10:22 fails the Kite execution-lease check, so create_system forces HISTORICAL; the
      operator completes the Kite login and switches to REAL at 10:24. The warm-memory file is still
      inside its 15-minute TTL but is never read, so pending arrows and restored candidate rows are
      silently lost and the scanner rebuilds from cold.


----------------------------------------------------------------------------------------------------
POINT 2  [PARTIAL]  Both-broker data -> 3-TF scan -> buffer -> intel filter -> accept/reject/wait
----------------------------------------------------------------------------------------------------

WHAT THE CODE ACTUALLY DOES
    The birth path exists end-to-end and is largely correct in the default INTRACANDLE fire mode:
    candidates are built per index across 1/5/15min (scanner.py:6505), one winner per index is
    picked by (timestamp, score) descending (signal_processor.py:4573-4574) plus cascade siblings,
    and `_coordinate_and_execute` splits INTRA-CANDLE signals into the anti-repaint buffer while
    CLOSED-CANDLE signals take a direct path (scanner.py:23266-23316) that still applies the entry
    cutoff and the opposite-buffer eviction before acceptance. The "wait for maturity" state is
    genuinely re-evaluated: the caller invokes `_coordinate_and_execute` whenever `candidates or
    self._pending_live_signals` is non-empty (scanner.py:6263), so a buffered signal is re-checked
    every scan and only leaves the buffer via an explicit path (future timestamp,
    `max_pending_seconds = tf*60+90`, repaint VANISHED/FLIPPED, entry cutoff, or maturity) — nothing
    is dropped on the floor. Two real gaps: (a) every per-candidate `_entry_cutoff_allows_candidate`
    call lives inside the `intrabar_enabled`-gated block, so in CANDLE_CLOSE fire mode the cutoff is
    never applied per candidate; (b) the buffered candidate object is frozen at buffer time, so the
    intelligence-derived grade/confidence the post-buffer filter judges is 20-30s stale. Note also
    that "both brokers" today means Kite (execution) + Fyers (cross-check at
    data/market_data.py:1254-1282); AngelOne is retired and `self.smart_api` is permanently None
    (data/market_data.py:203-208).

EVIDENCE IN CODE
    - scanner.py:23244 - the entire stabilization block, including all three per-candidate cutoff
    calls, is guarded by `if self.mode != "HISTORICAL" and not is_warmup and intrabar_enabled:`
    - scanner.py:23266 - `if not bool(getattr(c, 'entered_intracandle', True)):` is the
    closed-candle fast path that skips the buffer
    - scanner.py:23279 / 23307 - fast path still runs `_entry_cutoff_allows_candidate` and
    `_evict_superseded_opposite_buffer` before appending to matured_candidates
    - scanner.py:23207 - the only cutoff call outside the intrabar block is a wave-level `any(...)`
    behind `session_now.time() >= final_entry_time` (ut_5min_no_entry_after)
    - scanner.py:6263 - `if candidates or self._pending_live_signals:` ->
    `_coordinate_and_execute(candidates)`; pending buffers are re-evaluated even on a scan that
    produced no new candidates
    - scanner.py:23363-23551 - pending loop: future-ts discard, max_pending expiry, repaint/flip
    gate, momentum override, stability gate, matured cutoff re-check
    - scanner.py:23329-23347 - the candidate object is stored once (`if key not in
    self._pending_live_signals`) and never refreshed while buffered
    - engine/signal_processor.py:591 - `_live_signal_recheck_due` returns False whenever `pending`
    is True, so a buffered signal is not re-emitted/re-graded during the window
    - scanner.py:13428-13453 - `_apply_stabilization_preflight` copy_fields carries
    contract/quote/price/stop/target only; grade, confidence, intel score are absent
    - scanner.py:16004-16043 - `_live_signal_processing_deadline` grants a 300s post-close grace for
    5min/15min when the scanner was already running
    - config/settings.py:1185,1346,1492 - ut_no_entry_after="15:01",
    live_15min_entry_cutoff_grace_seconds=90.0, live_signal_post_close_grace_seconds_15min=300.0
    - engine/signal_processor.py:4202-4217,4306 - CANDLE_CLOSE mode defers a partial-candle signal
    without advancing the dedup lock; the lock advances only when the candidate is actually emitted

FINDINGS (3)

  [HIGH    ] bug      scanner.py:23244
    CANDLE_CLOSE mode never applies the per-candidate entry cutoff; a late-processed 15min bar still
    enters

      All three per-candidate calls to `_entry_cutoff_allows_candidate` (scanner.py:23279 fast path,
      :23324 pre-buffer, :23527 matured) sit inside the block guarded at :23244 by `if self.mode !=
      "HISTORICAL" and not is_warmup and intrabar_enabled:`. When `Settings.get_signal_fire_mode()`
      returns CANDLE_CLOSE (`live_intrabar_signal_entries=False`, user-settable via
      `set_signal_fire_mode`, config/settings.py:1842), `intrabar_enabled` is False, the whole block
      is skipped and `_pending_live_signals` is simply cleared at :23241. The only surviving cutoff
      call is scanner.py:23207, and it is (a) a wave-level `any()` across all candidates, not per
      candidate, and (b) only reached when `session_now.time() >= final_entry_time` i.e.
      ut_5min_no_entry_after = 15:14. The upstream guard is `after_no_entry`, computed in
      engine/signal_processor.py:4220 from `sig.timestamp.time() >= no_entry_after` — the BAR's
      timestamp, not the wall clock — so it cannot see a late-processed bar. No downstream gate
      re-checks it: grep of trading/trade_manager.py, engine/entry_risk_policy.py and
      engine/risk_manager.py for ut_no_entry_after returns nothing, and the remainder of
      `_coordinate_and_execute` (23565-24158) contains no cutoff check. The comment the authors
      wrote at scanner.py:23270-23273 ("The wave-level check above is an any() across all
      candidates, so it does not gate this one") documents exactly this hazard for the intrabar
      path, but the fix was placed inside the intrabar-only block.

      FAILURE
      CANDLE_CLOSE mode. NIFTY 15min bar timestamped 14:45 prints a BUY; signal_processor defers it
      (4202-4217) until the bar closes at 15:00. A slow refresh means it is emitted at 15:03:30 —
      still inside `_live_signal_processing_deadline` = close +
      live_signal_post_close_grace_seconds_15min (300s) = 15:05:00 (scanner.py:16016-16025,
      settings.py:1492), so the ghost guard passes. `after_no_entry` is False because 14:45 < 15:01.
      In `_coordinate_and_execute`, `_candidate_source_is_intrabar` is False (bar closed), the
      candidate joins timing_eligible, the :23244 block is skipped, and :23207 is not reached
      because 15:03:30 < 15:14. `_entry_cutoff_allows_candidate` would have returned False (now
      15:03:30 > cutoff 15:01 + 90s grace), but it is never called. A fresh long CE/PE position
      opens ~15:03:30, past the configured 15min no-entry policy and ~21 minutes before the 15:25
      hard square-off. In the default INTRACANDLE mode the identical candidate is correctly rejected
      at scanner.py:23279.

  [MEDIUM  ] partial  scanner.py:23329
    Buffered candidate is frozen at t0: grade/confidence/intel are not recomputed at maturity

      The claim is buffer/stabilise -> intel filter. In code the intelligence work happens BEFORE
      the buffer and is never redone. `self._pending_live_signals[key] = {..., "candidate": c, ...}`
      is written only when `key not in self._pending_live_signals` (scanner.py:23329-23347), so the
      stored TradeCandidate — with its `grade`, `confidence`, `graded_intel_score`, `graded_regime`,
      `adx_value` computed in engine/signal_processor.py:4267-4292 — is the t0 object. While the key
      is pending, engine/signal_processor.py:591 (`if not eligible or pending: return False`) blocks
      `recheck_due`, so the signal is not re-emitted and not re-graded during the window. The only
      in-window validation is `_repaint_signal_status` (scanner.py:23411 -> :5488), which is
      explicitly identity-only: it matches on timestamp + signal_type and states at :5516-5518 that
      price is intentionally ignored. `_apply_stabilization_preflight` (scanner.py:13428-13453)
      refreshes contract/quote/price/stop/target but its copy_fields list contains no grade,
      confidence or intel field. So the post-buffer 'intel filter' — the grade-preference gate at
      scanner.py:23731 — judges a grade that is `live_signal_stabilization_seconds` (20-30s) old.
      Only `latest_regimes` is read fresh (scanner.py:18554 inside
      `_passes_candidate_grade_preference`).

      FAILURE
      BANKNIFTY 5min LONG is buffered at 11:00:00 with intel score +45, graded A, confidence 0.78.
      Over the next 30s the OI/PCR aggregate flips hard negative (intel score -35) and volume dries
      up; a scan at 11:00:20 recomputes the bundle and would now grade the same arrow C. The UT1
      arrow on the 11:00 bar has not moved, so `_repaint_signal_status` returns PRESENT,
      `_stabilization_ready` passes at 11:00:30, and the candidate matures still carrying grade A /
      confidence 0.78. `_passes_candidate_grade_preference` sees rank(A)=3 >= min_rank and admits
      it; the trade opens on intelligence that was already invalidated 10 seconds earlier. The
      stabilization window therefore buys repaint protection but no intelligence protection, which
      is the part of the claim that is uncovered.

  [LOW     ] bug      scanner.py:23709
    Dead grade-rank block in the accept path: sig_rank/min_rank/is_choppy computed, never read

      scanner.py:23709-23729 builds `grade_hierarchy`, `base_grade`, `sig_rank`, `current_regime`,
      `regime_adaptation`, `is_choppy` and a full `min_rank` ladder (including the 'auto' rule
      `min_rank = 3 if is_choppy else 2`). The very next statement, :23731, delegates the decision
      entirely to `self._passes_candidate_grade_preference(sig)`, which recomputes its own
      grade_hierarchy/base_grade/sig_rank and its own regime at scanner.py:18532-18535 and :18554.
      Grepping 23705-24158 shows `min_rank` and `sig_rank` are assigned and never referenced again.
      The block is a no-op.

      FAILURE
      An operator tuning the choppy-regime grade floor edits `min_rank = 3 if is_choppy else 2` at
      scanner.py:23729, restarts, and observes zero behaviour change because the effective floor
      lives in `_passes_candidate_grade_preference` (scanner.py:18566+). No money is lost directly,
      but the visible knob is inert and the two copies of the ladder can silently diverge.


----------------------------------------------------------------------------------------------------
POINT 3  [PARTIAL]  Simultaneous signals add +1 lot to the running trade, not a second trade
----------------------------------------------------------------------------------------------------

WHAT THE CODE ACTUALLY DOES
    The cascade-add mechanism exists end to end: `Scanner._offer_cascade_add` (scanner.py:21040)
    matches a blocked duplicate against open trades on (base index, timeframe, direction) —
    deliberately NOT on strike or option type — and calls `TradeManager.scale_in_trade(trade_id,
    None, price, "CASCADE_ADD")`, so a duplicate at a different strike folds into the running trade
    and an opposite CE/PE is correctly refused as a reversal rather than a reinforcement. It is
    wired at two call sites (scanner.py:23856 after the concurrency guard,
    engine/signal_processor.py:4741 on the quad-prime same-TF/overlapping-source branch), both gated
    on auto_mode, and scanner.py:21217 now blocks a same-timeframe duplicate unconditionally so it
    is routed to the add path. The caps are self-consistent with the target shape: cascade per-index
    cap 3 primary + 2 dual = 5 (scanner.py:20651), CORE bucket 12 / MIDCAP 3 (scanner.py:21348,
    21339 — CORE is only NIFTY/BANKNIFTY/SENSEX, so 9 core + 3 midcap = the 12 signal-trades),
    global ceiling raised 12 -> 18 during a cascade wave (trade_manager.py:3444). What is NOT
    delivered: the add is disabled in the shipped config; the "simultaneous" case is structurally
    impossible because the add requires the running trade to already be +0.75R; reconstructed ghost
    rows can swallow the add; ordinary (non quad-prime) duplicates never reach the add path at all;
    and the same-timeframe dedupe inspects only `existing[0]`, so a second trade on the same
    index+timeframe can still slip through.

EVIDENCE IN CODE
    - C:/Users/sagnik/Desktop/rp46-3/scanner.py:21040-21094 - _offer_cascade_add matches open trades
    on base index + timeframe + direction and calls scale_in_trade(trade_id, None, price,
    'CASCADE_ADD'), returning on the FIRST match
    - C:/Users/sagnik/Desktop/rp46-3/scanner.py:23830-23858 - call site: a candidate blocked by
    _prepare_candidate_for_concurrency is offered to the running trade; manual mode declines with a
    log instead of adding
    - C:/Users/sagnik/Desktop/rp46-3/scanner.py:21217-21218 - 'same timeframe already active' block
    is now unconditional (above the `if not guard_on: return True`), which is what routes duplicates
    into the add path
    - C:/Users/sagnik/Desktop/rp46-3/scanner.py:21207 - existing_trade = existing[0]; the
    same-timeframe test reads only the first open trade on the index
    - C:/Users/sagnik/Desktop/rp46-3/scanner.py:21106 - `existing` is built from open_trades with no
    is_ghost filter, unlike _opposite_direction_stack_block (scanner.py:21024) and
    signal_processor._is_live_book_trade
    - C:/Users/sagnik/Desktop/rp46-3/scanner.py:407-467 - on every launch (mode HISTORICAL, pinned
    by .env TRADING_MODE) session candidates are reconstructed as is_ghost=True Trade rows with
    status 'OPEN' inserted into trades.open_trades
    - C:/Users/sagnik/Desktop/rp46-3/engine/signal_processor.py:4728-4773 - the add is offered only
    when quad_prime_entry AND same_tf AND source_overlaps; otherwise the loop `continue`s
    - C:/Users/sagnik/Desktop/rp46-3/engine/signal_processor.py:4790-4793 - any non quad-prime
    candidate on an index that already has a live open trade returns [] ('REJECTED - Existing Active
    Trade') before _coordinate_and_execute is ever reached
    - C:/Users/sagnik/Desktop/rp46-3/trading/trade_manager.py:1823-1824 - scale_in_trade returns
    (False, 'scale-in disabled') when scale_in_enabled is off
    - C:/Users/sagnik/Desktop/rp46-3/trading/trade_manager.py:1899-1936 - _scale_in_is_earned
    refuses unless open profit >= scale_in_min_profit_r (0.75R) measured against frozen entry risk
    - C:/Users/sagnik/Desktop/rp46-3/.env:762,765 - SCALE_IN_ENABLED='False',
    SCALE_IN_MIN_PROFIT_R='0.75' (config/settings.py:1279 default is also False)
    - C:/Users/sagnik/Desktop/rp46-3/scanner.py:20651-20654 & 21332-21350 &
    C:/Users/sagnik/Desktop/rp46-3/trading/trade_manager.py:3437-3459 - cascade per-index cap 3+2,
    CORE bucket 12 / MIDCAP 3, global max positions raised to cascade_max_concurrent_positions=18

FINDINGS (5)

  [HIGH    ] partial  trading/trade_manager.py:1907
    Simultaneous duplicate can never add: the add needs the running trade already at +0.75R

      _offer_cascade_add delegates the whole 'whether' decision to scale_in_trade, and
      scale_in_trade -> _scale_in_is_earned refuses unless open profit is >= scale_in_min_profit_r
      (0.75, live in .env:765): `open_r = (signed * max(1, qty) * mult) / risk; if open_r < min_r:
      return False, f"open profit {open_r:.2f}R below {min_r:.2f}R"`. A second signal fired on the
      SAME bar as the first meets a trade that was just filled at ~0.00R. It also requires
      trade.market_regime to be explicitly trending (lines 1938-1947: an empty or RANGING regime
      returns 'regime ... is not trending'). Because scanner.py:21217 now blocks the same-timeframe
      duplicate unconditionally, the refusal is terminal: the duplicate is neither added nor opened
      as a second trade, it is logged as 'cascade add declined' and dropped
      (scanner.py:23859-23879). The claimed behaviour therefore only fires for a LATER same-chart
      signal on a trade that has already run 0.75R, not for the simultaneous case the requirement
      names.

      FAILURE
      09:20:00 quad and dual scanners both emit NIFTY 5min LONG. The first opens 1 lot at premium
      200 with stop 170 (1R). The duplicate arrives in the same cycle; scale_in_trade computes
      open_r = 0.00 < 0.75 and returns (False, 'open profit 0.00R below 0.75R'). Result: 1 lot
      instead of the intended 2, and the second signal is discarded.

  [HIGH    ] bug      scanner.py:21067   [RE-VERIFIED]
    _offer_cascade_add does not skip ghost rows, so a virtual trade absorbs the live add

      The match loop is `for trade_id, trade in list(getattr(self.trades, 'open_trades',
      {}).items())` with only base-index / timeframe / direction filters - no `is_ghost` check and
      no `status == 'OPEN'` check - and it `return`s on the first match. Every sibling guard filters
      ghosts (_opposite_direction_stack_block skips is_ghost at scanner.py:21024; signal_processor
      uses _is_live_book_trade at 4656; _bucket_open_count at 21356 and _effective_max_positions at
      trade_manager.py:3453 exclude them). But scanner.py:407-467 inserts reconstructed
      is_ghost=True rows with status 'OPEN' into trades.open_trades at __init__ on every launch
      (mode HISTORICAL is pinned in .env:133, REAL arms later in the same process), so those ghosts
      sit AHEAD of any real trade in dict insertion order. scale_in_trade then takes the not-is_real
      branch (trade_manager.py:1884-1893), books the lot virtually and returns (True, 'booked'); the
      scanner logs 'Cascade add: extra lot on ...' and `continue`s, so the real running trade gets
      nothing and the signal is consumed.

      FAILURE
      Launch reconstructs a ghost NIFTY 5min LONG row from the session signal ledger. At 10:05 a
      real NIFTY 5min LONG trade is running with 2 lots. A duplicate NIFTY 5min LONG fires;
      _offer_cascade_add hits the ghost first, books a paper lot, logs a successful cascade add, and
      the live 2-lot position is never increased - while the operator log claims it was.

  [HIGH    ] partial  engine/signal_processor.py:4790
    Only quad-prime same-source duplicates reach the add; ordinary duplicates are dropped upstream

      process_best_signal loops the open book (signal_processor.py:4654) and, for a live trade on
      the same instrument, only the `quad_prime_entry and same_tf and source_overlaps` branch offers
      _offer_cascade_add (4728-4758). Every other same-index candidate falls through to
      `_emit_signal_decision(..., f'Already has active {trade.timeframe} {trade.direction} trade
      {tid}', 'REJECTED - Existing Active Trade'); return []` at 4790-4793 - it never reaches
      _coordinate_and_execute, which is the only other place the add is offered (scanner.py:23856).
      So a duplicate produced by the solo/dual engine (non quad-prime), or a quad-prime duplicate
      whose source set does NOT overlap the running trade's (4728 `continue`), gets no cascade add
      from this path at all. The claim 'when quad/dual scanners produce more than one signal for the
      same index+timeframe, add +1 lot' is covered for the quad-prime same-source shape only.

      FAILURE
      BANKNIFTY 15min LONG is running from the quad scanner. The dual/adaptive engine fires its own
      BANKNIFTY 15min LONG. quad_prime_entry is False for that candidate, so process_best_signal
      returns [] at 4793 with 'REJECTED - Existing Active Trade'; no lot is added and no cascade-add
      decision line is produced.

  [HIGH    ] partial  trading/trade_manager.py:1823   [RE-VERIFIED]
    Cascade adds are inert in shipped config (SCALE_IN_ENABLED='False'); duplicate is discarded

      scale_in_trade's first statement is `if not _setting_enabled(getattr(settings,
      'scale_in_enabled', False), False): return False, 'scale-in disabled'`.
      config/settings.py:1279 defaults it to False and .env:762 pins SCALE_IN_ENABLED='False', so
      every _offer_cascade_add call returns (False, 'scale-in disabled'). Combined with the newly
      unconditional same-timeframe block at scanner.py:21217 - which previously sat under `if not
      guard_on: return True` - the live behaviour today is strictly worse than 'add a lot': the
      duplicate is blocked, the add is refused as disabled, and the candidate is rejected at
      scanner.py:23873 with 'Concurrency guard blocked it'. The target shape of 12 signal-trades
      each taking +1 lot cascade adds cannot occur until this flag is flipped, and no code path
      enables it at runtime.

      FAILURE
      Aligned trend day, auto mode on. NIFTY 5min LONG is running at +1.2R and a confirming
      quad-prime same-source duplicate fires. _offer_cascade_add asks scale_in_trade, which returns
      'scale-in disabled'; the decision log shows 'cascade add declined (scale-in disabled)' and the
      position stays at its original lot count for the whole wave.

  [MEDIUM  ] bug      scanner.py:21207
    Same-timeframe dedupe inspects only existing[0], so a second same-TF trade can open

      After the cap and lane branches the guard does `existing_trade = existing[0]` and then `if
      getattr(existing_trade, 'timeframe', '') == candidate.timeframe: return _block('same timeframe
      already active')` (21217). It compares against the FIRST open trade on the index only, never
      the rest of `existing`. Whenever the per-index cap is above 2 - cascade multi-TF stack cap 5
      (scanner.py:20651), dual-mode cap 3 (20656-20666), rare-burst cap 3 (20669-20673) - two or
      more trades can be open on one index, and if existing[0] holds a different timeframe the
      duplicate passes this test, then passes _candidate_quality_same_or_better and is merely forced
      to the complementary instrument type (21226-21231) and admitted. The cascade branch at
      21185-21205 does scan all of `existing`, but it only runs when candidate.one_way_cascade_entry
      is set, so a non-cascade duplicate on a rare-burst or lane-B index escapes the dedupe entirely
      - the 'second trade at a different strike' outcome the requirement forbids, and it never
      reaches _offer_cascade_add either.

      FAILURE
      Rare-burst NIFTY: existing[0] = 15min LONG OPT, existing[1] = 1min LONG FUT (cap 3). A third
      candidate, NIFTY 1min LONG, arrives. Line 21217 compares it to the 15min row only, does not
      match, and the candidate is force-converted to OPT and opened - two 1min LONG NIFTY trades at
      different strikes instead of one trade plus a cascade add.


----------------------------------------------------------------------------------------------------
POINT 4  [PARTIAL]  Post-entry 20s re-scan to candle close; exit on repaint
----------------------------------------------------------------------------------------------------

WHAT THE CODE ACTUALLY DOES
    The mechanism exists and the 20s cadence is real: on broker acknowledgement of an intracandle
    entry, `_handle_broker_entry_result` spawns
    `asyncio.create_task(self._track_repaint_until_candle_close(trade, candidate))`
    (scanner.py:1016-1023), which is a dedicated task looping on `await
    asyncio.sleep(repaint_recheck_seconds)` (settings default 20.0, floored at 5s) until `now >=
    candle_close_time`, where `candle_close_time = signal_timestamp + timeframe`
    (scanner.py:23072-23087, 23169-23172). A repaint on a REAL trade does reach the broker: the
    abort branch calls `self._close_and_record(trade.id, price, "REPAINT_ABORT")` (23162) which, in
    auto mode, falls through to `_close_and_record_internal` -> `self.trades.close_trade(...)`
    (24933, 24943) and close_trade issues a broker exit in REAL mode (trade_manager.py:4276-4300).
    Three concrete defects remain. (1) The "re-scan" is not a scan: it re-reads
    `self._cached_results`, and a MISSING cache entry returns "UNKNOWN", which
    `_live_utbot_signal_still_present` maps to False and the tracker treats as a repaint - so any
    `_cached_results.clear()` (settings save, recalibration, rollover) makes live positions exit on
    no evidence. (2) The two operator limits that cap how long an option repaint abort may be
    deferred are structurally unreachable in the real-money path. (3) There is no rehydration path,
    so a restart mid-candle leaves an intracandle trade permanently unmonitored and permanently
    flagged "structurally unconfirmed".

EVIDENCE IN CODE
    - scanner.py:1021 - the tracker is spawned from exactly one place, on broker entry
    acknowledgement, only when trade.entered_intracandle is true
    - scanner.py:23077-23087 - recheck_seconds = max(5.0, settings.repaint_recheck_seconds or 20.0);
    dedicated asyncio task, real timer, not best-effort inside the scan loop
    - config/settings.py:1471 - repaint_recheck_seconds: float = 20.0 (post-entry re-check cadence);
    config/settings.py:1469 - repaint_check_interval_seconds: float = 20.0 (session-mirror cadence)
    - scanner.py:23086-23090 - while trade.status == 'OPEN': sleep, then still_present =
    self._live_utbot_signal_still_present(candidate, sig_timestamp)
    - scanner.py:23169-23172 - loop terminates at now >= candle_close_time and sets
    repaint_guard_checked=True; the final still_present evaluation happens on the same tick, so the
    close check is performed
    - scanner.py:23145-23168 - abort path: logs REPAINT ABORT, marks post-repaint recheck, refuses
    to close on a non-positive mark, else _close_and_record(..., 'REPAINT_ABORT')
    - scanner.py:24933-24943 - _close_and_record -> _close_and_record_internal ->
    self.trades.close_trade(tid, exit_price, reason)
    - trading/trade_manager.py:4276-4300 - close_trade computes requires_broker_exit for REAL
    non-ghost trades and errors to RECOVERY_REQUIRED without broker/token, i.e. the real broker exit
    path
    - scanner.py:5509-5513 - _repaint_signal_status returns 'UNKNOWN' when self._cached_results has
    no entry or an empty signals list
    - scanner.py:5542 - _live_utbot_signal_still_present returns status == 'PRESENT', so UNKNOWN is
    indistinguishable from VANISHED to the tracker
    - scanner.py:13743,13754 - _should_defer_option_repaint_abort reads _repaint_absent_checks /
    _repaint_first_absent_at off the passed object
    - trading/trade_manager.py:3230-3249 - rehydration comment states the async tracker 'is started
    from exactly one place - the entry acknowledgement - with no rehydration path'

FINDINGS (4)

  [CRITICAL] bug      scanner.py:23090   [RE-VERIFIED]
    An empty scan cache is treated as a repaint, so a cache clear market-exits live positions

      The tracker's only evidence is `self._live_utbot_signal_still_present(candidate,
      sig_timestamp)` (scanner.py:23090), which is `_repaint_signal_status(...) == 'PRESENT'`
      (5542). `_repaint_signal_status` reads `cache = getattr(self, '_cached_results', None) or {}`
      and returns 'UNKNOWN' when the `{instrument}_{timeframe}` key is absent or its signals list is
      empty (5509-5513). The docstring itself says "Callers treat it like VANISHED". The tracker has
      no freshness/populated-cache guard and no debounce: one False read goes straight to the abort
      branch (23144-23168). `_cached_results.clear()` is invoked while trackers are in flight from
      operator actions - configure/full-refresh on a settings save (scanner.py:3926),
      `perform_system_recalibration` (4027), explicit reset (3863), day rollover (883, 5904) and
      engine/scanner_recalculation.py:374. The session mirror at least brackets its close
      determination with `_repaint_close_check_in_window` (19899); the real-money tracker never
      calls it (grep: call sites are only 14589 and 19899).

      FAILURE
      NIFTY FUT long entered intracandle at 10:31:40 on the 10:30 5min bar, currently -6 points. At
      10:32:05 the operator saves any setting from the dashboard; the configure path hits
      `self._cached_results.clear()` (3926). At the tracker's next 20s tick (10:32:20) the cache is
      empty, `_repaint_signal_status` returns 'UNKNOWN', `still_present` is False. No mitigation
      applies (no stabilization override, not in profit, not an OPT so the defer returns False,''),
      so `_close_and_record(trade.id, mark, 'REPAINT_ABORT')` fires and close_trade sends a real
      market exit for a signal that never repainted.

  [HIGH    ] partial  scanner.py:23121
    option_repaint_abort_max_deferred_checks / _seconds are inert in the only path that closes a
    real position

      The tracker calls `Scanner._should_defer_option_repaint_abort(self, trade, mark, now, now >=
      candle_close_time)` (23121-23123) passing a `Trade`. Inside, the two persistence escape
      hatches read state off that object: `absent_checks = max(1, int(getattr(trade_like,
      '_repaint_absent_checks', 1) or 1))` (13743) and `first_absent = getattr(trade_like,
      '_repaint_first_absent_at', None)` (13754). Those attributes are written in exactly one place
      - `_check_session_candidates_breaches_locked` at 19812-19822, on the session/dashboard
      TradeCandidate, never on a Trade (grep over the repo returns only 19812-19822 as writers). So
      in the tracker `absent_checks` is pinned at 1 (never > max_deferred_checks=2,
      config/settings.py:904) and `first_absent` is always None (the max_deferred_seconds=55.0
      branch at 13759 is skipped entirely, config/settings.py:905). Both configured limits are
      unreachable for real money; only the severe-adverse branch (13737) can break the defer.

      FAILURE
      BANKNIFTY 52000CE bought at 10:31:10 off the forming 10:30 5min bar. The BUY arrow vanishes at
      10:31:30 and never returns. Every 20s tick the tracker gets still_present=False,
      `_should_defer_option_repaint_abort` returns True with 'check 1/2' (counters never advance),
      so hold_reason is set and no exit is taken. The operator's 55s / 2-check persistence limits
      say abort at ~10:32:25; the real position is instead carried to 10:35:00 (on a 15min signal,
      to +14.5 minutes), bleeding premium the whole way, while the session mirror row for the same
      signal exits on time.

  [MEDIUM  ] missing  trading/trade_manager.py:3238
    No repaint tracker after a restart, and repaint_guard_checked stays False for the rest of the
    session

      `_track_repaint_until_candle_close` is created only at scanner.py:1021 inside
      `_handle_broker_entry_result`; there is no other creation site in the repo. On restart,
      `load_state` sets `repaint_guard_checked = True` only if `datetime.now(IST) >= entry_time +
      timeframe` at load time (trade_manager.py:3238-3249). A trade restarted while its source
      candle is still forming therefore gets no monitor at all, and because nothing else ever sets
      the flag on a Trade (scanner.py:19767/19896 operate on session TradeCandidates, a different
      object), it stays False permanently. That value is consumed at scanner.py:12677-12683 as
      `repaint_window`, which arms the blunt sub-1R give-back guard and disables let-winners-run.

      FAILURE
      Process is restarted at 10:32 with an open NIFTY 5min intracandle entry from 10:31 on the
      10:30 bar. `now < entry+5min`, so repaint_guard_checked stays False and no tracker task is
      created. The signal repaints away at 10:33 and nothing exits the real position. For the
      remaining hours of that trade `repaint_window` stays True at 12681-12682, so the peak
      give-back guard remains armed and let-winners-run stays off on a position whose candle closed
      long ago.

  [LOW     ] bug      scanner.py:23165
    Tracker hardcodes REPAINT_ABORT instead of using _repaint_abort_exit_reason, mislabelling
    override failures

      `_repaint_abort_exit_reason` (scanner.py:11276-11283) exists to return
      ('MOMENTUM_OVERRIDE_FAILED','Momentum override failed after entry') when
      `candidate.stabilization_momentum_override` is set, and the session mirror uses it at 19849
      and 19921. The real-money tracker passes the literal string 'REPAINT_ABORT' to
      `_close_and_record` (23165), so a position that entered on a stabilization momentum override
      and then failed is booked under the wrong exit reason.

      FAILURE
      A trade admitted by the stabilization momentum override loses its arrow and is closed by the
      tracker. The trade book and the exit-reason diagnostics record REPAINT_ABORT, while the
      identical event on the dashboard/session row records MOMENTUM_OVERRIDE_FAILED - the two
      counters disagree and per-reason outcome calibration attributes the loss to the wrong policy.


----------------------------------------------------------------------------------------------------
POINT 5  [PARTIAL]  Per-signal trail, profit-lock ladder, rupee loss slabs
----------------------------------------------------------------------------------------------------

WHAT THE CODE ACTUALLY DOES
    A tiered profit-retention ladder and a rupee loss "band" guard both exist and run per-trade, and
    the per-signal separation part of the claim is genuinely OK (each fill is its own `Trade` object
    in `open_trades[trade_id]`, all trail/peak/bleed state is stored via `setattr` on that object,
    and the MTF trailing source is keyed `f"{instrument}_{timeframe}"` because `ut_smart_trailing`
    defaults True). The maths, however, does not match the claim on almost every number. The profit
    ladder's rupee tiers are 1000/2000/3000 read on the WHOLE position by default
    (`profit_lock_per_lot_basis = False`), so a 3-lot trade trips the major-win rung at Rs.1000/lot
    — exactly the failure the audit asked about. There is no Rs.1500 gate anywhere in the codebase,
    and no point at which breathing switches from a %-of-gain basis to a %-of-premium basis; the
    shipped band is a 50%-90% lock (10-50% breathing), not the claimed 80-95% lock (0-20%
    breathing). The loss "slabs" are computed but never used to escalate: `rung`/`band_lo`/`band_hi`
    feed only a log label, and the ladder does not arm at all until -1000/lot (OPT) or -1500/lot
    (FUT), so the claimed 0..-500 and -500..-1000 slabs have no implementation for a trade that
    never showed a peak. -3000/lot is a hard cap for OPT only; FUT forces at -4000/lot.

EVIDENCE IN CODE
    - C:/Users/sagnik/Desktop/rp46-3/config/settings.py:1417 - profit_lock_per_lot_basis: bool =
    False (ladder reads the whole-position peak by default)
    - C:/Users/sagnik/Desktop/rp46-3/scanner.py:11594 - ladder_pnl = peak_pnl; divided by lots only
    when the per-lot flag is on
    - C:/Users/sagnik/Desktop/rp46-3/scanner.py:11608 - tier boundaries are 3000/2000/1000 absolute
    rupees, no 1500 gate
    - C:/Users/sagnik/Desktop/rp46-3/scanner.py:11623 - lock_ratio = min(ceiling, 0.85 +
    0.05*(ladder_pnl-3000)/2000) -- % of peak GAIN, never of premium
    - C:/Users/sagnik/Desktop/rp46-3/scanner.py:11661 - locked_points = (peak_pnl * lock_ratio) /
    (qty * inst_mult) -- retention applied to gain
    - C:/Users/sagnik/Desktop/rp46-3/config/settings.py:1223 - profit_lock_max_ratio 0.90 /
    profit_lock_min_ratio 0.50 (band is 10-50% breathing, not 0-20%)
    - C:/Users/sagnik/Desktop/rp46-3/scanner.py:14852 - rung = int((loss - start_rs)//step); used
    only to build band_name at 14853-14855
    - C:/Users/sagnik/Desktop/rp46-3/scanner.py:14941 - freefall/slow-bleed exit tests are identical
    at every rung; only the 14887 catastrophe force differs
    - C:/Users/sagnik/Desktop/rp46-3/config/settings.py:718 - rupee_loss_band_start_rs_opt 1000 /
    _fut 1500, size 500 (nothing below -1000/lot)
    - C:/Users/sagnik/Desktop/rp46-3/config/settings.py:736 - force_loss_rs_opt 3000.0,
    force_loss_rs_fut 4000.0
    - C:/Users/sagnik/Desktop/rp46-3/scanner.py:12966 - loss_recovery_watch max/soft loss
    Rs.700/Rs.500 with no lot scaling
    - C:/Users/sagnik/Desktop/rp46-3/trading/trade_manager.py:590 - self.open_trades: Dict[str,
    Trade] keyed by unique trade_id (per-signal state is genuinely separate)

FINDINGS (5)

  [HIGH    ] partial  scanner.py:11594   [RE-VERIFIED]
    Profit-lock ladder tiers are per-TRADE by default, not per lot (profit_lock_per_lot_basis=False)

      scanner.py:11594-11600 sets `ladder_pnl = peak_pnl` and only divides by the lot count when
      `profit_lock_per_lot_basis` is enabled. config/settings.py:1417 ships that flag as `False`.
      The same flag gates `_profit_ladder_pnl` (scanner.py:8279-8284) and the Rs.3000 arm cap
      (scanner.py:11512-11518). So every rupee threshold in the retention ladder is read against the
      whole-position peak. tests/test_stop_precedence_and_per_lot_lock_2026_08_05.py:146-150
      enshrines this: `test_absolute_basis_is_unchanged_by_default` asserts a 3-lot trade at a
      Rs.3000 peak returns `MAJOR_WIN_GUARD`. The audit's stated requirement (a 3-lot trade must not
      trip a per-lot gate at ~Rs.500/lot) is violated out of the box.

      FAILURE
      3-lot NIFTY CE (qty 195), peak P&L Rs.3,000 = Rs.1,000/lot. `_profit_lock_floor` takes the
      `ladder_pnl >= 3000` branch, returns reason MAJOR_WIN_GUARD with lock_ratio clamped to the
      0.90 ceiling. The protective floor is set at 90% of peak, i.e. the trade is scalped after
      giving back Rs.300 on a position that has moved barely Rs.15 of premium per unit. A 1-lot
      trade at the same Rs.1,000/lot gain sits in the 50%-breathing tier and is left alone.

  [HIGH    ] bug      scanner.py:14852   [RE-VERIFIED]
    Rupee loss slabs are cosmetic: `rung` only names a log band, exit aggressiveness never escalates

      `rung = int((loss - start_rs) // step)` (14852) is consumed solely by
      `band_lo`/`band_hi`/`band_name` (14853-14855), which appear only inside f-string detail
      messages at 14889, 14943, 14955 and 14963. `grep -n rung scanner.py` returns no other use. The
      freefall test (min flag groups + `need_sev`, 14880/14902-14909) and the slow-bleed test
      (minutes/worsen/recovery, 14919-14938) read fixed settings values that do not vary with
      `rung`. The only escalation in the whole function is the single catastrophe branch at 14887
      (`loss >= force_loss`). The claimed walk through 0..-500, -500..-1000, ... -2000..-3000 slabs
      that progressively tries harder to exit at minimal loss does not exist.

      FAILURE
      OPT trade, 1 lot. At -Rs.1,100 the guard needs 2 distinct freefall flag groups AND severity >=
      4, or 4 minutes of monotonic bleed. At -Rs.2,900 it needs exactly the same 2 groups / severity
      4 / 4 minutes. A grinding loser that never produces 2 flag groups and bounces Rs.100 every few
      minutes (defeating `slow_bleed_max_recovery_rs`=80) is held all the way from -Rs.1,000 to the
      -Rs.3,000 catastrophe force, which is precisely the 'waiting for the hard stop' behaviour the
      ladder was supposed to replace.

  [HIGH    ] missing  scanner.py:11608
    No Rs.1500 per-lot gate and no switch to a %-of-premium breathing basis

      The tier boundaries in `_profit_lock_floor` are 3000 / 2000 / 1000 (scanner.py:11608, 11624,
      11627). `grep -rn 1500 engine/ trading/ config/settings.py scanner.py` finds 1500 only as
      `rupee_loss_band_start_rs_fut` — never as a profit gate. Every branch computes a fraction of
      `peak_pnl` (gain), and the floor at 11661 is `(peak_pnl * lock_ratio) / (qty * inst_mult)`,
      again gain-derived. The only %-of-premium quantity in the system is
      `_option_breathing_distance` (scanner.py:13468-13507), which returns 4%-18% of entry premium —
      but it is a *minimum stop distance clamp on young/unproven trades*, released once
      `signal_matured and proved_move` (scanner.py:13579, 13586-13587). It is not gated on gain at
      all, and it loosens the stop rather than being the widened post-Rs.1500 breathing the claim
      describes.

      FAILURE
      1-lot trade whose per-lot gain crosses Rs.1,500. Per the claim, breathing should widen to
      15-20% of the option premium. In code, Rs.1,500 lands in the `ladder_pnl > 1000.0` branch and
      yields lock_ratio = 0.65 + 0.05*(500/1000) = 0.675, then is raised to the 0.50 floor / capped
      at 0.90 -> 32.5% breathing measured on the GAIN, not on premium. On a Rs.180 premium that is
      Rs.487 of gain-space, not 15-20% (Rs.27-36) of premium — a completely different quantity, so
      the trade is managed on the wrong scale for the whole life of the position.

  [MEDIUM  ] partial  config/settings.py:718
    Loss ladder never arms below -1000/lot OPT (-1500 FUT); no minimal-loss path for a never-green
    trade

      `rupee_loss_band_start_rs_opt = 1000.0`, `_fut = 1500.0` (config/settings.py:718-720);
      scanner.py:14849-14850 returns `False, "", "loss < ladder start"` for anything shallower. The
      only sub-Rs.1000 minimal-loss machinery is `loss_recovery_watch`, which is gated at
      scanner.py:12955-12960 on `peak >= max(500, 0.25R)` — a trade that goes red immediately from
      entry never qualifies. `eod_loss_minimize` (config/settings.py:763-766) only runs in the last
      20 minutes of the session. So a mid-session trade that never printed a Rs.500 peak has no
      ladder between 0 and -1000/lot and simply waits for the hard stop. The claimed 0..-500 and
      -500..-1000 slabs are unimplemented for that case.

      FAILURE
      OPT 1-lot entered at 14:00 that ticks straight to -Rs.400 then -Rs.900 and stalls.
      `_rupee_loss_band_guard_decision` short-circuits at 14849 (loss < 1000). `loss_recovery_watch`
      returns NONE at 12960 (peak 0 < arm 500). `eod_loss_minimize` is inactive (>20 min to
      square-off). The trade holds to the configured hard stop instead of being exited at minimal
      loss in the -500 slab.

  [MEDIUM  ] bug      scanner.py:12966
    Former-winner recovery band uses whole-position rupees with no lot scaling, unlike the rupee
    ladder next to it

      `loss_recovery_watch_max_loss_rs` (700) and `_soft_loss_rs` (500) are read raw at
      scanner.py:12966-12973, and `loss_recovery_watch_min_peak_rs` (500) raw at 12955-12957, with
      no multiplication by `lots`. The neighbouring rupee ladder DOES scale per lot —
      scanner.py:14826-14843 multiplies `start_rs`, `step` and `force_loss` by `float(max(1,
      trade_like.lots))` under `rupee_loss_band_scale_per_lot` (default True,
      config/settings.py:714). The two guards therefore run on different bases on the same trade.
      Note also that even the per-lot ladder caps FUT at `rupee_loss_band_force_loss_rs_fut =
      4000.0` (config/settings.py:737), so the claimed -3000/lot hard cap holds for OPT only.

      FAILURE
      3-lot OPT that peaked at Rs.900 then slips red. `max_loss_rs` resolves to Rs.700 for the whole
      book = Rs.233/lot, so the recovery watch declares EXIT at 12984 while the per-lot rupee ladder
      next to it has not even armed (its OPT start is 1000*3 = Rs.3,000). The 3-lot trade is cut on
      a per-lot loss a 1-lot trade would be allowed to breathe through three times over.


----------------------------------------------------------------------------------------------------
POINT 6  [PARTIAL]  Per-tick intelligence per index, same or better for live trades
----------------------------------------------------------------------------------------------------

WHAT THE CODE ACTUALLY DOES
    Every active index (all four: NIFTY, BANKNIFTY, SENSEX, MIDCPNIFTY — config/settings.py:170)
    does get its own intelligence bundle each scan cycle: scanner.py:6177 fans out
    `_process_instrument` per index in parallel, and each run publishes `results["intelligence"]`
    (scanner.py:9523) plus per-index memory/snapshot records. Per-index isolation is real — every
    cache key is `INSTRUMENT_timeframe` (scanner.py:6792) and the stateful sub-analyzers key on
    `f"{instrument}_{timeframe}"` (intelligence/regime_detector.py:138), so no index reuses
    another's intel. But it is NOT recomputed per tick: the bundle is served from cache while `now -
    cached_time < intelligence_analysis_ttl_seconds` (30s default, scanner.py:9394-9401), layered
    over an option chain cached 90s and tolerated up to 300s, and the background refresher walks
    instruments with a 5s stagger + 25s interval (~45s per full pass, scanner.py:5059-5129). On the
    live-trade side, both production callers of the exit loop pass `allow_blocking=False`
    (scanner.py:4980, 6300), so live trades never recompute — they read the same cache under a 90s
    bound (`loss_guard_intel_max_age_seconds`), i.e. up to 3x older than the entry path's 30s
    recompute TTL. Worse, that 90s bound is measured off the wrong cache key for 15min trades, so it
    silently fails to fire exactly where a bundle can freeze.

EVIDENCE IN CODE
    - scanner.py:6177 - scan cycle builds one process_task per name in self.active_indices; all 4
    indices every cycle
    - scanner.py:9394-9401 - intel_is_fresh = cached within _intel_analysis_ttl_seconds(); a cache
    hit skips the whole recompute
    - config/settings.py:1201 - intelligence_analysis_ttl_seconds: float = 30.0 (falls back to the
    90s chain TTL when unset, scanner.py:6695-6717)
    - scanner.py:6819 - _last_intel_fetch[key] = now, where key = INSTRUMENT_timeframe
    - scanner.py:6841-6845 - the bare-instrument stamp (_intel_cache_time[name],
    _last_intel_fetch[name]) is written ONLY when mirror_legacy/tf == '5min'
    - scanner.py:14156-14163 - _intel_age_seconds falls back to _last_intel_fetch.get(instrument) -
    the bare name, i.e. the 5min stamp
    - scanner.py:25121-25129 - fast path reads _cached_timeframe_intel(instrument, trade.timeframe,
    allow_fallback=True)
    - scanner.py:25149-25157 - age = _intel_age_seconds(intel_result, instrument, ...); >90s ->
    drop; this is the only guard on the live exit path
    - scanner.py:4980 + 6300 - the only two production callers of _update_active_trades, both
    allow_blocking=False, so live exits never recompute intel
    - intelligence/intelligence_aggregator.py:560-622 - the returned results/aggregate dict carries
    no as_of/timestamp/captured_at, so a bundle cannot report its own age
    - scanner.py:5101-5124 - background worker refreshes only '5min' and '15min'; a raise on the
    15min pass is swallowed at 5127 while the 5min bundle stays cached
    - intelligence/regime_detector.py:138 - key = f"{instrument}_{timeframe}": per-index,
    per-timeframe regime history, no cross-index bleed

FINDINGS (3)

  [HIGH    ] bug      scanner.py:14158
    Live-exit staleness guard reads the 5min bundle's timestamp when judging a 15min trade's intel

      `_intel_age_seconds` has no timestamp on the intel dict to work with
      (IntelligenceAggregator.analyze never stamps its output - intelligence_aggregator.py:560-622),
      so it always falls through to `last = (self._last_intel_fetch or {}).get(instrument, 0.0)`
      (scanner.py:14158) - the BARE instrument key. But `_cache_timeframe_intel` writes that bare
      key only under `if mirror_legacy or tf == "5min"` (scanner.py:6841-6845); the 15min bundle is
      stamped only under `NIFTY_15min` (scanner.py:6819). The fast exit path fetches the trade's OWN
      timeframe bundle - `_cached_timeframe_intel(instrument, trade.timeframe, allow_fallback=True)`
      at 25124-25128 - then ages it with `_intel_age_seconds(intel_result, instrument, latest_time)`
      at 25149. For a 15min trade the guard therefore measures how recently the 5min bundle
      refreshed, not the 15min bundle it is about to act on. The correctly-keyed stamp already
      exists (`_intel_cache_time[self._timeframe_intel_key(name, tf)]`, used properly by
      _get_timeframe_intel at 7003-7004), so the data is there and the wrong one is read.

      FAILURE
      An open BANKNIFTY 15min trade. `self.intel.analyze(... timeframe='15min')` starts raising on
      the 15min frame (or returns regime UNKNOWN so `_intel_result_cacheable` is False): the
      background worker swallows it at scanner.py:5127 and the scan loop swallows it at 9484-9485,
      so `_intel_cache['BANKNIFTY_15min']` is never overwritten and never cleared
      (`_clear_timeframe_intel` only runs when `_regime_history_ready` is False). The 5min bundle
      keeps refreshing every 30s, so `_last_intel_fetch['BANKNIFTY']` stays current. The 100ms HFT
      loop keeps reading the frozen 15min bundle; `age` comes back ~0-30s, never exceeds the 90s
      bound, `fast_path_stale_skipped` never increments, and `_intel_reversal_reason` fires
      `_close_and_record(tid, opt_val, 'OI_PCR_REVERSAL')` (scanner.py:25190) on an OI/PCR flip
      captured 20 minutes earlier that has since reverted - squaring off a live position on dead
      evidence, precisely the failure the 2026-08-08 age bound was added to prevent.

  [MEDIUM  ] partial  scanner.py:25121
    Live exits tolerate 90s-old intel vs the entry gate's 30s, and never recompute

      Both production callers of the exit loop pass allow_blocking=False (scanner.py:4980 HFT 100ms
      worker, 6300 scan cycle), so the `allow_blocking` branch at 25122
      (`self._get_trade_intel(trade)`, which would recompute) is dead in live operation - every
      live-trade intelligence decision is a plain cache read at 25124. That read's only bound is
      `loss_guard_intel_max_age_seconds` = 90.0 (config/settings.py:513), whereas the entry path
      treats the bundle as expired after `intelligence_analysis_ttl_seconds` = 30.0
      (config/settings.py:1201, applied at scanner.py:9394-9401). So the answer to 'is there a path
      where a live trade reads intel older than the entry gate would allow' is yes, routinely: a
      31-90s-old bundle is refused for grading a new entry but accepted for closing an open
      position. Analysis DEPTH is equal (the exit reads the trade's own-timeframe bundle, falling
      back to the same 5min bundle the entry gate consumes at 9543-9608), so the claim fails on
      staleness tolerance rather than on depth.

      FAILURE
      The scan loop degrades - an index hits the 6s `live_index_timeout_seconds` budget
      (scanner.py:6153-6166) or is skipped as still-inflight (6102-6113), both of which return
      `_build_live_fallback_result` without touching `_intel_cache`. For up to 90s the 100ms exit
      loop keeps grading open positions on the last completed bundle and can fire OI_PCR_REVERSAL or
      the score < -40 intelligence-flip exit (25195-25198) on evidence the entry gate had already
      declared too old to open a trade on.

  [LOW     ] risk     intelligence/intelligence_aggregator.py:560
    Intel bundles carry no self-describing timestamp; every age check is a scanner wall-clock proxy

      `IntelligenceAggregator.analyze` returns `results` with `aggregate`, `oi`, `pcr`, `volume`,
      `order_flow`, `regime`, etc., and none of them carries `as_of` / `timestamp` / `captured_at`
      (the only `age_seconds` in the module, line 170, sits inside the synthesized `chain_quality`
      -> `results['data_quality']`, which `_intel_age_seconds` does not inspect).
      `_intel_age_seconds` (scanner.py:14121-14139) is written to prefer an embedded stamp and
      therefore always falls through to the scanner-side `_last_intel_fetch` proxy. Any consumer
      that receives a bundle detached from the live cache - e.g.
      `_trade_like_market_intelligence_context` (scanner.py:11810-11813) returning the snapshot
      frozen onto the trade at entry time - is aged against the live 5min refresh clock instead of
      its own capture time.

      FAILURE
      The loss guard at scanner.py:14274-14312 falls back to
      `trade_like.market_intelligence_context` (the entry-time snapshot) when `_get_trade_intel`
      returns a non-dict; `_intel_age_seconds` then reports the live 5min fetch age (~seconds), so
      `intel_stale` is False and the deliberate R5 'unknown age = half weight' protection
      (14300-14308) is bypassed for a bundle captured when the position was opened hours earlier.


----------------------------------------------------------------------------------------------------
POINT 7  [PARTIAL]  Per-index move size, volume and lot size handled correctly
----------------------------------------------------------------------------------------------------

WHAT THE CODE ACTUALLY DOES
    The *mechanism* for per-index adaptation is genuinely there and is mostly correct: every
    per-index constant (lot_size, strike_interval, tick_size, exchange NFO/BFO) is read from
    config/instruments.json rather than hardcoded; move-size thresholds are expressed as ratios
    (range/ATR, volume/avg-volume, bps of the index) rather than absolute points — I could not find
    a single absolute point-move comparison anywhere in scanner.py/engine/intelligence; the
    stagnation exit carries an explicit per-index bps baseline map (NIFTY 4.0 / BANKNIFTY 5.0 /
    SENSEX 4.0 / MIDCPNIFTY 5.0) and divides by the INDEX price, not the option premium; the rupee
    loss ladder scales per lot and gives MIDCPNIFTY (largest lot) an extra relaxation; volume is
    compared per index against its own trailing mean and is protected by a shares-vs-contracts unit
    guard in both Scanner and VolumeProfiler; and index volume refresh is per-index rather than one
    global lock. Absolute rupee thresholds (profit-lock ladder Rs.500/1000/2000/3000, arm cap
    Rs.3000) are index-neutral in practice because the exchange sets lot sizes to equalise notional
    (~Rs.15L/lot), so premium x lot_size is comparable across all four. The failure is in the
    *data*, not the logic: config/instruments.json still carries NIFTY lot_size = 65 (that is
    FINNIFTY's figure; NIFTY is 75, as the repo's own test file asserts), and that number is the one
    every quantity, P&L and margin figure is built from. The cross-check that exists to catch
    exactly this is REAL-mode-only, no-ops when the contract master has not loaded, and reads
    instruments.json through a CWD-relative path whose FileNotFoundError is swallowed.

EVIDENCE IN CODE
    - C:/Users/sagnik/Desktop/rp46-3/config/instruments.json:7 - NIFTY lot_size 65 (BANKNIFTY 30,
    SENSEX 20, MIDCPNIFTY 120 are correct post-Nov-2024 values)
    - C:/Users/sagnik/Desktop/rp46-3/tests/test_lot_size_matches_the_exchange_2026_08_06.py:79 -
    repo's own authority: lot_size_mismatches({'NIFTY': 65}) == [('NIFTY', 65, 75)], i.e. 65 is
    stale and 75 is the exchange figure
    - C:/Users/sagnik/Desktop/rp46-3/scanner.py:9266 - lot_size = cfg.get('lot_size', 25) in
    _process_instrument: the config number is what sizes every candidate
    - C:/Users/sagnik/Desktop/rp46-3/data/kite_market_data.py:150 - option_lot_size(): authoritative
    exchange figure, CE/PE rows only, both NFO and BFO, dominant-size vote during a revision;
    returns None when the master is empty
    - C:/Users/sagnik/Desktop/rp46-3/engine/safe_mode.py:407 - the mismatch cross-check, gated on
    data_provider and only inside validate_real_mode_readiness
    - C:/Users/sagnik/Desktop/rp46-3/engine/safe_mode.py:412 - _Path('config/instruments.json') is
    CWD-relative, unlike config/settings.py:1930 which resolves against the module directory
    - C:/Users/sagnik/Desktop/rp46-3/engine/safe_mode.py:91 - the funding-warning docstring bakes
    'NIFTY lot 65 -> Rs.7,800' into documentation
    - C:/Users/sagnik/Desktop/rp46-3/scanner.py:15215 - per-index baseline_bps map {NIFTY 4.0,
    BANKNIFTY 5.0, SENSEX 4.0, MIDCPNIFTY 5.0} with the index (not premium) as the bps denominator
    - C:/Users/sagnik/Desktop/rp46-3/scanner.py:14826 - rupee_loss_band ladder scaled by lot count
    plus a MIDCPNIFTY-specific relax_pct
    - C:/Users/sagnik/Desktop/rp46-3/engine/position_sizing.py:62 - risk_per_lot = per_unit *
    lot_size, so the risk budget is lot-normalised
    - C:/Users/sagnik/Desktop/rp46-3/intelligence/volume_profiler.py:356 - mixed-unit (shares vs
    futures contracts) guard on the volume pillar; scanner.py:10118 and :10194 wire the same guard
    to the power-move/thrust detectors
    - C:/Users/sagnik/Desktop/rp46-3/trading/trade_manager.py:418 - Trade.from_dict restores
    lot_size with a default of 1, reached from the open-trade recovery loop at trade_manager.py:3228

FINDINGS (4)

  [HIGH    ] bug      config/instruments.json:7   [RE-VERIFIED]
    NIFTY lot_size is 65 in config; the exchange uses 75 (65 is FINNIFTY's lot)

      config/instruments.json:7 declares "lot_size": 65 for NIFTY. The other three are the correct
      post-Nov-2024 values (BANKNIFTY 30, SENSEX 20, MIDCPNIFTY 120); 65 is FINNIFTY's lot size,
      transcribed into NIFTY's slot. The repo's own test states the exchange figure explicitly:
      tests/test_lot_size_matches_the_exchange_2026_08_06.py:79 asserts
      lot_size_mismatches({"NIFTY": 65}) == [("NIFTY", 65, 75)]. That config number is the sizing
      basis everywhere -- scanner.py:9266 `lot_size = cfg.get("lot_size", 25)` feeds
      _process_instrument, scanner.py:2840 feeds the engine lab, engine/safe_mode.py:125 feeds the
      funding floor, and every quantity is `lots * lot_size`. The guard that exists for this
      (engine/safe_mode.py:407-426) only runs inside validate_real_mode_readiness and only when
      data_provider.kite already holds a loaded contract master; data/kite_market_data.py:178
      returns None for an empty master and lot_size_mismatches then returns [], so nothing is
      reported. Note also that when it does fire it appends to `blockers`, which demotes the WHOLE
      process to HISTORICAL (main.py:765-771) -- not just NIFTY.

      FAILURE
      REAL mode with the Kite contract master not yet loaded (Kite data session unauthenticated at
      connect(), Fyers carrying the feed): the cross-check silently no-ops, a 1-lot NIFTY option
      entry is submitted with quantity=65. NSE requires a multiple of 75, so the order is rejected
      as an invalid quantity and reaches the operator as a generic rejection. In PAPER/HISTORICAL,
      where no guard runs at all, every NIFTY quantity, P&L, exposure, margin estimate and
      calibration sample is computed on 65 instead of 75 -- 13.3% low -- and the risk-budget sizer
      (engine/position_sizing.py:62,69) computes risk_per_lot 13.3% too small and therefore
      authorises ~15% more lots than the operator's risk budget intends.

  [MEDIUM  ] bug      engine/safe_mode.py:412   [RE-VERIFIED]
    Lot-size cross-check reads instruments.json via a CWD-relative path and swallows
    FileNotFoundError

      engine/safe_mode.py:412 does `_json.loads(_Path("config/instruments.json").read_text(...))` --
      resolved against the process working directory. Every other loader resolves against the
      module: config/settings.py:1930 uses `Path(__file__).parent / "instruments.json"`. Line
      427-428 then does `except FileNotFoundError: pass`, so when the CWD is not the repo root the
      entire lot-size comparison disappears with no blocker and no warning (only the generic `except
      Exception` at 429 produces a warning, and FileNotFoundError is caught before it). Secondarily,
      the `configured` map is read off disk rather than from `settings.get_instruments()`, so the
      guard can validate a different file from the one the scanner actually sized on.

      FAILURE
      The app is launched from any directory other than the repo root (a service unit with a
      different WorkingDirectory, a scheduled task, `python /path/to/rp46-3/main.py` from the user's
      home). validate_real_mode_readiness returns ok with no lot-size blocker even though NIFTY is
      configured at 65 against an exchange 75, REAL mode arms, and every NIFTY order is submitted on
      the wrong multiple -- the exact outcome the guard was written to prevent.

  [MEDIUM  ] risk     trading/trade_manager.py:418
    Open-trade recovery defaults lot_size to 1 and never reconciles it against the index's
    configured lot

      Trade.from_dict (trading/trade_manager.py:418) restores `lot_size=data.get("lot_size", 1)`,
      and trade_manager.py:3228 uses it to rehydrate OPEN positions from SQLite on restart. Nothing
      cross-checks the restored value against config["indices"][instrument]["lot_size"], which is
      available. Every downstream order quantity is then built from it: trade_manager.py:1606
      `child.broker_quantity = int(close_lots) * int(trade.lot_size or 1)`, :1703 `quantity =
      int(close_lots) * int(trade.lot_size or 1)`, :4087 `qty = max(1, lots * lot_size)`. The same
      silent-fallback pattern appears with three different wrong constants elsewhere --
      scanner.py:9266 and engine/multi_timeframe.py:343 default to 25 (a retired NIFTY lot),
      scanner.py:13645 defaults to 15 (a retired BANKNIFTY lot) inside the profit-lock qty used to
      derive a protective stop price.

      FAILURE
      A persisted open NIFTY row carries a NULL/absent lot_size (legacy row, partial write, schema
      change). After a restart the position is restored with lot_size=1, so quantity reads 1 instead
      of 75: the exit/flatten path submits an order for 1 unit, the broker fills it, the system
      marks the trade closed while 74 units remain live and unprotected, and the displayed P&L is
      75x too small.

  [LOW     ] missing  config/instruments.json:12
    Per-index freeze_qty is declared but read by nothing; no order slicing exists

      instruments.json carries a per-index freeze_qty (NIFTY 1800, BANKNIFTY 900, SENSEX 500,
      MIDCPNIFTY 2801) -- the exchange's single-order quantity ceiling, a genuinely per-index
      number. Grepping freeze/slice/max_qty/split_order across scanner.py, engine/,
      trading/kite_broker.py, trading/broker.py and trading/order_retry.py returns no consumer: the
      field is dead config and no order is ever split. Reachability is limited today because
      scanner.py:178 initialises user_lots to 1 for all four indices and I found no writer that
      raises it (only the snapshot read at scanner.py:2174), and market_lot_size_max_lots caps the
      market-driven boost at 4.

      FAILURE
      An operator (or a future sizing path) raises NIFTY to 25 lots: quantity 25*75 = 1875 exceeds
      the 1800 freeze limit, the whole order is rejected by the exchange in one shot rather than
      being sliced into 1800 + 75, and the entry is simply lost with a generic rejection.


----------------------------------------------------------------------------------------------------
POINT 8  [PARTIAL]  Session and per-timeframe regime for all 4 indices
----------------------------------------------------------------------------------------------------

WHAT THE CODE ACTUALLY DOES
    The machinery for all four layers genuinely exists: `RegimeDetector.detect()` is a real
    per-(instrument,timeframe) detector — it keys its smoothing/hysteresis history on
    `f"{instrument}_{timeframe}"` (regime_detector.py:138, 271-293) and is fed that timeframe's own
    candle frame (scanner.py:7010, 7074-7089); a 60-min anchor regime is resampled per instrument
    (scanner.py:8745-8760); a session layer (SessionContextAnalyzer day_type
    TREND_DAY_UP/DOWN/RANGE_DAY/MOOD_*) drives a range-day trend guard that demotes phantom trends
    (regime_detector.py:643-715) and a session prior (intelligence_aggregator.py:639-652); and a
    real "all 4 together" cross-index breadth is computed (scanner.py:8819-8879) and consumed as a
    ±0.08 score adjustment (intelligence_aggregator.py:696-704). Per-TF results are stored in
    `_timeframe_regimes` and correctly cascaded for open-trade management via
    `_get_trade_regime`/`_trade_regime_is_own_timeframe` (scanner.py:6947-6970). The gap is on the
    ENTRY side: per-timeframe grading is behind a flag that defaults to False
    (config/settings.py:169), and every live entry gate reads `latest_regimes[instrument]`, which is
    by declaration the 5-minute label (scanner.py:268, 6841-6848), regardless of the candidate's
    timeframe — on a codebase whose own comment says 5m/15m regimes disagree in 83% of
    same-timestamp pairs (scanner.py:6942). Of the four regimes the claim names,
    RANGING/VOLATILE/TRENDING/BREAKOUT have distinct behaviour, but "choppy" is display-only and
    "momentum" normalizes to UNKNOWN.

EVIDENCE IN CODE
    - intelligence/regime_detector.py:138 - per-TF history key `key = f"{instrument}_{timeframe}"`;
    smoothing/hysteresis are genuinely per timeframe
    - intelligence/regime_detector.py:271-293 - one history vote PER BAR (bar-id dedup) then
    majority-vote smoothing over `regime_smoothing_window`
    - scanner.py:7010 - `candle_tf = self._live_analysis_window(self.candles.get_candles(name, tf),
    tf)` — each TF detects on its own frame
    - scanner.py:9462-9484 - live scan computes 5min intel then warms 15min via
    `_get_timeframe_intel(name, "15min")`; 1min is never computed on this path
    - scanner.py:268-269 - `latest_regimes` declared '5min instrument regime for dashboard/legacy
    gates'; `_timeframe_regimes` 'Own-timeframe regime for active trade management'
    - scanner.py:6841-6848 - `latest_regimes[name]` is written only under `mirror_legacy or tf ==
    "5min"`, so it is always the 5-minute label
    - scanner.py:6942 - in-repo measurement: 'sampled 5m/15m regimes disagree in 83% of
    same-timestamp pairs'
    - scanner.py:8745-8760 - `_hourly_anchor_regime` resamples 5min to 60min and runs
    `_detect_regime_warmed`
    - scanner.py:8819-8879 - `_market_breadth_context` builds the cross-index (all-4) bias from each
    index's 5min regime
    - intelligence/intelligence_aggregator.py:696-704 - breadth consumed: `if breadth_votes >= 3` ->
    ±0.08 score adjustment
    - engine/signal_processor.py:509-537 - `_build_timeframe_contexts` returns None unless
    `per_timeframe_grading_enabled`, collapsing grading to the caller's 5-minute values
    - engine/signal_quality_policy.py:74,87-100 - `is_choppy_regime = is_risky_regime`
    (RANGING+VOLATILE+UNKNOWN); only the confidence floor separates VOLATILE

FINDINGS (6)

  [HIGH    ] partial  config/settings.py:169   [RE-VERIFIED]
    Per-timeframe grading defaults OFF: 15min candidates are graded on the 5min regime and intel

      `per_timeframe_grading_enabled: bool = False` (config/settings.py:169).
      `SignalProcessor._build_timeframe_contexts` (engine/signal_processor.py:516-518) returns
      `None` when the flag is off, and `_candidate_timeframe_context`
      (engine/signal_processor.py:489-491) then returns the caller's fallback verbatim: `if not
      isinstance(context, dict): return fallback_result, float(fallback_intel_score or 0.0),
      str(fallback_regime or "UNKNOWN")`. The fallback `regime`/`intel_result` handed to
      `process_best_signal` is the 5-minute snapshot. The grading call at
      engine/signal_processor.py:4283-4291 therefore feeds `_grade_signal(..., _tf_regime,
      day_type=_live_session_ctx['day_type'])` the 5-minute regime for a 15-minute signal. The
      source comment at config/settings.py:165-168 states this outright ('Today a single 5-minute
      snapshot grades every candidate, so 15-minute signals are scored on 5-minute intelligence').
      tests/test_direction_mode_and_tf_grading_2026_07_31.py:64 asserts the default stays False, so
      this is the shipped behaviour. The per-TF regime IS computed and cached (scanner.py:9465-9484
      caches 15min under `_timeframe_intel_key(name,'15min')`) — it is simply not read at the
      grading seam.

      FAILURE
      BANKNIFTY 15-minute UT-Bot BUY fires at 13:15. The 15-minute detector reads TRENDING_UP (ADX
      27); the 5-minute detector reads RANGING after a two-bar pullback. `_build_timeframe_contexts`
      returns None, so `_grade_signal` receives regime=RANGING and applies the range-bound penalty
      (engine/signal_manager.py:233-235, -0.10) instead of the +0.30 trend-alignment bonus
      (engine/signal_manager.py:220-222). The 15-minute trade is downgraded from A to B and dropped
      by the grade-preference floor — a valid trend entry skipped on a regime measured on the wrong
      timeframe.

  [HIGH    ] bug      scanner.py:18642
    Live entry gates use 5min `latest_regimes[instrument]` for every candidate, whatever its
    timeframe

      `latest_regimes` is instrument-keyed and, by its own declaration at scanner.py:268 and its
      only write site at scanner.py:6841-6848 (`if mirror_legacy or tf == "5min": ...
      self.latest_regimes[name] = regime`), always holds the 5-MINUTE label. Yet the live entry path
      keys on it without consulting the candidate's timeframe: scanner.py:18642 `regime =
      str(self.latest_regimes.get(candidate.instrument, "UNKNOWN")...)` feeding the choppy veto at
      18651-18653 (`choppy_regimes = {...}; risky_regime = regime_adaptation and (regime in
      choppy_regimes)`); scanner.py:18554 for the B-grade entry override; scanner.py:18439 for the
      location-risk override; scanner.py:13212 for the live-signal stabilization window. None of
      these route through `_candidate_timeframe_context`, so turning `per_timeframe_grading_enabled`
      ON does NOT fix them. The correct per-TF cascade already exists — `_get_trade_regime`
      (scanner.py:6955-6970) — and the repo used it at exactly one entry seam (missed-signal
      re-entry, scanner.py:16982-16992, with a comment explaining why) but nowhere else.
      scanner.py:6942 records that 5m and 15m labels disagree in 83% of same-timestamp pairs.

      FAILURE
      NIFTY 15-minute SELL candidate, 15-minute regime TRENDING_DOWN, 5-minute regime VOLATILE after
      an expiry-day spike. At scanner.py:18642 `regime` resolves to VOLATILE, which is in
      `choppy_regimes` (18651), so `is_choppy=True` and the strict choppy confidence floor is
      applied to a candidate whose own timeframe is cleanly trending — the entry is vetoed. The
      mirror case is worse: 15-minute regime RANGING while the 5-minute pocket reads TRENDING_UP
      leaves `risky_regime` False, so a chop-bound 15-minute BUY passes the choppy gate and is sent
      to the broker.

  [MEDIUM  ] partial  scanner.py:8863
    Cross-index breadth uses an absolute 3-vote threshold, dead when <3 indices are active

      `_market_breadth_context` builds the aggregate over `active = list(getattr(self,
      "active_indices", []))` (scanner.py:8821-8824) but hard-codes the majority as an absolute
      count: `if len(bullish) >= 3: bias = "BULLISH" elif len(bearish) >= 3: bias = "BEARISH"`
      (scanner.py:8863-8866). The single consumer applies the same absolute gate: `if breadth_votes
      >= 3:` (intelligence/intelligence_aggregator.py:698). `active_indices` is an operator-editable
      setting (config/settings.py:170, exposed via dashboard/server.py advanced config), so trading
      only NIFTY+BANKNIFTY, or dropping MIDCPNIFTY, makes `bias` mathematically unable to leave
      NEUTRAL and the entire cross-index layer — the score adjustment at aggregator:699-704 and the
      `breadth_bias`/`aligned_count` telemetry at aggregator:613 — becomes dead code with no log
      line saying so. No fraction-of-active-indices form exists anywhere.

      FAILURE
      Operator sets `active_indices = ["NIFTY", "BANKNIFTY"]` for a low-margin day. Both print
      TRENDING_UP all morning. `len(bullish) == 2`, so `bias` stays NEUTRAL and `aligned_count` is
      2; `_top_down_alignment` never adds the +0.08 cross-index confirmation. Every candidate that
      session is graded as if the market had no breadth signal, silently, while the dashboard shows
      a fully aligned bullish tape.

  [MEDIUM  ] partial  scanner.py:9071
    CHOPPY is display-only and MOMENTUM normalizes to UNKNOWN: neither changes anything downstream

      The canonical enum has no CHOPPY or MOMENTUM member (engine/market_regime.py:8-15). `_ALIASES`
      folds `"CHOPPY"`, `"SIDEWAYS"`, `"MEAN_REVERTING"` into `MarketRegime.RANGING` and maps
      `"MOMENTUM"` and bare `"BREAKOUT"` to `MarketRegime.UNKNOWN` (engine/market_regime.py:21-26).
      The only place a CHOPPY code is ever produced is the dashboard panel: `if regime_code in
      {"RANGING","CHOPPY"} and adx > 0 and adx < 16: display_code = "CHOPPY"`
      (scanner.py:9071-9073), emitted only as `display_regime`/`label` in the panel payload
      (scanner.py:9085-9097) — no trading path reads `display_regime`. On the trading side
      `is_choppy_regime` is literally aliased to `is_risky_regime`
      (engine/signal_quality_policy.py:74-75), which lumps RANGING, VOLATILE and UNKNOWN into one
      bucket (engine/market_regime.py:92-97), as do `uses_tight_trailing` (market_regime.py:100-105)
      and the entry veto set at scanner.py:18651. The only place VOLATILE separates from RANGING is
      the confidence floor at engine/signal_quality_policy.py:87-100. So of the four regimes the
      claim names, 'choppy' has zero distinct behaviour and 'momentum' has none at all — worse, any
      component emitting the string "MOMENTUM" is normalized to UNKNOWN, the maximum-penalty bucket
      (engine/signal_manager.py:237-239, -0.30).

      FAILURE
      A dead 09:30-10:15 tape with ADX 11 and a genuine 10:30 mean-reverting range with ADX 18 both
      resolve to RANGING at every gate — identical confidence floor, identical trailing tightness,
      identical -0.10 grading penalty — while the dashboard shows one as 'Choppy' and the other as
      'Range / Choppy'. An operator tuning against the panel labels has no lever that acts on the
      choppy case alone. Separately, if any calibration or memory path round-trips a stored
      "MOMENTUM" label through `normalize_regime`, it silently becomes UNKNOWN and takes the -0.30
      UNKNOWN penalty instead of a trend bonus.

  [MEDIUM  ] risk     scanner.py:6881
    `_cached_timeframe_intel` is age-blind, so breadth can vote with an arbitrarily stale per-index
    regime

      `_cached_timeframe_intel` returns whatever is in `_intel_cache` with no TTL check at all:
      `cache = getattr(self, "_intel_cache", {}); result = cache.get(key); if result is not None:
      return result` (scanner.py:6878-6890). The TTL is enforced only inside `_get_timeframe_intel`
      (scanner.py:7003-7005, `now_val - cached_time <= ttl`). `_market_breadth_context` — the 'all 4
      together' aggregate — uses the age-blind reader for every index other than the one currently
      scanning: `intel = self._cached_timeframe_intel(name, "5min", allow_fallback=True)`
      (scanner.py:8825). Entries are only ever removed wholesale (`_clear_timeframe_intel`,
      `_purge_intel_regime_caches` at scanner.py:6892-6935); there is no per-entry expiry sweeper.
      `_get_timeframe_intel` also returns early without refreshing whenever
      `_regime_history_ready(candle_tf)` is False (scanner.py:7011-7012), exactly the condition a
      stalled feed produces.

      FAILURE
      MIDCPNIFTY's candle feed stalls at 10:40 with its last cached 5-minute regime TRENDING_UP.
      NIFTY, BANKNIFTY and SENSEX turn over to TRENDING_DOWN by 14:00. When NIFTY is scanned,
      `_market_breadth_context` reads MIDCPNIFTY's 10:40 label from the age-blind cache, so the
      bearish count is held at 3 rather than 4 and `aligned_count` is computed from a label almost
      four hours old. The cross-index adjustment at intelligence_aggregator.py:698-704 is applied
      (or withheld) on data that no longer exists, with no staleness flag in the returned dict
      (scanner.py:8867-8877).

  [LOW     ] missing  scanner.py:9465
    The 1-minute slot of the per-timeframe regime store is never populated on any trading path

      `_timeframe_intel_key` accepts and namespaces `1min` (scanner.py:6791-6795) and
      `_build_timeframe_contexts` iterates `for tf in ("1min", "5min", "15min")`
      (engine/signal_processor.py:523), but no trading path ever calls `_cache_timeframe_intel` with
      tf='1min': the live scan computes 5min then only 15min (scanner.py:9462-9484), the background
      refresher loops `for tf in ("5min", "15min")` (scanner.py:5100), and the morning warm-up does
      the same (scanner.py:4820-4856). The only 1-minute regime that exists is the dashboard panel
      snapshot, which deliberately writes into an isolated `panel_{tf}` namespace on a separate
      `_panel_regime_detector` and never touches `_timeframe_regimes` (scanner.py:9004-9020).
      Consequently `_trade_regime_is_own_timeframe` (scanner.py:6947-6954) can never return True for
      a 1-minute trade and `_get_trade_regime` always falls back to the 5-minute label. Impact is
      limited today because candidates are collected only from the 5min/15min lanes
      (engine/multi_timeframe.py:229-230, engine/signal_processor.py:1058), so this is latent rather
      than live.

      FAILURE
      If a 1-minute lane is enabled (the plumbing is present: `missed_signal_reentry_timeframes`
      defaults to ['1min','5min','15min'] at config/settings.py:860, `_fast_1m` stabilization at
      scanner.py:13217, and MTF already builds a realtime 1-minute engine at
      engine/multi_timeframe.py:99-101), every 1-minute trade would be denied runner mode by
      `_trade_regime_is_own_timeframe` and managed on a borrowed 5-minute regime, with no log
      indicating the borrow.


----------------------------------------------------------------------------------------------------
POINT 9  [PARTIAL]  Broker states: flapping vs fully lost vs one-of-two down
----------------------------------------------------------------------------------------------------

WHAT THE CODE ACTUALLY DOES
    Case (c) "one of two brokers down" is genuinely implemented and correct: build_source_health
    reports kite and fyers independently, distinguishes "Kite session alive but stream stalled"
    (kite_stream_stalled / kite_data_fresh) from "broker down", and only escalates to
    all_brokers_unavailable when BOTH halves fail; get_ltp fails over kite-WS -> kite-REST ->
    fyers-REST -> Yahoo -> stale cache, order authority is restricted to kite* sources, and
    _filter_live_candidates_by_source blocks live ENTRIES while still letting exits be managed on
    delayed data. Case (b) "fully lost" is covered for token death by the main.py runtime watchdog
    plus the safe_mode preflight and terminal-rejection classification. Case (a) "flaky/fluctuating"
    is where it breaks: there is no hysteresis or debounce anywhere in the health path, and the Kite
    ticker's on_connect stamps _last_kite_ws_tick = now on every reconnect without a tick having
    arrived, so a socket that reconnects faster than the 45s freshness threshold while delivering
    nothing reads as a perfectly healthy feed. Separately, health["kite"] — the "can we place
    orders" half — is latched True at connect time and never revoked when the session dies
    mid-session, and the read-healthy/orders-blocked case (IP whitelist) is alert-only: nothing
    changes state, halts, or degrades behaviour.

EVIDENCE IN CODE
    - data/market_source_health.py:64 - all_brokers_unavailable = is_live and not (kite_health and
    kite_data_fresh) and not fyers_health: the one-of-two-down distinction, correct
    - data/market_source_health.py:50 - kite_stream_stalled separates 'session alive for orders'
    from 'ticks stopped', with a _provider_started_at uptime guard so a cold start is UNKNOWN not
    'dead'
    - data/market_source_health.py:153 - order_authority requires a source name starting with
    'kite'; fyers_ws is passed entry_eligible=False at line 217, so a Fyers-only feed can never
    authorize a live entry
    - scanner.py:9739 - _filter_live_candidates_by_source drops ENTRY candidates in REAL mode when
    no entry-eligible broker source is fresh, but passes non-ENTRY (exit/manage) actions through
    - data/market_data.py:2109 - tiered LTP failover kite REST -> fyers REST -> (dead angel) ->
    Yahoo -> stale _ltp_cache, with _flag_quote_divergence cross-check when both answer
    - data/market_data.py:1393 - on_connect sets self._last_kite_ws_tick = time.time() with no tick
    received
    - data/market_data.py:744 - the Fyers equivalent deliberately does the opposite
    (_last_fyers_ws_tick = 0.0) and pairs it with _fyers_ws_connected_since; Kite has no such
    no-tick-after-open detector
    - data/market_data.py:1206 - self.is_connected = auth.kite is not None, assigned only in
    connect(); also 1474 in rebind_kite(); never revoked at runtime
    - data/kite_auth.py:246 - _mark_auth_required sets self._kite = None, so auth.kite goes None
    mid-session while MarketDataProvider.is_connected stays True
    - trading/kite_broker.py:416 - _escalate_if_ip_blocked only logs CRITICAL;
    trading/trade_manager.py:936 _alert_blocking_broker_condition only logs + notifies, 600s
    deduped; neither changes any state
    - engine/safe_mode.py:283 - the proactive egress-IP drift check runs only at preflight and only
    when settings.kite_registered_ip is non-blank (config/settings.py:94 default "")
    - main.py:461 - runtime watchdog alerts on kite_status['auth_required'], which an IP-permission
    refusal never sets (invalidate() is called only on TokenException, data/kite_market_data.py:83)

FINDINGS (4)

  [HIGH    ] bug      data/market_data.py:1393   [RE-VERIFIED]
    Kite reconnect stamps a fake tick, so a flapping socket reads as a healthy live feed

      on_connect does `self._last_kite_ws_tick = time.time()` before any tick has arrived. That
      field is read by exactly one consumer, build_source_health (data/market_source_health.py:24),
      where `kite_age = now - kite_tick` drives kite_stream_stalled (line 50, threshold 45s),
      kite_data_fresh (59), broker_degraded (81) and all_brokers_unavailable (64). KiteTicker
      auto-reconnects, and on_reconnect/on_close (market_data.py:1401-1410) do not touch the field.
      The Fyers path deliberately does the reverse - `self._last_fyers_ws_tick = 0.0` on open (line
      744) - and backs it with a separate _fyers_ws_connected_since / no_tick_after_open detector;
      the Kite path has neither.

      FAILURE
      Flaky link at 11:00: the Kite socket drops and reconnects every ~25s and never delivers a tick
      (subscription replay fails, or the feed is throttled). Every on_connect resets kite_tick to
      now, so kite_age never exceeds 45s. kite_stream_stalled stays False, kite_data_fresh stays
      True, broker_degraded stays False, all_brokers_unavailable stays False. The SYS tape shows OK,
      the 'Kite feed silent' critical issue (engine/scanner_telemetry.py:741) never fires, the Yahoo
      emergency path is never armed (yahoo_mode stays 'idle_standby'), and a REAL-mode preflight at
      that moment passes the 'all broker sources unavailable' blocker (engine/safe_mode.py:234) on a
      feed that is delivering nothing.

  [HIGH    ] partial  data/market_source_health.py:26
    health['kite'] (order capability) is latched at connect, never revoked when the session dies
    mid-day

      kite_health = bool(provider.is_connected). MarketDataProvider.is_connected is a plain
      attribute written only in connect() (data/market_data.py:1206, 1228) and rebind_kite() (1474).
      When Kite returns a TokenException mid-session, KiteDataSource._call calls auth.invalidate
      (data/kite_market_data.py:83) -> _mark_auth_required sets _kite = None
      (data/kite_auth.py:246), so KiteDataSource.is_connected flips False while
      MarketDataProvider.is_connected stays True forever.
      tests/test_kite_health_asks_about_data_too_2026_08_05.py:64 asserts this as intended ('kite'
      is True at a 600s tick age). The file's own comment (lines 30-36) acknowledges the latch and
      works around it only for the DATA question, leaving the ORDER question permanently answered
      'yes'. Uncovered part of the claim: 'fully lost broker' is only ever inferred from data
      staleness, never from order capability.

      FAILURE
      Token revoked at 11:00 (login from another device). Kite WS dies and kite REST 403s, so after
      ~45s kite_stream_stalled=True and kite_data_fresh=False. Fyers is healthy, so
      fyers_health=True and all_brokers_unavailable = is_live and not(True and False) and not True =
      False. The operator sees only 'Broker degraded' at WARN severity
      (engine/scanner_telemetry.py:733) while zero orders can reach the exchange; the 'All brokers
      unavailable' CRITICAL (line 745) and the safe_mode blocker (engine/safe_mode.py:234) both stay
      silent. Only the separate main.py:461 watchdog notices, and only because it queries kite_auth
      directly rather than the health snapshot the rest of the system routes on.

  [MEDIUM  ] partial  trading/kite_broker.py:416
    Reads-healthy/orders-all-refused (IP whitelist) is alert-only: no state change, no halt

      The blocking condition IS recognised - _escalate_if_ip_blocked (kite_broker.py:416, markers at
      410) and _alert_blocking_broker_condition (trading/trade_manager.py:936, markers at 910) - but
      both only log CRITICAL and fire a desktop notification, deduped 600s per marker. Nothing sets
      a flag, halts auto mode, or changes get_source_health(), which keeps reporting kite: True. The
      proactive detector in engine/safe_mode.py:283 runs only at preflight and is skipped entirely
      when settings.kite_registered_ip is blank, which is the shipped default
      (config/settings.py:94). The main.py:461 runtime watchdog only tests auth_required, and an
      IP-permission refusal is not a TokenException so it never calls invalidate() and never sets
      that flag. So the claim's 'adjusts behaviour' half is unimplemented for this state; only the
      'notices, after the fact' half exists.

      FAILURE
      Home connection's public IPv4 rotates overnight and kite_registered_ip is left at its default
      blank. Every read (quotes, ticks, positions, order book) keeps working, so all health flags
      stay green all day. The first entry at 09:32 is refused with 'is not allowed to place orders',
      which logs one CRITICAL line and is then suppressed for 600s. The scanner keeps generating and
      attempting entries for the rest of the session; each dies terminally at the broker and is
      recorded as an ordinary per-signal execution_error, with no state change to stop the loop and
      no source-health change showing the operator that order routing is dead.

  [MEDIUM  ] bug      scanner.py:3787
    REAL hot-swap gates its reconnect on the stale is_connected, then logs a false success

      `if not self.data.is_connected: self.data.connect()` followed by `if self.data.is_connected:`
      -> `KiteBroker(kite=kite_auth.kite, ...)` and `logger.info('Dynamic Kite broker connection
      successful')` (scanner.py:3787-3813). is_connected is the latched attribute from
      data/market_data.py:1206, so after a mid-session invalidate it is stale-True: the reconnect
      attempt is skipped, and kite_auth.kite is None (data/kite_auth.py:246), so the constructed
      broker has api=None. Every subsequent order then raises the terminal 'no authenticated Kite
      session' rejection from _require_api (trading/kite_broker.py:139), while the log line claims a
      successful connection.

      FAILURE
      Process starts in PAPER at 09:10 (data.connect() succeeds, is_connected=True, no real_broker
      built). The Kite token is invalidated at 10:00. The operator switches to REAL at 10:30: `not
      self.trades.real_broker` is true, `self.data.is_connected` is stale True so
      connect()/load_cached_session() is never retried, KiteBroker is built around kite=None,
      assigned as trades.real_broker, and the session reports a successful REAL broker connection
      while every entry and every protective exit is refused terminally.


----------------------------------------------------------------------------------------------------
POINT 10  [PARTIAL]  No material delay between backend and frontend
----------------------------------------------------------------------------------------------------

WHAT THE CODE ACTUALLY DOES
    The backend half of the path is genuinely well engineered and I found no blocking stall on
    either event loop: the scanner runs on its own loop in a dedicated thread (main.py:953-956) and
    hands snapshots to the dashboard loop via asyncio.run_coroutine_threadsafe (main.py:945-949)
    into a latest-wins asyncio.Queue(maxsize=1) (server.py:2426-2455), so a slow or stalled
    websocket client can never delay the trading loop; chart builds are pushed off both loops onto a
    dedicated 2-worker ThreadPoolExecutor (server.py:141-165); trade entries/exits poke the panel
    immediately via call_soon_threadsafe (server.py:65-74) rather than waiting for the 1s poll; and
    grep found no requests/sqlite/pandas/time.sleep anywhere in dashboard/server.py. Payloads are
    not rebuilt per request either - /api/state serves scanner_ref.dashboard_cache.state()
    (server.py:2713-2716) and encodes with orjson. The material delay is on the frontend, not the
    backend: numeric state is ~1s fresh (scan_interval 1.0, broadcast throttle >=1.0s), but chart
    candles are deliberately stripped from every WS/state frame (_lighten_state_payload,
    server.py:3160-3161) and reach the UI only through a 3000ms client-side poll that is skipped
    entirely while the tab is hidden. The WS-stall safety net is also coarse - a 30s timer with a
    10s threshold - so a silently stalled push path shows stale numbers for up to 30s while the
    status dot still reads "Connected".

EVIDENCE IN CODE
    - scanner.py:6466-6480 - scan-cycle broadcast throttle: pushes when a candidate exists, a full
    chart is needed, or >=1.0s since _last_broadcast_time; self.scan_interval = 1.0
    (scanner.py:130), so backend cadence is ~1Hz
    - main.py:945-949 - `async def _threadsafe_broadcast(payload): future =
    asyncio.run_coroutine_threadsafe(broadcast(payload), dashboard_loop); await
    asyncio.wrap_future(future)` — correct cross-loop hop; server.py:2438-2455 broadcast() only
    enqueues onto a maxsize=1 latest-wins queue, so client I/O never blocks the scanner
    - server.py:132-165 - chart builds explicitly moved off the scanner loop onto _chart_executor =
    ThreadPoolExecutor(max_workers=2); only the two attribute writes stay on the scanner loop. Grep
    for sqlite/pandas/requests/time.sleep/open() across dashboard/server.py returned zero matches —
    nothing blocking is left on the FastAPI loop
    - server.py:65-74 + 3236-3268 - periodic_trades_updater waits on an asyncio.Event with a 1.0s
    timeout and notify_trade_change() wakes it via loop.call_soon_threadsafe, so entry/exit
    refreshes the trade panel immediately rather than on a fixed poll
    - dashboard/static/js/dashboard-charts.js:270-280 - startChartRefreshLoop: setInterval(...,
    3000) with `if (document.hidden) return;` — the only path chart candles take to the UI, because
    server.py:3160-3161 _lighten_state_payload strips chart arrays from every WS/state payload
    - dashboard/static/js/app.js:167-175 - restHydrationTimer = setInterval(..., 30000) with
    `wsIsStale = Date.now() - lastWsUpdateAt > 10000`; the ws.onclose handler (app.js:225-237) is
    the only thing that flips the status indicator, so an open-but-silent socket keeps showing
    "Connected"

FINDINGS (5)

  [MEDIUM  ] partial  dashboard/static/js/dashboard-charts.js:279
    Chart candles reach the UI only via a 3s client poll that stops entirely while the tab is hidden

      server.py:3160-3161 (_lighten_state_payload, "Remove heavy chart arrays from state/WS
      payloads; /api/chart serves charts") strips chart data from every websocket frame and from
      /api/state. The sole remaining chart path is startChartRefreshLoop's setInterval(..., 3000),
      which additionally short-circuits on `if (document.hidden) return;`. So while numbers, trades
      and diagnostics are ~1s fresh, the candlestick view is up to 3s + build-latency behind the
      backend, and does not advance at all while the dashboard tab is backgrounded.

      FAILURE
      Operator watches NIFTY 1min on the dashboard during a fast move. The backend has the new bar
      at T+0 (scan cycle, 1Hz) but the chart still draws the previous bar until the next 3s tick, so
      the visible chart and the signal/price numbers next to it disagree for up to 3 seconds. If the
      operator was on another tab and switches back, document.hidden suppressed every tick while
      hidden, so the first thing shown is a chart as stale as the moment they left, until the next
      3s tick repaints it.

  [MEDIUM  ] risk     dashboard/static/js/app.js:168
    A silently stalled push path is only detected after up to 30s, and the UI still shows
    "Connected"

      The REST fallback runs on a 30000ms interval and only fires when `Date.now() - lastWsUpdateAt
      > 10000`. Because the check itself only happens every 30s, the worst-case detection latency is
      ~30s, not 10s. Nothing else in the client notices staleness: the status dot/text are driven
      purely by ws.onopen/ws.onclose (app.js:155-237), so a socket that is open but no longer
      receiving (scanner loop wedged, broadcast worker exception, scanner-side _last_broadcast_time
      not advancing) renders a fully stale dashboard labelled "Connected" the whole time.

      FAILURE
      The scanner loop hangs mid-cycle (the code itself anticipates this at scanner.py:6463-6464,
      logging "System Freeze/Hang Detected" above 30s latency). The websocket stays open, so no
      reconnect fires and the status stays green. The operator reads position P&L, SL levels and
      signal state that are up to 30 seconds old and can act on them, because nothing on screen
      indicates the feed stopped.

  [MEDIUM  ] risk     main.py:898
    Scanner is constructed with the non-thread-safe raw broadcast; line 949 swaps in the safe
    wrapper later

      main.py:898 passes `on_update=broadcast` (the bare coroutine function) into the Scanner
      constructor; the thread-safe `_threadsafe_broadcast` is only assigned later at line 949,
      inside the startup hook, just before the scanner thread launches. broadcast() reaches
      _ensure_broadcast_worker (server.py:2426-2435), whose asyncio.Queue and pump task are created
      on whichever loop is running — normally the dashboard loop at lifespan (server.py:1166). If
      the raw callable is ever awaited from the scanner thread's loop (any code path that starts
      scanner.run() without re-running the line-949 assignment), queue.put_nowait completes the
      worker's waiter future cross-thread without call_soon_threadsafe, so the dashboard loop is not
      guaranteed to wake for it. Every other cross-thread hop here is explicitly guarded
      (notify_trade_change uses call_soon_threadsafe at server.py:72; _schedule_on_scanner_loop uses
      run_coroutine_threadsafe at server.py:95), which makes this constructor default the outlier.

      FAILURE
      A bootstrap variant or harness that instantiates the Scanner at main.py:892-900 and calls
      scanner.run() on its own loop without going through the startup hook keeps on_update ==
      broadcast. Broadcasts are then enqueued from the scanner thread onto the dashboard loop's
      queue without a thread-safe wakeup: the dashboard loop, parked in select(), may not process
      the item until some unrelated event or timer wakes it, so dashboard updates arrive late and
      irregularly with no error logged anywhere.

  [LOW     ] bug      dashboard/server.py:2716
    /api/state serializes the whole payload twice, the first pass over chart arrays it then discards

      api_state does `payload = make_serializable(cached_state)` (line 2716; same pattern at 2736,
      2748, 2757) — a full recursive pure-Python walk of the cached state including the
      per-instrument chart arrays. The result is then handed to _api_state_json_response
      (2634-2637), which calls _lighten_state_payload to strip exactly those chart arrays and then
      _encode_json_bytes, which calls make_serializable a second time. Every /api/state request
      therefore performs two complete walks of the payload on the FastAPI event loop, the first of
      which is spent on data that is thrown away one line later.

      FAILURE
      On websocket-open, on every mode_updated/config_ack, and on each periodic-stale tick the
      client hits /api/state. With four instruments x multiple timeframes of candle history in
      dashboard_cache, the discarded first make_serializable pass is the dominant cost of the
      request and runs inline on the event loop, delaying the broadcast worker's own delivery of the
      next scan snapshot to all connected clients.

  [LOW     ] bug      dashboard/server.py:2342
    Each broadcast pays a throwaway full json.dumps just to measure frame size

      _ws_frame_size (2341-2342) does a complete `json.dumps({"type": "full_update", "data": data},
      default=str).encode("utf-8")` purely to measure length, and _cap_ws_payload discards the bytes
      (2349-2350). _send_one then re-serializes with json.dumps for each client (2507 full_update /
      2513 delta_update) after running compute_delta (2511) against that client's last state. The
      rest of the file already prefers orjson via _encode_json_bytes (2627-2631), but the sizing
      path does not.

      FAILURE
      At the 1Hz broadcast cadence with several dashboard tabs open, _deliver_broadcast runs one
      throwaway full dump plus one compute_delta and one dump per client, all inline on the FastAPI
      event loop, before any client bytes go out. This is the loop that also serves /api/state and
      /api/chart, so the sizing dump directly adds to every HTTP request's queueing delay for no
      result.


----------------------------------------------------------------------------------------------------
POINT 11  [PARTIAL]  MACD histogram per timeframe prices the entry/exit bid
----------------------------------------------------------------------------------------------------

WHAT THE CODE ACTUALLY DOES
    There IS a real MACD-to-order-pricing path, but it is much narrower than the claim.
    `engine/order_pricing.momentum_urgency()` turns a MACD-histogram expansion into an urgency
    score, and `limit_confidence_penalty()` raises the confidence bar a LIMIT must clear inside
    `kite_broker._submit_entry` — so MACD can push an entry from LIMIT to MARKET, but it never sets
    or adjusts a bid PRICE. The histogram it reads is the one `RegimeDetector.detect()` computes
    from `df['close']` of the INDEX candle frame, never the option premium series; it is read only
    on the candidate's own timeframe (one key), not all three; and it is not wired into the exit
    path at all (exits go through `broker.close_order`, which has no momentum parameter). The
    standalone premium-capable module `engine/macd_histogram.py` states in its own header that it is
    "not wired into any engine yet", and its 4-state/veto logic has no call site outside its test.
    Finally the whole entry path is inert in the live configuration: `.env` sets
    `MACD_AWARE_ENTRY_PRICING='False'` and the settings default is `False`, so
    `limit_confidence_penalty` returns 0.0 unconditionally today. Missing/short data fails OPEN —
    urgency 0.0, threshold untouched, order still placed on the confidence-only rule; nothing is
    ever blocked.

EVIDENCE IN CODE
    - engine/order_pricing.py:77 — `if not _enabled(getattr(settings, "macd_aware_entry_pricing",
    False), False): return 0.0`; config/settings.py:1250 defaults it False and .env:774 sets
    MACD_AWARE_ENTRY_PRICING='False', so the penalty is 0.0 on every live entry.
    - trading/kite_broker.py:526-529 — `threshold = self._entry_limit_confidence_threshold();
    threshold += limit_confidence_penalty(momentum_urgency, _cfg()); use_limit = float(price or 0.0)
    > 0 and float(confidence or 0.0) >= threshold` — MACD only flips LIMIT vs MARKET; it never moves
    the limit price. The only momentum_urgency references in kite_broker are 483/498/512/527, all on
    the entry path.
    - intelligence/regime_detector.py:104-108 — `ema12 = close.ewm(span=12).mean(); ... macd_hist =
    float(macd_line.iloc[-1] - macd_signal.iloc[-1])` where `close = df['close']` (line 78) and df
    is the caller's `candle_df`; surfaced as `"macd_histogram"` at line 349.
    - scanner.py:5102 and 7075 — `candle_tf = candle_5min if tf == "5min" else
    self._live_analysis_window(self.candles.get_candles(name, tf), tf)` is passed as `candle_df=` to
    `self.intel.analyze(...)`: these are INDEX candles, so the histogram consumed by entry pricing
    is an index histogram, never option premium.
    - scanner.py:6837-6838 — `previous = self._timeframe_macd.get(key, (0.0, 0.0))[0]` /
    `self._timeframe_macd[key] = (macd_now, float(previous))`: "previous" is the value from the last
    intel recompute (per scan tick), not the previous MACD bar, and it is seeded to 0.0 on first
    sight.
    - scanner.py:6864-6866 — `key = self._timeframe_intel_key(instrument, timeframe)` then a single
    `.get(key, (0.0, 0.0))`: exactly one timeframe (the candidate's own) is consulted; the main scan
    loop at scanner.py:5101 only populates `("5min", "15min")`.

FINDINGS (5)

  [HIGH    ] missing  trading/kite_broker.py:512
    MACD is consulted on the ENTRY order only; the EXIT bid never sees it

      The claim covers ENTRY and EXIT bids. Only the entry path is wired:
      `place_order`/`_submit_entry` accept `momentum_urgency` (kite_broker.py:483, 498, 512, 527)
      and it is supplied from scanner.py:23962/24075 via trade_manager.py:3711. Exits are routed
      through `broker.close_order` (trade_manager.py:1755, 2618, 4178), whose signature has no
      momentum/MACD parameter — a grep for `momentum_urgency` across trading/ returns only
      entry-path lines. There is no MACD read anywhere in the exit/stop/target pricing code, so an
      exit fired into a premium that is still accelerating in the trade's favour is priced
      identically to one fired into a collapsing premium.

      FAILURE
      A long CE is exiting on a target while the index MACD histogram is still expanding hard in the
      trade's favour. The entry path would have read that as urgency and taken the market; the exit
      path has no such input, so it prices the close on the confidence/touch rule alone and gives
      back the accelerating leg — and symmetrically, an exit into a fading premium gets no urgency
      bump and rests behind a running market.

  [HIGH    ] bug      intelligence/regime_detector.py:106
    MACD is computed on the INDEX close series, not the option premium the order is priced against

      The claim is that MACD times/prices the order on the option premium. The only histogram
      reaching order pricing is `regime_payload['macd_histogram']` (scanner.py:6832), produced by
      `RegimeDetector.detect()` at regime_detector.py:104-108 from `close = df['close']` where `df`
      is the `candle_df` argument. Every call site passes index candles:
      `self.candles.get_candles(name, tf)` / `candle_5min` (scanner.py:5102, 7075, 4836, 9446). No
      option-premium series is ever fed to a MACD. `engine/macd_histogram.py` — the module that
      could take an arbitrary premium series — declares in its own docstring (line 2) that it is
      "STANDALONE - not wired into any engine yet", and grep confirms its only importer is
      tests/test_macd_histogram.py.

      FAILURE
      Near expiry or in a high-IV crush, a NIFTY PE premium decays while the index histogram keeps
      expanding downward. `momentum_urgency` signs the index histogram by trade direction, reports
      full urgency, raises the LIMIT bar and sends a MARKET buy at the top of a spread on a premium
      that is not actually running — the exact 'premium leaving the bid' judgement the module claims
      to make, made on the wrong series.

  [HIGH    ] partial  engine/order_pricing.py:77
    The MACD entry-pricing path is switched off in the live configuration, so it never fires

      `limit_confidence_penalty` returns 0.0 immediately unless `settings.macd_aware_entry_pricing`
      is truthy (order_pricing.py:77). config/settings.py:1250 declares `macd_aware_entry_pricing:
      bool = False` and .env line 774 explicitly sets `MACD_AWARE_ENTRY_PRICING='False'`. Since that
      penalty is the ONLY consumer of the urgency value on the order path (kite_broker.py:527), the
      MACD reading computed at scanner.py:6832-6838 and threaded through trade_manager.py:3711 has
      zero effect on any order the live system places today. The claim that the system consults MACD
      before placing a bid is therefore not true of the running configuration.

      FAILURE
      Live entry on an accelerating premium with confidence just above
      `_entry_limit_confidence_threshold()`: the penalty is 0.0, `use_limit` stays True, the order
      rests as a LIMIT behind the running market and never fills — the precise regression the
      module's docstring says it exists to prevent. Nothing in the logs indicates MACD was skipped;
      urgency is still computed and stored on the trade.

  [MEDIUM  ] bug      scanner.py:6837
    'previous histogram' is the last intel recompute, not the previous bar; seeded to 0.0

      scanner.py:6837-6838 keeps `(macd_now, previous)` where `previous` is whatever was cached at
      the last `_cache_timeframe_intel` call. That function runs once per scan iteration
      (scanner.py:5121, inside the per-instrument scan loop) and on demand (7096), not once per
      candle — so for a 15min candidate the two samples can be seconds apart within the same bar,
      making `growth = now - prev` intra-bar noise rather than bar-over-bar acceleration.
      `engine/macd_histogram.py:119-120` does it correctly with `hist.iloc[-1]` vs `hist.iloc[-2]`,
      but that module is unwired. Two further defects on the same lines: the first ever write uses
      the `(0.0, 0.0)` default so `previous = 0.0`, and `_timeframe_macd` is the only per-timeframe
      cache NOT popped by `_clear_timeframe_intel` (scanner.py:6891-6903) or
      `_purge_intel_regime_caches` (scanner.py:6923 clears `_intel_cache`, `_intel_cache_time`,
      `_last_intel_fetch`, `_timeframe_regimes`, `latest_regimes` only).

      FAILURE
      First scan tick after a restart or a HISTORICAL->REAL switch: key is absent, `previous = 0.0`,
      `macd_now = +8.0` for a LONG. `momentum_urgency` computes growth 8.0 over scale 8.0 = urgency
      1.0, the maximum penalty applies and the entry takes the market — exactly what
      `_entry_momentum_urgency`'s docstring (scanner.py:6853-6855) claims it prevents ("every entry
      would take the market on the first bar after a restart").
      tests/test_macd_paces_the_entry_bid_2026_08_07.py:155 only covers the empty-dict case, where
      `now` is also 0.0, so it passes without exercising this. Separately, replay-computed
      histograms survive the mode-switch purge and price the first live entries.

  [MEDIUM  ] partial  scanner.py:6864
    Only the candidate's own timeframe is consulted, not all three; 1min gets no reading

      `_entry_momentum_urgency` builds one key from `candidate.timeframe` and does a single dict
      lookup (scanner.py:6860-6866). There is no aggregation, agreement check, or fallback across
      the system's three timeframes — `_mtf_state_map` (scanner.py:7129-7134) confirms the MTF set
      is 1min/5min/15min. Compounding this, the main scan loop populates `_timeframe_macd` only for
      `for tf in ("5min", "15min")` (scanner.py:5101), and `_regime_history_ready` failures
      `continue` past the cache write (5103-5105). So a 1min candidate never finds its key on that
      path and silently scores 0.0 urgency.

      FAILURE
      A 1min-timeframe entry candidate on NIFTY: `key = NIFTY_1min` is absent from `_timeframe_macd`
      because the scan loop only writes 5min/15min, so `momentum_urgency` returns 0.0 and the order
      is priced as if no momentum existed — while the 5min and 15min histograms sitting in the same
      dict are both expanding hard. The failure is silent: the `except Exception` at scanner.py:6867
      only logs at debug, and the empty-key path does not log at all.


----------------------------------------------------------------------------------------------------
POINT 12  [PARTIAL]  Re-entry on winners and losers, pullbacks, hedging
----------------------------------------------------------------------------------------------------

WHAT THE CODE ACTUALLY DOES
    Only one of the three claimed behaviours is fully live. (a) Re-entry exists on both sides and is
    wired into the live eligibility path: `_stop_out_reentry_allowed` is called from
    `_can_trade_instrument` (scanner.py:2151) for loss-side re-entry, and the post-exit reversion
    window (`_maybe_arm_reversion_from_exit`, scanner.py:1736-1766,
    POST_EXIT_REVERSION_ENABLED='True' in .env) arms after "profitable major wins, stop/loss exits
    ... intel reverse exits" and outright bypasses the index cooldown at scanner.py:2123. The limits
    are not too tight — index_cooldown_minutes is 3.0, so the once-per-index re-entry only ever
    matters inside that 3-minute band. (b) Pullback-signal trading is implemented but unreachable in
    the live configuration: the `quality_pullback` route is emitted only by
    `AltSignalEngine._quality_hybrid_signals`, which runs only when `signal_source ==
    "quality_hybrid"` (engine/alt_scanners.py:1007-1008), while .env pins
    SCANNER_ENGINE='quad_prime'. (c) Hedging is not implemented as trading at all —
    `_pullback_hedge_arm`/`_pullback_hedge_release` are pure decision functions with no caller
    anywhere in scanner.py, engine/ or trading/; no leg is placed, sized, P&L-tracked or exited, and
    engine/safe_mode.py:454-463 says so explicitly. It is also a give-back hedge on a fading runner,
    not a hedge for high trend momentum.

EVIDENCE IN CODE
    - scanner.py:2151 - `reentry_ok, reentry_reason = self._stop_out_reentry_allowed(instrument,
    direction, signal_time, adx_value=adx_value)` inside the live `_can_trade_instrument` cooldown
    branch: loss-side re-entry is reachable
    - scanner.py:1751-1752 - `_maybe_arm_reversion_from_exit` docstring 'Arms after: profitable
    major wins, stop/loss exits (toggle), OI/PCR / intel reverse exits (toggle)'; .env:620
    POST_EXIT_REVERSION_ENABLED='True'; scanner.py:2123 `if cooldown_on and
    self._reversion_window_active(instrument): cooldown_on = False`
    - engine/alt_scanners.py:742-743 `elif pullback_ok: route = "quality_pullback"` - the only
    producer of the pullback route
    - engine/alt_scanners.py:1007-1008 `elif self.signal_source == "quality_hybrid": signals,
    positions = self._quality_hybrid_signals(` - the only call site; .env:4
    SCANNER_ENGINE='quad_prime'
    - engine/safe_mode.py:454-462 - 'Hedging is DORMANT: _pullback_hedge_arm/_pullback_hedge_release
    are written and tested, but no code path places the second leg'; .env:690
    PULLBACK_HEDGE_ENABLED='False'
    - Repo-wide grep for `_pullback_hedge_arm|_pullback_hedge_release|hedge_leg|hedges_used`: only
    scanner.py definitions (12552/12608), tests/test_pullback_hedge_2026_07_24.py, and
    scripts/backtest_exit_lifecycle.py:298-308. Zero hits in trading/ (no order placement, no
    position record)

FINDINGS (4)

  [HIGH    ] missing  scanner.py:12552   [RE-VERIFIED]
    Hedging is decision-only: no code path opens, sizes, tracks or exits a hedge leg

      `_pullback_hedge_arm` (scanner.py:12552) and `_pullback_hedge_release` (scanner.py:12608)
      return (bool, reason) and are never called from any live path. Repo-wide grep finds call sites
      only in tests/test_pullback_hedge_2026_07_24.py and
      scripts/backtest_exit_lifecycle.py:298-308; there are zero occurrences of 'hedge' anywhere
      under trading/, so no order is ever placed, no lot count is derived from
      pullback_hedge_size_fraction, and no hedge position exists to carry P&L or be exited.
      engine/safe_mode.py:454-463 documents this and emits a startup warning if the operator turns
      the toggle on. Separately, the design is a give-back hedge on a fading runner (armed off the
      peak, released when the move resumes), not the claimed 'hedge when trend momentum is high'.
      The seven pullback_hedge_* settings are live editable knobs in dashboard/server.py:586-598 and
      1703-1707, so the dashboard presents a control that drives nothing.

      FAILURE
      Operator flips pullback_hedge_enabled to True in the dashboard mid-session on a trending day.
      Every arm/release condition evaluates correctly in isolation but nothing calls them, so no
      hedge leg is ever placed. The runner gives back the full peak-to-lock distance the hedge was
      supposed to capture, and the trade book shows no hedge P&L to explain the miss. Because the
      toggle was changed at runtime, the safe_mode startup warning that would have said 'hedging is
      DORMANT' never fires.

  [HIGH    ] partial  engine/alt_scanners.py:1007   [RE-VERIFIED]
    Pullback route lives only in the quality_hybrid engine, which the live .env does not select

      The `quality_pullback` route is produced solely by `_quality_hybrid_signals`
      (engine/alt_scanners.py:742-743, tagged at 757). That method has exactly one call site, `elif
      self.signal_source == "quality_hybrid":` at engine/alt_scanners.py:1007-1008, and the engine
      selector at lines 201-236 routes quad_prime through the EMA/consensus streams instead. Live
      config pins SCANNER_ENGINE='quad_prime' (.env:4). Consequently the downstream pullback grading
      gate in scanner.py:17157-17164 (`if route == "quality_pullback": ...`) can never be entered
      under the shipped configuration, and the fix described in the comment at
      engine/alt_scanners.py:727-731 ('two entire entry routes ran the full evaluation and emitted
      nothing') restored the route only for an engine that is not the live one. Reaching it is not a
      per-feature toggle: the operator must swap the whole entry engine via `set_scanner_engine`
      (engine/multi_timeframe.py:103), changing every other signal too.

      FAILURE
      A clean trend-day pullback forms on NIFTY 5min with agreement>=5 and 15m agreement. Under the
      live quad_prime engine, `_quality_hybrid_signals` is never invoked, `pullback_ok` is never
      computed, no signal carries quality_hybrid_route, and scanner.py:17137 rejects on the
      route-membership check. The pullback entry never fires, and no reject is logged against the
      pullback route because the route object was never created.

  [MEDIUM  ] bug      scanner.py:2155
    Once-per-day stop-out re-entry allowance is consumed by the eligibility check, before any order
    is placed

      `_can_trade_instrument` is a pure permission query, but scanner.py:2154-2155 calls
      `self._note_stop_out_reentry(instrument)` the moment the gate passes, incrementing
      `_stop_out_reentries[base]`. With stop_out_reentry_max_per_index=1 (.env:699), the first
      candidate that merely *asks* whether it may trade burns the index's single second chance for
      the day. Nothing decrements the counter if the candidate is subsequently rejected by any
      downstream stage (quality grade, RR floor, funding/margin, spread, concurrency guard, order
      rejection), and nothing re-credits it. The counter is checked at scanner.py:2036-2039, so
      every later attempt inside the window returns 'stop-out re-entry cap 1 reached' and falls
      through to the plain cooldown message.

      FAILURE
      NIFTY is stopped out at 10:31 in a confirmed TREND_UP. At 10:32 a LONG candidate passes
      `_can_trade_instrument`, which grants the re-entry and increments the counter to 1. Sizing
      then finds insufficient option margin (or the spread guard rejects), so no order is placed. At
      10:33 a cleaner LONG candidate arrives while the 3-minute cooldown is still active;
      `_stop_out_reentry_allowed` now returns False with 'cap 1 reached' and the trade is blocked,
      even though the system never actually re-entered once.

  [MEDIUM  ] bug      scanner.py:2428
    ut_5min_loss_cooldown_minutes=45 is never enforced; the post-loss pause it configures does not
    exist

      Settings declare `ut_5min_loss_cooldown_minutes: int = 45` (config/settings.py:461) and
      .env:176 sets UT_5MIN_LOSS_COOLDOWN_MINUTES='45'. Repo-wide grep shows the only readers are
      broadcast/config payload dicts: scanner.py:2428 and scanner_frictionless.py:394. No cooldown
      computation uses it; `_can_trade_instrument` uses the flat `self.index_cooldown_minutes`
      (scanner.py:194, .env:117 INDEX_COOLDOWN_MINUTES='3.0'). The effect on this audit point: after
      a losing exit the real block is only 3 minutes, so `_stop_out_reentry_allowed`'s 30-minute
      window and ADX>=25 / regime-aligned conditions only ever apply inside a 3-minute band; past
      minute 3 any same-index re-entry is allowed with no trend confirmation at all, and the
      operator's configured 45-minute post-loss pause silently does nothing.

      FAILURE
      NIFTY is stopped out on a 5min-source trade at 11:00 in a choppy regime. The operator believes
      UT_5MIN_LOSS_COOLDOWN_MINUTES='45' holds the index out until 11:45 and that any earlier
      re-entry must clear the ADX>=25 aligned-trend test. In fact at 11:03:01 the flat 3-minute
      cooldown has lapsed, `_can_trade_instrument` returns True without consulting
      `_stop_out_reentry_allowed` at all, and the system re-enters the same chopping index with no
      trend gate - repeatedly, up to MAX_TRADES_PER_INDEX=10.


----------------------------------------------------------------------------------------------------
POINT 13  [PARTIAL]  Worker engage/release lifecycle per trade
----------------------------------------------------------------------------------------------------

WHAT THE CODE ACTUALLY DOES
    The thread-pool side of the lifecycle is sound: AsyncExecutionQueue runs each task inside
    try/except with task_done() in a finally, so a throwing task can never leak a worker slot or
    corrupt unfinished-task accounting, and a crashed task cannot starve the pool. Queue refusal is
    not silent — submit() returns False on shutdown and after a bounded 2s wait when full, and all
    four call sites in trade_manager check `is False` and either close the row outright (entry) or
    mark it RECOVERY_REQUIRED with a critical log (exit). Per-signal in-flight flags
    (_premium_refresh_inflight) are added under a guard and discarded in the worker's finally plus
    on dispatch failure, and RecalculationQueue is a correct single-flight with all state
    transitions in a finally. What is incomplete is the account execution lease: the live scanner
    acquires it on the switch into REAL (scanner.py:3823) but has no release anywhere — the only
    releases are main.py:981 (process shutdown) and the unused legacy scanner_frictionless.py:616 —
    so a REAL→PAPER downgrade holds the cross-process execution right until the process dies.
    Separately, the per-trade bookkeeping inside place_bg/close_bg has no finally, so an exception
    escaping the explicit branches is swallowed by the queue's blanket handler and strands the row
    pending_execution.

EVIDENCE IN CODE
    - engine/async_queue.py:87-92 — task func runs inside try/except Exception with
    self.task_queue.task_done() in a finally; a crashed task logs and the worker loops on, so no
    worker slot or unfinished-task count is ever leaked
    - engine/async_queue.py:96-129 — submit() returns False on shutdown and again after a bounded 2s
    retry window when full, logging at error/critical; broker tasks (PlaceOrder_/CloseOrder_) get
    the bounded wait, others are dropped with a warning
    - trading/trade_manager.py:3804-3826 and 4239-4258 — both broker submit sites test `is False`:
    entry closes the row REJECTED and removes it from open_trades, exit clears pending_exit, calls
    _mark_recovery and logs [EXIT NEVER QUEUED]. Refusal is surfaced, not silent
    - scanner.py:7581-7607 — _premium_refresh_inflight key is added under _premium_refresh_guard,
    discarded in the worker's finally, and also discarded if Thread.start() raises; no
    double-acquire, no leak
    - trading/account_execution_lease.py:144-154 — release() unlocks and closes the handle with the
    close() in a finally; atexit.register(self.release) at line 39 is the process-exit backstop
    - engine/recalculation_queue.py:66-99 — single-flight worker: state is resolved in a finally
    under the lock, generation check prevents a stale worker clearing a newer task, and an active
    runner is never cancelled so its own finally path always runs

FINDINGS (3)

  [MEDIUM  ] partial  scanner.py:3851
    Execution lease acquired on the switch into REAL is never released on the switch back out

      scanner.py:3823-3824 claims the cross-process AccountExecutionLease when configure() enters
      REAL, but the non-REAL branch at scanner.py:3851-3854 only swaps trades.broker back to the
      mock broker — it never calls release_execution_lease. A repo-wide grep finds
      release_execution_lease called in exactly two places: main.py:981 (graceful process shutdown)
      and scanner_frictionless.py:616, which is a legacy snapshot imported by nothing (only
      referenced by scripts/write_restorepoint_manifest.py). That legacy file releases the lease on
      precisely this downgrade branch, so the live orchestrator is the one missing it. The OS file
      lock is therefore held from the first REAL switch until the process exits (via the atexit
      backstop at account_execution_lease.py:39), regardless of mode.

      FAILURE
      Instance A enters REAL at 09:10 (scanner.py:3823 acquires the account lock), the operator
      flips it to PAPER at 11:00 to stop live trading. A recovery/second instance B is then started
      to take over the account: KiteBroker._require_account_execution_lease (kite_broker.py:193-201)
      cannot get the lock, scanner.py:3824 fails closed, B drops to HISTORICAL and refuses every
      entry, exit and protective-stop order for the rest of the session — while A, sitting in PAPER,
      is not trading either. Nothing releases the lock short of killing A's process.

  [MEDIUM  ] risk     trading/trade_manager.py:3659
    place_bg/close_bg have no finally: an escaping exception strands the row pending_execution, no
    order sent

      The queued entry task place_bg (trade_manager.py:3659) and exit task close_bg (4116) do all
      per-trade slot bookkeeping — pending_execution/pending_exit flags,
      _release_margin_reservations(reservations) at 3777, removal from open_trades at 3795 — inside
      explicit success/rejection branches only; neither function has a top-level try/finally. Every
      statement after execute_order_with_retry returns is unprotected: self.save_state() (3796),
      self._log_ui (3783), self._alert_blocking_broker_condition(error) (3784) and the
      on_execution_result callback (3745, 3798). Anything raising there propagates out of place_bg
      into the queue's blanket handler at engine/async_queue.py:89-90, which logs '❌ Error in
      AsyncExecutionQueue task' and moves on. The worker itself is fine (task_done is in a finally),
      but the trade's slot is not: the row keeps pending_execution=True inside open_trades, counting
      against exposure and position count, with nothing resting at the exchange. The same shape
      applies to close_bg, where the stranded row is pending_exit against a live broker position.

      FAILURE
      Broker definitively rejects an entry (margin/RMS). Control reaches 3777 (reservations
      released) and then 3784 _alert_blocking_broker_condition raises — e.g. the notification path
      throws on a network error. The exception escapes place_bg, async_queue.py:89 logs it once, and
      lines 3785-3798 never run: the trade is never marked CLOSED/REJECTED, never removed from
      open_trades, and on_execution_result is never called, so the scanner keeps a phantom open
      position holding a slot for the rest of the session while the broker rejected the order.

  [LOW     ] bug      trading/account_execution_lease.py:112
    After a broker rebuild, release() on the self-owned lease is a no-op but shutdown logs it as
    released

      Every AccountExecutionLease registers atexit.register(self.release) in __init__ (line 39) and
      is never unregistered, so each dynamic broker rebuild (scanner.py:3805 constructs a fresh
      KiteBroker on reconnect/re-auth) permanently retains the previous lease object and its open,
      locked file handle. The new lease hits the BlockingIOError branch, recognises its own pid and
      sets _self_owned=True with _handle=None (lines 112-114). release() on that object (144-154)
      clears _self_owned, sees handle is None and returns at 149-150 — the OS byte-range lock is
      never unlocked, because it belongs to the orphaned first object. main.py:982-985 calls
      release_execution_lease on trades.real_broker (the newest one) and then logs '✅ Broker account
      execution lease released.' The mirror image is the real hazard: has_execution_lease()/held
      returns True for a self-owned object even if the true handle owner has been released, so
      _require_account_execution_lease (kite_broker.py:193-201) would pass with no OS lock actually
      held.

      FAILURE
      Kite session drops and reconnects mid-session, so trades.real_broker is rebuilt and its lease
      is self-owned. At shutdown main.py awaits release_lease(), which returns immediately having
      unlocked nothing, and logs that the account lease was released. Any supervisor/restart script
      that treats that log (or a lock probe) as 'account is free' and launches the replacement
      instance before the old process has finished exiting gets its REAL mode refused, because the
      lock is still held by the orphaned handle until atexit runs.


----------------------------------------------------------------------------------------------------
POINT 14  [PARTIAL]  Full session day in warm memory with a rolling last hour
----------------------------------------------------------------------------------------------------

WHAT THE CODE ACTUALLY DOES
    The in-process side of the claim mostly holds: CandleBuilder keeps ~2000 bars per
    instrument/timeframe (data/candle_builder.py:32) so the whole session's OHLCV is live in RAM,
    MarketIntelSnapshotStore keeps per-instrument option/depth snapshots, and both are read
    pre-entry (scanner.py:5126, 7114) and post-entry (the loss-guard decay check calls score_trend
    at scanner.py:14178). But there is no "rolling last-1-hour" window anywhere in the live code:
    the only rolling retention is WarmMemoryStore's 15-minute TTL (data/warm_memory.py:29), used
    both to expire the checkpoint and to prune the candles/candidates written into it, so the
    persisted warm window is 15 minutes, not 60 and not the full day. Every window is enforced by
    row count (deque maxlen, iloc[-max_candles:]) rather than by time, except score_trend's 900s
    cutoff which sits on top of a row-bounded deque. Restart recovery is narrower than claimed: warm
    memory is active only when mode == "REAL" (scanner.py:3127) and any restart more than 15 minutes
    after the last checkpoint deletes it outright (data/warm_memory.py:92), leaving the SQLite
    session ledger as the only survivor. Post-entry SQLite access is write-heavy and synchronous:
    TradeManager.save_state() serializes all open plus the last 500 closed trades into a blocking
    write under the manager RLock, from ~40 trade-lifecycle call sites and from the scanner loop at
    scanner.py:17855.

EVIDENCE IN CODE
    - data/warm_memory.py:29 `ttl_minutes: int = 15` is the only rolling retention window in the
    warm store; _cutoff (110-111) = now - ttl and _prune_payload/_prune_candles (124-133, 208-223)
    drop every candle/candidate/pending-signal row older than it, so the checkpoint carries 15
    minutes, not 1 hour and not the day.
    - data/warm_memory.py:92-94 `if not saved_at or now - saved_at > self.ttl: self.clear(); return
    {}` - a restart later than the TTL erases the checkpoint instead of restoring a stale-but-useful
    window.
    - scanner.py:3127 `def _warm_memory_allowed(self): return str(getattr(self, 'mode','') or
    '').upper() == 'REAL'` gates both _save_warm_memory (3199) and _restore_warm_memory (3210).
    - intelligence/snapshot_store.py:35-40 `max_points: int = 1500` with `deque(maxlen=max_points)`
    for both raw snapshots and per-timeframe frozen bars - retention is row-count, not time.
    - intelligence/snapshot_store.py:93-117 score_trend applies a TIME cutoff (`newest_ts -
    max(60.0, lookback_seconds)`, default 900s) over that row-bounded deque; scanner.py:14178
    `trend_fn = getattr(store, 'score_trend', None)` is the post-entry loss-guard read.
    - trading/trade_manager.py:3205-3215 save_state() takes self._lock, builds `[t.to_dict() for t
    in open_tr + closed]` (closed truncated to 500) and calls db.save_trades(snapshot)
    synchronously; invoked from the trade path (e.g. 1231, 1234, 1274, 1318) and from the scanner
    loop at scanner.py:17855.

FINDINGS (5)

  [HIGH    ] partial  data/warm_memory.py:29   [RE-VERIFIED]
    No 1-hour rolling window exists; the persisted warm window is 15 minutes and prunes the day's
    candles away

      WarmMemoryStore's ttl defaults to 15 minutes and is used for two different jobs: expiring the
      checkpoint (load(), line 92) and pruning the payload contents (_prune_payload line 124 ->
      _prune_candles line 208, _prune_candidates 143, _prune_pending_signals 164,
      _prune_latest_results 177). scanner._build_warm_memory_payload (scanner.py:3135) computes its
      cutoff from the same ttl (scanner.py:3130-3133) before calling dataframe_to_records, so only
      the last 15 minutes of 1min/5min/15min bars are ever written. A grep for 3600 / hours=1 /
      minutes=60 across scanner.py, engine/, data/, config/settings.py and trade_manager.py finds no
      hour-scale window at all - the only 3600 hits divide seconds into hours-held.

      FAILURE
      Process restarts at 13:05 after a 13:00 checkpoint. _restore_warm_memory replays at most 15
      one-minute bars per instrument (12:50-13:05); the rest of the session's in-RAM candles, the
      last hour included, are gone until the historical API refetch completes, so the first
      post-restart scan evaluates MACD/ATR/regime on a 15-bar frame.

  [HIGH    ] bug      data/warm_memory.py:92
    A restart gap longer than the TTL deletes the checkpoint instead of restoring a stale window

      load() computes `saved_at` and, when `now - saved_at > self.ttl`, calls self.clear()
      (unlinking the file) and returns {}. There is no partial-restore path and no attempt to keep
      rows that are individually still inside the window. Because save() only runs every
      save_interval_seconds=15s while the scanner loop is alive, a crash plus any manual restart
      delay (lunch break, broker re-auth, IP whitelist fix) exceeds 15 minutes routinely.

      FAILURE
      Bot dies at 11:20 and is restarted at 11:45. load() sees a 25-minute-old checkpoint, unlinks
      it and returns {}, so restored_candles == 0 and latest_prices/pending_live_signals are lost
      even though the session ledger in SQLite still lists open candidates; the scanner restarts the
      day's warm state from empty mid-session.

  [MEDIUM  ] partial  scanner.py:3127
    Warm memory is disabled outside mode == 'REAL', so paper/sim sessions have no restart window

      _warm_memory_allowed() returns True only when self.mode upper-cases to exactly 'REAL'. It
      gates _save_warm_memory (scanner.py:3199-3201) and _restore_warm_memory
      (scanner.py:3210-3212), and engine/scanner_telemetry.py:644 reports warm memory as disabled on
      that same test. Any other mode string (PAPER, SIM, DRY, or an unset value that fails the exact
      comparison) means nothing is ever checkpointed, so the claimed warm-memory retention model
      does not exist for those runs.

      FAILURE
      A paper-mode session restarts after a crash: _restore_warm_memory returns immediately, no
      candles/latest prices/pending signals are recovered, and the restart-recovery behaviour being
      validated in paper never exercises the code path that will run in REAL.

  [MEDIUM  ] risk     intelligence/snapshot_store.py:35
    Intel retention is row-count bounded (maxlen=1500), so the post-entry decay window shortens
    silently

      _raw and _bars are deque(maxlen=1500) per instrument (and per timeframe for bars); the
      docstring on score_trend claims 'the store has held the whole session's worth of them'.
      push_snapshot is called once per intel refresh from _record_bar_aligned_intelligence
      (scanner.py:7114, reached from scanner.py:5126 and 9524) with no time-based trimming, so how
      much wall-clock the deque covers depends entirely on refresh cadence. score_trend then filters
      by time (900s) inside that row-bounded buffer, so it degrades silently rather than erroring.

      FAILURE
      On an expiry day with several active indices and a fast intel refresh, >1.7 snapshots/second
      per instrument means the 1500-slot deque spans under 15 minutes; _decaying_intel_points
      (scanner.py:14165) asks for a 900s window, gets only the tail of it, and either returns 0
      (min_points not met, read as 'not deteriorating') or computes a slope over a truncated window
      - the loss guard under-penalises a decaying position exactly on the busiest day.

  [MEDIUM  ] risk     trading/trade_manager.py:3205
    save_state() does a blocking full-book SQLite write under the manager RLock on the live trade
    path

      save_state() acquires self._lock, materializes `[t.to_dict() for t in open_tr + closed]` where
      closed is truncated to the last 500 trades, and calls db.save_trades(snapshot) (INSERT OR
      REPLACE per row) synchronously. It is invoked from ~40 lifecycle points including the
      protective-stop sync path (1231, 1234, 1260, 1274, 1318, 1331, 1361, 1382, 1400) and from the
      scanner loop at scanner.py:17855. Unlike the session-signal write, which has an
      asyncio.to_thread variant (scanner.py:16266), this one has no off-loop path.

      FAILURE
      Late in a heavy session with 500 closed trades cached, a trailing-stop tighten calls
      save_state(): ~500+ row serialization plus a synchronous disk commit runs while self._lock is
      held, and any concurrent exit/monitor thread that needs the same RLock stalls behind the write
      - the delay lands precisely when stops are moving.


----------------------------------------------------------------------------------------------------
POINT 15  [PARTIAL]  Daily / weekly / monthly data cleaning
----------------------------------------------------------------------------------------------------

WHAT THE CODE ACTUALLY DOES
    Daily and monthly maintenance are real and genuinely scheduled; weekly does not exist. An hourly
    LogCleaner daemon thread (main.py:69-154, started at main.py:1010) zips/removes yesterday's log
    folders and carries the only MONTHLY job: an expiry-based DB prune gated on the last Thursday of
    the month plus a logs/.last_db_cleanup marker (main.py:129-148). A DAILY pass fires from the
    live scan cycle at 15:45 IST (scanner.py:5932-5938 -> DataManager.run_daily_maintenance: history
    gap-fill, candle-CSV pruning at 30/30/65-day retention, intelligence_history.json pruning), plus
    a daily 15:50 session archiver (main.py:498-545) that correctly refuses to run during market
    hours (scripts/archive_session_data.py:104-106). Live-session protection is imperfect: the log
    cleaner only skips 09:00-15:45, so its DELETE+VACUUM on the shared live sqlite connection is
    legal from 15:45:01 and overlaps the 15:45 maintenance pass, the 15:50 archiver and post-close
    trade writes; the trades retention predicate's COALESCE fallback compares a year-less display
    string and would delete same-day CLOSED rows; and the daily flag has no session-start reset, so
    the daily job can permanently self-disable.

EVIDENCE IN CODE
    - main.py:69-154 - hourly LogCleaner thread (time.sleep(3600)); whole body gated by `if not
    (market_start <= now.time() <= market_end)` with 09:00/15:45; started at main.py:1010.
    - main.py:129-148 - the only monthly job: get_last_thursday(), `if now.date() >= last_thursday`
    + logs/.last_db_cleanup month marker, then DatabaseManager().cleanup_old_records(days=31), then
    the marker is written.
    - scanner.py:5932-5938 - `if now_ist.hour == 15 and now_ist.minute == 45:` runs
    run_daily_maintenance in an executor (future never awaited); flag cleared only by `elif ...
    minute == 47`; scanner.py:514 is the only other reset (__init__).
    - data/sqlite_db.py:113 + 118-145 - cleanup_old_records(days=30) DELETEs from
    session_signals/signal_decisions/trades then VACUUMs on the shared singleton conn
    (sqlite3.connect(check_same_thread=False), line 106); every exception is caught and only logged
    (144-145).
    - trading/trade_manager.py:307,309 - `exit_time` is stored as `%d %b %H:%M:%S` (no year, e.g.
    '31 Jul 14:00:00') and `exit_timestamp` is `''` when exit_time is falsy, so
    COALESCE(exit_timestamp, exit_time) never reaches a comparable value.
    - Repo-wide grep for weekly/isoweekday/weekday()== jobs: every hit is option-expiry logic
    (engine/expiry_manager.py, data/market_data.py); no weekly maintenance job exists. Live DB
    check: 80/80 CLOSED rows currently carry ISO exit_timestamp, so finding 2 is latent, not firing
    today.

FINDINGS (5)

  [HIGH    ] bug      data/sqlite_db.py:136   [RE-VERIFIED]
    Trade retention DELETE falls back to a year-less string, deleting CLOSED trades of any age

      The predicate is `DELETE FROM trades WHERE COALESCE(exit_timestamp, exit_time) <
      datetime('now','-31 days') AND status='CLOSED'`. exit_timestamp is TEXT (schema line 191) and
      is written as '' when exit_time is falsy (trading/trade_manager.py:309); '' is not NULL, so
      COALESCE returns '' and '' < '2026-07-08 ...' is TRUE. When exit_timestamp is genuinely NULL
      (rows predating the _ensure_columns ADD COLUMN at sqlite_db.py:211) COALESCE falls back to
      exit_time, which trade_manager.py:307 stores as '%d %b %H:%M:%S' - '08 Aug 15:20:03', no year.
      Lexicographic compare against 'YYYY-MM-DD HH:MM:SS' is TRUE for every day-of-month starting
      with 0, 1 or 2, i.e. days 01-29. The fallback is not a fallback: it is an unconditional delete
      on 29 days out of 31. The live DB currently has 0 such rows (80/80 CLOSED carry ISO
      exit_timestamp), so this is latent, not firing today.

      FAILURE
      A trade closes today via a path that leaves exit_time unset at to_dict time (exit_timestamp
      persisted as ''), or the process opens an older DB whose pre-migration rows have
      exit_timestamp NULL. cleanup_old_records runs at EVERY non-pytest startup (sqlite_db.py:113),
      so a mid-session restart on the 8th of the month deletes those CLOSED rows immediately -
      today's own closed trades - then VACUUMs, making recovery impossible. P&L accounting,
      calibration and the session archive lose the day.

  [MEDIUM  ] missing  main.py:1010
    No weekly maintenance job exists; only hourly, daily and monthly cadences are wired

      The scheduled maintenance set is: hourly LogCleaner (main.py:69-154), daily data maintenance
      at 15:45 (scanner.py:5932), daily session archive at 15:50 (main.py:498), monthly expiry DB
      prune (main.py:129-148). A repo-wide grep for weekly/isoweekday/weekday()== finds only
      option-expiry code (engine/expiry_manager.py, data/market_data.py) - no weekly cleaning, no
      weekly report, no weekly retention pass. Nothing sits between the daily 30-day CSV prune and
      the once-a-month DB prune.

      FAILURE
      Anything whose natural cadence is weekly - compacting the logs/*.zip files the cleaner
      produces daily, or trimming data_store/candles contract files between monthly runs - is never
      done. In a month where the last Thursday is missed (process down that evening, or the marker
      bug below), zero DB pruning happens for 30+ days and trades/session_signals grow unbounded.

  [MEDIUM  ] bug      main.py:145
    Monthly prune marker is written even when the prune fails, skipping cleanup for the month

      main.py:142-148 calls db.cleanup_old_records(days=31) and then writes the current month into
      logs/.last_db_cleanup, relying on an exception to skip the marker. cleanup_old_records
      (data/sqlite_db.py:124-145) wraps the entire DELETE+VACUUM in try/except Exception and only
      logs, so it always returns normally. The outer `except Exception as db_err` at main.py:147 is
      unreachable for any DB-side failure.

      FAILURE
      VACUUM raises 'cannot VACUUM from within a transaction' (another thread holds an open implicit
      transaction on the shared singleton connection) or 'database is locked'. The error is logged
      once, the marker is written as '2026-08', and every remaining hourly tick that month sees
      last_cleaned_month == current_month_str and skips. Retention silently does not happen for the
      whole month, with only one log line as evidence.

  [MEDIUM  ] risk     main.py:85
    Cleaner window ends 15:45, exactly when the daily pass and 15:50 archiver hit the same DB

      The cleaner guard is `if not (09:00 <= now.time() <= 15:45)`, so from 15:45:01 it may run the
      monthly DELETE+VACUUM. That VACUUM executes on the process-wide DatabaseManager singleton
      connection (data/sqlite_db.py:92-106, check_same_thread=False) from the LogCleaner thread
      while holding self.write_lock for the whole DELETE+VACUUM, and VACUUM rewrites the entire file
      under an exclusive lock. Meanwhile scanner.py:5932 kicks off run_daily_maintenance at 15:45
      and main.py:512 launches scripts/archive_session_data.py at 15:50, which reads the trades
      table; post-close exit fills, reconciliation and trade_accounting writes are still landing in
      that window.

      FAILURE
      Process started 07:46 on the last Thursday: the hourly tick lands at 15:46. The cleaner takes
      write_lock and VACUUMs; every scanner/TradeManager DB write blocks behind write_lock for the
      duration, and the 15:50 archiver child can hit 'database is locked' and lose that day's
      archive. On a ledger larger than today's 6.7 MB the stall grows linearly, and late exit writes
      are delayed inside the live loop.

  [MEDIUM  ] bug      scanner.py:5937
    Daily maintenance flag resets only at 15:47, so one missed minute disables it permanently

      _daily_maintenance_done is set False in __init__ (scanner.py:514) and cleared only by `elif
      now_ist.hour == 15 and now_ist.minute == 47` (5937-5938). There is no pre-session reset,
      unlike _daily_reset_done which is cleared at 09:17 (5924-5925). Worse, the block sits below an
      early return: 5927-5929 `if self.mode == 'REAL' and not cycle_is_warmup and
      self._ensure_market_session_refresh(indices, now_ist): return`, so a session refresh during
      either minute skips both the trigger and the reset.

      FAILURE
      Day 1 maintenance runs at 15:45 and sets the flag; at 15:47 the cycle is inside a session
      refresh or a rate-limit backoff, so the reset minute is missed. From day 2 on, the getattr
      check at 5933 sees True forever: no candle pruning, no intelligence pruning, no history
      gap-fill for the life of the process. Nothing logs the omission and data_store/candles (92
      CSVs today) grows until restart.


----------------------------------------------------------------------------------------------------
POINT 16  [PARTIAL]  Live market vs off-market discrimination
----------------------------------------------------------------------------------------------------

WHAT THE CODE ACTUALLY DOES
    A real session-discrimination layer exists and works on an IST host: `data/market_data.py`
    supplies `is_market_holiday` / `is_special_trading_session` / `is_market_open` /
    `get_market_session_label` (PRE-OPEN / LIVE / CLOSED), `data/market_calendar.py` supplies a
    versioned holiday list plus JSON overrides and event-blackout windows, and scanner.py:23566 is a
    genuine "Institutional Execution Gate" that returns before any new entry when the market is
    closed (warmup/HISTORICAL excepted). Off-market quotes are labelled REF rather than fabricated
    as LIVE. However the whole session layer is timezone-naive: `data/market_data.py` imports no
    pytz/ZoneInfo and every predicate defaults to bare `datetime.now()`, and the execution gate and
    most other callers invoke it with no argument, so on any non-IST host the 09:15-15:30 window is
    evaluated against the machine's local wall clock. Special-session support is date-only, so a
    Muhurat evening session cannot be expressed. The bundled calendar covers exactly one year
    (2026), after which the deliberate fail-closed path shuts trading down permanently behind a
    single CRITICAL log line.

EVIDENCE IN CODE
    - data/market_data.py:1131-1143 `is_market_open`: `dt = dt or datetime.now()`, then `return
    dtime(9,15) <= dt.time() <= dtime(15,30)` - the module's import block (lines 8-31) contains no
    pytz, no zoneinfo, no IST constant, so the default is machine-local naive time. Same pattern at
    1097 (is_market_holiday), 1128 (is_special_trading_session), 1147 (get_market_session_label),
    1180, 2290.
    - scanner.py:23566 `if not self.data.is_market_open() and not is_warmup:` - the '──
    Institutional Execution Gate ──' that blocks new trades, called with NO argument. Same no-arg
    form at scanner.py:5943, 6267, 6274, 17016, market_data.py:861/1682/3016/3019,
    engine/signal_processor.py:3376, data/market_source_health.py:16. Only scanner.py:4351, 4448,
    24210 and main.py:560 pass an IST-aware `now_ist`.
    - data/market_calendar.py:11-36: `NSE_HOLIDAYS_BY_YEAR` has exactly one key (2026, 16 dates) and
    `NSE_SPECIAL_TRADING_DAYS_BY_YEAR` exactly one key (2026, single date 2026-02-01). No 2027+
    coverage is bundled.
    - data/market_data.py:1106-1125: `if coverage_years and dt.year not in coverage_years: ...
    return True` - uncovered years fail CLOSED, with the CRITICAL log deliberately emitted only once
    per year (lines 1113-1123).
    - engine/signal_processor.py:3373-3382: the try-branch computes `market_session_active` as `date
    match and time < kill_threshold and bool(scanner.data.is_market_open())`, but the `except
    Exception` fallback at 3379-3382 recomputes it as `now_ist.date() == session_day and
    now_ist.time() < dtime(sess_hr, sess_min)` - the `is_market_open()` term is silently dropped.
    - scanner.py:4161-4175 `_is_trading_day`: the `is_market_holiday` call is wrapped in `try/except
    Exception: pass` and control falls through to `return True`, i.e. fail-OPEN, contradicting the
    fail-CLOSED policy that market_data.py:1106 deliberately implements.

FINDINGS (5)

  [HIGH    ] bug      data/market_data.py:1133   [RE-VERIFIED]
    Session predicates use naive datetime.now(), not IST - execution gate inverts on a non-IST host

      `is_market_open`, `is_market_holiday`, `is_special_trading_session` and
      `get_market_session_label` all default to `dt = dt or datetime.now()`. data/market_data.py
      imports no pytz and no zoneinfo (import block lines 8-31) and defines no IST constant, and no
      code anywhere in the repo sets os.environ['TZ'] or calls time.tzset (grep for
      TZ/tzset/Asia-Kolkata-env returned nothing). The scanner itself derives time correctly via
      `datetime.now(IST)` (scanner.py:3102, 4313, 5881, 24209), but the load-bearing execution gate
      at scanner.py:23566 and 12 other call sites invoke `is_market_open()` with no argument, so the
      IST-correct clock never reaches the comparison. Correct only by the accident that the
      operator's box is in IST.

      FAILURE
      Deploy the same code on a UTC cloud VM (or any non-IST host). The real NSE session 09:15-15:30
      IST is 03:45-10:00 UTC, so `dtime(9,15) <= dt.time() <= dtime(15,30)` is False for 03:45-09:15
      UTC and the Institutional Execution Gate (scanner.py:23566) rejects every entry for the first
      ~5.5 hours of the live session. Worse, the same comparison is True for 09:15-15:30 UTC =
      14:45-21:00 IST, so from 15:30 to 21:00 IST - after the exchange has closed -
      `is_market_open()` returns True, the gate opens, and the scanner submits fresh entry orders
      into a shut market. `market_source_health.py:16` simultaneously reports `is_live=True`, so the
      health layer confirms rather than catches it.

  [HIGH    ] risk     data/market_calendar.py:11
    Bundled exchange calendar covers only 2026; from 2027-01-01 fail-closed silently halts all
    trading

      `NSE_HOLIDAYS_BY_YEAR` and `NSE_SPECIAL_TRADING_DAYS_BY_YEAR` each contain exactly one year
      key: 2026. `load_nse_calendar_coverage` therefore returns {2026} unless the operator has
      created data_store/market_holidays.json or data_store/market_special_sessions.json.
      market_data.py:1106-1124 then treats every date outside the covered years as a holiday (fail
      CLOSED, deliberate and tested), and the CRITICAL warning is emitted exactly once per uncovered
      year (guarded by `_holiday_coverage_warned_years` at 1113-1118). The safety direction is
      right, but the cliff is undated in any runtime surface and unannounced after the first line.

      FAILURE
      On 2027-01-01 with no override JSON present, `is_market_holiday` returns True for every date,
      so `is_market_open` returns False all day every day and `get_market_session_label` returns
      CLOSED. scanner.py:23566 rejects 100% of entry candidates and scanner.py:4161
      `_is_trading_day` returns False, so the morning refresh never arms. The system runs, scans,
      logs and shows a dashboard as normal but never places a single trade; the only diagnostic is
      one CRITICAL line emitted once at first touch of 2027, which is gone from the console by the
      time anyone notices the flat P&L.

  [MEDIUM  ] partial  data/market_data.py:1134
    Special-session support is date-only - a Muhurat session outside 09:15-15:30 reads as CLOSED

      `is_special_trading_session` (1127-1129) only flips the weekend and holiday tests in
      `is_market_open` (1136, 1138) and `get_market_session_label` (1149). The session window itself
      stays hardcoded at `dtime(9,15)`/`dtime(15,30)` (1141-1143), and the label path uses the
      global `real_mode_live_start`/`real_mode_live_end` settings (1153-1154) - there is no per-date
      window anywhere. The override loader confirms this: `load_nse_special_trading_days` ->
      `_payload_dates` (market_calendar.py:52-60, 93-111) extracts bare date strings and discards
      any other key, so a `{"date":..., "start":..., "end":...}` row cannot express a non-standard
      session. The only bundled special day, 2026-02-01, happens to be a normal-hours Sunday budget
      session, which is why the gap is invisible today.

      FAILURE
      NSE announces Muhurat trading on Diwali 2026-11-10 (already in the holiday set at
      market_calendar.py:26) for 18:00-19:00 IST. The operator adds "2026-11-10" to
      data_store/market_special_sessions.json expecting the system to trade it. The special flag
      correctly suppresses the holiday veto, but at 18:15 IST `dtime(9,15) <= 18:15 <= dtime(15,30)`
      is False, so `is_market_open` returns False, `get_market_session_label` returns CLOSED, index
      quotes are labelled REF, and the execution gate blocks every entry for the entire session. The
      operator has no way to widen the window short of editing the hardcoded times.

  [MEDIUM  ] bug      engine/signal_processor.py:3379
    Exception fallback drops the is_market_open() term, so a holiday reads as an active session

      The try-branch at 3373-3377 defines `market_session_active` as `now_ist.date() == session_day
      and now_ist.time() < kill_threshold and bool(scanner.data.is_market_open())`. The `except
      Exception` handler at 3378-3382 recomputes the same variable from date and time only - the
      `is_market_open()` conjunct is gone. The try-block wraps the settings parse
      (`get_settings().ut_session_end`, `map(int, sess_end_str.split(':'))`), the `is_market_open()`
      call itself, and nothing narrows the except, so any failure inside - a malformed
      ut_session_end, a settings-load error, or an exception raised from the data provider -
      silently downgrades to the weaker predicate. `market_session_active` then feeds
      `signal_is_from_today` (3383-3387), which gates whether a stored signal is treated as live and
      actionable.

      FAILURE
      A transient failure inside `scanner.data.is_market_open()` (e.g. the provider is mid-reconnect
      and raises) on a Saturday backfill or an NSE holiday. The handler fires,
      `market_session_active` becomes True purely because the stored session_day matches today and
      the clock is before ut_session_end, so `signal_is_from_today` is True in REAL mode and a stale
      signal is promoted to a live actionable candidate on a non-trading day - the exact case the
      `is_market_open()` term was added to exclude.

  [LOW     ] bug      scanner.py:4170
    _is_trading_day fails OPEN on a holiday-check exception, contradicting market_data fail-CLOSED

      `_is_trading_day` (4161-4175) wraps `self.data.is_market_holiday(now_ist)` in `try/except
      Exception: pass` and then falls through to `return True`. market_data.py:1106-1124 goes to
      explicit trouble to fail CLOSED on an unverifiable calendar year ('deliberate safety - do not
      trade a year whose holidays we cannot verify'), but that intent is discarded here: any
      exception on the path makes the day a trading day. The same fail-open shape applies to the
      `is_special_trading_session` probe at 4164-4167, though that one defaults the safe way.
      `_is_trading_day` gates `_is_real_mode_startup_refresh_window` (4346) and the morning-refresh
      arming path.

      FAILURE
      The data provider is not yet fully constructed at REAL-mode startup (or `nse_holidays` is
      unset and the attribute lookup raises) on an NSE holiday. `is_market_holiday` raises, the
      except swallows it, `_is_trading_day` returns True, and the startup refresh window arms on a
      closed-market day - kicking off the morning warmup and burning broker history quota against a
      session that will never open.


----------------------------------------------------------------------------------------------------
POINT 17  [PARTIAL]  Slippage measured and fed back between trades
----------------------------------------------------------------------------------------------------

WHAT THE CODE ACTUALLY DOES
    Realised slippage IS genuinely measured, not faked:
    `_record_entry_slippage`/`_record_exit_slippage` (trading/trade_manager.py:804-861) compare the
    real broker fill against the pre-fill signal price (`signal_entry_price`, stamped at
    trade_manager.py:3626) and against the exit decision price, and the sign convention is correct
    in all four cases (OPT and FUT-LONG enter by buying / exit by selling; FUT-SHORT is inverted).
    `SlippageTracker.record` keeps a per-leg EWMA under two keys — an aggregate `OPT`/`FUT` key and
    a `OPT|<instrument>` key — persisted to data_store/slippage_stats.json. But the feedback loop
    stops at accounting: `calibrated_fraction` has exactly ONE caller in the whole repo,
    `engine/trade_accounting.py:23`, which only swaps the measured fraction into
    `estimate_trade_charges` — the P&L/charge model used for unrealized-P&L display, closed-trade
    P&L and shadow simulations. Nothing in order pricing, position sizing, or the entry go/no-go
    ever reads it: engine/order_pricing.py contains zero references to slippage, the option/futures
    entry limit buffers are fixed settings (option_limit_buffer_pct 0.40, fut_limit_buffer_bps 5.0),
    and the entry-risk budget hard-codes the static futures bps for every instrument type. So
    slippage adjusts what the system *reports* it earned, never what it *does* on the next order.

EVIDENCE IN CODE
    - engine/slippage_tracker.py:66-103 — record(): signed adverse_points / |reference| ->
    EWMA(alpha=0.2) written to BOTH the aggregate key and the `INST|instrument` key; ±5% samples
    rejected outright (line 86).
    - engine/slippage_tracker.py:105-122 — calibrated_fraction() prefers the per-instrument key,
    falls back to the aggregate, returns None below min_samples.
    - engine/trade_accounting.py:15-35 and 65-79 — the ONLY consumer of calibrated_fraction; the
    measured value is clamped and substituted for slippage_opt_pct / slippage_fut_bps inside
    estimate_trade_charges (a P&L charge model, not an order-pricing model).
    - trading/trade_manager.py:804-861 — entry/exit slippage sign conventions: OPT or LONG entry
    adverse = fill - reference; SHORT entry adverse = reference - fill; exits inverted. Verified
    correct.
    - trading/trade_manager.py:2148 — the pre-trade gate `_check_entry_risk_budget` passes
    `slippage_bps=float(getattr(settings, "slippage_fut_bps", 0.0) or 0.0)` for EVERY candidate,
    options included, and never consults the tracker.
    - engine/order_pricing.py — grep for `slippage` returns no matches; the file exposes only
    momentum_urgency() and limit_confidence_penalty(). Entry limit prices are built from static
    option_limit_buffer_pct / fut_limit_buffer_bps (config/settings.py:392, 411).

FINDINGS (5)

  [HIGH    ] partial  engine/trade_accounting.py:23
    Measured slippage feeds only the P&L charge model, never order pricing, sizing or go/no-go

      `calibrated_fraction()` has exactly one caller in the repository: `_calibrated_leg_fraction`
      in engine/trade_accounting.py:23, which is used only by `estimate_trade_charges` (display P&L
      at trade_manager.py:540, closed-trade P&L at 777, shadow sims at
      signal_processor.py:2065/3205/3955). No order-pricing, sizing, or admission path reads the
      realised EWMA: engine/order_pricing.py has zero slippage references, option entry limits use
      the constant `option_limit_buffer_pct` (0.40%) with a fixed tick floor, futures entries use
      the constant `fut_limit_buffer_bps` (5.0), and `evaluate_entry_risk` is handed a settings
      constant (trade_manager.py:2148). The module docstring's claim is accurate as written (the
      charge model uses these measured values), but the audit claim that slippage is 'adjusted for
      between trades' is met only in accounting, not in execution.

      FAILURE
      Option execution quality degrades (thin far-week book, widening spreads) and the EWMA
      correctly climbs to, say, 0.9% per leg. Displayed P&L dutifully gets more pessimistic, but the
      very next entry still posts a marketable limit only 0.40% past the signal price - so it
      repeatedly fails to fill or fills at the cap - and the entry-risk budget still admits it as if
      execution cost 2 bps. The system has measured that it cannot execute and changes nothing about
      how it executes.

  [HIGH    ] bug      trading/trade_manager.py:2148   [RE-VERIFIED]
    Entry-risk budget applies the futures bps slippage constant to option candidates

      `_check_entry_risk_budget` passes `slippage_bps=settings.slippage_fut_bps` (default 2.0) for
      every candidate regardless of inst_type. engine/entry_risk_policy.py:99-101 then computes
      `slip = entry * quantity * multiplier * bps / 10_000`. That is a fraction of a percent of
      premium, whereas the system's own option model (settings.slippage_opt_pct, default 0.30% per
      leg) and the realised OPT EWMA are expressed as a percentage of premium - roughly 15x larger.
      The OPT branch of `projected_stop_risk_rs` (lines 88-90) explicitly forces multiplier=1.0 and
      treats the candidate as long premium, so nothing downstream compensates, and the tracker is
      never consulted here at all.

      FAILURE
      An option entry at Rs.140 premium, 1 lot of 75: the budget adds slip = 140*75*2/10000 =
      Rs.2.10 of modeled execution cost, while the system's own static option assumption is
      140*75*0.003 = Rs.31.50 per leg (Rs.63 round trip). A candidate sitting ~Rs.60 above the
      per-trade / per-segment loss budget is therefore admitted, and the projected-stop ladder that
      evaluate_entry_risk shares with the circuit breaker is systematically optimistic for the only
      instrument this system actually trades.

  [MEDIUM  ] bug      engine/slippage_tracker.py:86
    One +/-5% plausibility filter for both futures prices and option premiums drops the worst option
    fills

      `record()` rejects any sample where `abs(adverse_points / reference) > 0.05`, logging it as a
      data error. That threshold is applied to the reference PRICE, which for futures is an index
      level (~24,000, where 5% = 1,200 points and is indeed impossible) but for options is the
      premium (~Rs.140, where 5% = Rs.7 and is an ordinary bad fill in a thin book). The filter
      therefore truncates the right tail of the option distribution only, and the truncation hits
      exactly the regime the calibration exists to detect.

      FAILURE
      A Rs.60 far-week MIDCPNIFTY CE fills Rs.5 (8.3%) worse than the signal price during a spike.
      The single most informative sample is logged as 'implausible' and thrown away; the EWMA
      continues to report benign execution while real fills are systematically worse, so
      estimate_trade_charges keeps under-charging and the displayed P&L of every option trade stays
      optimistic.

  [MEDIUM  ] partial  engine/slippage_tracker.py:57
    Per-index isolation is best-effort: below min_samples it falls back to one pooled global OPT
    number

      `_keys()` produces `[inst_type, f"{inst_type}|{instrument}"]` and `calibrated_fraction` walks
      them most-specific-first, returning the aggregate whenever the per-instrument row has fewer
      than `slippage_calibration_min_samples` (default 5) fills. Because record() writes the same
      fraction into both keys, the aggregate `OPT` row blends NIFTY, BANKNIFTY, SENSEX and
      MIDCPNIFTY premiums whose relative spreads differ by close to an order of magnitude. CE vs PE
      is not keyed at all (harmless today only because the system is always long premium, per
      entry_risk_policy.py:62-63). The claim of per-index/per-instrument tracking holds only after
      5+ fills accumulate on that exact key.

      FAILURE
      A session that has traded 30 NIFTY options then takes its first MIDCPNIFTY option: the
      MIDCPNIFTY key has 0 samples, so calibrated_fraction returns the NIFTY-dominated aggregate.
      estimate_trade_charges then models the illiquid MIDCPNIFTY leg with liquid-NIFTY execution
      quality, understating its charges for the entire life of that position.

  [MEDIUM  ] partial  trading/trade_manager.py:1020
    Kite postback discards the real average fill price; slippage is recorded only by the polling
    reconciler

      The postback COMPLETE branch parses `average_price` (line 1023) but uses it solely as a
      truthiness check to clear `execution_error` (lines 1028-1029); it never assigns
      `trade.entry_price` and never calls `_record_entry_slippage`. The sole recording site is
      trade_manager.py:3102, inside the broker-position confirmation loop, and it is additionally
      gated on `_option_premium_quote_plausible` (line 3097) - an option fill judged implausible
      against `entry_spot` is skipped for slippage recording as well as for entry-price sync.

      FAILURE
      The push postback arrives, the trade is marked filled, and the position is stopped out and
      squared off before the reconciliation pass next walks the broker position book - or the
      plausibility check at line 3097 rejects the fill. Either way no ENTRY sample is written, so
      the EWMA under-samples exactly the fast/volatile fills that carry the most slippage, and
      slippage_stats.json can sit below min_samples indefinitely (consistent with the standing note
      at engine/safe_mode.py:439 that the file currently holds FUT keys only).


----------------------------------------------------------------------------------------------------
POINT 18  [PARTIAL]  Cascade trade count and lot size driven by market data
----------------------------------------------------------------------------------------------------

WHAT THE CODE ACTUALLY DOES
    Sizing is genuinely derived from live analysis, not constants: `resolve_trade_lots`
    (engine/position_sizing.py:177) takes a base lot count from settings and then scales it by (a)
    `cascade_scale`/`heavy_trend_scale`, which signal_processor.py:6034-6040 only sets when the
    compression state is UNRESTRICTED or a validated BREAKOUT_CONFIRMED and the regime is trending
    and aligned, and (b) `_market_lot_multiplier` (position_sizing.py:140), a continuous 1.0-1.5x
    boost read off the live `intel_score`. Cascade trade COUNT is likewise market-derived:
    `_active_bucket_cap` (scanner.py:21303) widens the CORE/MIDCAP concurrent-position caps only for
    `one_way_cascade_entry` candidates, and duplicate same-chart signals are routed to
    `_offer_cascade_add` instead of a second strike. Concentration IS enforced before any order:
    `_bucket_capacity_allows` and `_passes_correlated_index_guard` (scanner.py:23883/23902) run in
    the signal gate, well before `log_trade` reaches the broker, and the main entry path runs a real
    Kite `/margins/orders` preflight with a process-local reservation ledger before `place_order`
    (trade_manager.py:3671). The gaps: the cascade-add (scale-in) order path skips that margin
    preflight entirely, and the conviction multiplier is applied AFTER every cap the core sizer
    enforced, so it compounds with the cascade multiplier and escapes both the cascade lot ceiling
    and the auto risk budget.

EVIDENCE IN CODE
    - engine/position_sizing.py:216-229 - `mult = _market_lot_multiplier(...)`, `grown =
    int(result.lots * mult)` runs AFTER `_resolve_trade_lots_core` has already applied cascade
    scaling and `cascade_max_lots_for_inst`; only `cap_lots` and `market_lot_size_max_lots` bound
    the result.
    - engine/loss_guard_policy.py:59-74 + 99-121 - `cascade_multiplier_for_inst` clamps OPT to 2x-3x
    ("single scale", explicitly "not 6"); `apply_cascade_lot_scale` applies `cascade_lot_max_lots_*`
    (default 0 = unlimited).
    - engine/signal_processor.py:6047-6060 - live entry path passes cascade/heavy flags plus
    `intel_score` into `resolve_trade_lots`; candidate_reasons at 6075 then prints "OPT 2-3x max"
    even when conviction pushed it past 3x.
    - trading/trade_manager.py:3671-3680 - main entry: `_entry_margin_verdict(...)` raises terminal
    `BrokerOrderRejected` before `broker.place_order`, and keeps `reservation_id`.
    - trading/trade_manager.py:2031 - `_run_scale_in` calls `self.broker.place_order(...)` for the
    cascade-add lots with no `_entry_margin_verdict` call and no margin reservation anywhere in
    `scale_in_trade`/`_request_broker_scale_in`/`_run_scale_in` (1803-2098).
    - engine/position_sizing.py:379-397 - in auto mode `lots_from_risk_budget` computes
    `floor(budget/risk_per_lot)`, then `_maybe_scale` multiplies it by the cascade/heavy multiplier
    and the wrapper multiplies again; the budget is never re-checked.

FINDINGS (4)

  [HIGH    ] missing  trading/trade_manager.py:2031
    Cascade-add lots reach the broker with no margin preflight and take no margin reservation

      Every new entry funnels through `_entry_margin_verdict` -> `preflight_entry_margin`
      (trade_manager.py:3671, kite_broker.py:1259), which compares the exact Kite order margin
      against free cash and RESERVES the committed amount so a concurrent entry cannot spend the
      same rupees. The cascade-add path does none of this: `_offer_cascade_add` (scanner.py:21040)
      -> `scale_in_trade` (trade_manager.py:1803) -> `_request_broker_scale_in` (1994) ->
      `_run_scale_in` (2020) goes straight to `self.broker.place_order` at line 2031. The only rupee
      gate in that path is `_entry_risk_admission` (1870), which is
      `entry_risk_budget_enabled`-gated and defaults to False (settings.py:1425), and which measures
      stop-risk, not margin. `scale_in_max_total_lots` defaults to 6, so adds can commit up to 5
      extra lots of premium unchecked.

      FAILURE
      scale_in_enabled=True (the switch the feature exists to be turned on by; scanner.py notes it
      is 'inert today only because scale_in_enabled is False'). A cascade duplicate fires on a chart
      that already has a 3-lot BANKNIFTY CE working. `scale_in_trade` admits the add and
      `_run_scale_in` sends a BUY for 1x65 premium with no funds check. Two effects: (1) on a thin
      account Kite returns a generic RMS rejection that `_run_scale_in` swallows with a
      `logger.error` and `return`, so the operator sees no funds diagnosis; (2) worse, because no
      reservation was taken, `kite_broker._margin_reservations` does not know that cash is
      committed, so a genuinely new entry arriving seconds later reads `available - outstanding` as
      if the add never happened, passes preflight, and the account over-commits - precisely the race
      the reservation ledger (kite_broker.py:1293-1325) was written to prevent.

  [HIGH    ] bug      engine/position_sizing.py:216
    Conviction multiplier runs after the cascade cap, compounding to ~4.5x and escaping
    cascade_lot_max_lots

      `_resolve_trade_lots_core` enforces the operator policy 'OPT cascade 2x-3x max, single scale'
      and applies `cascade_max_lots_for_inst` inside `apply_cascade_lot_scale`
      (loss_guard_policy.py:118). `resolve_trade_lots` then takes that already-capped result and
      multiplies again: `grown = int(result.lots * mult)` where `mult` is up to 1 +
      market_lot_size_max_boost_pct/100 = 1.5. The cascade cap is not re-applied - `grown` is
      bounded only by `cap_lots` and `market_lot_size_max_lots` (default 4, settings.py:1243). The
      net OPT ceiling becomes 3 x 1.5 = 4.5x base, not the documented 3x.
      tests/test_lot_size_follows_conviction_2026_08_07.py never exercises cascade_scale=True
      together with intel_score, so the compounding is untested. The reason string in
      signal_processor.py:6075 still reads 'OPT 2-3x max' for the compounded size.

      FAILURE
      Operator sets cascade_lot_max_lots_opt=3 (an explicit 'never more than 3 lots on a cascade'
      ceiling) and enables market_driven_lot_size. Base user_lots=1, one_way_cascade_entry with a
      trending aligned regime and intel_score=100. Core sizer: 1 x 3.0 = 3, capped at 3 - ceiling
      honoured. Wrapper: 3 x 1.5 = 4, bounded only by market_lot_size_max_lots=4 - the order ships 4
      lots, 33% above the configured cascade cap, while the decision log claims '2-3x max'. With
      base=2 the same path yields 6 -> 9 lots whenever market_lot_size_max_lots is left at 0/unset
      or above 6.

  [MEDIUM  ] bug      engine/position_sizing.py:388
    Auto risk-budget sizing is re-multiplied after the clamp, so risk can reach ~4.5x the budget

      With auto_risk_sizing_enabled=True, `lots_from_risk_budget` returns `floor(risk_budget /
      (per_unit_risk * lot_size))` - by construction the largest size whose stop loss fits the
      per-trade budget. Line 388 then calls `_maybe_scale(sized.lots)`, multiplying that clamped
      number by the cascade (up to 3x) or heavy-trend (2x) multiplier, and `resolve_trade_lots` line
      219 multiplies again by the conviction multiplier. The budget is never re-tested against the
      grown size, and the returned `PositionSizeResult` still carries the original `risk_budget`
      value, so any consumer reading `.risk_budget` sees a number the position no longer respects.

      FAILURE
      auto_risk_sizing_enabled=True, per-trade OPT budget Rs.10,000, risk_per_lot Rs.4,000 ->
      `lots_from_risk_budget` correctly answers 2 lots (Rs.8,000 at risk). Cascade flag set: 2 x 3.0
      = 6 lots; conviction 1.5x on intel_score 100 -> 9, clipped to market_lot_size_max_lots=4 -> 4
      lots. The stop is hit: Rs.16,000 lost against a Rs.10,000 per-trade budget, reported as
      'Risk-sized then CASCADE LOT SCALE'.

  [MEDIUM  ] risk     scanner.py:23856
    Correlated-index concentration limit counts positions, not size, and cascade adds skip it
    entirely

      `_passes_correlated_index_guard` (scanner.py:21234) bounds `len(exposures)` - the NUMBER of
      same-direction CORE positions - against `min(_active_bucket_cap('CORE'),
      ut_correlated_core_limit)`. It has no notion of lots or rupees. Meanwhile the cascade-add
      branch at scanner.py:23856 runs earlier in the gate and does `continue` on success, so an add
      never reaches `_bucket_capacity_allows` (23883) or `_passes_correlated_index_guard` (23902);
      trade_manager.py:1876 states this is deliberate ('this is the same idea'). The consequence is
      that the concentration control stops binding on actual exposure once cascade/conviction
      scaling and scale-in are both live.

      FAILURE
      ut_correlated_core_limit=2 with the A+ override giving 3. Three A+ same-direction CORE trades
      (NIFTY/BANKNIFTY/SENSEX CE) are admitted at the count limit, each sized 4 lots by
      cascade+conviction, then each grown by two cascade adds up to scale_in_max_total_lots=6. The
      book holds 18 lots of same-direction correlated index premium while the guard still reports '3
      of 3 exposures' and never re-evaluates. A single gap against the bucket takes all of it at
      once - the exact scenario the guard's 'one correlated risk bucket' docstring exists to bound.


----------------------------------------------------------------------------------------------------
POINT 19  [PARTIAL]  Cancels, partial fills, missed prices on fast moves
----------------------------------------------------------------------------------------------------

WHAT THE CODE ACTUALLY DOES
    The order lifecycle is unusually well hardened for a system this size: cancels are *proven*
    rather than acknowledged (`cancel_order_confirmed` in trading/broker.py:133-215 returns True
    only for a terminal state with zero filled quantity, False on a fill, None when unreadable), the
    stale-entry cancel/replace at trade_manager.py:2892-2980 acts only on `True` and marks
    RECOVERY_REQUIRED otherwise, ambiguous flattens are latched so an unacknowledged exit is never
    re-sent, and realised/unrealised P&L is sized on `Trade.pnl_quantity` (broker_quantity, not the
    intended lots) so a partial fill does not overstate the loss feeding the daily cap. Exit-side
    partial fills are re-examined via `_abandoned_exit_verdict`, which squares off only on a
    broker-stated CANCELLED/REJECTED and refuses to guess on UNKNOWN. Three real gaps remain. (1)
    The reconcile branch that closes a trade on a CANCELLED/REJECTED order status reads only the
    status text and never the filled quantity — the exact partial-fill-then-cancelled case that
    broker.py:178-205 was written to defend against — so a trade can be popped from open_trades at
    pnl≈0 while real quantity sits at the broker. (2) The unfilled remainder of a *partially filled
    entry* has no cancel and no timeout; the 12s stale-entry path is unreachable for it. (3) The
    PARTIAL_EXIT "UNKNOWN" verdict leaves a residual position whose protective stop was already
    cancelled, with the 15:25 force exit as the only backstop.

EVIDENCE IN CODE
    - trading/broker.py:194-205 - cancel_order_confirmed scans
    filledshares/filled_quantity/filledQuantity/filled on a safe-terminal status and returns `filled
    <= 0`, so a CANCELLED order that partially executed answers False (a fill) and the stale-entry
    replace cannot top a 1-of-3-lot fill up to 4 lots.
    - trading/trade_manager.py:2865-2880 - `status_text = self._order_status_text(order_status)`
    then `if status_text in {"rejected","cancelled","canceled"}:` -> status=CLOSED,
    pending_execution=False, popped from open_trades, exit_price=entry_price. No filled-quantity
    read anywhere in the branch; _order_status_text (line 863-872) returns only the status string.
    - trading/trade_manager.py:3075-3118 - on a partial entry fill: `filled_qty = abs(net_qty)`,
    `execution_status = "PARTIAL"`, `ensure_broker_protection(trade, filled_qty)`. Nothing cancels
    the resting remainder, and the 12s cancel/replace block at 2892 sits inside `if position is
    None:` + `execution_status == "ENTRY_SUBMITTED"`, both false once a partial fill has created a
    position row.
    - trading/trade_manager.py:508-526 and 766-786 - `pnl_quantity` returns broker_quantity when
    confirmed, and _finalize_closed_trade sizes gross and estimate_trade_charges on `sized =
    trade.pnl_quantity`, so partial-fill P&L is correct.
    - trading/trade_manager.py:4501-4509 - `flatten = _setting_enabled(getattr(settings,
    "force_exit_flattens_orphans", False), False)`; when off (the default) an orphan is only alerted
    via _alert_orphan_left_open and carried overnight in CARRYFORWARD/NRML.
    - trading/trade_manager.py:2196-2220 with
    tests/test_stale_entry_is_not_force_filled_2026_08_05.py:100 - `entry_revalidator` is read by
    _stale_entry_still_worth_filling but is assigned nowhere in production code; only the test sets
    `manager.entry_revalidator`. The live guard is the mark-vs-stop check, which returns True
    (fail-open) whenever mark or stop is <= 0.

FINDINGS (4)

  [HIGH    ] bug      trading/trade_manager.py:2866
    Reconcile closes a trade on a CANCELLED order status without reading filled quantity, dropping a
    real position

      In reconcile_broker_positions, the `position is None` branch fetches a live order status (line
      2842-2845) and, if the status text is rejected/cancelled/canceled, immediately sets
      status=CLOSED, pending_execution=False, execution_status=REJECTED, exit_price=entry_price and
      pops the trade out of open_trades. It never consults filled_quantity. This is the same broker
      behaviour that trading/broker.py:178-205 explicitly documents and defends against inside
      cancel_order_confirmed - Kite reports an order that partially executed and was then cancelled
      as CANCELLED with a non-zero filled quantity - but the defence was never applied to this
      branch. The branch is reachable because the positions snapshot is built once at the top of the
      pass (line 2802-2811) while get_order_status is called live per trade at line 2845, so the
      order book can be strictly newer than the position book. The abandoned quantity is later
      picked up as an orphan (line 3169-3183), but _handle_orphans_at_session_end only alerts unless
      force_exit_flattens_orphans is enabled, which defaults to False (line 4501-4503).

      FAILURE
      A 3-lot NIFTY CE entry LIMIT fills 1 lot and is then cancelled (exchange freeze, operator
      cancel at the Kite terminal, or broker-side cancel). It is the first trade on that contract
      today, so the positions snapshot taken moments earlier has no row for the symbol and
      `position` is None. get_order_status returns status=CANCELLED with filled_quantity=75. The
      code books the trade CLOSED at exit_price==entry_price (pnl 0), removes it from open_trades,
      and no protective stop is ever armed. 75 quantity of long premium is live at the broker with
      no stop, no local row, and - with force_exit_flattens_orphans False - is only logged CRITICAL
      and carried overnight in NRML. The system believes it is flat while holding a position.

  [MEDIUM  ] partial  trading/trade_manager.py:3076
    Unfilled remainder of a partially filled ENTRY has no cancel and no timeout

      The stale-entry cancel/replace timer (line 2884-2980,
      `broker_fill_confirmation_timeout_seconds`, default 12s) only runs inside the `position is
      None` branch and only for execution_status == 'ENTRY_SUBMITTED'. The moment a partial fill
      lands, line 3006 sets broker_quantity from netqty and line 3077 rewrites execution_status to
      'PARTIAL', so the trade now has a position row and a non-ENTRY_SUBMITTED status. Both
      preconditions for the timeout are permanently false. The branch at 3066 does accept 'PARTIAL'
      and re-runs each pass, but it only re-reads the fill and re-arms protection - it never cancels
      the still-resting remainder of the entry order and never escalates on age. The claim
      'unfilled/stale limit orders are cancelled and replaced or abandoned' therefore does not cover
      the partially-filled case at all; the only backstop is the 15:25 force exit.

      FAILURE
      A 3-lot entry LIMIT fills 1 lot at 10:05. Reconcile arms an SL-M for 1 lot and marks the trade
      PARTIAL. The remaining 2 lots rest at the exchange for the whole session. At 14:40 a spike
      sweeps the limit and the position becomes 3 lots while the protective stop still covers 1.
      Until the next reconcile tick (up to 5s) 2 lots are naked, and the P&L, risk headroom and
      concentration checks were all sized on 1 lot for four and a half hours.

  [MEDIUM  ] risk     trading/trade_manager.py:3150
    PARTIAL_EXIT with an UNKNOWN exit-order verdict leaves a residual with its stop already
    cancelled

      When an exit partially fills, _abandoned_exit_verdict (line 2445-2469) returns UNKNOWN
      whenever exit_order_id is empty, the broker has no get_order_lifecycle, the lookup raises, or
      the state string is blank/UNKNOWN. On UNKNOWN the code logs once per trade id and deliberately
      leaves the residual in place (line 3150-3160). The comment at 3135-3139 correctly notes that
      cancelling the protective stop is what precedes sending an exit at all, so in this state the
      residual quantity has no protective stop, no proven-working exit order, and nothing scheduled
      to touch it before 15:25. Refusing to double-exit on UNKNOWN is the right call, but the
      resulting unprotected window is bounded only by the session-end force exit.

      FAILURE
      A 3-lot exit fills 2 lots at 11:00 and exit_order_id was never stamped (or the lifecycle
      endpoint is rate-limited, which the codebase already treats as a live hazard). Verdict is
      UNKNOWN, the warning fires once, and 1 lot sits with its SL-M already cancelled. A 3% adverse
      move at 12:30 runs unbounded until the 15:25 force exit, because no stop exists and the
      trailing-stop path is gated off by pending_exit.

  [MEDIUM  ] risk     trading/trade_manager.py:2196
    Stale-entry force-fill guard fails open and its operator revalidation hook is never wired

      _stale_entry_still_worth_filling is what stops a stale LIMIT from being replaced with a
      guaranteed-fill MARKET order after price ran away. Its first arm, `entry_revalidator`, is read
      here but assigned nowhere in the production tree - the only assignment in the repo is
      tests/test_stale_entry_is_not_force_filled_2026_08_05.py:100 - so in live operation only the
      second arm runs. That arm compares trade.current_price against current_stop/trailing_stop and
      returns True (proceed with the market replacement) whenever either value is <= 0 or
      unparseable (lines 2211-2214). current_price is refreshed by the scanner's own price loop
      (scanner.py:25015/25022), not by TradeManager.update_trade, which returns early at line
      3874-3880 for exactly this pending_execution/zero-broker-quantity state - so the guard's
      freshness depends on a separate loop staying alive and on the stop having been stamped before
      the fill.

      FAILURE
      The scanner price loop stalls (a blocked quote fetch, or _get_live_premium_cached returning
      None so `price is None` skips the trade at scanner.py:24999) while an entry LIMIT rests.
      Twelve seconds later reconcile proves the cancel, _stale_entry_still_worth_filling reads a
      stale or zero mark, returns True, and a full-size MARKET order is placed into a move that has
      already carried the premium below the intended stop - opening a position that is a loser the
      instant it exists, which is precisely what the docstring at lines 2184-2192 says the function
      prevents.


----------------------------------------------------------------------------------------------------
POINT 20  [PARTIAL]  First start mid-session, and restart mid-session
----------------------------------------------------------------------------------------------------

WHAT THE CODE ACTUALLY DOES
    Mid-session first start and mid-session restart are genuinely implemented, not stubbed.
    TradeManager.__init__ calls load_state() (trade_manager.py:612), which rehydrates OPEN rows from
    SQLite, marks previous-session rows RECOVERY_REQUIRED, auto-closes stale ghost rows as
    STALE_SESSION_END, discards same-session rows with neither broker_order_id nor contract
    metadata, and bumps the trade-id counter above every persisted T-number, so open trades are
    neither orphaned nor double-booked. Session P&L and loss slabs are not reset to zero:
    risk_manager recomputes fut/opt/global P&L on every circuit-breaker pass from rows whose
    entry_time.date()==today (so the daily-loss latch re-arms after restart), and
    Scanner._reset_daily_counters rebuilds _trades_today plus the uninterrupted same-index loss
    streaks from one chronologically sorted tape of today's closed trades and session-ledger exits,
    deduped against real trades on (instrument, timeframe, signal_time) - and signal_time is a
    persisted column, so the dedup key survives the restart, preventing double-counting of the
    per-index budget. A 13:00 cold start still has indicator history: the REAL analysis window
    prepends 40 prior 15min bars (60 x 5min, 180 x 1min) to today's bars, and restart signal
    recovery is timeframe-aware (180s/360s measured from candle close, most-recent candle only). The
    one real gap is the repaint flag: its restart inference is one-shot and anchored to the wrong
    timestamp, so a position whose entry candle was still forming when the process died stays
    structurally unconfirmed for the rest of its life.

EVIDENCE IN CODE
    - trading/trade_manager.py:3217-3307 load_state(): previous-session rows -> RECOVERY_REQUIRED;
    is_ghost rows closed as STALE_SESSION_END; same-session rows with no broker_order_id AND no
    symbol/token closed CONTRACT_MISSING; called from __init__ (trade_manager.py:612);
    _recover_trade_counter (trade_manager.py:3340-3346) keeps new ids above every persisted
    T-number.
    - trading/trade_manager.py:3238-3249 repaint rehydration: `if not t.repaint_guard_checked and
    t.entered_intracandle:` then marks checked only when `datetime.now(IST) >= entry_time +
    timedelta(minutes=minutes)` - a single evaluation at load, anchored on entry_time rather than
    the candle close.
    - scanner.py:1016-1023 and scanner.py:23059-23063 are the only places a Trade's
    repaint_guard_checked is set (entry acknowledgement / the task it spawns); consumer
    scanner.py:12682 arms the repaint-window give-back guard on `not repaint_guard_checked`.
    - scanner.py:727-845 _reset_daily_counters rebuilds _trades_today/_losses_today/_last_exit_time
    from today's closed+open trades and the session ledger on one chronological tape;
    _last_reset_date starts None (scanner.py:544) and the rebuild is invoked from the scan paths
    (scanner.py:908, 2102, 24854), so a 13:00 start rebuilds instead of zeroing.
    - engine/risk_manager.py:262-352 recomputes fut/opt/global P&L each check from rows with
    entry_time.date()==today (closed realized + open projected-at-stop), so daily_loss_breached
    re-latches after a restart rather than re-arming spent losses.
    - scanner.py:8578-8587 REAL window = last 40 (15min) / 60 (5min) / 180 (1min) prior bars
    concatenated with today's bars; scanner.py:15959 _signal_in_restart_recovery_window with
    tests/test_restart_recovery_window.py:37-74 (360s for 15min, most-recent candle only).

FINDINGS (2)

  [MEDIUM  ] partial  trading/trade_manager.py:3246
    Repaint-flag restart inference is one-shot and keyed on entry_time, not candle close

      load_state() infers repaint_guard_checked=True only if `datetime.now(IST) >= entry_time +
      timedelta(minutes=tf)` at the moment of load. Two defects compound. (1) Wrong anchor: the
      source candle closes at signal_time + tf, not entry_time + tf, and entry_time is by definition
      mid-candle for an intracandle entry, so the deadline lands after the real close (signal_time
      is persisted - sqlite_db.py:55 TRADE_PERSISTED_COLUMNS - so the correct anchor is available).
      (2) One-shot: if the condition is false at load, nothing ever re-evaluates it. The async
      tracker _track_repaint_until_candle_close is spawned from exactly one place,
      _handle_broker_entry_result (scanner.py:1021), which never runs for a rehydrated trade, and
      scanner.py's other setters (19767, 19896) act on session-ledger candidates, not on the
      rehydrated Trade object. The flag therefore stays False permanently.
      tests/test_repaint_flag_survives_a_restart_2026_08_08.py covers only the round-trip and the
      already-elapsed case (test_load_marks_a_trade_whose_candle_has_closed is a source-text
      assertion); the not-yet-elapsed-at-load path is asserted to stay False (lines 124-127) and
      nothing covers what happens afterwards.

      FAILURE
      Intracandle LONG entry on the 15min NIFTY candle at 13:02 (candle closes 13:15); the process
      is restarted at 13:10 (or even 13:16, since entry_time+15min = 13:17). load_state sees now <
      entry_time+15min, leaves repaint_guard_checked False, and no tracker is started. For the
      remaining hours of the session scanner.py:12682 keeps repaint_window True on that position:
      let-winners-run stays off and the blunt sub-1R give-back guard stays armed, so the trade is
      cut on the first small give-back - exactly the behaviour the comment at
      trade_manager.py:3230-3237 claims to have fixed.

  [LOW     ] risk     restore.py:3
    Root-level restore.py rewrites another checkout's trade_manager.py at import time

      restore.py is not a runtime module: at module level it opens the hardcoded path
      c:/Users/sagnik/Desktop/ut index 2/trading/trade_manager.py, inserts a load_state()
      implementation at a fixed line index (lines.insert(578, ...)), and writes the file back.
      Nothing in the live system imports it, and the load_state it injects is an older, weaker
      version than the one now in trading/trade_manager.py:3217 (no stale-ghost handling, no
      missing-contract discard, no repaint inference). It sits in the project root next to main.py
      under a generic name that reads like part of the restart/recovery machinery being audited.

      FAILURE
      Anyone running `python restore.py` (or importing `restore` while exploring recovery code)
      silently line-splices a stale load_state into another checkout's trade_manager.py at line 578,
      producing a duplicate/shadowed load_state whose recovery semantics are a regression - with no
      confirmation and no backup.


----------------------------------------------------------------------------------------------------
POINT 21  [PARTIAL]  ATR flip: buy->sell transition through buffer and analysis
----------------------------------------------------------------------------------------------------

WHAT THE CODE ACTUALLY DOES
    The buffering/stabilisation half of the flip path is genuinely correct. The pending buffer is
    keyed instrument+timeframe+DIRECTION, so a mid-buffer BUY→SELL flip creates a NEW slot with
    "timestamp": now (scanner.py:23342-23347) — a fresh sell can never inherit a nearly-elapsed buy
    timer — and _evict_superseded_opposite_buffer (scanner.py:5544) drops the older opposite slot at
    buffer-creation (23350) and on the closed-candle-direct path (23307), recording it REJECTED in
    rtracker. Same-bar repaints are admitted because _live_signal_is_new (signal_processor.py:577)
    compares a "ts|signal_type" identity, not just the timestamp, and the per-index analysis cache
    key embeds each timeframe's last signal_type (scanner.py:9576), so a flip forces a full
    recompute rather than reusing the old side's graded candidates. The hole is downstream: once the
    losing side has already MATURED out of the buffer into a live trade, the only long+short guard
    is _opposite_direction_stack_block, and the adaptive dual-lane path returns an explicit ALLOW on
    an opposite-direction Lane B and returns before that guard is ever consulted.

EVIDENCE IN CODE
    - scanner.py:20944-20952 — _dual_lane_concurrency_decision: when Lane B direction != any live
    Lane A direction it tags 'Adaptive Dual Lane B reversal', sets dual_lane_entry=True and `return
    True, reason` — an ALLOW, before the same-direction proofs and before the Rs.12,000 exposure cap
    at 20972-20978
    - scanner.py:21168-21176 — `dual_allowed, dual_reason =
    self._dual_lane_concurrency_decision(...)`; `if dual_allowed is not None: ... return True` sits
    immediately ABOVE the opposite-direction check at 21178, so the allow short-circuits it
    - scanner.py:21017-21022 — 'dual_lane_entry' is in the exempt-flag list of
    _opposite_direction_stack_block, so the guard would no-op for this candidate even if the
    ordering were fixed
    - scanner.py:5544-5575 — _evict_superseded_opposite_buffer pops the
    f'{instrument}_{timeframe}_{opposite}' pending slot; only an OLDER arrow is evicted (`if old_ts
    > new_ts: return`)
    - scanner.py:23328-23350 — new-direction flip takes the `key not in self._pending_live_signals`
    branch, stores 'timestamp': now (fresh timer), then evicts the opposite buffer
    - engine/signal_processor.py:577 — `return signal_ts > last_ts or (signal_ts == last_ts and
    identity != last_identity)`: a same-bar BUY→SELL repaint is correctly seen as new

FINDINGS (3)

  [HIGH    ] bug      scanner.py:21176
    Dual-lane ALLOW on an opposite-direction Lane B short-circuits the long+short straddle guard

      _dual_lane_concurrency_decision (scanner.py:20944-20952) treats an opposite-direction Lane B
      as a first-class ALLOW: on `item_direction != candidate_direction` it tags the candidate, sets
      dual_lane_entry=True and returns (True, reason). The concurrency guard consumes that at
      21168-21176 with `if dual_allowed is not None: ... return True`, which sits two lines ABOVE
      `opposite_block = self._opposite_direction_stack_block(candidate, existing)` at 21178. So the
      one check that refuses a LONG while a SHORT is live on the same index is never reached on
      exactly the path an ATR flip takes. Nothing exits the Lane A leg first — the reversal is only
      labelled, not executed. The same early return also skips the dual-lane index exposure cap at
      20972-20978 (dual_lane_max_index_exposure_rs, default Rs.12,000), so the opposite leg is
      unmetered.

      FAILURE
      NIFTY Lane A LONG matures out of the stabilisation buffer and fills as a long CE. Two scans
      later UT1 flips bearish; the SHORT is buffered under its own key, but
      _evict_superseded_opposite_buffer finds no pending LONG (it already matured) so nothing is
      evicted. The SHORT matures, reaches the concurrency guard, _dual_lane_concurrency_decision
      sees direction != Lane A direction and returns (True, 'opposite Lane B SHORT versus Lane A
      LONG'), and 21176 returns True. A long PE is bought alongside the live long CE: two premiums,
      two spreads, theta bleeding both legs, and combined index risk above the Rs.12,000 dual-lane
      cap that the early return skipped.

  [MEDIUM  ] bug      scanner.py:21018
    dual_lane_entry is exempted inside _opposite_direction_stack_block, so it cannot fire for
    dual-lane flips

      _opposite_direction_stack_block exempts any candidate carrying one of
      ('pullback_hedge_leg','hedge_leg','dual_lane_entry','opposite_leg_entry','rare_burst_entry')
      at scanner.py:21017-21022. _dual_lane_concurrency_decision sets `dual_lane_entry = True` on
      the candidate at 20951 precisely on the opposite-direction branch, before returning the allow.
      This is a second, independent break of the same protection: reordering 21178 above the
      dual-lane return would still leave the guard a no-op for these candidates, because the flag
      that marks them was stamped on by the opposite-direction branch itself. The exemption is
      documented as covering deliberately armed hedge legs, but dual_lane_entry here is set
      automatically by a reversal, not armed by an operator.

      FAILURE
      A maintainer moves the _opposite_direction_stack_block call above the dual-lane return at
      21168 to fix the ordering. The flip-driven Lane B SHORT still passes, because 20951 already
      set dual_lane_entry=True on it and the loop at 21017-21022 returns '' before inspecting
      `existing`. The accidental long CE + long PE pair is opened anyway, and the fix appears to
      work in review while changing nothing at runtime.

  [LOW     ] bug      scanner.py:23537
    _last_live_signal_candle_time is never assigned in production code; its three pops are no-ops

      self._last_live_signal_candle_time is initialised at scanner.py:231, registered for bulk
      clearing at 3036 and cleared at 3895/3958, and popped at 23387 (future-timestamp discard),
      23400 (stale-buffer expiry) and 23537 (entry-cutoff discard). A repo-wide grep across
      scanner.py, engine/, trading/, intelligence/ finds no write to it outside tests (only
      tests/test_scanner_engine_pluggable.py:472 and tests/test_scanner_execution_guards.py:123
      stuff values in). The dict is therefore always empty in live operation and every pop is dead.
      The per-candle latch the pops are written to release does not exist; the real dedup is
      _last_signal_time/_last_signal_identity keyed f'{instrument}_{timeframe}' (23479, 23551,
      signal_processor.py:4306-4309).

      FAILURE
      A NIFTY 5min flip candidate is discarded at 23400 after exceeding max_pending_seconds. The
      `self._last_live_signal_candle_time.pop(dedup_key, None)` intended to re-arm that candle is a
      no-op, so the two tests that assert the dict is cleared pass against state no production path
      ever produces — any future guard relying on this latch to gate re-buffering of the same candle
      after a flip discard would be silently inert.


----------------------------------------------------------------------------------------------------
POINT 22  [PARTIAL]  Index spot vs index futures vs option premium kept apart
----------------------------------------------------------------------------------------------------

WHAT THE CODE ACTUALLY DOES
    The three series are genuinely fetched and cached separately: index spot by index token
    (scanner.py:4707), futures LTP by the NFO/BFO future token (scanner.py:4739), option LTP by the
    option token into a dedicated `_premium_cache` (scanner.py:4760-4770). Option marks go through
    `_active_option_decision_mark` (scanner.py:8043), which returns an explicit (mark, source,
    verified) triple that the exit loop records as `quote_source`/`quote_verified` and uses to
    overwrite the raw premium before handing it to TradeManager (scanner.py:25017-25037). The
    repaint-abort fix is complete on both halves: OPT marks on its own premium, FUT on the live
    futures LTP with a basis-adjusted spot fallback (scanner.py:24790-24844). However, the same
    class of bug survives elsewhere. `Trade.entry_spot` - the field every spot-to-premium conversion
    keys off - is silently defaulted to the CONTRACT price by `entry_spot=entry_spot or price`
    (trade_manager.py:3603), while all three OPT entry sites feed it an unguarded
    `candles.get_latest_price(...)` that can return None. A second silent substitution prices a
    futures conversion off index spot when the broker futures LTP fetch fails
    (scanner.py:22122/22133). And TradeManager's own synthetic-premium branches set `current_price`
    without setting or clearing the staleness flags the scanner path maintains.

EVIDENCE IN CODE
    - scanner.py:4707/4739/4760 - spot by index token, futures by NFO/BFO fut token, option LTP by
    option token into a separate `_premium_cache` keyed by option token and by NAME-CE/PE: the three
    series are distinct at the fetch layer
    - scanner.py:8043-8092 `_active_option_decision_mark` returns (mark, source, verified); a
    contract LTP older than `option_internal_mark_quote_age_seconds` is replaced by a synthetic mark
    tagged `synthetic_option_mark_stale_contract_ltp` with verified=False - theoretical marks ARE
    flagged on this path
    - scanner.py:25017-25037 stores `quote_source`/`quote_verified` on the trade and overwrites
    `real_premium` with the vetted mark before calling update_trade, so a rejected stale LTP cannot
    reach the stop logic
    - scanner.py:24790-24844 `_repaint_abort_exit_price` marks OPT on premium
    (current_price/entry_price) and FUT on `_get_live_fut_ltp_cached` with
    `_fut_price_from_spot_level` as fallback - the committed fix is correct and covers both halves
    - trading/trade_manager.py:3603 `entry_spot=entry_spot or price` - for an OPT trade `price` is
    the option premium, so a missing spot silently writes premium into the spot-space field
    - scanner.py:22122 `price = spot if spot > 0 else price`, then 22130 `price = fut_ltp` only if
    the broker call succeeds, and 22133 rejects only `price <= 0` - a FUT candidate keeps INDEX SPOT
    as its entry price when the futures LTP fetch fails

FINDINGS (4)

  [CRITICAL] bug      trading/trade_manager.py:3603   [RE-VERIFIED]
    `entry_spot = entry_spot or price` stores the option PREMIUM in the spot field

      `open_trade` defaults `entry_spot` to `price`. For an OPT trade `price` is the option premium
      (~120), not index spot (~24850). All three OPT entry sites pass an unguarded spot lookup -
      scanner.py:1373 `actual_spot = self.candles.get_latest_price(candidate.instrument)` ->
      scanner.py:1402, and the same pattern at 23941->23964 and 24028->24077 - and
      `get_latest_price` returns None/0 when no spot tick is cached for that index. `None or price`
      yields the premium. This also kills the guard at trade_manager.py:3911-3914 whose own comment
      reads 'If entry_spot is missing, do NOT use spot price as option price! Keep it at entry_price
      to avoid massive fake P&L': that else-branch is unreachable whenever price > 0 because line
      3603 guarantees entry_spot > 0. The synthetic branch at 3904-3910 then runs with a
      premium-valued entry_spot and, unlike the scanner path, applies NO plausibility sanitisation -
      only `max(0.05, ...)` at 3917. Every spot<->premium converter keys off the same field
      (scanner.py:25042-25055 prem_to_spot/spot_to_prem), so spot-space stops and targets are
      corrupted too.

      FAILURE
      NIFTY 25000CE bought at premium 120, instrument_multiplier 0.5, 75 qty. The spot tick is
      briefly unavailable at entry so scanner.py:1373 returns None and the trade is created with
      entry_spot = 120. On any later heartbeat where the live option quote is missing (real_premium
      None), trade_manager.py:3905 computes spot_change = 24850 - 120 = 24730, premium_move = 12365,
      current_price = 12485. The position reports roughly Rs 9.2 lakh of fabricated profit, fires
      TARGET_HIT, and books a fictitious exit price into the ledger.

  [HIGH    ] bug      scanner.py:22122
    Mixed-book FUT conversion prices the futures leg at INDEX SPOT when the futures LTP fetch fails

      In the multi-signal mixed-book FUT conversion, line 22122 seeds `price` with the index spot
      for any non-FUT candidate. Lines 22124-22132 try to overwrite it with the real futures LTP but
      swallow every exception and skip on a 0/None return. The only validation is `if price <= 0` at
      22133. So when the broker futures quote is unavailable the candidate is stamped `inst_type =
      'FUT'` (22156) with `candidate.price` = index spot, and the stop/target at 22147-22154 are
      derived from spot as well. Line 22169 then does `candidate.entry_spot = float(spot or price)`,
      which additionally lets the FUTURES price land in the spot field when spot is 0. This is the
      same spot/futures conflation the recent repaint commit fixed, but in the entry path; nothing
      downstream re-validates that a FUT entry price is actually a futures price.

      FAILURE
      NIFTY future trades at 24905 while spot is 24850 (55-point basis). `self.data.get_ltp('NFO',
      fut_symbol, token)` throws or returns 0 (rate limit or token gap), so the conversion is
      accepted with entry 24850 and stop/target derived from it. The order fills near 24905, so the
      position opens ~55 points against the recorded entry on every lot, the stop sits 55 points
      nearer than intended, and P&L is misreported by basis x quantity. In the mirror case (spot
      missing), entry_spot holds the futures price and trade_manager.py:3922 marks the future at
      entry_price + (current_spot - entry_spot) = entry - basis.

  [MEDIUM  ] partial  trading/trade_manager.py:3898
    TradeManager synthetic-premium fallbacks never set or clear quote_source/quote_verified

      The scanner path flags every theoretical mark (scanner.py:25023-25024 writes `quote_source`
      and `quote_verified` alongside `current_price`). TradeManager's `update_trade` has three
      independent fallback branches - 3893-3903 implausible-quote fallback, 3904-3910 no-quote delta
      model, 3911-3914 entry-price hold - that assign `trade.current_price` but never touch
      `quote_source`/`quote_verified`. Those attributes retain whatever the previous scanner cycle
      wrote, typically `broker_contract_ltp` / True. Any consumer that trusts the flag (dashboard
      display, exit gating) sees a verified broker mark while the number is a theoretical
      delta-model estimate. The claim that a theoretical premium is always flagged holds only on the
      scanner-driven path, not on update_trade's own fallbacks.

      FAILURE
      A cycle resolves a fresh broker LTP, so quote_source='broker_contract_ltp' and
      quote_verified=True. The next cycle loses the option feed; update_trade is called with
      real_premium=None and takes the 3904 branch, computing current_price from the spot-delta
      model. The trade still reports quote_verified=True, so a staleness-aware consumer treats a
      modelled premium as broker-confirmed and no operator warning is raised.

  [MEDIUM  ] bug      scanner.py:428
    Reconstructed trades resolve entry_spot to the candidate premium, with delta forced to 1.0

      `entry_spot=float(getattr(entry_cand, 'entry_spot', getattr(entry_cand, 'price', 0.0)))` -
      both roads lead to a wrong spot. If the candidate lacks the attribute, the default is the
      candidate PRICE, which for an OPT signal is the premium. If the attribute exists but is 0.0,
      0.0 is passed and trade_manager.py:3603's `or price` substitutes the premium anyway. The
      adjacent `instrument_multiplier=1.0` at line 425 is applied regardless of `inst_type` (line
      424 preserves OPT), so the delta model runs with delta 1.0 on options.
      engine/scanner_recalculation.py:337 repeats the identical expression. These are
      ghost/reconstruction rows, which limits blast radius to the displayed book rather than to live
      orders.

      FAILURE
      A day's signal ledger is reconstructed after a restart for a BANKNIFTY CE signal at premium
      210 with spot 52400. entry_spot resolves to 210 and instrument_multiplier to 1.0, so the next
      mark computes 210 + (52400 - 210) * 1.0 = 52400 - the option is displayed at the index spot
      value, reproducing in the reconstructed trades panel the exact fabricated-P&L display the
      repaint fix eliminated.


----------------------------------------------------------------------------------------------------
POINT 23  [PARTIAL]  AngelOne SmartAPI retired, Kite/Zerodha in its place
----------------------------------------------------------------------------------------------------

WHAT THE CODE ACTUALLY DOES
    AngelOne is genuinely retired as a broker: `MarketDataProvider.__init__` sets `self.smart_api =
    None` and nothing in the repo ever reassigns it, so every legacy Angel REST branch (`ltpData`,
    `getMarketData`, `get_historical_candles_angel`, the prev-close fetch, the history fallback in
    `get_historical_candles`) is unreachable dead code, no SmartConnect client is ever constructed,
    and the live dashboard JS carries no AngelOne pill (only gw_kite/gw_fyers/gw_yahoo). What did
    NOT survive the migration is the pacing and backoff layer: `trading/api_rate_limiter.py`
    (SharedApiRateLimiter, AngelHistoricalRateLimiter, AngelClientQuotaRegistry) is reachable only
    from inside those dead `smart_api`-guarded branches, and the Kite replacement —
    `KiteDataSource._call` — applies no pacing, no rolling quota, and no backoff, swallowing every
    exception into a `default` return. The Angel-shaped throttle telemetry
    (`angel_rest_backoff_active`, `angel_rest_quote_gate`) is still published in the provider health
    payload where it can never change from its idle values, and there is no Kite-side equivalent.
    Category (b) live-reachable Angel call paths: none found — every one is gated behind the
    permanently-None `smart_api`. The real residue is category (c): Kite paths that lost the
    Angel-era protections rather than inheriting them.

EVIDENCE IN CODE
    - data/market_data.py:207 `self.smart_api = None` — a repo-wide grep for `smart_api =` returns
    only this one assignment, so the guards at 2154, 2706, 2954, 3450 (`if self.is_connected and
    self.smart_api`) and `get_historical_candles_angel` (2753) are all unreachable; no SmartConnect
    is ever built.
    - config/inert_settings.py:79 documents the retirement ("AngelOne is retired; nothing constructs
    a SmartConnect client."), and the only remaining Angel credential consumers are
    engine/recalculation_worker.py:78-81 passing vestigial kwargs that market_data.py:194 marks as
    unused.
    - trading/api_rate_limiter.py:159/262/338 — the only non-test call sites of
    `get_shared_api_limiter`/`get_shared_history_limiter` are data/market_data.py:380, 410, 1542,
    2784, every one behind `smart_api is not None` or inside the dead Angel history fetch;
    `get_shared_client_quota`/`AngelClientQuotaRegistry` has zero call sites anywhere outside its
    own module.
    - data/kite_market_data.py:73-86 `_call()` — resolves the api, calls `fn(api)`, and on any
    exception logs a warning and returns `default`; there is no min-interval sleep, no quota
    reservation, and no cooldown, and it is the single funnel for `instruments()` (100),
    `quote/ltp`, and `historical_data` (556-560).
    - trading/kite_broker.py:111 `self._min_call_interval = 0.34` with the pacing at 158-171 —
    order-side pacing exists but is a bare inter-call gap on one lock; it counts no rolling
    per-minute or per-day order budget, and it is not shared with the data path's client.
    - dashboard/static/js/app.js:2371-2375 `updateGatewayStatus` maps only
    `gw_kite`/`gw_fyers`/`gw_yahoo` (status strip at 2436-2438 is K/F/Y); `gw_angel` survives only
    in dashboard/static_backup/, which is not served. The single live hit, app.js:903, is a
    log-classification regex matching the word "angel" — inert.

FINDINGS (4)

  [HIGH    ] partial  data/kite_market_data.py:73   [RE-VERIFIED]
    Kite REST data path has no pacing, quota or backoff; the Angel limiter stack is stranded on dead
    code

      Every Kite data REST call (`instruments()` at line 100, quote/LTP, `historical_data` at
      556-560) funnels through `_call()`, which does `return fn(api)` inside a bare try/except that
      logs a warning and returns `default`. There is no min-interval sleep, no rolling-quota
      reservation, and no cooldown after a throttle. The protections that used to cover the
      equivalent AngelOne calls all still exist in trading/api_rate_limiter.py, but their only
      non-test call sites (data/market_data.py:380, 410, 1542, 2784) sit behind `if self.smart_api
      is not None` or inside `get_historical_candles_angel`, and market_data.py:207 sets `smart_api
      = None` with no reassignment anywhere in the repo. So the migration deleted the broker but
      left its rate-limit layer attached to the corpse instead of porting it. The Angel side also
      had a backoff ladder (`_mark_angel_rest_backoff`, settings
      `angel_rest_backoff_seconds`/`angel_rest_backoff_max_seconds`, market_data.py:348-372) that
      made a throttled endpoint stop being re-hit; the Kite path has no analogue, so a 429 is
      retried at full rate on the next tick.

      FAILURE
      A scan pass warms history for several indices across 1min/5min/15min while the option-chain
      path issues quotes in the same second. Kite's historical endpoint (3 req/s) returns
      429/NetworkException. `_call` catches it, logs `Kite historical_data failed: ...`, and returns
      `default=None`, so `get_historical()` returns an empty DataFrame. The scanner sees no candles
      rather than an error, indicators fall back to stale or absent data, and because nothing
      records a cooldown the identical burst re-fires on the next scan tick, keeping the throttle
      alive indefinitely.

  [MEDIUM  ] missing  trading/api_rate_limiter.py:338
    AngelClientQuotaRegistry order budget never rewired to Kite; no rolling order count is enforced

      `AngelClientQuotaRegistry` (line 277) enforced rolling per-endpoint budgets, including a
      combined place/modify/cancel `order_family` budget of 500/min and 1000/hr, and
      `get_shared_client_quota` (338) was documented as surviving SDK replacement across reauth. A
      grep across all non-test code finds zero call sites for either symbol — the class and its
      factory are now unreachable. The Kite replacement in trading/kite_broker.py:148-171 enforces
      only `_min_call_interval = 0.34` (line 111), a stateless gap between consecutive calls on
      `_order_lock`. That bounds instantaneous rate but counts nothing over a window, so Kite's
      per-minute and per-day order caps are entirely unpoliced, and the `rejections` counter that
      used to make quota pressure visible is gone with no substitute.

      FAILURE
      An order-retry loop or a churny exit/re-entry session issues place+modify+cancel continuously.
      At the 0.34s floor that is roughly 176 order-family calls per minute sustained — under Kite's
      200/min but with nothing tracking the daily cap. Across a full session the account crosses
      Kite's daily order limit; from that point every `place_order` returns a broker refusal, and
      since no quota counter exists, the failure surfaces only as opaque per-order rejections with
      no operator-visible signal that a budget was exhausted.

  [LOW     ] risk     data/market_source_health.py:202
    WS cache entries with no `source` key are labelled `angel_ws` and silently lose order authority

      `build_market_source_snapshot` reads `source = str(cached.get("source") or "angel_ws")` for
      each `_ws_cache` entry, then `_source_record` (line 153) grants `order_authority` only when
      the name starts with `kite`, and `entry_eligible` requires `has_order_authority`. So any
      tick-cache entry that lacks a `source` key is attributed to the retired broker and stripped of
      entry eligibility. Today the Kite ticker writes `"source": "kite_ws"` (market_data.py:1366) so
      the default is not normally hit, but the same cache is mutated elsewhere (e.g.
      `cached.update(...)` at market_data.py:1960) and the very same dict is defaulted to `"ws"` —
      not `"angel_ws"` — at market_data.py:2332, so the two readers disagree about what an
      unlabelled entry means. The comment block at lines 180-188 records that this exact class of
      mistake previously made the only order-authorising realtime feed invisible.

      FAILURE
      Any future write path that seeds or partially updates `_ws_cache[token]` without carrying a
      `source` key — the pattern already used at market_data.py:1960 — makes that index's realtime
      record report `source="angel_ws"`, `order_authority=False`, `entry_eligible=False`.
      `_filter_live_candidates_by_source` then drops every live entry for that index whenever the
      slower REST record is older than its freshness window, silently suppressing entries with no
      error raised.

  [LOW     ] risk     data/market_data.py:1531
    Health payload publishes Angel throttle telemetry that can never fire, with no Kite equivalent

      The health dict exports `angel_rest_backoff_active`, `angel_rest_backoff_seconds_remaining`,
      `angel_rest_backoff_failures` (1531-1539) and, under `if self.smart_api is not None`,
      `angel_rest_quote_gate` (1540-1550). The only writers of
      `_angel_rest_backoff_until`/`_angel_rest_backoff_failures` are `_mark_angel_rest_backoff`
      (348) and `_mark_angel_rest_success` (361), both reachable only from
      `_best_effort_angel_rest_call` and the `ltpData` block, all gated on the permanently-None
      `smart_api`. The fields are therefore frozen at `False`/`0.0`/`0`, and `angel_rest_quote_gate`
      never appears at all. Because finding 1 leaves the Kite data path with no backoff state of its
      own, the health surface offers a broker-throttle indicator that reads healthy by construction
      while the feed that actually matters has no instrumentation.

      FAILURE
      Kite starts throttling the quote/history endpoints mid-session. An operator (or any automated
      check) reading the provider health payload sees `angel_rest_backoff_active: false` and
      `angel_rest_backoff_failures: 0` and concludes broker pacing is fine, while
      `KiteDataSource._call` is swallowing 429s into empty frames. The misleading-green indicator
      delays diagnosis of a live data outage.


----------------------------------------------------------------------------------------------------
POINT 24  [PARTIAL]  Dynamic RR, target, exit and stop-loss
----------------------------------------------------------------------------------------------------

WHAT THE CODE ACTUALLY DOES
    RR, target and exit ARE genuinely dynamic on the default path: `dynamic_rr()`
    (engine/signal_quality_policy.py:351-399) scales RR from grade + confidence + intel score +
    timeframe, target is always derived as entry ± (stop_distance × RR), and the option stop
    distance is ATR-aware and per-timeframe bounded (`_option_premium_stop_distance`,
    engine/signal_processor.py:1414-1469, using the `option_stop_tf_atr_aware` min/max % bands). But
    the stop is the weakest of the four. On every special entry path (one-way cascade, late-session
    reclaim, volume-spike / LTP-shock / reversion scalp) the ATR/structure stop is passed through
    `min(best_sig.stop_distance, flat_pct_of_premium)` at engine/signal_processor.py:5747-5760, and
    with .env pinning all three flat percentages to 5, the flat cap almost always binds and discards
    the candle-derived stop. Those same paths also hard-replace the dynamic RR with the constant
    1.5, below dynamic_rr's own declared floor of 1.60. The intelligence-driven stop flex exists but
    is default-OFF in both settings and .env, and nothing clamps the stop against the per-lot rupee
    catastrophe cap or against the live bid-ask spread.

EVIDENCE IN CODE
    - engine/signal_processor.py:5747-5760 — option_stop_pct = one_way_cascade_options_sl_pct /
    late_session_reclaim_options_sl_pct / volume_spike_entry_options_sl_pct (all '5' in .env) else
    scanner.options_sl_pct (15.0); then `index_stop_distance = min(best_sig.stop_distance,
    hard_cap_index_sl)` — the flat % is a hard cap over the ATR stop.
    - engine/signal_processor.py:5635 computes `final_rr = self._dynamic_rr(best_grade, best_conf,
    intel_score, timeframe=best_tf)`, then 5649/5651 unconditionally overwrite it: `final_rr =
    float(getattr(settings, 'one_way_cascade_rr', 1.5) or 1.5)`.
    - engine/signal_quality_policy.py:369-370 — `max_rr = max(1.60, ...)`, `min_rr = min(max(1.60,
    ...), max_rr)`: the policy's own hard RR floor is 1.60, so the 1.5 overrides sit below it.
    - engine/signal_processor.py:1440 `pct_stop = max(0.5, price * (opt_sl_pct / 100.0))` and 1468
    `return max(0.5, stop)` — the only floor on an option stop distance is Rs 0.50; no
    bid-ask/spread term appears anywhere in stop placement.
    - config/settings.py:1233 `dynamic_stop_from_intelligence: bool = False` and .env line 768
    DYNAMIC_STOP_FROM_INTELLIGENCE='False' — `_intelligence_stop_pct`
    (engine/signal_processor.py:332-334) returns base unchanged in production.
    - scanner.py:14795/14843/14887 — `force_loss = rupee_loss_band_force_loss_rs_opt (3000)`,
    `force_loss *= ladder_scale` (per lot), `if loss >= force_loss:` returns RUPEE_LOSS_BAND_EXIT
    unconditionally; nothing feeds this number back into stop placement, and
    effective_trade_loss_cap (config/settings.py:1806) is consumed only by
    engine/position_sizing.py:130 in risk_budget mode (AUTO_RISK_SIZING_ENABLED='False' in .env).

FINDINGS (5)

  [HIGH    ] partial  engine/signal_processor.py:5760
    Special-entry option stops discard the ATR stop via a flat 5% premium cap

      `index_stop_distance = min(best_sig.stop_distance, hard_cap_index_sl)` where
      `hard_cap_index_sl = est_premium * option_stop_pct/100 / |instrument_multiplier|`. For
      one_way_cascade_entry, late_session_reclaim_entry and
      volume_spike/market_ltp_shock/reversion_scalp entries, option_stop_pct is a flat constant that
      .env pins at 5 (ONE_WAY_CASCADE_OPTIONS_SL_PCT, LATE_SESSION_RECLAIM_OPTIONS_SL_PCT,
      VOLUME_SPIKE_ENTRY_OPTIONS_SL_PCT, MISSED_SIGNAL_REENTRY_OPTIONS_SL_PCT all = '5'). The
      candle-derived stop_distance only survives if it is already tighter than the flat cap, which
      it essentially never is. Target inherits the truncated distance (5764/5767: target = spot +/-
      index_stop_distance * final_rr), so target is dynamic only in its RR multiplier, not in its
      base.

      FAILURE
      NIFTY one-way cascade LONG, spot 24000, ATM premium ~150, signed delta ~0.5. The candle/ATR
      stop_distance from the 15min signal is ~55 index points. hard_cap_index_sl = 150*0.05/0.5 = 15
      index points. min() picks 15. The trade is stopped out on a 15-point wiggle the ATR said was
      noise, and its target is set at 15*1.5 = 22.5 points instead of the structure-derived 82.5.

  [HIGH    ] bug      engine/signal_processor.py:5651
    Cascade/late-reclaim RR override of 1.5 sits below dynamic_rr's own 1.60 floor

      Line 5635 computes the quality-scaled RR from grade, confidence, intel score and timeframe.
      Lines 5649 and 5651 then replace it outright (not max(), unlike the volume-spike branch at
      5644-5647) with `late_session_reclaim_rr` / `one_way_cascade_rr`, both defaulting to 1.5
      (config/settings.py:850). engine/signal_quality_policy.py:369-370 clamps every dynamically
      computed RR to a minimum of 1.60, so these two paths deliberately take a reward-to-risk the
      policy layer declares unacceptable, and grade/confidence/intel are discarded entirely for
      those entries.

      FAILURE
      An A+ grade cascade signal with confidence 0.85 and intel 78 computes dynamic RR ~4.0 at line
      5635. Line 5651 overwrites it with 1.5. The target is set at 1.5R and the trade books out
      there; the same setup entered through the normal path would have run to 4R. Every cascade
      entry is systematically capped below the system's own minimum acceptable RR.

  [HIGH    ] bug      engine/signal_processor.py:1440
    Option stop floors at Rs 0.50 with no bid-ask term, so cheap premiums stop inside the spread

      Both branches of `_option_premium_stop_distance` floor at `max(0.5, ...)` (line 1440 for the
      pct path, 1468 for the ATR path) and neither consults the live bid/ask. The same is true of
      the index-points path at 5758-5760. Nothing in engine/ or scanner.py applies a spread-derived
      minimum to stop placement. The only spread-aware code, `Scanner._spread_aware_exit_decision`
      (scanner.py:24655-24754), defers *profit* exits only, is gated on `spread_aware_exit_enabled`
      which is False by default (config/settings.py:1441) and 'False' in .env line 753, and never
      runs on the entry/stop path.

      FAILURE
      Weekly OTM CE, bid 11.00 / ask 12.00 (Rs 1.00 spread, common on non-ATM weeklies). Cascade
      entry fills at the ask 12.00 with the flat 5% stop: 12.00*0.05 = 0.60 premium points, stop at
      11.40. The bid is already 11.00, i.e. below the stop at the moment of entry, so the first
      LTP/bid print stops the trade out for a guaranteed loss with zero adverse move in the
      underlying.

  [MEDIUM  ] partial  config/settings.py:1233
    Intelligence-driven stop flex is default-OFF, so only RR/target/exit read intelligence

      `_intelligence_stop_pct` (engine/signal_processor.py:315-349) is the only place the intel
      score touches the stop, and lines 332-334 return the base percentage unchanged unless
      `dynamic_stop_from_intelligence` is truthy. The dataclass default is False
      (config/settings.py:1233) and .env line 768 sets DYNAMIC_STOP_FROM_INTELLIGENCE='False'. The
      in-code comment at 1430-1434 states RR, target and exit all flex with the intelligence score
      and that the stop was the one that did not; that remains true as shipped. So of the four
      quantities in the claim, only three consume market intelligence; the stop is driven by ATR +
      regime + the flat % cap only.

      FAILURE
      Two identical-structure BANKNIFTY entries, one with intel score 88 (all pillars corroborating)
      and one with intel 22 (order flow and OI opposing), receive the exact same stop percentage.
      The high-conviction trade gets no extra room and the low-conviction trade gets no tightening,
      because the flex function short-circuits at line 334.

  [MEDIUM  ] risk     scanner.py:14887
    Stop is never clamped to the Rs 3000/lot cap, so the flat ladder fires before the structural
    stop

      `rupee_loss_band_force_loss_rs_opt` defaults to 3000.0 (config/settings.py:736), is scaled per
      lot at scanner.py:14843, and triggers an unconditional RUPEE_LOSS_BAND_EXIT at 14887 with no
      severity or flag precondition. Stop placement (engine/signal_processor.py:1414-1469,
      5747-5760) has no knowledge of this number. The only rupee cap that could bind,
      `effective_trade_loss_cap` (config/settings.py:1806), is consumed solely by
      engine/position_sizing.py:130 and only in risk_budget mode; .env sets
      AUTO_RISK_SIZING_ENABLED='False' (fixed_lots path, engine/position_sizing.py:265-285, which
      computes risk_per_lot but never compares it to any cap) and MAX_TRADE_LOSS_ABS='0.0'. Result:
      for higher-priced options the protective stop is unreachable and the exit is governed by a
      flat rupee constant rather than by candle structure.

      FAILURE
      NIFTY option at premium 400, lot size 65, default OPTIONS_SL_PCT='15.0'. Implied per-lot loss
      at the stop is 400*0.15*65 = Rs 3900, above the Rs 3000/lot force_loss. The trade always exits
      on the catastrophe ladder at Rs 3000/lot, so the ATR/timeframe-derived stop never governs the
      exit, and the entry-risk accounting in engine/entry_risk_policy.py (which prices risk off the
      stop, lines 50-94) overstates real per-trade risk by ~30%.


----------------------------------------------------------------------------------------------------
POINT 25  [PARTIAL]  Full chart+scanner read with no signal delay; server executor
----------------------------------------------------------------------------------------------------

WHAT THE CODE ACTUALLY DOES
    The refactor is real but incomplete. (B) A dedicated `_chart_executor =
    ThreadPoolExecutor(max_workers=2)` exists (dashboard/server.py:141), is distinct from the
    default pool, is shut down on exit, and both entry points (ws `get_chart` at :2013, REST
    `api_chart` at :2854) route through `_build_chart_off_loop`. The live spot overlay is genuinely
    tick-driven: `d.ltp || d.spot_price` in app.js is fed from `CandleBuilder.get_latest_price`, a
    pure dict read of `_latest_prices` populated by `update_from_tick`; there is no
    `get_ltp`/`quote()` call anywhere in dashboard/server.py or engine/scanner_dashboard.py, so no
    broker hammering. (A) The "no signal-generation delay" half does NOT hold: the scan cycle still
    calls `_build_chart_from_cache(name)` inline on the trading loop (scanner.py:9643), and that
    call now has to acquire the same `_chart_build_lock` a chart-pool thread holds across a
    `pd.read_csv` and a full indicator/dual-lane rebuild — so the dashboard can stall the scan loop,
    which is exactly the coupling the change claims to remove. On max_workers=2: queueing is not the
    risk (only the single active instrument+timeframe is ever built, and the RLock serialises builds
    anyway, so the 4x3 fan-out never materialises); the second worker buys no throughput and instead
    enables a cross-request interleave that can return a stale chart stamped as current.

EVIDENCE IN CODE
    - dashboard/server.py:141 `_chart_executor = ThreadPoolExecutor(max_workers=2,
    thread_name_prefix="chart")`; :155-165 `_build_chart_off_loop` marshals only the two
    `active_chart_*` writes onto the scanner loop, then `run_in_executor(_chart_executor, ...
    get_chart_snapshot(...))`
    - engine/scanner_chart_mixin.py:525-526 `get_chart_snapshot` itself does
    `self.active_chart_instrument = instrument` / `self.active_chart_tf = timeframe` — on the
    chart-pool thread, defeating the loop-marshalling the server docstring relies on
    - engine/scanner_chart_mixin.py:47-58 the build reads
    `active_chart_tf`/`active_chart_instrument` and `continue`s every timeframe that is not the
    active one; :551-554 on an empty build it falls back to the very cache
    `_chart_cache_matches_candles` just rejected, and :563 stamps `"timestamp": datetime.now(IST)`
    - scanner.py:9643 `chart_data = self._build_chart_from_cache(name) if getattr(self,
    "chart_stream_enabled", True) else {}` — still inline on the trading loop, so it blocks on
    `_chart_build_lock` (engine/scanner_chart_mixin.py:39-42)
    - engine/scanner_chart_mixin.py:96-98 `if len(df) <= 5 or volume_sum <= 0: df =
    self._hydrate_chart_frame_from_disk(...)` → :285 `pd.read_csv(disk_path,
    parse_dates=["timestamp"])`, executed while holding `_chart_build_lock`
    - data/candle_builder.py:90-92 `get_latest_price` returns `self._latest_prices.get(instrument,
    0.0)`, filled at :118 inside `update_from_tick` — the spot overlay is tick-cache only, no broker
    round-trip; `update_candles` (:240, lock at :279) is properly locked, so the disk-hydration
    write-back is not itself an unlocked mutation

FINDINGS (4)

  [HIGH    ] partial  scanner.py:9643   [RE-VERIFIED]
    Chart builds still block the trading loop: scan cycle contends on _chart_build_lock with the
    chart pool

      The refactor moved the dashboard's chart build off the scanner event loop, but the scan cycle
      itself still calls `self._build_chart_from_cache(name)` synchronously at scanner.py:9643. That
      method now takes `_chart_build_lock` (engine/scanner_chart_mixin.py:39-42), the same RLock a
      chart-executor thread holds for the entire duration of a forced rebuild — including
      `pd.read_csv` disk hydration (mixin:285), `_chart_result_for_display` and
      `_chart_result_with_adaptive_dual_lanes`. So a dashboard chart request can now make the
      trading loop wait. The in-code justification (server.py:139-140, 'builds are ~10-20ms,
      contention is not a concern') does not cover the forced-rebuild path, which is the path every
      dashboard request takes on a cache miss and which does blocking file I/O. Latency was not
      eliminated; it was moved from 'chart build runs on the loop' to 'chart build blocks the loop
      via a mutex'.

      FAILURE
      Operator switches the dashboard to BANKNIFTY/15min during the session. `get_chart_snapshot`
      misses the cache, calls `_build_chart_from_cache(force=True)` on the chart pool, which takes
      `_chart_build_lock` and (because index candles report zero volume) does a full `pd.read_csv`
      of data_store/candles/BANKNIFTY_15min.csv. On the next `_scan_cycle`, scanner.py:9643 blocks
      on that same lock for the whole read+rebuild, delaying signal generation and exit checks for
      every index in that cycle.

      >> SEVERITY REVISED DOWN by me. I measured the in-lock disk read on the real files:
      >> pd.read_csv on NIFTY_1min.csv (5000 rows) = 7.6/10.0 ms min/median, NIFTY_5min.csv =
      >> 3.7/4.7 ms. It also fires only for the ACTIVE instrument+timeframe
      >> (scanner_chart_mixin.py:~78 skips the rest). Real and worth fixing, but ~10 ms -- not the
      >> loop stall the HIGH label implied.

  [MEDIUM  ] bug      engine/scanner_chart_mixin.py:525
    get_chart_snapshot re-writes active_chart_instrument off-loop; concurrent workers can serve a
    stale chart

      `_build_chart_off_loop` (dashboard/server.py:155-160) deliberately marshals the
      `active_chart_instrument`/`active_chart_tf` writes onto the scanner loop, documenting that
      setting them from another thread 'could have it refresh the wrong one'. But
      `get_chart_snapshot` — which runs ON the chart pool thread — sets both attributes again at
      mixin:525-526. The guard is therefore dead code. Because `_build_chart_from_cache_locked`
      decides what to build by reading those two globals (mixin:47-48, `continue` at :57), a second
      in-flight request for a different instrument silently turns the first request's `force=True`
      rebuild into a no-op. The empty result then falls through to :551-554, which returns the
      cached payload that `_chart_cache_matches_candles` had already judged non-current, with
      `"timestamp": datetime.now(IST)` attached at :563 — the precise 'stale bars presented as
      current' defect that :475-489 documents and claims to have fixed. max_workers=2 is what makes
      this reachable; max_workers=1 would serialise it away.

      FAILURE
      A browser tab issues GET /api/chart?instrument=NIFTY&tf=5min while another client's websocket
      sends get_chart for BANKNIFTY. Both land on the 2-worker pool. The BANKNIFTY thread sets
      active_chart_instrument='BANKNIFTY' before the NIFTY thread acquires `_chart_build_lock`; the
      NIFTY build then skips all three timeframes, returns {}, and the endpoint answers with the
      pre-switch NIFTY cache stamped with the current time. The operator reads bars from minutes ago
      as live.

  [MEDIUM  ] bug      engine/scanner_chart_mixin.py:97
    volume_sum <= 0 forces a pd.read_csv on every index chart rebuild, not just on cold start

      `_hydrate_chart_frame_from_disk` is documented as a restart-recovery path ('Load chart candles
      from the local cache when memory is empty after restart', mixin:271), but the trigger at :97
      is `if len(df) <= 5 or volume_sum <= 0`. Index spot candles routinely carry no traded volume —
      the codebase acknowledges this at dashboard/static/js/app.js:1809 ('zero because index spot
      candles commonly do not carry traded volume'). So for a spot-driven index chart the
      `volume_sum <= 0` branch is true on essentially every rebuild, and mixin:285 does a full
      `pd.read_csv` of the timeframe CSV before the early-out at :289 can decide the disk copy is
      not better. This is the blocking I/O that finding #1's lock holds across, and it repeats on
      every cache-missing dashboard chart request rather than once per process.

      FAILURE
      NIFTY 1min chart, volume feed absent or zeroed (a normal state for index spot). Each tab
      switch / REST chart poll takes the chart lock, reads and parses the entire NIFTY_1min.csv from
      disk, discards it at :289-290 because it is no worse than memory, and releases the lock.
      Throughput of the endpoint degrades with CSV size and the trading loop pays the wait at
      scanner.py:9643.

  [LOW     ] risk     engine/scanner_chart_mixin.py:60
    Chart pool reads self._cached_results while the scan loop mutates it, outside any shared lock

      `_build_chart_from_cache_locked` reads `self._cached_results.get(key)` at :60 and then
      consumes `result['signals']` (:72) and passes `result` into `_chart_result_for_display`.
      `_chart_build_lock` only serialises chart builders against each other; it does not exclude the
      scan cycle's own writer of `_cached_results`. The dict lookup is GIL-atomic, but the
      per-instrument result dict it returns is mutated in place by the analysis path, so the chart
      thread can read a half-updated result (signals list appended to while `len()` is taken for the
      fingerprint at :72/:78). Consequence is display-only — a fingerprint that does not match the
      payload it labels, causing a chart that looks cached-and-current for one extra cycle. Not
      observed to affect trade logic.

      FAILURE
      Scan cycle is mid-way through replacing the signals list for NIFTY_5min when a dashboard
      request on the chart pool computes `sig_count = len(result.get('signals', []))` for the
      fingerprint. The fingerprint records the intermediate count, is stored at :94, and the next
      scan-loop build sees a matching fingerprint for a payload built from the other signal set, so
      the stale chart is served until a value in the fingerprint tuple changes.


----------------------------------------------------------------------------------------------------
POINT 26  [PARTIAL]  How a generated signal is graded
----------------------------------------------------------------------------------------------------

WHAT THE CODE ACTUALLY DOES
    The grading core is coherent and single-sourced: `engine/signal_quality_policy.py:33` defines
    one `GRADE_BANDS` table (A+ 0.85 / A 0.70 / B+ 0.55 / B 0.40, else C) and `confidence_to_grade`
    is the only producer of a letter — grade is literally bucket(confidence), so no band is
    unreachable and there is no second competing grading engine (signal_processor's
    `_grade_rank`/`_grade_min_confidence`/`_option_grade_allowed` are thin delegations to the policy
    module; the only real duplicates, `scanner_frictionless.py` and `scanner_old.py`, are imported
    nowhere). Confidence itself is produced once, in `SignalManager._grade_signal`
    (engine/signal_manager.py:119) from four pillars (technical/intelligence/regime/manipulation),
    logistic-squashed and capped at 0.82 when ADX>40. Where it breaks down is downstream: (1) the
    two live entry gates that apply the grade read the *5-minute, instrument-keyed* regime rather
    than the candidate's own-timeframe regime, so a 1min/15min signal's grade floor and B-grade
    bypass are decided on 5-minute context — the exact defect the repo already fixed at the
    missed-reentry seam but never propagated to the main gates; (2) two exit-management sites
    re-implement the grade ladder with an ad-hoc map that neither strips the composite suffixes the
    engine actually emits ("A+ (Hist)", "B+ (EOD, Hist)", "A (…, Fut Shadow)") nor knows
    "Recovered", so those trades rank 0 and silently lose the trend-option profit-lock protections;
    (3) grade influences entry gating and RR (`dynamic_rr` grade_bonus) but has zero effect on
    position size — `position_sizing.py` never references grade or confidence and the documented
    "high_conviction (A+/power_move)" branch is fed a hardcoded False.

EVIDENCE IN CODE
    - engine/signal_quality_policy.py:33-34 — GRADE_BANDS =
    (("A+",0.85),("A",0.70),("B+",0.55),("B",0.40)); GRADE_RANKS =
    {"C":0,"B":1,"B+":2,"A":3,"A+":4,"Recovered":4} — the single source of truth;
    grade_rank()/grade_min_confidence() both do str(grade).split()[0] before lookup
    - scanner.py:268 — self.latest_regimes: Dict[str,str] = {}  # 5min instrument regime for
    dashboard/legacy gates
    - scanner.py:18554 — current_regime = str(self.latest_regimes.get(candidate.instrument,
    "UNKNOWN") or "UNKNOWN").upper() — then feeds _b_grade_entry_allowed(regime=current_regime) at
    18558 and is_choppy/min_rank at 18567-18584; same pattern at scanner.py:18642 for the live
    quality gate and 13212/18439
    - scanner.py:15083-15085 — grade = str(getattr(trade_like,"grade","") or "").upper(); if
    {"B+":3,"A":4,"A+":5}.get(grade,0) < 3: return False, "grade below B+"  (no .split()[0], no
    "Recovered"); identical map at scanner.py:11640-11645, while scanner.py:17684 correctly uses
    SignalProcessor._grade_rank
    - engine/signal_processor.py:2086 grade=f"{grade} ({display_suffix}, Fut Shadow)"; :3256
    grade=f"{hist_grade} (Hist)"; :3983 grade=f"{eod_grade} (EOD, Hist)" — composite grade strings
    are real, and scanner.py:18586 itself special-cases 'Recovered'/'EOD' substrings
    - engine/position_sizing.py:260 docstring "high_conviction (A+/power_move) never multiplies" vs
    engine/signal_processor.py:6057 high_conviction=False (the only call site); position_sizing.py
    contains no reference to grade or confidence at all

FINDINGS (4)

  [HIGH    ] bug      scanner.py:18554
    Grade-preference and live quality gates grade every candidate on the 5-minute regime, not its
    own timeframe

      `_passes_candidate_grade_preference` (scanner.py:18526) and the live entry gate
      (scanner.py:18642) both derive `current_regime`/`regime` from `self.latest_regimes`, which
      scanner.py:268 declares as the "5min instrument regime for dashboard/legacy gates" and which
      is keyed by INSTRUMENT only. That regime then decides (a) `_b_grade_entry_allowed(...)`'s
      `is_trending_regime` test at 18558, which lets a B-band signal bypass the grade floor
      entirely, and (b) `is_choppy` at 18567, which moves the auto-mode floor between min_rank 2
      (B+) and 3 (A). The per-timeframe answer already exists: `_timeframe_regimes` plus
      `_get_trade_regime`, and commit-era code at scanner.py:16982-16993 converted the
      missed-reentry seam for exactly this reason (see
      tests/test_entry_grades_on_its_own_timeframe_2026_08_07.py). The two gates that actually apply
      the grade were not converted, so the fix is only half landed.

      FAILURE
      NIFTY 1min BUY, confidence 0.52 (B band), ADX 27. Own-timeframe
      `_timeframe_regimes['NIFTY_1min']` is RANGING but `latest_regimes['NIFTY']` (5min) is
      TRENDING_UP. At scanner.py:18558 `b_grade_entry_allowed` sees a trending regime, 15m agrees
      and ADX>=25, so it returns True and the candidate bypasses the grade-preference floor
      entirely; `is_choppy` is also False so min_rank stays 2 instead of 3. A B-grade 1-minute
      signal fired inside a 1-minute ranging pocket is admitted as a live option order. The mirror
      case is equally live: a 15min candidate whose own 15min regime is TRENDING_UP is tightened to
      min_rank 3 and blocked because the 5min label happens to read VOLATILE.

  [MEDIUM  ] bug      scanner.py:15084
    Exit-side grade ladder omits 'Recovered' and does not strip grade suffixes, ranking real A/A+
    trades as 0

      scanner.py:15083-15085 and scanner.py:11640-11645 rank grades with a local
      `{"B+":3,"A":4,"A+":5}` map applied to `str(grade).upper()` — no `.split()[0]`, no "Recovered"
      key — instead of the canonical `grade_rank` (engine/signal_quality_policy.py:69), which strips
      the suffix and ranks "Recovered" at 4 (A+ equivalent). Grade strings in this system are
      routinely composite: engine/signal_processor.py:2086 emits `f"{grade} ({display_suffix}, Fut
      Shadow)"`, :3256 `f"{hist_grade} (Hist)"`, :3983 `f"{eod_grade} (EOD, Hist)"`, and
      scanner.py:18586 explicitly handles `"Recovered" in raw_grade or "EOD" in raw_grade`. Any such
      grade falls through `.get(grade, 0)` to rank 0. That scanner.py:17684 in the same file calls
      `SignalProcessor._grade_rank` correctly shows the canonical helper was available at both
      sites.

      FAILURE
      An open 5min NIFTY CE trade carries grade "A+ (Hist)" (or "Recovered") and is up Rs.4,000 from
      a Rs.5,000 peak. At scanner.py:15084 the map yields 0 < 3, so
      `_should_defer_trend_option_profit_lock` returns (False, "grade below B+") and the
      trend-aligned breathing room is refused; at scanner.py:11641 `grade_score` is likewise 0, so
      unless `runner_mode` is set the MAJOR_WIN_GUARD `option_major_win_trend_lock_ratio_cap` is
      skipped and the tighter generic lock_ratio applies. The trade is stopped out of a working
      winner that an otherwise identical trade graded plain "A+" would have been allowed to ride.

  [MEDIUM  ] partial  engine/position_sizing.py:260   [RE-VERIFIED]
    Grade never reaches position sizing; the documented A+/high_conviction branch is fed a hardcoded
    False

      The claim that the grade governs entry, size and risk holds for entry
      (`_passes_candidate_grade_preference`, `option_grade_allowed`) and for risk/reward
      (`dynamic_rr` grade_bonus, engine/signal_quality_policy.py:372-379), but not for size.
      `engine/position_sizing.py` contains no reference to `grade` or `confidence` anywhere;
      `_resolve_trade_lots_core` sizes from configured `user_lots`, the FUT hard cap, compression
      state, and cascade/heavy-trend multipliers only. Its own docstring at line 260 states
      "high_conviction (A+/power_move) never multiplies", yet the sole caller,
      engine/signal_processor.py:6057, passes `high_conviction=False` unconditionally, so that guard
      is dead code and every other caller takes the `False` default.

      FAILURE
      Two simultaneous NIFTY option candidates, one graded A+ at confidence 0.91 and one graded B+
      at confidence 0.56, both pass their gates on the same cascade-scaled scan. Both are sized to
      exactly the same lot count, and because `high_conviction` is never True the A+ setup is not
      even excluded from the cascade/market-conviction multiplier the docstring says it should be
      excluded from. Grade ranks entry and RR but contributes zero to capital allocation, so the
      highest-quality setups carry no more size than marginal ones.

  [LOW     ] risk     scanner.py:18532
    Five separate copies of the grade rank ladder exist; parity is only enforced by tests on two of
    them

      Besides the canonical `GRADE_RANKS` (engine/signal_quality_policy.py:34), the ladder is
      re-spelled inline at scanner.py:18532 `{"C":0,"B":1,"B+":2,"A":3,"A+":4}` (missing
      "Recovered", rescued only by the substring check at 18586), trading/trade_manager.py:4871, and
      the ad-hoc exit-side maps at scanner.py:11641/15084 (finding 2). Two more full stale copies
      live in scanner_frictionless.py:3312/4388 and scanner_old.py:1569 — grep finds no importer for
      either module, so they are dead today, but tests/test_live_gate_consolidation_parity.py and
      tests/test_grading_consolidation_parity.py pin only the live pair, leaving the rest free to
      drift.

      FAILURE
      A future band change — e.g. inserting an "A-" tier or re-ranking "Recovered" — is applied to
      GRADE_BANDS/GRADE_RANKS and to the two parity-tested call sites, and the suite stays green
      because no test covers scanner.py:18532, 11641 or 15084. The entry gate and the profit-lock
      deferral then rank the same trade differently from the policy module, and the disagreement
      only surfaces in live exits.


----------------------------------------------------------------------------------------------------
POINT 27  [PARTIAL]  Every intelligence engine alone and combined; track/trail horizons
----------------------------------------------------------------------------------------------------

WHAT THE CODE ACTUALLY DOES
    All ten engines exist and eight are genuinely wired: VolumeProfiler, OITracker, PCREngine,
    GreeksEngine, RegimeDetector and OrderFlowAnalyzer are instantiated in
    IntelligenceAggregator.__init__ and voted in analyze(); SessionContextAnalyzer/anchor/breadth
    enter as the top_down pillar; PriceActionAnalyzer and the calibration/memory engines are
    consumed outside the aggregator (engine/signal_processor.py:261, scanner.py:9540
    intel_memory.record). Abstention is handled correctly and deliberately for volume, OI, PCR and
    order flow — each requires a positive availability marker before adding to both numerator and
    denominator, and OrderFlowAnalyzer._empty_result returns ratio 1.0 so the +/-0.30 divergence
    stress test cannot fire on an abstaining read. The combination math is where it breaks: the
    published score is score/max_possible over only the pillars that voted, so a single available
    pillar produces full 100-point conviction, and no consumer anywhere checks components_analyzed.
    The multi-horizon tracking is real for day/hour/minute but there is no tick horizon — ticks are
    consumed only to build 1-minute candles, and every intelligence and trailing horizon bottoms out
    at the 1-minute bar.

EVIDENCE IN CODE
    - intelligence/intelligence_aggregator.py:567 — `averaged_score = (score / max_possible) if
    max_possible > 0 else 0.0`; max_possible accumulates ONLY for pillars that voted (0.35 volume,
    0.1 oi, 0.2 pcr, 0.2 oi_wall, 0.25 regime, 0.2 order_flow, 0.3 divergence, 0.35 top_down), so
    one pillar voting at full strength normalises to 1.0.
    - intelligence/intelligence_aggregator.py:415-416 — regime is the only pillar that does
    `components += 1` / `max_possible += 0.25` unconditionally; combined with the 09:15-09:19
    order-flow abstain described in the comment at lines 431-434 and a non-authoritative chain (line
    190), regime can be the sole denominator entry.
    - intelligence/intelligence_aggregator.py:219-239 — market_structure (+/-0.04) and aroon
    (+/-0.03) are added to `score` unconditionally, but `max_possible += 0.35` is inside `if
    volume_available:`; numerator terms with no denominator budget.
    - intelligence/intelligence_aggregator.py:340-376 — `dist_res`/`dist_sup` initialise to 999.0
    and only move `score` when a wall is within `dynamic_threshold`, yet `components += 1` /
    `max_possible += 0.2  # oi_wall` run unconditionally whenever a chain and spot exist.
    - scanner.py:9210-9253 `_live_regime_horizons` — publishes pulse (1min), hour (5min x36), day
    (15m+1h hybrid), m15. `pulse["title"] = "1–5m"`. No tick horizon;
    intelligence/session_context.py:365-369 `_mood_horizons` specs are exactly {minute, quarter,
    hour} counted in bars of the active stream.
    - main.py:853 — `data_provider.on_tick = candle_builder.update_from_tick` is the only tick
    consumer; it aggregates into 1-min candles (data/candle_builder.py:94-102). No engine or
    trailing path subscribes to ticks.

FINDINGS (4)

  [CRITICAL] bug      intelligence/intelligence_aggregator.py:567   [RE-VERIFIED]
    A single available pillar saturates the aggregate to 100/STRONG_BUY; no minimum-component gate

      `averaged_score = score / max_possible` divides by the sum of maximum votes of only the
      pillars that actually voted. Regime is the one pillar that increments `max_possible`
      unconditionally (line 415-416, +0.25). Volume needs `volume_available` (237), OI/PCR need
      `chain_authoritative` (242, 190), order flow needs `flow_available` (450). When the option
      chain is not authoritative and order flow abstains — the exact 09:15-09:19 condition the
      comment at lines 431-434 documents — regime alone owns the denominator: a TRENDING_UP read
      contributes score=+0.25 over max_possible=0.25, giving averaged_score=1.0, final_score=100.0
      and final_signal STRONG_BUY (lines 571-580). Grep across scanner.py and engine/ shows
      `components_analyzed` is only ever written (line 595) and never read as a gate, so nothing
      downstream can tell a one-engine verdict from a seven-engine consensus. The
      weight-normalisation rewrite documented at lines 143-157 fixed the old unreachable-ceiling
      problem by making the denominator adaptive, but did not add a floor.

      FAILURE
      09:16 IST, option chain still fallback/stale so `intelligence_chain` is None, 1-min frame has
      <5 bars so OrderFlowAnalyzer returns available=False, index has no traded volume so
      `volume_available` is False. RegimeDetector returns TRENDING_UP. max_possible=0.25, score=0.25
      (+0.07 if aroon/structure agree), averaged_score clamps to 1.0. The aggregate publishes
      score=100.0 / STRONG_BUY on the least-informed read of the session, clearing every
      `min_intel_score` gate in scanner.py (0.20 missed-signal reentry at :16803, 0.20 choppy scalp
      at :20403, even the 0.70 rare-burst gate at :20475) on the strength of one engine.

  [HIGH    ] partial  scanner.py:9210
    No tick horizon exists; finest intelligence/trailing horizon is the 1-minute bar

      The claim is track/trail at day/hour/minute/tick. `_live_regime_horizons` publishes exactly
      four pockets — pulse (`_detect_tf_regime_snapshot(name, "1min")`, titled "1–5m"), hour (5min
      x36 bars), day (15m+1h hybrid) and m15. `SessionContextAnalyzer._mood_horizons`
      (intelligence/session_context.py:357-381) defines three specs — minute, quarter, hour — each
      expressed as a count of bars of the active stream via `bars_for(minutes) =
      round(minutes/bar_minutes)`; on the 5-minute stream `specs["minute"]` evaluates to 1 bar, i.e.
      5 minutes of tape carrying the label "minute". Ticks do arrive over a real KiteTicker socket
      (data/market_data.py:1328-1427) but the single subscriber is `candle_builder.update_from_tick`
      (main.py:853), which aggregates them into 1-min candles; `update_latest_price`
      (data/candle_builder.py:67) only stamps a display price. No intelligence engine, no aggregator
      pillar and no stop-trailing path reads a tick stream. `intel_df` in the aggregator is the
      1-minute frame (line 198).

      FAILURE
      A 30-second spike-and-reverse inside one 1-minute candle is invisible to every horizon. The
      'tick' pocket a reader expects to catch it does not exist, and the 'pulse'/'minute' pockets
      only re-evaluate on the 1-minute (or, on the 5-minute stream, the 5-minute) bar boundary — so
      a signal is tracked and trailed at minute resolution while being reported as tick-resolution.

  [MEDIUM  ] bug      intelligence/intelligence_aggregator.py:219   [RE-VERIFIED]
    market_structure/aroon add to the score numerator but never to max_possible

      Lines 219-234 add up to +/-0.04 (market_structure ACCEPTING/REJECTING) and +/-0.03 (aroon
      trend) to `score` unconditionally. The matching `max_possible += 0.35` at line 239 is guarded
      by `if volume_available:`. The comment at 235-236 justifies the numerator side
      ('Price-structure/Aroon remain valid without traded volume') but leaves the denominator
      unbudgeted, which is the exact defect the lines 143-157 rewrite set out to remove. The same
      shape appears in reverse at lines 340-376: the oi_wall pillar takes `max_possible += 0.2`
      unconditionally while `dist_res`/`dist_sup` sit at their 999.0 initialisers whenever no wall
      is within `dynamic_threshold`, so a wall that has nothing to say still dilutes every other
      pillar by its full weight.

      FAILURE
      Volume pillar abstains and only regime counts: max_possible=0.25. A +0.03 aroon nudge is
      divided by 0.25 and becomes +12 score points — the same nudge with all eight pillars counted
      (max_possible=1.85) is worth +1.6, a 7.5x swing driven purely by option-chain and volume
      availability. In the same run score=0.32 exceeds max_possible=0.25, so `averaged_score`=1.28
      and the clamp at line 571 is silently absorbing a mis-normalised value rather than guarding an
      edge case.

  [MEDIUM  ] bug      scanner.py:25845
    Live OPT positions never trail on the live-trade-preservation path; new_stop is discarded

      In the position-monitor loop, the `getattr(trade, 'is_live', False) and self.mode ==
      "HISTORICAL"` branch computes `new_stop` for the trade but applies it only to futures:

                      if trade.inst_type == "OPT":
                          pass
                      else:
                          trade.current_stop = new_stop

      The exit alert eleven lines earlier (25815) tests `trade.current_price <= trade.current_stop`,
      i.e. against the stale stop that was just declined an update, while the futures branch at
      25824/25831 correctly tests against the fresh `new_stop`. The result is that an option leg on
      this path is frozen at whatever stop it carried when the mode flipped, and the operator alert
      is generated from that stale level.

      FAILURE
      Operator holds a live option position and switches the scanner to HISTORICAL to run a
      backtest. The option runs +40% in favour; `new_stop` ratchets up on every monitor tick but is
      thrown away, so `trade.current_stop` stays at the entry stop. Price then round-trips back
      through the entry stop before any TRAILING_STOP alert fires, and the dashboard shows the
      original stop the whole time — the entire locked-in gain is given back with no warning, while
      an identical futures position would have alerted.


----------------------------------------------------------------------------------------------------
POINT 28  [PARTIAL]  Regime detection across day / hour / minute and its effect on live trades
----------------------------------------------------------------------------------------------------

WHAT THE CODE ACTUALLY DOES
    Regime detection is real and multi-horizon, not a stub. `RegimeDetector.detect()`
    (intelligence/regime_detector.py) computes ADX/ATR%/BB-width/RSI/MACD, applies a Kaufman-ER
    early-trend promotion, a confirmed-breakout override and a range-day demotion guard, then
    appends ONE label per bar (bar-id de-duplicated, keyed `"{instrument}_{timeframe}"`) and exposes
    a majority vote over the last `regime_smoothing_window` (default 5) bars — that vote plus
    `_recent_trend_regime` strong-ADX hysteresis is the entire debounce mechanism. A genuine hourly
    regime exists for trading (`Scanner._hourly_anchor_regime`, a true `resample("60min")` over
    5-min candles, gating `rare_burst_require_hourly_anchor`), and live trades DO re-read the
    CURRENT regime: `_get_trade_regime` resolves the trade's own timeframe first, and that value
    drives `uses_tight_trailing` in the trailing loop and the LIVE_REVERSAL_EXIT profit-taker, so a
    mid-trade flip really does change the trail and can force an exit. The defects are at the edges:
    the scale-in "still with the trend" gate reads the FROZEN entry regime instead of the live one;
    the detector's smoothing history is never reset at the session rollover, so it votes across the
    overnight gap; the tie-break disables smoothing in exactly the chop it exists to damp; and the
    dashboard's "Hourly" horizon is a rolling 3-hour window, not an hour.

EVIDENCE IN CODE
    - intelligence/regime_detector.py:138,271-289 — history key is `f"{instrument}_{timeframe}"`;
    one entry PER BAR (bar_id = `str(df.index[-1])`, same bar revises `history[-1]` instead of
    adding a vote), trimmed to 50. Debounce = `_smooth_regime` majority vote over
    `regime_smoothing_window` (default 5) plus `_recent_trend_regime` hysteresis (needs >=3 of last
    5).
    - scanner.py:8745 — the TRADING hourly anchor is a genuine resample, not a rolling window:
    `hourly = work.resample("60min", origin="start_day", label="right", closed="right").agg(agg)`,
    requiring >=30 hourly bars before it grades.
    - scanner.py:6954-6968 `_get_trade_regime` — a live trade resolves `_timeframe_regimes[inst_tf]
    or latest_regimes[inst_tf] or ... 5min fallback`, i.e. the CURRENT regime, and
    `_trade_regime_is_own_timeframe` (scanner.py:6935-6952) blocks a borrowed 5-min label from
    granting runner privilege to a 15-min trade.
    - scanner.py:19282 / 25264 `uses_tight_trailing(regime)` and scanner.py:12742-12765
    LIVE_REVERSAL_EXIT (`live_regime = regime or live_regime`; `self._regime_opposes_direction(view,
    live_regime)`) — a mid-trade regime flip genuinely tightens the trail and can fire an exit on a
    fading winner.
    - trading/trade_manager.py:148 — `market_regime: str = ""   # Regime at entry (for post-hoc
    attribution)`; its only writer is trade creation (trade_manager.py:3610), never refreshed after
    entry.
    - scanner.py:853-903 session rollover clears `_prev_close_cache`, `_vix_session_open_cache`,
    `_breadth_asof_cache` etc. but never `latest_regimes`, `_timeframe_regimes`, or
    `intel.regime.reset_history()`; the only live `reset_history()` is in
    `_reset_simulation_diagnostics` (engine/scanner_telemetry.py:79-84) and the per-instrument one
    at scanner.py:9598-9604 is gated on `self.mode == "HISTORICAL"`.

FINDINGS (5)

  [HIGH    ] bug      trading/trade_manager.py:1939
    Scale-in "still with the trend" gate reads the regime frozen at ENTRY, not the current regime

      `_scale_in_is_earned` docstring promises "Only add to a trade that is already working, and
      still with the trend", but the trend test is `regime = str(getattr(trade, "market_regime", "")
      or "").upper()`. `Trade.market_regime` is documented at trade_manager.py:148 as "Regime at
      entry (for post-hoc attribution)" and is written exactly once at construction
      (trade_manager.py:3610); nothing ever refreshes it. The scanner-side live label
      (`Scanner._get_trade_regime`, scanner.py:6954) is never consulted here, and
      `scale_in_requires_trend` appears nowhere else in the codebase, so there is no second
      live-regime check upstream. A scale-in is a NEW risk decision taken minutes-to-hours after
      entry and must be judged on the regime at the time of the add.

      FAILURE
      NIFTY 15-min LONG entered at 10:05 while the regime is TRENDING_UP;
      `market_regime="TRENDING_UP"` is frozen on the trade. By 13:20 the live 15-min regime has
      flipped to TRENDING_DOWN (the trailing loop has already switched to `uses_tight_trailing` and
      the LIVE_REVERSAL_EXIT arm is watching). The position is still +0.9R from the morning push, so
      `scale_in_min_profit_r` passes, and the trend gate reads the frozen "TRENDING_UP":
      bullish=True, wants_up=True, so it returns (True, "") and `_request_broker_scale_in` adds lots
      into a confirmed downtrend. The identical inverse case adds to a SHORT after the regime turned
      TRENDING_UP.

  [HIGH    ] bug      scanner.py:889
    Regime smoothing history is never reset at session rollover; the vote spans the overnight gap

      `_regime_history` is a per-process dict on the long-lived `self.intel.regime` detector. The
      session rollover (scanner.py:853-903) explicitly purges overnight-contaminated session caches
      (`_prev_close_cache`, `_vix_session_open_cache`, `_breadth_asof_cache`) with the comment "a
      process running overnight must not serve yesterday's ... into today's gap analysis", yet it
      never calls `intel.regime.reset_history()` and never clears
      `latest_regimes`/`_timeframe_regimes`. The only live-mode `reset_history()` call sits in
      `_reset_simulation_diagnostics` (engine/scanner_telemetry.py:79-84), and the per-instrument
      reset at scanner.py:9598-9604 is guarded by `if self.mode == "HISTORICAL"`. Candle caches are
      multi-day, so `detect()` clears its 30-bar minimum immediately at the open and starts
      appending today's bars onto yesterday's tail.

      FAILURE
      Process stays up from Monday into Tuesday. Monday's last four 5-min bars for NIFTY are
      TRENDING_UP. Tuesday gaps down 1.2% and the first bar detects TRENDING_DOWN. `_smooth_regime`
      with window=5 sees ['TRENDING_UP' x4, 'TRENDING_DOWN'] and returns TRENDING_UP;
      `_recent_trend_regime` also reports TRENDING_UP (ups=4>=3). For the first ~20 minutes (a full
      hour on the 15-min key) every consumer of that label is wrong at once: `regime_aligns` admits
      fresh BUY signals into a gap-down, `uses_tight_trailing` returns False so live SHORT trades
      keep the loose trend trail, and `_regime_opposes_direction` reports no opposition so
      LIVE_REVERSAL_EXIT withholds the profit-take.

  [MEDIUM  ] bug      intelligence/regime_detector.py:775
    _smooth_regime tie-break turns the debounce off in exactly the chop it exists to damp

      After `counts = Counter(recent)`, `top = counts.most_common(1)[0][1]`, the code collects every
      label tied at `top` and returns the most RECENT of them. When the window holds N distinct
      labels every count ties at 1, so `tied` is the whole window and the function returns
      `recent[-1]` — the raw single-bar label, i.e. no smoothing at all. Even a 2-2-1 split lets a
      40% plurality decide by recency. The regime space (TRENDING_UP/DOWN, BREAKOUT_UP/DOWN,
      RANGING, VOLATILE) is larger than the default window of 5, so the fully-degenerate case is
      reachable. This is the one mechanism standing between a flapping bar-to-bar label and the
      trailing branch, the reversal exit and entry alignment; it disengages precisely when the tape
      is oscillating.

      FAILURE
      BANKNIFTY on an expiry-afternoon chop produces the 5-bar sequence RANGING, VOLATILE,
      TRENDING_UP, BREAKOUT_UP, TRENDING_DOWN. All five counts are 1, so the smoothed output is
      TRENDING_DOWN — the raw last bar, unfiltered. The next bar prints BREAKOUT_UP and the smoothed
      output flips again. A live LONG alternates between the loose trend trail
      (`uses_tight_trailing` False on TRENDING_UP/BREAKOUT_UP) and the tight 1-min-barrier trail
      (True on VOLATILE/RANGING) every five minutes, ratcheting the stop up on the tight bars and
      stranding it there, while `_regime_opposes_direction` can fire LIVE_REVERSAL_EXIT on the
      single TRENDING_DOWN print.

  [MEDIUM  ] bug      scanner.py:9234
    Dashboard "Hourly" horizon is a rolling 3-hour window, not an hour, and ignores the 60min anchor

      `_live_regime_horizons` builds `hour = self._detect_tf_regime_snapshot(name, "5min",
      lookback_bars=36, force_redetect=True)` and labels it `hour["title"] = "Hourly (5m pocket)"`;
      the `day` horizon is then derived from that same pocket via `_hybrid_session_day_regime(hour,
      m15)` and titled "Day (15m + 1h hybrid)". 36 five-minute bars is 180 minutes, and the
      function's own docstring admits the range is "last ~12-36 5min bars". The genuine hourly
      regime — `_hourly_anchor_regime` (scanner.py:8745), a true `resample("60min")` requiring 30
      hourly bars, which is what actually gates trades through `rare_burst_require_hourly_anchor`
      (scanner.py:20516) — is never called by `_live_regime_horizons`. So the panel labelled
      "Hourly" is a different statistic on a different timeframe from the hourly anchor the engine
      enforces, and the "Day" row inherits the mislabel.

      FAILURE
      A trend that started 40 minutes ago has fully turned the true 60-min anchor bar to
      TRENDING_UP, so `rare_burst_require_hourly_anchor` admits a burst entry. The panel's "Hourly"
      pocket still averages the previous two flat hours inside its 180-minute lookback and shows
      RANGING. The operator reading the Regime panel sees an hourly regime that contradicts the gate
      that just fired, and any manual intervention is made against a horizon that no live decision
      uses.

  [LOW     ] bug      scanner.py:108
    self._hourly_regime_detector is constructed at Scanner init and never used anywhere in the repo

      `self._hourly_regime_detector = RegimeDetector()` is the only occurrence of that attribute in
      the entire non-test tree (verified by a repo-wide grep). The hourly path instead uses
      `_detect_regime_warmed`, which builds a throwaway `RegimeDetector()` per call and replays bars
      to warm its smoothing. So the instance is dead state that reads as if the hourly regime
      carried its own persistent smoothing history when it does not.

      FAILURE
      A maintainer adding hourly smoothing wires it into `self._hourly_regime_detector` (the obvious
      home) and sees no behavioural change, because `_hourly_anchor_regime` ->
      `_detect_regime_warmed` is a @staticmethod that constructs its own detector and discards it;
      the hourly anchor's smoothing state is rebuilt from scratch on every call and no persistent
      hourly hysteresis exists despite the attribute implying one.

====================================================================================================
PART 5 -- CROSS-CUTTING THEMES
====================================================================================================

  * Config, not code, is where requirements die
      Reqs 3, 5, 8, 11 and 12 are implemented and tested but inert as shipped. Any audit that reads
      only source would pass them. Check .env before believing a feature is live.

  * One price series wearing another's clothes
      The two most severe defects in this report are both "value X stored or read where value Y was
      meant": the option premium written into entry_spot (C2), and the futures price used for an
      index-spot decision (fixed in your last commits). Point 22 found a third instance still live
      at scanner.py:22122, where a mixed-book FUT conversion prices the futures leg at index spot
      when the futures LTP fetch fails. This is a recurring class, not three unrelated bugs.

  * Missing data read as a decision
      C1 (empty cache -> repaint -> market exit) and C3 (one pillar -> STRONG_BUY) are the same
      mistake in two engines: absence of evidence being scored as evidence. Both need an explicit
      "not enough data" state that is distinct from a verdict.

  * The 5-minute regime is the default everywhere
      Points 8, 26 and 28 independently found the same thing: latest_regimes is keyed by INSTRUMENT
      only and holds the 5min regime, yet it feeds gates that are deciding on 1min and 15min
      candidates. The per-timeframe store (_timeframe_regimes) exists and the missed-reentry seam
      was already converted to use it -- the main entry gates were not. This fix is half-landed.

  * Escalation ladders that only label
      Point 5: the rupee loss slabs compute a "rung" that names a log band and drives nothing
      (scanner.py:14852; grep confirms no other consumer). Escalation does exist, via a separate
      _loss_guard_severity path -- but it is not keyed to the 0/-500/-1000/-1500/-2000/-3000 slabs
      you specified.

  * Retired broker, retired protections
      Requirement 23 is the one genuine success -- smart_api = None is the only assignment in the
      repo, so no live AngelOne path exists. But the rate-limit stack (SharedApiRateLimiter,
      AngelHistoricalRateLimiter, AngelClientQuotaRegistry) is still wired only to that dead code,
      and the Kite replacement _call() has no pacing, no quota and no backoff. The broker was
      removed; its safety layer was left attached to the corpse.

====================================================================================================
PART 6 -- METHOD, AND WHAT THIS DOES NOT COVER
====================================================================================================

  - One agent per requirement, three at a time in a rolling pool -- as each finished, the next
  pending point took its slot. 28 points, 118 findings.

  - The session token limit was hit twice mid-sweep. The 19 and then 6 interrupted points were
  re-run in waves (10-22, then 23-25, then 26-28), so all 28 are complete rather than partially
  reported.

  - Baseline suite was run first, unmodified: 1 failed, 2584 passed, 12 skipped, 4 deselected in
  99.86s.

  - Findings marked [RE-VERIFIED] I confirmed myself. That is 21 of 118. The remainder carry a
  file:line and a failure mode but were not independently checked -- treat them as leads, not
  verdicts.

  - I revised one agent severity DOWN after measuring it (scanner.py:9643). Agent severity labels
  are judgement calls and were not otherwise re-scored.

  - This is static analysis plus the existing test suite. Nothing ran against a live market session,
  no orders were placed, and no file in the repo was modified.

  - Every database under data/ and data_store/ currently has zero tables, so all retention and
  persistence findings are argued from code, not from stored rows.

  - Points 1-9 were audited before the ground-truth notes (failing test, .env values, sqlite VACUUM)
  were available to later agents, so those nine had slightly less context than points 10-28.

====================================================================================================
end of report
====================================================================================================