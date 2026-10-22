'use strict';

// ── Stock detail: candlestick chart (TradingView Lightweight Charts) ──────────
// Clicking a stock in the watchlist or a portfolio opens this modal instead of Yahoo.
// Candles + volume from /api/history (server.py proxies Yahoo, cached), a TradingView-
// style OHLC legend that follows the crosshair, and range tabs. The portfolio history
// chart stays a line (app.js buildLineChart).

const CANDLE_RANGES = [
    { key: '1d',  label: '1D', range: '1d',  interval: '5m',  intraday: true  },
    { key: '5d',  label: '5D', range: '5d',  interval: '30m', intraday: true  },
    { key: '1mo', label: '1M', range: '1mo', interval: '1h',  intraday: true  },
    { key: '6mo', label: '6M', range: '6mo', interval: '1d',  intraday: false },
    { key: '1y',  label: '1Y', range: '1y',  interval: '1d',  intraday: false },
];
const CANDLE_UP = '#52c48a', CANDLE_DOWN = '#c97070';

const _candle = { sym: null, rangeKey: '6mo', chart: null, req: 0 };

async function fetchOHLC(sym, r) {
    const res = await fetch(`/api/history?symbol=${encodeURIComponent(sym)}&range=${r.range}&interval=${r.interval}`,
        { signal: AbortSignal.timeout(15000) });
    const j = await res.json();
    const result = j?.data?.chart?.result?.[0];
    if (!j.ok || !result?.timestamp) throw new Error(j.error || 'No price history');
    const q = result.indicators?.quote?.[0] ?? {};
    const bars = [];
    let lastT = -Infinity;
    result.timestamp.forEach((t, i) => {
        const o = q.open?.[i], h = q.high?.[i], l = q.low?.[i], c = q.close?.[i];
        if ([o, h, l, c].some(v => v == null) || t <= lastT) return;   // Yahoo leaves gaps as null
        lastT = t;
        bars.push({ time: t, open: o, high: h, low: l, close: c, volume: q.volume?.[i] ?? 0 });
    });
    if (bars.length < 2) throw new Error('Not enough price history');
    return { bars, meta: result.meta ?? {} };
}

function candleModal() {
    let el = document.getElementById('modal-candle');
    if (el) return el;
    document.body.insertAdjacentHTML('beforeend', `
        <div class="overlay" id="modal-candle" onclick="if (event.target === this) closeCandles()">
            <div class="modal candle-modal" role="dialog" aria-labelledby="cd-sym">
                <div class="cd-head">
                    <div class="cd-title">
                        <span class="cd-sym" id="cd-sym"></span>
                        <span class="cd-name" id="cd-name"></span>
                    </div>
                    <div class="cd-quote">
                        <span class="cd-price mono" id="cd-price"></span>
                        <span class="cd-chg mono" id="cd-chg"></span>
                    </div>
                    <button class="close-x" onclick="closeCandles()" title="Close">
                        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
                    </button>
                </div>
                <div class="cd-tabs" id="cd-tabs">
                    ${CANDLE_RANGES.map(r => `<button class="range-tab" data-range="${r.key}" onclick="setCandleRange('${r.key}')">${r.label}</button>`).join('')}
                </div>
                <div class="cd-chart">
                    <div class="cd-legend mono" id="cd-legend"></div>
                    <div class="cd-host" id="cd-host"></div>
                    <div class="cd-msg" id="cd-msg"></div>
                </div>
                <div class="cd-foot">
                    <span id="cd-asof"></span>
                    <a id="cd-yahoo" target="_blank" rel="noopener">Open on Yahoo Finance ↗</a>
                </div>
            </div>
        </div>`);
    return document.getElementById('modal-candle');
}

function openCandles(sym) {
    _candle.sym = sym;
    const el = candleModal();
    document.getElementById('cd-sym').textContent = sym;
    document.getElementById('cd-name').textContent = '';
    document.getElementById('cd-price').textContent = '';
    document.getElementById('cd-chg').textContent = '';
    document.getElementById('cd-yahoo').href = `https://finance.yahoo.com/quote/${encodeURIComponent(sym)}`;
    el.classList.add('open');
    setCandleRange(_candle.rangeKey);
}

function closeCandles() {
    document.getElementById('modal-candle')?.classList.remove('open');
    if (_candle.chart) { _candle.chart.remove(); _candle.chart = null; }
    _candle.req++;
}

async function setCandleRange(key) {
    const r = CANDLE_RANGES.find(x => x.key === key) ?? CANDLE_RANGES[3];
    _candle.rangeKey = r.key;
    document.querySelectorAll('#cd-tabs .range-tab').forEach(b => b.classList.toggle('active', b.dataset.range === r.key));
    const msg = document.getElementById('cd-msg');
    msg.textContent = 'Loading…';
    msg.style.display = 'flex';
    const req = ++_candle.req;
    try {
        const { bars, meta } = await fetchOHLC(_candle.sym, r);
        if (req !== _candle.req) return;   // a newer range or another stock was opened
        msg.style.display = 'none';
        drawCandles(bars, meta, r);
    } catch (e) {
        if (req !== _candle.req) return;
        if (_candle.chart) { _candle.chart.remove(); _candle.chart = null; }
        document.getElementById('cd-legend').textContent = '';
        msg.textContent = `Could not load ${_candle.sym}: ${e.message}`;
    }
}

function drawCandles(bars, meta, r) {
    const host = document.getElementById('cd-host');
    if (_candle.chart) { _candle.chart.remove(); _candle.chart = null; }
    const cur = meta.currency || 'USD';
    const px = v => new Intl.NumberFormat('en-US', { minimumFractionDigits: 2, maximumFractionDigits: v < 1 ? 4 : 2 }).format(v);
    const vol = v => v >= 1e9 ? (v / 1e9).toFixed(2) + 'B' : v >= 1e6 ? (v / 1e6).toFixed(2) + 'M' : v >= 1e3 ? (v / 1e3).toFixed(1) + 'K' : String(v);
    const fmtTime = t => new Date(t * 1000).toLocaleString('en-US', r.intraday
        ? { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit', hourCycle: 'h23' }
        : { month: 'short', day: 'numeric', year: 'numeric' });

    // Header: name, last price and change vs the previous close.
    const last = bars[bars.length - 1];
    const price = meta.regularMarketPrice ?? last.close;
    const prev = meta.chartPreviousClose ?? meta.previousClose ?? bars[0].open;
    const chg = prev ? (price / prev - 1) * 100 : 0;
    document.getElementById('cd-name').textContent = meta.longName || meta.shortName || '';
    document.getElementById('cd-price').textContent = `${px(price)} ${cur}`;
    const chgEl = document.getElementById('cd-chg');
    chgEl.textContent = `${chg >= 0 ? '+' : ''}${chg.toFixed(2)}% ${r.key === '1d' ? 'today' : 'over ' + r.label}`;
    chgEl.className = 'cd-chg mono ' + (chg >= 0 ? 'up' : 'down');
    document.getElementById('cd-asof').textContent = `${r.intraday ? r.interval + ' candles' : 'Daily candles'} · ${fmtTime(last.time)} · Yahoo Finance`;

    const chart = LightweightCharts.createChart(host, {
        autoSize: true,
        layout: { background: { type: 'solid', color: 'transparent' }, textColor: 'rgba(239,239,239,0.45)',
                  fontFamily: "'Space Grotesk', sans-serif", fontSize: 11, attributionLogo: false },
        grid: { vertLines: { color: 'rgba(255,255,255,0.03)' }, horzLines: { color: 'rgba(255,255,255,0.04)' } },
        rightPriceScale: { borderVisible: false, scaleMargins: { top: 0.1, bottom: 0.24 } },
        timeScale: { borderVisible: false, timeVisible: r.intraday, secondsVisible: false, rightOffset: 4,
                     tickMarkFormatter: t => new Date(t * 1000).toLocaleString('en-US',
                         r.intraday && r.key === '1d' ? { hour: '2-digit', minute: '2-digit', hourCycle: 'h23' } : { month: 'short', day: 'numeric' }) },
        crosshair: { mode: 0,
                     vertLine: { color: 'rgba(255,255,255,0.22)', style: 3, labelBackgroundColor: '#1d1d1d' },
                     horzLine: { color: 'rgba(255,255,255,0.22)', style: 3, labelBackgroundColor: '#1d1d1d' } },
        localization: { timeFormatter: fmtTime, priceFormatter: px },
    });
    const candles = chart.addCandlestickSeries({
        upColor: CANDLE_UP, downColor: CANDLE_DOWN, borderVisible: false,
        wickUpColor: CANDLE_UP, wickDownColor: CANDLE_DOWN,
        priceLineColor: 'rgba(255,255,255,0.25)', priceLineStyle: 3,
    });
    candles.setData(bars.map(({ volume, ...b }) => b));
    const volume = chart.addHistogramSeries({ priceFormat: { type: 'volume' }, priceScaleId: 'vol', lastValueVisible: false, priceLineVisible: false });
    chart.priceScale('vol').applyOptions({ scaleMargins: { top: 0.82, bottom: 0 } });
    volume.setData(bars.map(b => ({ time: b.time, value: b.volume,
        color: b.close >= b.open ? 'rgba(82,196,138,0.35)' : 'rgba(201,112,112,0.35)' })));
    chart.timeScale().fitContent();

    // TradingView-style legend: O H L C, change and volume of the bar under the crosshair
    // (the last bar when the pointer is outside the chart).
    const legend = document.getElementById('cd-legend');
    const byTime = new Map(bars.map(b => [b.time, b]));
    const show = b => {
        const c = (b.close / b.open - 1) * 100, cls = c >= 0 ? 'up' : 'down';
        legend.innerHTML = `<span class="lg-t">${fmtTime(b.time)}</span>`
            + `<span>O <b class="${cls}">${px(b.open)}</b></span><span>H <b class="${cls}">${px(b.high)}</b></span>`
            + `<span>L <b class="${cls}">${px(b.low)}</b></span><span>C <b class="${cls}">${px(b.close)}</b></span>`
            + `<span class="${cls}">${c >= 0 ? '+' : ''}${c.toFixed(2)}%</span><span>Vol <b>${vol(b.volume)}</b></span>`;
    };
    show(last);
    chart.subscribeCrosshairMove(p => show((p.time != null && byTime.get(p.time)) || last));
    _candle.chart = chart;
}

document.addEventListener('keydown', e => {
    if (e.key === 'Escape' && document.getElementById('modal-candle')?.classList.contains('open')) closeCandles();
});
