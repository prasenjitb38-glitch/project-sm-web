(function () {
    const chart = window.smChart;
    const candle = window.smSeries;

    const STORAGE_KEY = 'projectSmAnalysisScan';
    let analysisOpen = false;
    let chartHome = null;
    let extraSeries = [];
    let priceLine = null;
    let scanTimer = null;
    let catalogReady = false;
    let lastPayload = null;
    let scanRows = [];
    let replay = null;
    let replayCursor = 0;
    let replayTimer = null;
    let fibLines = [];
    let sessionLine = null;
    let alertTimer = null;
    let fibDrawings = [];
    let fibMode = '';
    let fibDraft = null;
    let selectedFib = -1;
    let drawnFibLines = [];
    let lastFibSymbol = '';
    let lastTrades = [];
    let paperTrades = [];
    let tradeLines = [];
    let pendingTrade = null;
    let backtestTimer = null;
    let analysisRequest = 0;
    const FIB_RATIOS = [0, 0.236, 0.382, 0.5, 0.618, 0.786, 1];
    const overlayFlags = {
        ema: true, ema200: true, sma: true, bb: true, supertrend: true, vwap: true,
        volume: true, rsi: true, macd: true, zones: true, fib: false,
    };

    function esc(value) {
        return String(value ?? '').replace(/[&<>"']/g, function (ch) {
            return ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[ch];
        });
    }

    function inr(value) {
        if (value === null || value === undefined || !Number.isFinite(Number(value))) return '—';
        return '₹' + Number(value).toLocaleString('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
    }

    function timeframeLabel(value) {
        const labels = { '5m': '5 Min candles', '15m': '15 Min candles', '1h': '1 Hour candles', '4h': '4 Hour candles', '6h': '6H', '12h': '12H', '1d': 'Daily candles', '1wk': 'Weekly candles', '1mo': 'Monthly candles', '3mo': '3M', '6mo': '6M', '1y': '1Y', '5y': '5Y' };
        return labels[value] || String(value || '').toUpperCase();
    }

    function readSaved() {
        try {
            const saved = JSON.parse(localStorage.getItem(STORAGE_KEY) || '{}');
            return {
                universe: saved.universe || 'nifty50',
                logic: saved.logic === 'OR' ? 'OR' : 'AND',
                timeframe: saved.timeframe || '1d',
                limit: saved.limit || '',
                conditions: Array.isArray(saved.conditions) ? saved.conditions : [],
                params: saved.params && typeof saved.params === 'object' ? saved.params : {},
            };
        } catch (error) {
            return { universe: 'nifty50', logic: 'AND', timeframe: '1d', limit: '', conditions: [], params: {} };
        }
    }

    function selectedConditions() {
        return Array.from(document.querySelectorAll('#analysisFilters input[type="checkbox"]:checked')).map(function (node) { return node.value; });
    }

    function scanParams() {
        const params = {};
        document.querySelectorAll('#analysisFilters input[data-param]').forEach(function (node) {
            const text = node.value.trim();
            params[node.dataset.param] = text === '' || !Number.isFinite(Number(text)) ? null : Number(text);
        });
        return params;
    }

    function saveScan() {
        const logicNode = document.querySelector('input[name="analysisLogic"]:checked');
        const limit = document.getElementById('analysisLimit').value.trim();
        localStorage.setItem(STORAGE_KEY, JSON.stringify({
            universe: document.getElementById('analysisUniverse').value,
            logic: logicNode ? logicNode.value : 'AND',
            timeframe: document.getElementById('analysisTimeframe').value,
            limit: limit,
            conditions: selectedConditions(),
            params: catalogReady ? scanParams() : readSaved().params,
        }));
    }

    function setStatus(message) {
        document.getElementById('analysisScanStatus').textContent = message;
        const found = document.getElementById('analysisFound');
        if (found) found.title = message;
    }

    function clearOverlays() {
        extraSeries.forEach(function (series) {
            try { chart.removeSeries(series); } catch (error) { /* already removed */ }
        });
        extraSeries = [];
        if (priceLine) {
            try { candle.removePriceLine(priceLine); } catch (error) { /* already removed */ }
            priceLine = null;
        }
        fibLines.forEach(function (line) {
            try { candle.removePriceLine(line); } catch (error) { /* already removed */ }
        });
        fibLines = [];
        if (sessionLine) {
            try { candle.removePriceLine(sessionLine); } catch (error) { /* already removed */ }
            sessionLine = null;
        }
        chart.priceScale('right').applyOptions({ scaleMargins: { top: 0.2, bottom: 0.1 } });
    }

    function ascending(points) {
        const clean = [];
        let last = -1;
        (points || []).forEach(function (point) {
            if (!point || !Number.isFinite(point.time) || point.time <= last) return;
            if (point.value !== undefined && !Number.isFinite(Number(point.value))) return;
            clean.push(point);
            last = point.time;
        });
        return clean;
    }

    function addLine(points, options) {
        const data = ascending(points);
        if (data.length < 2) return null;
        const series = chart.addLineSeries({
            color: options.color,
            lineWidth: options.width || 1,
            lineStyle: options.style || 0,
            priceLineVisible: false,
            lastValueVisible: false,
            crosshairMarkerVisible: false,
            priceScaleId: options.scale || 'right',
            title: '',
        });
        series.setData(data);
        if (options.scale && options.margins) {
            chart.priceScale(options.scale).applyOptions({ scaleMargins: options.margins, borderVisible: false });
        }
        extraSeries.push(series);
        return series;
    }

    function addHistogram(points, scale, margins) {
        const data = ascending(points);
        if (!data.length) return null;
        const series = chart.addHistogramSeries({
            priceScaleId: scale,
            priceLineVisible: false,
            lastValueVisible: false,
            priceFormat: scale === 'volume' ? { type: 'volume' } : { type: 'price', precision: 2, minMove: 0.01 },
        });
        series.setData(data);
        chart.priceScale(scale).applyOptions({ scaleMargins: margins, borderVisible: false });
        extraSeries.push(series);
        return series;
    }

    function applyOverlays(overlays, price) {
        clearOverlays();
        const showVolume = overlayFlags.volume && overlays.volume && overlays.volume.length;
        const showRsi = overlayFlags.rsi && overlays.rsi && overlays.rsi.length;
        const showMacd = overlayFlags.macd && ((overlays.macd && overlays.macd.length) || (overlays.macd_hist && overlays.macd_hist.length));
        const hasLower = showVolume || showRsi || showMacd;
        chart.priceScale('right').applyOptions({
            scaleMargins: hasLower ? { top: 0.04, bottom: 0.50 } : { top: 0.08, bottom: 0.08 },
        });
        const lines = [];
        const show = function (name, group) {
            if (overlayFlags[name] === undefined) return overlayFlags[group] !== false;
            return !!overlayFlags[name];
        };
        if (show('ema20', 'ema')) lines.push(['ema20', '#f5c542', 0]);
        if (show('ema50', 'ema')) lines.push(['ema50', '#60a5fa', 0]);
        if (show('ema200', 'ema')) lines.push(['ema200', '#f472b6', 0]);
        if (show('sma50', 'sma')) lines.push(['sma50', '#c4b5fd', 2]);
        if (show('sma200', 'sma')) lines.push(['sma200', '#fdba74', 2]);
        if (overlayFlags.bb) {
            lines.push(['bb_upper', 'rgba(125,211,252,0.9)', 0], ['bb_mid', 'rgba(125,211,252,0.45)', 2], ['bb_lower', 'rgba(125,211,252,0.9)', 0]);
        }
        if (overlayFlags.vwap) lines.push(['vwap', '#f9a8d4', 0]);
        lines.forEach(function (item) {
            try { addLine(overlays[item[0]], { color: item[1], style: item[2] }); } catch (error) { /* skip a series the chart rejects */ }
        });
        if (overlayFlags.supertrend) {
            try { addLine(overlays.supertrend_up, { color: '#34d399', width: 2 }); } catch (error) { /* no bullish supertrend */ }
            try { addLine(overlays.supertrend_down, { color: '#fb7185', width: 2 }); } catch (error) { /* no bearish supertrend */ }
        }
        if (showVolume) {
            try { addHistogram(overlays.volume, 'volume', { top: 0.535, bottom: 0.39 }); } catch (error) { /* volume scale unavailable */ }
        }
        if (showRsi) {
            try {
                const rsi = addLine(overlays.rsi, { color: '#c084fc', scale: 'rsi', margins: { top: 0.635, bottom: 0.25 } });
                if (rsi) {
                    rsi.createPriceLine({ price: 70, color: 'rgba(148,163,184,0.7)', lineWidth: 1, lineStyle: 2, axisLabelVisible: false, title: '' });
                    rsi.createPriceLine({ price: 30, color: 'rgba(148,163,184,0.7)', lineWidth: 1, lineStyle: 2, axisLabelVisible: false, title: '' });
                }
            } catch (error) { /* RSI pane unavailable */ }
        }
        if (showMacd) {
            try {
                addLine(overlays.macd, { color: '#38bdf8', scale: 'macd', margins: { top: 0.79, bottom: 0.03 } });
                addLine(overlays.macd_signal, { color: '#fb923c', scale: 'macd', margins: { top: 0.79, bottom: 0.03 } });
                addHistogram(overlays.macd_hist, 'macd', { top: 0.79, bottom: 0.03 });
            } catch (error) { /* MACD pane unavailable */ }
        }
        if (Number.isFinite(Number(price))) {
            priceLine = candle.createPriceLine({
                price: Number(price),
                color: '#f8fafc',
                lineWidth: 1,
                lineStyle: 2,
                axisLabelVisible: true,
                title: 'Price',
            });
        }
    }

    function lastValue(points) {
        for (let index = (points || []).length - 1; index >= 0; index -= 1) {
            const value = Number(points[index] && points[index].value);
            if (Number.isFinite(value)) return value;
        }
        return null;
    }

    function fmt(value, digits) {
        if (value === null || value === undefined || !Number.isFinite(Number(value))) return '—';
        return Number(value).toLocaleString('en-IN', { minimumFractionDigits: digits === undefined ? 2 : digits, maximumFractionDigits: digits === undefined ? 2 : digits });
    }

    function compact(value) {
        if (!Number.isFinite(Number(value))) return '—';
        const number = Number(value);
        if (number >= 1e7) return (number / 1e7).toFixed(2) + ' Cr';
        if (number >= 1e5) return (number / 1e5).toFixed(2) + ' L';
        return number.toLocaleString('en-IN', { maximumFractionDigits: 0 });
    }

    function renderLegend(payload) {
        const box = document.querySelector('#chartArea .chart-box');
        if (!box) return;
        box.querySelectorAll('.sa-pane-label,.sa-pane-sep').forEach(function (node) { node.remove(); });
        const legend = document.getElementById('saLegend');
        if (!legend) return;
        if (!payload) {
            legend.innerHTML = '';
            return;
        }
        const overlays = payload.overlays || {};
        const quote = payload.quote || {};
        const price = Number(payload.price);
        const change = Number(quote.change);
        const pct = Number(quote.change_pct);
        const changeText = Number.isFinite(change)
            ? '<b class="' + (change > 0 ? 'positive' : change < 0 ? 'negative' : '') + '">' + (change > 0 ? '+' : '') + fmt(change) + (Number.isFinite(pct) ? ' (' + (pct > 0 ? '+' : '') + pct.toFixed(2) + '%)' : '') + '</b>'
            : '';
        const lines = ['<span class="sa-lg-head">' + esc(payload.symbol || window.smGetSymbol()) + ' · ' + esc(timeframeLabel(payload.timeframe)) + ' · NSE &nbsp;<em>O</em>' + fmt(quote.open) + ' <em>H</em>' + fmt(quote.high) + ' <em>L</em>' + fmt(quote.low) + ' <em>C</em>' + fmt(price) + ' ' + changeText + '</span>'];
        const add = function (flag, label, color, value) {
            if (!flag || value === null) return;
            lines.push('<span><i style="background:' + color + '"></i>' + label + ' <b style="color:' + color + '">' + fmt(value) + '</b></span>');
        };
        add(overlayFlags.ema20 !== false && overlayFlags.ema !== false, 'EMA 20', '#f5c542', lastValue(overlays.ema20));
        add(overlayFlags.sma50 !== false && overlayFlags.sma !== false, 'SMA 50', '#c4b5fd', lastValue(overlays.sma50));
        add(overlayFlags.sma200 !== false && overlayFlags.sma !== false, 'SMA 200', '#fdba74', lastValue(overlays.sma200));
        if (overlayFlags.bb && lastValue(overlays.bb_mid) !== null) {
            lines.push('<span><i style="background:#7dd3fc"></i>BB 20 2 <b style="color:#7dd3fc">' + fmt(lastValue(overlays.bb_upper)) + ' ' + fmt(lastValue(overlays.bb_mid)) + ' ' + fmt(lastValue(overlays.bb_lower)) + '</b></span>');
        }
        legend.innerHTML = lines.join('');
        const pane = function (fraction, html) {
            const sep = document.createElement('div');
            sep.className = 'sa-pane-sep';
            sep.style.top = 'calc((100% - 26px) * ' + fraction + ')';
            box.appendChild(sep);
            const label = document.createElement('div');
            label.className = 'sa-pane-label';
            label.style.top = 'calc((100% - 26px) * ' + fraction + ' + 3px)';
            label.innerHTML = html;
            box.appendChild(label);
        };
        if (overlayFlags.volume && overlays.volume && overlays.volume.length) {
            pane(0.505, '<em>Volume</em><b>' + compact(lastValue(overlays.volume)) + '</b>');
        }
        if (overlayFlags.rsi && overlays.rsi && overlays.rsi.length) {
            pane(0.605, '<em>RSI 14</em><b style="color:#c084fc">' + fmt(lastValue(overlays.rsi)) + '</b>');
        }
        if (overlayFlags.macd && overlays.macd && overlays.macd.length) {
            pane(0.765, '<em>MACD 12 26 close 9</em><b style="color:#38bdf8">' + fmt(lastValue(overlays.macd)) + '</b> <b style="color:#fb923c">' + fmt(lastValue(overlays.macd_signal)) + '</b> <b>' + fmt(lastValue(overlays.macd_hist)) + '</b>');
        }
    }

    function drawSessionVwap(payload) {
        if (sessionLine) {
            try { candle.removePriceLine(sessionLine); } catch (error) { /* already removed */ }
            sessionLine = null;
        }
        if (!overlayFlags.vwap || !payload || payload.vwap_basis !== 'session') return;
        if (payload.overlays && payload.overlays.vwap && payload.overlays.vwap.length > 1) return;
        const row = (payload.technicals || []).find(function (item) { return item.name === 'VWAP'; });
        if (!row || !Number.isFinite(Number(row.value))) return;
        sessionLine = candle.createPriceLine({
            price: Number(row.value),
            color: '#f9a8d4',
            lineWidth: 1,
            lineStyle: 2,
            axisLabelVisible: true,
            title: 'Session VWAP',
        });
    }

    function drawFib(levels) {
        fibLines.forEach(function (line) {
            try { candle.removePriceLine(line); } catch (error) { /* already removed */ }
        });
        fibLines = [];
        if (!overlayFlags.fib) return;
        (levels || []).forEach(function (level) {
            if (!Number.isFinite(Number(level.price))) return;
            fibLines.push(candle.createPriceLine({
                price: Number(level.price),
                color: 'rgba(167,139,250,0.9)',
                lineWidth: 1,
                lineStyle: 2,
                axisLabelVisible: true,
                title: 'Auto ' + (level.label || 'Fib'),
            }));
        });
    }

    function filteredZones(zones) {
        const focus = window.smZoneFocus || 'all';
        if (focus === 'all') return zones || [];
        return (zones || []).filter(function (zone) {
            return (zone.timeframes || [zone.timeframe]).indexOf(focus) >= 0;
        });
    }

    function paintSelectedSignal(signal) {
        const node = document.getElementById('selectedSignal');
        if (!node) return;
        if (!signal || signal.data_unavailable) {
            node.textContent = 'Signal data is unavailable for this symbol.';
            return;
        }
        const reasons = (signal.reasons || []).slice(0, 6).join(' · ') || 'No supportive reasons on this bar.';
        node.textContent = (signal.signal || 'NEUTRAL') + ' · Score ' + (signal.strength === undefined ? '—' : signal.strength) +
            ' · Entry ' + inr(signal.entry) + ' · SL ' + inr(signal.stop_loss) +
            ' · T1 ' + inr(signal.target_1) + ' · T2 ' + inr(signal.target_2) +
            ' · R:R ' + (signal.risk_reward || '—') + ' · ' + reasons;
    }

    function tone(status) {
        const text = String(status || '');
        if (/bull/i.test(text)) return 'positive';
        if (/bear/i.test(text)) return 'negative';
        return '';
    }

    function renderZones(payload) {
        const focus = window.smZoneFocus || 'all';
        const slots = [
            ['daily', 'demand', 'Daily Demand'],
            ['weekly', 'demand', 'Weekly Demand'],
            ['monthly', 'demand', 'Monthly Demand'],
            ['daily', 'supply', 'Daily Supply'],
            ['weekly', 'supply', 'Weekly Supply'],
            ['monthly', 'supply', 'Monthly Supply'],
        ];
        const rows = [].concat(payload.demand || []).concat(payload.supply || []);
        const blocks = slots.map(function (slot) {
            const row = rows.find(function (item) {
                return String(item.label || '').toLowerCase().indexOf(slot[0]) === 0 && (item.kind === slot[1] || (!item.kind && slot[1] === 'demand'));
            }) || { label: slot[2], kind: slot[1], text: '' };
            const kind = slot[1];
            const range = row.text ? row.text : 'No nearby valid zone';
            const state = row.text
                ? [row.strength, row.status, row.tests === undefined || row.tests === null ? '' : row.tests + ' test(s)', row.distance_pct === null || row.distance_pct === undefined ? '' : Number(row.distance_pct).toFixed(2) + '% away'].filter(Boolean).join(' · ')
                : 'No valid zone within the volatility-based distance limit';
            const tf = slot[0] === 'daily' ? '1d' : slot[0] === 'weekly' ? '1wk' : '1mo';
            const focused = focus === tf;
            return '<div class="sa-zone ' + kind + (focused ? ' focus' : '') + (row.text ? '' : ' none') + '" title="' + esc(state) + '"><i></i><span>' + esc(row.label || slot[2]) + ' Zone</span><b>' + esc(range) + '</b></div>';
        });
        const feed = payload.zone_feed || {};
        const feedLine = feed.status ? '<p class="analysis-note">' + esc(feed.status) + ' · ' + esc(feed.data_status || '') + '</p>' : '';
        const messages = payload.zone_messages || {};
        const missing = ['demand', 'supply'].filter(function (kind) { return messages[kind]; }).map(function (kind) {
            return '<p class="analysis-note">' + esc(messages[kind]) + '</p>';
        }).join('');
        document.getElementById('analysisZones').innerHTML = feedLine + blocks.join('') + missing;
    }

    function renderTrends(rows) {
        const order = ['1mo', '1wk', '1d', '4h', '1h', '15m', '5m'];
        const names = { '1mo': 'Monthly', '1wk': 'Weekly', '1d': 'Daily', '4h': '4H', '1h': '1H', '15m': '15M', '5m': '5M' };
        const byKey = {};
        (rows || []).forEach(function (row) { byKey[row.timeframe] = row; });
        document.getElementById('analysisTrend').innerHTML = '<div class="sa-mtfgrid">' + order.map(function (key) {
            const row = byKey[key] || { status: 'Unavailable', detail: 'Not calculated' };
            const status = row.status || 'Unavailable';
            const arrow = /bull/i.test(status) ? '↑' : /bear/i.test(status) ? '↓' : /neutral/i.test(status) ? '→' : '·';
            const klass = tone(status) || (/neutral/i.test(status) ? 'amber' : '');
            return '<div title="' + esc(status + (row.detail ? ' · ' + row.detail : '')) + '"><span>' + names[key] + '</span><b class="' + klass + '">' + arrow + '</b></div>';
        }).join('') + '</div>';
    }

    function formatValue(row) {
        if (row.value === null || row.value === undefined || !Number.isFinite(Number(row.value))) return '—';
        if (row.name === 'Volume') return Number(row.value).toLocaleString('en-IN', { maximumFractionDigits: 0 });
        if (row.name === 'Relative Volume') return Number(row.value).toFixed(2) + '×';
        return Number(row.value).toLocaleString('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
    }

    function shortStatus(row) {
        const status = row.status || 'Unavailable';
        if (row.name === 'RSI' && /between 50 and 70/i.test(status)) return 'Positive';
        if (row.name === 'RSI' && /between 30 and 50/i.test(status)) return 'Negative';
        if (row.name === 'MACD' && status === 'Above signal') return 'Bullish';
        if (row.name === 'MACD' && status === 'Below signal') return 'Bearish';
        if (row.name === 'ADX' && status === 'Strong trend') return 'Strong';
        if (row.name === 'ADX' && status === 'Weak trend') return 'Weak';
        if (/price above/i.test(status)) return 'Price > ' + row.name.replace('EMA ', 'EMA').replace('SMA ', 'SMA');
        if (/price below/i.test(status)) return 'Price < ' + row.name;
        return status;
    }

    function renderTechnicals(rows) {
        const wanted = ['RSI', 'MACD', 'ADX', 'EMA 20', 'SMA 50', 'SMA 200', 'Supertrend', 'Bollinger Bands', 'ATR'];
        const extra = ['+DI', '-DI', 'Stochastic %K', 'Williams %R', 'CCI', 'OBV', 'Parabolic SAR', 'Ichimoku', 'VWAP', 'Relative Volume'];
        const byName = {};
        (rows || []).forEach(function (row) { byName[row.name] = row; });
        const line = function (name) {
            const row = byName[name] || { name: name, value: null, status: 'Unavailable' };
            const label = shortStatus(row);
            const klass = /bull|positive|strong|above|price >/i.test(label) ? 'positive' : /bear|negative|weak|below|price </i.test(label) ? 'negative' : 'amber';
            const title = name === 'RSI' ? 'RSI (14)' : name === 'EMA 20' ? '20 EMA' : name === 'SMA 50' ? '50 SMA' : name === 'SMA 200' ? '200 SMA' : name === 'Bollinger Bands' ? 'Bollinger Band' : name;
            return '<div class="sa-tech" title="' + esc(row.status || '') + '"><span>' + esc(title) + '</span><b>' + esc(formatValue(row)) + '</b><em class="' + klass + '">' + esc(label) + '</em></div>';
        };
        document.getElementById('analysisTechnicals').innerHTML = wanted.map(line).join('');
        const moreNode = document.getElementById('analysisTechnicalsMore');
        if (moreNode) moreNode.innerHTML = extra.map(line).join('');
        const readout = document.getElementById('chartReadout');
        if (!readout) return;
        const rsi = byName.RSI;
        const macd = byName.MACD;
        const volume = byName.Volume;
        const rvol = byName['Relative Volume'];
        const parts = [];
        if (rsi) parts.push('RSI 14 ' + formatValue(rsi));
        if (macd) parts.push('MACD ' + formatValue(macd) + (macd.status ? ' · ' + macd.status : ''));
        if (volume && Number.isFinite(Number(volume.value))) {
            let text = 'Volume ' + formatValue(volume);
            if (rvol && Number.isFinite(Number(rvol.value)) && Number(rvol.value) !== 0) {
                text += ' · Avg ' + (Number(volume.value) / Number(rvol.value)).toLocaleString('en-IN', { maximumFractionDigits: 0 });
                text += ' · RVOL ' + Number(rvol.value).toFixed(2) + '×';
            }
            parts.push(text);
        }
        readout.textContent = parts.join('   ');
    }

    function paintAnalysis(payload) {
        lastPayload = payload;
        const backtestSymbol = document.getElementById('backtestSymbol');
        if (backtestSymbol) backtestSymbol.value = payload.symbol || window.smGetSymbol() || backtestSymbol.value;
        document.getElementById('analysisSymbol').textContent = payload.symbol || window.smGetSymbol();
        document.getElementById('analysisPrice').textContent = inr(payload.price);
        const sidePrice = document.getElementById('sidePrice');
        if (sidePrice) sidePrice.textContent = inr(payload.price);
        const changeNode = document.getElementById('analysisChange');
        if (!Number.isFinite(Number(payload.change_pct))) {
            changeNode.textContent = '';
            changeNode.className = '';
        } else {
            const change = Number(payload.change_pct);
            changeNode.textContent = (change > 0 ? '+' : '') + change.toFixed(2) + '%';
            changeNode.className = change > 0 ? 'positive' : change < 0 ? 'negative' : '';
        }
        document.getElementById('analysisTechnicalTitle').textContent = 'Key Technicals · ' + timeframeLabel(payload.timeframe);
        renderQuote(payload);
        renderIntelligence(payload.intelligence);
        renderZones(payload);
        renderTrends(payload.trends || []);
        renderTechnicals(payload.technicals || []);
        applyOverlays(payload.overlays || {}, payload.price);
        renderLegend(payload);
        drawFib(payload.fib_levels || []);
        drawSessionVwap(payload);
        restoreDrawings(payload.symbol || window.smGetSymbol());
        applyMarkers();
        loadPaperMarkers();
        if (pendingTrade) drawTradeLines(pendingTrade);
        window.smSetZones(overlayFlags.zones ? filteredZones(payload.zones || []) : []);
        paintSelectedSignal(payload.signal);
        window.smResizeChart();
        renderSignal(payload.signal);
        try {
            if (pendingTrade) {
                /* a selected trade keeps its own range */
            } else if (window.smPendingBars && window.smCandleCount) {
                const count = window.smCandleCount;
                const bars = window.smPendingBars;
                chart.timeScale().setVisibleLogicalRange({ from: Math.max(0, count - bars), to: count + 1 });
            } else {
                chart.timeScale().fitContent();
            }
        } catch (error) { /* chart not measurable yet */ }
    }

    function showUnavailable(message) {
        document.getElementById('analysisPrice').textContent = '—';
        const sidePrice = document.getElementById('sidePrice');
        if (sidePrice) sidePrice.textContent = '—';
        document.getElementById('analysisChange').textContent = '';
        ['analysisOpen','analysisHigh','analysisLow','analysisPrev','analysisVolume','analysisHigh52','analysisLow52'].forEach(function (id) {
            const node = document.getElementById(id);
            if (node) node.textContent = '—';
        });
        const mcap = document.getElementById('analysisMcap');
        if (mcap) mcap.textContent = 'Data Unavailable';
        const intel = document.getElementById('analysisIntelligence');
        if (intel) intel.innerHTML = '<p>' + esc(message) + '</p>';
        document.getElementById('analysisZones').innerHTML = '<div class="analysis-row"><b>Zones</b><span>' + esc(message) + '</span></div>';
        document.getElementById('analysisTrend').innerHTML = '<div class="analysis-row"><b>Trend</b><span>Unavailable</span></div>';
        document.getElementById('analysisTechnicals').innerHTML = '<div class="analysis-tech"><span>Indicators</span><div>' + esc(message) + '</div></div>';
        renderSignal({ data_unavailable: true, message: message });
        clearOverlays();
        renderLegend(null);
        window.smSetZones([]);
        paintQuickQuote();
    }

    const ANALYSIS_CLIENT = Math.random().toString(36).slice(2) + Date.now().toString(36);
    let analysisInflight = null;

    function refreshAnalysis() {
        if (!analysisOpen) return Promise.resolve();
        const key = window.smGetSymbol() + '|' + window.smGetTimeframe();
        if (analysisInflight && analysisInflight.key === key) return analysisInflight.promise;
        const promise = runAnalysis().finally(function () {
            if (analysisInflight && analysisInflight.promise === promise) analysisInflight = null;
        });
        analysisInflight = { key: key, promise: promise };
        return promise;
    }

    async function runAnalysis() {
        if (!analysisOpen) return;
        const symbol = window.smGetSymbol();
        const timeframe = window.smGetTimeframe();
        if (!symbol) return;
        const requestId = ++analysisRequest;
        if (window.lastChartOffline) {
            showUnavailable('Live history is unavailable, so indicators are hidden.');
            return;
        }
        document.getElementById('analysisSymbol').textContent = symbol;
        if (!quickQuote || quickQuote.symbol !== symbol) document.getElementById('analysisPrice').textContent = 'Loading…';
        try {
            const response = await fetch('/api/analysis/' + encodeURIComponent(symbol) + '?timeframe=' + encodeURIComponent(timeframe) + '&client=' + ANALYSIS_CLIENT);
            const payload = await response.json();
            if (!analysisOpen || requestId !== analysisRequest) return;
            if (window.smGetSymbol() !== symbol || window.smGetTimeframe() !== timeframe) {
                analysisInflight = null;
                refreshAnalysis();
                return;
            }
            if (response.status === 409 && payload.superseded) return;
            if (!response.ok || payload.data_unavailable || payload.error) throw new Error(payload.message || payload.error || 'Analysis unavailable');
            paintAnalysis(payload);
        } catch (error) {
            if (!analysisOpen || requestId !== analysisRequest) return;
            showUnavailable(error.message || 'Analysis unavailable');
        }
    }

    function openStockAnalysis() {
        if (typeof closeFundamentalScan === 'function') closeFundamentalScan();
        if (typeof restoreFeaturePage === 'function') restoreFeaturePage();
        const area = document.getElementById('chartArea');
        const slot = document.getElementById('analysisChartSlot');
        if (!chartHome) {
            chartHome = document.createComment('chart-home');
            area.parentNode.insertBefore(chartHome, area);
        }
        slot.appendChild(area);
        analysisOpen = true;
        window.smAnalysisOpen = true;
        document.body.classList.add('sa-mode');
        document.querySelector('.workspace').classList.add('analysis-open');
        applyTheme(localStorage.getItem(THEME_KEY) || 'night');
        if (location.hash !== '#analysis') history.replaceState(null, '', '#analysis');
        if (!document.getElementById('saLegend')) {
            const legend = document.createElement('div');
            legend.id = 'saLegend';
            legend.className = 'sa-legend';
            const box = document.querySelector('#chartArea .chart-box');
            if (box) box.appendChild(legend);
        }
        if (window.smGetTimeframe() === '6mo') window.smSetTimeframe('1d');
        if (!window.smPendingBars) window.smPendingBars = 160;
        document.querySelectorAll('.nav-menu button').forEach(function (button) { button.classList.remove('active'); });
        document.getElementById('analysisNav').classList.add('active');
        loadCatalog();
        window.smResizeChart();
        const symbol = window.smGetSymbol() || localStorage.getItem('projectSmSelectedSymbol') || 'RELIANCE';
        loadChart(symbol);
        refreshAnalysis();
        const start = document.getElementById('backtestStart');
        const end = document.getElementById('backtestEnd');
        if (end && !end.value) end.value = new Date().toISOString().slice(0, 10);
        if (start && !start.value) {
            const past = new Date();
            past.setMonth(past.getMonth() - 6);
            start.value = past.toISOString().slice(0, 10);
        }
    }

    function closeStockAnalysis() {
        if (!analysisOpen) return;
        analysisOpen = false;
        window.smAnalysisOpen = false;
        clearOverlays();
        clearDrawnFib();
        clearTradeLines();
        try { candle.setMarkers([]); } catch (error) { /* markers already clear */ }
        fibMode = '';
        const area = document.getElementById('chartArea');
        if (chartHome && chartHome.parentNode) {
            chartHome.parentNode.insertBefore(area, chartHome);
            chartHome.remove();
            chartHome = null;
        }
        document.body.classList.remove('sa-mode');
        document.querySelector('.workspace').classList.remove('analysis-open');
        try {
            chart.applyOptions({
                layout: { background: { color: '#091727' }, textColor: '#ffffff' },
                grid: { vertLines: { color: '#172a40' }, horzLines: { color: '#172a40' } },
            });
        } catch (error) { /* chart keeps its current colours */ }
        if (location.hash === '#analysis') history.replaceState(null, '', location.pathname + location.search);
        window.smResizeChart();
        const symbol = window.smGetSymbol();
        if (symbol) loadChart(symbol);
    }

    async function loadCatalog() {
        if (catalogReady) return;
        const saved = readSaved();
        document.getElementById('analysisUniverse').value = saved.universe;
        const timeframe = document.getElementById('analysisTimeframe');
        if (timeframe && saved.timeframe) timeframe.value = saved.timeframe;
        const limit = document.getElementById('analysisLimit');
        if (limit) limit.value = saved.limit || '';
        const logic = document.querySelector('input[name="analysisLogic"][value="' + saved.logic + '"]');
        if (logic) logic.checked = true;
        try {
            const response = await fetch('/api/technical-scanner/catalog');
            const payload = await response.json();
            if (!response.ok) throw new Error(payload.error || 'Condition list unavailable');
            const shortName = {
                daily_bullish: 'Daily bullish', daily_bearish: 'Daily bearish', weekly_bullish: 'Weekly bullish', weekly_bearish: 'Weekly bearish', monthly_bullish: 'Monthly bullish', monthly_bearish: 'Monthly bearish',
                price_above_ema20: 'Price > 20 EMA', price_above_ema50: 'Price > 50 EMA', price_above_ema200: 'Price > 200 EMA', price_below_ema20: 'Price < 20 EMA',
                ema20_gt_ema50: '20 EMA > 50 EMA', ema50_gt_ema200: '50 EMA > 200 EMA', price_above_sma50: 'Price > 50 SMA', price_above_sma200: 'Price > 200 SMA', sma50_gt_sma200: '50 SMA > 200 SMA',
                rsi_50_70: 'RSI 50–70', rsi_oversold: 'RSI below 30', rsi_overbought: 'RSI above 70', macd_above_signal: 'MACD Bullish', macd_hist_positive: 'MACD histogram positive',
                volume_gt_avg: 'Volume > 20D Average', rvol_gt_1_5: 'Relative Volume > 1.5', price_above_vwap: 'Price above session VWAP', price_below_vwap: 'Price below session VWAP',
                above_upper_bb: 'Bollinger Breakout', below_lower_bb: 'Close below lower band', inside_bb: 'Close inside bands',
                adx_gt_25: 'ADX > 25', supertrend_bullish: 'Supertrend Bullish', supertrend_bearish: 'Supertrend Bearish',
                bullish_engulfing: 'Bullish Engulfing', bearish_engulfing: 'Bearish Engulfing', inverted_hammer: 'Inverted Hammer', shooting_star: 'Shooting Star', hanging_man: 'Hanging Man', morning_star: 'Morning Star', evening_star: 'Evening Star', piercing: 'Piercing Pattern', dark_cloud: 'Dark Cloud Cover', inside_bar: 'Inside Bar', breakout_candle: 'Breakout', breakdown_candle: 'Breakdown',
                higher_high_low: 'Higher High & Higher Low',
                near_fib_236: 'Near 23.6%', near_fib_382: 'Near 38.2%', near_fib_500: 'Near 50%', near_fib_618: 'Near 61.8%', near_fib_786: 'Near 78.6%',
                daily_demand: 'Near Daily Demand', weekly_demand: 'Near Weekly Demand', monthly_demand: 'Near Monthly Demand', daily_supply: 'Near Daily Supply', weekly_supply: 'Near Weekly Supply', monthly_supply: 'Near Monthly Supply',
                mtf_daily_weekly_bullish: 'Daily and Weekly Bullish', mtf_all_bullish: 'Daily, Weekly, and Monthly Bullish',
                change_positive: 'Price change positive', change_negative: 'Price change negative',
            };
            Object.assign(shortName, {
                higher_high_low: 'Higher High & Higher Low', golden_cross: 'Golden Cross', death_cross: 'Death Cross',
                rsi_above_50: 'RSI > 50', macd_above_signal: 'MACD Bullish', volume_gt_2x: 'Volume > 2× Average',
                price_above_cloud: 'Ichimoku Bullish', price_below_cloud: 'Ichimoku Bearish', hammer: 'Hammer',
                breakout_candle: 'Breakout', breakdown_candle: 'Breakdown', double_bottom: 'Double Bottom', double_top: 'Double Top',
                fib_golden_zone: 'Between 38.2% – 61.8%', monthly_bullish: 'Monthly Bullish', weekly_bullish: 'Weekly Bullish', daily_bullish: 'Daily Bullish',
                monthly_bearish: 'Monthly Bearish', weekly_bearish: 'Weekly Bearish', daily_bearish: 'Daily Bearish',
            });
            const params = saved.params || {};
            const box = function (key, fallback, wide) {
                const value = params[key] !== undefined && params[key] !== null ? params[key] : fallback;
                return '<input class="sa-param' + (wide ? ' wide' : '') + '" type="number" step="any" data-param="' + key + '" value="' + esc(value === null ? '' : value) + '"' + (wide ? ' placeholder="' + (key === 'price_min' ? 'Min' : 'Max') + '"' : '') + '>';
            };
            Object.assign(shortName, {
                near_52w_high: 'Near 52 Week High', price_above_sma200: 'Price > 200 SMA', price_above_ema20: 'Price > 20 EMA',
                above_upper_bb: 'Bollinger Band Breakout', breakout_candle: 'Breakout (20D High)', fib_golden_zone: 'Between 38.2 – 61.8%',
                near_fib_618: 'Fibonacci Support (61.8%)', daily_demand: 'Near Daily Demand Zone', weekly_demand: 'Near Weekly Demand Zone',
                monthly_demand: 'Near Monthly Demand Zone', daily_supply: 'Near Daily Supply Zone', weekly_supply: 'Near Weekly Supply Zone', monthly_supply: 'Near Monthly Supply Zone',
            });
            const unit = function (text) { return '<span class="sa-unit">' + text + '</span>'; };
            const withParams = {
                near_52w_high: function () { return shortName.near_52w_high + box('near_52w_pct', 5).replace('sa-param', 'sa-param push') + unit('%'); },
                rsi_50_70: function () { return 'RSI ' + box('rsi_min', 50, true).replace('sa-param wide', 'sa-param wide push').replace(' placeholder="Max"', '') + unit('to') + box('rsi_max', 70, true).replace(' placeholder="Max"', ''); },
                adx_gt_25: function () { return 'ADX &gt; 20<input type="hidden" data-param="adx_min" value="20">'; },
                volume_gt_avg: function () { return 'Volume &gt; 20D Average' + box('vol_mult', 1.5).replace('sa-param', 'sa-param push') + unit('x'); },
                rvol_gt_1_5: function () { return 'Relative Volume &gt;' + box('rvol_min', 2).replace('sa-param', 'sa-param push') + unit('x'); },
                atr_pct_gt: function () { return 'ATR % &gt;' + box('atr_pct', 2, true).replace(' placeholder="Max"', ''); },
                price_range: function () { return 'Price Range' + box('price_min', '', true).replace('sa-param wide', 'sa-param wide push') + unit('-') + box('price_max', '', true); },
            };
            const visible = { price_trend: 3, moving_averages: 3, momentum: 3, volume: 2, volatility: 2, trend_indicators: 2, price_action: 3, chart_patterns: 3, fibonacci: 2, supply_demand: 4, multi_timeframe: 3, other: 1 };
            const groupTitle = { 'Other Filters': 'Other', 'Multi-Timeframe': 'Multi Timeframe' };
            const groups = (payload.categories || []).map(function (group, index) {
                const shown = visible[group.id] || 3;
                let extra = 0;
                const checks = (group.conditions || []).map(function (item, position) {
                    const isChecked = saved.conditions.indexOf(item.id) >= 0;
                    const hidden = position >= shown && !isChecked;
                    if (hidden) extra += 1;
                    const name = withParams[item.id] ? withParams[item.id]() : esc(shortName[item.id] || item.label);
                    return '<label title="' + esc(item.label) + '"' + (hidden ? ' class="sa-extra"' : '') + '><input type="checkbox" value="' + esc(item.id) + '"' + (isChecked ? ' checked' : '') + '>' + name + '</label>';
                }).join('');
                const title = groupTitle[group.label] || group.label;
                const chevron = extra ? '⌄' : '⌃';
                return '<div class="analysis-group" data-extra="' + extra + '"><div class="sa-ghead" title="' + (extra ? 'Show all ' + (group.conditions || []).length + ' conditions' : '') + '"><span>' + (index + 1) + '. ' + esc(title) + '<small>(' + (group.conditions || []).length + ')</small></span><i>' + chevron + '</i></div>' + checks + '</div>';
            });
            const left = groups.slice(0, 6).join('');
            const right = groups.slice(6).join('');
            document.getElementById('analysisFilters').innerHTML = '<div class="sa-col">' + left + '</div><div class="sa-col">' + right + '</div>';
            catalogReady = true;
        } catch (error) {
            document.getElementById('analysisFilters').innerHTML = '<p class="analysis-note">' + esc(error.message) + '</p>';
        }
    }

    function numText(value, digits) {
        return Number.isFinite(Number(value)) ? Number(value).toFixed(digits) : '—';
    }

    function zoneCell(text, status) {
        if (!text) return '—';
        return text + (status ? ' · ' + status : '');
    }

    function renderResults(rows) {
        scanRows = rows || [];
        const sector = document.getElementById('analysisSector');
        const chosen = sector ? sector.value : '';
        const names = {};
        scanRows.forEach(function (row) { if (row.industry) names[row.industry] = true; });
        if (sector) {
            const options = '<option value="">All sectors</option>' + Object.keys(names).sort().map(function (name) {
                return '<option value="' + esc(name) + '"' + (name === chosen ? ' selected' : '') + '>' + esc(name) + '</option>';
            }).join('');
            sector.innerHTML = options;
        }
        const visible = !chosen ? scanRows : scanRows.filter(function (row) { return row.industry === chosen; });
        const found = document.getElementById('analysisFound');
        if (found) found.textContent = visible.length + (visible.length === 1 ? ' Stock Found' : ' Stocks Found');
        const count = document.getElementById('resultCount');
        if (count) count.textContent = '(' + visible.length + ')';
        const resultsTab = document.querySelector('#bottomTabs [data-bottom="results"]');
        if (resultsTab) resultsTab.textContent = 'Scan Results (' + visible.length + ')';
        const body = document.getElementById('analysisRows');
        if (!visible.length) {
            const count = document.getElementById('resultCount');
            if (count) count.textContent = '(0)';
            body.innerHTML = '<tr><td colspan="22">No stocks matched these calculated conditions.</td></tr>';
            return;
        }
        body.innerHTML = visible.map(function (row, index) {
            const change = Number.isFinite(Number(row.change_pct)) ? Number(row.change_pct) : null;
            const changeText = change === null ? '—' : (change > 0 ? '+' : '') + change.toFixed(2) + '%';
            const changeClass = change === null ? '' : change > 0 ? 'positive' : change < 0 ? 'negative' : '';
            const arrowFor = function (status) {
                if (/bull/i.test(status)) return '↑';
                if (/bear/i.test(status)) return '↓';
                if (/neutral/i.test(status)) return '→';
                return '·';
            };
            const mtfOrder = ['1d', '1wk', '1mo'];
            const mtfText = mtfOrder.map(function (key) {
                const item = (row.mtf || []).find(function (entry) { return entry.timeframe === key; });
                return item ? arrowFor(item.status) : '·';
            }).join(' ');
            const pair = row.ema20_vs_ema50 === 'above' ? '↑' : row.ema20_vs_ema50 === 'below' ? '↓' : row.ema20_vs_ema50 === 'equal' ? '=' : '·';
            return '<tr data-symbol="' + esc(row.symbol) + '">' +
                '<td><input type="checkbox" data-row="' + esc(row.symbol) + '" aria-label="Select ' + esc(row.symbol) + '"></td>' +
                '<td>' + (index + 1) + '</td>' +
                '<td><b>' + esc(row.symbol) + '</b><br><small>' + esc(row.company || '') + '</small></td>' +
                '<td>' + inr(row.ltp) + '</td>' +
                '<td class="' + changeClass + '">' + changeText + '</td>' +
                '<td>' + (Number.isFinite(Number(row.volume)) ? Number(row.volume).toLocaleString('en-IN') : '—') + '</td>' +
                '<td>' + numText(row.rsi, 2) + '</td>' +
                '<td>' + numText(row.macd, 2) + '</td>' +
                '<td class="wrap">' + esc(mtfText) + '</td>' +
                '<td>' + esc(pair) + '</td>' +
                '<td>' + esc(zoneCell(row.demand_zone, row.demand_status)) + '</td>' +
                '<td>' + esc(zoneCell(row.supply_zone, row.supply_status)) + '</td>' +
                '<td>' + esc(row.pattern || '—') + '</td>' +
                '<td class="wrap">' + esc(row.remarks || row.score_formula || '—') + '</td>' +
                '<td class="' + (row.signal === 'BUY' ? 'positive' : row.signal === 'SELL' ? 'negative' : '') + '" title="' + esc((row.reasons || []).join(' · ')) + '">' + esc(row.signal || '—') + '</td>' +
                '<td>' + esc(row.signal_score === undefined || row.signal_score === null ? '—' : row.signal_score) + '</td>' +
                '<td>' + inr(row.entry) + '</td>' +
                '<td>' + inr(row.stop_loss) + '</td>' +
                '<td>' + inr(row.target_1) + '</td>' +
                '<td>' + inr(row.target_2) + '</td>' +
                '<td>' + inr(row.target_3) + '</td>' +
                '<td>' + esc(row.risk_reward || '—') + '</td>' +
                '</tr>';
        }).join('');
        body.querySelectorAll('tr[data-symbol]').forEach(function (row) {
            row.addEventListener('click', function (event) {
                if (event.target.matches('input[data-row]')) return;
                const signal = document.getElementById('selectedSignal');
                if (signal) {
                    signal.classList.remove('sa-hidden');
                    signal.textContent = 'Loading ' + row.dataset.symbol + ' signal…';
                }
                loadChart(row.dataset.symbol);
            });
        });
    }

    async function pollScan(jobId) {
        const response = await fetch('/api/technical-scanner/' + encodeURIComponent(jobId));
        const job = await response.json();
        const button = document.getElementById('runTechnicalScan');
        if (!response.ok || job.error) {
            setStatus(job.error || 'Scan unavailable');
            button.disabled = false;
            clearInterval(scanTimer);
            return;
        }
        renderResults(job.results || []);
        const matches = (job.results || []).length;
        const stage = job.stage ? ' · ' + job.stage : '';
        const current = job.current_symbol ? ' · ' + job.current_symbol : '';
        setStatus('Scanning ' + job.completed + ' / ' + job.total + current + stage + ' · ' + matches + ' matches');
        if (job.status === 'complete') {
            const unavailable = job.unavailable ? ' · ' + job.unavailable + ' had no ' + (job.timeframe || '1d') + ' history' : '';
            setStatus('Completed ' + job.completed + ' / ' + job.total + ' · Results: ' + matches + ' matches' + unavailable);
            button.disabled = false;
            button.textContent = 'Scan Stocks';
            clearInterval(scanTimer);
        }
    }

    async function startScan() {
        const button = document.getElementById('runTechnicalScan');
        const conditions = selectedConditions();
        const logicNode = document.querySelector('input[name="analysisLogic"]:checked');
        saveScan();
        if (!conditions.length) {
            setStatus('Select at least one calculated condition.');
            return;
        }
        const timeframe = document.getElementById('analysisTimeframe').value;
        const limitText = document.getElementById('analysisLimit').value.trim();
        button.disabled = true;
        button.textContent = 'Scanning…';
        setStatus('Starting ' + timeframe + ' scan…');
        const body = {
            universe: document.getElementById('analysisUniverse').value,
            logic: logicNode ? logicNode.value : 'AND',
            timeframe: timeframe,
            conditions: conditions,
            params: scanParams(),
        };
        if (limitText) body.limit = Number(limitText);
        if (document.getElementById('analysisFresh').checked) body.fresh = true;
        try {
            const response = await fetch('/api/technical-scanner', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(body),
            });
            const payload = await response.json();
            if (!response.ok || payload.error) throw new Error(payload.error || 'Could not start the scan');
            clearInterval(scanTimer);
            await pollScan(payload.job_id);
            let scanPollBusy = false;
            scanTimer = setInterval(function () {
                if (scanPollBusy) return;
                scanPollBusy = true;
                pollScan(payload.job_id).finally(function () { scanPollBusy = false; });
            }, 1500);
        } catch (error) {
            setStatus(error.message || 'Scan unavailable');
            button.disabled = false;
            button.textContent = 'Scan Stocks';
        }
    }

    function clearDrawnFib() {
        drawnFibLines.forEach(function (line) {
            try { candle.removePriceLine(line); } catch (error) { /* already removed */ }
        });
        drawnFibLines = [];
    }

    function fibKey() {
        return 'projectSmFibDrawings:' + (window.smGetSymbol() || '');
    }

    function saveFibs() {
        localStorage.setItem(fibKey(), JSON.stringify(fibDrawings));
    }

    function restoreDrawings(symbol) {
        if (symbol && symbol !== lastFibSymbol) {
            lastFibSymbol = symbol;
            try {
                const stored = JSON.parse(localStorage.getItem('projectSmFibDrawings:' + symbol) || '[]');
                fibDrawings = Array.isArray(stored) ? stored : [];
            } catch (error) {
                fibDrawings = [];
            }
            selectedFib = fibDrawings.length ? fibDrawings.length - 1 : -1;
            fibMode = '';
            fibDraft = null;
        }
        redrawDrawnFib();
    }

    function redrawDrawnFib() {
        clearDrawnFib();
        fibDrawings.forEach(function (drawing, index) {
            const first = Number(drawing && drawing.a && drawing.a.price);
            const second = Number(drawing && drawing.b && drawing.b.price);
            if (!Number.isFinite(first) || !Number.isFinite(second)) return;
            FIB_RATIOS.forEach(function (ratio) {
                drawnFibLines.push(candle.createPriceLine({
                    price: first + (second - first) * ratio,
                    color: index === selectedFib ? '#f5c542' : '#c4b5fd',
                    lineWidth: 1,
                    lineStyle: 2,
                    axisLabelVisible: true,
                    title: 'Drawn ' + (ratio * 100).toFixed(1) + '%',
                }));
            });
        });
    }

    function setFibStatus(message) {
        const node = document.getElementById('fibStatus');
        if (node) node.textContent = message;
    }

    function chartPrice(param) {
        if (!param || !param.point) return null;
        const box = document.getElementById('chart');
        if (box && param.point.y > box.clientHeight * 0.58) {
            setFibStatus('Click the price pane');
            return null;
        }
        const price = candle.coordinateToPrice(param.point.y);
        return Number.isFinite(Number(price)) ? Number(price) : null;
    }

    function handleFibClick(param) {
        const price = chartPrice(param);
        if (price === null) return;
        const point = { time: param.time || null, price: price };
        if (fibMode === 'first') {
            fibDraft = { a: point };
            fibMode = 'second';
            setFibStatus('Click the second swing point');
            return;
        }
        if (fibMode === 'second' && fibDraft) {
            fibDrawings.push({ a: fibDraft.a, b: point });
            selectedFib = fibDrawings.length - 1;
            fibDraft = null;
            fibMode = '';
            saveFibs();
            redrawDrawnFib();
            setFibStatus('Drawn Fibonacci saved');
            return;
        }
        if ((fibMode === 'move-a' || fibMode === 'move-b') && fibDrawings[selectedFib]) {
            fibDrawings[selectedFib][fibMode === 'move-a' ? 'a' : 'b'] = point;
            fibMode = '';
            saveFibs();
            redrawDrawnFib();
            setFibStatus('Drawing updated');
        }
    }

    function clearTradeLines() {
        tradeLines.forEach(function (line) {
            try { candle.removePriceLine(line); } catch (error) { /* already removed */ }
        });
        tradeLines = [];
    }

    function drawTradeLines(trade) {
        clearTradeLines();
        if (!trade) return;
        [['Entry', trade.entry, '#e2e8f0'], ['Stop Loss', trade.stop_loss, '#ef4444'], ['Target 1', trade.target_1, '#22c55e'], ['Target 2', trade.target_2, '#86efac']].forEach(function (item) {
            if (!Number.isFinite(Number(item[1]))) return;
            tradeLines.push(candle.createPriceLine({
                price: Number(item[1]),
                color: item[2],
                lineWidth: 1,
                axisLabelVisible: true,
                title: item[0],
            }));
        });
    }

    const BAR_SECONDS = { '5m': 300, '15m': 900, '1h': 3600, '4h': 14400, '1d': 86400, '1wk': 604800, '1mo': 2678400 };

    function tradeOnChart(trade) {
        const symbol = String(trade.symbol || '').toUpperCase();
        return (!symbol || symbol === String(window.smGetSymbol() || '').toUpperCase())
            && (!trade.timeframe || trade.timeframe === window.smGetTimeframe());
    }

    function tradeZoneBoxes() {
        return lastTrades.filter(function (trade) {
            return trade.zone && trade.zone.nearby && Number.isFinite(Number(trade.time)) && tradeOnChart(trade);
        }).map(function (trade) {
            const zone = trade.zone;
            const bar = BAR_SECONDS[trade.timeframe] || 86400;
            const formed = Number(zone.time);
            const start = Math.max(Number.isFinite(formed) ? formed : 0, Number(trade.time) - 40 * bar);
            return {
                type: zone.type,
                top: zone.top,
                bottom: zone.bottom,
                start_time: start,
                end_time: Number(trade.time),
                label: (zone.type === 'demand' ? 'Demand → BUY #' : 'Supply → SELL #') + trade.trade,
            };
        });
    }

    function applyMarkers() {
        const byTime = {};
        if (typeof window.smSetTradeZones === 'function') window.smSetTradeZones('backtest', tradeZoneBoxes());
        lastTrades.concat(paperTrades).forEach(function (trade) {
            if (!Number.isFinite(Number(trade.time))) return;
            const fromZone = trade.zone && trade.zone.nearby;
            byTime[trade.time] = {
                time: trade.time,
                position: trade.side === 'SELL' ? 'aboveBar' : 'belowBar',
                color: trade.side === 'SELL' ? '#ef4444' : '#22c55e',
                shape: trade.side === 'SELL' ? 'arrowDown' : 'arrowUp',
                text: fromZone ? trade.side + ' · ' + (trade.zone.type === 'demand' ? 'Demand' : 'Supply') : trade.side,
            };
            if (!Number.isFinite(Number(trade.exit_time)) || Number(trade.exit_time) === Number(trade.time)) return;
            const reason = String(trade.exit_reason || '');
            const exitText = /stop/i.test(reason) ? 'SL' : /target 3/i.test(reason) ? 'T3' : /target 2/i.test(reason) ? 'T2' : /target 1/i.test(reason) ? 'T1' : 'EXIT';
            byTime['exit-' + trade.exit_time] = {
                time: Number(trade.exit_time),
                position: 'aboveBar',
                color: exitText === 'SL' ? '#f59e0b' : '#38bdf8',
                shape: 'circle',
                text: exitText,
            };
        });
        const extra = typeof window.smExtraMarkers === 'function' ? window.smExtraMarkers() : [];
        const markers = Object.keys(byTime).map(function (key) { return byTime[key]; }).concat(extra).sort(function (a, b) { return a.time - b.time; });
        try { candle.setMarkers(markers); } catch (error) { /* chart not ready */ }
    }
    window.smApplyMarkers = applyMarkers;

    function tradeClock(unix) {
        if (!Number.isFinite(Number(unix))) return '—';
        return new Date(Number(unix) * 1000).toLocaleString('en-IN', { hour12: false });
    }

    function renderTradeDetail(trade) {
        const node = document.getElementById('tradeDetail');
        if (!node || !trade) return;
        const reasons = (trade.reasons || []).map(function (item) { return '<div>' + esc(item) + '</div>'; }).join('');
        const kind = trade.side === 'SELL' ? 'supply' : 'demand';
        const zone = trade.zone;
        let zoneLine = zone && zone.nearby
            ? kind.charAt(0).toUpperCase() + kind.slice(1) + ' zone at signal: ' + inr(zone.bottom) + ' – ' + inr(zone.top)
                + ' (' + esc([zone.label, zone.strength, zone.freshness].filter(Boolean).join(', '))
                + (Number.isFinite(Number(zone.distance_pct)) ? ', ' + esc(zone.distance_pct) + '% away' : '') + ')'
            : 'No nearby ' + kind + ' zone at the signal bar';
        if (trade.zone_used === false) zoneLine += ' (context only; the Indicator strategy does not use zones)';
        node.innerHTML = [
            '<b>' + esc(trade.side) + ' · ' + esc(trade.label || '') + '</b>',
            'Date ' + esc(tradeClock(trade.time)),
            zoneLine,
            'Entry ' + inr(trade.entry),
            'Signal score ' + esc(trade.score),
            'Stop loss ' + inr(trade.stop_loss),
            'Target 1 ' + inr(trade.target_1),
            'Target 2 ' + inr(trade.target_2),
            'Maximum favorable excursion ' + inr(trade.mfe),
            'Maximum adverse excursion ' + inr(trade.mae),
            'Final result ' + esc(trade.exit_reason || ''),
            'Profit/Loss ' + inr(trade.pnl) + ' (' + esc(trade.pnl_pct) + '%)',
            'Holding period ' + esc(trade.holding_bars) + ' bars',
            reasons || '<div>No stored reasons.</div>',
        ].join('<br>');
    }

    function revealTrade(trade) {
        pendingTrade = null;
        drawTradeLines(trade);
        renderTradeDetail(trade);
        if (!trade || !Number.isFinite(Number(trade.time))) return;
        const span = trade.timeframe && trade.timeframe.indexOf('m') >= 0 ? 6 * 3600 : 30 * 24 * 3600;
        try {
            chart.timeScale().setVisibleRange({ from: Number(trade.time) - span, to: Number(trade.time) + span });
        } catch (error) { /* range not on this chart */ }
    }

    function focusTrade(trade) {
        pendingTrade = trade;
        const timeframe = trade.timeframe || '1d';
        const symbol = (trade.symbol || document.getElementById('backtestSymbol').value || window.smGetSymbol() || '').trim().toUpperCase();
        const sameChart = window.smGetTimeframe() === timeframe && symbol === window.smGetSymbol();
        if (!sameChart) {
            if (typeof window.smSetTimeframe === 'function') window.smSetTimeframe(timeframe);
            if (symbol) loadChart(symbol);
            return;
        }
        revealTrade(trade);
    }

    function nearestTrade(time) {
        let best = null;
        let gap = Infinity;
        lastTrades.concat(paperTrades).forEach(function (trade) {
            const distance = Math.abs(Number(trade.time) - Number(time));
            if (distance < gap) {
                gap = distance;
                best = trade;
            }
        });
        return best;
    }

    function renderCurrentSignal(signal) {
        const card = document.getElementById('currentSignalCard');
        if (!card) return;
        if (!signal || signal.data_unavailable) {
            card.className = 'cs-card';
            card.innerHTML = '<div class="cs-title">Current Signal</div><div class="cs-empty">' + esc((signal && signal.message) || 'DATA UNAVAILABLE') + '</div>';
            return;
        }
        const side = signal.signal || 'NEUTRAL';
        const strong = /^strong/i.test(signal.label || '');
        const tone = side === 'BUY' ? 'buy' : side === 'SELL' ? 'sell' : 'neutral';
        const arrow = side === 'BUY' ? '↑' : side === 'SELL' ? '↓' : '→';
        const strength = Math.max(0, Math.min(100, Number(signal.strength) || 0));
        const filled = Math.round(strength / 10);
        const bars = Array.from({ length: 10 }, function (_, index) { return '<i class="' + (index < filled ? 'on' : '') + '"></i>'; }).join('');
        const reasons = (signal.reasons || []).map(function (item) { return '<li>' + esc(item) + '</li>'; }).join('')
            || '<li class="cs-muted">Bullish and bearish rules did not reach a Buy or Sell ratio.</li>';
        const entry = Number(signal.entry);
        const pct = function (value) {
            if (!Number.isFinite(Number(value)) || !Number.isFinite(entry) || !entry) return '';
            const change = (Number(value) - entry) / entry * 100;
            return ' <span class="cs-pct">(' + (change > 0 ? '+' : '') + change.toFixed(1) + '%)</span>';
        };
        const levels = signal.levels || {};
        const row = function (name, value, extra, cls, title) {
            return '<div class="cs-row' + (cls ? ' ' + cls : '') + '"' + (title ? ' title="' + esc(title) + '"' : '') + '><span>' + esc(name) + '</span><b>' + value + (extra || '') + '</b></div>';
        };
        const levelRows = side === 'NEUTRAL' ? '<div class="cs-muted">No entry, stop or targets while the signal is Neutral.</div>' : [
            row('Entry', inr(signal.entry), '', '', levels.setup ? 'Entry is the ' + levels.setup : ''),
            row('Stop Loss', inr(signal.stop_loss), '', 'cs-stop', levels.stop_basis || ''),
            row('Target 1', inr(signal.target_1), pct(signal.target_1), 'cs-target', levels.target_1_basis || ''),
            row('Target 2', inr(signal.target_2), pct(signal.target_2), 'cs-target', levels.target_2_basis || ''),
            row('Target 3', 'Unavailable', '', 'cs-muted-row', 'The signal engine calculates two targets only'),
            row('Risk : Reward', esc(signal.risk_reward || '—')),
        ].join('');
        const nearestZoneText = function (kind) {
            const zone = ((lastPayload && lastPayload.zones) || []).filter(function (item) { return item.type === kind; })
                .sort(function (a, b) { return Number(a.distance_pct) - Number(b.distance_pct); })[0];
            return zone ? zone.label + ' ' + inr(zone.bottom) + ' – ' + inr(zone.top) : null;
        };
        const supplyText = signal.supply || nearestZoneText('supply');
        const demandText = signal.demand || nearestZoneText('demand');
        const watch = [];
        if (side !== 'SELL' && supplyText) watch.push('If price reaches the supply zone ' + supplyText + ', a rejection there is a condition to watch for the SELL rules.');
        if (side !== 'BUY' && demandText) watch.push('If price reaches the demand zone ' + demandText + ', holding there is a condition to watch for the BUY rules.');
        card.className = 'cs-card cs-' + tone;
        card.innerHTML = [
            '<div class="cs-title">Current Signal</div>',
            '<div class="cs-signal"><span class="cs-arrow">' + arrow + '</span>' + esc(side) + (strong ? ' <small>(Strong)</small>' : '') + '</div>',
            '<div class="cs-strength">Signal Strength ' + strength + '%</div><div class="cs-bars">' + bars + '</div>',
            '<div class="cs-sub">Why ' + (side === 'BUY' ? 'Buy' : side === 'SELL' ? 'Sell' : 'Neutral') + '?</div><ul class="cs-reasons">' + reasons + '</ul>',
            '<div class="cs-levels">' + levelRows + '</div>',
            '<div class="cs-next"><div class="cs-next-title">Condition to watch</div>' + (watch.length ? watch.map(function (line) { return '<div>' + esc(line) + '</div>'; }).join('') : '<div class="cs-muted">No nearby demand or supply zone was found.</div>') + '<div class="cs-muted">Monitoring only. This is not a prediction or a guaranteed signal.</div></div>',
            '<div class="cs-muted">Last closed delayed candle. Strength = net rule points / maximum points.</div>',
        ].join('');
    }

    function renderSignal(signal) {
        renderCurrentSignal(signal);
        const node = document.getElementById('analysisSignal');
        const score = document.getElementById('analysisSignalScore');
        const levels = document.getElementById('analysisLevels');
        const reasons = document.getElementById('analysisReasons');
        const components = document.getElementById('analysisComponents');
        if (!node) return;
        if (!signal || signal.data_unavailable) {
            node.className = '';
            node.textContent = 'DATA UNAVAILABLE';
            score.textContent = '';
            levels.textContent = '';
            reasons.textContent = (signal && signal.message) || 'DATA UNAVAILABLE';
            components.textContent = '';
            return;
        }
        node.className = signal.signal === 'BUY' ? 'signal-buy' : signal.signal === 'SELL' ? 'signal-sell' : 'signal-neutral';
        node.textContent = (signal.label || signal.signal) + ' · ' + signal.signal;
        score.textContent = 'Strength ' + signal.strength + '/100. ' + (signal.score_formula || '');
        const rules = (signal.label_rules || []).map(function (rule) { return rule.label + ': ' + rule.rule; }).join(' · ');
        const formula = (signal.levels && signal.levels.formula) || '';
        levels.innerHTML = [
            'Entry ' + inr(signal.entry),
            'Stop Loss ' + inr(signal.stop_loss),
            'Target 1 ' + inr(signal.target_1),
            'Target 2 ' + inr(signal.target_2),
            'R:R ' + (signal.risk_reward || '—'),
            'Trend ' + (signal.trend || '—'),
            'Demand ' + (signal.demand || '—'),
            'Supply ' + (signal.supply || '—'),
            formula,
            rules,
        ].map(function (line) { return '<div>' + esc(line) + '</div>'; }).join('');
        const listed = signal.reasons || [];
        reasons.innerHTML = listed.length
            ? listed.map(function (item) { return '<div>' + esc(item) + '</div>'; }).join('')
            : '<div>No supportive reasons. Neutral means the bullish and bearish points did not reach a Buy or Sell ratio.</div>';
        components.innerHTML = (signal.components || []).map(function (row) {
            const points = row.points === null || row.points === undefined ? 'n/a' : (row.points > 0 ? '+' + row.points : String(row.points));
            return '<div><b>' + esc(points) + '</b> ' + esc(row.name) + ' — ' + esc(row.detail || '') + '</div>';
        }).join('');
    }

    function money(value) {
        if (value === null || value === undefined || !Number.isFinite(Number(value))) return '—';
        return '₹' + Number(value).toLocaleString('en-IN', { maximumFractionDigits: 2 });
    }

    function renderBacktest(job) {
        const result = job.result || {};
        const summary = document.getElementById('backtestSummary');
        const rows = [
            ['Total Trades', result.total_trades],
            ['Winning Trades', result.winning_trades],
            ['Losing Trades', result.losing_trades],
            ['Win Rate', result.win_rate === null || result.win_rate === undefined ? '—' : result.win_rate + '%'],
            ['Average Win', money(result.average_win)],
            ['Average Loss', money(result.average_loss)],
            ['Profit Factor', result.profit_factor === null || result.profit_factor === undefined ? '—' : result.profit_factor],
            ['Net Profit', money(result.net_profit)],
            ['Max Drawdown', result.max_drawdown_pct === null || result.max_drawdown_pct === undefined ? '—' : result.max_drawdown_pct + '%'],
            ['Average Holding Period', result.average_holding_bars === null || result.average_holding_bars === undefined ? '—' : result.average_holding_bars + ' bars'],
            ['Risk/Reward', result.risk_reward === null || result.risk_reward === undefined ? '—' : '1:' + result.risk_reward],
            ['Expectancy', money(result.expectancy)],
            ['Final Capital', money(result.final_capital)],
            ['P&L %', result.pnl_pct === null || result.pnl_pct === undefined ? '—' : result.pnl_pct + '%'],
            ['Largest Win', money(result.largest_win)],
            ['Largest Loss', money(result.largest_loss)],
            ['Average Trade', money(result.average_trade)],
            ['Result', result.profitable ? 'Net profit is positive in this test' : 'Net profit is not positive in this test'],
        ];
        summary.innerHTML = '<div class="backtest-grid">' + rows.map(function (row) {
            return '<div><span>' + esc(row[0]) + '</span><b>' + esc(row[1]) + '</b></div>';
        }).join('') + '</div><p>' + esc(result.methodology || '') + '</p><p>' + esc(result.disclaimer || '') + '</p>';
        drawEquity(result.equity || []);
        lastTrades = result.trades || [];
        const body = document.getElementById('backtestRows');
        body.innerHTML = lastTrades.length ? lastTrades.map(function (trade, index) {
            return '<tr data-trade="' + index + '"><td>' + esc(trade.trade) + '</td><td>' + esc(tradeClock(trade.time)) + '</td><td>' + esc(trade.symbol) + '</td><td>' + esc(trade.timeframe) + '</td><td>' + esc(trade.side) + '</td><td>' + inr(trade.entry) + '</td><td>' + inr(trade.stop_loss) + '</td><td>' + inr(trade.target_1) + '</td><td>' + inr(trade.target_2) + '</td><td>' + inr(trade.exit) + '</td><td>' + esc(trade.exit_reason) + '</td><td>' + inr(trade.pnl) + '</td><td>' + esc(trade.pnl_pct) + '</td><td>' + esc(trade.holding_bars) + ' bars</td><td>' + esc(trade.score) + '</td></tr>';
        }).join('') : '<tr><td colspan="15">No trades in this window.</td></tr>';
        applyMarkers();
    }

    function drawEquity(points) {
        const canvas = document.getElementById('backtestEquity');
        if (!canvas) return;
        const width = Math.max(320, canvas.clientWidth || 640);
        canvas.width = width;
        canvas.height = 140;
        const ctx = canvas.getContext('2d');
        ctx.clearRect(0, 0, width, 140);
        ctx.fillStyle = '#071422';
        ctx.fillRect(0, 0, width, 140);
        const values = (points || []).map(function (point) { return Number(point.equity); }).filter(function (value) { return Number.isFinite(value); });
        if (values.length < 2) return;
        const min = Math.min.apply(null, values);
        const max = Math.max.apply(null, values);
        const span = max - min || 1;
        ctx.beginPath();
        ctx.strokeStyle = '#38bdf8';
        ctx.lineWidth = 2;
        values.forEach(function (value, index) {
            const x = (index / (values.length - 1)) * (width - 16) + 8;
            const y = 128 - ((value - min) / span) * 108;
            if (index === 0) ctx.moveTo(x, y);
            else ctx.lineTo(x, y);
        });
        ctx.stroke();
        const drawdowns = (points || []).map(function (point) { return Number(point.drawdown_pct); });
        if (drawdowns.filter(function (value) { return Number.isFinite(value); }).length > 1) {
            const peak = Math.max.apply(null, drawdowns.map(function (value) { return Number.isFinite(value) ? value : 0; })) || 1;
            ctx.beginPath();
            ctx.strokeStyle = '#f59e0b';
            ctx.lineWidth = 1;
            drawdowns.forEach(function (value, index) {
                const x = (index / (drawdowns.length - 1)) * (width - 16) + 8;
                const y = 128 - ((Number.isFinite(value) ? value : 0) / peak) * 108;
                if (index === 0) ctx.moveTo(x, y);
                else ctx.lineTo(x, y);
            });
            ctx.stroke();
        }
    }

    async function pollBacktest(jobId) {
        const response = await fetch('/api/backtest/' + encodeURIComponent(jobId));
        const job = await response.json();
        const status = document.getElementById('backtestStatus');
        if (!response.ok || job.error) {
            status.textContent = job.error || 'Backtest unavailable';
            clearInterval(backtestTimer);
            document.getElementById('runBacktest').disabled = false;
            return;
        }
        status.textContent = job.status === 'running' ? 'Backtest running · ' + (job.stage || '') : (job.status === 'unavailable' ? 'DATA UNAVAILABLE' : 'Backtest finished');
        if (job.status === 'complete' || job.status === 'unavailable') {
            clearInterval(backtestTimer);
            document.getElementById('runBacktest').disabled = false;
            if (job.result) renderBacktest(job);
        }
    }

    async function startBacktest() {
        const button = document.getElementById('runBacktest');
        const symbolInput = document.getElementById('backtestSymbol');
        if (!symbolInput.value.trim() && window.smGetSymbol()) symbolInput.value = window.smGetSymbol();
        const symbol = symbolInput.value.trim().toUpperCase();
        button.disabled = true;
        document.getElementById('backtestStatus').textContent = 'Starting backtest…';
        try {
            const response = await fetch('/api/backtest', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    symbol: symbol,
                    timeframe: document.getElementById('backtestTimeframe').value,
                    start: document.getElementById('backtestStart').value || null,
                    end: document.getElementById('backtestEnd').value || null,
                    strategy: document.getElementById('backtestStrategy').value,
                    capital: Number(document.getElementById('backtestCapital').value),
                    risk_pct: Number(document.getElementById('backtestRisk').value),
                }),
            });
            const payload = await response.json();
            if (!response.ok || payload.error || payload.data_unavailable) {
                throw new Error(payload.message || payload.error || 'DATA UNAVAILABLE');
            }
            clearInterval(backtestTimer);
            await pollBacktest(payload.job_id);
            backtestTimer = setInterval(function () { pollBacktest(payload.job_id); }, 1500);
        } catch (error) {
            document.getElementById('backtestStatus').textContent = error.message || 'DATA UNAVAILABLE';
            button.disabled = false;
        }
    }

    window.closeStockAnalysis = closeStockAnalysis;
    window.onChartLoaded = function () {
        setChartLoading('');
        if (!analysisOpen) return;
        const bars = window.smPendingBars;
        const count = window.smCandleCount || 0;
        if (bars && count && chart) {
            try {
                chart.timeScale().setVisibleLogicalRange({ from: Math.max(0, count - bars), to: count + 1 });
            } catch (error) { /* range is applied after the next fit */ }
        }
        refreshAnalysis().then(function () {
            if (pendingTrade) revealTrade(pendingTrade);
        });
    };
    window.onChartFailed = function () {
        setChartLoading('Chart data is unavailable for this symbol.');
        if (analysisOpen) showUnavailable('Chart data is unavailable.');
    };

    window.smOpenAnalysis = openStockAnalysis;
    document.getElementById('closeAnalysis').addEventListener('click', function () {
        closeStockAnalysis();
        const dashboard = document.querySelector('.nav-menu button[data-target="dashboard"]');
        if (dashboard) {
            document.querySelectorAll('.nav-menu button').forEach(function (button) { button.classList.remove('active'); });
            dashboard.classList.add('active');
        }
    });
    document.getElementById('analysisUniverse').addEventListener('change', saveScan);
    document.getElementById('analysisTimeframe').addEventListener('change', saveScan);
    document.getElementById('analysisLimit').addEventListener('change', saveScan);
    document.querySelectorAll('input[name="analysisLogic"]').forEach(function (node) {
        node.addEventListener('change', saveScan);
    });
    document.getElementById('analysisFilters').addEventListener('change', saveScan);
    document.getElementById('runTechnicalScan').addEventListener('click', startScan);
    document.getElementById('analysisChartTools').addEventListener('change', function (event) {
        const key = event.target.getAttribute('data-overlay');
        if (!key) return;
        overlayFlags[key] = event.target.checked;
        window.smClearTemplate && window.smClearTemplate();
        if (!lastPayload) {
            if (key === 'zones' && !overlayFlags.zones) window.smSetZones([]);
            return;
        }
        applyOverlays(lastPayload.overlays || {}, lastPayload.price);
        renderLegend(lastPayload);
        drawFib(lastPayload.fib_levels || []);
        drawSessionVwap(lastPayload);
        redrawDrawnFib();
        if (pendingTrade) drawTradeLines(pendingTrade);
        window.smSetZones(overlayFlags.zones ? filteredZones(lastPayload.zones || []) : []);
    });

    function renderAlertResults(symbol, payload) {
        const message = document.getElementById('alertMessage');
        const results = document.getElementById('alertResults');
        if (!message || !results) return;
        message.textContent = payload.delivery || payload.message || payload.error || 'In-app only. Nothing is emailed, pushed, or sent to a broker.';
        const rows = payload.results || [];
        results.innerHTML = rows.length ? rows.map(function (row) {
            const state = row.triggered ? 'Triggered' : (row.state === 'unavailable' ? 'Unavailable' : 'Not triggered');
            return '<p><b>' + esc(symbol) + ' · ' + esc(row.type || '') + '</b> ' + esc(state) + ' — ' + esc(row.detail || '') + ' <small>(in-app only)</small></p>';
        }).join('') : '<p>' + esc(payload.message || payload.error || 'No result') + '</p>';
    }

    async function postAlerts(symbol, timeframe, rules) {
        const response = await fetch('/api/alerts/evaluate', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ symbol: symbol, timeframe: timeframe, rules: rules }),
        });
        const payload = await response.json();
        renderAlertResults(symbol, payload);
    }

    const checkAlert = document.getElementById('checkAlert');
    if (checkAlert) {
        checkAlert.addEventListener('click', function () {
            const symbol = (document.getElementById('alertSymbol').value || '').trim().toUpperCase();
            const type = document.getElementById('alertType').value;
            const direction = document.getElementById('alertDirection').value;
            const raw = document.getElementById('alertValue').value;
            const rule = { id: type, type: type, direction: direction };
            if (raw !== '') rule.value = Number(raw);
            if (type === 'rsi_threshold') rule.operator = direction === 'bearish' ? 'below' : 'above';
            if (type === 'score_threshold') rule.conditions = selectedConditions();
            postAlerts(symbol, document.getElementById('alertTimeframe').value, [rule]).catch(function (error) {
                document.getElementById('alertMessage').textContent = error.message || 'Alert check failed';
            });
        });
    }

    const alertEnabled = document.getElementById('alertEnabled');
    if (alertEnabled) {
        alertEnabled.addEventListener('change', function () {
            clearInterval(alertTimer);
            alertTimer = null;
            if (!alertEnabled.checked) {
                document.getElementById('alertMessage').textContent = 'Zone alert check is off. Nothing is emailed, pushed, or sent to a broker.';
                return;
            }
            const run = function () {
                const symbol = window.smGetSymbol();
                if (!symbol) return;
                const symbolInput = document.getElementById('alertSymbol');
                if (symbolInput && !symbolInput.value) symbolInput.value = symbol;
                postAlerts(symbol, '1d', [
                    { id: 'demand_entry', type: 'demand_entry' },
                    { id: 'supply_entry', type: 'supply_entry' },
                ]).catch(function (error) {
                    document.getElementById('alertMessage').textContent = error.message || 'Alert check failed';
                });
            };
            run();
            alertTimer = setInterval(run, 60000);
        });
    }
    window.addEventListener('resize', function () {
        if (analysisOpen) window.smResizeChart();
    });

    document.getElementById('fibDraw').addEventListener('click', function () {
        fibMode = 'first';
        fibDraft = null;
        setFibStatus('Click the first swing point');
    });
    document.getElementById('fibMoveA').addEventListener('click', function () {
        if (!fibDrawings.length) { setFibStatus('Draw a Fibonacci first'); return; }
        if (selectedFib < 0) selectedFib = fibDrawings.length - 1;
        fibMode = 'move-a';
        setFibStatus('Click a new first point');
    });
    document.getElementById('fibMoveB').addEventListener('click', function () {
        if (!fibDrawings.length) { setFibStatus('Draw a Fibonacci first'); return; }
        if (selectedFib < 0) selectedFib = fibDrawings.length - 1;
        fibMode = 'move-b';
        setFibStatus('Click a new second point');
    });
    document.getElementById('fibDelete').addEventListener('click', function () {
        if (selectedFib < 0 || !fibDrawings[selectedFib]) { setFibStatus('No drawing selected'); return; }
        fibDrawings.splice(selectedFib, 1);
        selectedFib = fibDrawings.length ? fibDrawings.length - 1 : -1;
        fibMode = '';
        saveFibs();
        redrawDrawnFib();
        setFibStatus('Drawing deleted');
    });
    document.getElementById('fibClear').addEventListener('click', function () {
        fibDrawings = [];
        selectedFib = -1;
        fibMode = '';
        fibDraft = null;
        saveFibs();
        redrawDrawnFib();
        setFibStatus('All drawn Fibonacci levels cleared');
    });
    document.getElementById('runBacktest').addEventListener('click', startBacktest);
    document.getElementById('backtestRows').addEventListener('click', function (event) {
        const row = event.target.closest('tr');
        if (!row || row.dataset.trade === undefined) return;
        const trade = lastTrades[Number(row.dataset.trade)];
        if (trade) focusTrade(trade);
    });
    if (chart) chart.subscribeClick(function (param) {
        if (!analysisOpen || !param || window.smDrawingTool) return;
        if (fibMode) {
            handleFibClick(param);
            return;
        }
        if (!param.time || (!lastTrades.length && !paperTrades.length)) return;
        const trade = nearestTrade(param.time);
        if (!trade) return;
        const limit = String(trade.timeframe || '').indexOf('m') >= 0 || String(trade.timeframe || '').indexOf('h') >= 0 ? 6 * 3600 : 5 * 24 * 3600;
        if (Math.abs(Number(trade.time) - Number(param.time)) > limit) return;
        pendingTrade = trade;
        revealTrade(trade);
    });

    function money(value) {
        return Number.isFinite(Number(value)) ? inr(value) : '—';
    }

    function badgeClass(status) {
        const value = String(status || '').toUpperCase();
        if (value.includes('UNAVAILABLE')) return 'bad';
        if (value === 'LIVE') return 'live';
        if (value.includes('HISTORICAL')) return 'hist';
        if (value.includes('DELAYED') || value.includes('STALE')) return 'delay';
        return '';
    }

    function companyName(symbol) {
        const found = (window.stockDirectory || []).find(function (item) { return item.symbol === symbol; });
        return found && found.company ? found.company : symbol;
    }

    function crore(value) {
        const number = Number(value);
        if (!value || !Number.isFinite(number)) return 'Unavailable';
        return (number / 1e7).toLocaleString('en-IN', { maximumFractionDigits: 0 });
    }

    const THEME_KEY = 'projectSmTheme';
    const CHART_THEMES = {
        night: { background: '#0b1322', text: '#cbd5e1', grid: '#142035', border: '#1c2a40' },
        day: { background: '#ffffff', text: '#334155', grid: '#eef2f7', border: '#d6dee9' },
    };

    function applyTheme(name) {
        const theme = name === 'day' ? 'day' : 'night';
        document.body.classList.toggle('sa-light', theme === 'day');
        const button = document.getElementById('saTheme');
        if (button) {
            button.textContent = theme === 'day' ? '☾' : '☀';
            button.title = theme === 'day' ? 'Switch to night mode' : 'Switch to day mode';
        }
        const colors = CHART_THEMES[theme];
        try {
            chart.applyOptions({
                layout: { background: { color: colors.background }, textColor: colors.text },
                grid: { vertLines: { color: colors.grid }, horzLines: { color: colors.grid } },
                rightPriceScale: { borderColor: colors.border },
                timeScale: { borderColor: colors.border },
            });
        } catch (error) { /* chart is not ready; the CSS theme still applies */ }
        localStorage.setItem(THEME_KEY, theme);
    }

    function setChartLoading(message) {
        const node = document.getElementById('saChartLoading');
        if (!node) return;
        node.textContent = message || '';
        node.classList.toggle('sa-hidden', !message);
    }

    let quickQuote = null;

    function paintQuickQuote() {
        if (!quickQuote || window.smGetSymbol() !== quickQuote.symbol) return;
        if (lastPayload && lastPayload.symbol === quickQuote.symbol) return;
        const quote = quickQuote.quote;
        renderQuote({ symbol: quickQuote.symbol, quote: quote, quote_status: quote.data_status || 'UNAVAILABLE' });
        const priceText = quote.ltp == null ? '—' : inr(quote.ltp);
        document.getElementById('analysisSymbol').textContent = quickQuote.symbol;
        document.getElementById('analysisPrice').textContent = priceText;
        const side = document.getElementById('sidePrice');
        if (side) side.textContent = priceText;
    }

    function loadQuickQuote(symbol) {
        fetch('/api/market/quote/' + encodeURIComponent(symbol) + '?timeframe=1d')
            .then(function (response) { return response.json(); })
            .then(function (quote) {
                if (window.smGetSymbol() !== symbol) return;
                quickQuote = { symbol: symbol, quote: quote || {} };
                paintQuickQuote();
            })
            .catch(function () { /* the full analysis request still fills the header */ });
    }

    function renderQuote(payload) {
        const quote = payload.quote || {};
        const symbol = payload.symbol || window.smGetSymbol();
        const company = document.getElementById('analysisCompany');
        if (company) company.textContent = companyName(symbol);
        const exchange = document.getElementById('analysisExchange');
        if (exchange) exchange.textContent = quote.exchange || 'NSE';
        const set = function (id, value) {
            const node = document.getElementById(id);
            if (node) node.textContent = value;
        };
        set('analysisOpen', money(quote.open));
        set('analysisHigh', money(quote.high));
        set('analysisLow', money(quote.low));
        set('analysisPrev', money(quote.previous_close));
        set('analysisVolume', Number.isFinite(Number(quote.volume)) ? Number(quote.volume).toLocaleString('en-IN') : '—');
        set('analysisMcap', crore(quote.market_cap));
        set('analysisHigh52', money(quote.week52_high));
        set('analysisLow52', money(quote.week52_low));
        const feed = payload.market || {};
        const statusNode = document.getElementById('analysisDataStatus');
        if (statusNode) {
            const quoteStatus = payload.quote_status || feed.data_status || 'DELAYED';
            statusNode.textContent = quoteStatus;
            statusNode.title = 'Stream ' + (feed.stream || 'NOT CONNECTED');
            statusNode.className = 'sa-badge ' + badgeClass(quoteStatus);
        }
        const change = Number(quote.change);
        const changeNode = document.getElementById('analysisChange');
        if (changeNode && Number.isFinite(change)) {
            const pct = Number(quote.change_pct);
            const sign = change > 0 ? '+' : change < 0 ? '-' : '';
            changeNode.textContent = sign + '₹' + Math.abs(change).toFixed(2) + (Number.isFinite(pct) ? ' (' + (pct > 0 ? '+' : '') + pct.toFixed(2) + '%)' : '');
            changeNode.className = change > 0 ? 'positive' : change < 0 ? 'negative' : '';
            const priceNode = document.getElementById('analysisPrice');
            if (priceNode) priceNode.className = change > 0 ? 'positive' : change < 0 ? 'negative' : '';
        }
        fetch('/api/sector-trend/' + encodeURIComponent(symbol)).then(function (response) { return response.json(); }).then(function (body) {
            const node = document.getElementById('analysisSectorName');
            if (node && window.smGetSymbol() === symbol) node.textContent = body.sector || 'Data Unavailable';
        }).catch(function () {
            const node = document.getElementById('analysisSectorName');
            if (node) node.textContent = 'Data Unavailable';
        });
    }

    function renderIntelligence(report) {
        const node = document.getElementById('analysisIntelligence');
        if (!node) return;
        if (!report || report.data_unavailable) {
            node.innerHTML = '<p>Data Unavailable</p>';
            return;
        }
        const setups = (report.setups || []).map(function (item) {
            return '<div><b>' + esc(item.name) + ' · ' + esc(item.side) + '</b>' + (item.reasons || []).map(function (reason) { return '<div>' + esc(reason) + '</div>'; }).join('') + '</div>';
        }).join('') || '<div>No setup rule is true on this bar.</div>';
        const confirm = report.confirmation || {};
        const quality = report.quality || {};
        const groups = (quality.groups || []).map(function (group) {
            return '<div>' + esc(group.name) + ' ' + (group.score === null || group.score === undefined ? 'Unavailable' : group.score + '/100') + ' · ' + esc(group.direction || '') + '</div>';
        }).join('');
        const plan = report.trade_plan || {};
        const regime = report.regime || {};
        node.innerHTML = [
            '<div class="sa-card"><h3>Setups</h3>' + setups + '<p class="analysis-note">' + esc((report.omitted_patterns || []).join(' ')) + '</p></div>',
            '<div class="sa-card"><h3>Confirmation</h3><b>Primary</b>' + ((confirm.primary || []).map(function (item) { return '<div>' + esc(item) + '</div>'; }).join('') || '<div>None</div>'),
            '<b>Confirmation</b>' + ((confirm.confirmation || []).map(function (item) { return '<div>' + esc(item) + '</div>'; }).join('') || '<div>None</div>'),
            '<div>' + esc(confirm.invalidation || '') + '</div></div>',
            '<div class="sa-card"><h3>Signal quality</h3><div>Signal score ' + esc(quality.signal_score) + '/100</div><div>Quality score ' + esc(quality.quality_score) + '/100</div>' + groups + '<p class="analysis-note">' + esc(quality.formula || '') + '</p></div>',
            '<div class="sa-card"><h3>Trade plan</h3><div>' + esc(plan.entry_zone || '') + '</div><div>Target 3 ' + money(plan.target_3) + '</div><div>Risk ' + money(plan.risk) + '</div><p class="analysis-note">' + esc(plan.formula || '') + '</p></div>',
            '<div class="sa-card"><h3>Market regime</h3><b>' + esc(regime.label || 'Unavailable') + '</b>' + ((regime.conditions || []).map(function (item) { return '<div>' + esc(item) + '</div>'; }).join('')) + '</div>',
            '<div class="sa-card"><h3>Signal age</h3><div>' + esc((report.signal_age || {}).status || 'Data Unavailable') + '</div><p class="analysis-note">' + esc((report.signal_age || {}).basis || '') + '</p></div>'
        ].join('');
    }

    function showTab(group, selected, prefix) {
        document.querySelectorAll(group).forEach(function (button) {
            button.classList.toggle('active', button.dataset[prefix] === selected);
        });
    }

    document.getElementById('scannerTabs').addEventListener('click', function (event) {
        const tab = event.target.dataset.scannerTab;
        if (!tab) return;
        showTab('#scannerTabs button', tab, 'scannerTab');
        document.getElementById('scannerTabTechnical').classList.toggle('sa-hidden', tab !== 'technical');
        document.getElementById('scannerTabMine').classList.toggle('sa-hidden', tab !== 'mine');
        document.getElementById('scannerTabSaved').classList.toggle('sa-hidden', tab !== 'saved');
        if (tab === 'saved') renderSavedScans();
    });
    document.getElementById('scanTimeframes').addEventListener('click', function (event) {
        const tf = event.target.dataset.scanTf;
        if (!tf) return;
        document.getElementById('analysisTimeframe').value = tf;
        document.querySelectorAll('#scanTimeframes [data-scan-tf]').forEach(function (button) {
            button.classList.toggle('active', button.dataset.scanTf === tf);
        });
        window.smPendingBars = 0;
        if (window.smSetTimeframe) window.smSetTimeframe(tf);
        document.querySelectorAll('#saChartToolbar [data-range]').forEach(function (button) { button.classList.remove('active'); });
        if (window.smGetSymbol && window.smGetSymbol()) loadChart(window.smGetSymbol());
        saveScan();
    });
    document.getElementById('resetScanConditions').addEventListener('click', function () {
        document.querySelectorAll('#analysisFilters input[type="checkbox"]').forEach(function (box) { box.checked = false; });
        saveScan();
        const cleared = readSaved();
        cleared.params = {};
        localStorage.setItem(STORAGE_KEY, JSON.stringify(cleared));
        catalogReady = false;
        loadCatalog();
    });
    const filters = document.getElementById('analysisFilters');
    filters.addEventListener('click', function (event) {
        const head = event.target.closest('.sa-ghead');
        if (!head) return;
        const group = head.closest('.analysis-group');
        if (group.dataset.extra === '0') return;
        const open = group.classList.toggle('expanded');
        head.querySelector('i').textContent = open ? '⌃' : '⌄';
    });
    filters.addEventListener('input', function (event) {
        if (!event.target.dataset.param) return;
        const label = event.target.closest('label');
        const box = label && label.querySelector('input[type="checkbox"]');
        if (box && event.target.value.trim() !== '') box.checked = true;
    });
    document.getElementById('analysisSector').addEventListener('change', function () { renderResults(scanRows); });
    document.getElementById('saveNamedScan').addEventListener('click', function () {
        const name = window.prompt('Scan name');
        if (!name) return;
        const saved = JSON.parse(localStorage.getItem('projectSmSavedScans') || '[]');
        saved.unshift({ name: name, config: readSaved() });
        localStorage.setItem('projectSmSavedScans', JSON.stringify(saved.slice(0, 20)));
        renderSavedScans();
    });
    function renderSavedScans() {
        const node = document.getElementById('savedScanList');
        const saved = JSON.parse(localStorage.getItem('projectSmSavedScans') || '[]');
        node.innerHTML = saved.map(function (item, index) {
            return '<button type="button" data-saved="' + index + '">' + esc(item.name) + '</button>';
        }).join('') || '<p class="analysis-note">No saved scan yet.</p>';
    }
    document.getElementById('savedScanList').addEventListener('click', function (event) {
        const index = event.target.dataset.saved;
        if (index === undefined) return;
        const saved = JSON.parse(localStorage.getItem('projectSmSavedScans') || '[]')[Number(index)];
        if (!saved) return;
        localStorage.setItem(STORAGE_KEY, JSON.stringify(saved.config));
        catalogReady = false;
        loadCatalog();
    });
    document.addEventListener('click', function (event) {
        document.querySelectorAll('#saChartToolbar details[open]').forEach(function (menu) {
            if (!menu.contains(event.target)) menu.removeAttribute('open');
        });
    });
    document.getElementById('bottomTabs').addEventListener('click', function (event) {
        const tab = event.target.dataset.bottom;
        if (!tab) return;
        showTab('#bottomTabs button', tab, 'bottom');
        ['results', 'watch', 'alerts', 'paper', 'backtest', 'history', 'compare', 'monitor', 'chartbt'].forEach(function (name) {
            document.getElementById('bottom-' + name).classList.toggle('sa-hidden', name !== tab);
        });
    });
    document.getElementById('infoTabs').addEventListener('click', function (event) {
        const tab = event.target.dataset.infoTab;
        if (!tab) return;
        showTab('#infoTabs button', tab, 'infoTab');
        const technical = document.getElementById('infoTechnical');
        if (technical) technical.classList.toggle('sa-hidden', tab !== 'technical');
        document.getElementById('infoFundamental').classList.toggle('sa-hidden', tab !== 'fundamental');
        document.getElementById('infoInfo').classList.toggle('sa-hidden', tab !== 'info');
        document.getElementById('infoSignal').classList.toggle('sa-hidden', tab !== 'signal');
        if (tab === 'info') {
            const symbol = window.smGetSymbol() || '—';
            const quote = (lastPayload && lastPayload.quote) || {};
            const line = function (name, value) {
                return '<div class="sa-tech"><span>' + esc(name) + '</span><b>' + esc(value || 'Data Unavailable') + '</b><em></em></div>';
            };
            const sector = document.getElementById('analysisSectorName');
            const status = document.getElementById('analysisDataStatus');
            document.getElementById('infoBody').innerHTML = [
                line('Company', companyName(symbol)),
                line('Ticker', symbol),
                line('Exchange', quote.exchange || 'NSE'),
                line('Sector', sector ? sector.textContent : ''),
                line('Industry', 'Data Unavailable'),
                line('52W High', document.getElementById('analysisHigh52').textContent),
                line('52W Low', document.getElementById('analysisLow52').textContent),
                line('Listing', 'Data Unavailable'),
                line('Data status', status ? status.textContent : ''),
            ].join('');
        }
    });
    document.getElementById('loadFundamentals').addEventListener('click', function () {
        const symbol = window.smGetSymbol();
        const node = document.getElementById('fundamentalBody');
        node.textContent = 'Loading…';
        fetch('/api/fundamentals/' + encodeURIComponent(symbol)).then(function (response) { return response.json().then(function (body) { return { ok: response.ok, body: body }; }); }).then(function (result) {
            if (!result.ok || result.body.data_unavailable) {
                node.textContent = 'Data Unavailable';
                return;
            }
            const checks = Object.keys(result.body.checks || {}).map(function (key) {
                const value = result.body.checks[key];
                return key + ': ' + (value === null ? 'Data Unavailable' : value ? 'pass' : 'fail');
            }).join(' · ');
            const cap = result.body.market_cap ? Number(result.body.market_cap).toLocaleString('en-IN') : 'Data Unavailable';
            const mcap = document.getElementById('analysisMcap');
            if (mcap) mcap.textContent = crore(result.body.market_cap);
            const sector = document.getElementById('analysisSectorName');
            if (sector && (result.body.industry || result.body.sector)) sector.textContent = result.body.industry || result.body.sector;
            node.textContent = (result.body.company || symbol) + ' · market cap ' + cap + ' · ' + checks;
        }).catch(function () { node.textContent = 'Data Unavailable'; });
    });
    document.getElementById('exportScan').addEventListener('click', function () {
        const lines = ['symbol,ltp,change_pct,volume,rsi,macd,industry'];
        const ticked = Array.from(document.querySelectorAll('#analysisRows input[data-row]:checked')).map(function (box) { return box.dataset.row; });
        const rows = ticked.length ? scanRows.filter(function (row) { return ticked.indexOf(row.symbol) >= 0; }) : scanRows;
        rows.forEach(function (row) {
            lines.push([row.symbol, row.ltp, row.change_pct, row.volume, row.rsi, row.macd, row.industry || ''].join(','));
        });
        const blob = new Blob([lines.join('\n')], { type: 'text/csv' });
        const link = document.createElement('a');
        link.href = URL.createObjectURL(blob);
        link.download = 'scan-results.csv';
        link.click();
    });
    document.getElementById('calcSize').addEventListener('click', function () {
        const signal = (lastPayload && lastPayload.signal) || {};
        fetch('/api/position-size', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                capital: document.getElementById('sizeCapital').value,
                risk_pct: document.getElementById('sizeRisk').value,
                entry: signal.entry,
                stop_loss: signal.stop_loss,
            }),
        }).then(function (response) { return response.json(); }).then(function (body) {
            document.getElementById('sizeResult').textContent = body.message || ('Risk ' + money(body.risk_amount) + ' · quantity ' + body.quantity + ' · max loss ' + money(body.maximum_loss));
        });
    });
    document.getElementById('saCheckAlert').addEventListener('click', function () {
        const symbol = (document.getElementById('saAlertSymbol').value || window.smGetSymbol() || '').trim().toUpperCase();
        const rule = { type: document.getElementById('saAlertType').value };
        const level = document.getElementById('saAlertValue').value;
        if (level !== '') rule.value = Number(level);
        fetch('/api/alerts/evaluate', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ symbol: symbol, timeframe: document.getElementById('analysisTimeframe').value, rules: [rule] }),
        }).then(function (response) { return response.json(); }).then(function (body) {
            const row = (body.results || [])[0] || {};
            document.getElementById('saAlertResults').textContent = (body.delivery || '') + ' ' + (row.detail || body.message || 'Data Unavailable') + (row.triggered ? ' · triggered' : '');
        }).catch(function () {
            document.getElementById('saAlertResults').textContent = 'Data Unavailable';
        });
    });
    document.getElementById('loadWatchIntel').addEventListener('click', function () {
        const symbols = JSON.parse(localStorage.getItem('projectSmWatchlist') || '[]');
        const body = document.getElementById('watchIntelRows');
        body.innerHTML = '<tr><td colspan="10">Calculating…</td></tr>';
        fetch('/api/watchlist/intelligence', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                symbols: symbols,
                timeframe: document.getElementById('analysisTimeframe').value,
                include_zones: document.getElementById('watchZones').checked,
            }),
        }).then(function (response) { return response.json(); }).then(function (payload) {
            body.innerHTML = (payload.rows || []).map(function (row) {
                if (row.data_unavailable) return '<tr><td>' + esc(row.symbol) + '</td><td colspan="9">Data Unavailable</td></tr>';
                const mtf = (row.mtf || []).map(function (item) { return item.label + ' ' + item.status; }).join(' · ');
                return '<tr data-symbol="' + esc(row.symbol) + '"><td>' + esc(row.symbol) + '</td><td>' + esc(row.trend || '—') + '</td><td>' + esc(row.label || row.signal || '—') + '</td><td>' + esc(row.score) + '</td><td>' + esc(row.demand_state === 'unavailable' ? 'Data Unavailable' : (row.demand || 'None')) + '</td><td>' + esc(row.supply_state === 'unavailable' ? 'Data Unavailable' : (row.supply || 'None')) + '</td><td>' + numText(row.rsi, 2) + '</td><td>' + numText(row.macd, 2) + '</td><td>' + (Number.isFinite(Number(row.volume)) ? Number(row.volume).toLocaleString('en-IN') : '—') + '</td><td class="wrap">' + esc(mtf) + '</td></tr>';
            }).join('');
            body.querySelectorAll('tr[data-symbol]').forEach(function (row) {
                row.addEventListener('click', function () { loadChart(row.dataset.symbol); });
            });
        }).catch(function () {
            body.innerHTML = '<tr><td colspan="10">Data Unavailable</td></tr>';
        });
    });
    document.getElementById('loadHistory').addEventListener('click', function () {
        const params = new URLSearchParams();
        const side = document.getElementById('historySide').value;
        const result = document.getElementById('historyResult').value;
        const symbol = document.getElementById('historySymbol').value.trim();
        if (side) params.set('side', side);
        if (result) params.set('result', result);
        if (symbol) params.set('symbol', symbol);
        fetch('/api/signal-history?' + params.toString()).then(function (response) { return response.json(); }).then(function (body) {
            const rows = body.trades || [];
            document.getElementById('historyRows').innerHTML = rows.length ? rows.map(function (trade) {
                const outcome = (trade.pnl || 0) > 0 ? 'Win' : (trade.pnl || 0) < 0 ? 'Loss' : 'Flat';
                return '<tr><td>' + esc(tradeClock(trade.time)) + '</td><td>' + esc(trade.symbol) + '</td><td>' + esc(trade.timeframe) + '</td><td>' + esc(trade.side) + '</td><td>' + esc(trade.score) + '</td><td>' + money(trade.entry) + '</td><td>' + money(trade.stop_loss) + '</td><td>' + money(trade.target_1) + '</td><td>' + outcome + '</td><td>' + money(trade.pnl) + '</td><td>' + esc(trade.holding_bars) + ' bars</td></tr>';
            }).join('') : '<tr><td colspan="11">No session trades match these filters.</td></tr>';
        });
    });
    function pollJob(url, onDone, statusNode) {
        const timer = setInterval(function () {
            fetch(url).then(function (response) { return response.json(); }).then(function (job) {
                if (statusNode) statusNode.textContent = job.status + (job.error ? ' ' + job.error : '');
                if (job.status === 'complete' || job.status === 'unavailable' || job.status === 'error') {
                    clearInterval(timer);
                    onDone(job);
                }
            }).catch(function () {
                clearInterval(timer);
                if (statusNode) statusNode.textContent = 'Data Unavailable';
            });
        }, 1500);
    }
    document.getElementById('replayLoad').addEventListener('click', function () {
        const symbol = window.smGetSymbol();
        document.getElementById('replayStatus').textContent = 'Loading replay…';
        fetch('/api/replay', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                symbol: symbol,
                timeframe: window.smGetTimeframe(),
                start: document.getElementById('replayStart').value || null,
                end: document.getElementById('replayEnd').value || null,
            }),
        }).then(function (response) { return response.json(); }).then(function (body) {
            if (!body.job_id) throw new Error(body.message || 'DATA UNAVAILABLE');
            pollJob('/api/replay/' + body.job_id, function (job) {
                replay = job.result;
                replayCursor = 0;
                document.getElementById('replayStatus').textContent = replay && replay.steps ? replay.steps.length + ' candles. ' + (replay.methodology || '') : 'Data Unavailable';
                showReplay();
            }, document.getElementById('replayStatus'));
        }).catch(function (error) {
            document.getElementById('replayStatus').textContent = error.message || 'Data Unavailable';
        });
    });
    function showReplay() {
        if (!replay || !replay.steps || !replay.steps.length) return;
        const step = replay.steps[Math.max(0, Math.min(replayCursor, replay.steps.length - 1))];
        const visible = (replay.candles || []).filter(function (candleRow) { return candleRow.time <= step.time; });
        try { candle.setData(visible.map(function (item) { return { time: item.time, open: item.open, high: item.high, low: item.low, close: item.close }; })); } catch (error) { /* chart scale */ }
        document.getElementById('replayStatus').textContent = new Date(step.time * 1000).toLocaleString('en-IN') + ' · ' + (step.label || step.signal) + ' · ' + step.strength + '/100';
        const detail = document.getElementById('tradeDetail');
        if (detail) {
            detail.innerHTML = '<b>' + esc(step.label || step.signal) + '</b><div>Entry ' + money(step.entry) + '</div><div>Stop ' + money(step.stop_loss) + '</div><div>Target ' + money(step.target_1) + '</div>' + (step.reasons || []).map(function (reason) { return '<div>' + esc(reason) + '</div>'; }).join('') + '<p class="analysis-note">Zones on replay bars: ' + esc(replay.zones || 'Data Unavailable') + '</p>';
        }
    }
    document.getElementById('replayPrev').addEventListener('click', function () { replayCursor = Math.max(0, replayCursor - 1); showReplay(); });
    document.getElementById('replayNext').addEventListener('click', function () {
        if (!replay || !replay.steps) return;
        replayCursor = Math.min(replay.steps.length - 1, replayCursor + 1);
        showReplay();
    });
    document.getElementById('replayPlay').addEventListener('click', function () {
        if (replayTimer) clearInterval(replayTimer);
        replayTimer = setInterval(function () {
            if (!replay || replayCursor >= replay.steps.length - 1) { clearInterval(replayTimer); return; }
            replayCursor += 1;
            showReplay();
        }, 700);
    });
    document.getElementById('replayPause').addEventListener('click', function () { if (replayTimer) clearInterval(replayTimer); });
    document.getElementById('runCompare').addEventListener('click', function () {
        document.getElementById('compareStatus').textContent = 'Comparing…';
        fetch('/api/strategies/compare', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                symbol: document.getElementById('backtestSymbol').value.trim().toUpperCase(),
                timeframe: document.getElementById('backtestTimeframe').value,
                start: document.getElementById('backtestStart').value || null,
                end: document.getElementById('backtestEnd').value || null,
                capital: document.getElementById('backtestCapital').value,
                risk_pct: document.getElementById('backtestRisk').value,
            }),
        }).then(function (response) { return response.json(); }).then(function (body) {
            if (!body.job_id) throw new Error(body.message || 'DATA UNAVAILABLE');
            pollJob('/api/strategies/compare/' + body.job_id, function (job) {
                const rows = (job.result && job.result.strategies) || [];
                document.getElementById('compareRows').innerHTML = rows.map(function (row) {
                    if (row.data_unavailable) return '<tr><td>' + esc(row.name) + '</td><td colspan="7">' + esc(row.message) + '</td></tr>';
                    return '<tr><td>' + esc(row.name) + '</td><td>' + esc(row.total_trades) + '</td><td>' + esc(row.win_rate) + '</td><td>' + esc(row.profit_factor) + '</td><td>' + money(row.net_profit) + '</td><td>' + esc(row.max_drawdown_pct) + '%</td><td>' + money(row.expectancy) + '</td><td class="wrap">' + esc(row.rule) + '</td></tr>';
                }).join('') || '<tr><td colspan="8">Data Unavailable</td></tr>';
                document.getElementById('compareStatus').textContent = (job.result && job.result.disclaimer) || job.status;
            }, document.getElementById('compareStatus'));
        }).catch(function (error) {
            document.getElementById('compareStatus').textContent = error.message || 'Data Unavailable';
        });
    });

    let quoteCandleTime = null;
    let monitorCache = [];

    function paintMonitor(rows) {
        const filter = (document.getElementById('monitorFilter').value || '').toLowerCase();
        const sort = document.getElementById('monitorSort').value;
        const visible = rows.filter(function (row) {
            const blob = (row.symbol + ' ' + row.signal + ' ' + row.data_status).toLowerCase();
            return !filter || blob.indexOf(filter) >= 0;
        }).sort(function (a, b) {
            if (sort === 'symbol') return String(a.symbol).localeCompare(String(b.symbol));
            if (sort === 'signal') return String(a.signal).localeCompare(String(b.signal));
            return Number(b.score || 0) - Number(a.score || 0);
        });
        document.getElementById('monitorRows').innerHTML = visible.map(function (row) {
            const when = row.signal_time ? new Date(row.signal_time * 1000).toLocaleString('en-IN', { timeZone: 'Asia/Kolkata' }) : '—';
            return '<tr data-symbol="' + esc(row.symbol) + '"><td>' + esc(row.symbol) + '</td><td>' + money(row.ltp) + '</td><td>' + esc(row.timeframe) + '</td><td>' + esc(row.signal) + '</td><td>' + esc(row.score) + '</td><td>' + money(row.entry) + '</td><td>' + money(row.stop_loss) + '</td><td>' + money(row.target_1) + '</td><td>' + money(row.target_2) + '</td><td>' + esc(row.demand || 'Unavailable') + '</td><td>' + esc(row.supply || 'Unavailable') + '</td><td>' + esc(row.trend || '—') + '</td><td>' + esc(when) + '</td><td>' + esc(row.data_status || 'UNAVAILABLE') + '</td></tr>';
        }).join('') || '<tr><td colspan="14">Data Unavailable</td></tr>';
    }

    document.getElementById('loadMonitor').addEventListener('click', function () {
        const stored = JSON.parse(localStorage.getItem('projectSmWatchlist') || '[]');
        const symbols = (stored.length ? stored : [window.smGetSymbol()]).slice(0, 8);
        const timeframe = document.getElementById('analysisTimeframe').value || '1d';
        document.getElementById('monitorRows').innerHTML = '<tr><td colspan="14">Loading delayed candles…</td></tr>';
        fetch('/api/market/monitor?symbols=' + encodeURIComponent(symbols.join(',')) + '&timeframe=' + encodeURIComponent(timeframe))
            .then(function (response) { return response.json(); })
            .then(function (body) {
                monitorCache = body.rows || [];
                paintMonitor(monitorCache);
            })
            .catch(function () {
                document.getElementById('monitorRows').innerHTML = '<tr><td colspan="14">Data Unavailable</td></tr>';
            });
    });
    document.getElementById('monitorRows').addEventListener('click', function (event) {
        const row = event.target.closest('tr');
        if (row && row.dataset.symbol && window.loadChart) window.loadChart(row.dataset.symbol);
    });
    document.getElementById('monitorFilter').addEventListener('input', function () { paintMonitor(monitorCache); });
    document.getElementById('monitorSort').addEventListener('change', function () { paintMonitor(monitorCache); });

    document.getElementById('startLiveScan').addEventListener('click', function () {
        const conditions = selectedConditions();
        const status = document.getElementById('liveScanStatus');
        if (!conditions.length) {
            status.textContent = 'Select at least one scanner condition.';
            return;
        }
        status.textContent = 'Starting one delayed scan…';
        fetch('/api/market/live-scan/start', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                universe: document.getElementById('analysisUniverse').value,
                timeframe: document.getElementById('analysisTimeframe').value,
                conditions: conditions,
                logic: (document.querySelector('input[name="analysisLogic"]:checked') || {}).value || 'AND',
                limit: document.getElementById('analysisLimit').value || 8,
            }),
        }).then(function (response) { return response.json().then(function (body) { return { ok: response.ok, body: body }; }); })
            .then(function (result) {
                const body = result.body;
                status.textContent = body.error || ((body.running ? 'Running' : 'Stopped') + ' · ' + (body.data_status || 'DELAYED') + ' · stream NOT CONNECTED');
            })
            .catch(function () { status.textContent = 'Data Unavailable'; });
    });
    document.getElementById('stopLiveScan').addEventListener('click', function () {
        fetch('/api/market/live-scan/stop', { method: 'POST' }).then(function (response) { return response.json(); }).then(function (body) {
            document.getElementById('liveScanStatus').textContent = body.running ? 'Still running' : 'Live scan stopped. Stream NOT CONNECTED.';
        });
    });

    function pollAnalysisQuote() {
        const workspace = document.querySelector('.workspace');
        if (!workspace || !workspace.classList.contains('analysis-open') || !window.smGetSymbol) return;
        const symbol = window.smGetSymbol();
        const chartTf = window.smGetTimeframe ? window.smGetTimeframe() : '1d';
        const supported = { '1m': 1, '3m': 1, '5m': 1, '15m': 1, '30m': 1, '1h': 1, '4h': 1, '1d': 1 };
        const timeframe = supported[chartTf] ? chartTf : '1d';
        fetch('/api/market/quote/' + encodeURIComponent(symbol) + '?timeframe=' + encodeURIComponent(timeframe))
            .then(function (response) { return response.json(); })
            .then(function (quote) {
                const statusNode = document.getElementById('analysisDataStatus');
                if (statusNode) {
                    const quoteStatus = quote.data_status || 'UNAVAILABLE';
                    statusNode.textContent = quoteStatus;
                    statusNode.className = 'sa-badge ' + badgeClass(quoteStatus);
                }
                if (quote.ltp == null || window.smGetSymbol() !== symbol) return;
                const priceText = '₹' + Number(quote.ltp).toLocaleString('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
                const price = document.getElementById('analysisPrice');
                if (price) price.textContent = priceText;
                const side = document.getElementById('sidePrice');
                if (side) side.textContent = priceText;
                if (timeframe !== chartTf || !window.smSeries || quote.candle_time == null) return;
                try {
                    if (quoteCandleTime !== null && quote.candle_time !== quoteCandleTime && window.loadChart) {
                        quoteCandleTime = quote.candle_time;
                        window.loadChart(symbol);
                        return;
                    }
                    quoteCandleTime = quote.candle_time;
                    window.smSeries.update({ time: quote.candle_time, open: quote.open, high: quote.high, low: quote.low, close: quote.ltp });
                } catch (error) { /* the quote label stays; a mismatched candle is not a missing price */ }
            })
            .catch(function () {
                const statusNode = document.getElementById('analysisDataStatus');
                if (statusNode && statusNode.textContent === 'DATA UNAVAILABLE') {
                    statusNode.className = 'sa-badge bad';
                }
            });
    }
    setInterval(pollAnalysisQuote, 20000);

    let paperLines = [];
    function drawPaperLines(openTrades) {
        paperLines.forEach(function (line) {
            try { candle.removePriceLine(line); } catch (error) { /* already removed */ }
        });
        paperLines = [];
        openTrades.forEach(function (trade) {
            const tag = 'Paper ' + trade.direction;
            [[trade.entry_price, '#e2e8f0', 0, tag + ' entry'], [trade.stop_loss, '#f59e0b', 2, tag + ' SL'], [trade.target_1, '#38bdf8', 2, tag + ' T1']].forEach(function (item) {
                if (!Number.isFinite(Number(item[0]))) return;
                paperLines.push(candle.createPriceLine({ price: Number(item[0]), color: item[1], lineWidth: 1, lineStyle: item[2], axisLabelVisible: true, title: item[3] }));
            });
        });
    }

    function loadPaperMarkers() {
        const symbol = window.smGetSymbol && window.smGetSymbol();
        if (!symbol) return;
        fetch('/api/paper/trades?symbol=' + encodeURIComponent(symbol))
            .then(function (response) { return response.json(); })
            .then(function (body) {
                paperTrades = (body.trades || []).map(function (trade) {
                    const entry = Date.parse(trade.entry_time);
                    const exit = Date.parse(trade.exit_time);
                    return {
                        time: Number.isFinite(entry) ? Math.floor(entry / 1000) : null,
                        exit_time: Number.isFinite(exit) ? Math.floor(exit / 1000) : null,
                        side: trade.direction,
                        entry: trade.entry_price,
                        stop_loss: trade.stop_loss,
                        target_1: trade.target_1,
                        target_2: trade.target_2,
                        target_3: trade.target_3,
                        exit_reason: trade.exit_reason,
                        reasons: trade.signal_reasons,
                        score: trade.signal_score,
                        pnl: trade.pnl,
                        pnl_pct: trade.pnl_pct,
                        label: trade.status,
                        risk_reward: trade.risk_reward,
                        timeframe: trade.timeframe,
                    };
                }).filter(function (trade) { return trade.time; });
                drawPaperLines((body.trades || []).filter(function (trade) { return trade.status === 'open'; }));
                applyMarkers();
            })
            .catch(function () { /* paper markers stay hidden when the book is unavailable */ });
    }

    function paperTimeframe() {
        const chartFrame = window.smGetTimeframe && window.smGetTimeframe();
        if (BAR_SECONDS[chartFrame]) return chartFrame;
        return document.getElementById('analysisTimeframe').value || '1d';
    }

    function showPaperPreview(direction) {
        const node = document.getElementById('paperConfirm');
        const symbol = window.smGetSymbol();
        node.textContent = 'Checking the last closed candle…';
        const capital = Number(document.getElementById('sizeCapital').value) || 100000;
        const risk = Number(document.getElementById('sizeRisk').value) || 1;
        fetch('/api/paper/preview', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                symbol: symbol,
                direction: direction,
                timeframe: paperTimeframe(),
                capital: capital,
                risk_pct: risk,
                maximum_loss: capital * risk / 100,
            }),
        }).then(function (response) { return response.json().then(function (body) { return { ok: response.ok, body: body }; }); })
            .then(function (result) {
                const body = result.body;
                if (!result.ok) {
                    node.textContent = body.message || 'DATA UNAVAILABLE';
                    return;
                }
                node.innerHTML = [
                    '<div>' + esc(body.symbol) + ' ' + esc(body.direction) + ' · ' + esc(body.data_status) + '</div>',
                    body.manual || body.capped_by ? '<div class="paper-manual">' + esc(body.message) + '</div>' : '',
                    '<label>Entry <input id="paperEntry" type="number" step="0.01" value="' + esc(body.entry_price) + '"></label>',
                    '<label>Quantity <input id="paperQty" type="number" step="0.0001" value="' + esc(body.quantity) + '"></label>',
                    '<label>Stop <input id="paperStop" type="number" step="0.01" value="' + esc(body.stop_loss) + '"></label>',
                    '<label>Target 1 <input id="paperTarget" type="number" step="0.01" value="' + esc(body.target_1) + '"></label>',
                    '<div>Risk ' + money(body.risk) + ' · R:R ' + esc(body.risk_reward || '—') + '</div>',
                    '<div>' + (body.signal_reasons || []).map(esc).join(' · ') + '</div>',
                    '<button id="confirmPaper" type="button">Confirm paper trade</button>',
                ].join('');
                document.getElementById('confirmPaper').onclick = function () {
                    fetch('/api/paper/trades', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({
                            symbol: body.symbol,
                            direction: body.direction,
                            timeframe: body.timeframe,
                            entry_price: document.getElementById('paperEntry').value,
                            quantity: document.getElementById('paperQty').value,
                            stop_loss: document.getElementById('paperStop').value,
                            target_1: document.getElementById('paperTarget').value,
                            target_2: body.target_2,
                            target_3: body.target_3,
                            signal_score: body.signal_score,
                            signal_reasons: body.signal_reasons,
                            demand_zone: body.demand_zone,
                            supply_zone: body.supply_zone,
                            market_trend: body.market_trend,
                            maximum_loss: capital * risk / 100,
                            source: body.manual ? 'manual' : 'signal',
                            chart_ref: body.symbol + ' ' + body.timeframe,
                        }),
                    }).then(function (response) { return response.json().then(function (saved) { return { ok: response.ok, saved: saved }; }); })
                        .then(function (saved) {
                            if (!saved.ok) {
                                const info = saved.saved || {};
                                const distance = Math.abs(Number(document.getElementById('paperEntry').value) - Number(document.getElementById('paperStop').value));
                                const cap = Number(info.maximum_loss);
                                const hint = Number.isFinite(cap) && distance > 0
                                    ? ' Risk ' + money(info.risk) + ' is above the ' + money(cap) + ' limit. At this stop the largest quantity is ' + (Math.floor(cap / distance * 10000) / 10000) + '.'
                                    : '';
                                let error = node.querySelector('.paper-error');
                                if (!error) {
                                    error = document.createElement('div');
                                    error.className = 'paper-error';
                                    document.getElementById('confirmPaper').before(error);
                                }
                                error.textContent = 'Not saved: ' + (info.message || 'DATA UNAVAILABLE') + '.' + hint;
                                return;
                            }
                            node.innerHTML = '<div class="paper-saved">' + esc(body.symbol) + ' ' + esc(body.direction)
                                + ' paper trade saved. No broker order was sent. It is listed in the <a data-open-paper>Paper Trades</a> tab below and marked on the chart.</div>';
                            loadPaperMarkers();
                            openPaperTab();
                        });
                };
            })
            .catch(function () { node.textContent = 'DATA UNAVAILABLE'; });
    }
    function paperClock(value) {
        const stamp = Date.parse(value);
        return Number.isFinite(stamp) ? new Date(stamp).toLocaleString('en-IN', { hour12: false, day: '2-digit', month: 'short', hour: '2-digit', minute: '2-digit' }) : '—';
    }

    function pnlCell(value, text) {
        const number = Number(value);
        const cls = !Number.isFinite(number) ? '' : number >= 0 ? 'positive' : 'negative';
        return '<td class="' + cls + '">' + text + '</td>';
    }

    function loadPaperBook() {
        const openRows = document.getElementById('saPaperOpen');
        const closedRows = document.getElementById('saPaperClosed');
        const summary = document.getElementById('saPaperSummary');
        openRows.innerHTML = '<tr><td colspan="13">Loading paper trades…</td></tr>';
        fetch('/api/paper/portfolio')
            .then(function (response) { return response.json(); })
            .then(function (book) {
                const open = book.open_positions || [];
                const closed = (book.closed_positions || []).slice().sort(function (a, b) { return String(b.exit_time || '').localeCompare(String(a.exit_time || '')); });
                document.getElementById('paperTab').textContent = 'Paper Trades (' + open.length + ')';
                summary.textContent = 'Cash ' + money(book.available_capital) + ' · Used ' + money(book.used_capital)
                    + ' · Total P&L ' + money(book.total_pnl) + '. Virtual cash only. No broker order is sent. LTP is a delayed Yahoo price.';
                openRows.innerHTML = open.length ? open.map(function (row) {
                    const canExit = Number.isFinite(Number(row.ltp));
                    return '<tr data-paper-symbol="' + esc(row.symbol) + '"><td>' + esc(paperClock(row.entry_time)) + '</td><td><b>' + esc(row.symbol) + '</b></td><td class="' + (row.direction === 'SELL' ? 'negative' : 'positive') + '">' + esc(row.direction) + '</td><td>' + esc(row.remaining_quantity) + '</td><td>' + inr(row.entry_price) + '</td><td>' + inr(row.ltp) + '</td><td>' + inr(row.stop_loss) + '</td><td>' + inr(row.target_1) + '</td><td>' + inr(row.target_2) + '</td>'
                        + pnlCell(row.pnl, inr(row.pnl)) + pnlCell(row.pnl, row.pnl_pct === null || row.pnl_pct === undefined ? '—' : esc(row.pnl_pct) + '%')
                        + '<td>' + esc(row.source || '—') + '</td><td>' + (canExit ? '<button type="button" data-paper-exit="' + esc(row.id) + '" data-price="' + esc(row.ltp) + '" title="Close at the delayed LTP">Exit at LTP</button>' : '<span class="analysis-note">LTP unavailable</span>') + '</td></tr>';
                }).join('') : '<tr><td colspan="13">No open paper trades.</td></tr>';
                closedRows.innerHTML = closed.length ? closed.map(function (row) {
                    return '<tr data-paper-symbol="' + esc(row.symbol) + '"><td>' + esc(paperClock(row.entry_time)) + '</td><td>' + esc(paperClock(row.exit_time)) + '</td><td><b>' + esc(row.symbol) + '</b></td><td>' + esc(row.direction) + '</td><td>' + esc(row.quantity) + '</td><td>' + inr(row.entry_price) + '</td><td>' + inr(row.exit_price) + '</td><td>' + esc(row.exit_reason || '') + '</td>'
                        + pnlCell(row.pnl, inr(row.pnl)) + pnlCell(row.pnl, row.pnl_pct === null || row.pnl_pct === undefined ? '—' : esc(row.pnl_pct) + '%') + '</tr>';
                }).join('') : '<tr><td colspan="10">No closed paper trades.</td></tr>';
            })
            .catch(function () { openRows.innerHTML = '<tr><td colspan="13">DATA UNAVAILABLE</td></tr>'; });
    }

    function openPaperTab() {
        document.getElementById('paperTab').click();
    }

    document.getElementById('paperTab').addEventListener('click', loadPaperBook);
    document.getElementById('saPaperRefresh').addEventListener('click', loadPaperBook);
    document.getElementById('saPaperSync').addEventListener('click', function () {
        const summary = document.getElementById('saPaperSummary');
        summary.textContent = 'Checking stop and target against the latest delayed candle…';
        fetch('/api/paper/sync', { method: 'POST' }).then(function () { loadPaperBook(); loadPaperMarkers(); })
            .catch(function () { summary.textContent = 'DATA UNAVAILABLE'; });
    });
    document.getElementById('bottom-paper').addEventListener('click', function (event) {
        const exit = event.target.closest('[data-paper-exit]');
        if (exit) {
            exit.disabled = true;
            fetch('/api/paper/trades/' + encodeURIComponent(exit.dataset.paperExit) + '/exit', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ mode: 'full', exit_price: Number(exit.dataset.price), reason: 'manual exit at delayed LTP' }),
            }).then(function () { loadPaperBook(); loadPaperMarkers(); });
            return;
        }
        const row = event.target.closest('tr[data-paper-symbol]');
        if (row && row.dataset.paperSymbol !== window.smGetSymbol()) loadChart(row.dataset.paperSymbol);
    });
    document.getElementById('paperConfirm').addEventListener('click', function (event) {
        if (event.target.closest('[data-open-paper]')) openPaperTab();
    });
    loadPaperBook();

    document.getElementById('paperBuy').addEventListener('click', function () { showPaperPreview('BUY'); });
    document.getElementById('paperSell').addEventListener('click', function () { showPaperPreview('SELL'); });
    document.getElementById('openBacktestTab').addEventListener('click', function () {
        if (window.smGetSymbol()) document.getElementById('backtestSymbol').value = window.smGetSymbol();
        document.querySelector('#bottomTabs [data-bottom="backtest"]').click();
    });
    document.querySelector('#bottomTabs [data-bottom="backtest"]').addEventListener('click', function () {
        const input = document.getElementById('backtestSymbol');
        if (!input.value.trim() && window.smGetSymbol()) input.value = window.smGetSymbol();
    });
    document.getElementById('openHistoryTab').addEventListener('click', function () {
        document.querySelector('#bottomTabs [data-bottom="history"]').click();
    });

    const rangeBars = { '1d': 160, '5d': 5, '1m': 22, '3m': 63, '6m': 126, '1y': 252, '5y': 0 };
    let activeTemplate = '';
    window.smClearTemplate = function () {
        activeTemplate = '';
        document.querySelectorAll('#saChartToolbar [data-template]').forEach(function (button) {
            button.classList.remove('sa-template-on');
        });
    };
    document.getElementById('saChartToolbar').addEventListener('click', function (event) {
        const range = event.target.dataset.range;
        const draw = event.target.dataset.draw;
        const template = event.target.dataset.template;
        if (range) {
            document.querySelectorAll('#saChartToolbar [data-range]').forEach(function (button) {
                button.classList.toggle('active', button.dataset.range === range);
            });
            window.smPendingBars = rangeBars[range] || 0;
            if (window.smSetTimeframe) window.smSetTimeframe('1d');
            const symbol = window.smGetSymbol && window.smGetSymbol();
            if (symbol) loadChart(symbol);
            return;
        }
        if (draw && window.smSetDrawingTool) {
            window.smSetDrawingTool(window.smDrawingTool === draw ? '' : draw);
            return;
        }
        if (!template) return;
        const presets = {
            full: ['ema20', 'ema50', 'ema200', 'sma50', 'sma200', 'bb', 'supertrend', 'vwap', 'volume', 'rsi', 'macd', 'zones'],
            trend: ['ema20', 'ema50', 'ema200', 'sma50', 'sma200', 'volume', 'zones'],
            momentum: ['volume', 'rsi', 'macd', 'zones'],
            price: ['volume', 'zones'],
        };
        const on = presets[template] || [];
        const removing = activeTemplate === template;
        activeTemplate = removing ? '' : template;
        document.querySelectorAll('#analysisChartTools input[data-overlay]').forEach(function (box) {
            const listed = on.indexOf(box.getAttribute('data-overlay')) >= 0;
            box.checked = removing ? (box.checked && !listed) : listed;
            overlayFlags[box.getAttribute('data-overlay')] = box.checked;
        });
        document.querySelectorAll('#saChartToolbar [data-template]').forEach(function (button) {
            button.classList.toggle('sa-template-on', button.dataset.template === activeTemplate);
        });
        const menu = event.target.closest('details');
        if (menu) menu.removeAttribute('open');
        if (lastPayload) {
            applyOverlays(lastPayload.overlays || {}, lastPayload.price);
            renderLegend(lastPayload);
            drawFib(lastPayload.fib_levels || []);
            drawSessionVwap(lastPayload);
            window.smSetZones(overlayFlags.zones ? filteredZones(lastPayload.zones || []) : []);
        }
    });
    const addWatch = document.getElementById('addToWatchlist');
    if (addWatch) addWatch.addEventListener('click', function () {
        const symbol = window.smGetSymbol && window.smGetSymbol();
        if (!symbol) return;
        let list = [];
        try { list = JSON.parse(localStorage.getItem('projectSmWatchlist') || '[]'); } catch (error) { list = []; }
        if (!Array.isArray(list)) list = [];
        if (list.indexOf(symbol) < 0) list.push(symbol);
        localStorage.setItem('projectSmWatchlist', JSON.stringify(list));
        if (typeof renderWatchlist === 'function') renderWatchlist();
        addWatch.textContent = '✓ Added';
        updateWatchCount();
    });

    function updateWatchCount() {
        let list = [];
        try { list = JSON.parse(localStorage.getItem('projectSmWatchlist') || '[]'); } catch (error) { list = []; }
        const tab = document.getElementById('watchTab');
        if (tab) tab.textContent = 'My Watchlist (' + (Array.isArray(list) ? list.length : 0) + ')';
    }

    const RESULT_COLUMNS = ['#', 'Stock', 'LTP', 'Chg %', 'Volume', 'RSI', 'MACD', 'Trend (D/W/M)', '20 EMA > 50 EMA', 'Demand Zone', 'Supply Zone', 'Pattern', 'Remarks', 'Signal', 'Score', 'Entry', 'Stop Loss', 'Target 1', 'Target 2', 'Target 3', 'Risk/Reward'];
    const COLUMN_KEY = 'projectSmResultColumns';

    function hiddenColumns() {
        try {
            const saved = JSON.parse(localStorage.getItem(COLUMN_KEY) || 'null');
            if (Array.isArray(saved)) return saved;
        } catch (error) { /* fall back to the default set */ }
        return RESULT_COLUMNS.slice(13);
    }

    function applyColumns() {
        const hidden = hiddenColumns();
        let style = document.getElementById('saColumnStyle');
        if (!style) {
            style = document.createElement('style');
            style.id = 'saColumnStyle';
            document.head.appendChild(style);
        }
        style.textContent = RESULT_COLUMNS.map(function (name, index) {
            if (hidden.indexOf(name) < 0) return '';
            const nth = index + 2;
            return '#resultsTable th:nth-child(' + nth + '),#resultsTable td:nth-child(' + nth + '){display:none}';
        }).join('');
    }

    function renderColumnMenu() {
        const menu = document.getElementById('columnMenu');
        if (!menu) return;
        const hidden = hiddenColumns();
        menu.innerHTML = RESULT_COLUMNS.map(function (name) {
            return '<label><input type="checkbox" data-column="' + esc(name) + '"' + (hidden.indexOf(name) < 0 ? ' checked' : '') + '> ' + esc(name) + '</label>';
        }).join('');
        applyColumns();
    }

    document.getElementById('columnMenu').addEventListener('change', function () {
        const hidden = Array.from(document.querySelectorAll('#columnMenu input[data-column]')).filter(function (box) { return !box.checked; }).map(function (box) { return box.dataset.column; });
        localStorage.setItem(COLUMN_KEY, JSON.stringify(hidden));
        applyColumns();
    });
    document.getElementById('selectAllRows').addEventListener('change', function (event) {
        document.querySelectorAll('#analysisRows input[data-row]').forEach(function (box) { box.checked = event.target.checked; });
    });
    document.getElementById('saUndo').addEventListener('click', function () {
        if (window.smUndoDrawing) window.smUndoDrawing();
    });
    document.getElementById('saRedo').addEventListener('click', function () {
        if (window.smRedoDrawing) window.smRedoDrawing();
    });
    document.getElementById('zoneTfButtons').addEventListener('click', function (event) {
        let tf = event.target.dataset.zoneTf;
        if (!tf) return;
        if (window.smZoneFocus === tf) tf = 'all';
        window.smZoneFocus = tf;
        document.querySelectorAll('#zoneTfButtons button').forEach(function (button) {
            button.classList.toggle('active', button.dataset.zoneTf === tf);
        });
        if (lastPayload) {
            renderZones(lastPayload);
            window.smSetZones(overlayFlags.zones ? filteredZones(lastPayload.zones || []) : []);
        }
    });
    document.querySelector('.sa-topnav').addEventListener('click', function (event) {
        const bottom = event.target.closest('[data-sa-bottom]');
        if (bottom) {
            const tab = document.querySelector('#bottomTabs [data-bottom="' + bottom.dataset.saBottom + '"]');
            if (tab) tab.click();
            bottom.closest('details').removeAttribute('open');
            return;
        }
        const button = event.target.closest('[data-sa-nav]');
        const dropdown = button && button.closest('details');
        if (dropdown) dropdown.removeAttribute('open');
        if (!button || button.dataset.saNav === 'analysis' || button.dataset.saNav === 'dashboard') {
            if (button && button.dataset.saNav === 'dashboard') openStockAnalysis();
            return;
        }
        const name = button.dataset.saNav;
        const menu = Array.from(document.querySelectorAll('.nav-menu button'));
        const match = {
            dashboard: function (item) { return item.dataset.target === 'dashboard'; },
            scanner: function (item) { return item.dataset.target === 'scannerPanel'; },
            supply: function (item) { return /supply/i.test(item.textContent); },
            watchlist: function (item) { return item.id === 'watchlistNav'; },
            paper: function (item) { return item.dataset.target === 'paperPanel'; },
            alerts: function (item) { return item.dataset.target === 'alertsPanel'; },
            chart: function (item) { return /live market/i.test(item.textContent); },
            fundamental: function (item) { return item.id === 'fundamentalButton'; },
            settings: function (item) { return item.dataset.target === 'settingsPanel'; },
            backtest: function (item) { return item.dataset.target === 'backtestPanel'; },
            journal: function (item) { return item.dataset.target === 'journalPanel'; },
        }[name];
        const target = match ? menu.find(match) : null;
        if (target) target.click();
    });
    const saSearch = document.getElementById('saSearch');
    const saSuggest = document.createElement('div');
    saSuggest.className = 'sa-suggest';
    saSuggest.setAttribute('role', 'listbox');
    saSearch.parentElement.appendChild(saSuggest);
    let saMatches = [];
    let saActive = -1;

    function searchMatches(text) {
        const query = text.trim().toUpperCase();
        if (!query) return [];
        const ranked = [];
        (window.stockDirectory || []).forEach(function (stock) {
            const symbol = String(stock.symbol || '').toUpperCase();
            const company = String(stock.company || '').toUpperCase();
            let rank = -1;
            if (symbol === query) rank = 0;
            else if (symbol.startsWith(query)) rank = 1;
            else if (company.startsWith(query)) rank = 2;
            else if (company.split(/[\s.&-]+/).some(function (word) { return word.startsWith(query); })) rank = 3;
            else if (symbol.includes(query) || company.includes(query)) rank = 4;
            if (rank >= 0) ranked.push({ rank: rank, stock: stock });
        });
        ranked.sort(function (a, b) { return a.rank - b.rank || String(a.stock.symbol).localeCompare(String(b.stock.symbol)); });
        return ranked.slice(0, 10).map(function (item) { return item.stock; });
    }

    function highlight(text, query) {
        const value = String(text || '');
        const at = value.toUpperCase().indexOf(query);
        if (!query || at < 0) return esc(value);
        return esc(value.slice(0, at)) + '<mark>' + esc(value.slice(at, at + query.length)) + '</mark>' + esc(value.slice(at + query.length));
    }

    function closeSuggest() {
        saSuggest.classList.remove('open');
        saActive = -1;
    }

    function renderSuggest() {
        const query = saSearch.value.trim().toUpperCase();
        saMatches = searchMatches(saSearch.value);
        saActive = saMatches.length ? 0 : -1;
        if (!query) { closeSuggest(); return; }
        saSuggest.innerHTML = saMatches.length
            ? saMatches.map(function (stock, index) {
                return '<button type="button" role="option" data-index="' + index + '"' + (index === saActive ? ' class="active"' : '') + '><b>' + highlight(stock.symbol, query) + '</b><span>' + highlight(stock.company, query) + '</span></button>';
            }).join('')
            : '<div class="sa-suggest-empty">No NIFTY 500 stock matches "' + esc(saSearch.value.trim()) + '"</div>';
        saSuggest.classList.add('open');
    }

    function moveActive(step) {
        if (!saMatches.length) return;
        saActive = (saActive + step + saMatches.length) % saMatches.length;
        saSuggest.querySelectorAll('button').forEach(function (button, index) {
            button.classList.toggle('active', index === saActive);
            if (index === saActive) button.scrollIntoView({ block: 'nearest' });
        });
    }

    function pickSymbol(symbol) {
        if (!symbol) return;
        saSearch.value = symbol;
        closeSuggest();
        saSearch.blur();
        const search = document.getElementById('search');
        if (search) search.value = symbol;
        if (typeof window.loadChart === 'function') window.loadChart(symbol);
    }

    saSearch.setAttribute('autocomplete', 'off');
    saSearch.addEventListener('input', renderSuggest);
    saSearch.addEventListener('focus', function () { if (saSearch.value.trim()) renderSuggest(); });
    saSearch.addEventListener('keydown', function (event) {
        if (event.key === 'ArrowDown') { event.preventDefault(); moveActive(1); return; }
        if (event.key === 'ArrowUp') { event.preventDefault(); moveActive(-1); return; }
        if (event.key === 'Escape') { closeSuggest(); return; }
        if (event.key !== 'Enter') return;
        event.preventDefault();
        const typed = saSearch.value.trim().toUpperCase();
        if (!typed) return;
        const chosen = saMatches[saActive] || searchMatches(typed)[0];
        pickSymbol(chosen ? chosen.symbol : typed);
    });
    saSuggest.addEventListener('mousedown', function (event) {
        const button = event.target.closest('button[data-index]');
        if (!button) return;
        event.preventDefault();
        const stock = saMatches[Number(button.dataset.index)];
        if (stock) pickSymbol(stock.symbol);
    });
    document.addEventListener('click', function (event) {
        if (!saSearch.parentElement.contains(event.target)) closeSuggest();
    });
    document.getElementById('saToggleScanner').addEventListener('click', function () {
        document.getElementById('analysisPanel').classList.toggle('sa-hide-left');
        if (window.smResizeChart) window.smResizeChart();
    });
    document.getElementById('saToggleSide').addEventListener('click', function () {
        document.getElementById('analysisPanel').classList.toggle('sa-hide-right');
        if (window.smResizeChart) window.smResizeChart();
    });
    const saveBar = document.getElementById('saveNamedScanBar');
    if (saveBar) saveBar.addEventListener('click', function () { document.getElementById('saveNamedScan').click(); });
    const baseLoadChart = window.loadChart;
    if (typeof baseLoadChart === 'function') {
        window.loadChart = function (symbol) {
            const result = baseLoadChart.apply(this, arguments);
            const clean = window.smGetSymbol ? window.smGetSymbol() : '';
            if (analysisOpen && clean) {
                setChartLoading('Loading ' + clean + ' candles…');
                document.getElementById('analysisCompany').textContent = companyName(clean);
                document.getElementById('analysisSymbol').textContent = clean;
                loadQuickQuote(clean);
                refreshAnalysis();
            }
            return result;
        };
    }
    document.getElementById('saTheme').addEventListener('click', function () {
        applyTheme(document.body.classList.contains('sa-light') ? 'night' : 'day');
    });
    applyTheme(localStorage.getItem(THEME_KEY) || 'night');
    window.smZoneFocus = window.smZoneFocus || '1d';
    updateWatchCount();
    renderColumnMenu();
    openStockAnalysis();
})();
