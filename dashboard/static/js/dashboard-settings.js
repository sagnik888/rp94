/* Settings presentation, synchronization, and commands. */

const SCANNER_ENGINE_LABELS = {
    utbot: 'UT1',
    ema: 'EMA 9/21',
    consensus: 'Consensus ST+EMA',
    utbot_consensus: 'UT1 + Consensus',
    quad_union: 'Quad Union',
    quad_prime: 'Quad Prime',
    quad_prime_s1: 'Quad Prime + S1',
    ut_smart_s1: 'UT Smart Exit + S1',
    adaptive_dual: 'Adaptive Dual',
    s1: 'S1 Confluence',
    ut_smart: 'UT Smart Exit',
    s1s2_dual: 'S1+S2 Dual (Pine v5.4)',
    ut_ema: 'ut/ema',
};

const SCANNER_ENGINE_OPTIONS = [
    ['utbot', 'UT1'],
    ['ema', 'EMA 9/21'],
    ['consensus', 'Consensus ST+EMA'],
    ['utbot_consensus', 'UT1 + Consensus'],
    ['quad_union', 'Quad Union'],
    ['quad_prime', 'Quad Prime'],
    ['quad_prime_s1', 'Quad Prime + S1'],
    ['ut_smart_s1', 'UT Smart Exit + S1'],
    ['ut_smart', 'UT Smart Exit'],
    ['s1', 'S1 Confluence'],
    ['s1s2_dual', 'S1+S2 Dual (Pine v5.4)'],
    ['ut_ema', 'ut/ema'],
];

let S12_SETTINGS_FIELDS = [];

function refreshS12SettingsCard(config = state.latestData?.config || {}) {
    const schema = config?.s12_settings_schema;
    const grid = document.querySelector('#settings-s12-engine .settings-grid');
    if (!schema || !grid) return;
    const fields = Object.entries(schema).map(([key, meta]) => {
        const label = key.slice(4).replaceAll('_', ' ').replace(/\b\w/g, c => c.toUpperCase());
        const id = `sett${key.slice(4).split('_').map(part => part[0].toUpperCase() + part.slice(1)).join('')}`;
        const kind = String(meta?.kind || 'float');
        const fallback = meta?.default ?? config[key];
        const extra = kind === 'enum'
            ? (meta.choices || []).map(value => [value, String(value)])
            : ['int', 'float'].includes(kind)
                ? { min: meta.min, max: meta.max, step: meta.step || (kind === 'int' ? '1' : '0.01') }
                : undefined;
        return [key, id, label, kind, fallback, extra];
    });
    if (fields.length === S12_SETTINGS_FIELDS.length && fields.every((field, i) => field[0] === S12_SETTINGS_FIELDS[i]?.[0])) return;
    S12_SETTINGS_FIELDS = fields;
    grid.replaceChildren();
    for (const [, id, label, kind, , extra] of fields) {
        let control;
        if (kind === 'bool') {
            control = document.createElement('label');
            control.className = 'settings-switch-card';
            const title = document.createElement('span');
            const strong = document.createElement('strong');
            strong.textContent = label;
            title.appendChild(strong);
            const input = document.createElement('input');
            input.type = 'checkbox';
            input.id = id;
            control.append(title, input);
        } else {
            control = document.createElement('div');
            control.className = 'setting-group';
            const title = document.createElement('label');
            title.textContent = label;
            const input = document.createElement(kind === 'enum' ? 'select' : 'input');
            input.id = id;
            if (kind === 'time') input.type = 'time';
            else if (kind !== 'enum') {
                input.type = 'number';
                if (extra?.min !== undefined) input.min = String(extra.min);
                if (extra?.max !== undefined) input.max = String(extra.max);
                if (extra?.step !== undefined) input.step = String(extra.step);
            }
            if (kind === 'enum') for (const [value, text] of extra || []) {
                const option = document.createElement('option');
                option.value = value;
                option.textContent = text;
                input.appendChild(option);
            }
            control.append(title, input);
        }
        grid.appendChild(control);
    }
}

// S1 engine / lane settings. Mirrors engine/s1_lane.py S1_SETTING_SPEC (a test keeps the
// two key sets equal). [key, element id, label, kind, fallback, attrs-or-choices]
const S1_SETTINGS_FIELDS = [
    ['s1_candle_type', 'settS1CandleType', 'S1 Candle Source', 'enum', 'heikin_ashi', [['heikin_ashi', 'Heikin Ashi (manual reference)'], ['standard', 'Standard OHLC']]],
    ['s1_min_score', 'settS1MinScore', 'Min Confluence Score', 'float', 60, { step: '1', min: '0', max: '100' }],
    ['s1_conflict_margin', 'settS1ConflictMargin', 'Min Bull-Bear Gap', 'float', 20, { step: '1', min: '0', max: '100' }],
    ['s1_min_adx', 'settS1MinAdx', 'Min ADX', 'float', 20, { step: '0.5', min: '0', max: '60' }],
    ['s1_use_htf', 'settS1UseHtf', '15m EMA Alignment Gate', 'bool', true],
    ['s1_signal_cooldown', 'settS1Cooldown', 'Min Bars Between Signals', 'int', 0, { step: '1', min: '0', max: '50' }],
    ['s1_chart_sl1_pct', 'settS1ChartSl1', 'Chart SL1 % (your TradingView input)', 'float', 0.20, { step: '0.05', min: '0.01', max: '10' }],
    ['s1_chart_sl2_pct', 'settS1ChartSl2', 'Chart SL2 % (your TradingView input)', 'float', 0.40, { step: '0.05', min: '0.05', max: '15' }],
    ['s1_session_end', 'settS1SessionEnd', 'Chart Session Ends (drop bars from)', 'time', '15:15'],
    ['s1_sl_mode', 'settS1SlMode', 'SL1 Mode', 'enum', 'fixed', [['anchor', 'Dynamic: 3-bar extreme'], ['atr', 'Dynamic: k x ATR'], ['fixed', 'Fixed %']]],
    ['s1_sl_min_pct', 'settS1SlMinPct', 'Dynamic SL1 Min % of LTP', 'float', 0.10, { step: '0.01', min: '0.01', max: '2' }],
    ['s1_sl_max_pct', 'settS1SlMaxPct', 'Dynamic SL1 Max % of LTP', 'float', 0.25, { step: '0.01', min: '0.01', max: '2' }],
    ['s1_sl1_pct', 'settS1Sl1Pct', 'Fixed SL1 % of LTP', 'float', 0.10, { step: '0.01', min: '0.01', max: '2' }],
    ['s1_sl_atr_mult', 'settS1SlAtrMult', 'ATR SL Multiple', 'float', 1.0, { step: '0.1', min: '0.1', max: '5' }],
    ['s1_use_sl2', 'settS1UseSl2', 'SL2 Gap Stop', 'bool', false],
    ['s1_sl2_pct', 'settS1Sl2Pct', 'SL2 % of LTP', 'float', 0.40, { step: '0.05', min: '0.05', max: '5' }],
    ['s1_t1_r', 'settS1T1R', 'T1 (R)', 'float', 2, { step: '0.5', min: '0.5', max: '10' }],
    ['s1_t2_r', 'settS1T2R', 'T2 (R)', 'float', 3, { step: '0.5', min: '0.5', max: '10' }],
    ['s1_t3_r', 'settS1T3R', 'T3 (R)', 'float', 4, { step: '0.5', min: '0.5', max: '20' }],
    ['s1_trail_start_r', 'settS1TrailStartR', 'Trail Starts At (R)', 'float', 1.5, { step: '0.25', min: '0.25', max: '10' }],
    ['s1_trail_mult', 'settS1TrailMult', 'Trail ATR Offset', 'float', 1.8, { step: '0.1', min: '0.1', max: '10' }],
    ['s1_exit_conf_bars', 'settS1ExitConfBars', 'Momentum Exit Confirm Bars', 'int', 3, { step: '1', min: '1', max: '10' }],
    ['s1_circuit_pause', 'settS1CircuitPause', 'Circuit Breaker Pause Bars', 'int', 5, { step: '1', min: '0', max: '100' }],
    ['s1_max_consec_loss_nifty', 'settS1ConsecNifty', 'Loss Streak Stop: NIFTY (0 = system setting)', 'int', 0, { step: '1', min: '0', max: '50' }],
    ['s1_max_consec_loss_banknifty', 'settS1ConsecBankNifty', 'Loss Streak Stop: BANKNIFTY (0 = system setting)', 'int', 0, { step: '1', min: '0', max: '50' }],
    ['s1_max_consec_loss_sensex', 'settS1ConsecSensex', 'Loss Streak Stop: SENSEX (0 = system setting)', 'int', 0, { step: '1', min: '0', max: '50' }],
    ['s1_max_consec_loss_midcpnifty', 'settS1ConsecMidcp', 'Loss Streak Stop: MIDCPNIFTY (0 = system setting)', 'int', 0, { step: '1', min: '0', max: '50' }],
    ['s1_entry_mode', 'settS1EntryMode', 'Signal State Machine', 'enum', 'pine', [['pine', 'Pine indicator state rules'], ['close', 'Close-entry replay']]],
    ['s1_lane_enabled', 'settS1LaneEnabled', 'S1 Own Lane (skip other gates/guards)', 'bool', true],
    ['s1_lane_guard_pct_1min', 'settS1Guard1m', 'Premium Guard % 1m', 'float', 5, { step: '1', min: '1', max: '50' }],
    ['s1_lane_guard_pct_5min', 'settS1Guard5m', 'Premium Guard % 5m', 'float', 10, { step: '1', min: '1', max: '50' }],
    ['s1_lane_guard_pct_15min', 'settS1Guard15m', 'Premium Guard % 15m', 'float', 15, { step: '1', min: '1', max: '50' }],
    ['s1_lane_guard_late_after', 'settS1GuardLateAfter', 'Guard One Step Tighter After', 'time', '14:30'],
    ['s1_lane_guard_strong_score', 'settS1GuardStrongScore', 'Guard One Step Wider At Score', 'float', 80, { step: '1', min: '0', max: '100' }],
    ['s1_lane_t1_lock_r', 'settS1T1LockR', 'Index Stop Lock At T1 (R)', 'float', 0.7, { step: '0.1', min: '0', max: '5' }],
    ['s1_lane_lock_arm_r', 'settS1LockArmR', 'Profit Lock Arms At (R)', 'float', 1.0, { step: '0.25', min: '0.1', max: '10' }],
    ['s1_lane_rr_floor_at_target', 'settS1RrFloorAtTarget', 'Entry RR Floor = Target (no sub-target entries)', 'bool', true],
    ['s1_lane_signal_only', 'settS1SignalOnly', 'S1 Signals Only (no Auto orders)', 'bool', true],
    ['s1_lane_breakeven_enabled', 'settS1BreakevenEnabled', 'Breakeven Stop After Small Gain', 'bool', true],
    ['s1_lane_breakeven_arm_r', 'settS1BreakevenArmR', 'Breakeven Arms At (R)', 'float', 1.0, { step: '0.05', min: '0.05', max: '5' }],
    ['s1_lane_breakeven_cost_rs', 'settS1BreakevenCost', 'Breakeven Covers Round-Trip Cost (Rs)', 'float', 140, { step: '10', min: '0', max: '2000' }],
    ['s1_sl1_pct_5min', 'settS1Sl1Pct5m', '5m Fixed Index Stop % (0 = use SL mode)', 'float', 0.1, { step: '0.01', min: '0', max: '2' }],
    ['s1_sl1_pct_15min', 'settS1Sl1Pct15m', '15m Fixed Index Stop % (0 = use SL mode)', 'float', 0.15, { step: '0.01', min: '0', max: '2' }],
    ['s1_lane_giveback_with_trend', 'settS1GivebackWith', 'Giveback % (trend with trade)', 'float', 25, { step: '1', min: '0', max: '95' }],
    ['s1_lane_giveback_neutral', 'settS1GivebackNeutral', 'Giveback % (no clear trend)', 'float', 10, { step: '1', min: '0', max: '95' }],
    ['s1_lane_giveback_unknown', 'settS1GivebackUnknown', 'Giveback % (trend unknown: no closed bar yet / data missing)', 'float', 25, { step: '1', min: '0', max: '95' }],
    ['s1_lane_giveback_against', 'settS1GivebackAgainst', 'Giveback % (trend against)', 'float', 0, { step: '1', min: '0', max: '95' }],
    ['s1_lane_trend_margin', 'settS1TrendMargin', 'Clear Trend = Score Gap >=', 'float', 20, { step: '1', min: '1', max: '100' }],
    ['s1_lane_intrabar_wait_seconds', 'settS1IntrabarWait', 'Intrabar Signal Buffer Sec', 'float', 15, { step: '1', min: '0', max: '120' }],
    ['s1_lane_intrabar_wait_seconds_1min', 'settS1IntrabarWait1m', 'Intrabar Signal Buffer Sec (1min)', 'float', 5, { step: '1', min: '0', max: '60' }],
    ['s1_lane_forming_recompute_seconds', 'settS1FormingRecompute', 'Forming-Bar Recompute Every (sec, 0 = every scan)', 'float', 15, { step: '1', min: '0', max: '120' }],
    ['s1_lane_base_rr', 'settS1BaseRr', 'Target: Normal Trade (R)', 'float', 1.0, { step: '0.1', min: '0.3', max: '10' }],
    ['s1_lane_weak_rr', 'settS1WeakRr', 'Target: Weak Move (R)', 'float', 0.7, { step: '0.1', min: '0.3', max: '10' }],
    ['s1_lane_runner_max_r', 'settS1RunnerMaxR', 'Runner Max In Special Trend (R, else T3)', 'float', 30, { step: '1', min: '1', max: '100' }],
    ['s1_lane_special_trend_margin', 'settS1SpecialTrend', 'Special Trend = Score Gap >=', 'float', 40, { step: '1', min: '1', max: '100' }],
    ['s1_lane_weak_adx', 'settS1WeakAdx', 'Weak Move: ADX Below', 'float', 20, { step: '1', min: '0', max: '100' }],
    ['s1_lane_weak_rel_volume', 'settS1WeakVol', 'Weak Move: Volume Below x Avg', 'float', 1.2, { step: '0.1', min: '0', max: '10' }],
    ['s1_lane_min_entry_rr', 'settS1MinEntryRr', 'Final Entry Check Min RR (S1)', 'float', 0.5, { step: '0.1', min: '0.1', max: '5' }],
    ['s1_lane_reentry_enabled', 'settS1Reentry', 'Missed-Signal Re-entry (same or stronger score)', 'bool', true],
    ['s1_lane_reentry_bars_1min', 'settS1ReentryBars1', 'Re-entry Window 1m (bars after close)', 'int', 3, { step: '1', min: '1', max: '20' }],
    ['s1_lane_reentry_bars_5min', 'settS1ReentryBars5', 'Re-entry Window 5m (bars after close)', 'int', 1, { step: '1', min: '1', max: '20' }],
    ['s1_lane_reentry_bars_15min', 'settS1ReentryBars15', 'Re-entry Window 15m (bars after close)', 'int', 1, { step: '1', min: '1', max: '20' }],
];

// Morning HISTORICAL -> REAL switch (editable any time; the worker re-reads it). It
// changes the trading mode only: the Auto Mode toggle stays exactly as the operator left it.
// UT SMART EXIT engine / lane settings. Mirrors engine/uts_lane.py UTS_SETTING_SPEC
// (a test keeps the two key sets equal). Defaults match the uploaded Pine script.
const UTS_SETTINGS_FIELDS = [
    ['uts_key_value', 'settUtsKeyValue', 'UT Sensitivity / Key Value', 'float', 1.0, { step: '0.1', min: '0.1', max: '10' }],
    ['uts_atr_period', 'settUtsAtrPeriod', 'UT ATR Period', 'int', 10, { step: '1', min: '1', max: '100' }],
    ['uts_key_value_5min', 'settUtsKeyValue5', '5m Key Value', 'float', 1.0, { step: '0.1', min: '0.1', max: '30' }],
    ['uts_atr_period_5min', 'settUtsAtrPeriod5', '5m ATR Period', 'int', 10, { step: '1', min: '1', max: '100' }],
    ['uts_key_value_1min', 'settUtsKeyValue1', '1m Key Value', 'float', 1.0, { step: '0.1', min: '0.1', max: '30' }],
    ['uts_atr_period_1min', 'settUtsAtrPeriod1', '1m ATR Period', 'int', 10, { step: '1', min: '1', max: '100' }],
    ['uts_use_heikin_ashi', 'settUtsUseHa', 'Use Heikin Ashi Source', 'bool', false],
    ['uts_smart_exit', 'settUtsSmartExit', 'Enable Smart Exit', 'bool', true],
    ['uts_arm_atr', 'settUtsArmAtr', 'Arm Exit After Profit (ATR)', 'float', 0.8, { step: '0.05', min: '0.1', max: '10' }],
    ['uts_retrace_atr', 'settUtsRetraceAtr', 'Peak Retracement Trigger (ATR)', 'float', 0.45, { step: '0.05', min: '0.05', max: '10' }],
    ['uts_hard_retrace_atr', 'settUtsHardRetraceAtr', 'Emergency Peak Retracement (ATR)', 'float', 1.1, { step: '0.05', min: '0.2', max: '10' }],
    ['uts_min_bars', 'settUtsMinBars', 'Minimum Bars Before Smart Exit', 'int', 2, { step: '1', min: '0', max: '50' }],
    ['uts_confirmations', 'settUtsConfirmations', 'Momentum Confirmations Required', 'int', 2, { step: '1', min: '1', max: '4' }],
    ['uts_rsi_length', 'settUtsRsiLength', 'RSI Length', 'int', 14, { step: '1', min: '2', max: '100' }],
    ['uts_rsi_ob', 'settUtsRsiOb', 'RSI Overbought', 'float', 70, { step: '1', min: '50', max: '100' }],
    ['uts_rsi_os', 'settUtsRsiOs', 'RSI Oversold', 'float', 30, { step: '1', min: '0', max: '50' }],
    ['uts_rsi_lookback', 'settUtsRsiLookback', 'RSI Peak Lookback', 'int', 5, { step: '1', min: '2', max: '50' }],
    ['uts_exit_ema', 'settUtsExitEma', 'Exit EMA Length', 'int', 5, { step: '1', min: '1', max: '100' }],
    ['uts_wick_ratio', 'settUtsWickRatio', 'Rejection Wick Ratio', 'float', 0.35, { step: '0.05', min: '0.1', max: '0.9' }],
    ['uts_session_end', 'settUtsSessionEnd', 'Session End (bars from here dropped)', 'time', '15:15'],
    ['uts_lane_enabled', 'settUtsLaneEnabled', 'UT Smart Exit Lane (own entries + exits)', 'bool', true],
    ['uts_lane_allow_intrabar_entries', 'settUtsAllowIntrabar', 'Allow UTS forming candle entries', 'bool', true],
    ['uts_lane_result_max_age_seconds', 'settUtsResultAge', 'Repaint Evidence Max Age (seconds)', 'float', 30, { step: '1', min: '1', max: '120' }],
    ['uts_lane_reclaim_start', 'settUtsReclaimStart', 'Late Big-Move Check From', 'time', '13:00'],
    ['uts_lane_sl_max_pct', 'settUtsSlMaxPct', 'Max UT Stop (% of index)', 'float', 1.0, { step: '0.05', min: '0.05', max: '5' }],
    ['uts_lane_min_entry_rr', 'settUtsMinRr', 'Min Entry RR (nominal)', 'float', 0.5, { step: '0.1', min: '0.1', max: '5' }],
    ['uts_lane_target_r', 'settUtsTargetR', 'Nominal Target (R, never closes)', 'float', 3.0, { step: '0.5', min: '0.5', max: '50' }],
    ['uts_lane_guard_mult', 'settUtsGuardMult', 'Premium Guard (x UT stop)', 'float', 2.0, { step: '0.1', min: '1', max: '10' }],
    ['uts_lane_guard_min_pct', 'settUtsGuardMin', 'Premium Guard Min %', 'float', 10, { step: '1', min: '1', max: '80' }],
    ['uts_lane_guard_max_pct', 'settUtsGuardMax', 'Premium Guard Max %', 'float', 15, { step: '1', min: '1', max: '90' }],
    ['uts_lane_lock_arm_r', 'settUtsLockArm', 'Premium Profit Lock Arm (R)', 'float', 1, { step: '0.1', min: '0.5', max: '10' }],
    ['uts_lane_lock_giveback_pct', 'settUtsLockGiveback', 'Premium Peak Gain Giveback %', 'float', 30, { step: '1', min: '0', max: '100' }],
    ['uts_lane_5m_intel_gate_enabled', 'settUts5mIntelGate', '5m Entries Need Intel Support (vol + ADX + RSI + PCR)', 'bool', true],
    ['uts_lane_5m_min_volume_ratio', 'settUts5mMinVol', '5m Min Volume (x average)', 'float', 1.0, { step: '0.1', min: '0', max: '5' }],
    ['uts_lane_5m_min_adx', 'settUts5mMinAdx', '5m Min ADX', 'float', 20, { step: '1', min: '0', max: '60' }],
    ['uts_lane_5m_require_directional_rsi', 'settUts5mDirRsi', '5m Require RSI On Trade Side (55/45)', 'bool', true],
    ['uts_lane_5m_pcr_veto_enabled', 'settUts5mPcrVeto', '5m Block On Opposing PCR Intel', 'bool', true],
    ['uts_lane_intrabar_wait_seconds', 'settUtsWait', '5m / 15m Intra-candle Entry Wait (s)', 'float', 15, { step: '1', min: '0', max: '120' }],
    ['uts_lane_intrabar_wait_seconds_1min', 'settUtsWait1min', '1m Intra-candle Entry Wait (s)', 'float', 5, { step: '1', min: '5', max: '7' }],
    ['uts_lane_repaint_check_seconds', 'settUtsRepaintCheck', 'Intra-candle Repaint Check (s)', 'float', 10, { step: '1', min: '1', max: '120' }],
    ['uts_lane_repaint_reentry_grace_seconds', 'settUtsRepaintReentry', 'Re-entry Grace After Repaint Exit (s)', 'float', 90, { step: '5', min: '0', max: '600' }],
];

const AUTO_START_FIELDS = [
    ['auto_real_mode_premarket_enabled', 'settAutoRealPremarket', 'Switch HISTORICAL to REAL at the time below (Auto toggle unchanged)', 'bool', false],
    ['auto_real_mode_premarket_switch_time', 'settAutoRealSwitchTime', 'REAL Switch Time (IST)', 'time', '09:00:00'],
];

const UT_PRESET_HELP = {
    AGGRESSIVE: 'Trail 1.0x / ATR 10 - fastest UT trail, more flips',
    BALANCED: 'Trail 1.3x / ATR 10 - smoother UT trail',
    CONSERVATIVE: 'Trail 1.6x / ATR 14 - widest UT trail, slowest flips',
};

function ensureScannerEngineOptions(selectEl) {
    if (!selectEl) return;
    const current = String(selectEl.value || '').toLowerCase();
    const values = Array.from(selectEl.options || []).map(option => String(option.value || '').toLowerCase());
    const expected = SCANNER_ENGINE_OPTIONS.map(([value]) => value);
    const needsRepair = values.length !== expected.length || expected.some((value, idx) => values[idx] !== value);
    if (!needsRepair) return;
    selectEl.innerHTML = SCANNER_ENGINE_OPTIONS
        .map(([value, label]) => `<option value="${value}">${label}</option>`)
        .join('');
    if (expected.includes(current)) {
        selectEl.value = current;
    }
}

function getActiveScannerEngine() {
    return String(
        document.getElementById('settScannerEngine')?.value
        || document.getElementById('scannerEngineSelect')?.value
        || state.latestData?.config?.scanner_engine
        || 'quad_prime'
    ).toLowerCase();
}

function syncUtsSignalTiming(changedId) {
    if (!['ut_smart', 'ut_smart_s1'].includes(document.getElementById('settScannerEngine')?.value)) return;
    const main = document.getElementById('settIntrabarSignals');
    const uts = document.getElementById('settUtsAllowIntrabar');
    if (!main || !uts) return;
    if (changedId === 'settUtsAllowIntrabar') main.checked = uts.checked;
    else uts.checked = main.checked;
    const help = document.getElementById('settSignalModeHelp');
    if (help) help.textContent = main.checked
        ? 'ON: closed signals plus buffered forming entries; UTS 5m/15m 20s, 1m 5-7s; repaint checks every 10s after entry'
        : 'OFF: confirmed entries only after candle close';
}

function syncSettingsScannerEngine(mode) {
    const normalized = String(mode || 'quad_prime').toLowerCase();
    const settingsEl = document.getElementById('settScannerEngine');
    const topEl = document.getElementById('scannerEngineSelect');
    ensureScannerEngineOptions(settingsEl);
    ensureScannerEngineOptions(topEl);
    if (settingsEl && settingsEl.value !== normalized) settingsEl.value = normalized;
    if (topEl && topEl.value !== normalized) topEl.value = normalized;
    updateUtPresetHelp();
}

function updateUtPresetHelp() {
    const help = document.getElementById('settUtPresetHelp');
    if (!help) return;
    const preset = String(document.getElementById('settUtPreset')?.value || 'AGGRESSIVE').toUpperCase();
    const engine = getActiveScannerEngine();
    const presetHelp = UT_PRESET_HELP[preset] || UT_PRESET_HELP.AGGRESSIVE;
    const engineLabel = SCANNER_ENGINE_LABELS[engine] || engine.toUpperCase();
    if (engine === 'ut_ema') {
        help.textContent = `${engineLabel}: Native UT + EMA Pine rules and timing. One shared trade per index/timeframe; alignment adds one lot, and the first native exit closes it. Index stop 0.2%; option premium stop capped at 15%. UT presets and the global signal timing toggle do not change this engine.`;
        return;
    }
    let scope = 'Affects UT1 flips, ATR trail, chart trail, and exits.';
    if (engine === 'quad_union') {
        scope = 'Runs UT1, EMA, Supertrend, and Consensus raw arrows after repaint buffering; exits are source-aware.';
    } else if (engine === 'quad_prime_s1' || engine === 'ut_smart_s1') {
        scope = 'Runs both native engines independently. Signals retain their own stops and exits; portfolio risk and execution limits are shared.';
    } else if (engine === 'quad_prime') {
        scope = 'Runs all four raw entry sources; opposite exits need UT1 plus trend-stack confirmation unless risk is threatened.';
    } else if (engine === 'adaptive_hybrid') {
        scope = 'Engine 10 meta-router: chooses UT1+Consensus, Quad-style thrust, or strict consensus based on trend, chop, volume, ER, and source agreement.';
    } else if (engine === 'ut_smart') {
        scope = 'UT Smart Exit: its own UT flips (key x ATR trail) and its own exits (opposite flip, smart peak exit) on the frames the TF toggle selects (15M is the tuned frame; 1M trades bursts only); these UT presets do not drive it - use the UT Smart Exit card.';
    } else if (engine === 'adaptive_dual') {
        scope = 'Engine 11 dual-lane mode: Lane A uses Adaptive Hybrid; Lane B runs APEX five-gate confluence through the same gates and risk stack.';
    } else if (engine === 'quality_hybrid') {
        scope = 'Engine 12 independent scanner: Consensus ST+EMA, EMA, Aroon, Donchian, KAMA, and VWAP vote on direction; ATR stays stop/risk context only.';
    } else if (engine === 'utbot_consensus') {
        scope = 'Affects the UT1 leg and shared ATR trail; partner flips stay engine-driven.';
    } else if (engine === 'ema' || engine === 'consensus') {
        scope = 'EMA/Consensus flips stay unchanged; ATR trail and protective exits still use this UT trail.';
    }
    help.textContent = `${engineLabel}: ${presetHelp}. ${scope}`;
}

function attachSettingsSaveHandler(button) {
    if (!button || button.dataset.saveReady === '1') return;
    button.dataset.saveReady = '1';
    button.addEventListener('click', saveSettings);
}

function setupSettingsExperience() {
    const modal = document.getElementById('settingsModal');
    const dialog = modal?.querySelector('.modal');
    const body = modal?.querySelector('.modal-body');
    const header = modal?.querySelector('.modal-header');
    if (!modal || !dialog || !body) return;
    ensureScannerEngineOptions(document.getElementById('settScannerEngine'));
    ensureScannerEngineOptions(document.getElementById('scannerEngineSelect'));
    if (body.dataset.compactReady === '1') {
        updateUtPresetHelp();
        return;
    }

    dialog.classList.add('settings-modal');
    header?.classList.add('settings-header');
    body.classList.add('settings-body');
    body.dataset.compactReady = '1';

    const saveBtn = document.getElementById('saveSettings');
    const resetBtn = document.getElementById('resetCacheBtn');
    const closeBtn = document.getElementById('closeSettings');
    let headerSaveBtn = document.getElementById('saveSettingsTop');
    if (!headerSaveBtn) {
        headerSaveBtn = document.createElement('button');
        headerSaveBtn.className = 'btn-primary settings-header-save';
        headerSaveBtn.type = 'button';
        headerSaveBtn.id = 'saveSettingsTop';
        headerSaveBtn.textContent = 'Save & Apply';
        closeBtn?.before(headerSaveBtn);
    }
    attachSettingsSaveHandler(saveBtn);
    attachSettingsSaveHandler(headerSaveBtn);
    const findGroup = (id) => {
        const el = document.getElementById(id);
        return el?.closest('.setting-group') || el;
    };
    const makeInputGroup = (id, label, type = 'number', attrs = {}) => {
        if (document.getElementById(id)) return findGroup(id);
        const group = document.createElement('div');
        group.className = 'setting-group';
        const inputAttrs = Object.entries(attrs)
            .map(([key, value]) => `${key}="${String(value)}"`)
            .join(' ');
        group.innerHTML = `<label>${label}</label><input type="${type}" id="${id}" ${inputAttrs}>`;
        body.appendChild(group);
        return group;
    };
    const makeSwitchCard = (id, title, detail = '') => {
        if (document.getElementById(id)) return document.getElementById(id)?.closest('.settings-switch-card');
        const card = document.createElement('label');
        card.className = 'settings-switch-card';
        card.innerHTML = `
            <span><strong>${title}</strong>${detail ? `<small>${detail}</small>` : ''}</span>
            <input type="checkbox" id="${id}">
        `;
        body.appendChild(card);
        return card;
    };
    const makeCard = (id, title, nodes = []) => {
        const section = document.createElement('section');
        section.className = 'settings-card';
        section.id = id;
        const titleEl = document.createElement('div');
        titleEl.className = 'settings-card-title';
        titleEl.textContent = title;
        const grid = document.createElement('div');
        grid.className = 'settings-grid';
        nodes.filter(Boolean).forEach(node => grid.appendChild(node));
        section.append(titleEl, grid);
        return section;
    };

    if (!document.getElementById('settScannerEngine')) {
        const engineGroup = document.createElement('div');
        engineGroup.className = 'setting-group';
        engineGroup.innerHTML = `
            <label>Signal Engine</label>
            <select id="settScannerEngine"></select>
        `;
        body.appendChild(engineGroup);
    }
    ensureScannerEngineOptions(document.getElementById('settScannerEngine'));
    ensureScannerEngineOptions(document.getElementById('scannerEngineSelect'));
    if (!document.getElementById('settUtPreset')) {
        const presetGroup = document.createElement('div');
        presetGroup.className = 'setting-group';
        presetGroup.innerHTML = `
            <label>UT Trail Sensitivity</label>
            <select id="settUtPreset" aria-describedby="settUtPresetHelp">
                <option value="AGGRESSIVE">Aggressive - Trail 1.0x / ATR 10</option>
                <option value="BALANCED">Balanced - Trail 1.3x / ATR 10</option>
                <option value="CONSERVATIVE">Conservative - Trail 1.6x / ATR 14</option>
            </select>
            <small id="settUtPresetHelp" class="settings-control-help"></small>
        `;
        body.appendChild(presetGroup);
    }
    if (!document.getElementById('settFuturesShadow')) {
        const shadowCard = document.createElement('label');
        shadowCard.className = 'settings-switch-card';
        shadowCard.innerHTML = `
            <span>
                <strong>Futures Shadow Comparison</strong>
                <small>Research-only FUT rows in historical AUTO mode; never submits live orders</small>
            </span>
            <input type="checkbox" id="settFuturesShadow">
        `;
        body.appendChild(shadowCard);
    }
    if (!document.getElementById('settPullbackHedge')) {
        const hedgeCard = document.createElement('label');
        hedgeCard.className = 'settings-switch-card';
        hedgeCard.innerHTML = `
            <span>
                <strong>Pullback Hedge (A/B)</strong>
                <small>OFF by default. Adds a fresh-ATM opposite leg on a runner's pullback. Backtest (8 sessions): armed once and lost — the 85-90% runner lock already protects the give-back more cheaply.</small>
            </span>
            <input type="checkbox" id="settPullbackHedge">
        `;
        body.appendChild(hedgeCard);
    }
    if (!document.getElementById('settStopOutReentry')) {
        const soCard = document.createElement('label');
        soCard.className = 'settings-switch-card';
        soCard.innerHTML = `
            <span>
                <strong>Stop-Out Re-Entry</strong>
                <small>Optional shadow/A-B feature. Current cost-correct archive replay is negative after costs, so it ships OFF. If enabled, it bypasses only the cooldown — never the daily budget or loss halt.</small>
            </span>
            <input type="checkbox" id="settStopOutReentry">
        `;
        body.appendChild(soCard);
    }
    if (!document.getElementById('settPerTfGrading')) {
        const tfCard = document.createElement('label');
        tfCard.className = 'settings-switch-card';
        tfCard.innerHTML = `
            <span>
                <strong>Per-Timeframe Grading (A/B)</strong>
                <small>OFF by default. Grades each candidate with its OWN timeframe's intelligence. Today one 5m snapshot grades everything, so 15m signals are scored on 5m context (they differ in 83% of samples). Changes grades/confidence system-wide — A/B before enabling.</small>
            </span>
            <input type="checkbox" id="settPerTfGrading">
        `;
        body.appendChild(tfCard);
    }
    if (!document.getElementById('settEngineLab')) {
        const engineLabCard = document.createElement('label');
        engineLabCard.className = 'settings-switch-card';
        engineLabCard.innerHTML = `
            <span>
                <strong>12-Engine Lab</strong>
                <small>Shadow-compare all scanner modes in dashboard; never submits orders</small>
            </span>
            <input type="checkbox" id="settEngineLab" checked>
        `;
        body.appendChild(engineLabCard);
    }
    if (!document.getElementById('settEngineLabRealOnly')) {
        const engineLabRealCard = document.createElement('label');
        engineLabRealCard.className = 'settings-switch-card';
        engineLabRealCard.innerHTML = `
            <span>
                <strong>Engine Lab Real Mode Only</strong>
                <small>OFF also allows historical/offline comparison scans</small>
            </span>
            <input type="checkbox" id="settEngineLabRealOnly">
        `;
        body.appendChild(engineLabRealCard);
    }
    if (!document.getElementById('settIntrabarSignals')) {
        const signalCard = document.createElement('label');
        signalCard.className = 'settings-switch-card';
        signalCard.title = 'ON permits buffered forming entries alongside closed signals. UTS waits 20s on 5m/15m and 5-7s on 1m.';
        signalCard.innerHTML = `
            <span>
                <strong>Intra-candle Signals</strong>
                <small id="settSignalModeHelp">ON: faster entries while the candle forms</small>
            </span>
            <input type="checkbox" id="settIntrabarSignals" checked aria-describedby="settSignalModeHelp">
        `;
        const input = signalCard.querySelector('input');
        const help = signalCard.querySelector('small');
        input?.addEventListener('change', () => {
            help.textContent = input.checked
                ? 'ON: faster entries while the candle forms'
                : 'OFF: confirmed entries only after candle close';
            syncUtsSignalTiming('settIntrabarSignals');
        });
        body.appendChild(signalCard);
    }


    const nav = document.createElement('nav');
    nav.className = 'settings-nav';
    [
        ['settings-system', 'System'],
        ['settings-risk', 'Risk'],
        ['settings-options', 'Options'],
        ['settings-lots', 'Lots'],
        ['settings-limits', 'Limits'],
        ['settings-indices', 'Indices'],
        ['settings-gates', 'Gates'],
        ['settings-timing', 'Timing'],
    ].forEach(([href, label]) => {
        const link = document.createElement('a');
        link.href = `#${href}`;
        link.textContent = label;
        nav.appendChild(link);
    });

        // Tabs Structure
    const tabsNav = document.createElement('nav');
    tabsNav.className = 'settings-nav';
    tabsNav.style.display = 'flex';
    tabsNav.style.gap = '10px';
    tabsNav.style.marginBottom = '20px';
    tabsNav.style.borderBottom = '1px solid var(--border-color)';
    tabsNav.style.paddingBottom = '10px';
    
    const generalTab = document.createElement('button');
    generalTab.className = 'sett-tab active';
    generalTab.textContent = 'General Settings';
    generalTab.style.background = 'transparent';
    generalTab.style.border = 'none';
    generalTab.style.color = 'var(--text-color)';
    generalTab.style.fontWeight = 'bold';
    generalTab.style.cursor = 'pointer';

    const advancedTab = document.createElement('button');
    advancedTab.className = 'sett-tab';
    advancedTab.textContent = 'Advanced Settings';
    advancedTab.style.background = 'transparent';
    advancedTab.style.border = 'none';
    advancedTab.style.color = 'var(--text-muted)';
    advancedTab.style.cursor = 'pointer';

    const forensicsTab = document.createElement('button');
    forensicsTab.className = 'sett-tab';
    forensicsTab.textContent = 'Signal Forensics';
    forensicsTab.style.background = 'transparent';
    forensicsTab.style.border = 'none';
    forensicsTab.style.color = 'var(--text-muted)';
    forensicsTab.style.cursor = 'pointer';

    tabsNav.append(generalTab, advancedTab, forensicsTab);

    const content = document.createElement('div');
    content.className = 'settings-content-wrapper';

    const generalContent = document.createElement('div');
    generalContent.className = 'sett-content-pane active';
    generalContent.id = 'generalSettContentView';

    const advancedContent = document.createElement('div');
    advancedContent.className = 'sett-content-pane';
    advancedContent.id = 'advancedSettContentView';
    advancedContent.style.display = 'none';

    const forensicsContent = document.createElement('div');
    forensicsContent.className = 'sett-content-pane';
    forensicsContent.id = 'forensicsSettContentView';
    forensicsContent.style.display = 'none';
    forensicsContent.innerHTML = `
        <div class="sf-shell">
            <!-- Header -->
            <div class="sf-header">
                <div>
                    <div class="sf-header-label">LIVE SESSION FLIGHT RECORDER</div>
                    <div class="sf-title">Signal Forensics</div>
                    <div class="sf-subtitle">Structured evidence from detection through final exit. Analysis only.</div>
                </div>
                <div class="sf-header-right">
                    <span class="sf-live-badge"><span class="sf-live-dot"></span> LIVE</span>
                    <span class="sf-live-time" id="sfLiveTime"></span>
                </div>
            </div>
            <!-- Session + Stats -->
            <div class="sf-stats-bar">
                <div class="sf-stats-session">
                    <div class="sf-stats-label">Session</div>
                    <select id="sfFilterDate" class="sf-stats-select"><option value="">Current</option></select>
                </div>
                <div class="sf-stats-cell"><div class="sf-stats-label">EVENTS</div><div class="sf-stats-num" id="sfStatEvents">0</div></div>
                <div class="sf-stats-cell"><div class="sf-stats-label">SIGNALS</div><div class="sf-stats-num" id="sfStatSignals">0</div></div>
                <div class="sf-stats-cell"><div class="sf-stats-label">LATEST ID</div><div class="sf-stats-num" id="sfStatLatest">0</div></div>
            </div>
            <!-- Filters -->
            <div class="sf-filters">
                <select id="sfFilterInstrument" class="sf-flt"><option value="">All instruments</option><option>NIFTY</option><option>BANKNIFTY</option><option>SENSEX</option><option>MIDCPNIFTY</option></select>
                <select id="sfFilterTimeframe" class="sf-flt"><option value="">All TF</option><option value="1min">1min</option><option value="3min">3min</option><option value="5min">5min</option><option value="15min">15min</option></select>
                <select id="sfFilterPhase" class="sf-flt sf-flt-wide"><option value="">All phases</option><option>DETECTED</option><option>STABILIZING</option><option>ACCEPTED</option><option>REJECTED</option><option>ORDER_PENDING</option><option>ORDER_SUBMITTED</option><option>FILLED</option><option>ENTERED</option><option>TRAILING</option><option>MARK_TO_MARKET</option><option>EXITED</option></select>
                <select id="sfFilterDecision" class="sf-flt"><option value="">All decisions</option><option>PENDING</option><option>ACCEPTED</option><option>REJECTED</option><option>SUBMITTED</option><option>FILLED</option><option>OPEN</option><option>CLOSED</option><option>CANCELLED</option></select>
                <input type="text" id="sfFilterSearch" class="sf-flt sf-flt-search" placeholder="Reason, contract, signal ID\u2026">
            </div>
            <!-- Actions -->
            <div class="sf-actions">
                <div class="sf-actions-l">
                    <button class="sf-btn" id="sfBtnApply">APPLY</button>
                    <button class="sf-btn" id="sfBtnClear">CLEAR</button>
                    <button class="sf-btn" id="sfBtnRefresh">REFRESH</button>
                </div>
                <div class="sf-actions-r">
                    <label class="sf-toggle"><input type="checkbox" id="sfAutoFollow" checked> Auto follow newest</label>
                    <button class="sf-btn" id="sfBtnExportCsv">EXPORT CSV</button>
                    <button class="sf-btn" id="sfBtnExportJson">EXPORT JSON</button>
                </div>
            </div>
            <!-- Table -->
            <div class="sf-ledger-wrap">
                <table class="sf-ledger">
                    <thead><tr>
                        <th>IST TIME</th><th>SIGNAL / LIFECYCLE</th><th>INSTRUMENT / CONTRACT</th>
                        <th>TF</th><th>DIR</th><th>PHASE</th><th>DECISION</th>
                        <th>EXACT REASON</th><th>SOURCE / GATE</th><th>GRADE / CONF</th>
                        <th>LTP / ENTRY</th><th>STOP I&rarr;NOW</th><th>TARGET</th><th>P&amp;L</th>
                    </tr></thead>
                    <tbody></tbody>
                </table>
            </div>
            <button class="sf-load-more" style="display:none;">Load more\u2026</button>
        </div>
        <div class="sf-drawer" id="sfDrawer">
            <div class="sf-drawer-header">
                <h3 id="sfDrawerTitle">Lifecycle Detail</h3>
                <button class="sf-drawer-close" aria-label="Close drawer">\u00d7</button>
            </div>
            <div class="sf-drawer-body" id="sfDrawerBody"></div>
        </div>
    `;
    let _forensicsInited = false;

    function _activateTab(activeBtn, activePane) {
        [generalTab, advancedTab, forensicsTab].forEach(t => {
            t.style.fontWeight = 'normal';
            t.style.color = 'var(--text-muted)';
        });
        [generalContent, advancedContent, forensicsContent].forEach(c => {
            c.style.display = 'none';
        });
        activeBtn.style.fontWeight = 'bold';
        activeBtn.style.color = 'var(--text-color)';
        activePane.style.display = 'block';
    }

    generalTab.addEventListener('click', () => _activateTab(generalTab, generalContent));
    advancedTab.addEventListener('click', () => _activateTab(advancedTab, advancedContent));
    forensicsTab.addEventListener('click', () => {
        _activateTab(forensicsTab, forensicsContent);
        if (!_forensicsInited && window.SignalForensics) {
            window.SignalForensics.init({ pane: forensicsContent });
            _forensicsInited = true;
        }
    });

    content.className = 'settings-content';

    const indexGrid = body.querySelector('.index-selection-grid');
    if (indexGrid) indexGrid.classList.add('compact');
    generalContent.appendChild(makeCard('settings-system', 'System Controls', [
        findGroup('settBacktestDays'),
        findGroup('settInstPref'),
        findGroup('settStrikeSelection'),
        findGroup('settGradePreference'),
        findGroup('settScannerEngine'),
        findGroup('settUtPreset'),
        document.getElementById('settIntrabarSignals')?.closest('.settings-switch-card'),
        // Execution Auto Mode deliberately has one authority: the guarded top-bar
        // switch in app.js.  A second copy in this modal retained stale state while
        // open and could re-enable REAL execution when saving an unrelated setting.
        document.getElementById('settStopOutReentry')?.closest('.settings-switch-card'),
        document.getElementById('settPerTfGrading')?.closest('.settings-switch-card'),
        document.getElementById('settPullbackHedge')?.closest('.settings-switch-card'),
        document.getElementById('settFuturesShadow')?.closest('.settings-switch-card'),
        document.getElementById('settEngineLab')?.closest('.settings-switch-card'),
        document.getElementById('settEngineLabRealOnly')?.closest('.settings-switch-card'),
    ]));
    generalContent.appendChild(makeCard('settings-risk', 'Capital & Futures Risk', [
        findGroup('settCapitalTotal'),
        findGroup('settCapitalFut'),
        findGroup('settRiskFut'),
        findGroup('settFutureSL'),
        findGroup('settFutCost')
    ]));
    generalContent.appendChild(makeCard('settings-options', 'Options Configuration', [
        findGroup('settCapitalOpt'),
        findGroup('settRiskOpt'),
        findGroup('settOptionSL'),
        findGroup('settOptCost')
    ]));

    const lots = document.createElement('section');
    lots.className = 'settings-card';
    lots.id = 'settings-lots';
    const lotsTitle = document.createElement('div');
    lotsTitle.className = 'settings-card-title';
    lotsTitle.textContent = 'Trading Lots';
    const lotsMatrix = document.createElement('div');
    lotsMatrix.className = 'lots-matrix';
    ['Index', 'Options', 'Futures'].forEach(text => {
        const head = document.createElement('div');
        head.className = 'lots-head';
        head.textContent = text;
        lotsMatrix.appendChild(head);
    });
    [
        ['NIFTY', 'settLotsNifty', 'settLotsFutNifty'],
        ['BANKNIFTY', 'settLotsBN', 'settLotsFutBN'],
        ['SENSEX', 'settLotsSensex', 'settLotsFutSensex'],
        ['MIDCPNIFTY', 'settLotsMidcap', 'settLotsFutMidcap'],
    ].forEach(([name, optId, futId]) => {
        const label = document.createElement('div');
        label.className = 'lots-name';
        label.textContent = name;
        lotsMatrix.appendChild(label);
        [optId, futId].forEach(id => {
            const input = document.getElementById(id);
            if (input) lotsMatrix.appendChild(input);
        });
    });
    lots.append(lotsTitle, lotsMatrix);
    generalContent.appendChild(lots);

    generalContent.appendChild(makeCard('settings-limits', 'Per-Index Trade Limits', [
        findGroup('settMaxTrades'),
        findGroup('settMaxLosses'),
        findGroup('settIndexCooldown'),
    ]));

    generalContent.appendChild(makeCard('settings-indices', 'Active Trading Indices', [indexGrid]));

    const cacheCard = makeCard('settings-cache', 'Cache Management', [resetBtn?.closest('.setting-group') || resetBtn]);
    cacheCard.classList.add('settings-actions-card');
    generalContent.appendChild(cacheCard);
    // Advanced Settings Injection
    const advTradeCard = makeCard('settings-gates', 'Advanced Trade Gates', [
        makeSwitchCard('settLeanRouting', 'Lean Routing To Options', 'AUTO routes marginal setups to defined-risk options'),
        makeSwitchCard('settPriceActionModule', 'Price Action Module', 'Structural S/R and candle context'),
        makeSwitchCard('settPriceActionBlock', 'Block PA-Adverse', 'Reject adverse setups when enabled'),
        makeInputGroup('settPriceActionThreshold', 'PA Adverse Threshold', 'number', { step: '0.05', min: '-1', max: '1' }),
        makeSwitchCard('settPriceActionRelief', 'PA Reversal Relief', 'Confirmed breakout/breakdown can override weak S/R vetoes'),
        makeSwitchCard('settRegimeVeto', 'Regime Veto', 'Block trendless directional FUT entries'),
        makeInputGroup('settRegimeVetoAdx', 'Regime Veto ADX Floor', 'number', { step: '1', min: '0', max: '100' }),
        makeSwitchCard('settRegimeErPromotion', 'ER Early Trend', 'Kaufman ER promotes clean low-ADX opening drives; confirmed range days block it'),
        makeInputGroup('settRegimeErPeriod', 'ER Lookback Bars', 'number', { step: '1', min: '2', max: '60' }),
        makeInputGroup('settRegimeErThreshold', 'ER Threshold', 'number', { step: '0.01', min: '0', max: '1' }),
        makeInputGroup('settRegimeErAdxCeiling', 'ER ADX Ceiling', 'number', { step: '1', min: '0', max: '100' }),
        makeInputGroup('settRegimeErRangeConf', 'ER Range-Block Conf', 'number', { step: '0.01', min: '0', max: '1' }),
        makeSwitchCard('settFutLossCut', 'FUT Early Loss Cut', 'Cut failed FUTs before full stop'),
        makeInputGroup('settFutLossCutR', 'FUT Loss-Cut R', 'number', { step: '0.05', min: '0.05', max: '5' }),
        makeSwitchCard('settTrendContinuation', '5m Trend Continuation', 'Strong 5m continuation in confirmed trend'),
        makeInputGroup('settTrendContinuationAdx', 'Trend Continuation ADX', 'number', { step: '1', min: '0', max: '100' }),
        makeSwitchCard('settConfidenceCalibration', 'Confidence Calibration', 'No-op until enough REAL outcomes exist'),
        makeInputGroup('settCalibrationMinSamples', 'Calibration Min Trades', 'number', { step: '10', min: '1', max: '100000' })
    ]);

    const advTimingCard = makeCard('settings-timing', 'Spike & Timing Gates', [
        makeSwitchCard('settPowerMoveOverride', 'Power-Move Override', 'Data-confirmed late directional thrust'),
        makeSwitchCard('settSpikeBypass', 'Spike Bypass 15m', 'Allow explosive spikes before 15m agrees'),
        makeInputGroup('settSpikeRangeAtr', 'Spike Range ATR Mult', 'number', { step: '0.1', min: '0.1', max: '20' }),
        makeSwitchCard('settVolumeSpikeEntry', 'Volume Spike Entry', '1m/5m directional thrust entries'),
        makeSwitchCard('settOneMinScalping', '1m Scalping Mode', 'Allow 1m thrust entries; tracking stays on'),
        makeInputGroup('settVolumeSpikeVolRatio', 'Volume Spike Ratio', 'number', { step: '0.1', min: '0', max: '100' }),
        makeInputGroup('settVolumeSpikeRangeAtr', 'Volume Spike Range ATR', 'number', { step: '0.1', min: '0', max: '100' }),
        makeInputGroup('settVolumeSpikeCloseLocation', 'Volume Spike Close Location', 'number', { step: '0.05', min: '0', max: '1' }),
        makeInputGroup('settVolumeSpikeFutSL', 'Volume Spike FUT SL %', 'number', { step: '0.01', min: '0', max: '100' }),
        makeInputGroup('settVolumeSpikeOptSL', 'Volume Spike OPT SL %', 'number', { step: '0.1', min: '0', max: '100' }),
        makeInputGroup('settVolumeSpikeStabilization', '1m Spike Stabilization Sec (floor 3s)', 'number', { step: '0.5', min: '3', max: '15' }),
        makeSwitchCard('settVolumeSpikeDepthConfirm', '1m Spike Depth/OFR Confirm', 'When L1 depth is usable, require OFR aligned with BUY/SELL'),
        makeInputGroup('settVolumeSpikeOfrBull', 'Spike OFR Bullish Floor', 'number', { step: '0.01', min: '0.5', max: '3' }),
        makeInputGroup('settVolumeSpikeOfrBear', 'Spike OFR Bearish Ceiling', 'number', { step: '0.01', min: '0.1', max: '2' }),
        makeSwitchCard('settStabilizationMomentumOverride', 'Stabilization Momentum Override', 'Re-admit a flickered signal when thrust keeps confirming direction'),
        makeInputGroup('settLiveStab5', '5m Stabilization Sec', 'number', { step: '1', min: '30', max: '60' }),
        makeInputGroup('settLiveStab15', '15m Stabilization Sec', 'number', { step: '1', min: '30', max: '60' }),
        makeInputGroup('settNoEntryAfter', '15m Last Entry', 'time'),
        makeInputGroup('sett5mNoEntryAfter', '5m Last Entry', 'time'),
        makeInputGroup('sett1mNoEntryAfter', '1m Last Entry', 'time'),
        makeInputGroup('settForceExitTime', 'Force Exit Time', 'time'),
        makeInputGroup('settPowerMoveCutoff', 'Power-Move Cutoff', 'time')
    ]);

    const advMomentumCard = makeCard('settings-reversal-relief', 'Reversal & Momentum Relief', [
        makeSwitchCard('settLive15MomentumOverride', '15m Momentum Override', 'Allow high-thrust 15m continuation/reversal relief'),
        makeInputGroup('settLive15MomentumMinConf', '15m Momentum Min Conf', 'number', { step: '0.01', min: '0', max: '1' }),
        makeInputGroup('settLive15MomentumVolRatio', '15m Momentum Volume Ratio', 'number', { step: '0.1', min: '0', max: '100' }),
        makeInputGroup('settLive15MomentumRangeAtr', '15m Momentum Range ATR', 'number', { step: '0.05', min: '0', max: '100' }),
        makeInputGroup('settLive15MomentumCloseLocation', '15m Momentum Close Location', 'number', { step: '0.05', min: '0', max: '1' }),
        makeInputGroup('settLive15MomentumAdx', '15m Momentum ADX', 'number', { step: '1', min: '0', max: '100' }),
        makeInputGroup('settLive15MomentumIntel', '15m Momentum Intel Score', 'number', { step: '0.05', min: '0', max: '1' }),
        makeSwitchCard('settReversalEntryRelief', 'Reversal Entry Relief', 'Confirmed rollovers can relax counter-session bias'),
        makeInputGroup('settReversalMinConf', 'Reversal Min Conf', 'number', { step: '0.01', min: '0', max: '1' }),
        makeInputGroup('settReversalRangeAtr', 'Reversal Range ATR', 'number', { step: '0.05', min: '0', max: '100' }),
        makeInputGroup('settReversalCloseLocation', 'Reversal Close Location', 'number', { step: '0.05', min: '0', max: '1' }),
        makeInputGroup('settReversalVolumeRatio', 'Reversal Volume Ratio', 'number', { step: '0.1', min: '0', max: '100' }),
        makeInputGroup('settReversalVwapDev', 'Reversal VWAP Dev %', 'number', { step: '0.01', min: '0', max: '10' }),
        makeSwitchCard('settDirectionalCensusRelief', 'Census Reversal Relief', 'Let broad reversal evidence override stale session direction memory'),
        makeInputGroup('settDirectionalCensusMinConf', 'Census Relief Min Conf', 'number', { step: '0.01', min: '0', max: '1' }),
        makeSwitchCard('settLateSessionChoppyGuard', 'Late-Session Choppy Guard', 'Block low-runway chop unless a momentum scalp qualifies'),
        makeInputGroup('settLateSessionRunway', 'Late-Session Min Runway', 'number', { step: '1', min: '0', max: '180' })
    ]);

    const advPeakExitCard = makeCard('settings-peak-exit', 'Peak Exit Protection', [
        makeSwitchCard('settPeakThreatExit', 'Peak Threat Exit', 'Exit near peak when reversal, OI, volume, and intel evidence turns adverse'),
        makeInputGroup('settPeakThreatMinPeak', 'Min Peak Rs', 'number', { step: '50', min: '0', max: '100000' }),
        makeInputGroup('settPeakThreatMinPeakR', 'Min Peak R', 'number', { step: '0.05', min: '0', max: '5' }),
        makeInputGroup('settPeakThreatRetention', 'Min Retention', 'number', { step: '0.01', min: '0', max: '1' }),
        makeInputGroup('settPeakThreatScore', 'Threat Score', 'number', { step: '1', min: '1', max: '20' }),
        makeInputGroup('settPeakThreatSevereScore', 'Severe Score', 'number', { step: '1', min: '1', max: '20' }),
        makeInputGroup('settPeakThreatIntel', 'Adverse Intel', 'number', { step: '0.05', min: '0', max: '1' }),
        makeInputGroup('settPeakThreatWeakVol', 'Weak Volume', 'number', { step: '0.05', min: '0', max: '100' }),
        makeSwitchCard('settPeakThreatLossExit', 'Loss Threat Exit', 'Cut small losers early when adverse evidence is overwhelming'),
        makeInputGroup('settPeakThreatLossScore', 'Loss Score', 'number', { step: '1', min: '1', max: '20' }),
        makeInputGroup('settPeakThreatLossMaxR', 'Loss Max R', 'number', { step: '0.05', min: '0.05', max: '5' }),
        makeInputGroup('settOptionMajorWinTrendCap', 'Option Major-Win Trend Cap', 'number', { step: '0.01', min: '0.75', max: '1' }),
        makeSwitchCard('settBreakevenStop', 'Breakeven Stop', 'Once a trade has shown this much profit it may not close negative. Only fires on a trade already heading to a loss, so it never clips a winner still running'),
        makeInputGroup('settBreakevenStopArm', 'Breakeven Arm Rs', 'number', { step: '25', min: '0', max: '100000' }),
        makeInputGroup('settBreakevenStopFloor', 'Breakeven Floor Rs', 'number', { step: '10', min: '-5000', max: '5000' }),
        makeSwitchCard('settPeakTrail', 'Peak Trail (large peaks only)', 'Above the gate below, the breakeven stop is raised from a flat floor to a share of the running peak. Gated on purpose: an ungated percentage trail clips winners and measured WORSE than no trail at every tightness'),
        makeInputGroup('settPeakTrailAbove', 'Peak Trail Gate Rs', 'number', { step: '50', min: '0', max: '100000' }),
        makeInputGroup('settPeakTrailGiveback', 'Peak Trail Give-back', 'number', { step: '0.05', min: '0', max: '1' }),
        makeSwitchCard('settPeakGivebackCap', 'Give-back Cap', 'Cap how much a trade may hand back from its peak. Nets more than the breakeven stop but CAN exit a trade that is still in profit — off by default'),
        makeInputGroup('settPeakGivebackCapArm', 'Give-back Arm Rs', 'number', { step: '25', min: '0', max: '100000' }),
        makeInputGroup('settPeakGivebackCapRs', 'Give-back Cap Rs', 'number', { step: '25', min: '0', max: '100000' }),
        makeInputGroup('settPeakGivebackCapPct', 'Give-back Cap % of Peak', 'number', { step: '0.05', min: '0', max: '1' }),
        makeInputGroup('settPeakGivebackCapMaxPeak', 'Give-back Ceiling Rs', 'number', { step: '100', min: '0', max: '100000' }),
        makeSwitchCard('settBacktestLiveExits', 'Backtest Uses Live Exits', 'Let the replay evaluate the live exit guards. OFF by default: a run with this on is NOT comparable to a run with it off')
    ]);

    const advMarketCard = makeCard('settings-market-wide', 'Market-Wide Reversal', [
        makeSwitchCard('settMarketWideReversal', 'Cross-Index Reversal Detection', 'Use broad index alignment for reversal entry/exit context'),
        makeInputGroup('settMarketWideCoreVotes', 'Core Votes', 'number', { step: '1', min: '1', max: '4' }),
        makeInputGroup('settMarketWideTotalVotes', 'Total Votes', 'number', { step: '1', min: '1', max: '4' }),
        makeInputGroup('settMarketWideRangeAtr', 'Range ATR Base', 'number', { step: '0.05', min: '0', max: '100' }),
        makeInputGroup('settMarketWideRangeAtr5', '5m Range ATR', 'number', { step: '0.05', min: '0', max: '100' }),
        makeInputGroup('settMarketWideRangeAtr15', '15m Range ATR', 'number', { step: '0.05', min: '0', max: '100' }),
        makeInputGroup('settMarketWideMidcapRangeAtr5', 'MIDCP 5m Range ATR', 'number', { step: '0.05', min: '0', max: '100' }),
        makeInputGroup('settMarketWideMidcapRangeAtr15', 'MIDCP 15m Range ATR', 'number', { step: '0.05', min: '0', max: '100' }),
        makeInputGroup('settMarketWideCloseLocation', 'Close Location', 'number', { step: '0.05', min: '0', max: '1' }),
        makeInputGroup('settMarketWideSkew5', '5m Max Skew Sec', 'number', { step: '30', min: '0', max: '3600' }),
        makeInputGroup('settMarketWideSkew15', '15m Max Skew Sec', 'number', { step: '60', min: '0', max: '7200' })
    ]);

    const advOneWayCascadeCard = makeCard('settings-one-way-cascade', 'One-Way Cascade Runner', [
        makeSwitchCard('settOneWayCascadeEnabled', 'Cascade Runner', 'Broad all-index thrust can override location risk and trail winners by total gain'),
        makeInputGroup('settOneWayCascadeCoreVotes', 'Core Votes', 'number', { step: '1', min: '1', max: '3' }),
        makeInputGroup('settOneWayCascadeTotalVotes', 'Total Votes', 'number', { step: '1', min: '1', max: '4' }),
        makeInputGroup('settOneWayCascadeVolumeVotes', 'Volume Votes', 'number', { step: '1', min: '1', max: '4' }),
        makeInputGroup('settOneWayCascadeVolume', 'Volume Floor', 'number', { step: '0.05', min: '0', max: '100' }),
        makeInputGroup('settOneWayCascadeRange1', '1m Range ATR', 'number', { step: '0.05', min: '0', max: '100' }),
        makeInputGroup('settOneWayCascadeRange5', '5m Range ATR', 'number', { step: '0.05', min: '0', max: '100' }),
        makeInputGroup('settOneWayCascadeRange15', '15m Range ATR', 'number', { step: '0.05', min: '0', max: '100' }),
        makeInputGroup('settOneWayCascadeClose', 'Close Location', 'number', { step: '0.05', min: '0', max: '1' }),
        makeInputGroup('settOneWayCascadeRunnerLock', 'Runner Lock', 'number', { step: '0.01', min: '0.5', max: '0.98' }),
        makeInputGroup('settOneWayCascadeOptSL', 'OPT SL %', 'number', { step: '0.1', min: '0', max: '100' }),
        makeInputGroup('settOneWayCascadeFutSL', 'FUT SL %', 'number', { step: '0.01', min: '0', max: '100' }),
        makeInputGroup('settOneWayCascadeRR', 'RR', 'number', { step: '0.1', min: '0.1', max: '10' })
    ]);

    const advMissedReentryCard = makeCard('settings-missed-reentry', 'Missed Signal Re-entry', [
        makeSwitchCard('settMissedReentryEnabled', 'Re-entry Lane', 'Rebuild a recent soft-rejected signal only if trend, cascade, and chase checks still agree'),
        makeSwitchCard('settMissedReentryTrend', 'Trend Context', 'Allow aligned regime plus intel/ADX support when cascade is absent'),
        makeSwitchCard('settMissedReentryPreferOptions', 'Prefer Options', 'Use fresh ATM CE/PE for re-entry unless disabled'),
        makeInputGroup('settMissedReentryMaxAge', 'Max Age Min', 'number', { step: '0.5', min: '1', max: '30' }),
        makeInputGroup('settMissedReentryMinAge', 'Min Age Sec', 'number', { step: '5', min: '0', max: '300' }),
        makeInputGroup('settMissedReentryChaseAtr', 'Max Chase ATR', 'number', { step: '0.05', min: '0', max: '5' }),
        makeInputGroup('settMissedReentryConf', 'Min Confidence', 'number', { step: '0.01', min: '0', max: '1' }),
        makeInputGroup('settMissedReentryIntel', 'Min Intel', 'number', { step: '0.05', min: '0', max: '1' }),
        makeInputGroup('settMissedReentryAdx', 'Trend ADX', 'number', { step: '1', min: '0', max: '100' }),
        makeInputGroup('settMissedReentryRunway', 'Min Runway', 'number', { step: '1', min: '0', max: '180' }),
        makeInputGroup('settMissedReentryOptSL', 'OPT SL %', 'number', { step: '0.1', min: '0', max: '100' }),
        makeInputGroup('settMissedReentryFutSL', 'FUT SL %', 'number', { step: '0.01', min: '0', max: '100' }),
        makeInputGroup('settMissedReentryRR', 'RR', 'number', { step: '0.1', min: '0.1', max: '10' })
    ]);

    const advLateReclaimCard = makeCard('settings-late-reclaim', 'Late-Session Reclaim', [
        makeSwitchCard('settLateReclaimEnabled', 'Reclaim Override', '14:25-15:10 broad directional reclaim with tight scalp stops'),
        makeInputGroup('settLateReclaimStart', 'Start', 'time'),
        makeInputGroup('settLateReclaimEnd', 'End', 'time'),
        makeInputGroup('settLateReclaimCoreVotes', 'Basket Votes', 'number', { step: '1', min: '1', max: '4' }),
        makeInputGroup('settLateReclaimTotalVotes', 'Total Votes', 'number', { step: '1', min: '1', max: '4' }),
        makeInputGroup('settLateReclaimReclaimVotes', 'Reclaim Votes', 'number', { step: '1', min: '1', max: '4' }),
        makeInputGroup('settLateReclaimSkew', 'Max Skew Sec', 'number', { step: '30', min: '0', max: '3600' }),
        makeInputGroup('settLateReclaimCloseLocation', 'Close Location', 'number', { step: '0.05', min: '0', max: '1' }),
        makeInputGroup('settLateReclaimVolume', 'Volume Floor', 'number', { step: '0.05', min: '0', max: '100' }),
        makeInputGroup('settLateReclaimVolumeVotes', 'Volume Votes', 'number', { step: '1', min: '1', max: '4' }),
        makeInputGroup('settLateReclaimOptSL', 'OPT SL %', 'number', { step: '0.1', min: '0', max: '100' }),
        makeInputGroup('settLateReclaimFutSL', 'FUT SL %', 'number', { step: '0.01', min: '0', max: '100' }),
        makeInputGroup('settLateReclaimRR', 'RR', 'number', { step: '0.1', min: '0.1', max: '10' })
    ]);

    const advChoppyScalpCard = makeCard('settings-15m-choppy-scalp', '15m Choppy Scalp Guard', [
        makeSwitchCard('sett15mChoppyScalpEnabled', '15m Scalp Guard', 'Ranging 15m options need 1m/5m same-side confirmation'),
        makeInputGroup('sett15mChoppyScalpIntel', 'Min Intel', 'number', { step: '0.05', min: '0', max: '1' }),
        makeInputGroup('sett15mChoppyScalpVolume', 'Volume Floor', 'number', { step: '0.05', min: '0', max: '100' }),
        makeInputGroup('sett15mChoppyScalpSkew', 'Micro Max Skew Sec', 'number', { step: '30', min: '0', max: '3600' }),
        makeInputGroup('sett15mChoppyScalpClose', 'Close Location', 'number', { step: '0.05', min: '0', max: '1' }),
        makeInputGroup('sett15mChoppyScalpLockR', 'Profit Lock R', 'number', { step: '0.05', min: '0', max: '5' }),
        makeInputGroup('sett15mChoppyScalpPeakRs', 'Min Peak Rs', 'number', { step: '50', min: '0', max: '100000' }),
        makeInputGroup('settMidcap15mBufferSec', 'MIDCP Buffer Sec', 'number', { step: '1', min: '0', max: '300' }),
        makeInputGroup('settMidcap15mBufferScans', 'MIDCP Min Scans', 'number', { step: '1', min: '1', max: '20' }),
        makeInputGroup('settMidcap15mVolume', 'MIDCP Volume Floor', 'number', { step: '0.05', min: '0', max: '100' }),
        makeInputGroup('settMidcap15mIntel', 'MIDCP Intel Floor', 'number', { step: '0.05', min: '0', max: '1' })
    ]);

    const advDualLaneCard = makeCard('settings-dual-lane', 'Adaptive Dual Lane', [
        makeSwitchCard('settDualLaneEnabled', 'APEX Lane Enabled', 'Lane B uses APEX v5.5 confluence as independent entries'),
        makeInputGroup('settDualLaneGapBars', 'Same-Dir Gap Bars', 'number', { step: '1', min: '0', max: '20' }),
        makeInputGroup('settDualLaneMinA_R', 'Lane A Min R', 'number', { step: '0.05', min: '0', max: '5' }),
        makeInputGroup('settDualLaneSizeFraction', 'Lane B Size Fraction', 'number', { step: '0.05', min: '0.1', max: '1' }),
        makeInputGroup('settDualLaneExposureCap', 'Index Exposure Cap Rs', 'number', { step: '500', min: '0', max: '1000000' }),
        makeInputGroup('settDualLaneMinBConf', 'Lane B Min Conf', 'number', { step: '0.01', min: '0', max: '1' }),
        makeInputGroup('settDualLaneJaccard', 'Jaccard Alert', 'number', { step: '0.01', min: '0', max: '1' }),
        makeSwitchCard('settDualCloneReduction', 'Clone Reduction', 'Same index+TF dual engines within window keep first only'),
        makeInputGroup('settDualCloneWindowSec', 'Clone Window Sec', 'number', { step: '1', min: '0', max: '600' })
    ]);

    const advCascadeBookCard = makeCard('settings-cascade-book', 'Cascade Lots / Multi-TF / Mixed Book', [
        makeSwitchCard('settCascadeLotScale', 'Cascade OPT Lot Scale', 'Cascade multiplies OPT lots only (FUT stays 1×)'),
        makeInputGroup('settCascadeLotOptMin', 'OPT Mult Min', 'number', { step: '0.1', min: '1', max: '3' }),
        makeInputGroup('settCascadeLotOpt', 'OPT Mult Max', 'number', { step: '0.1', min: '1', max: '3' }),
        makeInputGroup('settCascadeLotFut', 'FUT Mult', 'number', { step: '0.1', min: '1', max: '3' }),
        makeSwitchCard('settHeavyTrendScale', 'Heavy-Trend OPT ×2', 'Non-cascade ≥2/4 index align → OPT 2× only'),
        makeInputGroup('settHeavyTrendOptMult', 'Heavy OPT Mult', 'number', { step: '0.1', min: '1', max: '2' }),
        makeInputGroup('settHeavyTrendMinAlign', 'Min Aligned Indices', 'number', { step: '1', min: '2', max: '4' }),
        makeInputGroup('settHeavyTrendVoteAge', 'Vote Max Age Sec', 'number', { step: '30', min: '30', max: '3600' }),
        makeSwitchCard('settCascadeMultiTf', 'Cascade Multi-TF Stack', 'Core 1m+5m+15m; midcap solo best-2 / dual 2+1'),
        makeInputGroup('settCascadePrimaryMaxTf', 'Core Primary Max TF', 'number', { step: '1', min: '1', max: '3' }),
        makeInputGroup('settCascadeDualAddon', 'Core Dual Addon Max', 'number', { step: '1', min: '0', max: '2' }),
        makeInputGroup('settCascadeMidSolo', 'Midcap Solo Max TF', 'number', { step: '1', min: '1', max: '3' }),
        makeInputGroup('settCascadeMidDualMain', 'Midcap Dual Main Max', 'number', { step: '1', min: '1', max: '3' }),
        makeInputGroup('settCascadeMidDualAddon', 'Midcap Dual Addon', 'number', { step: '1', min: '0', max: '2' }),
        makeInputGroup('settCascadeCoreBucket', 'Cascade CORE Bucket Cap', 'number', { step: '1', min: '3', max: '24' }),
        makeInputGroup('settCascadeMidBucket', 'Cascade MIDCAP Bucket Cap', 'number', { step: '1', min: '1', max: '6' }),
        makeInputGroup('settCascadeMaxPos', 'Cascade Max Concurrent', 'number', { step: '1', min: '6', max: '30' }),
        makeSwitchCard('settMixedBookEnabled', 'Mixed Book 1 FUT + OPT', 'Multi-index wave: sole FUT 1 lot, rest OPT'),
        makeInputGroup('settMixedBookMaxFut', 'Max FUT Slots', 'number', { step: '1', min: '0', max: '2' }),
        makeInputGroup('settMixedBookFutLots', 'FUT Lots', 'number', { step: '1', min: '1', max: '5' }),
        makeInputGroup('settMixedBookMaxOpt', 'Max OPT Slots', 'number', { step: '1', min: '1', max: '24' })
    ]);

    const advExecutionCard = makeCard('settings-execution', 'Execution Quality', [
        makeSwitchCard('settExecutionTouchQuote', 'Touch-Quote Execution', 'OFF keeps LTP/signal limit; ON anchors option limits to fresh bid/ask touch'),
        makeSwitchCard('settExecutionTouchQuoteShadow', 'Touch-Quote Shadow Compare', 'Record old-vs-touch would-fill quality while execution stays unchanged'),
        makeSwitchCard('settExecutionTouchQuoteStrict', 'Strict Touch Quote', 'If touch quote is stale/missing, block instead of falling back to signal/LTP anchor')
    ]);

    const makeSelectGroup = (id, label, choices) => {
        if (document.getElementById(id)) return findGroup(id);
        const group = document.createElement('div');
        group.className = 'setting-group';
        group.innerHTML = `<label>${label}</label><select id="${id}">${choices.map(([v, t]) => `<option value="${v}">${t}</option>`).join('')}</select>`;
        body.appendChild(group);
        return group;
    };
    const advS1Card = makeCard('settings-s1-engine', 'S1 Engine & Lane', S1_SETTINGS_FIELDS.map(([, id, label, kind, , extra]) => (
        kind === 'bool' ? makeSwitchCard(id, label)
            : kind === 'enum' ? makeSelectGroup(id, label, extra)
            : kind === 'time' ? makeInputGroup(id, label, 'time')
            : makeInputGroup(id, label, 'number', extra || {})
    )));
    const advUtsCard = makeCard('settings-uts-engine', 'UT Smart Exit Engine & Lane', UTS_SETTINGS_FIELDS.map(([, id, label, kind, , extra]) => (
        kind === 'bool' ? makeSwitchCard(id, label)
            : kind === 'enum' ? makeSelectGroup(id, label, extra)
            : kind === 'time' ? makeInputGroup(id, label, 'time')
            : makeInputGroup(id, label, 'number', extra || {})
    )));
    const advS12Card = makeCard('settings-s12-engine', 'S1+S2 Dual (Pine v5.4)', []);
    const advAutoStartCard = makeCard('settings-auto-start', 'Morning REAL Switch', AUTO_START_FIELDS.map(([, id, label, kind]) => (
        kind === 'bool' ? makeSwitchCard(id, label) : makeInputGroup(id, label, 'time', { step: '1' })
    )));
    advancedContent.append(advAutoStartCard, advS1Card, advUtsCard, advS12Card, advTradeCard, advTimingCard, advMomentumCard, advPeakExitCard, advMarketCard, advOneWayCascadeCard, advCascadeBookCard, advMissedReentryCard, advLateReclaimCard, advChoppyScalpCard, advDualLaneCard, advExecutionCard);
    content.append(generalContent, advancedContent, forensicsContent);


    body.innerHTML = '';
    body.append(tabsNav, content);

    const footer = document.createElement('div');
    footer.className = 'settings-footer';
    const cancelBtn = document.createElement('button');
    cancelBtn.className = 'btn-secondary';
    cancelBtn.type = 'button';
    cancelBtn.id = 'cancelSettings';
    cancelBtn.textContent = 'Cancel';
    cancelBtn.addEventListener('click', () => {
        modal.style.display = 'none';
    });
    if (saveBtn) footer.append(cancelBtn, saveBtn);
    dialog.appendChild(footer);

    if (closeBtn && !closeBtn.dataset.closeReady) {
        closeBtn.dataset.closeReady = '1';
        closeBtn.addEventListener('click', () => {
            modal.style.display = 'none';
        });
    }
    document.getElementById('settScannerEngine')?.addEventListener('change', () => {
        updateUtPresetHelp();
        syncUtsSignalTiming('settIntrabarSignals');
    });
    document.getElementById('settUtsAllowIntrabar')?.addEventListener('change', () => syncUtsSignalTiming('settUtsAllowIntrabar'));
    document.getElementById('settUtPreset')?.addEventListener('change', updateUtPresetHelp);
    syncSettingsScannerEngine(
        state.latestData?.config?.scanner_engine
        || document.getElementById('scannerEngineSelect')?.value
        || 'quad_prime'
    );
    updateUtPresetHelp();
}


function syncSettingsFromConfig(data) {
    refreshS12SettingsCard(data?.config || {});
    const modal = document.getElementById('settingsModal');
    // The dedicated execution control must always mirror backend truth.  General
    // settings stay frozen while their modal is open so the operator's edits are not
    // overwritten, but Auto is not a modal setting and must never retain stale state.
    const autoSwitch = document.getElementById('autoModeSwitch');
    if (autoSwitch && data.config) autoSwitch.checked = data.config.auto_mode === true;
    if (data.config && (!modal || modal.style.display !== 'flex')) {
        const cfg = data.config;
        const setVal = (id, val) => { const el = document.getElementById(id); if (el) el.value = val; };
        const setChecked = (id, val) => { const el = document.getElementById(id); if (el) el.checked = val === true; };
        setVal('settCapitalTotal', cfg.capital_total);
        setVal('settCapitalFut', cfg.capital_fut);
        setVal('settRiskFut', cfg.risk_fut_pct);
        setVal('settCapitalOpt', cfg.capital_opt);
        setVal('settRiskOpt', cfg.risk_opt_pct);
        setVal('settFutureSL', cfg.fut_sl);
        setVal('settOptionSL', cfg.opt_sl);
        setVal('settFutCost', cfg.fut_cost !== undefined ? cfg.fut_cost : 200);
        setVal('settOptCost', cfg.opt_cost !== undefined ? cfg.opt_cost : 80);
        setVal('settBacktestDays', cfg.backtest_days);
        setVal('settMaxTrades', cfg.max_trades_per_index !== undefined ? cfg.max_trades_per_index : 5);
        setVal('settMaxLosses', cfg.max_consecutive_losses !== undefined ? cfg.max_consecutive_losses : 3);
        setVal('settIndexCooldown', cfg.index_cooldown_minutes !== undefined ? cfg.index_cooldown_minutes : 4);
        if (cfg.lots) {
            setVal('settLotsNifty', cfg.lots.NIFTY || 1);
            setVal('settLotsBN', cfg.lots.BANKNIFTY || 1);
            setVal('settLotsSensex', cfg.lots.SENSEX || 1);
            setVal('settLotsMidcap', cfg.lots.MIDCPNIFTY || 1);
        }
        if (cfg.lots_fut) {
            setVal('settLotsFutNifty', cfg.lots_fut.NIFTY || 1);
            setVal('settLotsFutBN', cfg.lots_fut.BANKNIFTY || 1);
            setVal('settLotsFutSensex', cfg.lots_fut.SENSEX || 1);
            setVal('settLotsFutMidcap', cfg.lots_fut.MIDCPNIFTY || 1);
        }
        const shadowEl = document.getElementById('settFuturesShadow');
        if (shadowEl) shadowEl.checked = cfg.futures_shadow_enabled === true;
        const hedgeEl = document.getElementById('settPullbackHedge');
        if (hedgeEl) hedgeEl.checked = cfg.pullback_hedge_enabled === true;
        const soEl = document.getElementById('settStopOutReentry');
        if (soEl) soEl.checked = cfg.stop_out_reentry_enabled === true;
        const tfEl = document.getElementById('settPerTfGrading');
        if (tfEl) tfEl.checked = cfg.per_timeframe_grading_enabled === true;
        setChecked('settEngineLab', cfg.engine_lab_enabled !== undefined ? cfg.engine_lab_enabled : true);
        setChecked('settEngineLabRealOnly', cfg.engine_lab_real_mode_only === true);
        const signalModeEl = document.getElementById('settIntrabarSignals');
        if (signalModeEl) {
            signalModeEl.checked = (cfg.signal_fire_mode || 'INTRACANDLE') === 'INTRACANDLE';
            signalModeEl.dispatchEvent(new Event('change'));
        }
        setChecked('settLeanRouting', cfg.lean_routing_to_options);
        setChecked('settPriceActionModule', cfg.price_action_module_enabled);
        setChecked('settPriceActionBlock', cfg.price_action_block_adverse);
        setVal('settPriceActionThreshold', cfg.price_action_adverse_threshold !== undefined ? cfg.price_action_adverse_threshold : -0.4);
        setChecked('settPriceActionRelief', cfg.price_action_reversal_relief_enabled !== undefined ? cfg.price_action_reversal_relief_enabled : true);
        setChecked('settRegimeVeto', cfg.regime_veto_enabled);
        setVal('settRegimeVetoAdx', cfg.regime_veto_adx_floor !== undefined ? cfg.regime_veto_adx_floor : 18);
        setChecked('settRegimeErPromotion', cfg.regime_early_trend_er_promotion_enabled !== undefined ? cfg.regime_early_trend_er_promotion_enabled : true);
        setVal('settRegimeErPeriod', cfg.regime_early_trend_er_period !== undefined ? cfg.regime_early_trend_er_period : 10);
        setVal('settRegimeErThreshold', cfg.regime_early_trend_er_threshold !== undefined ? cfg.regime_early_trend_er_threshold : 0.60);
        setVal('settRegimeErAdxCeiling', cfg.regime_early_trend_er_adx_ceiling !== undefined ? cfg.regime_early_trend_er_adx_ceiling : 20.0);
        setVal('settRegimeErRangeConf', cfg.regime_early_trend_er_range_day_min_confidence !== undefined ? cfg.regime_early_trend_er_range_day_min_confidence : 0.60);
        setChecked('settFutLossCut', cfg.fut_early_loss_cut_enabled);
        setVal('settFutLossCutR', cfg.fut_loss_cut_r !== undefined ? cfg.fut_loss_cut_r : 0.6);
        setChecked('settPowerMoveOverride', cfg.power_move_override_enabled);
        setChecked('settSpikeBypass', cfg.power_move_spike_bypass_15m);
        setVal('settSpikeRangeAtr', cfg.power_move_spike_range_atr_mult !== undefined ? cfg.power_move_spike_range_atr_mult : 2.5);
        setChecked('settTrendContinuation', cfg.live_5min_trend_continuation_enabled);
        setVal('settTrendContinuationAdx', cfg.live_5min_trend_continuation_adx !== undefined ? cfg.live_5min_trend_continuation_adx : 28);
        setVal('settQualityVoteScore', cfg.quality_hybrid_min_vote_score !== undefined ? cfg.quality_hybrid_min_vote_score : 3.0);
        setVal('settQualityBroadAgreement', cfg.quality_hybrid_broad_agreement !== undefined ? cfg.quality_hybrid_broad_agreement : 4);
        setVal('settQualityChopAgreement', cfg.quality_hybrid_chop_min_agreement !== undefined ? cfg.quality_hybrid_chop_min_agreement : 4);
        setVal('settQualityTrendAdx', cfg.quality_hybrid_trend_adx !== undefined ? cfg.quality_hybrid_trend_adx : 22.0);
        setVal('settQualityTrendEr', cfg.quality_hybrid_trend_er !== undefined ? cfg.quality_hybrid_trend_er : 0.50);
        setVal('settQualityThrustVol', cfg.quality_hybrid_thrust_volume_ratio !== undefined ? cfg.quality_hybrid_thrust_volume_ratio : 1.6);
        setVal('settQualityThrustRange', cfg.quality_hybrid_thrust_range_atr !== undefined ? cfg.quality_hybrid_thrust_range_atr : 1.05);
        setVal('settQualityThrustClose', cfg.quality_hybrid_thrust_close_loc !== undefined ? cfg.quality_hybrid_thrust_close_loc : 0.60);
        setChecked('settConfidenceCalibration', cfg.ut_confidence_calibration_enabled);
        setVal('settCalibrationMinSamples', cfg.ut_confidence_calibration_min_samples !== undefined ? cfg.ut_confidence_calibration_min_samples : 200);
        setChecked('settExecutionTouchQuote', cfg.execution_use_touch_quote === true);
        setChecked('settExecutionTouchQuoteShadow', cfg.execution_touch_quote_shadow_enabled !== undefined ? cfg.execution_touch_quote_shadow_enabled : true);
        setChecked('settExecutionTouchQuoteStrict', cfg.execution_touch_quote_strict === true);
        setChecked('settVolumeSpikeEntry', cfg.volume_spike_entry_enabled);
        setChecked('settOneMinScalping', cfg.one_min_scalping_enabled !== undefined ? cfg.one_min_scalping_enabled : true);
        setVal('settVolumeSpikeVolRatio', cfg.volume_spike_entry_min_volume_ratio !== undefined ? cfg.volume_spike_entry_min_volume_ratio : 2.5);
        setVal('settVolumeSpikeRangeAtr', cfg.volume_spike_entry_min_range_atr_ratio !== undefined ? cfg.volume_spike_entry_min_range_atr_ratio : 1.5);
        setVal('settVolumeSpikeCloseLocation', cfg.volume_spike_entry_min_close_location !== undefined ? cfg.volume_spike_entry_min_close_location : 0.65);
        setVal('settVolumeSpikeFutSL', cfg.volume_spike_entry_futures_sl_pct !== undefined ? cfg.volume_spike_entry_futures_sl_pct : 0.05);
        setVal('settVolumeSpikeOptSL', cfg.volume_spike_entry_options_sl_pct !== undefined ? cfg.volume_spike_entry_options_sl_pct : 5.0);
        setVal('settVolumeSpikeStabilization', cfg.volume_spike_entry_stabilization_seconds !== undefined ? cfg.volume_spike_entry_stabilization_seconds : 3.0);
        setChecked('settVolumeSpikeDepthConfirm', cfg.volume_spike_depth_confirm_enabled !== undefined ? cfg.volume_spike_depth_confirm_enabled : true);
        setVal('settVolumeSpikeOfrBull', cfg.volume_spike_depth_ofr_bullish !== undefined ? cfg.volume_spike_depth_ofr_bullish : 1.08);
        setVal('settVolumeSpikeOfrBear', cfg.volume_spike_depth_ofr_bearish !== undefined ? cfg.volume_spike_depth_ofr_bearish : 0.92);
        setChecked('settStabilizationMomentumOverride', cfg.stabilization_momentum_override_enabled !== undefined ? cfg.stabilization_momentum_override_enabled : true);
        setVal('settLiveStab5', cfg.live_signal_stabilization_seconds_5min !== undefined ? cfg.live_signal_stabilization_seconds_5min : 30.0);
        setVal('settLiveStab15', cfg.live_signal_stabilization_seconds_15min !== undefined ? cfg.live_signal_stabilization_seconds_15min : 30.0);
        setChecked('settLive15MomentumOverride', cfg.live_15min_momentum_override_enabled !== undefined ? cfg.live_15min_momentum_override_enabled : true);
        setVal('settLive15MomentumMinConf', cfg.live_15min_momentum_min_confidence !== undefined ? cfg.live_15min_momentum_min_confidence : 0.20);
        setVal('settLive15MomentumVolRatio', cfg.live_15min_momentum_volume_ratio !== undefined ? cfg.live_15min_momentum_volume_ratio : 2.0);
        setVal('settLive15MomentumRangeAtr', cfg.live_15min_momentum_range_atr_ratio !== undefined ? cfg.live_15min_momentum_range_atr_ratio : 1.25);
        setVal('settLive15MomentumCloseLocation', cfg.live_15min_momentum_close_location !== undefined ? cfg.live_15min_momentum_close_location : 0.65);
        setVal('settLive15MomentumAdx', cfg.live_15min_momentum_adx_threshold !== undefined ? cfg.live_15min_momentum_adx_threshold : 24.0);
        setVal('settLive15MomentumIntel', cfg.live_15min_momentum_intel_threshold !== undefined ? cfg.live_15min_momentum_intel_threshold : 0.65);
        setChecked('settReversalEntryRelief', cfg.reversal_entry_relief_enabled !== undefined ? cfg.reversal_entry_relief_enabled : true);
        setVal('settReversalMinConf', cfg.reversal_entry_min_confidence !== undefined ? cfg.reversal_entry_min_confidence : 0.20);
        setVal('settReversalRangeAtr', cfg.reversal_entry_thrust_min_range_atr_ratio !== undefined ? cfg.reversal_entry_thrust_min_range_atr_ratio : 1.20);
        setVal('settReversalCloseLocation', cfg.reversal_entry_thrust_min_close_location !== undefined ? cfg.reversal_entry_thrust_min_close_location : 0.62);
        setVal('settReversalVolumeRatio', cfg.reversal_entry_thrust_min_volume_ratio !== undefined ? cfg.reversal_entry_thrust_min_volume_ratio : 1.20);
        setVal('settReversalVwapDev', cfg.reversal_entry_vwap_min_dev_pct !== undefined ? cfg.reversal_entry_vwap_min_dev_pct : 0.03);
        setChecked('settDirectionalCensusRelief', cfg.directional_census_reversal_relief_enabled !== undefined ? cfg.directional_census_reversal_relief_enabled : true);
        setVal('settDirectionalCensusMinConf', cfg.directional_census_reversal_min_confidence !== undefined ? cfg.directional_census_reversal_min_confidence : 0.20);
        setChecked('settLateSessionChoppyGuard', cfg.late_session_choppy_entry_guard_enabled !== undefined ? cfg.late_session_choppy_entry_guard_enabled : true);
        setVal('settLateSessionRunway', cfg.late_session_choppy_min_runway_minutes !== undefined ? cfg.late_session_choppy_min_runway_minutes : 30.0);
        setChecked('settPeakThreatExit', cfg.peak_threat_exit_enabled !== undefined ? cfg.peak_threat_exit_enabled : true);
        setVal('settPeakThreatMinPeak', cfg.peak_threat_exit_min_peak_rs !== undefined ? cfg.peak_threat_exit_min_peak_rs : 400.0);
        setChecked('settBreakevenStop', cfg.breakeven_stop_enabled !== undefined ? cfg.breakeven_stop_enabled : true);
        setVal('settBreakevenStopArm', cfg.breakeven_stop_arm_rs !== undefined ? cfg.breakeven_stop_arm_rs : 100.0);
        setVal('settBreakevenStopFloor', cfg.breakeven_stop_floor_rs !== undefined ? cfg.breakeven_stop_floor_rs : 80.0);
        setChecked('settPeakTrail', cfg.peak_trail_enabled !== undefined ? cfg.peak_trail_enabled : false);
        setVal('settPeakTrailAbove', cfg.peak_trail_above_rs !== undefined ? cfg.peak_trail_above_rs : 600.0);
        setVal('settPeakTrailGiveback', cfg.peak_trail_giveback_pct !== undefined ? cfg.peak_trail_giveback_pct : 0.40);
        setChecked('settPeakGivebackCap', cfg.peak_giveback_cap_enabled !== undefined ? cfg.peak_giveback_cap_enabled : false);
        setVal('settPeakGivebackCapArm', cfg.peak_giveback_cap_arm_rs !== undefined ? cfg.peak_giveback_cap_arm_rs : 100.0);
        setVal('settPeakGivebackCapRs', cfg.peak_giveback_cap_rs !== undefined ? cfg.peak_giveback_cap_rs : 200.0);
        setVal('settPeakGivebackCapPct', cfg.peak_giveback_cap_pct !== undefined ? cfg.peak_giveback_cap_pct : 0.25);
        setVal('settPeakGivebackCapMaxPeak', cfg.peak_giveback_cap_max_peak_rs !== undefined ? cfg.peak_giveback_cap_max_peak_rs : 1500.0);
        setChecked('settBacktestLiveExits', cfg.backtest_live_exits_enabled !== undefined ? cfg.backtest_live_exits_enabled : true);
        setVal('settPeakThreatMinPeakR', cfg.peak_threat_exit_min_peak_r !== undefined ? cfg.peak_threat_exit_min_peak_r : 0.35);
        setVal('settPeakThreatRetention', cfg.peak_threat_exit_min_retention !== undefined ? cfg.peak_threat_exit_min_retention : 0.72);
        setVal('settPeakThreatScore', cfg.peak_threat_exit_min_score !== undefined ? cfg.peak_threat_exit_min_score : 5);
        setVal('settPeakThreatSevereScore', cfg.peak_threat_exit_severe_score !== undefined ? cfg.peak_threat_exit_severe_score : 4);
        setVal('settPeakThreatIntel', cfg.peak_threat_exit_adverse_intel !== undefined ? cfg.peak_threat_exit_adverse_intel : 0.55);
        setVal('settPeakThreatWeakVol', cfg.peak_threat_exit_weak_volume_ratio !== undefined ? cfg.peak_threat_exit_weak_volume_ratio : 1.05);
        setChecked('settPeakThreatLossExit', cfg.peak_threat_exit_loss_enabled !== undefined ? cfg.peak_threat_exit_loss_enabled : true);
        setVal('settPeakThreatLossScore', cfg.peak_threat_exit_loss_min_score !== undefined ? cfg.peak_threat_exit_loss_min_score : 6);
        setVal('settPeakThreatLossMaxR', cfg.peak_threat_exit_loss_max_loss_r !== undefined ? cfg.peak_threat_exit_loss_max_loss_r : 0.35);
        setVal('settOptionMajorWinTrendCap', cfg.option_major_win_trend_lock_ratio_cap !== undefined ? cfg.option_major_win_trend_lock_ratio_cap : 0.95);
        setChecked('settMarketWideReversal', cfg.market_wide_reversal_enabled !== undefined ? cfg.market_wide_reversal_enabled : true);
        setVal('settMarketWideCoreVotes', cfg.market_wide_reversal_core_votes !== undefined ? cfg.market_wide_reversal_core_votes : 2);
        setVal('settMarketWideTotalVotes', cfg.market_wide_reversal_total_votes !== undefined ? cfg.market_wide_reversal_total_votes : 2);
        setVal('settMarketWideRangeAtr', cfg.market_wide_reversal_min_range_atr_ratio !== undefined ? cfg.market_wide_reversal_min_range_atr_ratio : 1.10);
        setVal('settMarketWideRangeAtr5', cfg.market_wide_reversal_min_range_atr_ratio_5min !== undefined ? cfg.market_wide_reversal_min_range_atr_ratio_5min : 1.10);
        setVal('settMarketWideRangeAtr15', cfg.market_wide_reversal_min_range_atr_ratio_15min !== undefined ? cfg.market_wide_reversal_min_range_atr_ratio_15min : 0.50);
        setVal('settMarketWideMidcapRangeAtr5', cfg.market_wide_reversal_midcap_min_range_atr_ratio_5min !== undefined ? cfg.market_wide_reversal_midcap_min_range_atr_ratio_5min : 0.65);
        setVal('settMarketWideMidcapRangeAtr15', cfg.market_wide_reversal_midcap_min_range_atr_ratio_15min !== undefined ? cfg.market_wide_reversal_midcap_min_range_atr_ratio_15min : 0.45);
        setVal('settMarketWideCloseLocation', cfg.market_wide_reversal_min_close_location !== undefined ? cfg.market_wide_reversal_min_close_location : 0.65);
        setVal('settMarketWideSkew5', cfg.market_wide_reversal_max_skew_seconds_5min !== undefined ? cfg.market_wide_reversal_max_skew_seconds_5min : 180.0);
        setVal('settMarketWideSkew15', cfg.market_wide_reversal_max_skew_seconds_15min !== undefined ? cfg.market_wide_reversal_max_skew_seconds_15min : 900.0);
        setChecked('settOneWayCascadeEnabled', cfg.one_way_cascade_enabled !== undefined ? cfg.one_way_cascade_enabled : true);
        setVal('settOneWayCascadeCoreVotes', cfg.one_way_cascade_core_votes !== undefined ? cfg.one_way_cascade_core_votes : 3);
        setVal('settOneWayCascadeTotalVotes', cfg.one_way_cascade_total_votes !== undefined ? cfg.one_way_cascade_total_votes : 3);
        setVal('settOneWayCascadeVolumeVotes', cfg.one_way_cascade_volume_votes !== undefined ? cfg.one_way_cascade_volume_votes : 3);
        setVal('settOneWayCascadeVolume', cfg.one_way_cascade_min_volume_ratio !== undefined ? cfg.one_way_cascade_min_volume_ratio : 1.40);
        setVal('settOneWayCascadeRange1', cfg.one_way_cascade_min_range_atr_ratio_1min !== undefined ? cfg.one_way_cascade_min_range_atr_ratio_1min : 1.50);
        setVal('settOneWayCascadeRange5', cfg.one_way_cascade_min_range_atr_ratio_5min !== undefined ? cfg.one_way_cascade_min_range_atr_ratio_5min : 1.20);
        setVal('settOneWayCascadeRange15', cfg.one_way_cascade_min_range_atr_ratio_15min !== undefined ? cfg.one_way_cascade_min_range_atr_ratio_15min : 0.75);
        setVal('settOneWayCascadeClose', cfg.one_way_cascade_min_close_location !== undefined ? cfg.one_way_cascade_min_close_location : 0.68);
        setVal('settOneWayCascadeRunnerLock', cfg.one_way_cascade_runner_lock_pct !== undefined ? cfg.one_way_cascade_runner_lock_pct : 0.85);
        setVal('settOneWayCascadeOptSL', cfg.one_way_cascade_options_sl_pct !== undefined ? cfg.one_way_cascade_options_sl_pct : 5.0);
        setVal('settOneWayCascadeFutSL', cfg.one_way_cascade_futures_sl_pct !== undefined ? cfg.one_way_cascade_futures_sl_pct : 0.05);
        setVal('settOneWayCascadeRR', cfg.one_way_cascade_rr !== undefined ? cfg.one_way_cascade_rr : 1.5);
        setChecked('settMissedReentryEnabled', cfg.missed_signal_reentry_enabled !== undefined ? cfg.missed_signal_reentry_enabled : true);
        setChecked('settMissedReentryTrend', cfg.missed_signal_reentry_allow_trend_context !== undefined ? cfg.missed_signal_reentry_allow_trend_context : true);
        setChecked('settMissedReentryPreferOptions', cfg.missed_signal_reentry_prefer_options !== undefined ? cfg.missed_signal_reentry_prefer_options : true);
        setVal('settMissedReentryMaxAge', cfg.missed_signal_reentry_max_age_minutes !== undefined ? cfg.missed_signal_reentry_max_age_minutes : 6.0);
        setVal('settMissedReentryMinAge', cfg.missed_signal_reentry_min_age_seconds !== undefined ? cfg.missed_signal_reentry_min_age_seconds : 20.0);
        setVal('settMissedReentryChaseAtr', cfg.missed_signal_reentry_max_chase_atr !== undefined ? cfg.missed_signal_reentry_max_chase_atr : 0.75);
        setVal('settMissedReentryConf', cfg.missed_signal_reentry_min_confidence !== undefined ? cfg.missed_signal_reentry_min_confidence : 0.55);
        setVal('settMissedReentryIntel', cfg.missed_signal_reentry_min_intel_score !== undefined ? cfg.missed_signal_reentry_min_intel_score : 0.20);
        setVal('settMissedReentryAdx', cfg.missed_signal_reentry_min_trend_adx !== undefined ? cfg.missed_signal_reentry_min_trend_adx : 24.0);
        setVal('settMissedReentryRunway', cfg.missed_signal_reentry_min_remaining_minutes !== undefined ? cfg.missed_signal_reentry_min_remaining_minutes : 8.0);
        setVal('settMissedReentryOptSL', cfg.missed_signal_reentry_options_sl_pct !== undefined ? cfg.missed_signal_reentry_options_sl_pct : 5.0);
        setVal('settMissedReentryFutSL', cfg.missed_signal_reentry_futures_sl_pct !== undefined ? cfg.missed_signal_reentry_futures_sl_pct : 0.05);
        setVal('settMissedReentryRR', cfg.missed_signal_reentry_rr !== undefined ? cfg.missed_signal_reentry_rr : 1.5);
        setChecked('settLateReclaimEnabled', cfg.late_session_reclaim_override_enabled !== undefined ? cfg.late_session_reclaim_override_enabled : true);
        setVal('settLateReclaimStart', cfg.late_session_reclaim_start || '14:25');
        setVal('settLateReclaimEnd', cfg.late_session_reclaim_end || '15:10');
        setVal('settLateReclaimCoreVotes', cfg.late_session_reclaim_core_votes !== undefined ? cfg.late_session_reclaim_core_votes : 3);
        setVal('settLateReclaimTotalVotes', cfg.late_session_reclaim_total_votes !== undefined ? cfg.late_session_reclaim_total_votes : 3);
        setVal('settLateReclaimReclaimVotes', cfg.late_session_reclaim_reclaim_votes !== undefined ? cfg.late_session_reclaim_reclaim_votes : 2);
        setVal('settLateReclaimSkew', cfg.late_session_reclaim_max_skew_seconds !== undefined ? cfg.late_session_reclaim_max_skew_seconds : 240.0);
        setVal('settLateReclaimCloseLocation', cfg.late_session_reclaim_min_close_location !== undefined ? cfg.late_session_reclaim_min_close_location : 0.55);
        setVal('settLateReclaimVolume', cfg.late_session_reclaim_min_volume_ratio !== undefined ? cfg.late_session_reclaim_min_volume_ratio : 1.10);
        setVal('settLateReclaimVolumeVotes', cfg.late_session_reclaim_volume_votes !== undefined ? cfg.late_session_reclaim_volume_votes : 3);
        setVal('settLateReclaimOptSL', cfg.late_session_reclaim_options_sl_pct !== undefined ? cfg.late_session_reclaim_options_sl_pct : 5.0);
        setVal('settLateReclaimFutSL', cfg.late_session_reclaim_futures_sl_pct !== undefined ? cfg.late_session_reclaim_futures_sl_pct : 0.05);
        setVal('settLateReclaimRR', cfg.late_session_reclaim_rr !== undefined ? cfg.late_session_reclaim_rr : 1.5);
        setChecked('sett15mChoppyScalpEnabled', cfg.fifteen_min_choppy_scalp_guard_enabled !== undefined ? cfg.fifteen_min_choppy_scalp_guard_enabled : true);
        setVal('sett15mChoppyScalpIntel', cfg.fifteen_min_choppy_scalp_min_intel_score !== undefined ? cfg.fifteen_min_choppy_scalp_min_intel_score : 0.20);
        setVal('sett15mChoppyScalpVolume', cfg.fifteen_min_choppy_scalp_min_volume_ratio !== undefined ? cfg.fifteen_min_choppy_scalp_min_volume_ratio : 1.05);
        setVal('sett15mChoppyScalpSkew', cfg.fifteen_min_choppy_scalp_micro_max_skew_seconds !== undefined ? cfg.fifteen_min_choppy_scalp_micro_max_skew_seconds : 240.0);
        setVal('sett15mChoppyScalpClose', cfg.fifteen_min_choppy_scalp_min_close_location !== undefined ? cfg.fifteen_min_choppy_scalp_min_close_location : 0.55);
        setVal('sett15mChoppyScalpLockR', cfg.fifteen_min_choppy_scalp_profit_lock_r_multiple !== undefined ? cfg.fifteen_min_choppy_scalp_profit_lock_r_multiple : 0.20);
        setVal('sett15mChoppyScalpPeakRs', cfg.fifteen_min_choppy_scalp_min_peak_rs !== undefined ? cfg.fifteen_min_choppy_scalp_min_peak_rs : 250.0);
        setVal('settMidcap15mBufferSec', cfg.midcap_15min_risky_stabilization_seconds !== undefined ? cfg.midcap_15min_risky_stabilization_seconds : 60.0);
        setVal('settMidcap15mBufferScans', cfg.midcap_15min_risky_stabilization_min_scans !== undefined ? cfg.midcap_15min_risky_stabilization_min_scans : 3);
        setVal('settMidcap15mVolume', cfg.midcap_15min_risky_stabilization_min_volume_ratio !== undefined ? cfg.midcap_15min_risky_stabilization_min_volume_ratio : 1.05);
        setVal('settMidcap15mIntel', cfg.midcap_15min_risky_stabilization_min_intel_score !== undefined ? cfg.midcap_15min_risky_stabilization_min_intel_score : 0.25);
        setChecked('settDualLaneEnabled', cfg.dual_lane_enabled !== undefined ? cfg.dual_lane_enabled : true);
        AUTO_START_FIELDS.forEach(([key, id, , kind, fallback]) => {
            const value = cfg[key] !== undefined && cfg[key] !== null ? cfg[key] : fallback;
            if (kind === 'bool') setChecked(id, value === true);
            else setVal(id, value);
        });
        S1_SETTINGS_FIELDS.forEach(([key, id, , kind, fallback]) => {
            const value = cfg[key] !== undefined && cfg[key] !== null ? cfg[key] : fallback;
            if (kind === 'bool') setChecked(id, value === true);
            else setVal(id, value);
        });
        UTS_SETTINGS_FIELDS.forEach(([key, id, , kind, fallback]) => {
            const value = cfg[key] !== undefined && cfg[key] !== null ? cfg[key] : fallback;
            if (kind === 'bool') setChecked(id, value === true);
            else setVal(id, value);
        });
        S12_SETTINGS_FIELDS.forEach(([key, id, , kind, fallback]) => {
            const value = cfg[key] !== undefined && cfg[key] !== null ? cfg[key] : fallback;
            if (kind === 'bool') setChecked(id, value === true);
            else setVal(id, value);
        });
        if (['ut_smart', 'ut_smart_s1'].includes(document.getElementById('settScannerEngine')?.value)) {
            // Display the effective mode of older saved configurations.
            const main = document.getElementById('settIntrabarSignals');
            const uts = document.getElementById('settUtsAllowIntrabar');
            if (main && uts) main.checked = main.checked && uts.checked;
            syncUtsSignalTiming('settIntrabarSignals');
        }
        setVal('settDualLaneGapBars', cfg.dual_lane_same_dir_min_bars_gap !== undefined ? cfg.dual_lane_same_dir_min_bars_gap : 3);
        setVal('settDualLaneMinA_R', cfg.dual_lane_same_dir_min_lane_a_r !== undefined ? cfg.dual_lane_same_dir_min_lane_a_r : 0.50);
        setVal('settDualLaneSizeFraction', cfg.dual_lane_size_fraction !== undefined ? cfg.dual_lane_size_fraction : 0.65);
        setVal('settDualLaneExposureCap', cfg.dual_lane_max_index_exposure_rs !== undefined ? cfg.dual_lane_max_index_exposure_rs : 12000.0);
        setVal('settDualLaneMinBConf', cfg.dual_lane_min_lane_b_confidence !== undefined ? cfg.dual_lane_min_lane_b_confidence : 0.70);
        setVal('settDualLaneJaccard', cfg.dual_lane_jaccard_alert_threshold !== undefined ? cfg.dual_lane_jaccard_alert_threshold : 0.30);
        setChecked('settDualCloneReduction', cfg.dual_clone_reduction_enabled !== undefined ? cfg.dual_clone_reduction_enabled : true);
        setVal('settDualCloneWindowSec', cfg.dual_clone_reduction_window_seconds !== undefined ? cfg.dual_clone_reduction_window_seconds : 60);
        setChecked('settCascadeLotScale', cfg.cascade_lot_scale_enabled !== undefined ? cfg.cascade_lot_scale_enabled : true);
        setVal('settCascadeLotOptMin', cfg.cascade_lot_multiplier_opt_min !== undefined ? cfg.cascade_lot_multiplier_opt_min : 2.0);
        setVal('settCascadeLotOpt', cfg.cascade_lot_multiplier_opt !== undefined ? cfg.cascade_lot_multiplier_opt : 3.0);
        setVal('settCascadeLotFut', cfg.cascade_lot_multiplier_fut !== undefined ? cfg.cascade_lot_multiplier_fut : 1.0);
        setChecked('settHeavyTrendScale', cfg.heavy_trend_lot_scale_enabled !== undefined ? cfg.heavy_trend_lot_scale_enabled : true);
        setVal('settHeavyTrendOptMult', cfg.heavy_trend_lot_multiplier_opt !== undefined ? cfg.heavy_trend_lot_multiplier_opt : 2.0);
        setVal('settHeavyTrendMinAlign', cfg.heavy_trend_min_aligned_indices !== undefined ? cfg.heavy_trend_min_aligned_indices : 2);
        setVal('settHeavyTrendVoteAge', cfg.heavy_trend_vote_max_age_seconds !== undefined ? cfg.heavy_trend_vote_max_age_seconds : 600);
        setChecked('settCascadeMultiTf', cfg.cascade_multi_tf_stack_enabled !== undefined ? cfg.cascade_multi_tf_stack_enabled : true);
        setVal('settCascadePrimaryMaxTf', cfg.cascade_primary_max_tf !== undefined ? cfg.cascade_primary_max_tf : 3);
        setVal('settCascadeDualAddon', cfg.cascade_dual_addon_max !== undefined ? cfg.cascade_dual_addon_max : 2);
        setVal('settCascadeMidSolo', cfg.cascade_midcap_solo_max_tf !== undefined ? cfg.cascade_midcap_solo_max_tf : 2);
        setVal('settCascadeMidDualMain', cfg.cascade_midcap_dual_main_max !== undefined ? cfg.cascade_midcap_dual_main_max : 2);
        setVal('settCascadeMidDualAddon', cfg.cascade_midcap_dual_addon_max !== undefined ? cfg.cascade_midcap_dual_addon_max : 1);
        setVal('settCascadeCoreBucket', cfg.cascade_core_bucket_max_positions !== undefined ? cfg.cascade_core_bucket_max_positions : 12);
        setVal('settCascadeMidBucket', cfg.cascade_midcap_bucket_max_positions !== undefined ? cfg.cascade_midcap_bucket_max_positions : 3);
        setVal('settCascadeMaxPos', cfg.cascade_max_concurrent_positions !== undefined ? cfg.cascade_max_concurrent_positions : 18);
        setChecked('settMixedBookEnabled', cfg.multi_signal_mixed_book_enabled !== undefined ? cfg.multi_signal_mixed_book_enabled : true);
        setVal('settMixedBookMaxFut', cfg.multi_signal_max_fut !== undefined ? cfg.multi_signal_max_fut : 1);
        setVal('settMixedBookFutLots', cfg.multi_signal_fut_lots !== undefined ? cfg.multi_signal_fut_lots : 1);
        setVal('settMixedBookMaxOpt', cfg.multi_signal_max_opt_instruments !== undefined ? cfg.multi_signal_max_opt_instruments : 18);
        setVal('settNoEntryAfter', cfg.ut_no_entry_after || '14:50');
        setVal('sett5mNoEntryAfter', cfg.ut_5min_no_entry_after || '15:10');
        setVal('sett1mNoEntryAfter', cfg.ut_1min_no_entry_after || '15:14');
        setVal('settForceExitTime', cfg.ut_force_exit_time || '15:22');
        setVal('settPowerMoveCutoff', cfg.power_move_late_hard_cutoff || '15:15');
        
        const prefEl = document.getElementById('settInstPref');
        if (prefEl) prefEl.value = cfg.inst_pref;
        
        const strikeEl = document.getElementById('settStrikeSelection');
        if (strikeEl) strikeEl.value = cfg.strike_selection || 'BOTH';
        
        const gradeEl = document.getElementById('settGradePreference');
        if (gradeEl) gradeEl.value = cfg.grade_preference || 'auto';
        
        const presetEl = document.getElementById('settUtPreset');
        if (presetEl) presetEl.value = cfg.ut_preset || 'AGGRESSIVE';
        syncSettingsScannerEngine(cfg.scanner_engine || 'quad_prime');
        updateUtPresetHelp();

        const tfPolicyEl = document.getElementById('topTimeframePolicy');
        if (tfPolicyEl) tfPolicyEl.value = cfg.ut_timeframe_entry_policy || 'INCLUDE_5MIN';

        // Sync Index Checkboxes
        if (cfg.active_indices) {
            const nifty = document.getElementById('checkNifty');
            if (nifty) nifty.checked = cfg.active_indices.includes('NIFTY');
            const bn = document.getElementById('checkBN');
            if (bn) bn.checked = cfg.active_indices.includes('BANKNIFTY');
            const sensex = document.getElementById('checkSensex');
            if (sensex) sensex.checked = cfg.active_indices.includes('SENSEX');
            const midcap = document.getElementById('checkMidcap');
            if (midcap) midcap.checked = cfg.active_indices.includes('MIDCPNIFTY');
        }

        // Sync 3-Way Mode Control
        if (data.mode) {
            if (window.syncModeControls) {
                window.syncModeControls(data.mode);
            } else {
                document.querySelectorAll('.mode-opt').forEach(btn => {
                    btn.classList.toggle('active', btn.dataset.mode === data.mode);
                });
            }
        }
    }
}

function setupSettingsEventListeners() {
    const settingsBtn = document.getElementById('settingsBtn');
    if (settingsBtn) {
        settingsBtn.addEventListener('click', () => {
            syncSettingsScannerEngine(
                state.latestData?.config?.scanner_engine
                || document.getElementById('scannerEngineSelect')?.value
                || 'quad_prime'
            );
            document.getElementById('settingsModal').style.display = 'flex';
        });
    }

    const saveBtn = document.getElementById('saveSettings');
    attachSettingsSaveHandler(saveBtn);
    const headerSaveBtn = document.getElementById('saveSettingsTop');
    attachSettingsSaveHandler(headerSaveBtn);

    const resetBtn = document.getElementById('resetCacheBtn');
    if (resetBtn) resetBtn.addEventListener('click', resetCache);
}

function saveSettings() {
    const numberVal = (id, fallback) => {
        const raw = document.getElementById(id)?.value;
        const value = parseFloat(raw);
        return Number.isFinite(value) ? value : fallback;
    };
    const intVal = (id, fallback) => {
        const raw = document.getElementById(id)?.value;
        const value = parseInt(raw);
        return Number.isFinite(value) ? value : fallback;
    };
    const timeVal = (id, fallback) => document.getElementById(id)?.value || fallback;
    const checkedVal = (id, fallback = false) => {
        const el = document.getElementById(id);
        return el ? el.checked === true : fallback;
    };
    const capital_fut = parseFloat(document.getElementById('settCapitalFut')?.value) || 250000;
    const risk_fut_pct = parseFloat(document.getElementById('settRiskFut')?.value) || 2.0;
    const capital_opt = parseFloat(document.getElementById('settCapitalOpt')?.value) || 100000;
    const risk_opt_pct = parseFloat(document.getElementById('settRiskOpt')?.value) || 7.0;
    const activeIndices = [];
    if (document.getElementById('checkNifty')?.checked) activeIndices.push('NIFTY');
    if (document.getElementById('checkBN')?.checked) activeIndices.push('BANKNIFTY');
    if (document.getElementById('checkSensex')?.checked) activeIndices.push('SENSEX');
    if (document.getElementById('checkMidcap')?.checked) activeIndices.push('MIDCPNIFTY');

    if (activeIndices.length === 0) {
        showToast("At least one index must be selected!", "error");
        return;
    }

    const config = {
        cmd: 'configure',
        capital_total: parseFloat(document.getElementById('settCapitalTotal')?.value) || 500000,
        capital_fut: parseFloat(document.getElementById('settCapitalFut')?.value) || 250000,
        risk_fut_pct: parseFloat(document.getElementById('settRiskFut')?.value) || 2.0,
        capital_opt: parseFloat(document.getElementById('settCapitalOpt')?.value) || 100000,
        risk_opt_pct: parseFloat(document.getElementById('settRiskOpt')?.value) || 7.0,
        futures_sl_pct: parseFloat(document.getElementById('settFutureSL')?.value) || 0.30,
        options_sl_pct: parseFloat(document.getElementById('settOptionSL')?.value) || 15,
        fut_cost: parseFloat(document.getElementById('settFutCost')?.value) || 200,
        opt_cost: parseFloat(document.getElementById('settOptCost')?.value) || 80,
        backtest_days: parseInt(document.getElementById('settBacktestDays')?.value) || 1,
        inst_pref: document.getElementById('settInstPref')?.value || 'AUTO',
        strike_selection: document.getElementById('settStrikeSelection')?.value || 'BOTH',
        grade_preference: document.getElementById('settGradePreference')?.value || 'auto',
        ut_preset: document.getElementById('settUtPreset')?.value || 'AGGRESSIVE',
        timeframe_entry_policy: document.getElementById('topTimeframePolicy')?.value || 'INCLUDE_5MIN',
        max_trades_per_index: parseInt(document.getElementById('settMaxTrades')?.value) || 5,
        max_consecutive_losses: parseInt(document.getElementById('settMaxLosses')?.value) || 3,
        index_cooldown_minutes: Math.max(0, parseFloat(document.getElementById('settIndexCooldown')?.value) || 0),
        futures_shadow_enabled: document.getElementById('settFuturesShadow')?.checked === true,
        pullback_hedge_enabled: document.getElementById('settPullbackHedge')?.checked === true,
        stop_out_reentry_enabled: document.getElementById('settStopOutReentry')?.checked === true,
        per_timeframe_grading_enabled: document.getElementById('settPerTfGrading')?.checked === true,
        engine_lab_enabled: document.getElementById('settEngineLab')?.checked !== false,
        engine_lab_real_mode_only: document.getElementById('settEngineLabRealOnly')?.checked === true,
        signal_fire_mode: document.getElementById('settIntrabarSignals')?.checked === false
            ? 'CANDLE_CLOSE'
            : 'INTRACANDLE',
        lean_routing_to_options: document.getElementById('settLeanRouting')?.checked === true,
        price_action_module_enabled: document.getElementById('settPriceActionModule')?.checked === true,
        price_action_block_adverse: document.getElementById('settPriceActionBlock')?.checked === true,
        price_action_adverse_threshold: numberVal('settPriceActionThreshold', -0.4),
        price_action_reversal_relief_enabled: checkedVal('settPriceActionRelief', true),
        regime_veto_enabled: document.getElementById('settRegimeVeto')?.checked === true,
        regime_veto_adx_floor: numberVal('settRegimeVetoAdx', 18),
        regime_early_trend_er_promotion_enabled: checkedVal('settRegimeErPromotion', true),
        regime_early_trend_er_period: intVal('settRegimeErPeriod', 10),
        regime_early_trend_er_threshold: numberVal('settRegimeErThreshold', 0.60),
        regime_early_trend_er_adx_ceiling: numberVal('settRegimeErAdxCeiling', 20.0),
        regime_early_trend_er_range_day_min_confidence: numberVal('settRegimeErRangeConf', 0.60),
        fut_early_loss_cut_enabled: document.getElementById('settFutLossCut')?.checked === true,
        fut_loss_cut_r: numberVal('settFutLossCutR', 0.6),
        power_move_override_enabled: document.getElementById('settPowerMoveOverride')?.checked === true,
        power_move_late_hard_cutoff: timeVal('settPowerMoveCutoff', '15:15'),
        power_move_spike_bypass_15m: document.getElementById('settSpikeBypass')?.checked === true,
        power_move_spike_range_atr_mult: numberVal('settSpikeRangeAtr', 2.5),
        live_5min_trend_continuation_enabled: document.getElementById('settTrendContinuation')?.checked === true,
        live_5min_trend_continuation_adx: numberVal('settTrendContinuationAdx', 28),
        quality_hybrid_min_vote_score: numberVal('settQualityVoteScore', 3.0),
        quality_hybrid_broad_agreement: intVal('settQualityBroadAgreement', 4),
        quality_hybrid_chop_min_agreement: intVal('settQualityChopAgreement', 4),
        quality_hybrid_trend_adx: numberVal('settQualityTrendAdx', 22.0),
        quality_hybrid_trend_er: numberVal('settQualityTrendEr', 0.50),
        quality_hybrid_thrust_volume_ratio: numberVal('settQualityThrustVol', 1.6),
        quality_hybrid_thrust_range_atr: numberVal('settQualityThrustRange', 1.05),
        quality_hybrid_thrust_close_loc: numberVal('settQualityThrustClose', 0.60),
        volume_spike_entry_enabled: document.getElementById('settVolumeSpikeEntry')?.checked === true,
        one_min_scalping_enabled: document.getElementById('settOneMinScalping')?.checked !== false,
        volume_spike_entry_min_volume_ratio: numberVal('settVolumeSpikeVolRatio', 2.5),
        volume_spike_entry_min_range_atr_ratio: numberVal('settVolumeSpikeRangeAtr', 1.5),
        volume_spike_entry_min_close_location: numberVal('settVolumeSpikeCloseLocation', 0.65),
        volume_spike_entry_futures_sl_pct: numberVal('settVolumeSpikeFutSL', 0.05),
        volume_spike_entry_options_sl_pct: numberVal('settVolumeSpikeOptSL', 5.0),
        volume_spike_entry_stabilization_seconds: Math.min(15, Math.max(3, numberVal('settVolumeSpikeStabilization', 3.0))),
        volume_spike_depth_confirm_enabled: checkedVal('settVolumeSpikeDepthConfirm', true),
        volume_spike_depth_ofr_bullish: numberVal('settVolumeSpikeOfrBull', 1.08),
        volume_spike_depth_ofr_bearish: numberVal('settVolumeSpikeOfrBear', 0.92),
        stabilization_momentum_override_enabled: checkedVal('settStabilizationMomentumOverride', true),
        live_signal_stabilization_seconds_5min: Math.min(60, Math.max(30, numberVal('settLiveStab5', 30.0))),
        live_signal_stabilization_seconds_15min: Math.min(60, Math.max(30, numberVal('settLiveStab15', 30.0))),
        live_15min_momentum_override_enabled: checkedVal('settLive15MomentumOverride', true),
        live_15min_momentum_min_confidence: numberVal('settLive15MomentumMinConf', 0.20),
        live_15min_momentum_volume_ratio: numberVal('settLive15MomentumVolRatio', 2.0),
        live_15min_momentum_range_atr_ratio: numberVal('settLive15MomentumRangeAtr', 1.25),
        live_15min_momentum_close_location: numberVal('settLive15MomentumCloseLocation', 0.65),
        live_15min_momentum_adx_threshold: numberVal('settLive15MomentumAdx', 24.0),
        live_15min_momentum_intel_threshold: numberVal('settLive15MomentumIntel', 0.65),
        reversal_entry_relief_enabled: checkedVal('settReversalEntryRelief', true),
        reversal_entry_min_confidence: numberVal('settReversalMinConf', 0.20),
        reversal_entry_thrust_min_range_atr_ratio: numberVal('settReversalRangeAtr', 1.20),
        reversal_entry_thrust_min_close_location: numberVal('settReversalCloseLocation', 0.62),
        reversal_entry_thrust_min_volume_ratio: numberVal('settReversalVolumeRatio', 1.20),
        reversal_entry_vwap_min_dev_pct: numberVal('settReversalVwapDev', 0.03),
        directional_census_reversal_relief_enabled: checkedVal('settDirectionalCensusRelief', true),
        directional_census_reversal_min_confidence: numberVal('settDirectionalCensusMinConf', 0.20),
        late_session_choppy_entry_guard_enabled: checkedVal('settLateSessionChoppyGuard', true),
        late_session_choppy_min_runway_minutes: numberVal('settLateSessionRunway', 30.0),
        peak_threat_exit_enabled: checkedVal('settPeakThreatExit', true),
        peak_threat_exit_min_peak_rs: numberVal('settPeakThreatMinPeak', 400.0),
        breakeven_stop_enabled: checkedVal('settBreakevenStop', true),
        breakeven_stop_arm_rs: numberVal('settBreakevenStopArm', 100.0),
        breakeven_stop_floor_rs: numberVal('settBreakevenStopFloor', 80.0),
        peak_trail_enabled: checkedVal('settPeakTrail', false),
        peak_trail_above_rs: numberVal('settPeakTrailAbove', 600.0),
        peak_trail_giveback_pct: numberVal('settPeakTrailGiveback', 0.40),
        peak_giveback_cap_enabled: checkedVal('settPeakGivebackCap', false),
        peak_giveback_cap_arm_rs: numberVal('settPeakGivebackCapArm', 100.0),
        peak_giveback_cap_rs: numberVal('settPeakGivebackCapRs', 200.0),
        peak_giveback_cap_pct: numberVal('settPeakGivebackCapPct', 0.25),
        peak_giveback_cap_max_peak_rs: numberVal('settPeakGivebackCapMaxPeak', 1500.0),
        backtest_live_exits_enabled: checkedVal('settBacktestLiveExits', true),
        peak_threat_exit_min_peak_r: numberVal('settPeakThreatMinPeakR', 0.35),
        peak_threat_exit_min_retention: numberVal('settPeakThreatRetention', 0.72),
        peak_threat_exit_min_score: intVal('settPeakThreatScore', 5),
        peak_threat_exit_severe_score: intVal('settPeakThreatSevereScore', 4),
        peak_threat_exit_adverse_intel: numberVal('settPeakThreatIntel', 0.55),
        peak_threat_exit_weak_volume_ratio: numberVal('settPeakThreatWeakVol', 1.05),
        peak_threat_exit_loss_enabled: checkedVal('settPeakThreatLossExit', true),
        peak_threat_exit_loss_min_score: intVal('settPeakThreatLossScore', 6),
        peak_threat_exit_loss_max_loss_r: numberVal('settPeakThreatLossMaxR', 0.35),
        option_major_win_trend_lock_ratio_cap: numberVal('settOptionMajorWinTrendCap', 0.95),
        market_wide_reversal_enabled: checkedVal('settMarketWideReversal', true),
        market_wide_reversal_core_votes: intVal('settMarketWideCoreVotes', 2),
        market_wide_reversal_total_votes: intVal('settMarketWideTotalVotes', 2),
        market_wide_reversal_min_range_atr_ratio: numberVal('settMarketWideRangeAtr', 1.10),
        market_wide_reversal_min_range_atr_ratio_5min: numberVal('settMarketWideRangeAtr5', 1.10),
        market_wide_reversal_min_range_atr_ratio_15min: numberVal('settMarketWideRangeAtr15', 0.50),
        market_wide_reversal_midcap_min_range_atr_ratio_5min: numberVal('settMarketWideMidcapRangeAtr5', 0.65),
        market_wide_reversal_midcap_min_range_atr_ratio_15min: numberVal('settMarketWideMidcapRangeAtr15', 0.45),
        market_wide_reversal_min_close_location: numberVal('settMarketWideCloseLocation', 0.65),
        market_wide_reversal_max_skew_seconds_5min: numberVal('settMarketWideSkew5', 180.0),
        market_wide_reversal_max_skew_seconds_15min: numberVal('settMarketWideSkew15', 900.0),
        one_way_cascade_enabled: checkedVal('settOneWayCascadeEnabled', true),
        one_way_cascade_core_votes: intVal('settOneWayCascadeCoreVotes', 3),
        one_way_cascade_total_votes: intVal('settOneWayCascadeTotalVotes', 3),
        one_way_cascade_volume_votes: intVal('settOneWayCascadeVolumeVotes', 3),
        one_way_cascade_min_volume_ratio: numberVal('settOneWayCascadeVolume', 1.40),
        one_way_cascade_min_range_atr_ratio_1min: numberVal('settOneWayCascadeRange1', 1.50),
        one_way_cascade_min_range_atr_ratio_5min: numberVal('settOneWayCascadeRange5', 1.20),
        one_way_cascade_min_range_atr_ratio_15min: numberVal('settOneWayCascadeRange15', 0.75),
        one_way_cascade_min_close_location: numberVal('settOneWayCascadeClose', 0.68),
        one_way_cascade_runner_lock_pct: numberVal('settOneWayCascadeRunnerLock', 0.85),
        one_way_cascade_options_sl_pct: numberVal('settOneWayCascadeOptSL', 5.0),
        one_way_cascade_futures_sl_pct: numberVal('settOneWayCascadeFutSL', 0.05),
        one_way_cascade_rr: numberVal('settOneWayCascadeRR', 1.5),
        missed_signal_reentry_enabled: checkedVal('settMissedReentryEnabled', true),
        missed_signal_reentry_allow_trend_context: checkedVal('settMissedReentryTrend', true),
        missed_signal_reentry_prefer_options: checkedVal('settMissedReentryPreferOptions', true),
        missed_signal_reentry_max_age_minutes: numberVal('settMissedReentryMaxAge', 6.0),
        missed_signal_reentry_min_age_seconds: numberVal('settMissedReentryMinAge', 20.0),
        missed_signal_reentry_max_chase_atr: numberVal('settMissedReentryChaseAtr', 0.75),
        missed_signal_reentry_min_confidence: numberVal('settMissedReentryConf', 0.55),
        missed_signal_reentry_min_intel_score: numberVal('settMissedReentryIntel', 0.20),
        missed_signal_reentry_min_trend_adx: numberVal('settMissedReentryAdx', 24.0),
        missed_signal_reentry_min_remaining_minutes: numberVal('settMissedReentryRunway', 8.0),
        missed_signal_reentry_options_sl_pct: numberVal('settMissedReentryOptSL', 5.0),
        missed_signal_reentry_futures_sl_pct: numberVal('settMissedReentryFutSL', 0.05),
        missed_signal_reentry_rr: numberVal('settMissedReentryRR', 1.5),
        late_session_reclaim_override_enabled: checkedVal('settLateReclaimEnabled', true),
        late_session_reclaim_start: timeVal('settLateReclaimStart', '14:25'),
        late_session_reclaim_end: timeVal('settLateReclaimEnd', '15:10'),
        late_session_reclaim_core_votes: intVal('settLateReclaimCoreVotes', 3),
        late_session_reclaim_total_votes: intVal('settLateReclaimTotalVotes', 3),
        late_session_reclaim_reclaim_votes: intVal('settLateReclaimReclaimVotes', 2),
        late_session_reclaim_max_skew_seconds: numberVal('settLateReclaimSkew', 240.0),
        late_session_reclaim_min_close_location: numberVal('settLateReclaimCloseLocation', 0.55),
        late_session_reclaim_min_volume_ratio: numberVal('settLateReclaimVolume', 1.10),
        late_session_reclaim_volume_votes: intVal('settLateReclaimVolumeVotes', 3),
        late_session_reclaim_options_sl_pct: numberVal('settLateReclaimOptSL', 5.0),
        late_session_reclaim_futures_sl_pct: numberVal('settLateReclaimFutSL', 0.05),
        late_session_reclaim_rr: numberVal('settLateReclaimRR', 1.5),
        fifteen_min_choppy_scalp_guard_enabled: checkedVal('sett15mChoppyScalpEnabled', true),
        fifteen_min_choppy_scalp_min_intel_score: numberVal('sett15mChoppyScalpIntel', 0.20),
        fifteen_min_choppy_scalp_min_volume_ratio: numberVal('sett15mChoppyScalpVolume', 1.05),
        fifteen_min_choppy_scalp_micro_max_skew_seconds: numberVal('sett15mChoppyScalpSkew', 240.0),
        fifteen_min_choppy_scalp_min_close_location: numberVal('sett15mChoppyScalpClose', 0.55),
        fifteen_min_choppy_scalp_profit_lock_r_multiple: numberVal('sett15mChoppyScalpLockR', 0.20),
        fifteen_min_choppy_scalp_min_peak_rs: numberVal('sett15mChoppyScalpPeakRs', 250.0),
        midcap_15min_risky_stabilization_seconds: numberVal('settMidcap15mBufferSec', 60.0),
        midcap_15min_risky_stabilization_min_scans: intVal('settMidcap15mBufferScans', 3),
        midcap_15min_risky_stabilization_min_volume_ratio: numberVal('settMidcap15mVolume', 1.05),
        midcap_15min_risky_stabilization_min_intel_score: numberVal('settMidcap15mIntel', 0.25),
        dual_lane_enabled: checkedVal('settDualLaneEnabled', true),
        ...Object.fromEntries(AUTO_START_FIELDS.map(([key, id, , kind, fallback]) => [key,
            kind === 'bool' ? checkedVal(id, fallback) : timeVal(id, fallback)])),
        ...Object.fromEntries(S1_SETTINGS_FIELDS.map(([key, id, , kind, fallback]) => [key,
            kind === 'bool' ? checkedVal(id, fallback)
                : kind === 'int' ? intVal(id, fallback)
                : kind === 'float' ? numberVal(id, fallback)
                : kind === 'time' ? timeVal(id, fallback)
                : (document.getElementById(id)?.value || fallback)])),
        ...Object.fromEntries(UTS_SETTINGS_FIELDS.map(([key, id, , kind, fallback]) => [key,
            kind === 'bool' ? checkedVal(id, fallback)
                : kind === 'int' ? intVal(id, fallback)
                : kind === 'float' ? numberVal(id, fallback)
                : kind === 'time' ? timeVal(id, fallback)
                : (document.getElementById(id)?.value || fallback)])),
        ...Object.fromEntries(S12_SETTINGS_FIELDS.map(([key, id, , kind, fallback]) => [key,
            kind === 'bool' ? checkedVal(id, fallback)
                : kind === 'int' ? intVal(id, fallback)
                : kind === 'float' ? numberVal(id, fallback)
                : kind === 'time' ? timeVal(id, fallback)
                : (document.getElementById(id)?.value || fallback)])),
        dual_lane_same_dir_min_bars_gap: intVal('settDualLaneGapBars', 3),
        dual_lane_same_dir_min_lane_a_r: numberVal('settDualLaneMinA_R', 0.50),
        dual_lane_size_fraction: numberVal('settDualLaneSizeFraction', 0.65),
        dual_lane_max_index_exposure_rs: numberVal('settDualLaneExposureCap', 12000.0),
        dual_lane_min_lane_b_confidence: numberVal('settDualLaneMinBConf', 0.70),
        dual_lane_jaccard_alert_threshold: numberVal('settDualLaneJaccard', 0.30),
        dual_clone_reduction_enabled: checkedVal('settDualCloneReduction', true),
        dual_clone_reduction_window_seconds: intVal('settDualCloneWindowSec', 60),
        cascade_lot_scale_enabled: checkedVal('settCascadeLotScale', true),
        cascade_lot_multiplier_opt_min: numberVal('settCascadeLotOptMin', 2.0),
        cascade_lot_multiplier_opt: numberVal('settCascadeLotOpt', 3.0),
        cascade_lot_multiplier_fut: numberVal('settCascadeLotFut', 1.0),
        heavy_trend_lot_scale_enabled: checkedVal('settHeavyTrendScale', true),
        heavy_trend_lot_multiplier_opt: numberVal('settHeavyTrendOptMult', 2.0),
        heavy_trend_min_aligned_indices: intVal('settHeavyTrendMinAlign', 2),
        heavy_trend_vote_max_age_seconds: numberVal('settHeavyTrendVoteAge', 600),
        cascade_multi_tf_stack_enabled: checkedVal('settCascadeMultiTf', true),
        cascade_primary_max_tf: intVal('settCascadePrimaryMaxTf', 3),
        cascade_dual_addon_max: intVal('settCascadeDualAddon', 2),
        cascade_midcap_solo_max_tf: intVal('settCascadeMidSolo', 2),
        cascade_midcap_dual_main_max: intVal('settCascadeMidDualMain', 2),
        cascade_midcap_dual_addon_max: intVal('settCascadeMidDualAddon', 1),
        cascade_core_bucket_max_positions: intVal('settCascadeCoreBucket', 12),
        cascade_midcap_bucket_max_positions: intVal('settCascadeMidBucket', 3),
        cascade_max_concurrent_positions: intVal('settCascadeMaxPos', 18),
        multi_signal_mixed_book_enabled: checkedVal('settMixedBookEnabled', true),
        multi_signal_max_fut: intVal('settMixedBookMaxFut', 1),
        multi_signal_fut_lots: intVal('settMixedBookFutLots', 1),
        multi_signal_max_opt_instruments: intVal('settMixedBookMaxOpt', 18),
        ut_confidence_calibration_enabled: document.getElementById('settConfidenceCalibration')?.checked === true,
        ut_confidence_calibration_min_samples: intVal('settCalibrationMinSamples', 200),
        execution_use_touch_quote: checkedVal('settExecutionTouchQuote', false),
        execution_touch_quote_shadow_enabled: checkedVal('settExecutionTouchQuoteShadow', true),
        execution_touch_quote_strict: checkedVal('settExecutionTouchQuoteStrict', false),
        ut_no_entry_after: timeVal('settNoEntryAfter', '14:50'),
        ut_5min_no_entry_after: timeVal('sett5mNoEntryAfter', '15:10'),
        ut_1min_no_entry_after: timeVal('sett1mNoEntryAfter', '15:14'),
        ut_force_exit_time: timeVal('settForceExitTime', '15:22'),
        active_indices: activeIndices,
        lots: {
            NIFTY: parseInt(document.getElementById('settLotsNifty')?.value) || 1,
            BANKNIFTY: parseInt(document.getElementById('settLotsBN')?.value) || 1,
            SENSEX: parseInt(document.getElementById('settLotsSensex')?.value) || 1,
            MIDCPNIFTY: parseInt(document.getElementById('settLotsMidcap')?.value) || 1,
        },
        lots_fut: {
            NIFTY: parseInt(document.getElementById('settLotsFutNifty')?.value) || 1,
            BANKNIFTY: parseInt(document.getElementById('settLotsFutBN')?.value) || 1,
            SENSEX: parseInt(document.getElementById('settLotsFutSensex')?.value) || 1,
            MIDCPNIFTY: parseInt(document.getElementById('settLotsFutMidcap')?.value) || 1,
        }
    };
    const selectedScannerEngine = getActiveScannerEngine();
    const currentScannerEngine = String(
        state.latestData?.config?.scanner_engine
        || document.getElementById('scannerEngineSelect')?.value
        || 'quad_prime'
    ).toLowerCase();
    window.sendCommand(config);
    if (selectedScannerEngine && selectedScannerEngine !== currentScannerEngine) {
        window.sendCommand({ cmd: 'set_scanner_engine', state: selectedScannerEngine });
        if (state.latestData?.config) state.latestData.config.scanner_engine = selectedScannerEngine;
        syncSettingsScannerEngine(selectedScannerEngine);
        const label = SCANNER_ENGINE_LABELS[selectedScannerEngine] || selectedScannerEngine.toUpperCase();
        showToast(`Signal Engine: ${label}`, selectedScannerEngine === 'utbot' ? 'info' : 'warning');
    }
    const modal = document.getElementById('settingsModal');
    if (modal) modal.style.display = 'none';
}

function resetCache() {
    if (!confirm("Are you sure you want to reset cache and re-sync all data? This will pause scanning for a few minutes.")) {
        return;
    }
    
    authFetch('/api/reset_cache', {
        method: 'POST',
        headers: {
            'Content-Type': 'application/json'
        }
    })
    .then(response => response.json())
    .then(data => {
        if (data.status === 'ok') {
            showToast("Cache reset initiated! System is re-syncing data...", "success");
            document.getElementById('settingsModal').style.display = 'none';
        } else {
            showToast("Failed to reset cache: " + data.message, "error");
        }
    })
    .catch(error => {
        console.error('Error resetting cache:', error);
        showToast("Error resetting cache!", "error");
    });
}
