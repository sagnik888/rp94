/* Chart lifecycle, streaming, rendering, and chart controls.
 * Requires TradingView Lightweight Charts v5.2.0 only (no v4 vendor path).
 */

function requireLightweightChartsV5() {
    const LWC = window.LightweightCharts;
    if (!LWC || typeof LWC.createChart !== 'function') {
        throw new Error('Lightweight Charts v5.2.0 not loaded (window.LightweightCharts missing)');
    }
    // v5 exposes series class defs + addSeries; v4 used addCandlestickSeries factories only.
    if (!LWC.CandlestickSeries || !LWC.LineSeries || !LWC.HistogramSeries) {
        throw new Error('Lightweight Charts v5 series types missing — expected 5.2.0 bundle');
    }
    return LWC;
}

function addChartSeries(chart, seriesDefinition, legacyFactoryName, options) {
    // Prefer v5 addSeries(SeriesType, options). Legacy factory kept only as hard-fail path.
    if (chart?.addSeries && seriesDefinition) {
        return chart.addSeries(seriesDefinition, options);
    }
    throw new Error(
        `Lightweight Charts v5.2.0 required (addSeries/${legacyFactoryName} unavailable). ` +
        'Remove any v4 script tags and load vendor/lightweight-charts-5.2.0 only.'
    );
}

function setCandleMarkers(markers = []) {
    const nextMarkers = Array.isArray(markers) ? markers : [];
    if (!state.candleSeries) return;

    if (LightweightCharts?.createSeriesMarkers) {
        if (!state.candleSeriesMarkers) {
            state.candleSeriesMarkers = LightweightCharts.createSeriesMarkers(state.candleSeries, nextMarkers);
        } else {
            state.candleSeriesMarkers.setMarkers(nextMarkers);
        }
        return;
    }

    if (state.candleSeries.setMarkers) {
        state.candleSeries.setMarkers(nextMarkers);
    }
}

function initChart() {
    const chartElement = document.getElementById('mainChart');
    if (!chartElement) return;

    const LightweightCharts = requireLightweightChartsV5();

    // ═══ PREMIUM CHART INITIALIZATION (LWC v5.2.0) ═══
    const chart = LightweightCharts.createChart(chartElement, {
        width: chartElement.clientWidth,
        height: chartElement.clientHeight,
        layout: {
            background: { type: 'solid', color: '#05070b' },
            textColor: 'rgba(226, 232, 240, 0.9)',
            fontSize: 12,
            fontFamily: "'JetBrains Mono', monospace",
        },
        grid: {
            vertLines: { color: 'rgba(148, 163, 184, 0.035)' },
            horzLines: { color: 'rgba(148, 163, 184, 0.035)' },
        },
        crosshair: {
            mode: LightweightCharts.CrosshairMode.Normal,
            vertLine: { color: '#555', labelBackgroundColor: '#111', width: 1 },
            horzLine: { color: '#555', labelBackgroundColor: '#111', width: 1 },
        },
        rightPriceScale: {
            // v5 rightPriceScale: autoScale tightens range to visible candles.
            borderColor: 'rgba(255, 255, 255, 0.1)',
            autoScale: true,
            scaleMargins: { top: 0.06, bottom: 0.26 }, // candles get the top ~70%, volume sits below
        },
        localization: {
            timeFormatter: (time) => {
                return new Date(time * 1000).toLocaleString('en-IN', {
                    timeZone: 'Asia/Kolkata',
                    day: '2-digit',
                    month: 'short',
                    hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false,
                });
            }
        },
        timeScale: {
            borderColor: 'rgba(255, 255, 255, 0.1)',
            timeVisible: true,
            secondsVisible: false,
            barSpacing: 28, // Wider, more visible candles (TradingView-like)
            tickMarkFormatter: (time, tickMarkType) => {
                const date = new Date(time * 1000);
                if (tickMarkType === 0 || tickMarkType === 1 || tickMarkType === 2) { // Year, Month, Day
                    return date.toLocaleDateString('en-IN', {
                        timeZone: 'Asia/Kolkata',
                        day: '2-digit',
                        month: 'short',
                    });
                }
                return date.toLocaleTimeString('en-IN', {
                    timeZone: 'Asia/Kolkata',
                    hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false,
                });
            }
        },
        handleScroll: true,
        handleScale: true,
    });
    
    state.chart = chart;

    // 1. Candlestick Series (Vibrant & Sharp)
    state.candleSeries = addChartSeries(chart, LightweightCharts.CandlestickSeries, 'addCandlestickSeries', {
        upColor: '#26a69a',
        downColor: '#ef5350',
        borderVisible: false,
        wickUpColor: '#26a69a',
        wickDownColor: '#ef5350',
        priceLineVisible: true,
        priceLineWidth: 2,
        priceLineColor: 'rgba(255, 255, 255, 0.8)',
    });
    state.candleSeriesMarkers = null;
    
    // 2. Volume Series (Subtle)
    state.volumeSeries = addChartSeries(chart, LightweightCharts.HistogramSeries, 'addHistogramSeries', {
        priceFormat: { type: 'volume' },
        priceScaleId: '',
        scaleMargins: { top: 0.88, bottom: 0 }
    });

    // 3. Trailing Stop Line (Thick Professional)
    state.tsLineSeries = addChartSeries(chart, LightweightCharts.LineSeries, 'addLineSeries', {
        color: '#f97316', // Bold Orange
        lineWidth: 3, // Thicker for visibility
        lineStyle: LightweightCharts.LineStyle.Dashed,
        crosshairMarkerVisible: false,
        lastValueVisible: true,
        priceLineVisible: false
    });

    // 3b. Engine-specific indicator overlays (EMA / Supertrend). A fixed pool of
    // solid line series reused across updates so switching the scanner engine swaps
    // the on-chart indicator without churning series.
    state.engineOverlaySeries = [];
    for (let i = 0; i < 3; i++) {
        state.engineOverlaySeries.push(addChartSeries(chart, LightweightCharts.LineSeries, 'addLineSeries', {
            color: '#888888',
            lineWidth: 2,
            crosshairMarkerVisible: false,
            lastValueVisible: true,
            priceLineVisible: false,
        }));
    }

    state.pcrLineSeries = addChartSeries(chart, LightweightCharts.LineSeries, 'addLineSeries', {
        color: '#38bdf8',
        lineWidth: 1,
        priceScaleId: 'intel',
        crosshairMarkerVisible: false,
        lastValueVisible: true,
        priceLineVisible: false,
    });
    state.ofrLineSeries = addChartSeries(chart, LightweightCharts.LineSeries, 'addLineSeries', {
        color: '#facc15',
        lineWidth: 1,
        priceScaleId: 'intel',
        crosshairMarkerVisible: false,
        lastValueVisible: true,
        priceLineVisible: false,
    });
    state.oiDeltaSeries = addChartSeries(chart, LightweightCharts.HistogramSeries, 'addHistogramSeries', {
        priceScaleId: 'intel',
        priceFormat: { type: 'volume' },
        scaleMargins: { top: 0.78, bottom: 0.02 },
    });
    state.supportWallSeries = addChartSeries(chart, LightweightCharts.LineSeries, 'addLineSeries', {
        color: 'rgba(34,197,94,0.75)',
        lineWidth: 1,
        lineStyle: LightweightCharts.LineStyle.Dashed,
        crosshairMarkerVisible: false,
        lastValueVisible: false,
        priceLineVisible: false,
    });
    state.resistanceWallSeries = addChartSeries(chart, LightweightCharts.LineSeries, 'addLineSeries', {
        color: 'rgba(239,68,68,0.75)',
        lineWidth: 1,
        lineStyle: LightweightCharts.LineStyle.Dashed,
        crosshairMarkerVisible: false,
        lastValueVisible: false,
        priceLineVisible: false,
    });
    
    const resizeChart = () => {
        if (state.chartResizeFrame) cancelAnimationFrame(state.chartResizeFrame);
        state.chartResizeFrame = requestAnimationFrame(() => {
            chart.applyOptions({ width: chartElement.clientWidth, height: chartElement.clientHeight });
            state.chartResizeFrame = null;
        });
    };
    if (window.ResizeObserver) {
        const observer = new ResizeObserver(resizeChart);
        observer.observe(chartElement);
        state.chartResizeObserver = observer;
    } else {
        window.addEventListener('resize', resizeChart);
    }
}


async function requestChartSnapshot(instrument, tf, reason = 'tab-change') {
    if (!state.chartStreamEnabled) return;
    if (state.chartRequestInFlight && reason === 'auto-refresh') return;
    state.pendingChartRequest = `${instrument}_${tf}_${Date.now()}`;
    if (window.sendCommand && reason !== 'auto-refresh') {
        window.sendCommand({ cmd: 'subscribe_chart', instrument, tf });
    }

    // REST fallback covers missed/stale WebSocket responses and makes tab
    // changes deterministic across all active indices x 3 timeframes.
    try {
        state.chartRequestInFlight = true;
        const token = (typeof getDashboardToken === 'function')
            ? await getDashboardToken()
            : (window.dashboardAuthToken || '');
        const headers = token ? { 'X-Dashboard-Token': token } : {};
        const response = await fetch(
            `/api/chart?instrument=${encodeURIComponent(instrument)}&tf=${encodeURIComponent(tf)}`,
            { cache: 'no-store', headers },
        );
        if (!response.ok) throw new Error(`Chart fetch failed: ${response.status}`);
        applyChartSnapshot(await response.json());
    } catch (error) {
        console.warn(`Chart snapshot failed (${reason})`, error);
    } finally {
        state.chartRequestInFlight = false;
    }
}

function applyChartSnapshot(snapshot) {
    if (!snapshot || snapshot.status === 'starting') return;
    if (snapshot.chart_enabled === false) {
        applyChartStreamState(false, { notifyBackend: false, requestSnapshot: false });
        return;
    }
    if (!state.chartStreamEnabled) return;
    const instrument = TICKER_MAP[snapshot.instrument] || snapshot.instrument || state.currentInstrument;
    if (!instrument || instrument !== state.currentInstrument) return;

    const current = state.latestData || {};
    const instruments = { ...(current.instruments || {}) };
    const inst = { ...(instruments[instrument] || {}) };
    inst.chart = { ...(inst.chart || {}), ...(snapshot.chart || {}) };
    inst.simulation_id = snapshot.simulation_id || current.simulation_id || 0;
    instruments[instrument] = inst;

    state.latestData = {
        ...current,
        instruments,
        simulation_id: snapshot.simulation_id || current.simulation_id || 0,
    };
    state.lastChartId = null;
    state.lastBarTime = null;
    state.lastBarKey = null;
    updateChart(inst);
    updateInfoPanel(inst);
}

function startChartRefreshLoop() {
    if (state.chartRefreshTimer) return;
    state.chartRefreshTimer = setInterval(() => {
        if (!state.chartStreamEnabled) return;
        if (document.hidden) return;
        const hasActiveChart = Boolean(state.latestData?.instruments?.[state.currentInstrument]);
        if (hasActiveChart) {
            requestChartSnapshot(state.currentInstrument, state.currentTimeframe, 'auto-refresh');
        }
    }, 3000);
}

function applyChartStreamState(enabled, options = {}) {
    state.chartStreamEnabled = enabled !== false;
    if (state.latestData) {
        state.latestData.config = {
            ...(state.latestData.config || {}),
            chart_stream_enabled: state.chartStreamEnabled,
        };
    }
    localStorage.setItem('ut_chart_stream_enabled', String(state.chartStreamEnabled));

    const toggle = document.getElementById('chartStreamSwitch');
    if (toggle && toggle.checked !== state.chartStreamEnabled) {
        toggle.checked = state.chartStreamEnabled;
    }

    const mainGrid = document.getElementById('mainGrid');
    const container = document.getElementById('mainChart');
    if (mainGrid) mainGrid.classList.toggle('chart-off', !state.chartStreamEnabled);
    if (container) {
        let placeholder = container.querySelector('.chart-disabled-placeholder');
        if (!state.chartStreamEnabled && !placeholder) {
            placeholder = document.createElement('div');
            placeholder.className = 'chart-disabled-placeholder';
            placeholder.textContent = 'Chart stream is paused. Trading, signals, intel, logs, and P&L are still running.';
            container.appendChild(placeholder);
        } else if (state.chartStreamEnabled && placeholder) {
            placeholder.remove();
        }
    }

    if (!state.chartStreamEnabled) {
        state.candleSeries?.setData([]);
        state.volumeSeries?.setData([]);
        state.tsLineSeries?.setData([]);
        (state.engineOverlaySeries || []).forEach(s => s?.setData([]));
        clearIntelOverlay();
        setCandleMarkers([]);
        state.lastChartId = null;
        state.lastBarTime = null;
        state.lastBarKey = null;

    } else if (options.requestSnapshot !== false) {
        requestChartSnapshot(state.currentInstrument, state.currentTimeframe, 'chart-stream-enabled');
    }

    if (options.notifyBackend !== false && window.sendCommand) {
        window.sendCommand({ cmd: 'set_chart_stream', enabled: state.chartStreamEnabled });
    }
}


function mergeChartData(currentChart, incomingChart) {
    if (!currentChart || !incomingChart) return incomingChart || currentChart;

    const incomingCandles = incomingChart.candles || [];
    const hasFullCurrent = (currentChart.candles || []).length > 1;
    const isIncremental = incomingCandles.length <= 1 && hasFullCurrent;
    if (!isIncremental) return incomingChart;

    return {
        ...currentChart,
        ...incomingChart,
        candles: upsertByTime(currentChart.candles || [], incomingCandles[0]),
        trailing_stop: upsertByTime(currentChart.trailing_stop || [], (incomingChart.trailing_stop || [])[0]),
        indicator_overlays: (incomingChart.indicator_overlays && incomingChart.indicator_overlays.length) ? incomingChart.indicator_overlays : (currentChart.indicator_overlays || []),
        markers: incomingChart.markers && incomingChart.markers.length ? incomingChart.markers : (currentChart.markers || []),
    };
}

function upsertByTime(series, point) {
    if (!point || point.time === undefined || point.time === null) return series;

    const next = series.slice();
    const last = next[next.length - 1];
    if (last && last.time === point.time) {
        next[next.length - 1] = point;
    } else if (!last || point.time > last.time) {
        next.push(point);
    }
    return next;
}


function updateChart(data) {
    if (!state.chartStreamEnabled) return;
    const chartData = data.chart || {};
    const tf = state.currentTimeframe;
    const instName = state.currentInstrument;
    const tfData = chartData[tf];

    if (!tfData || !tfData.candles || tfData.candles.length === 0) return;
    document.getElementById('mainChart')?.querySelector('.chart-disabled-placeholder')?.remove();

    // ═══ INCREMENTAL RENDERING OPTIMIZATION ═══
    const lastBar = tfData.candles[tfData.candles.length - 1];
    const chartId = `${instName}_${tf}_${data.simulation_id || 0}`;
    
    const lastBarKey = [
        lastBar.time,
        Number(lastBar.open || 0).toFixed(2),
        Number(lastBar.high || 0).toFixed(2),
        Number(lastBar.low || 0).toFixed(2),
        Number(lastBar.close || 0).toFixed(2),
        Number(lastBar.volume || 0),
        tfData.intel_history?.latest?.time || 0,
        Number(tfData.intel_history?.latest?.primary_pcr || 0).toFixed(3),
        Number(tfData.intel_history?.latest?.net_oi_change || 0),
    ].join('|');

    // Same candle timestamp can still move tick-by-tick; only skip if OHLCV is unchanged too.
    if (state.lastChartId === chartId && state.lastBarKey === lastBarKey) {
        return;
    }
    state.lastBarTime = lastBar.time;
    state.lastBarKey = lastBarKey;
    
    if (state.lastChartId !== chartId) {
        // Full Reset (Instrument/TF changed or Simulation Reset)
        const candles = tfData.candles.map(c => ({
            time: c.time, open: c.open, high: c.high, low: c.low, close: c.close,
        }));
        state.candleSeries.setData(candles);

        const volData = tfData.candles.map(c => ({
            time: c.time, value: c.volume || 0,
            color: c.close >= c.open ? 'rgba(34,197,94,0.5)' : 'rgba(239,68,68,0.5)',
        }));
        state.volumeSeries.setData(volData);

        if (tfData.trailing_stop && tfData.trailing_stop.length > 0) {
            const tsData = tfData.trailing_stop.map(t => ({
                time: t.time, value: t.value || 0
            }));
            state.tsLineSeries.setData(tsData);
        } else {
            state.tsLineSeries.setData([]);
        }

        // Engine-specific indicator overlays (EMA / Supertrend). Reuse the pool:
        // set data+color for active overlays, clear the rest. Switching engine swaps them.
        const overlays = tfData.indicator_overlays || [];
        (state.engineOverlaySeries || []).forEach((series, i) => {
            const ov = overlays[i];
            if (ov && ov.data && ov.data.length) {
                series.applyOptions({ color: ov.color || '#888888', title: ov.name || '' });
                series.setData(ov.data.map(p => ({ time: p.time, value: p.value })));
            } else {
                series.setData([]);
            }
        });
        syncIntelOverlay(tfData, true);
        
        // Clear markers on full sync to prevent ghosting
        setCandleMarkers(tfData.markers);
        
        state.lastChartId = chartId;
        // console.log(`📊 Full Chart Sync: ${chartId}`);
    } else {
        // Incremental Update (The "Fast Path")
        state.candleSeries.update({
            time: lastBar.time,
            open: lastBar.open,
            high: lastBar.high,
            low: lastBar.low,
            close: lastBar.close
        });
        
        state.volumeSeries.update({
            time: lastBar.time,
            value: lastBar.volume || 0, // Null-Coalescing Fix
            color: lastBar.close >= lastBar.open ? 'rgba(34,197,94,0.5)' : 'rgba(239,68,68,0.5)'
        });

        if (tfData.trailing_stop && tfData.trailing_stop.length > 0) {
            const lastTS = tfData.trailing_stop[tfData.trailing_stop.length - 1];
            state.tsLineSeries.update({
                time: lastTS.time,
                value: lastTS.value
            });
        }
        syncIntelOverlay(tfData, false);

        // Always sync markers on incremental update if they exist
        setCandleMarkers(tfData.markers);
    }

}

function syncIntelOverlay(tfData, fullSync) {
    const intel = tfData.intel_history || {};
    const pcr = Array.isArray(intel.pcr) ? intel.pcr.filter(p => Number.isFinite(Number(p.value))) : [];
    const ofr = Array.isArray(intel.ofr) ? intel.ofr.filter(p => Number.isFinite(Number(p.value))) : [];
    const oiDelta = Array.isArray(intel.oi_delta) ? intel.oi_delta : [];
    const support = Array.isArray(intel.support) ? intel.support.filter(p => Number(p.value) > 0) : [];
    const resistance = Array.isArray(intel.resistance) ? intel.resistance.filter(p => Number(p.value) > 0) : [];

    if (fullSync) {
        state.pcrLineSeries?.setData(pcr);
        state.ofrLineSeries?.setData(ofr);
        state.oiDeltaSeries?.setData(oiDelta);
        state.supportWallSeries?.setData(support);
        state.resistanceWallSeries?.setData(resistance);
        return;
    }

    const updateLast = (series, points) => {
        if (!series || !points.length) return;
        const last = points[points.length - 1];
        if (last && last.time !== undefined && Number.isFinite(Number(last.value))) {
            series.update(last);
        }
    };
    updateLast(state.pcrLineSeries, pcr);
    updateLast(state.ofrLineSeries, ofr);
    updateLast(state.oiDeltaSeries, oiDelta);
    updateLast(state.supportWallSeries, support);
    updateLast(state.resistanceWallSeries, resistance);
}

function clearIntelOverlay() {
    state.pcrLineSeries?.setData([]);
    state.ofrLineSeries?.setData([]);
    state.oiDeltaSeries?.setData([]);
    state.supportWallSeries?.setData([]);
    state.resistanceWallSeries?.setData([]);
}


function setupChartEventListeners() {
    document.querySelectorAll('.inst-tab').forEach(tab => {
        tab.addEventListener('click', () => {
            document.querySelectorAll('.inst-tab').forEach(t => t.classList.remove('active'));
            tab.classList.add('active');
            state.currentInstrument = tab.dataset.instrument;
            const instEl = document.getElementById('chartInstrument');
            if (instEl) instEl.textContent = state.currentInstrument;
            
            window.sendCommand({
                cmd: 'subscribe_chart',
                instrument: state.currentInstrument,
                tf: state.currentTimeframe
            });

            // Explicitly Clear Series for Anti-Ghosting
            state.candleSeries.setData([]);
            state.volumeSeries.setData([]);
            state.tsLineSeries.setData([]);
            clearIntelOverlay();
            setCandleMarkers([]);
            
            // Force Full Re-render on next updateChart call
            state.lastChartId = null;
            state.lastBarTime = null;
            state.lastBarKey = null;
            
            // Safety: Clear series immediately
            if (state.candleSeries) state.candleSeries.setData([]);
            if (state.volumeSeries) state.volumeSeries.setData([]);
            if (state.tsLineSeries) state.tsLineSeries.setData([]);
            clearIntelOverlay();
            
            if (state.latestData.instruments) {
                updateDashboard(state.latestData);
            }
            requestChartSnapshot(state.currentInstrument, state.currentTimeframe, 'instrument-tab');
        });
    });
    
    document.querySelectorAll('.tf-tab').forEach(tab => {
        tab.addEventListener('click', () => {
            document.querySelectorAll('.tf-tab').forEach(t => t.classList.remove('active'));
            tab.classList.add('active');
            state.currentTimeframe = tab.dataset.tf;
            const tfEl = document.getElementById('chartTimeframe');
            if (tfEl) tfEl.textContent = state.currentTimeframe;
            
            window.sendCommand({
                cmd: 'subscribe_chart',
                instrument: state.currentInstrument,
                tf: state.currentTimeframe
            });

            // Explicitly Clear Series for Anti-Ghosting
            state.candleSeries.setData([]);
            state.volumeSeries.setData([]);
            state.tsLineSeries.setData([]);
            clearIntelOverlay();
            setCandleMarkers([]);

            // Force Full Re-render
            state.lastChartId = null;
            state.lastBarTime = null;
            state.lastBarKey = null;

            // Safety: Clear series immediately
            if (state.candleSeries) state.candleSeries.setData([]);
            if (state.volumeSeries) state.volumeSeries.setData([]);
            if (state.tsLineSeries) state.tsLineSeries.setData([]);
            clearIntelOverlay();

            if (state.latestData.instruments) {
                updateDashboard(state.latestData);
            }
            requestChartSnapshot(state.currentInstrument, state.currentTimeframe, 'timeframe-tab');
        });
    });

    const chartStreamSwitch = document.getElementById('chartStreamSwitch');
    if (chartStreamSwitch && chartStreamSwitch.dataset.listenerReady !== '1') {
        chartStreamSwitch.dataset.listenerReady = '1';
        chartStreamSwitch.addEventListener('change', () => {
            applyChartStreamState(chartStreamSwitch.checked, { notifyBackend: true, requestSnapshot: true });
            showToast(`Chart Stream: ${chartStreamSwitch.checked ? 'ON' : 'OFF'}`, chartStreamSwitch.checked ? 'info' : 'warning');
        });
    }
}
