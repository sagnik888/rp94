/* Trade history pagination. Loaded before app.js as a classic script. */

function tradeHistoryTotals(trades) {
    const closedTotal = Number(trades.closed_total ?? (trades.closed || []).length) || 0;
    const signalsTotal = Number(trades.signals_total ?? (trades.signals || []).length) || 0;
    const closedVisible = (trades.closed || []).length;
    const signalsVisible = (trades.signals || []).length;
    const summaryTotal = Number((trades.summary || {}).total_trades || 0) || 0;
    const windowTotal = Number((trades.window || {}).total || 0) || 0;
    const duplicateLedger = summaryTotal > 0 && closedTotal > 0 && signalsTotal > 0 && Math.max(closedTotal, signalsTotal) === summaryTotal;
    const visible = duplicateLedger ? Math.max(closedVisible, signalsVisible) : closedVisible + signalsVisible;
    const total = windowTotal || (duplicateLedger
        ? summaryTotal
        : Math.max(summaryTotal, closedTotal + signalsTotal));
    return { closedTotal, signalsTotal, total, visible };
}

function tradePayloadContextKey(data = {}, trades = {}) {
    const meta = trades.meta || {};
    const totals = tradeHistoryTotals(trades);
    return [
        String(data.mode || data.config?.mode || meta.mode || '').toUpperCase(),
        String(data.simulation_id || meta.simulation_id || ''),
        String(meta.source || ''),
        String(meta.backtest_days || data.config?.backtest_days || ''),
        String(meta.inst_pref || data.config?.inst_pref || ''),
        String(meta.grade_preference || data.config?.signal_grade_preference || ''),
        String(meta.timeframe_entry_policy || data.config?.ut_timeframe_entry_policy || ''),
        String(meta.ut_concurrency_guard ?? data.config?.ut_concurrency_guard ?? ''),
        String(totals.total || 0),
    ].join('|');
}

function syncTradePaginationState(data, trades) {
    if (!trades || typeof trades !== 'object') return;
    const key = tradePayloadContextKey(data || {}, trades);
    if (state.tradePagination.key !== key) {
        state.tradePagination.key = key;
        state.tradePagination.page = 1;
    }
    const windowInfo = trades.window || {};
    const pageSize = Number(windowInfo.limit || windowInfo.page_size || state.tradePagination.pageSize || 100) || 100;
    state.tradePagination.pageSize = Math.max(1, Math.min(2000, pageSize));
    const offset = Number(windowInfo.offset || 0) || 0;
    state.tradePagination.page = Math.max(1, Math.floor(offset / state.tradePagination.pageSize) + 1);
}

function getTradePageInfo(trades) {
    const totals = tradeHistoryTotals(trades || {});
    const windowInfo = (trades || {}).window || {};
    const pageSize = Math.max(1, Number(windowInfo.limit || windowInfo.page_size || state.tradePagination.pageSize || 100) || 100);
    const pageCount = Math.max(1, Number(windowInfo.page_count || Math.ceil((totals.total || totals.visible || 1) / pageSize)) || 1);
    const offset = Math.max(0, Number(windowInfo.offset || (state.tradePagination.page - 1) * pageSize) || 0);
    const page = Math.max(1, Math.min(pageCount, Number(windowInfo.page || Math.floor(offset / pageSize) + 1) || 1));
    return { page, pageSize, pageCount, total: totals.total, visible: totals.visible, offset };
}

function paginationSequence(current, total) {
    if (total <= 7) return Array.from({ length: total }, (_, idx) => idx + 1);
    const pages = new Set([1, total]);
    const start = current <= 4 ? 2 : Math.max(2, current - 2);
    const end = current <= 4 ? Math.min(total - 1, 6) : Math.min(total - 1, current + 2);
    for (let page = start; page <= end; page += 1) pages.add(page);
    return Array.from(pages).sort((a, b) => a - b).reduce((acc, page) => {
        if (acc.length && page - acc[acc.length - 1] > 1) acc.push('...');
        acc.push(page);
        return acc;
    }, []);
}

async function loadTradePage(page) {
    const latest = state.latestData || {};
    const currentTrades = latest.trades || {};
    const info = getTradePageInfo(currentTrades);
    const targetPage = Math.max(1, Math.min(info.pageCount, Number(page) || 1));
    if (state.tradePagination.inFlight || targetPage === info.page) return;

    state.tradePagination.inFlight = true;
    renderTradePagination(currentTrades);
    try {
        const offset = (targetPage - 1) * info.pageSize;
        const token = (typeof getDashboardToken === 'function')
            ? await getDashboardToken()
            : (window.dashboardAuthToken || '');
        const headers = token ? { 'X-Dashboard-Token': token } : {};
        const response = await fetch(
            `/api/trades?limit=${info.pageSize}&offset=${offset}`,
            { cache: 'no-store', headers },
        );
        if (!response.ok) throw new Error(`Trade page fetch failed: ${response.status}`);
        const pagePayload = await response.json();
        const currentLatest = state.latestData || latest;
        state.latestData = {
            ...currentLatest,
            trades: {
                ...(currentLatest.trades || {}),
                ...pagePayload,
            },
            simulation_id: currentLatest.simulation_id,
        };
        syncTradePaginationState(state.latestData, state.latestData.trades || {});
        updateTradesPanel(state.latestData.trades || {}, latest.simulation_id);
        updatePnlPanel(state.latestData.trades || {});

    } catch (error) {
        console.warn('Trade page load failed', error);
    } finally {
        state.tradePagination.inFlight = false;
        renderTradePagination((state.latestData && state.latestData.trades) || currentTrades);
    }
}

function renderTradePagination(trades) {
    const container = document.getElementById('tradePagination');
    if (!container) return;
    const info = getTradePageInfo(trades || {});
    if (info.pageCount <= 1) {
        container.innerHTML = '';
        container.classList.remove('active');
        return;
    }

    const startRow = info.visible > 0 && info.total ? info.offset + 1 : 0;
    const endRow = info.visible > 0 ? Math.min(info.total || info.visible, info.offset + info.visible) : 0;
    const busy = state.tradePagination.inFlight;
    const buttons = paginationSequence(info.page, info.pageCount).map(item => {
        if (item === '...') return '<span class="trade-page-ellipsis">...</span>';
        const active = item === info.page ? ' active' : '';
        return `<button type="button" class="trade-page-btn${active}" data-page="${item}" ${busy ? 'disabled' : ''}>${item}</button>`;
    }).join('');
    container.classList.add('active');
    container.innerHTML = `
        <span class="trade-page-status">${startRow}-${endRow} / ${info.total}</span>
        <div class="trade-page-buttons">
            <button type="button" class="trade-page-btn trade-page-prev" data-page="${info.page - 1}" ${info.page <= 1 || busy ? 'disabled' : ''}>Prev</button>
            ${buttons}
            <button type="button" class="trade-page-btn trade-page-next" data-page="${info.page + 1}" ${info.page >= info.pageCount || busy ? 'disabled' : ''}>Next ›</button>
        </div>
    `;
    container.querySelectorAll('button[data-page]').forEach(button => {
        button.addEventListener('click', () => loadTradePage(Number(button.dataset.page)));
    });
}

