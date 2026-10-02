(function () {
    const chart = window.smChart;
    const candle = window.smSeries;
    if (!chart || !candle || !document.getElementById('cbtPanel')) return;

    const DEFAULT_ENTRY = ['price_above_ema20', 'ema20_gt_ema50', 'rsi_above_50'];
    const ZONE_IDS = ['daily_demand', 'weekly_demand', 'monthly_demand', 'daily_supply', 'weekly_supply', 'monthly_supply'];
    const EXIT_CHOICES = [
        ['ema_cross_down', 'EMA 20 crosses below EMA 50'],
        ['macd_cross_down', 'MACD crosses below signal'],
        ['close_below_ema20', 'Close below EMA 20'],
        ['supertrend_flip', 'Supertrend turns bearish'],
        ['supply_reached', 'Supply zone reached (1D)'],
        ['rsi_overbought', 'RSI overbought'],
        ['daily_bearish', 'Daily trend bearish'],
    ];
    const FILTERS = [
        ['entry', 'Entry', '#22c55e', true], ['exit', 'Exit', '#a78bfa', true], ['target', 'Target', '#38bdf8', true],
        ['stop', 'Stop Loss', '#f59e0b', true], ['breakout', 'Breakout', '#22c55e', false], ['breakdown', 'Breakdown', '#ef4444', false],
        ['ema_cross', 'EMA Cross', '#eab308', false], ['volume_spike', 'Volume Spike', '#64748b', false], ['candlestick', 'Candlestick Pattern', '#c084fc', false],
        ['demand', 'Demand', '#16a34a', true], ['supply', 'Supply', '#dc2626', true], ['support', 'Support', '#0ea5e9', false],
        ['resistance', 'Resistance', '#fb923c', true], ['fibonacci', 'Fibonacci', '#a855f7', false],
    ];
    const STOP_STYLE = {
        swing_low: ['#ef4444', 2], atr: ['#f97316', 1], percent: ['#fb7185', 3], demand: ['#dc2626', 0], fixed: ['#f43f5e', 2],
    };
    const STOP_DEFAULT = { swing_low: '10', atr: '1.5', percent: '2', demand: '0.25', fixed: '' };
    const TARGET_DEFAULT = { r_multiple: '1, 2, 3', atr: '1, 2, 3', percent: '2, 4, 6' };
    const filters = {};
    FILTERS.forEach(function (item) { filters[item[0]] = item[3]; });

    let result = null;
    let selected = null;
    let lines = [];
    let timer = null;
    let catalog = [];

    function $(id) { return document.getElementById(id); }

    function esc(value) {
        return String(value ?? '').replace(/[&<>"']/g, function (ch) {
            return ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[ch];
        });
    }

    function inr(value) {
        if (value === null || value === undefined || !Number.isFinite(Number(value))) return '—';
        return '₹' + Number(value).toLocaleString('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
    }

    function onThisChart() {
        return !!(result && result.symbol === window.smGetSymbol() && result.timeframe === window.smGetTimeframe());
    }

    function barSeconds() {
        const map = { '5m': 300, '15m': 900, '1h': 3600, '4h': 14400, '1d': 86400, '1wk': 604800, '1mo': 2678400 };
        return map[(result && result.timeframe) || '1d'] || 86400;
    }

    window.smExtraMarkers = function () {
        if (!onThisChart()) return [];
        return (result.markers || []).filter(function (marker) {
            return filters[marker.kind];
        }).map(function (marker) {
            const mine = selected && marker.trade === selected.trade && ['entry', 'exit', 'stop', 'target'].indexOf(marker.kind) >= 0;
            const trade = marker.kind === 'entry' && filters.demand ? tradeById(marker.trade) : null;
            const text = trade && trade.demand_zone ? marker.text + ' · Demand' : marker.text;
            return { time: marker.time, position: marker.position, shape: marker.shape, color: marker.color, text: text, size: mine ? 2 : 1 };
        });
    };

    function zoneBoxes() {
        if (!onThisChart()) return [];
        const bar = barSeconds();
        const boxes = [];
        (result.trades || []).forEach(function (trade) {
            const signal = Number(trade.signal_time ?? trade.entry_time);
            const demand = trade.demand_zone;
            if (filters.demand && demand && Number.isFinite(signal)) {
                const formed = Number(demand.time);
                boxes.push({
                    type: 'demand', top: demand.top, bottom: demand.bottom,
                    start_time: Math.max(Number.isFinite(formed) ? formed : 0, signal - 40 * bar),
                    end_time: signal,
                    label: 'Demand → BUY #' + trade.trade,
                });
            }
            const supply = trade.supply_zone;
            const isSelected = selected && selected.trade === trade.trade;
            if (filters.supply && supply && isSelected && Number.isFinite(Number(trade.entry_time))) {
                boxes.push({
                    type: 'supply', top: supply.top, bottom: supply.bottom,
                    start_time: Number(trade.entry_time),
                    end_time: Number(trade.exit_time ?? trade.entry_time) + bar,
                    label: 'Supply above BUY #' + trade.trade,
                });
            }
        });
        return boxes;
    }

    function refreshMarkers() {
        if (typeof window.smSetTradeZones === 'function') window.smSetTradeZones('cbt', result ? zoneBoxes() : []);
        if (typeof window.smApplyMarkers === 'function') window.smApplyMarkers();
    }

    function clearLines() {
        lines.forEach(function (line) {
            try { candle.removePriceLine(line); } catch (error) { /* already removed */ }
        });
        lines = [];
    }

    function addLine(price, color, style, title, width) {
        if (!Number.isFinite(Number(price))) return;
        lines.push(candle.createPriceLine({
            price: Number(price), color: color, lineWidth: width || 1, lineStyle: style, axisLabelVisible: true, title: title,
        }));
    }

    function drawLines(trade) {
        clearLines();
        if (!trade || !onThisChart()) return;
        addLine(trade.entry, '#e2e8f0', 0, 'Entry', 2);
        const stopStyle = STOP_STYLE[(result.config || {}).stop_method] || ['#ef4444', 2];
        if (filters.stop) {
            addLine(trade.stop_loss, stopStyle[0], stopStyle[1], 'SL · ' + shortStop(), 2);
            if (Number(trade.final_stop) !== Number(trade.stop_loss)) addLine(trade.final_stop, '#f59e0b', 1, 'Trailing SL');
        }
        if (filters.target) {
            (trade.targets || []).forEach(function (target, index) {
                addLine(target.price, ['#22c55e', '#4ade80', '#86efac'][index], 0, 'T' + (index + 1) + (target.name === trade.exit_target ? ' (exit)' : ''));
            });
        }
        if (filters.exit && Number.isFinite(Number(trade.exit))) addLine(trade.exit, '#a78bfa', 3, 'Exit');
        const demand = trade.demand_zone;
        if (filters.demand && demand) {
            addLine(demand.top, '#16a34a', 1, 'Demand top');
            addLine(demand.bottom, '#16a34a', 1, 'Demand low');
        }
        const supply = trade.supply_zone;
        if (filters.supply && supply) {
            addLine(supply.bottom, '#dc2626', 1, 'Supply low');
            addLine(supply.top, '#dc2626', 1, 'Supply top');
        }
        if (filters.resistance) {
            (trade.resistance_levels || []).filter(function (level) { return level > trade.entry; }).slice(0, 3).forEach(function (level) {
                addLine(level, '#fb923c', 2, 'Resistance');
            });
        }
        if (filters.support) {
            (trade.resistance_levels || []).filter(function (level) { return level < trade.entry; }).slice(-2).forEach(function (level) {
                addLine(level, '#0ea5e9', 2, 'Prior swing high');
            });
        }
        if (filters.fibonacci) {
            (trade.fib_levels || []).forEach(function (level) {
                addLine(level.price, '#a855f7', 3, 'Fib ' + (Number(level.ratio) * 100).toFixed(1) + '%');
            });
        }
    }

    function shortStop() {
        const method = (result.config || {}).stop_method;
        return { swing_low: 'swing low', atr: 'ATR', percent: '%', demand: 'demand', fixed: '₹' }[method] || method;
    }

    function stateList(details, state, cls, mark) {
        const rows = (details || []).filter(function (item) { return item.state === state; });
        if (!rows.length) return '<li class="cbt-na">None</li>';
        return rows.map(function (item) {
            return '<li class="' + cls + '">' + mark + ' ' + esc(item.label) + (item.value ? ' <span class="cbt-na">(' + esc(item.value) + ')</span>' : '') + '</li>';
        }).join('');
    }

    function zoneText(zone) {
        if (!zone) return 'Not found';
        return inr(zone.bottom) + ' – ' + inr(zone.top) + (zone.grade ? ' · ' + esc(zone.grade) : '');
    }

    function renderDetail(trade) {
        const node = $('cbtDetail');
        if (!trade) {
            node.classList.add('sa-hidden');
            return;
        }
        const path = (trade.events || []).filter(function (event) {
            return ['entry', 'target', 'supply', 'exit'].indexOf(event.kind) >= 0;
        }).map(function (event) {
            return '<span title="' + esc(event.label) + '">' + esc(event.short) + ' ' + inr(event.price) + ' · ' + esc(event.date) + '</span>';
        }).join('→');
        const targets = (trade.targets || []).map(function (target) {
            return '<tr><td>' + esc(target.name) + (target.name === trade.exit_target ? ' (exit)' : '') + '</td><td>' + inr(target.price) + ' <span class="cbt-na">' + esc(target.method) + '</span></td></tr>';
        }).join('');
        const win = Number(trade.pnl) >= 0;
        node.style.borderLeftColor = win ? 'var(--sa-green)' : 'var(--sa-red)';
        node.innerHTML = [
            '<div class="cbt-head"><b>BUY #' + esc(trade.trade) + ' · ' + esc(result.symbol) + '</b><span>' + esc(result.timeframe) + '</span><button type="button" data-cbt-close title="Close">✕</button></div>',
            '<div>Signal on the close of <b>' + esc(trade.signal_date) + '</b> (close ' + inr(trade.signal_close) + '). Filled at the next open on <b>' + esc(trade.entry_date) + '</b>.</div>',
            '<h4>Why this entry — TRIGGERED</h4><ul>' + stateList(trade.conditions, 'match', 'cbt-ok', '✓') + '</ul>',
            '<h4>NOT TRIGGERED</h4><ul>' + stateList(trade.conditions, 'fail', 'cbt-no', '✗') + '</ul>',
            (trade.unavailable || []).length ? '<h4>Unavailable (never counted as matched)</h4><ul>' + stateList(trade.conditions, 'unavailable', 'cbt-na', '–') + '</ul>' : '',
            '<h4>Levels</h4><table>',
            '<tr><td>Entry</td><td>' + inr(trade.entry) + ' <span class="cbt-na">' + esc(trade.entry_basis) + '</span></td></tr>',
            '<tr><td>Stop loss</td><td>' + inr(trade.stop_loss) + ' <span class="cbt-na">' + esc(trade.stop_method) + '</span>' + (Number(trade.final_stop) !== Number(trade.stop_loss) ? '<br>Trailed to ' + inr(trade.final_stop) : '') + '</td></tr>',
            '<tr><td>Risk / share</td><td>' + inr(trade.risk) + '</td></tr>',
            targets,
            '<tr><td>Demand at entry</td><td>' + zoneText(trade.demand_zone) + '</td></tr>',
            '<tr><td>Supply at entry</td><td>' + zoneText(trade.supply_zone) + '</td></tr>',
            trade.zone_note ? '<tr><td></td><td class="cbt-na">' + esc(trade.zone_note) + '</td></tr>' : '',
            '</table>',
            '<h4>Movement</h4><div class="cbt-path">' + (path || '—') + '</div>',
            '<h4>Exit</h4><div>' + esc(trade.exit_reason) + '</div>',
            '<table><tr><td>Exit</td><td>' + inr(trade.exit) + ' on ' + esc(trade.exit_date) + '</td></tr>',
            '<tr><td>P/L</td><td class="' + (win ? 'cbt-ok' : 'cbt-no') + '">' + inr(trade.pnl) + ' (' + esc(trade.pnl_pct) + '%)</td></tr>',
            '<tr><td>Holding</td><td>' + esc(trade.holding_bars) + ' bars · ' + esc(trade.holding_days) + ' days</td></tr>',
            '<tr><td>Best / worst move</td><td>+' + inr(trade.mfe) + ' / −' + inr(trade.mae) + '</td></tr></table>',
            '<p class="cbt-note">Historical record from this test. It does not predict the next trade.</p>',
        ].join('');
        node.classList.remove('sa-hidden');
    }

    function select(trade, jump) {
        selected = trade || null;
        drawLines(selected);
        renderDetail(selected);
        refreshMarkers();
        document.querySelectorAll('#cbtRows tr').forEach(function (row) {
            row.classList.toggle('active', !!selected && Number(row.dataset.trade) === selected.trade);
        });
        document.querySelectorAll('#cbtTimeline button').forEach(function (button) {
            button.classList.toggle('active', !!selected && Number(button.dataset.trade) === selected.trade);
        });
        if (selected && jump) jumpTo(selected.entry_time, selected.exit_time);
    }

    function jumpTo(from, to) {
        const pad = barSeconds() * 25;
        const end = Math.max(Number(to || from), Number(from));
        try { chart.timeScale().setVisibleRange({ from: Number(from) - pad, to: end + pad }); } catch (error) { /* outside loaded range */ }
    }

    function tradeById(id) {
        return ((result && result.trades) || []).find(function (trade) { return trade.trade === Number(id); }) || null;
    }

    function renderTimeline() {
        const node = $('cbtTimeline');
        const slot = $('analysisChartSlot');
        if (!onThisChart()) {
            node.classList.add('sa-hidden');
            return;
        }
        if (slot && node.parentNode === slot) slot.appendChild(node);
        const kinds = { entry: 'entry', target: 'target', supply: 'supply', exit: 'exit', market: null };
        const items = (result.timeline || []).filter(function (event) {
            const kind = event.kind === 'exit' ? event.category : (kinds[event.kind] || event.category);
            return filters[kind] !== false;
        });
        const colors = { entry: '#22c55e', target: '#38bdf8', supply: '#dc2626', exit: '#a78bfa', stop: '#f59e0b' };
        node.innerHTML = '<b>Timeline</b>' + (items.length ? items.map(function (event) {
            const color = colors[event.category] || colors[event.kind] || '#94a3b8';
            return '<button type="button" data-time="' + event.time + '" data-trade="' + event.trade + '" title="' + esc(event.label) + '"><span class="cbt-swatch" style="background:' + color + '"></span>' + esc(event.date) + ' · ' + esc(event.short) + ' ' + inr(event.price) + '</button>';
        }).join('') : '<span class="cbt-note">No events for the ticked filters.</span>');
        node.classList.remove('sa-hidden');
    }

    function renderCurrent() {
        const node = $('cbtCurrent');
        const current = result && result.current;
        if (!current || !onThisChart()) {
            node.classList.add('sa-hidden');
            return;
        }
        const matched = current.matched.length ? current.matched.map(function (item) {
            return '<li>✓ ' + esc(item.label) + (item.value ? ' <span class="cbt-note">(' + esc(item.value) + ')</span>' : '') + '</li>';
        }).join('') : '<li class="cbt-note">No entry condition is true on the latest close.</li>';
        const watch = current.watch.length ? current.watch.map(function (item) {
            return '<li><span class="cbt-watch-tag">Condition to watch</span>' + esc(item.label) + ' <span class="cbt-note">— ' + esc(item.detail) + '</span></li>';
        }).join('') : '<li class="cbt-note">Nothing to monitor for this rule set.</li>';
        const open = current.open_position;
        node.innerHTML = [
            '<h4>Current conditions · ' + esc(result.date_to) + ' close ' + inr(current.price) + '</h4><ul>' + matched + '</ul>',
            current.unavailable.length ? '<div class="cbt-note">Unavailable: ' + esc(current.unavailable.map(function (item) { return item.label; }).join(', ')) + '</div>' : '',
            open ? '<div>Open test position from ' + esc(open.entry_date) + ' at ' + inr(open.entry) + ', stop ' + inr(open.stop_loss) + '</div>' : '',
            '<h4>Conditions being monitored</h4><ul>' + watch + '</ul>',
            '<div class="cbt-note">' + esc(current.note) + '</div>',
        ].join('');
        node.classList.remove('sa-hidden');
    }

    function renderTable() {
        const trades = (result && result.trades) || [];
        $('cbtTab').classList.remove('sa-hidden');
        $('cbtTableNote').textContent = result
            ? result.symbol + ' · ' + result.timeframe + ' · ' + result.date_from + ' to ' + result.date_to + ' · ' + trades.length + ' trades. ' + (result.disclaimer || '')
            : 'Run Backtest Mode on the chart to list trades.';
        $('cbtRows').innerHTML = trades.length ? trades.map(function (trade) {
            const cls = Number(trade.pnl) >= 0 ? 'positive' : 'negative';
            return '<tr data-trade="' + trade.trade + '"><td>' + esc(trade.trade) + '</td><td>' + esc(trade.signal_date) + '</td><td>' + esc(trade.entry_date) + '</td><td>' + inr(trade.entry) + '</td><td>' + inr(trade.stop_loss) + '</td><td>' + inr(trade.target_1) + '</td><td>' + inr(trade.target_2) + '</td><td>' + inr(trade.target_3) + '</td><td>' + esc(trade.exit_date) + '</td><td>' + inr(trade.exit) + '</td><td class="' + cls + '">' + inr(trade.pnl) + '</td><td class="' + cls + '">' + esc(trade.pnl_pct) + '%</td><td>' + esc(trade.holding_bars) + ' bars</td><td>' + esc((trade.matched || []).join(', ')) + '</td><td>' + esc(trade.exit_reason) + '</td></tr>';
        }).join('') : '<tr><td colspan="15">No trades matched these rules in this window.</td></tr>';
    }

    function renderSummary() {
        const node = $('cbtSummary');
        if (!result) {
            node.innerHTML = '';
            return;
        }
        const pct = function (value) { return value === null || value === undefined ? '—' : value + '%'; };
        const stats = [
            ['Trades', result.total_trades], ['Win rate', pct(result.win_rate)], ['Net P/L', inr(result.net_profit)], ['Profit factor', result.profit_factor ?? '—'],
            ['Max drawdown', pct(result.max_drawdown_pct)], ['Avg holding', result.average_holding_bars === null ? '—' : result.average_holding_bars + ' bars'],
            ['Expectancy', inr(result.expectancy)], ['Final capital', inr(result.ending_capital)],
        ];
        node.innerHTML = '<div class="cbt-stats">' + stats.map(function (item) {
            return '<div><span>' + esc(item[0]) + '</span><b>' + esc(item[1]) + '</b></div>';
        }).join('') + '</div><p class="cbt-note">' + esc(result.methodology) + (result.skipped && result.skipped.length ? ' ' + result.skipped.length + ' signal(s) skipped: ' + esc(result.skipped[result.skipped.length - 1].reason) + '.' : '') + '</p>';
    }

    function renderAll() {
        renderSummary();
        renderTable();
        renderTimeline();
        renderCurrent();
        if (selected) select(tradeById(selected.trade), false);
        else refreshMarkers();
    }

    function clearAll() {
        result = null;
        selected = null;
        clearLines();
        $('cbtDetail').classList.add('sa-hidden');
        $('cbtTimeline').classList.add('sa-hidden');
        $('cbtCurrent').classList.add('sa-hidden');
        $('cbtSummary').innerHTML = '';
        $('cbtRows').innerHTML = '<tr><td colspan="15">No backtest trades yet.</td></tr>';
        setStatus('Cleared. Choose conditions and run.');
        refreshMarkers();
    }

    function setStatus(text, error) {
        const node = $('cbtStatus');
        node.textContent = text;
        node.classList.toggle('error', !!error);
    }

    function checked(container) {
        return Array.from(document.querySelectorAll('#' + container + ' input:checked')).map(function (input) { return input.value; });
    }

    function renderEntryList() {
        const daily = $('cbtTimeframe').value === '1d';
        const keep = checked('cbtEntryList');
        const active = keep.length ? keep : DEFAULT_ENTRY;
        $('cbtEntryList').innerHTML = catalog.map(function (group) {
            return '<span class="cbt-group">' + esc(group.label) + '</span>' + group.conditions.map(function (item) {
                const zone = ZONE_IDS.indexOf(item.id) >= 0;
                const off = zone && !daily;
                return '<label class="' + (off ? 'cbt-off' : '') + '" title="' + esc(item.label) + (off ? ' — daily timeframe only' : '') + '"><input type="checkbox" value="' + esc(item.id) + '"' + (active.indexOf(item.id) >= 0 && !off ? ' checked' : '') + (off ? ' disabled' : '') + '>' + esc(item.label) + '</label>';
            }).join('');
        }).join('');
    }

    function renderExitList() {
        const daily = $('cbtTimeframe').value === '1d';
        $('cbtExitList').innerHTML = EXIT_CHOICES.map(function (item) {
            const off = item[0] === 'supply_reached' && !daily;
            return '<label class="' + (off ? 'cbt-off' : '') + '"><input type="checkbox" value="' + item[0] + '"' + (item[0] === 'ema_cross_down' && !off ? ' checked' : '') + (off ? ' disabled' : '') + '>' + esc(item[1]) + '</label>';
        }).join('');
    }

    function renderFilters() {
        $('cbtFilters').innerHTML = FILTERS.map(function (item) {
            return '<label><input type="checkbox" value="' + item[0] + '"' + (filters[item[0]] ? ' checked' : '') + '><span class="cbt-swatch" style="background:' + item[2] + '"></span>' + esc(item[1]) + '</label>';
        }).join('');
    }

    async function loadCatalog() {
        if (catalog.length) return;
        try {
            const response = await fetch('/api/technical-scanner/catalog');
            const body = await response.json();
            catalog = body.categories || [];
        } catch (error) {
            catalog = [];
        }
        renderEntryList();
    }

    function openPanel() {
        const panel = $('cbtPanel');
        const opening = panel.classList.contains('sa-hidden');
        panel.classList.toggle('sa-hidden', !opening);
        $('cbtToggle').classList.toggle('active', opening);
        if (!opening) return;
        $('cbtStock').textContent = window.smGetSymbol() || '';
        const timeframe = window.smGetTimeframe();
        if (Array.from($('cbtTimeframe').options).some(function (option) { return option.value === timeframe; })) $('cbtTimeframe').value = timeframe;
        if (!$('cbtStart').value && ['1d', '1wk', '1mo'].indexOf($('cbtTimeframe').value) >= 0) {
            const start = new Date();
            start.setFullYear(start.getFullYear() - ($('cbtTimeframe').value === '1d' ? 2 : 8));
            $('cbtStart').value = start.toISOString().slice(0, 10);
        }
        renderExitList();
        loadCatalog().then(renderEntryList);
        renderFilters();
    }

    function parseValues(text) {
        const parts = String(text || '').split(/[,\s]+/).filter(Boolean).map(Number);
        return parts.length ? parts : null;
    }

    async function poll(jobId) {
        try {
            const response = await fetch('/api/chart-backtest/' + encodeURIComponent(jobId));
            const job = await response.json();
            if (!response.ok || job.status === 'error') {
                finish();
                setStatus(job.error || 'Backtest failed', true);
                return;
            }
            if (job.status === 'running') {
                setStatus('Running · ' + (job.stage || '') + (job.progress ? ' · ' + job.progress + '%' : ''));
                return;
            }
            finish();
            if (job.status === 'unavailable' || !job.result || job.result.data_unavailable) {
                setStatus((job.result && job.result.message) || 'DATA UNAVAILABLE', true);
                return;
            }
            result = job.result;
            selected = null;
            const trades = result.trades || [];
            setStatus(trades.length + ' historical trade(s) on ' + result.symbol + ' ' + result.timeframe + '. Click an ENTRY marker, a timeline chip or a table row.');
            if (!onThisChart()) {
                if (window.smGetTimeframe() !== result.timeframe && typeof window.smSetTimeframe === 'function') window.smSetTimeframe(result.timeframe);
                if (typeof window.loadChart === 'function') window.loadChart(result.symbol);
            }
            renderAll();
            if (trades.length) select(trades[trades.length - 1], true);
        } catch (error) {
            finish();
            setStatus(error.message || 'Backtest failed', true);
        }
    }

    function finish() {
        clearInterval(timer);
        timer = null;
        $('cbtRun').disabled = false;
    }

    async function run() {
        const symbol = window.smGetSymbol();
        const entry = checked('cbtEntryList');
        if (!symbol) return setStatus('Open a stock first.', true);
        if (!entry.length) return setStatus('Choose at least one entry condition.', true);
        const target = $('cbtTarget').value;
        const body = {
            symbol: symbol,
            name: $('cbtName').value,
            timeframe: $('cbtTimeframe').value,
            start: $('cbtStart').value || null,
            end: $('cbtEnd').value || null,
            entry_conditions: entry,
            entry_logic: $('cbtLogic').value,
            exit_conditions: checked('cbtExitList'),
            target_method: target,
            target_values: TARGET_DEFAULT[target] ? parseValues($('cbtTargetValues').value) : null,
            exit_at: $('cbtExitAt').value,
            stop_method: $('cbtStop').value,
            stop_value: $('cbtStopValue').value === '' ? null : Number($('cbtStopValue').value),
            trail_atr: $('cbtTrail').value === '' ? null : Number($('cbtTrail').value),
            zone_levels: $('cbtZones').checked,
            capital: Number($('cbtCapital').value),
            risk_pct: Number($('cbtRisk').value),
        };
        $('cbtRun').disabled = true;
        setStatus('Starting backtest on ' + symbol + '…');
        try {
            const response = await fetch('/api/chart-backtest', {
                method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
            });
            const payload = await response.json();
            if (!response.ok || payload.error || payload.data_unavailable) throw new Error(payload.error || payload.message || 'DATA UNAVAILABLE');
            clearInterval(timer);
            timer = setInterval(function () { poll(payload.job_id); }, 1500);
            poll(payload.job_id);
        } catch (error) {
            finish();
            setStatus(error.message || 'Backtest failed', true);
        }
    }

    function exportAudit() {
        if (!result) return setStatus('Run a backtest first.', true);
        const payload = {
            stock: result.symbol, timeframe: result.timeframe, strategy: result.strategy_name, config: result.config,
            date_from: result.date_from, date_to: result.date_to, run_id: result.run_id || null,
            methodology: result.methodology, signals: result.audit,
        };
        const blob = new Blob([JSON.stringify(payload, null, 2)], { type: 'application/json' });
        const link = document.createElement('a');
        link.href = URL.createObjectURL(blob);
        link.download = result.symbol + '_' + result.timeframe + '_backtest_audit.json';
        document.body.appendChild(link);
        link.click();
        setTimeout(function () { URL.revokeObjectURL(link.href); link.remove(); }, 500);
    }

    function nearestTrade(time) {
        if (!onThisChart()) return null;
        const limit = barSeconds() * 3;
        let best = null;
        let gap = Infinity;
        (result.trades || []).forEach(function (trade) {
            [trade.entry_time, trade.signal_time, trade.exit_time].concat((trade.events || []).map(function (event) { return event.time; })).forEach(function (stamp) {
                const distance = Math.abs(Number(stamp) - Number(time));
                if (distance < gap) {
                    gap = distance;
                    best = trade;
                }
            });
        });
        return gap <= limit ? best : null;
    }

    $('cbtToggle').addEventListener('click', openPanel);
    $('cbtClose').addEventListener('click', function () {
        $('cbtPanel').classList.add('sa-hidden');
        $('cbtToggle').classList.remove('active');
    });
    $('cbtRun').addEventListener('click', run);
    $('cbtClear').addEventListener('click', clearAll);
    $('cbtExport').addEventListener('click', exportAudit);
    $('cbtTimeframe').addEventListener('change', function () {
        renderEntryList();
        renderExitList();
        $('cbtZones').disabled = this.value !== '1d';
    });
    $('cbtTarget').addEventListener('change', function () {
        const preset = TARGET_DEFAULT[this.value];
        $('cbtTargetValues').disabled = !preset;
        $('cbtTargetValues').value = preset || '';
        $('cbtTargetValues').placeholder = preset ? preset : 'Levels come from the chart';
    });
    $('cbtStop').addEventListener('change', function () {
        $('cbtStopValue').value = STOP_DEFAULT[this.value];
        $('cbtStopValue').placeholder = this.value === 'fixed' ? '₹ distance' : '';
    });
    $('cbtFilters').addEventListener('change', function (event) {
        if (!event.target.value) return;
        filters[event.target.value] = event.target.checked;
        drawLines(selected);
        renderTimeline();
        refreshMarkers();
    });
    $('cbtDetail').addEventListener('click', function (event) {
        if (event.target.closest('[data-cbt-close]')) select(null, false);
    });
    $('cbtTimeline').addEventListener('click', function (event) {
        const button = event.target.closest('button[data-time]');
        if (!button) return;
        const trade = tradeById(button.dataset.trade);
        selected = trade;
        select(trade, false);
        jumpTo(Number(button.dataset.time), Number(button.dataset.time));
    });
    $('cbtRows').addEventListener('click', function (event) {
        const row = event.target.closest('tr[data-trade]');
        if (!row) return;
        const trade = tradeById(row.dataset.trade);
        if (!trade) return;
        if (!onThisChart()) {
            if (typeof window.smSetTimeframe === 'function') window.smSetTimeframe(result.timeframe);
            if (typeof window.loadChart === 'function') window.loadChart(result.symbol);
        }
        select(trade, true);
    });
    chart.subscribeClick(function (param) {
        if (!param || !param.time || window.smDrawingTool || !result) return;
        const trade = nearestTrade(param.time);
        if (trade) select(trade, false);
    });

    const previousLoaded = window.onChartLoaded;
    window.onChartLoaded = function () {
        if (typeof previousLoaded === 'function') previousLoaded.apply(this, arguments);
        if (!result) return;
        if (!onThisChart()) {
            clearLines();
            $('cbtDetail').classList.add('sa-hidden');
        } else if (selected) {
            drawLines(selected);
        }
        renderTimeline();
        renderCurrent();
        refreshMarkers();
    };
})();
