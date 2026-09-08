/* Trade and signal table rendering. Loaded before app.js as a classic script. */

// XSS guard for broker-sourced text (symbols, reasons) inserted via innerHTML.
function escHtml(value) {
    if (value === null || value === undefined) return '';
    return String(value)
        .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

function formatTradeTableSharpeDailyYearly(source = {}) {
    if (source && source.sharpe_ratio_display) return source.sharpe_ratio_display;
    const dailyRaw = source && source.sharpe_ratio_daily !== undefined
        ? source.sharpe_ratio_daily
        : source && source.sharpe_ratio;
    const daily = Number(dailyRaw || 0);
    const dailySafe = Number.isFinite(daily) ? daily : 0;
    const yearlyRaw = source && source.sharpe_ratio_yearly !== undefined
        ? source.sharpe_ratio_yearly
        : dailySafe * Math.sqrt(252);
    const yearly = Number(yearlyRaw || 0);
    const yearlySafe = Number.isFinite(yearly) ? yearly : 0;
    return `${dailySafe.toFixed(2)} / ${yearlySafe.toFixed(2)}`;
}

function isQuoteInvalidTradeRow(row = {}) {
    const reason = String(row.exit_reason || row.status || '').toUpperCase().replace(/\s+/g, '_');
    return reason.includes('QUOTE_INVALID') || row.quote_invalid === true;
}

function accountingTradePnl(row = {}) {
    if (isQuoteInvalidTradeRow(row)) return 0;
    const value = Number(row.accounting_pnl !== undefined ? row.accounting_pnl : row.pnl || 0);
    return Number.isFinite(value) ? value : 0;
}

function parseDashboardTimestamp(value) {
    if (value === null || value === undefined || value === '') return 0;
    if (typeof value === 'number' && Number.isFinite(value)) {
        return value > 1e12 ? value : value * 1000;
    }
    const numeric = Number(value);
    if (Number.isFinite(numeric) && String(value).trim() !== '') {
        return numeric > 1e12 ? numeric : numeric * 1000;
    }
    const parsed = Date.parse(String(value));
    return Number.isNaN(parsed) ? 0 : parsed;
}

function getTradeRowEventTimestamp(row) {
    const exitTs = parseDashboardTimestamp(row && (row.exit_timestamp || row.exit_time));
    const entryTs = parseDashboardTimestamp(row && (row.entry_timestamp || row.timestamp || row.entry_time));
    return entryTs || exitTs;
}

function formatDashboardTime(value) {
    const ms = parseDashboardTimestamp(value);
    if (!ms) return '';
    return new Date(ms).toLocaleTimeString('en-GB', { hour12: false });
}

function formatDashboardDate(value) {
    const ms = parseDashboardTimestamp(value);
    if (!ms) return '';
    return new Date(ms).toLocaleDateString('en-IN', { day: '2-digit', month: 'short', year: '2-digit' });
}

function normalizeTradeDateTime(explicitTime, explicitDate, fallbackTimestamp) {
    const rawTime = String(explicitTime || '').trim();
    const fallback = fallbackTimestamp || '';
    let timeText = '';

    // Accept both time-only values and ISO/full datetime values. substring(0, 8)
    // rendered an ISO value as "2026-08-", which made real closed rows ambiguous.
    const clock = rawTime.match(/(?:^|[T\s])(\d{2}:\d{2}(?::\d{2})?)(?:[.Z+\-]|$)/);
    if (clock) {
        timeText = clock[1].length === 5 ? `${clock[1]}:00` : clock[1];
    } else if (/^\d{2}:\d{2}(?::\d{2})?$/.test(rawTime)) {
        timeText = rawTime.length === 5 ? `${rawTime}:00` : rawTime;
    } else {
        timeText = formatDashboardTime(rawTime || fallback);
    }

    let dateText = String(explicitDate || '').trim();
    if (dateText) {
        // Preserve the conventional "21 Aug 26" label while bounding arbitrary
        // broker strings before insertion into the table.
        dateText = dateText.split(/\s+/).slice(0, 3).join(' ');
    } else {
        const timeCarriesDate = /^\d{4}-\d{2}-\d{2}[T\s]/.test(rawTime);
        dateText = formatDashboardDate(timeCarriesDate ? rawTime : fallback);
    }
    return { timeText, dateText };
}

function getTradeRowSortRank(row) {
    const status = String(row && row.status ? row.status : '').toUpperCase();
    if (status === 'OPEN' || (row && row.row_kind === 'manager_open')) return 0;
    if (status.indexOf('EXIT') !== -1 || (row && row.row_kind === 'signal')) return 1;
    if (status === 'CLOSED' || (row && row.row_kind === 'manager_closed')) return 2;
    return 3;
}

function getTradePoints(row) {
    if (!row) return null;
    const entry = Number(row.entry_price || row.price || 0);
    if (!Number.isFinite(entry) || entry <= 0) return null;

    const exit = Number(row.exit_price || 0);
    const current = Number(row.current_price || 0);
    const mark = exit > 0 ? exit : current;
    if (!Number.isFinite(mark) || mark <= 0) return null;

    const instrumentText = String(row.instrument || '');
    const isOption = row.inst_type === 'OPT' || instrumentText.includes('CE') || instrumentText.includes('PE');
    const direction = String(row.direction || '').toUpperCase();
    const points = isOption || direction !== 'SHORT' ? mark - entry : entry - mark;
    return Number.isFinite(points) ? points : null;
}

function formatTradePoints(points) {
    if (points === null || points === undefined || !Number.isFinite(points)) return '';
    const sign = points < 0 ? '-' : '';
    return `${sign}${Math.abs(points).toLocaleString('en-IN', {
        minimumFractionDigits: 2,
        maximumFractionDigits: 2,
    })}`;
}

function collectSessionSignalRows(existingRows = [], backendSignals = []) {
    const baseName = value => String(value || '').split(/\s+/)[0];
    const renderedIds = new Set(existingRows.map(row => String(row.id || '')).filter(Boolean));
    const renderedLifecycleIds = new Set(existingRows.map(row => String(row.lifecycle_id || '')).filter(Boolean));
    const rowMinuteKey = row => {
        const ts = row.entry_timestamp ? new Date(Number(row.entry_timestamp) * 1000) : null;
        const validTs = ts && !Number.isNaN(ts.getTime());
        const dateText = row.entry_date || (validTs ? ts.toLocaleDateString('en-IN', { day: '2-digit', month: 'short', year: '2-digit' }) : '');
        const timeText = row.entry_time || (validTs ? ts.toLocaleTimeString('en-GB', { hour12: false }) : '');
        return [
            baseName(row.base_instrument || row.instrument || row.trading_symbol),
            row.timeframe || '',
            row.direction || '',
            dateText,
            String(timeText || '').substring(0, 8)
        ].join('|');
    };
    const realKeys = new Set(existingRows.map(rowMinuteKey));
    const signalMinuteKey = sig => {
        const ts = sig.timestamp ? new Date(sig.timestamp) : null;
        const validTs = ts && !Number.isNaN(ts.getTime());
        const dateText = sig.entry_date || (validTs ? ts.toLocaleDateString('en-IN', { day: '2-digit', month: 'short', year: '2-digit' }) : '');
        const timeText = sig.entry_time || (validTs ? ts.toLocaleTimeString('en-GB', { hour12: false }) : '');
        return [
            baseName(sig.base_instrument || sig.instrument || sig.trading_symbol),
            sig.timeframe || '',
            sig.direction || '',
            dateText,
            String(timeText || '').substring(0, 8)
        ].join('|');
    };

    const rawRows = [];
    const candidateSignals = [];
    (backendSignals || []).forEach(sig => candidateSignals.push(sig));
    Object.values((state.latestData && state.latestData.instruments) || {}).forEach(inst => {
        (inst.trade_candidates || []).forEach(sig => candidateSignals.push(sig));
    });

    candidateSignals.forEach(sig => {
            const sigId = String(sig.id || '');
            const sigLifecycleId = String(sig.lifecycle_id || '');
            if (sigId && renderedIds.has(sigId)) return;
            if (sigLifecycleId && renderedLifecycleIds.has(sigLifecycleId)) return;
            if (realKeys.has(signalMinuteKey(sig))) return;
            const rowKey = [
                sig.trading_symbol || sig.instrument || '',
                sig.direction || '',
                sig.timeframe || ''
            ].join('|');
            if (realKeys.has(rowKey)) return;

            const ts = sig.timestamp ? new Date(sig.timestamp) : null;
            const isValidTs = ts && !Number.isNaN(ts.getTime());
            const exitTs = sig.exit_timestamp ? new Date(sig.exit_timestamp) : null;
            const isValidExitTs = exitTs && !Number.isNaN(exitTs.getTime());
            const instrumentName = sig.inst_type === 'OPT' && sig.atm_strike
                ? `${sig.instrument} ${sig.atm_strike} ${sig.option_type || ''}`
                : (sig.trading_symbol || sig.instrument || '--');
            const isExit = sig.is_exit || sig.action === 'EXIT' || sig.status === 'EXIT SIGNAL';
            const isNoEntry = sig.action === 'NO_ENTRY' || String(sig.status || '').includes('NO ENTRY');
            const exitReason = sig.exit_reason || sig.status || '';
            const rowPnl = accountingTradePnl({ ...sig, exit_reason: exitReason });
            
            // Fix: Filter out ghost 'NO_ENTRY' signals so they do not flash on the dashboard
            if (isNoEntry) return;
            
            const key = [
                sig.instrument || '',
                sig.timeframe || '',
                sig.action || 'ENTRY',
                sig.direction || '',
                sig.trading_symbol || sig.atm_strike || '',
                isValidTs ? Math.floor(ts.getTime() / 1000) : 0,
                isValidExitTs ? Math.floor(exitTs.getTime() / 1000) : 0
            ].join('|');

            rawRows.push({
                row_kind: 'signal',
                id: key,
                signal_id: sig.id || '',
                lifecycle_id: sig.lifecycle_id || '',
                status: isExit ? 'EXIT_SIGNAL' : (isNoEntry ? 'NO_ENTRY' : 'SIGNAL'),
                base_instrument: sig.instrument || '',
                instrument: instrumentName,
                direction: sig.direction || '--',
                underlying_direction: sig.underlying_direction || sig.direction || '--',
                trade_side: sig.trade_side || '',
                entry_price: sig.price,
                current_price: sig.current_price || sig.price,
                exit_price: sig.exit_price || (isExit ? (sig.current_price || sig.price) : 0),
                trailing_stop: sig.stop,
                target: isExit ? 0 : sig.target,
                pnl: rowPnl,
                accounting_pnl: rowPnl,
                quote_invalid: isQuoteInvalidTradeRow({ ...sig, exit_reason: exitReason }),
                lots: sig.lots || 0,
                lot_size: sig.lot_size || 0,
                grade: sig.grade || '--',
                confidence: sig.confidence || 0,
                rr_ratio: sig.rr || 0,
                atm_strike: sig.atm_strike,
                option_type: sig.option_type,
                inst_type: sig.inst_type || 'FUT',
                timeframe: sig.timeframe || '5min',
                trading_symbol: sig.trading_symbol || '',
                entry_time: isValidTs ? ts.toLocaleTimeString('en-GB', { hour12: false }) : '--',
                entry_date: isValidTs ? ts.toLocaleDateString('en-IN', { day: '2-digit', month: 'short', year: '2-digit' }) : '--',
                entry_timestamp: isValidTs ? Math.floor(ts.getTime() / 1000) : 0,
                exit_time: isValidExitTs ? exitTs.toLocaleTimeString('en-GB', { hour12: false }) : null,
                exit_date: isValidExitTs ? exitTs.toLocaleDateString('en-IN', { day: '2-digit', month: 'short', year: '2-digit' }) : null,
                exit_timestamp: isValidExitTs ? Math.floor(exitTs.getTime() / 1000) : 0,
                exit_reason: exitReason,
                accepted_by_gate: !!sig.accepted_by_gate,
                watch_only: !!sig.watch_only,
                manual_entry_executed: !!sig.manual_entry_executed,
                manual_exit_trade_id: sig.manual_exit_trade_id || '',
                manual_exit_price: Number(sig.manual_exit_price || 0),
                manual_exit_enabled: sig.manual_exit_enabled === true,
                manual_exit_status: sig.manual_exit_status || '',
                manual_exit_execution_status: sig.manual_exit_execution_status || '',
                exec_type: isExit ? 'EXIT' : (isNoEntry ? 'BLOCK' : 'SIG'),
            });
    });

    const rowsNotAlreadyRendered = rawRows.filter(row => !realKeys.has(rowMinuteKey(row)));
    rawRows.length = 0;
    rawRows.push(...rowsNotAlreadyRendered);

    const exitEntryKeys = new Set();
    rawRows.forEach(row => {
        if (row.status !== 'EXIT_SIGNAL') return;
        exitEntryKeys.add(row.lifecycle_id || [
            row.base_instrument || row.instrument,
            row.timeframe,
            row.direction,
            row.entry_timestamp || 0
        ].join('|'));
    });

    const byKey = new Map();
    rawRows.forEach(row => {
        if (row.status === 'SIGNAL') {
            const entryKey = row.lifecycle_id || [
                row.base_instrument || row.instrument,
                row.timeframe,
                row.direction,
                row.entry_timestamp || 0
            ].join('|');
            if (exitEntryKeys.has(entryKey)) return;
        }
        byKey.set(row.id, row);
    });

    return Array.from(byKey.values());
}

function buildSessionSummary(baseSummary, openRows, closedRows, signalRows) {
    const summary = { ...(baseSummary || {}) };
    const backendHasTrades = (summary.total_trades || 0) > 0 || (summary.open_count || 0) > 0 || openRows.length || closedRows.length;
    if (backendHasTrades || !signalRows.length) return summary;

    const exitRows = signalRows.filter(row => row.status === 'EXIT_SIGNAL');
    const exitKeys = new Set(exitRows.map(row => [
        row.base_instrument || row.instrument,
        row.timeframe,
        row.direction,
        row.entry_timestamp || 0,
    ].join('|')));

    const activeRows = signalRows.filter(row => {
        if (row.status !== 'SIGNAL') return false;
        const key = [
            row.base_instrument || row.instrument,
            row.timeframe,
            row.direction,
            row.entry_timestamp || 0,
        ].join('|');
        return !exitKeys.has(key);
    });
    const accountingRows = [...exitRows, ...activeRows];
    const pnls = accountingRows.map(row => accountingTradePnl(row));
    const wins = pnls.filter(v => v > 0).length;
    const losses = pnls.filter(v => v < 0).length;
    const grossWin = pnls.filter(v => v > 0).reduce((a, b) => a + b, 0);
    const grossLoss = Math.abs(pnls.filter(v => v < 0).reduce((a, b) => a + b, 0));
    const totalPnl = pnls.reduce((a, b) => a + b, 0);
    const isOptRow = (row) => {
        return row.inst_type === 'OPT' ||
               (row.instrument && (row.instrument.includes('CE') || row.instrument.includes('PE'))) ||
               (row.trading_symbol && (row.trading_symbol.includes('CE') || row.trading_symbol.includes('PE')));
    };
    let futPnl = 0, optPnl = 0, futTrades = 0, optTrades = 0;
    accountingRows.forEach(row => {
        const pnl = accountingTradePnl(row);
        if (isOptRow(row)) {
            optPnl += pnl;
            optTrades++;
        } else {
            futPnl += pnl;
            futTrades++;
        }
    });

    let equity = 0;
    let peak = 0;
    let maxDd = 0;
    accountingRows
        .slice()
        .sort((a, b) => (a.entry_timestamp || 0) - (b.entry_timestamp || 0))
        .forEach(row => {
            equity += accountingTradePnl(row);
            peak = Math.max(peak, equity);
            maxDd = Math.max(maxDd, peak - equity);
        });

    const avg = pnls.length ? totalPnl / pnls.length : 0;
    const variance = pnls.length > 1
        ? pnls.reduce((sum, val) => sum + Math.pow(val - avg, 2), 0) / (pnls.length - 1)
        : 0;
    const std = Math.sqrt(variance);
    const sharpeDaily = std > 0 ? avg / std : 0;
    const sharpeYearly = sharpeDaily * Math.sqrt(252);

    return {
        ...summary,
        daily_pnl: totalPnl,
        fut_pnl: futPnl,
        opt_pnl: optPnl,
        fut_trades: futTrades,
        opt_trades: optTrades,
        total_trades: accountingRows.length,
        open_count: activeRows.length,
        wins,
        losses,
        win_rate: accountingRows.length ? (wins / accountingRows.length) * 100 : 0,
        profit_factor: grossLoss > 0 ? Math.min(99.99, grossWin / grossLoss) : (grossWin > 0 ? 99.99 : 1),
        sharpe_ratio: sharpeDaily,
        sharpe_ratio_daily: sharpeDaily,
        sharpe_ratio_yearly: sharpeYearly,
        sharpe_ratio_display: formatTradeTableSharpeDailyYearly({
            sharpe_ratio_daily: sharpeDaily,
            sharpe_ratio_yearly: sharpeYearly,
        }),
        max_drawdown: maxDd,
    };
}

function renderLtpExitCell(row = {}, isClosedTradeRow = false, isExitSignalRow = false) {
    const current = Number(row.current_price || 0);
    const exit = Number(row.exit_price || 0);
    const showExit = (isClosedTradeRow || isExitSignalRow) && exit > 0;
    const main = showExit ? exit : current;
    if (!Number.isFinite(main) || main <= 0) return '--';

    const liveDifferent = showExit && current > 0 && Math.abs(current - exit) >= 0.01;
    const sub = showExit
        ? (liveDifferent ? `exit · ltp ${formatPrice(current)}` : 'exit')
        : 'ltp';
    return `<span class="price-stack"><span>${formatPrice(main)}</span><small>${sub}</small></span>`;
}

function parseRiskRewardValue(value) {
    if (value === null || value === undefined || value === '') return null;
    if (typeof value === 'number') {
        return Number.isFinite(value) && value > 0 ? value : null;
    }
    const text = String(value).trim();
    if (!text) return null;
    const ratioMatch = text.match(/(?:^|\s)1\s*[:/]\s*([0-9]+(?:\.[0-9]+)?)/i);
    const numericText = ratioMatch ? ratioMatch[1] : text;
    const parsed = Number(numericText);
    return Number.isFinite(parsed) && parsed > 0 ? parsed : null;
}

function resolveRiskReward(row = {}) {
    const stored = parseRiskRewardValue(row.rr_ratio) ?? parseRiskRewardValue(row.rr);
    if (stored !== null) return stored;

    const entry = Number(row.entry_price || row.price || 0);
    const target = Number(row.target || row.display_target || row.current_target || 0);
    const stop = Number(row.initial_stop || row.display_stop || row.stop || row.current_stop || 0);
    if (![entry, target, stop].every(v => Number.isFinite(v) && v > 0)) return null;

    const risk = Math.abs(entry - stop);
    const reward = Math.abs(target - entry);
    if (risk <= 0 || reward <= 0) return null;
    return reward / risk;
}

function formatRiskReward(row = {}) {
    const rr = resolveRiskReward(row);
    if (rr === null) return '--';
    return `1:${rr.toFixed(1)}`;
}

function renderTradeControlCell(row = {}, flags = {}) {
    const {
        isSignalRow = false,
        isExitSignalRow = false,
        isOpenTrade = false,
        executionRetryable = false,
    } = flags;
    if (isOpenTrade) {
        const label = executionRetryable ? 'RETRY' : 'EXIT';
        // A close is a MARKET order: price is only a local accounting mark until the
        // broker confirms the fill.  Keep the emergency exit usable during a quote/feed
        // outage instead of presenting a button that can only say "invalid_price".
        const exitPrice = Number(row.current_price) || 0;
        const title = exitPrice > 0
            ? (label === 'RETRY' ? 'Retry pending exit' : 'Submit manual exit')
            : 'Submit MARKET exit; quote unavailable, broker fill will reconcile';
        return `<div class="trade-control-stack"><button class="trade-control-btn exit" data-action="close-trade" data-trade-id="${escHtml(row.id)}" data-trade-price="${exitPrice}" title="${escHtml(title)}">${label}</button></div>`;
    }
    if (isSignalRow) {
        const manualExitTradeId = row.manual_exit_trade_id || '';
        if (manualExitTradeId) {
            const manualExitPrice = Number(row.manual_exit_price || row.current_price || row.exit_price || row.entry_price || 0);
            const canExit = row.manual_exit_enabled !== false;
            const statusText = String(row.manual_exit_execution_status || row.manual_exit_status || '').replace(/_/g, ' ').toLowerCase();
            const title = canExit
                ? (manualExitPrice > 0
                    ? 'Submit dashboard exit for this manual signal'
                    : 'Submit MARKET exit; quote unavailable, broker fill will reconcile')
                : `Exit unavailable${statusText ? `: ${statusText}` : ''}`;
            return `<div class="trade-control-stack"><button class="trade-control-btn exit" data-action="close-trade" data-trade-id="${escHtml(manualExitTradeId)}" data-trade-price="${manualExitPrice}" ${canExit ? '' : 'disabled'} title="${escHtml(title)}">EXIT</button></div>`;
        }
    }
    if (isSignalRow && !isExitSignalRow) {
        const signalId = row.signal_id || row.id || '';
        const lifecycleId = row.lifecycle_id || '';
        const canEnter = !!row.accepted_by_gate && !row.watch_only && !!(signalId || lifecycleId);
        return `<div class="trade-control-stack"><button class="trade-control-btn entry" data-action="execute-signal" data-signal-id="${escHtml(signalId)}" data-lifecycle-id="${escHtml(lifecycleId)}" ${canEnter ? '' : 'disabled'} title="${canEnter ? 'Execute accepted manual signal' : 'Entry unavailable until scanner accepts the signal'}">ENTRY</button></div>`;
    }
    if (isSignalRow && isExitSignalRow) {
        return '<div class="trade-control-stack"><button class="trade-control-btn exit" disabled title="Exit signal already recorded">EXIT</button></div>';
    }
    return '<span style="opacity:0.35">--</span>';
}

function updateTradesPanel(trades, simId) {
    const tbody = document.getElementById('tradesBody');
    if (!tbody) return;
    const panelTotals = tradeHistoryTotals(trades || {});
    const pageInfo = getTradePageInfo(trades || {});
    const backendSummary = trades.summary || {};
    const modeBadge = document.getElementById('tradeDataMode');
    if (modeBadge) {
        const meta = trades.meta || {};
        const restored = meta.restored_signal_rows || 0;
        const simulated = meta.simulated_signal_rows || 0;
        const managerRows = (meta.open_rows || 0) + (meta.closed_rows || 0);
        const source = String(meta.source || '').toLowerCase();
        const expectedRows = restored + simulated || panelTotals.total;
        const visibleLabel = panelTotals.total > panelTotals.visible
            ? ` page ${pageInfo.page}/${pageInfo.pageCount}, showing ${panelTotals.visible}/${panelTotals.total}`
            : '';
        let modeText = `TRADE MANAGER (${managerRows})`;
        let modeClass = 'trade-data-mode live';
        if (backendSummary.is_signal_tracker === true) {
            modeText = `SESSION SIGNAL TRACKER (${restored || panelTotals.total}${visibleLabel})`;
            modeClass = 'trade-data-mode restored';
        } else if (source === 'historical_session_plus_simulation') {
            modeText = `BACKTEST (${expectedRows}${visibleLabel})`;
            modeClass = 'trade-data-mode restored';
        } else if (source === 'restored_session_signals') {
            modeText = `RESTORED SESSION SIGNALS (${restored || panelTotals.total}${visibleLabel})`;
            modeClass = 'trade-data-mode restored';
        } else if (restored && !managerRows) {
            modeText = `RESTORED SESSION SIGNALS (${restored}${visibleLabel})`;
            modeClass = 'trade-data-mode restored';
        }
        modeBadge.textContent = modeText;
        modeBadge.className = modeClass;
    }
    renderTradePagination(trades || {});

    // Show a calm resimulation notice after the initial bind. Avoid panel glow loops.
    if (simId && simId !== state.lastSimId) {
        const previousSimId = state.lastSimId;
        state.lastSimId = simId;
        const indicator = document.getElementById('syncIndicator');
        if (previousSimId && indicator) {
            indicator.innerText = 'RE-SIMULATION COMPLETE';
            indicator.style.color = 'var(--buy)';
            indicator.style.opacity = '1';
            setTimeout(() => {
                indicator.innerText = 'System Synchronized';
                indicator.style.color = 'inherit';
                indicator.style.opacity = '0.6';
            }, 3000);
        }
    }

    const open = (trades.open || []).map(row => ({ ...row, row_kind: row.row_kind || 'manager_open' }));
    const closed = (trades.closed || []).map(row => ({ ...row, row_kind: row.row_kind || 'manager_closed' }));
    const signalRows = collectSessionSignalRows([...open, ...closed], trades.signals || []);
    // Keep the table in reverse-chronological order by actual accepted entry time.
    const all = [...signalRows, ...open, ...closed]
        .map((row, index) => ({ ...row, __renderIndex: index }))
        .sort((a, b) => {
            const tsDiff = getTradeRowEventTimestamp(b) - getTradeRowEventTimestamp(a);
            if (tsDiff !== 0) return tsDiff;
            const rankDiff = getTradeRowSortRank(a) - getTradeRowSortRank(b);
            if (rankDiff !== 0) return rankDiff;
            return (a.__renderIndex || 0) - (b.__renderIndex || 0);
        });
    const isPagedHistory = pageInfo.pageCount > 1 && panelTotals.total > panelTotals.visible;
    const summary = isPagedHistory && trades.summary
        ? trades.summary
        : buildSessionSummary(trades.summary || {}, open, closed, signalRows);
    const makeTradeRowKey = (row, fallbackIndex) => String(row.id || `${row.row_kind || 'row'}:${row.entry_timestamp || row.exit_timestamp || fallbackIndex}`);

    if (all.length === 0) {
        setHtml(tbody, '<tr><td colspan="15" style="text-align:center; padding:20px; opacity:0.5">No trades found. Scanner searching...</td></tr>');
    } else {
        const newKeys = new Set(all.map((t, index) => makeTradeRowKey(t, index)));
        // Remove dead rows
        Array.from(tbody.children).forEach(row => {
            const rowKey = row.getAttribute('data-key');
            if (rowKey && !newKeys.has(rowKey) && rowKey !== 'empty') {
                row.remove();
            }
        });
        // Clear empty state if it exists
        if (tbody.children.length === 1 && !tbody.children[0].hasAttribute('data-key')) {
            tbody.innerHTML = '';
        }

        all.forEach((t, index) => {
            const isSignalRow = t.row_kind === 'signal';
            const isClosedTradeRow = t.row_kind === 'manager_closed' || t.status === 'CLOSED';
            const executionStatus = String(t.execution_status || t.status || '').toUpperCase();
            const executionPending = ['ENTRY_PENDING', 'ENTRY_SUBMITTED', 'EXIT_PENDING', 'RECOVERY_REQUIRED', 'PARTIAL'].includes(executionStatus);
            const executionRetryable = ['EXIT_FAILED', 'PARTIAL_EXIT'].includes(executionStatus);
            const pnl = accountingTradePnl(t);
            const pnlClass = pnl >= 0 ? 'green' : 'red';
            const sideColor = t.direction === 'LONG' ? 'var(--buy)' : 'var(--sell)';
            
            const isOpt = t.inst_type === 'OPT' || t.instrument.includes('CE') || t.instrument.includes('PE');
            let typeLabel = 'FUT';
            let typeClass = 'fut-badge';
            if (isOpt) {
                // The fallback used to be `instrument.match(/\d+/)[0]` — the FIRST run of
                // digits — which on a monthly symbol is the expiry YEAR, not the strike:
                // NIFTY26AUG24350CE rendered as "26CE". Take the digits immediately
                // before the CE/PE suffix instead, and prefer the server's atm_strike,
                // which trade_dashboard.py now parses properly for orphan rows too.
                const symText = String(t.trading_symbol || t.instrument || '');
                const tailMatch = symText.match(/(\d+)(?:CE|PE)$/);
                const strike = t.atm_strike || (tailMatch ? tailMatch[1] : '');
                const optType = t.option_type || (t.instrument.includes('CE') ? 'CE' : 'PE');
                typeLabel = `${strike}${optType}`;
                typeClass = optType === 'CE' ? 'ce-badge' : 'pe-badge';
            }
            const dirLabel = isOpt
                ? (t.trade_side || `BUY ${t.option_type || (t.instrument.includes('PE') ? 'PE' : 'CE')}`)
                : t.direction;

            const conf = (t.confidence || 0) * 100;
            const gradeText = t.grade || '--';
            const actionText = String(t.action || '').toUpperCase();
            const statusText = String(t.status || '').toUpperCase();
            const isExitSignalRow = actionText === 'EXIT' || statusText.indexOf('EXIT') !== -1;
            const entryPriceValue = Number(t.entry_price || t.price || 0);
            const stopValue = Number(t.trailing_stop || t.stop || t.current_stop || 0);
            const rrText = formatRiskReward(t);
            const targetValue = isExitSignalRow ? 0 : Number(t.target || 0);
            const targetPrice = targetValue ? formatPrice(targetValue) : '--';
            const stopPrice = stopValue ? formatPrice(stopValue) : '--';
            const mergedPriceHtml = renderLtpExitCell(t, isClosedTradeRow, isExitSignalRow);
            
            const entryStamp = t.entry_timestamp || t.timestamp || t.signal_timestamp;
            const exitStamp = t.exit_timestamp;
            const entryClock = normalizeTradeDateTime(t.entry_time, t.entry_date, entryStamp);
            const exitClock = normalizeTradeDateTime(t.exit_time, t.exit_date, exitStamp);
            const eTimeStr = entryClock.timeText
                ? `<div class="time-col-wrapper"><span>${escHtml(entryClock.timeText)}</span><span style="font-size:0.65rem; opacity:0.5">${escHtml(entryClock.dateText)}</span></div>`
                : '--:--:--';
            const xTimeStr = exitClock.timeText
                ? `<div class="time-col-wrapper"><span>${escHtml(exitClock.timeText)}</span><span style="font-size:0.65rem; opacity:0.5">${escHtml(exitClock.dateText)}</span></div>`
                : '--:--:--';
            const pointsText = formatTradePoints(getTradePoints(t));
            const lotsText = t.lots ? `<span class="lots-chip">[${t.lots}]</span>` : '';
            const pnlText = `₹${pnl.toLocaleString()}${pointsText ? `<span class="points-chip">(${pointsText})</span>` : ''}${lotsText}`;
            const execLabel = t.exec_type || (isExitSignalRow ? 'EXIT' : 'A');
            const closedReason = escHtml((t.exit_reason || '').replace(/_/g, ' '));
            const closedReasonHtml = isExitSignalRow && closedReason
                ? `<span style="opacity:0.8; font-size:0.75rem">${closedReason}</span>`
                : `<span style="opacity:0.65; font-size:0.75rem">closed: ${closedReason}</span>`;
            const controlHtml = renderTradeControlCell(t, {
                isSignalRow,
                isExitSignalRow,
                isOpenTrade: t.status === 'OPEN',
                executionRetryable,
            });

            const rowHtml = `
                <td class="font-bold">${escHtml(baseInstrumentLabel(t.base_instrument || t.instrument))}</td>
                <td><span class="${typeClass}">${typeLabel}</span></td>
                <td style="color:${sideColor}; font-weight:bold">${dirLabel}</td>
                <td>${formatPrice(entryPriceValue)}</td>
                <td class="ltp-cell">${mergedPriceHtml}</td>
                <td style="opacity:0.5">${targetPrice}</td>
                <td style="color:var(--orange); opacity:0.6">${stopPrice}</td>
                <td class="rr-cell">${rrText}</td>
                <td class="${pnlClass} font-bold pnl-points-cell">${pnlText}</td>
                <td>
                    <div class="conf-grade-cell">
                        <span class="conf-chip grade-${gradeText.replace('+', 'p').replace(/[^a-zA-Z0-9]/g, '-')}" title="Grade ${gradeText} (grade is a label for this confidence)">${conf.toFixed(0)}%</span>
                        <span style="font-size:0.8rem; opacity:0.6">${t.timeframe || '5min'}</span>
                    </div>
                </td>
                <td><span class="exe-badge ${execLabel === 'A' ? 'exe-a' : 'exe-m'}">${execLabel}</span></td>
                <td class="font-mono">
                    <span class="time-val">${eTimeStr}</span>
                </td>
                <td class="font-mono">
                    <span class="time-val">${xTimeStr}</span>
                </td>
                <td>${controlHtml}</td>
                <td>
                    ${t.is_ghost ? `<div style="color:var(--orange); font-size:0.75rem; font-weight:bold; margin-bottom:4px; text-align:center;">[WATCH]</div>` : ''}
                    <div style="text-align:center;">
                    ${isSignalRow ?
                        `<span style="opacity:0.55; font-size:0.75rem">${t.status === 'EXIT_SIGNAL' ? ((t.exit_reason || '').replace(/_/g, ' ') || 'exit signal') : (t.status === 'NO_ENTRY' ? 'no entry' : 'signal')}</span>` :
                        t.status === 'ORPHANED' ?
                        `<span style="color:var(--sell); font-size:0.75rem">broker orphan</span>` :
                        t.status === 'OPEN' && executionRetryable ?
                        `<span style="color:var(--sell); font-size:0.72rem; font-weight:700">${executionStatus.replace(/_/g, ' ').toLowerCase()}</span>` :
                        t.status === 'OPEN' && executionPending ?
                        `<span style="opacity:0.75; font-size:0.75rem">${executionStatus.replace(/_/g, ' ').toLowerCase()}</span>` :
                        t.status === 'OPEN' ?
                        `<span style="opacity:0.65; font-size:0.75rem">open</span>` :
                        closedReasonHtml
                    }
                    </div>
                </td>
            `;
            
            const rowClass = `${t.status === 'OPEN' || t.status === 'SIGNAL' ? 'active-row' : ''} ${isSignalRow ? 'signal-row' : ''} ${isClosedTradeRow ? 'closed-trade-row' : ''}`;
            const rowKey = makeTradeRowKey(t, index);
            
            let tr = tbody.querySelector(`tr[data-key="${rowKey}"]`);
            if (!tr) {
                tr = document.createElement('tr');
                tr.setAttribute('data-key', rowKey);
                tr.className = rowClass;
                patchRow(tr, rowHtml);
                
                if (tbody.children[index]) {
                    tbody.insertBefore(tr, tbody.children[index]);
                } else {
                    tbody.appendChild(tr);
                }
            } else {
                tr.className = rowClass;
                patchRow(tr, rowHtml);
                // Ensure order
                if (tbody.children[index] !== tr) {
                    tbody.insertBefore(tr, tbody.children[index]);
                }
            }
        });
    }

    // The headline total must say WHICH LANE produced it.
    //
    // A restart outside 09:00-15:30 refuses REAL and comes up HISTORICAL, so the header
    // was showing a backtest replay as "DAILY P&L". On 2026-08-31 that rendered +Rs 6,065
    // for a session whose live result was -Rs 1,106.50. The lane is decided server-side
    // in TradeManager.get_dashboard_payload; this only renders it.
    const isReplay = summary.is_replay === true;
    const isSignalTracker = summary.is_signal_tracker === true;
    const isNonMoney = isReplay || isSignalTracker || summary.pnl_is_money === false;
    const nonMoneyTitle = isSignalTracker
        ? 'SIGNAL TRACKER - hypothetical signal marks; no broker fill is implied'
        : isReplay
            ? 'BACKTEST REPLAY - simulated, not live money'
            : '';
    const pnlEl = document.getElementById('dailyPnl');
    if (pnlEl) {
        const dp = summary.daily_pnl || 0;
        pnlEl.textContent = '₹' + dp.toLocaleString();
        pnlEl.className = 'h-summary-value '
            + (isNonMoney ? 'replay-value nonmoney-value' : (dp >= 0 ? 'green' : 'red'));
        pnlEl.title = nonMoneyTitle;
    }
    const pnlLabelEl = document.getElementById('dailyPnlLabel');
    if (pnlLabelEl) {
        pnlLabelEl.textContent = (summary.pnl_label || 'DAILY P&L') + ':';
        pnlLabelEl.className = 'h-summary-label' + (isNonMoney ? ' replay-label nonmoney-label' : '');
        pnlLabelEl.title = pnlEl ? pnlEl.title : '';
    }
    
    const futPnlEl = document.getElementById('futPnl');
    if (futPnlEl) {
        const fp = summary.fut_pnl || 0;
        futPnlEl.textContent = '₹' + fp.toLocaleString();
        futPnlEl.className = 'h-summary-value ' + (isNonMoney ? 'nonmoney-value' : (fp >= 0 ? 'green' : 'red'));
        futPnlEl.title = nonMoneyTitle;
    }
    
    const optPnlEl = document.getElementById('optPnl');
    if (optPnlEl) {
        const op = summary.opt_pnl || 0;
        optPnlEl.textContent = '₹' + op.toLocaleString();
        optPnlEl.className = 'h-summary-value ' + (isNonMoney ? 'nonmoney-value' : (op >= 0 ? 'green' : 'red'));
        optPnlEl.title = nonMoneyTitle;
    }
    setText('totalTrades', summary.total_trades || 0);
    setText('winLoss', `${summary.wins || 0}/${summary.losses || 0}`);
    if (document.getElementById('futOptTrades')) {
        setText('futOptTrades', `FUT: ${summary.fut_trades || 0} / OPT: ${summary.opt_trades || 0}`);
    }
    setText('perfWinRate', `WR: ${(summary.win_rate || 0).toFixed(1)}%`);
    setText('perfPF', `PF: ${(summary.profit_factor || 0).toFixed(2)}`);
    setText('perfSR', `SR: ${formatTradeTableSharpeDailyYearly(summary)}`);
    setText('perfDD', `DD: ₹${(summary.max_drawdown || 0).toLocaleString('en-IN', {maximumFractionDigits: 0})}`);
}

function updateSignalsFeed(instruments) {
    const tbody = document.getElementById('tradesBody');
    if (!tbody) return;

    const currentRows = Array.from(tbody.querySelectorAll('tr'));
    const hasRealTradeRows = currentRows.some(row => row.cells.length > 1 && !/No (active )?trades/i.test(row.innerText));
    if (hasRealTradeRows) return;

    const byKey = new Map();
    Object.values(instruments || {}).forEach(inst => {
        (inst.trade_candidates || []).forEach(sig => {
            const key = `${sig.instrument}_${sig.timeframe}_${sig.action || 'ENTRY'}_${sig.direction}_${sig.trading_symbol || sig.atm_strike || 'FUT'}`;
            byKey.set(key, { ...sig, source: 'candidate' });
        });
    });

    const signalsArray = Array.from(byKey.values())
        .sort((a, b) => {
            if (a.source === 'candidate' && b.source !== 'candidate') return -1;
            if (a.source !== 'candidate' && b.source === 'candidate') return 1;
            return new Date(b.timestamp || 0) - new Date(a.timestamp || 0);
        });
        
    const isHistorical = state.latestData && state.latestData.mode === 'HISTORICAL';
    const signalsLimited = isHistorical ? signalsArray.slice(0, 50000) : signalsArray.slice(0, 1000);
    const signals = signalsLimited;

    if (signals.length === 0) {
        setHtml(tbody, '<tr><td colspan="15" style="text-align:center; padding:20px; opacity:0.5">No final trade signals yet — scanner is analysing filters...</td></tr>');
        return;
    }

    const newKeys = new Set(signals.map(s => s.instrument + s.timeframe + s.direction + s.timestamp));
    
    // Remove dead rows
    Array.from(tbody.children).forEach(row => {
        const rowKey = row.getAttribute('data-key');
        if (rowKey && !newKeys.has(rowKey) && rowKey !== 'empty') {
            row.remove();
        }
    });
    // Clear empty state if it exists
    if (tbody.children.length === 1 && !tbody.children[0].hasAttribute('data-key')) {
        tbody.innerHTML = '';
    }

    signals.forEach((sig, index) => {
        const isCandidate = sig.source === 'candidate';
        const isExit = sig.is_exit || sig.action === 'EXIT' || sig.status === 'EXIT SIGNAL';
        const direction = sig.direction || (sig.type === 'BUY' ? 'LONG' : 'SHORT');
        const sideColor = isExit ? 'var(--orange)' : (direction === 'LONG' ? 'var(--buy)' : 'var(--sell)');
        const conf = Math.round((sig.confidence || 0) * 100);
        const actionable = isCandidate || sig.is_actionable !== false;
        const grade = sig.grade || (actionable ? 'B' : 'C');
        const ts = sig.timestamp ? new Date(sig.timestamp) : null;
        const timeText = ts && !Number.isNaN(ts.getTime())
            ? ts.toLocaleTimeString('en-IN', { hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false })
            : '--:--';
        const reasons = (sig.reasons || []).join(', ');
        const rowClass = actionable ? 'active-row' : '';
        const exeLabel = isExit ? 'EXIT SIGNAL' : (sig.status || (actionable ? 'TRADE SIGNAL' : 'FILTERED'));
        const exeClass = isExit ? 'exe-m' : (actionable ? 'exe-a' : 'exe-m');
        const isOpt = sig.inst_type === 'OPT';
        const typeLabel = isOpt
            ? `${sig.atm_strike || ''}${sig.option_type || ''}`
            : (sig.inst_type === 'FUT' ? (sig.trading_symbol || 'FUT') : '--');
        const typeClass = isOpt ? (sig.option_type === 'CE' ? 'ce-badge' : 'pe-badge') : 'fut-badge';
        const dirLabel = isOpt
            ? (sig.trade_side || `BUY ${sig.option_type || ''}`.trim())
            : direction;
        const entryPrice = sig.price;
        const currentPrice = sig.current_price || entryPrice;
        const stopPrice = sig.stop;
        const targetPrice = isExit ? '--' : sig.target;
        const mergedPriceHtml = renderLtpExitCell(
            { current_price: currentPrice, exit_price: isExit ? currentPrice : 0 },
            false,
            isExit,
        );
        const controlHtml = renderTradeControlCell({
            id: `${sig.instrument}_${sig.timeframe}_${sig.direction}_${sig.timestamp}`,
            signal_id: sig.id || '',
            lifecycle_id: sig.lifecycle_id || '',
            accepted_by_gate: !!sig.accepted_by_gate,
            watch_only: !!sig.watch_only,
            manual_entry_executed: !!sig.manual_entry_executed,
            manual_exit_trade_id: sig.manual_exit_trade_id || '',
            manual_exit_price: Number(sig.manual_exit_price || 0),
            manual_exit_enabled: sig.manual_exit_enabled === true,
            manual_exit_status: sig.manual_exit_status || '',
            manual_exit_execution_status: sig.manual_exit_execution_status || '',
            current_price: currentPrice,
            exit_price: isExit ? currentPrice : 0,
            entry_price: entryPrice,
        }, {
            isSignalRow: true,
            isExitSignalRow: isExit,
        });
        
        const rowHtml = `
            <td class="font-bold">${escHtml(baseInstrumentLabel(sig.instrument))}</td>
            <td><span class="${typeClass}">${typeLabel}</span></td>
            <td style="color:${sideColor}; font-weight:bold">${dirLabel}</td>
            <td>${entryPrice ? formatPrice(entryPrice) : '--'}</td>
            <td class="ltp-cell">${mergedPriceHtml}</td>
            <td style="opacity:0.5">${targetPrice ? formatPrice(targetPrice) : '--'}</td>
            <td style="color:var(--orange); opacity:0.6">${stopPrice ? formatPrice(stopPrice) : '--'}</td>
            <td class="rr-cell">${formatRiskReward(sig)}</td>
            <td class="font-bold" style="opacity:0.5">--</td>
            <td>
                <div class="conf-grade-cell">
                    <span class="conf-chip grade-${grade.replace('+', 'p').replace(/[^a-zA-Z0-9]/g, '-')}" title="Grade ${grade} (grade is a label for this confidence)">${conf}%</span>
                    <span style="font-size:0.8rem; opacity:0.6">${sig.timeframe || '5min'}</span>
                </div>
            </td>
            <td><span class="exe-badge ${exeClass}">${exeLabel}</span></td>
            <td class="font-mono">
                <span class="time-val">${timeText}</span>
            </td>
            <td class="font-mono">
                <span class="time-val">--:--</span>
            </td>
            <td>${controlHtml}</td>
            <td style="font-size:0.8rem; opacity:0.6" title="${reasons}">
                ${(sig.status || '').replace(/_/g, ' ') || 'SCANNING'}
            </td>
        `;

        const rowKey = sig.instrument + sig.timeframe + sig.direction + sig.timestamp;
        
        let tr = tbody.querySelector(`tr[data-key="${rowKey}"]`);
        if (!tr) {
            tr = document.createElement('tr');
            tr.setAttribute('data-key', rowKey);
            tr.className = rowClass;
            patchRow(tr, rowHtml);
            
            if (tbody.children[index]) {
                tbody.insertBefore(tr, tbody.children[index]);
            } else {
                tbody.appendChild(tr);
            }
        } else {
            tr.className = rowClass;
            patchRow(tr, rowHtml);
            if (tbody.children[index] !== tr) {
                tbody.insertBefore(tr, tbody.children[index]);
            }
        }
    });
}


function notifyTradeCommandResult(result, successMessage, blockedMessage) {
    const status = String((result && result.data && result.data.status) || '').toLowerCase();
    if (status === 'submitted' || status === 'ok') {
        if (window.showToast) window.showToast(successMessage, 'success');
        return;
    }
    const reason = result && result.data && result.data.reason ? `: ${result.data.reason}` : '';
    if (window.showToast) window.showToast(`${blockedMessage}${reason}`, 'warning');
}

function closeTrade(tradeId, price) {
    const result = window.sendCommand({ cmd: 'close_trade', trade_id: tradeId, price });
    if (result && typeof result.then === 'function') {
        result.then(msg => notifyTradeCommandResult(msg, 'Exit submitted', 'Exit blocked'));
    }
    return result;
}

function executeSignal(signalId, lifecycleId) {
    const result = window.sendCommand({ cmd: 'execute_signal', signal_id: signalId || '', lifecycle_id: lifecycleId || '' });
    if (result && typeof result.then === 'function') {
        result.then(msg => notifyTradeCommandResult(msg, 'Entry submitted', 'Entry blocked'));
    }
    return result;
}

window.closeTrade = closeTrade;
window.executeSignal = executeSignal;

// CSP-safe delegated handler for ENTRY / EXIT / RETRY buttons (replaces inline onclick,
// which a strict script-src CSP blocks). Works for dynamically re-rendered rows.
document.addEventListener('click', (e) => {
    const btn = e.target && e.target.closest && e.target.closest('[data-action="close-trade"], [data-action="execute-signal"]');
    if (!btn || btn.disabled) return;
    const action = btn.getAttribute('data-action');
    if (action === 'close-trade') {
        const id = btn.getAttribute('data-trade-id');
        const price = Number(btn.getAttribute('data-trade-price')) || 0;
        if (id) closeTrade(id, price);
        return;
    }
    if (action === 'execute-signal') {
        const signalId = btn.getAttribute('data-signal-id') || '';
        const lifecycleId = btn.getAttribute('data-lifecycle-id') || '';
        if (signalId || lifecycleId) executeSignal(signalId, lifecycleId);
    }
});
