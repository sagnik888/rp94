(function() {
    function createForensicsController() {
        const model = {
            items: [],
            nextCursor: null,
            total: 0,
            signalTotal: 0,
            latestId: 0,
            sessionDate: '',
            availableDates: [],
            autoFollow: true,
            drawerOpen: false,
            drawerData: null,
            drawerOrigin: null,
        };

        let rootElement = null;
        let pollInterval = null;
        let inflight = false;

        function esc(value) {
            if (value === null || value === undefined) return '';
            return String(value)
                .replace(/&/g, '&amp;')
                .replace(/</g, '&lt;')
                .replace(/>/g, '&gt;')
                .replace(/"/g, '&quot;')
                .replace(/'/g, '&#39;');
        }

        function badge(text, tone) {
            return `<span class="sf-chip sf-chip-${tone}">${esc(text)}</span>`;
        }

        function toneForDecision(d) {
            const pos = ['ACCEPTED', 'FILLED', 'OPEN', 'PARTIAL_EXIT'];
            const neg = ['REJECTED', 'CLOSED', 'CANCELLED'];
            const warn = ['PENDING', 'SUBMITTED', 'PROPOSED'];
            if (pos.includes(d)) return 'pos';
            if (neg.includes(d)) return 'neg';
            if (warn.includes(d)) return 'warn';
            return 'neutral';
        }

        function toneForPhase(p) {
            const pos = ['ENTERED', 'FILLED', 'TRAILING', 'EXITED'];
            const neg = ['REJECTED', 'DISCARDED', 'TIMED_OUT'];
            const warn = ['STABILIZING', 'ORDER_PENDING', 'ORDER_SUBMITTED', 'STOP_MOVED', 'TARGET_MOVED', 'MARK_TO_MARKET'];
            if (pos.includes(p)) return 'pos';
            if (neg.includes(p)) return 'neg';
            if (warn.includes(p)) return 'warn';
            return 'neutral';
        }

        function formatIST(iso) {
            if (!iso) return '';
            const m = iso.match(/T(\d{2}:\d{2}:\d{2}(?:\.\d{3})?)/);
            return m ? m[1] : iso;
        }

        function formatDelta(initial, current) {
            if (initial === null || initial === undefined) initial = '-';
            if (current === null || current === undefined) current = '-';
            if (initial === current) return esc(initial);
            return `${esc(initial)} &rarr; ${esc(current)}`;
        }

        function formatPnL(pnl, peak, dd) {
            if (pnl === null || pnl === undefined) return '-';
            const tone = Number(pnl) >= 0 ? 'pos' : 'neg';
            const safePeak = peak !== null && peak !== undefined ? peak : '-';
            const safeDd = dd !== null && dd !== undefined ? dd : '-';
            return `<span class="sf-pnl sf-pnl-${tone}">&#8377;${esc(pnl)}</span><small class="sf-sub">pk &#8377;${esc(safePeak)} &middot; dd &#8377;${esc(safeDd)}</small>`;
        }

        function populateDateSelect(current, dates) {
            const select = rootElement.querySelector('#sfFilterDate');
            if (!select || !dates || !dates.length) return;
            const prev = select.value;
            select.innerHTML = dates.map((d, i) =>
                `<option value="${esc(d)}">${i === 0 ? 'Current' : 'Previous'} \u00b7 ${esc(d)}</option>`
            ).join('');
            // Restore previous selection if it still exists
            if (prev && Array.from(select.options).some(o => o.value === prev)) {
                select.value = prev;
            } else if (current && Array.from(select.options).some(o => o.value === current)) {
                select.value = current;
            }
        }

        function renderSummary() {
            const evEl = rootElement.querySelector('#sfStatEvents');
            const sigEl = rootElement.querySelector('#sfStatSignals');
            const idEl = rootElement.querySelector('#sfStatLatest');
            const timeEl = rootElement.querySelector('#sfLiveTime');
            if (evEl) evEl.textContent = model.total.toLocaleString('en-IN');
            if (sigEl) sigEl.textContent = model.signalTotal.toLocaleString('en-IN');
            if (idEl) idEl.textContent = model.latestId || '—';
            if (timeEl && model.items.length > 0) {
                timeEl.textContent = 'Latest ' + formatIST(model.items[0].event_ts);
            }
        }

        function renderRows(append) {
            const tbody = rootElement.querySelector('.sf-ledger tbody');
            if (!tbody) return;

            if (!append) tbody.innerHTML = '';

            if (model.items.length === 0 && !append) {
                tbody.innerHTML = '<tr><td colspan="14" class="sf-empty">No items found</td></tr>';
            } else {
                let html = '';
                model.items.forEach(item => {
                    const dirTone = item.direction === 'LONG' ? 'pos' : (item.direction === 'SHORT' ? 'neg' : 'neutral');

                    html += `<tr tabindex="0" data-lifecycle="${esc(item.lifecycle_id)}">
                        <td class="sf-mono">${esc(formatIST(item.event_ts))}</td>
                        <td>
                            <button class="sf-btn-link sf-open-drawer" data-lifecycle-id="${esc(item.lifecycle_id)}">
                                <strong>${esc(item.signal_id || item.lifecycle_id)}</strong><small class="sf-sub">${esc(item.lifecycle_id)}</small>
                            </button>
                        </td>
                        <td><strong>${esc(item.instrument)}</strong><small class="sf-sub">${esc(item.contract || 'No contract')}</small></td>
                        <td>${esc(item.timeframe)}</td>
                        <td>${badge(item.direction, dirTone)}</td>
                        <td>${badge(item.phase, toneForPhase(item.phase))}</td>
                        <td>${badge(item.decision, toneForDecision(item.decision))}</td>
                        <td title="${esc(item.reason_text)}"><strong>${esc(item.reason_code)}</strong><br><small class="sf-sub">${esc(item.reason_text)}</small></td>
                        <td>${esc(item.source)}<br><small class="sf-sub">${esc(item.engine)}/${esc(item.lane)}</small></td>
                        <td>${badge(item.grade, 'neutral')}<br><small class="sf-sub">${esc(item.confidence)}%</small></td>
                        <td>${esc(item.ltp)}<br><small class="sf-sub">${esc(item.entry_price)}</small></td>
                        <td>${formatDelta(item.initial_stop, item.current_stop)}</td>
                        <td>${formatDelta(item.initial_target, item.current_target)}</td>
                        <td>${formatPnL(item.pnl, item.peak_pnl, item.drawdown)}</td>
                    </tr>`;
                });

                if (append) {
                    tbody.insertAdjacentHTML('beforeend', html);
                } else {
                    tbody.innerHTML = html;
                }
            }

            const btnLoadMore = rootElement.querySelector('.sf-load-more');
            if (btnLoadMore) {
                if (model.nextCursor) {
                    btnLoadMore.style.display = 'block';
                    btnLoadMore.dataset.cursor = model.nextCursor;
                } else {
                    btnLoadMore.style.display = 'none';
                }
            }
        }

        async function fetchLedger({append = false, cursor = null} = {}) {
            if (inflight) return;
            inflight = true;

            try {
                const dateEl = rootElement.querySelector('#sfFilterDate');
                const instrumentEl = rootElement.querySelector('#sfFilterInstrument');
                const tfEl = rootElement.querySelector('#sfFilterTimeframe');
                const phaseEl = rootElement.querySelector('#sfFilterPhase');
                const decisionEl = rootElement.querySelector('#sfFilterDecision');
                const searchEl = rootElement.querySelector('#sfFilterSearch');

                const params = new URLSearchParams();
                if (dateEl && dateEl.value) params.append('date', dateEl.value);
                if (instrumentEl && instrumentEl.value) params.append('instrument', instrumentEl.value);
                if (tfEl && tfEl.value) params.append('timeframe', tfEl.value);
                if (phaseEl && phaseEl.value) params.append('phase', phaseEl.value);
                if (decisionEl && decisionEl.value) params.append('decision', decisionEl.value);
                if (searchEl && searchEl.value) params.append('search', searchEl.value);
                params.append('limit', '100');

                if (cursor) {
                    params.append('cursor', cursor);
                }

                const response = await (typeof authFetch === 'function' ? authFetch : fetch)('/api/signal-forensics?' + params.toString(), { cache: 'no-store' });
                if (!response.ok) throw new Error('HTTP error ' + response.status);

                const data = await response.json();

                model.nextCursor = data.next_cursor || null;
                model.total = data.total || 0;
                model.signalTotal = data.signal_total || 0;
                model.latestId = data.latest_event_id || 0;

                if (data.session_date) {
                    model.sessionDate = data.session_date;
                }
                if (data.available_dates) {
                    model.availableDates = data.available_dates;
                    populateDateSelect(model.sessionDate, model.availableDates);
                }

                if (append) {
                    model.items = model.items.concat(data.items || []);
                } else {
                    model.items = data.items || [];
                }

                renderSummary();
                renderRows(append);

            } catch (err) {
                console.error('Error fetching ledger:', err);
            } finally {
                inflight = false;
            }
        }

        async function openDrawer(lifecycleId, triggerElement) {
            model.drawerOrigin = triggerElement;
            model.drawerOpen = true;

            const drawer = rootElement.querySelector('#sfDrawer');
            const drawerBody = rootElement.querySelector('#sfDrawerBody');
            const drawerTitle = rootElement.querySelector('#sfDrawerTitle');
            if (!drawer || !drawerBody) return;

            drawer.classList.add('sf-drawer-open');
            if (drawerTitle) drawerTitle.textContent = lifecycleId;
            drawerBody.innerHTML = '<div class="sf-loading">Loading timeline\u2026</div>';

            try {
                const response = await (typeof authFetch === 'function' ? authFetch : fetch)('/api/signal-forensics/' + encodeURIComponent(lifecycleId), { cache: 'no-store' });
                if (!response.ok) throw new Error('HTTP error ' + response.status);

                const data = await response.json();
                model.drawerData = data;

                if (data.status === 'error') {
                    drawerBody.innerHTML = '<div class="sf-error">' + esc(data.message || 'Lifecycle not found') + '</div>';
                    return;
                }

                const events = data.events || [];
                const first = events[0] || {};

                // Identity grid
                let html = '<div class="sf-identity-grid">';
                html += '<div><span>INSTRUMENT</span><strong>' + esc(first.instrument || '—') + '</strong></div>';
                html += '<div><span>CONTRACT / TOKEN</span><strong>' + esc(first.contract || '—') + ' · ' + esc(first.symbol_token || '—') + '</strong></div>';
                html += '<div><span>ENGINE / LANE</span><strong>' + esc(first.engine || '—') + ' · ' + esc(first.lane || '—') + '</strong></div>';
                html += '<div><span>DIRECTION / TF</span><strong>' + esc(first.direction || '—') + ' · ' + esc(first.timeframe || '—') + '</strong></div>';
                html += '</div>';

                // Timeline rail
                html += '<ol class="sf-timeline-rail">';
                events.forEach((ev, idx) => {
                    html += `<li class="sf-timeline-item">
                        <time class="sf-timeline-time">${esc(formatIST(ev.event_ts))}</time>
                        <div>
                            <b>${badge(ev.phase, toneForPhase(ev.phase))} ${badge(ev.decision, toneForDecision(ev.decision))}</b>
                            <p class="sf-timeline-reason">${esc(ev.reason_text || 'No additional reason')}</p>
                            <small class="sf-timeline-source">${esc(ev.source || 'Unknown source')}</small>
                        </div>
                    </li>`;
                });
                html += '</ol>';

                // Raw payload from last event
                const last = events[events.length - 1] || {};
                if (last.payload && Object.keys(last.payload).length > 0) {
                    html += '<details class="sf-raw-json"><summary>Raw structured evidence</summary>';
                    html += '<pre>' + esc(JSON.stringify(last.payload, null, 2)) + '</pre></details>';
                }

                drawerBody.innerHTML = html;

            } catch (err) {
                console.error('Error fetching drawer data:', err);
                drawerBody.innerHTML = '<div class="sf-error">Failed to load lifecycle timeline.</div>';
            }
        }

        function closeDrawer() {
            model.drawerOpen = false;
            const drawer = rootElement.querySelector('#sfDrawer');
            if (drawer) {
                drawer.classList.remove('sf-drawer-open');
            }
            if (model.drawerOrigin) {
                model.drawerOrigin.focus();
                model.drawerOrigin = null;
            }
        }

        function exportCsv() {
            if (!model.items || model.items.length === 0) {
                alert('No data to export.');
                return;
            }

            const headers = [
                'event_id', 'session_date', 'event_ts', 'lifecycle_id', 'signal_id', 'instrument',
                'contract', 'symbol_token', 'timeframe', 'direction', 'phase', 'decision',
                'reason_code', 'reason_text', 'source', 'grade', 'confidence', 'engine', 'lane',
                'ltp', 'entry_price', 'initial_stop', 'current_stop', 'initial_target', 'current_target',
                'pnl', 'peak_pnl', 'drawdown', 'quote_source', 'quote_verified'
            ];

            let csv = headers.join(',') + '\\n';
            model.items.forEach(item => {
                const row = headers.map(h => {
                    let val = item[h];
                    if (val === null || val === undefined) val = '';
                    val = String(val).replace(/"/g, '""');
                    return '"' + val + '"';
                });
                csv += row.join(',') + '\\n';
            });

            const blob = new Blob([csv], { type: 'text/csv;charset=utf-8;' });
            const url = URL.createObjectURL(blob);
            const a = document.createElement('a');
            a.href = url;
            a.download = 'signal_forensics_' + (model.sessionDate || 'export') + '.csv';
            document.body.appendChild(a);
            a.click();
            document.body.removeChild(a);
            URL.revokeObjectURL(url);
        }

        function exportJson() {
            if (!model.items || model.items.length === 0) {
                alert('No data to export.');
                return;
            }
            const blob = new Blob([JSON.stringify(model.items, null, 2)], { type: 'application/json' });
            const url = URL.createObjectURL(blob);
            const a = document.createElement('a');
            a.href = url;
            a.download = 'signal_forensics_' + (model.sessionDate || 'export') + '.json';
            document.body.appendChild(a);
            a.click();
            document.body.removeChild(a);
            URL.revokeObjectURL(url);
        }

        function startPolling() {
            if (pollInterval) clearInterval(pollInterval);
            pollInterval = setInterval(() => {
                if (model.autoFollow && !model.drawerOpen && isVisible()) {
                    fetchLedger();
                }
            }, 5000);
        }

        function stopPolling() {
            if (pollInterval) {
                clearInterval(pollInterval);
                pollInterval = null;
            }
        }

        function isVisible() {
            return rootElement && rootElement.style.display !== 'none';
        }

        function attachEvents() {
            const btnApply = rootElement.querySelector('#sfBtnApply');
            if (btnApply) btnApply.addEventListener('click', () => fetchLedger());

            const btnRefresh = rootElement.querySelector('#sfBtnRefresh');
            if (btnRefresh) btnRefresh.addEventListener('click', () => fetchLedger());

            const btnClear = rootElement.querySelector('#sfBtnClear');
            if (btnClear) btnClear.addEventListener('click', () => {
                const selects = rootElement.querySelectorAll('select');
                selects.forEach(s => s.selectedIndex = 0);
                const search = rootElement.querySelector('#sfFilterSearch');
                if (search) search.value = '';
                fetchLedger();
            });

            const btnExportCsv = rootElement.querySelector('#sfBtnExportCsv');
            if (btnExportCsv) btnExportCsv.addEventListener('click', () => exportCsv());

            const btnExportJson = rootElement.querySelector('#sfBtnExportJson');
            if (btnExportJson) btnExportJson.addEventListener('click', () => exportJson());

            const chkAutoFollow = rootElement.querySelector('#sfAutoFollow');
            if (chkAutoFollow) {
                chkAutoFollow.checked = model.autoFollow;
                chkAutoFollow.addEventListener('change', (e) => {
                    model.autoFollow = e.target.checked;
                });
            }

            rootElement.addEventListener('click', (e) => {
                const loadMore = e.target.closest('.sf-load-more');
                if (loadMore) {
                    const cursor = loadMore.dataset.cursor;
                    if (cursor) {
                        fetchLedger({append: true, cursor: cursor});
                    }
                    return;
                }

                const openDrawerBtn = e.target.closest('.sf-open-drawer');
                if (openDrawerBtn) {
                    const lcId = openDrawerBtn.dataset.lifecycleId;
                    if (lcId) {
                        openDrawer(lcId, openDrawerBtn);
                    }
                    return;
                }

                const closeDrawerBtn = e.target.closest('.sf-drawer-close');
                if (closeDrawerBtn) {
                    closeDrawer();
                    return;
                }
            });

            document.addEventListener('keydown', (e) => {
                if (e.key === 'Escape' && model.drawerOpen) {
                    closeDrawer();
                }
            });
        }

        function init(options) {
            rootElement = (options && options.pane) ? options.pane : document.body;
            attachEvents();
            fetchLedger();
            startPolling();
        }

        function destroy() {
            stopPolling();
        }

        return {
            init: init,
            destroy: destroy
        };
    }

    window.SignalForensics = createForensicsController();
})();
