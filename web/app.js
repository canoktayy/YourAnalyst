'use strict';

// ── State ──────────────────────────────────────────────────────────────────────
let S = {
    accounts:       [],   // { id, bank, cur, balance, color }
    debts:          [],   // { id, creditor, note, cur, amount }
    portfolios:     [],   // { id, name, isMain, stocks, portSnaps }
    activePortId:   null,
    watchlist:      [],   // { id, sym, name, price, changePct, priceCur, fetching, error }
    rates:          { USD: 38.5, EUR: 41.0, GBP: 48.0 },
    priceHistory:   {},   // { sym: [{ t, p }, ...] }
    calendarEvents: [],   // { id, date: 'YYYY-MM-DD', text }
    goals:          [],   // { id, title, cur, targetAmount, currentAmount, deadline }
};

// Base (display) currency. S.rates[c] = units of BASE per 1 unit of c.
// Both come from the server (app_user.base_currency, fx_rate) — see "Server sync".
let BASE = 'EUR';

let donutChart       = null;
let portChartInst    = null;
let portAltChartInst = null;
let portChartRange   = '6mo';   // Portfolio History opens on 6 months
let refreshInterval  = null;
let ratesInterval    = null;
let editingAccountId = null;
let editingDebtId    = null;
let editingPortId    = null;
const AUTO_REFRESH_MS  = 10_000;   // every 10 s: live prices + dashboard data from n8n/Postgres
const RATES_REFRESH_MS = 60_000;   // refresh FX rates every 60s

// ── Portfolio accessors ────────────────────────────────────────────────────────
function activePort() {
    return S.portfolios.find(p => p.id === S.activePortId) ?? S.portfolios[0];
}

function mainPort() {
    return S.portfolios.find(p => p.isMain) ?? S.portfolios[0];
}

// ── Persistence ────────────────────────────────────────────────────────────────
function load() {
    try {
        const raw = localStorage.getItem('fd3');
        if (!raw) return;
        const p = JSON.parse(raw);

        if (p.portfolios) {
            // New multi-portfolio structure
            S = {
                accounts:       p.accounts       ?? [],
                debts:          p.debts          ?? [],
                portfolios:     p.portfolios     ?? [],
                activePortId:   p.activePortId   ?? null,
                watchlist:      p.watchlist      ?? [],
                rates:          p.rates          ?? { USD: 38.5, EUR: 41.0, GBP: 48.0 },
                priceHistory:   p.priceHistory   ?? {},
                calendarEvents: p.calendarEvents ?? [],
                goals:          p.goals          ?? [],
            };
        } else {
            // Migrate from old flat structure
            const defPort = {
                id: uid(), name: 'Main Portfolio', isMain: true,
                stocks:    p.stocks    ?? [],
                portSnaps: p.portSnaps ?? [],
            };
            S = {
                accounts:       p.accounts       ?? [],
                debts:          p.debts          ?? [],
                portfolios:     [defPort],
                activePortId:   defPort.id,
                watchlist:      p.watchlist      ?? [],
                rates:          p.rates          ?? { USD: 38.5, EUR: 41.0, GBP: 48.0 },
                priceHistory:   p.priceHistory   ?? {},
                calendarEvents: p.calendarEvents ?? [],
                goals:          p.goals          ?? [],
            };
        }

        // Sanitize
        S.portfolios.forEach(p => { p.stocks = p.stocks ?? []; p.portSnaps = p.portSnaps ?? []; });
        if (!S.portfolios.find(p => p.id === S.activePortId)) S.activePortId = S.portfolios[0]?.id ?? null;
        const mains = S.portfolios.filter(p => p.isMain);
        if (mains.length === 0 && S.portfolios.length > 0) S.portfolios[0].isMain = true;
        if (mains.length > 1) S.portfolios.forEach((p, i) => { if (i > 0) p.isMain = false; });
    } catch {}
}

function save() {
    const stripStk = ({ fetching, ...r }) => r;
    const clean = {
        ...S,
        portfolios: S.portfolios.map(port => ({ ...port, stocks: port.stocks.map(stripStk) })),
        watchlist:  S.watchlist.map(stripStk),
    };
    const str = JSON.stringify(clean);
    localStorage.setItem('fd3', str);
    fetch('/api/data', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: str
    }).catch(() => {});
}

// ── Utils ──────────────────────────────────────────────────────────────────────
function uid() {
    return Date.now().toString(36) + Math.random().toString(36).slice(2, 7);
}

function toBase(amount, cur) {
    return cur === BASE ? amount : amount * (S.rates[cur] ?? 1);
}

function symOf(cur) {
    return { TRY: '₺', USD: '$', EUR: '€', GBP: '£' }[cur] ?? cur;
}

function fmt(amount, cur = 'TRY', decimals = 2) {
    const abs = Math.abs(amount);
    const str = new Intl.NumberFormat('en-US', {
        minimumFractionDigits: decimals,
        maximumFractionDigits: decimals
    }).format(abs);
    return (amount < 0 ? '−' : '') + symOf(cur) + str;
}

function fmtBase(n) { return fmt(n, BASE); }

function fmtCompact(n) {
    return new Intl.NumberFormat('en-US', {
        minimumFractionDigits: 0,
        maximumFractionDigits: 0
    }).format(Math.round(n));
}

function guessCur(sym) {
    sym = sym.toUpperCase();
    if (sym.endsWith('.IS'))                                        return 'TRY';
    if (sym.endsWith('.L') || sym.endsWith('.LON'))                 return 'GBP';
    if (/\.(PA|DE|AS|MI|MC|BR|AX|FRA|ETR|EPA)$/.test(sym))        return 'EUR';
    return 'USD';
}

function esc(s) {
    return String(s ?? '')
        .replace(/&/g, '&amp;').replace(/</g, '&lt;')
        .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

// ── Portfolio value helpers ────────────────────────────────────────────────────
function calcPortBase(port) {
    return (port?.stocks ?? []).reduce((sum, stk) => {
        if (!stk.qty) return sum;
        const price = stk.price ?? stk.cost ?? 0;
        return sum + toBase(price * stk.qty, stk.priceCur || stk.cur || 'TRY');
    }, 0);
}

function calcPortfolioBase() {
    return calcPortBase(mainPort());
}

// ── History accumulation ───────────────────────────────────────────────────────
function appendDataPoints() {
    const now = Date.now();

    // Price history (all stocks across all portfolios + watchlist)
    const allStocks = S.portfolios.flatMap(p => p.stocks);
    [...allStocks, ...S.watchlist].forEach(item => {
        if (!item.price || item.fetching || item.error) return;
        if (!S.priceHistory[item.sym]) S.priceHistory[item.sym] = [];
        const hist = S.priceHistory[item.sym];
        hist.push({ t: now, p: item.price });
        if (hist.length > 300) hist.splice(0, hist.length - 300);
    });

    // Portfolio snapshots — per portfolio
    S.portfolios.forEach(port => {
        const v = calcPortBase(port);
        if (v > 0) {
            if (!port.portSnaps) port.portSnaps = [];
            port.portSnaps.push({ t: now, v });
            trimPortSnaps(port);
        }
    });
}

function trimPortSnaps(port) {
    const snaps = port.portSnaps;
    if (!snaps || snaps.length <= 500) return;

    const now    = Date.now();
    const day    = 86_400_000;
    const cut24h = now - day;
    const cut7d  = now - 7 * day;

    const recent = snaps.filter(s => s.t >= cut24h);
    const mid    = snaps.filter(s => s.t >= cut7d && s.t < cut24h);
    const old    = snaps.filter(s => s.t < cut7d);

    const thin = (arr, bucketMs) => {
        const map = new Map();
        arr.forEach(s => {
            const k = Math.floor(s.t / bucketMs);
            if (!map.has(k)) map.set(k, s);
        });
        return [...map.values()].sort((a, b) => a.t - b.t);
    };

    port.portSnaps = [...thin(old, 60 * 60_000), ...thin(mid, 5 * 60_000), ...recent].slice(-6000);
}

function getPortSnapsForRange(range, port) {
    const snaps = port?.portSnaps ?? [];
    const now   = Date.now();
    const day   = 86_400_000;
    const cuts  = { '1d': now - day, '1mo': now - 30 * day, '3mo': now - 90 * day, '6mo': now - 180 * day };
    const since = cuts[range] ?? cuts['1d'];
    const data  = snaps.filter(s => s.t >= since);
    if (data.length < 2) return null;

    const step    = Math.max(1, Math.ceil(data.length / 120));
    const sampled = data.filter((_, i) => i % step === 0 || i === data.length - 1);

    const fmt24  = t => new Date(t).toLocaleTimeString('en-US', { hour: '2-digit', minute: '2-digit', hourCycle: 'h23' });
    const fmtDay = t => new Date(t).toLocaleDateString('en-US', { month: 'short', day: 'numeric' });
    const labelFn = range === '1d' ? fmt24 : fmtDay;

    // Lightweight Charts needs strictly increasing unix-second times.
    const seen = new Set();
    const pts = sampled.map(x => ({ t: Math.floor(x.t / 1000), v: x.v }))
        .filter(x => !seen.has(x.t) && seen.add(x.t));
    return {
        labels: pts.map(x => labelFn(x.t * 1000)),
        values: pts.map(x => x.v),
        times: pts.map(x => x.t),
        intraday: range === '1d'
    };
}

// ── Sparkline SVG ─────────────────────────────────────────────────────────────
// Intraday 5-minute closes per symbol from /api/history (last session), refreshed every
// 5 min. The locally collected price samples were too sparse and drew step-shaped lines.
const _spark = {};          // sym -> { ts, prices }
let _sparkLoading = false;

async function loadSparklines() {
    if (_sparkLoading) return;
    _sparkLoading = true;
    try {
        const syms = [...new Set([...S.watchlist, ...S.portfolios.flatMap(p => p.stocks)].map(s => s.sym))];
        const stale = syms.filter(sym => !_spark[sym] || Date.now() - _spark[sym].ts > 5 * 60_000);
        await Promise.all(stale.map(async sym => {
            const h = await fetchYahooHistory(sym, '1d');
            const prices = (h?.closes ?? []).filter(v => v != null && v > 0);
            if (prices.length >= 2) _spark[sym] = { ts: Date.now(), prices };
        }));
        if (stale.length) { renderWatchlist(); renderStocks(); }
    } finally {
        _sparkLoading = false;
    }
}

function sparklineSVG(sym, changePct) {
    const prices = _spark[sym]?.prices ?? ((S.priceHistory ?? {})[sym] ?? []).map(h => h.p);
    const W = 56, H = 22, pad = 2;

    if (prices.length < 2) {
        const y = H / 2;
        return `<svg width="${W}" height="${H}" viewBox="0 0 ${W} ${H}">
            <line x1="${pad}" y1="${y}" x2="${W-pad}" y2="${y}" stroke="rgba(255,255,255,0.1)" stroke-width="1" stroke-dasharray="2,3"/>
        </svg>`;
    }

    const mn = Math.min(...prices), mx = Math.max(...prices);
    const rng = mx - mn || 0.001;
    const pts = prices.map((p, i) => {
        const x = pad + (i / (prices.length - 1)) * (W - pad * 2);
        const y = pad + (1 - (p - mn) / rng) * (H - pad * 2);
        return `${x.toFixed(1)},${y.toFixed(1)}`;
    }).join(' ');

    const isUp = changePct == null ? prices[prices.length - 1] >= prices[0] : changePct >= 0;
    const color = isUp === null
        ? 'rgba(200,200,200,0.35)'
        : isUp ? 'rgba(82,196,138,0.9)' : 'rgba(201,112,112,0.9)';

    const gid = 'sg-' + String(sym).replace(/[^A-Za-z0-9]/g, '') + (isUp ? 'u' : 'd');
    const lastPt = pts.split(' ').pop().split(',');
    const fill = color.replace(/[\d.]+\)$/, '0.22)');
    return `<svg width="${W}" height="${H}" viewBox="0 0 ${W} ${H}">
        <defs><linearGradient id="${gid}" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0" stop-color="${fill}"/><stop offset="1" stop-color="${fill.replace(/[\d.]+\)$/, '0)')}"/>
        </linearGradient></defs>
        <polygon points="${pad},${H - pad} ${pts} ${W - pad},${H - pad}" fill="url(#${gid})"/>
        <polyline points="${pts}" fill="none" stroke="${color}" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/>
        <circle cx="${lastPt[0]}" cy="${lastPt[1]}" r="1.8" fill="${color}"/>
    </svg>`;
}

// ── Header ────────────────────────────────────────────────────────────────────
function updateHeader() {
    // Prefer the server total (SQL view v_net_worth); fall back to the local sum offline.
    const accTRY  = S.accounts.reduce((s, a) => s + toBase(a.balance, a.cur), 0);
    const debtTRY = S.debts.reduce((s, d) => s + toBase(d.amount, d.cur), 0);
    const local   = accTRY - debtTRY + calcPortfolioBase();
    const netBase = S.serverTotals ? Number(S.serverTotals.net_worth) : local;

    animateValue('nw-main', netBase, v => fmtBase(v));
    ['USD', 'EUR', 'GBP', 'TRY'].filter(c => c !== BASE).slice(0, 3).forEach((cur, i) => {
        setText(`fx${i}-sym`, cur);
        animateValue(`fx${i}`, netBase / (S.rates[cur] ?? NaN), v => Number.isFinite(v) ? fmtCompact(v) : '—');
    });
}

// Smooth count-up animation for numeric displays
const _animPrev = {};
function animateValue(id, target, formatter, duration = 800) {
    const el = document.getElementById(id);
    if (!el) return;
    const from = _animPrev[id] ?? target;
    _animPrev[id] = target;
    if (Math.abs(from - target) < 0.01) { el.textContent = formatter(target); return; }
    const start = performance.now();
    const diff = target - from;
    // Flash green/red on change
    if (diff > 0) el.style.color = '#3FB950';
    else if (diff < 0) el.style.color = '#F85149';
    function step(now) {
        const t = Math.min((now - start) / duration, 1);
        const ease = t < 0.5 ? 2 * t * t : -1 + (4 - 2 * t) * t; // ease-in-out
        el.textContent = formatter(from + diff * ease);
        if (t < 1) requestAnimationFrame(step);
        else { el.style.color = ''; }
    }
    requestAnimationFrame(step);
}

function setText(id, val) {
    const el = document.getElementById(id);
    if (el) el.textContent = val;
}

// ── Donut Chart ───────────────────────────────────────────────────────────────
// Muted, premium palette that fits the dark dashboard
const PALETTE     = ['#7da8c8','#7ab898','#c4a460','#a07898','#88b0b8','#b87878','#8898c0','#90a880'];
const STOCK_COLOR = '#8898b8';

// Allocation donut: one slice per stock (summed per symbol) and per account, sized by value
// in the base currency. Two quiet colour families that match the rest of the dashboard:
// stocks in steel-blue tints, cash in warm greys, each family ordered by size (light =
// largest). Slices are grouped by family, so the ring also reads as stocks vs cash; the
// legend, 2px gaps and hover carry the exact identity. More than 8 items fold into "Other".
const ALLOC_SHADES = {
    stock: ['#c9d9ea', '#a3bfdb', '#7fa4cb', '#6089b5', '#4a719c', '#3a5c82'],
    cash:  ['#d3cfc7', '#aaa59b', '#827e76', '#64615b'],
};

function allocItems() {
    const stocks = [], cash = [];
    S.accounts.forEach(a => {
        const v = toBase(a.balance, a.cur);
        if (v > 0) cash.push({ name: a.bank, sub: a.cur === BASE ? 'Cash' : `Cash · ${a.cur}`, kind: 'cash', v });
    });
    const bySym = {};
    (mainPort()?.stocks ?? []).forEach(st => {
        if (!(st.qty > 0) || st.price == null) return;
        const v = toBase(st.qty * st.price, st.priceCur || st.cur || 'USD');
        (bySym[st.sym] ??= { name: st.sym, sub: st.name || 'Stock', kind: 'stock', v: 0 }).v += v;
    });
    stocks.push(...Object.values(bySym));
    const fold = (list, max, kind, label) => {
        list.sort((a, b) => b.v - a.v);
        if (list.length <= max) return list;
        const rest = list.splice(max - 1);
        return [...list, { name: label, sub: `${rest.length} more`, kind, v: rest.reduce((s, x) => s + x.v, 0) }];
    };
    return [
        ...fold(stocks, ALLOC_SHADES.stock.length, 'stock', 'Other stocks').map((x, i) => ({ ...x, color: ALLOC_SHADES.stock[i] })),
        ...fold(cash, ALLOC_SHADES.cash.length, 'cash', 'Other cash').map((x, i) => ({ ...x, color: ALLOC_SHADES.cash[i] })),
    ];
}

function updateDonut() {
    const host = document.getElementById('alloc-donut');
    const list = document.getElementById('legend');
    if (!host || !list) return;
    const items = allocItems();
    const total = items.reduce((s, x) => s + x.v, 0);
    setText('di-val', total > 0 ? fmtBase(total) : '—');
    if (!total) {
        host.innerHTML = '';
        list.innerHTML = '<div class="alloc-empty">No assets yet</div>';
        document.getElementById('alloc-debt').innerHTML = '';
        return;
    }

    const R = 54, W = 9, C = 2 * Math.PI * R, GAP = 2.5;
    let acc = 0;
    const arcs = items.map((x, i) => {
        const len = x.v / total * C;
        const start = acc; acc += len;
        const draw = Math.max(0.6, len - GAP - (len > W + GAP ? W : 0));   // room for the round caps
        const cap = len > W + GAP ? 'round' : 'butt';
        const off = -(start + GAP / 2 + (cap === 'round' ? W / 2 : 0));
        return `<circle class="dn-seg" data-i="${i}" r="${R}" cx="70" cy="70" fill="none" stroke="${x.color}"
                    stroke-width="${W}" stroke-linecap="${cap}" stroke-dasharray="${draw} ${C}" stroke-dashoffset="${off}"
                    transform="rotate(-90 70 70)"><title>${esc(x.name)} · ${fmtBase(x.v)} · ${(x.v / total * 100).toFixed(1)}%</title></circle>`;
    }).join('');
    host.innerHTML = `
        <svg viewBox="0 0 140 140" role="img" aria-label="Allocation by account and stock">
            <circle r="${R}" cx="70" cy="70" fill="none" stroke="rgba(255,255,255,0.04)" stroke-width="${W}"/>
            ${arcs}
        </svg>
        <div class="dn-center" id="dn-center">
            <span class="dn-val mono leg-amt">${allocCompact(total)}</span><span class="dn-lbl">Assets</span>
        </div>`;

    list.innerHTML = items.map((x, i) => `
        <div class="dn-row" data-i="${i}" title="${esc(x.name)} · ${fmtBase(x.v)}">
            <i style="background:${x.color}"></i>
            <span class="dn-name">${esc(x.name)}</span>
            <span class="dn-pct mono">${(x.v / total * 100).toFixed(1)}%</span>
        </div>`).join('');

    const center = document.getElementById('dn-center');
    const focus = i => {
        host.classList.toggle('focus', i != null);
        host.querySelectorAll('.dn-seg').forEach(c => c.classList.toggle('on', Number(c.dataset.i) === i));
        list.querySelectorAll('.dn-row').forEach(r => r.classList.toggle('on', Number(r.dataset.i) === i));
        const x = items[i];
        center.innerHTML = x
            ? `<span class="dn-val mono leg-amt">${allocCompact(x.v)}</span><span class="dn-lbl">${esc(x.name)}</span><span class="dn-sub">${(x.v / total * 100).toFixed(1)}%</span>`
            : `<span class="dn-val mono leg-amt">${allocCompact(total)}</span><span class="dn-lbl">Assets</span>`;
    };
    [...host.querySelectorAll('.dn-seg'), ...list.querySelectorAll('.dn-row')].forEach(el => {
        el.addEventListener('mouseenter', () => focus(Number(el.dataset.i)));
        el.addEventListener('mouseleave', () => focus(null));
    });
    document.getElementById('alloc-debt').innerHTML = debtRatioHTML(total);
}

// Debt-to-assets: one quiet line under the donut (same total as the Debts panel, from
// the SQL view). Not a slice: the donut shows how the assets are spread.
function debtRatioHTML(totalAssets) {
    const debt = S.serverTotals ? Number(S.serverTotals.total_liabilities)
                                : S.debts.reduce((s, d) => s + toBase(d.amount, d.cur), 0);
    if (!(debt > 0) || !(totalAssets > 0)) return '';
    const pct = debt / totalAssets * 100;
    return `<div class="alloc-debt" title="Total debt divided by total assets">
        <div class="alloc-debt-top"><span>Debt <b class="leg-amt mono">${fmtBase(debt)}</b></span>
            <span class="mono">${pct.toFixed(1)}% of assets</span></div>
        <div class="alloc-debt-track"><span style="width:${Math.min(100, pct)}%"></span></div>
    </div>`;
}

function allocCompact(v) {
    try { return new Intl.NumberFormat('en-US', { style: 'currency', currency: BASE, notation: 'compact', maximumFractionDigits: 1 }).format(v); }
    catch { return fmtBase(v); }
}

// ── Shared line chart builder ─────────────────────────────────────────────────
// Portfolio value over time: TradingView Lightweight Charts area series with a crosshair
// and a floating value tooltip. Single series, so no legend; the panel title names it.
// Returns an object with destroy() so callers can treat it like the old Chart.js chart.
function buildLineChart(canvasId, hist) {
    const canvas = document.getElementById(canvasId);
    if (!canvas || !window.LightweightCharts || !hist?.times?.length) return null;
    const wrap = canvas.parentElement;
    canvas.style.display = 'none';
    wrap.querySelector('.lw-host')?.remove();
    const host = document.createElement('div');
    host.className = 'lw-host';
    wrap.appendChild(host);

    const first = hist.values[0], last = hist.values[hist.values.length - 1];
    const up = last >= first;
    const rgb = up ? '82,196,138' : '201,112,112';
    const intraday = hist.intraday;
    const fmtTime = t => new Date(t * 1000).toLocaleString('en-US', intraday
        ? { hour: '2-digit', minute: '2-digit', hourCycle: 'h23' }
        : { month: 'short', day: 'numeric', year: 'numeric' });

    const chart = LightweightCharts.createChart(host, {
        autoSize: true,
        layout: { background: { type: 'solid', color: 'transparent' }, textColor: 'rgba(239,239,239,0.35)',
                  fontFamily: "'Space Grotesk', sans-serif", fontSize: 10, attributionLogo: false },
        grid: { vertLines: { visible: false }, horzLines: { color: 'rgba(255,255,255,0.04)' } },
        rightPriceScale: { visible: false },
        leftPriceScale: { visible: false },
        timeScale: { borderVisible: false, timeVisible: intraday, secondsVisible: false,
                     fixLeftEdge: true, fixRightEdge: true, lockVisibleTimeRangeOnResize: true,
                     tickMarkFormatter: t => new Date(t * 1000).toLocaleString('en-US',
                         intraday ? { hour: '2-digit', minute: '2-digit', hourCycle: 'h23' } : { month: 'short', day: 'numeric' }) },
        crosshair: { mode: 1,   // magnet: the marker snaps to the line
                     vertLine: { color: 'rgba(255,255,255,0.18)', width: 1, style: 3, labelVisible: false },
                     horzLine: { visible: false, labelVisible: false } },
        localization: { timeFormatter: fmtTime, priceFormatter: v => fmtBase(v) },
        handleScroll: false, handleScale: false,
    });
    const series = chart.addAreaSeries({
        lineColor: `rgb(${rgb})`, lineWidth: 2,
        topColor: `rgba(${rgb},0.28)`, bottomColor: `rgba(${rgb},0)`,
        priceLineVisible: false, lastValueVisible: false,
        crosshairMarkerRadius: 4, crosshairMarkerBorderColor: '#0b0b0b', crosshairMarkerBackgroundColor: `rgb(${rgb})`,
    });
    series.setData(hist.times.map((t, i) => ({ time: t, value: hist.values[i] })));
    chart.timeScale().fitContent();

    const tip = document.createElement('div');
    tip.className = 'lw-tip';
    host.appendChild(tip);
    chart.subscribeCrosshairMove(param => {
        const pt = param.point, d = param.seriesData?.get(series);
        if (!pt || !d || param.time == null) { tip.style.display = 'none'; return; }
        const chg = (d.value / first - 1) * 100;
        tip.innerHTML = `<span class="lw-tip-t">${fmtTime(param.time)}</span>`
            + `<span class="lw-tip-v">${fmtBase(d.value)}</span>`
            + `<span class="lw-tip-c ${chg >= 0 ? 'up' : 'down'}">${chg >= 0 ? '+' : ''}${chg.toFixed(2)}%</span>`;
        tip.style.display = 'flex';
        const w = tip.offsetWidth, W = host.clientWidth;
        tip.style.left = Math.min(Math.max(pt.x - w / 2, 0), W - w) + 'px';
    });

    return { destroy() { chart.remove(); host.remove(); canvas.style.display = ''; } };
}

// ── Yahoo historical OHLCV ────────────────────────────────────────────────────
async function fetchYahooHistory(symbol, range) {
    const interval = range === '1d' ? '5m' : range === '5d' ? '15m' : '1d';

    // 1) Local server proxy first
    try {
        const r = await fetch(`/api/history?symbol=${encodeURIComponent(symbol)}&range=${range}&interval=${interval}`,
            { signal: AbortSignal.timeout(15000) });
        if (r.ok) {
            const j = await r.json();
            const result = j?.data?.chart?.result?.[0];
            if (j.ok && result) {
                const timestamps = result.timestamp;
                const closes     = result.indicators?.quote?.[0]?.close;
                const currency   = result.meta?.currency || 'USD';
                if (timestamps && closes) return { timestamps, closes, currency };
            }
        }
    } catch {}

    const url = `https://query1.finance.yahoo.com/v8/finance/chart/${encodeURIComponent(symbol)}?interval=${interval}&range=${range}`;
    const tryProxy = async (proxyUrl) => {
        const res = await fetch(proxyUrl, { signal: AbortSignal.timeout(10000) });
        if (!res.ok) throw new Error('bad');
        let raw = await res.json();
        if (typeof raw.contents === 'string') raw = JSON.parse(raw.contents);
        const result = raw?.chart?.result?.[0];
        if (!result) throw new Error('no result');
        const timestamps = result.timestamp;
        const closes     = result.indicators?.quote?.[0]?.close;
        const currency   = result.meta?.currency || 'USD';
        if (!timestamps || !closes) throw new Error('no data');
        return { timestamps, closes, currency };
    };
    try {
        return await Promise.any([
            tryProxy(`https://corsproxy.io/?${encodeURIComponent(url)}`),
            tryProxy(`https://api.allorigins.win/get?url=${encodeURIComponent(url)}`),
            tryProxy(`https://api.codetabs.com/v1/proxy?quest=${encodeURIComponent(url)}`),
        ]);
    } catch { return null; }
}

// cache: { range: { labels, values, ts } }
const _portHistCache = {};

async function buildPortHistFromYahoo(range, port) {
    const cacheKey = range + '_' + port.id;
    const cached   = _portHistCache[cacheKey];
    // 10 min cache for intraday, 60 min for others
    const ttl = range === '1d' ? 10 * 60_000 : 60 * 60_000;
    if (cached && Date.now() - cached.ts < ttl) return cached;

    // Value of TODAY's holdings at past prices: shows market moves only. (Snapshots of the
    // stored value jumped whenever a holding was added and looked like a price spike.)
    const qtyBySym = {};
    port.stocks.filter(s => s.qty > 0).forEach(s => { qtyBySym[s.sym] = (qtyBySym[s.sym] ?? 0) + s.qty; });
    const syms = Object.keys(qtyBySym);
    if (!syms.length) return null;

    const results = await Promise.all(syms.map(sym => fetchYahooHistory(sym, range)));
    const step = range === '1d' ? 300 : range === '5d' ? 900 : 86400;
    const series = syms.map((sym, i) => {
        const h = results[i];
        if (!h) return null;
        const rate = h.currency === BASE ? 1 : (S.rates[h.currency] ?? null);
        if (rate == null) return null;
        const pts = new Map();
        h.timestamps.forEach((ts, j) => {
            const price = h.closes[j];
            if (price != null && price > 0) pts.set(Math.floor(ts / step) * step, price * qtyBySym[sym] * rate);
        });
        return pts.size ? pts : null;
    });
    if (series.some(x => x == null)) return null;   // a missing symbol would distort the total

    // Union of timestamps, each symbol carried forward from its last known value; start
    // once every symbol has a value so the line does not jump when one series begins.
    const start = Math.max(...series.map(m => Math.min(...m.keys())));
    const times = [...new Set(series.flatMap(m => [...m.keys()]))].filter(t => t >= start).sort((a, b) => a - b);
    const last = series.map(() => null);
    const sorted = times.map(t => {
        series.forEach((m, i) => { if (m.has(t)) last[i] = m.get(t); });
        return [t, last.reduce((s, v) => s + (v ?? 0), 0)];
    });
    if (sorted.length < 2) return null;
    const fmt24  = t => new Date(t * 1000).toLocaleTimeString('en-US', { hour: '2-digit', minute: '2-digit', hourCycle: 'h23' });
    const fmtDay = t => new Date(t * 1000).toLocaleDateString('en-US', { month: 'short', day: 'numeric' });
    const labelFn = range === '1d' ? fmt24 : fmtDay;

    const out = {
        labels: sorted.map(([t]) => labelFn(t)),
        values: sorted.map(([, v]) => v),
        times: sorted.map(([t]) => t),
        intraday: range === '1d' || range === '5d',
        ts: Date.now()
    };
    _portHistCache[cacheKey] = out;
    return out;
}

// ── Portfolio History Chart (left panel — always main portfolio) ───────────────
async function renderPortfolioChart(range) {
    const canvas      = document.getElementById('port-chart');
    const placeholder = document.getElementById('port-placeholder');
    if (!canvas) return;

    if (portChartInst) { portChartInst.destroy(); portChartInst = null; }

    const mp = mainPort();
    if (!mp || mp.stocks.filter(s => s.qty > 0).length === 0) {
        if (placeholder) { placeholder.textContent = 'No stocks in portfolio'; placeholder.style.display = 'flex'; }
        return;
    }

    // Every range is built from Yahoo price history × current quantities.
    if (placeholder) { placeholder.textContent = 'Loading…'; placeholder.style.display = 'flex'; }
    let hist = await buildPortHistFromYahoo(range, mp);
    // Right after the US open "Day" has only a few points: show the previous session too.
    if (range === '1d' && (!hist || hist.values.length < 12)) {
        const h5 = await buildPortHistFromYahoo('5d', mp);
        if (h5) {
            const from = h5.times[h5.times.length - 1] - 26 * 3600;
            const keep = h5.times.map((t, i) => t >= from ? i : -1).filter(i => i >= 0);
            if (keep.length >= 2) hist = { ...h5, labels: keep.map(i => h5.labels[i]), values: keep.map(i => h5.values[i]), times: keep.map(i => h5.times[i]) };
        }
    }
    if (!hist) {
        if (placeholder) { placeholder.textContent = 'Could not load data'; placeholder.style.display = 'flex'; }
        return;
    }
    if (placeholder) placeholder.style.display = 'none';
    portChartInst = buildLineChart('port-chart', hist);
}

function setPortChartRange(range) {
    portChartRange = range;
    document.querySelectorAll('.range-tab').forEach(btn => {
        btn.classList.toggle('active', btn.dataset.range === range);
    });
    renderPortfolioChart(range);
}

// ── Alt portfolio chart (right panel — non-main active portfolio) ──────────────
function renderAltPortChart() {
    const section = document.getElementById('port-alt-section');
    if (!section) return;

    if (portAltChartInst) { portAltChartInst.destroy(); portAltChartInst = null; }

    const ap = activePort();
    const mp = mainPort();

    if (!ap || ap.id === mp?.id) {
        section.style.display = 'none';
        return;
    }

    section.style.display = '';
    setText('port-alt-label', ap.name + ' — Performance');

    const hist        = getPortSnapsForRange('1d', ap);
    const placeholder = document.getElementById('port-alt-placeholder');

    if (!hist) {
        if (placeholder) { placeholder.textContent = 'Collecting data…'; placeholder.style.display = 'flex'; }
        return;
    }
    if (placeholder) placeholder.style.display = 'none';
    portAltChartInst = buildLineChart('port-alt-chart', hist);
}

// ── Portfolio Tabs ─────────────────────────────────────────────────────────────
function renderPortfolioTabs() {
    const container = document.getElementById('port-tabs');
    if (!container) return;

    const ap = activePort();

    // Update the active portfolio label (shown once in panel-head)
    const nameEl = document.getElementById('active-port-name');
    if (nameEl) nameEl.textContent = ap?.name ?? 'Portfolio';

    // Show edit button for active portfolio
    const editBtn = document.getElementById('port-edit-active');
    if (editBtn) editBtn.style.display = '';

    // Show only OTHER portfolios as switch pills, plus the "+" add button
    const others = S.portfolios.filter(p => p.id !== ap?.id);
    container.innerHTML = others.map(p => {
        const star = p.isMain ? '<span class="port-tab-star">★</span>' : '';
        return `<div class="port-tab" onclick="setActivePort('${p.id}')">
            ${star}<span class="port-tab-name">${esc(p.name)}</span>
        </div>`;
    }).join('') + `<button class="port-tab-add" onclick="openPortfolioModal(null)" title="New Portfolio">+</button>`;
}

function setActivePort(id) {
    S.activePortId = id;
    save();
    renderPortfolioTabs();
    renderStocks();
    renderAltPortChart();
}

// ── Accounts ──────────────────────────────────────────────────────────────────
function renderAccounts() {
    const list   = document.getElementById('accounts-list');
    const footer = document.getElementById('accounts-footer');
    if (!list) return;

    if (S.accounts.length === 0) {
        list.innerHTML = '<div class="hint">Click + Add<br>to add a bank account</div>';
        footer.innerHTML = '';
        return;
    }

    list.innerHTML = S.accounts.map(a => {
        const tryVal   = toBase(a.balance, a.cur);
        const showConv = a.cur !== BASE;
        return `<div class="acc-row" onclick="openEditAccountModal('${a.id}')" style="cursor:pointer">
            <div class="acc-left">
                <div class="acc-dot" style="background:${a.color || '#818cf8'}"></div>
                <div>
                    <div class="acc-name">${esc(a.bank)}</div>
                    <div class="acc-bank">${esc(a.cur)}</div>
                </div>
            </div>
            <div style="display:flex;align-items:center;gap:4px">
                <div class="acc-right">
                    <div class="acc-amount mono">${fmt(a.balance, a.cur)}</div>
                    ${showConv ? `<div class="acc-try">≈ ${fmtBase(tryVal)}</div>` : ''}
                </div>
                <button class="del-btn" onclick="event.stopPropagation();deleteAccount('${a.id}')" title="Delete">✕</button>
            </div>
        </div>`;
    }).join('');

    const total = S.accounts.reduce((s, a) => s + toBase(a.balance, a.cur), 0);
    footer.innerHTML = `
        <div class="footer-row"><span class="footer-label">Total Balance</span></div>
        <div class="footer-val mono">${fmtBase(total)}</div>`;
}

async function deleteAccount(id) {
    if (await yaWrite('asset', 'delete', id)) toast('Account deleted', 'ok');
}

// ── Debts ─────────────────────────────────────────────────────────────────────
function renderDebts() {
    const list   = document.getElementById('debts-list');
    const footer = document.getElementById('debts-footer');
    if (!list) return;

    if (S.debts.length === 0) {
        list.innerHTML = '<div class="hint">Click + Add<br>to add a debt</div>';
        footer.innerHTML = '';
        return;
    }

    list.innerHTML = S.debts.map(d => {
        const tryVal   = toBase(d.amount, d.cur);
        const showConv = d.cur !== BASE;
        return `<div class="debt-row" onclick="openEditDebtModal('${d.id}')" style="cursor:pointer">
            <div class="debt-left">
                <div class="debt-icon"></div>
                <div>
                    <div class="debt-name">${esc(d.creditor)}</div>
                    ${d.note ? `<div class="debt-note">${esc(d.note)}</div>` : ''}
                </div>
            </div>
            <div style="display:flex;align-items:center;gap:4px">
                <div class="debt-right">
                    <div class="debt-amount mono">${fmt(d.amount, d.cur)}</div>
                    ${showConv ? `<div class="debt-try">≈ ${fmtBase(tryVal)}</div>` : ''}
                </div>
                <button class="del-btn" onclick="event.stopPropagation();deleteDebt('${d.id}')" title="Delete">✕</button>
            </div>
        </div>`;
    }).join('');

    const total = S.debts.reduce((s, d) => s + toBase(d.amount, d.cur), 0);
    footer.innerHTML = `
        <div class="footer-row"><span class="footer-label">Total Debt</span></div>
        <div class="footer-val mono debt-total">${fmtBase(total)}</div>`;
}

async function deleteDebt(id) {
    if (await yaWrite('liability', 'delete', id)) toast('Debt deleted', 'ok');
}

// ── Watchlist ─────────────────────────────────────────────────────────────────
function renderWatchlist() {
    const list = document.getElementById('watchlist-list');
    if (!list) return;

    if (S.watchlist.length === 0) {
        list.innerHTML = '<div class="hint">Add stocks to track prices</div>';
        return;
    }

    list.innerHTML = S.watchlist.map(w => {
        const priceCur = w.priceCur || 'USD';
        let priceHtml, changeHtml = '';

        if (w.fetching && w.price == null) {   // keep showing the last known price while refreshing
            priceHtml = `<span class="stk-price stk-loading">…</span>`;
        } else {
            const isUp  = w.changePct != null ? w.changePct >= 0 : null;
            const pcCls = isUp === null ? '' : isUp ? 'up' : 'down';
            priceHtml   = `<span class="stk-price mono ${pcCls}">${w.price != null ? fmt(w.price, priceCur) : '—'}</span>`;
            if (w.changePct != null) {
                const sign = w.changePct >= 0 ? '+' : '';
                const cls  = w.changePct > 0.001 ? 'up' : w.changePct < -0.001 ? 'down' : 'flat';
                changeHtml = `<div class="stk-change ${cls}">${sign}${w.changePct.toFixed(2)}%</div>`;
            }
        }

        return `<div class="watch-row" id="wt-${w.id}" draggable="true" data-id="${w.id}">
            <span class="drag-handle">⠿</span>
            <div class="stk-left" onclick="openCandles('${esc(w.sym)}')" style="cursor:pointer;flex:1">
                <div class="stk-sym">${esc(w.sym)}</div>
                ${w.name ? `<div class="stk-name">${esc(w.name)}</div>` : ''}
            </div>
            <div class="stk-mid">${priceHtml}</div>
            <div class="stk-spark">${sparklineSVG(w.sym, w.changePct)}</div>
            <div class="watch-change">${changeHtml}</div>
            <button class="del-btn" onclick="event.stopPropagation();deleteWatchItem('${w.id}')" title="Delete">✕</button>
        </div>`;
    }).join('');

    let _dragId = null;
    list.querySelectorAll('.watch-row').forEach(row => {
        row.addEventListener('dragstart', e => {
            _dragId = row.dataset.id;
            setTimeout(() => row.classList.add('dragging'), 0);
            e.dataTransfer.effectAllowed = 'move';
        });
        row.addEventListener('dragend', () => {
            row.classList.remove('dragging');
            list.querySelectorAll('.watch-row').forEach(r => r.classList.remove('drag-over'));
        });
        row.addEventListener('dragover', e => {
            e.preventDefault();
            if (row.dataset.id === _dragId) return;
            list.querySelectorAll('.watch-row').forEach(r => r.classList.remove('drag-over'));
            row.classList.add('drag-over');
        });
        row.addEventListener('drop', e => {
            e.preventDefault();
            if (!_dragId || row.dataset.id === _dragId) return;
            const fromIdx = S.watchlist.findIndex(w => w.id === _dragId);
            const toIdx   = S.watchlist.findIndex(w => w.id === row.dataset.id);
            if (fromIdx < 0 || toIdx < 0) return;
            const [item] = S.watchlist.splice(fromIdx, 1);
            S.watchlist.splice(toIdx, 0, item);
            renderWatchlist();
            // New position = midway between the new neighbours, so only this row is written.
            const before = S.watchlist[toIdx - 1]?.position, after = S.watchlist[toIdx + 1]?.position;
            const position = before == null ? (after ?? 0) - 1 : after == null ? before + 1 : (before + after) / 2;
            yaWrite('watchlist', 'update', item.id, { position });
        });
    });
}

async function deleteWatchItem(id) {
    if (await yaWrite('watchlist', 'delete', id)) toast('Removed from watchlist', 'ok');
}

// ── Stocks ────────────────────────────────────────────────────────────────────
function renderStocks() {
    const list   = document.getElementById('stocks-list');
    const footer = document.getElementById('stocks-footer');
    if (!list) return;
    if (typeof renderPortHeatmap === 'function') renderPortHeatmap();   // heatmap.js (no-op in list view)

    const port   = activePort();
    const stocks = port?.stocks ?? [];

    if (stocks.length === 0) {
        list.innerHTML = '<div class="hint">Add stocks to the portfolio</div>';
        footer.innerHTML = '';
        return;
    }

    list.innerHTML = stocks.map(stk => {
        const priceCur = stk.priceCur || stk.cur || 'TRY';
        const price    = stk.price ?? stk.cost ?? null;
        const hasPos   = stk.qty > 0;
        const value    = (hasPos && price !== null) ? price * stk.qty : null;
        const invested = (hasPos && stk.cost)       ? stk.cost * stk.qty : null;
        const pnl      = (value !== null && invested !== null) ? value - invested : null;
        const pnlPct   = (pnl !== null && invested > 0) ? (pnl / invested) * 100 : null;

        let priceHtml, changeHtml = '';
        if (stk.fetching) {
            priceHtml = `<div class="stk-price stk-loading">…</div>`;
        } else {
            const isUp  = stk.changePct != null ? stk.changePct >= 0 : null;
            const pcCls = isUp === null ? '' : isUp ? 'up' : 'down';
            priceHtml   = `<div class="stk-price mono ${pcCls}">${price !== null ? fmt(price, priceCur) : '—'}</div>`;
            if (stk.changePct != null) {
                const sign = stk.changePct >= 0 ? '+' : '';
                const cls  = stk.changePct > 0.001 ? 'up' : stk.changePct < -0.001 ? 'down' : 'flat';
                changeHtml = `<div class="stk-change ${cls}">${sign}${stk.changePct.toFixed(2)}%</div>`;
            }
            if (stk.priceStatus && stk.priceStatus !== 'ok') {
                changeHtml += `<div class="stk-change flat" title="${esc(stk.priceAsOf ?? '')}">${stk.priceStatus === 'stale' ? 'stale price' : 'no price'}</div>`;
            }
        }

        let posHtml = '';
        if (hasPos && value !== null) {
            const pnlCls  = pnl === null ? '' : pnl >= 0 ? 'up' : 'down';
            const pnlSign = pnl !== null && pnl >= 0 ? '+' : '';
            posHtml = `<div class="stk-right">
                <div class="stk-col-lbl">Total</div>
                <div class="stk-val mono">${fmt(value, priceCur)}</div>
                ${pnl !== null ? `<div class="stk-pnl ${pnlCls}">${pnlSign}${pnlPct?.toFixed(1)}%</div>` : ''}
            </div>`;
        }

        return `<div class="stk-row" id="stk-${stk.id}" onclick="openCandles('${esc(stk.sym)}')" style="cursor:pointer">
            <div class="stk-left">
                <div class="stk-sym">${esc(stk.sym)}</div>
                ${stk.name ? `<div class="stk-name">${esc(stk.name)}</div>` : ''}
            </div>
            <div class="stk-mid">
                <div class="stk-col-lbl">Live</div>
                ${priceHtml}${changeHtml}
            </div>
            <div class="stk-spark">${sparklineSVG(stk.sym, stk.changePct)}</div>
            ${posHtml}
            <button class="del-btn" onclick="event.stopPropagation();deleteStock('${stk.id}')" title="Delete">✕</button>
        </div>`;
    }).join('');

    let totalTRY = 0, investedTRY = 0;
    stocks.forEach(stk => {
        if (!stk.qty) return;
        const price = stk.price ?? stk.cost ?? 0;
        totalTRY    += toBase(price * stk.qty, stk.priceCur || stk.cur || 'TRY');
        if (stk.cost) investedTRY += toBase(stk.cost * stk.qty, stk.cur || 'TRY');
    });

    const pnlTRY  = totalTRY - investedTRY;
    const pnlPct  = investedTRY > 0 ? (pnlTRY / investedTRY) * 100 : 0;
    const pnlCls  = pnlTRY >= 0 ? 'up' : 'down';
    const pnlSign = pnlTRY >= 0 ? '+' : '';

    footer.innerHTML = `
        <div class="footer-row">
            <span class="footer-label">Portfolio Value</span>
            ${investedTRY > 0 ? `<span class="footer-pnl ${pnlCls}">${pnlSign}${fmtBase(pnlTRY)} (${pnlSign}${pnlPct.toFixed(1)}%)</span>` : ''}
        </div>
        <div class="footer-val mono">${fmtBase(totalTRY)}</div>`;
}

async function deleteStock(id) {
    const port = activePort();
    if (port === mainPort()) {
        if (await yaWrite('holding', 'delete', id)) toast('Stock deleted', 'ok');
        return;
    }
    if (port) port.stocks = port.stocks.filter(s => s.id !== id);
    save(); render();
    toast('Stock deleted', 'ok');
}

function openYahoo(sym) {
    window.open(`https://finance.yahoo.com/quote/${encodeURIComponent(sym)}`, '_blank', 'noopener');
}

// ── Server sync (YourAnalyst) ─────────────────────────────────────────────────
// Accounts, debts and the main portfolio live in Postgres behind n8n; server.py
// proxies /ya/<name> to the n8n webhook ya-<name> and adds the user token.
// The watchlist lives there too. Other UI state (extra portfolios, calendar) stays local.
async function yaRequest(path, body) {
    const r = await fetch(`/ya/${path}`, {
        method: body ? 'POST' : 'GET',
        headers: { 'Content-Type': 'application/json' },
        body: body ? JSON.stringify(body) : undefined,
        signal: AbortSignal.timeout(30000),
    });
    const data = await r.json().catch(() => ({ ok: false, error: `HTTP ${r.status}` }));
    if (!r.ok || !data.ok) throw new Error([data.error, ...(data.details ?? [])].filter(Boolean).join(' · '));
    return data;
}

function applyDashboard(d) {
    BASE = d.user?.base_currency ?? BASE;
    if (typeof setChatGreeting === 'function') setChatGreeting(d.user?.name);
    S.rates = Object.fromEntries(Object.entries(d.rates ?? {}).map(([c, v]) => [c, Number(v)]));
    S.serverTotals = d.totals;
    S.accounts = d.assets.map(a => ({
        id: a.id, bank: a.name, cur: a.currency, balance: Number(a.amount), color: a.color, kind: a.kind,
    }));
    S.debts = d.liabilities.map(l => ({
        id: l.id, creditor: l.counterparty, note: l.note ?? '', cur: l.currency, amount: Number(l.amount),
    }));
    const port = mainPort();
    if (port) port.stocks = d.positions.map(p => ({
        id: p.id, sym: p.symbol, name: p.name ?? '', cur: p.cost_currency,
        qty: Number(p.quantity), cost: Number(p.cost_per_share) || null,
        price: p.last_price == null ? null : Number(p.last_price),
        priceCur: p.price_currency ?? p.cost_currency,
        changePct: p.change_pct == null ? null : Number(p.change_pct),
        priceStatus: p.price_status, priceAsOf: p.price_as_of,
    }));
    // Watchlist rows come from the database; a live price fetched in this browser
    // (newer than the stored quote) is kept until the next fetch.
    const live = Object.fromEntries(S.watchlist.map(w => [w.id, w]));
    S.watchlist = (d.watchlist ?? []).map(w => {
        const prev = live[w.id];
        return {
            id: w.id, sym: w.symbol, name: w.name ?? '', position: Number(w.position),
            price: prev?.lastFetched ? prev.price : (w.price == null ? null : Number(w.price)),
            changePct: prev?.lastFetched ? prev.changePct : (w.change_pct == null ? null : Number(w.change_pct)),
            priceCur: prev?.lastFetched ? prev.priceCur : (w.price_currency ?? guessCur(w.symbol)),
            lastFetched: prev?.lastFetched, fetching: prev?.fetching ?? false, error: false,
        };
    });
}

// A sync can replace S.watchlist while a price fetch is in flight; results go to the
// row that is in the list now (same id), not to the replaced object.
function liveItem(item) {
    return S.watchlist.find(w => w.id === item.id) ?? item;
}

async function yaSync({ quiet = true } = {}) {
    try {
        applyDashboard(await yaRequest('dashboard'));
        save(); render();
        loadSparklines();   // new symbols only; the rest are cached for 5 min
        return true;
    } catch (e) {
        if (!quiet) toast(`Could not reach the server: ${e.message}`, 'err');
        return false;
    }
}

// Write one record through n8n; the reply is the fresh dashboard.
async function yaWrite(type, action, id, fields) {
    try {
        applyDashboard(await yaRequest('record', { type, action, id, fields }));
        save(); render();
        return true;
    } catch (e) {
        toast(`Could not save: ${e.message}`, 'err');
        return false;
    }
}

// Refresh button: ask n8n to re-fetch quotes/FX, then re-read the dashboard.
async function refreshNow() {
    const btn = document.querySelector('.icon-btn');
    btn?.classList.add('spinning');
    try { await yaRequest('refresh', {}); } catch (e) { toast(`Could not start the price refresh: ${e.message}`, 'err'); }
    await new Promise(r => setTimeout(r, 5000));
    btn?.classList.remove('spinning');
    await refreshAllPrices();
}

// ── Full Render ───────────────────────────────────────────────────────────────
function render() {
    updateHeader();
    updateDonut();
    renderAccounts();
    renderDebts();
    renderWatchlist();
    renderPortfolioTabs();
    renderStocks();
    renderAltPortChart();
}

// ── Price Fetching — parallel proxies ─────────────────────────────────────────
async function fetchYahoo(symbol) {
    // 1) Prefer the local server proxy (no CORS, no rate limits).
    try {
        const res = await fetch(`/api/quote?symbol=${encodeURIComponent(symbol)}`,
            { signal: AbortSignal.timeout(12000) });
        if (res.ok) {
            const j = await res.json();
            if (j.ok && j.price > 0) {
                return { price: j.price, changePct: j.changePct, priceCur: j.priceCur || 'USD' };
            }
        }
    } catch {}

    // 2) Fallback: public CORS proxies (kept for when running without server.py).
    const url = `https://query1.finance.yahoo.com/v8/finance/chart/${encodeURIComponent(symbol)}?interval=1d&range=1d`;

    const tryProxy = async (proxyUrl) => {
        const res = await fetch(proxyUrl, { signal: AbortSignal.timeout(7000) });
        if (!res.ok) throw new Error('bad');
        let raw = await res.json();
        if (typeof raw.contents === 'string') raw = JSON.parse(raw.contents);
        const meta = raw?.chart?.result?.[0]?.meta;
        if (!meta) throw new Error('no meta');
        const price     = meta.regularMarketPrice;
        const prevClose = meta.chartPreviousClose ?? meta.regularMarketPreviousClose ?? null;
        const changePct = (price && prevClose) ? ((price - prevClose) / prevClose) * 100 : null;
        const priceCur  = meta.currency || 'USD';
        if (!price || price <= 0) throw new Error('no price');
        return { price, changePct, priceCur };
    };

    try {
        return await Promise.any([
            tryProxy(`https://corsproxy.io/?${encodeURIComponent(url)}`),
            tryProxy(`https://api.allorigins.win/get?url=${encodeURIComponent(url)}`),
            tryProxy(`https://api.codetabs.com/v1/proxy?quest=${encodeURIComponent(url)}`),
        ]);
    } catch { return null; }
}

let fetchEpoch = 0;

async function refreshAllPrices() {
    // Main portfolio prices come from the database (n8n valuation refresh);
    // only the local lists (watchlist, extra portfolios) are fetched here.
    const allStocks = S.portfolios.filter(p => p !== mainPort()).flatMap(p => p.stocks);
    const allItems  = [...allStocks, ...S.watchlist];
    if (allItems.length === 0) { await yaSync(); return; }

    const epoch = ++fetchEpoch;
    const btn   = document.querySelector('.icon-btn');
    btn?.classList.add('spinning');

    allItems.forEach(s => { s.fetching = true; s.error = false; });
    renderStocks();
    renderWatchlist();

    await Promise.all(allItems.map(async item => {
        const result = await fetchYahoo(item.sym);
        if (epoch !== fetchEpoch) return;
        item = liveItem(item);

        const prevPrice = item.price;
        item.fetching   = false;

        if (result) {
            const dir = prevPrice != null
                ? (result.price > prevPrice ? 'up' : result.price < prevPrice ? 'down' : null)
                : null;

            item.price       = result.price;
            item.changePct   = result.changePct;
            item.priceCur    = result.priceCur;
            item.error       = false;
            item.lastFetched = Date.now();

            if (dir) {
                const isWatch = S.watchlist.includes(item);
                const prefix  = isWatch ? 'wt' : 'stk';
                setTimeout(() => {
                    const row = document.getElementById(`${prefix}-${item.id}`);
                    if (!row) return;
                    row.classList.add(`flash-${dir}`);
                    setTimeout(() => row.classList.remove(`flash-${dir}`), 900);
                }, 60);
            }
        } else {
            item.error = true;
        }
    }));

    btn?.classList.remove('spinning');
    await yaSync();
    appendDataPoints();
    save();
    render();
    renderPortfolioChart(portChartRange);

    const now = new Date().toLocaleTimeString('en-US', { hour: '2-digit', minute: '2-digit', hourCycle: 'h23' });
    setText('updated-badge', `· ${now}`);
}

async function fetchSingle(id, stocksArray) {
    const item = stocksArray.find(s => s.id === id);
    if (!item) return;
    item.fetching = true;
    item.error    = false;
    const isWatch = stocksArray === S.watchlist;
    if (isWatch) renderWatchlist(); else renderStocks();

    const result = await fetchYahoo(item.sym);
    const cur = isWatch ? liveItem(item) : item;
    item.fetching = cur.fetching = false;

    if (result) {
        for (const it of new Set([item, cur])) {
            it.price       = result.price;
            it.changePct   = result.changePct;
            it.priceCur    = result.priceCur;
            it.error       = false;
            it.lastFetched = Date.now();
        }

        if (!S.priceHistory[item.sym]) S.priceHistory[item.sym] = [];
        S.priceHistory[item.sym].push({ t: Date.now(), p: result.price });

        toast(`${item.sym}: ${fmt(result.price, result.priceCur)}`, 'ok');
    } else {
        item.error = true;
        toast(`${item.sym} price could not be fetched`, 'err');
    }

    save();
    render();
    if (!isWatch) renderPortfolioChart(portChartRange);
}

function startAutoRefresh() {
    clearInterval(refreshInterval);
    refreshInterval = setInterval(refreshAllPrices, AUTO_REFRESH_MS);
}

// ── Exchange Rates ─────────────────────────────────────────────────────────────
async function fetchRates() {
    // FX rates come from the fx_rate table (n8n valuation refresh) with the dashboard payload.
    await yaSync();
}

function startRatesRefresh() {
    clearInterval(ratesInterval);
    ratesInterval = setInterval(fetchRates, RATES_REFRESH_MS);
}

// ── Modals ────────────────────────────────────────────────────────────────────
function openModal(id) {
    document.getElementById(id)?.classList.add('open');
}

function closeModal(id) {
    document.getElementById(id)?.classList.remove('open');
    const form = document.querySelector(`#${id} form`);
    if (form) form.reset();
    if (id === 'modal-account')   editingAccountId = null;
    if (id === 'modal-debt')      editingDebtId    = null;
    if (id === 'modal-portfolio') editingPortId    = null;
}

function openAccountModal() {
    editingAccountId = null;
    document.getElementById('form-account').reset();
    const idx = S.accounts.length % PALETTE.length;
    document.getElementById('a-color').value = PALETTE[idx];
    document.getElementById('modal-account-title').textContent = 'Add Account';
    openModal('modal-account');
}

function openEditAccountModal(id) {
    const acc = S.accounts.find(a => a.id === id);
    if (!acc) return;
    editingAccountId = id;
    document.getElementById('a-bank').value  = acc.bank;
    document.getElementById('a-cur').value   = acc.cur;
    document.getElementById('a-bal').value   = acc.balance;
    document.getElementById('a-color').value = acc.color || '#888888';
    document.getElementById('modal-account-title').textContent = 'Edit Account';
    openModal('modal-account');
}

function openDebtModal() {
    editingDebtId = null;
    document.getElementById('form-debt').reset();
    document.getElementById('modal-debt-title').textContent = 'Add Debt';
    openModal('modal-debt');
}

function openEditDebtModal(id) {
    const debt = S.debts.find(d => d.id === id);
    if (!debt) return;
    editingDebtId = id;
    document.getElementById('d-creditor').value = debt.creditor;
    document.getElementById('d-note').value     = debt.note || '';
    document.getElementById('d-cur').value      = debt.cur;
    document.getElementById('d-amount').value   = debt.amount;
    document.getElementById('modal-debt-title').textContent = 'Edit Debt';
    openModal('modal-debt');
}

function openStockModal(type = 'portfolio') {
    document.getElementById('form-stock').reset();
    document.getElementById('s-type').value = type;
    const fields = document.getElementById('s-portfolio-fields');
    if (fields) fields.style.display = type === 'watchlist' ? 'none' : '';
    const title  = document.getElementById('modal-stk-title');
    if (title) title.textContent = type === 'watchlist' ? 'Add to Watchlist' : 'Add Stock to Portfolio';
    openModal('modal-stock');
}

// ── Portfolio Modal ───────────────────────────────────────────────────────────
function openPortfolioModal(id) {
    editingPortId = id ?? null;
    const port = id ? S.portfolios.find(p => p.id === id) : null;

    document.getElementById('p-name').value    = port?.name ?? '';
    document.getElementById('p-ismain').checked = port?.isMain ?? false;

    // Show "Set as main" only when editing a non-main portfolio
    const mainRow = document.getElementById('p-ismain-row');
    if (mainRow) mainRow.style.display = (port && !port.isMain) ? '' : 'none';

    // Show delete only when editing, not only portfolio, not main
    const delBtn = document.getElementById('p-delete-btn');
    if (delBtn) delBtn.style.display = (port && S.portfolios.length > 1 && !port.isMain) ? '' : 'none';

    setText('modal-port-title', port ? 'Edit Portfolio' : 'New Portfolio');
    openModal('modal-portfolio');
}

function deletePortfolio(id) {
    const port = S.portfolios.find(p => p.id === id);
    if (!port) return;
    if (port.isMain) { toast('The main portfolio cannot be deleted', 'err'); return; }
    if (S.portfolios.length <= 1) { toast('The last portfolio cannot be deleted', 'err'); return; }
    if (!confirm(`"${port.name}" portfolio will be deleted. Are you sure?`)) return;
    S.portfolios = S.portfolios.filter(p => p.id !== id);
    if (S.activePortId === id) S.activePortId = mainPort()?.id ?? S.portfolios[0].id;
    save(); closeModal('modal-portfolio'); render();
    toast(`${port.name} deleted`, 'ok');
}

// ── Export / Import / Reset ───────────────────────────────────────────────────
function exportData() {
    const stripStk = ({ fetching, ...r }) => r;
    const clean = {
        ...S,
        portfolios: S.portfolios.map(port => ({ ...port, stocks: port.stocks.map(stripStk) })),
        watchlist:  S.watchlist.map(stripStk),
    };
    const blob  = new Blob([JSON.stringify(clean, null, 2)], { type: 'application/json' });
    const url   = URL.createObjectURL(blob);
    const a     = Object.assign(document.createElement('a'), {
        href: url, download: `findash_${new Date().toISOString().split('T')[0]}.json`
    });
    a.click();
    URL.revokeObjectURL(url);
    toast('Exported', 'ok');
}

function importData() {
    const inp = Object.assign(document.createElement('input'), { type: 'file', accept: '.json' });
    inp.onchange = e => {
        const file = e.target.files[0];
        if (!file) return;
        const reader = new FileReader();
        reader.onload = ev => {
            try {
                if (!confirm('Current data will be replaced by the import. Continue?')) return;
                const p = JSON.parse(ev.target.result);
                // Support both old and new format via load migration
                localStorage.setItem('fd3', JSON.stringify(p));
                load();
                save(); render();
                const allStocks = S.portfolios.flatMap(pt => pt.stocks);
                if (allStocks.length > 0 || S.watchlist.length > 0) refreshAllPrices();
                toast('Imported', 'ok');
            } catch { toast('Invalid file', 'err'); }
        };
        reader.readAsText(file);
    };
    inp.click();
}

function confirmReset() {
    if (!confirm('All data will be deleted. Are you sure?')) return;
    localStorage.removeItem('fd3');
    const defPort = { id: uid(), name: 'Main Portfolio', isMain: true, stocks: [], portSnaps: [] };
    S = {
        accounts: [], debts: [],
        portfolios: [defPort], activePortId: defPort.id,
        watchlist: [], rates: { USD: 38.5, EUR: 41.0, GBP: 48.0 }, priceHistory: {},
    };
    render();
    toast('Reset done', 'ok');
}

// ── Toast ─────────────────────────────────────────────────────────────────────
function toast(msg, type = 'ok') {
    const el = document.createElement('div');
    el.className = `toast ${type}`;
    el.textContent = msg;
    document.getElementById('toasts').appendChild(el);
    setTimeout(() => el.remove(), 3000);
}

// ── Form Handlers ─────────────────────────────────────────────────────────────
function initForms() {
    document.getElementById('form-account').addEventListener('submit', async e => {
        e.preventDefault();
        const bank    = document.getElementById('a-bank').value.trim();
        const cur     = document.getElementById('a-cur').value;
        const balance = parseFloat(document.getElementById('a-bal').value);
        const color   = document.getElementById('a-color').value;
        if (!bank || isNaN(balance)) return;
        const fields = { kind: 'cash', name: bank, currency: cur, amount: balance, color };
        const ok = editingAccountId
            ? await yaWrite('asset', 'update', editingAccountId, fields)
            : await yaWrite('asset', 'create', null, fields);
        if (!ok) return;
        toast(`${bank} ${editingAccountId ? 'updated' : 'added'}`, 'ok');
        editingAccountId = null;
        closeModal('modal-account');
    });

    document.getElementById('form-debt').addEventListener('submit', async e => {
        e.preventDefault();
        const creditor = document.getElementById('d-creditor').value.trim();
        const note     = document.getElementById('d-note').value.trim();
        const cur      = document.getElementById('d-cur').value;
        const amount   = parseFloat(document.getElementById('d-amount').value);
        if (!creditor || isNaN(amount) || amount <= 0) return;
        const fields = { counterparty: creditor, note, currency: cur, amount };
        const ok = editingDebtId
            ? await yaWrite('liability', 'update', editingDebtId, fields)
            : await yaWrite('liability', 'create', null, fields);
        if (!ok) return;
        toast(editingDebtId ? `${creditor} updated` : `${creditor} debt added`, 'ok');
        editingDebtId = null;
        closeModal('modal-debt');
    });

    document.getElementById('form-stock').addEventListener('submit', async e => {
        e.preventDefault();
        const sym  = document.getElementById('s-sym').value.trim().toUpperCase();
        const name = document.getElementById('s-name').value.trim();
        const type = document.getElementById('s-type').value;
        if (!sym) return;

        if (type === 'watchlist') {
            if (!(await yaWrite('watchlist', 'create', null, { symbol: sym, name }))) return;
            closeModal('modal-stock');
            toast(`${sym} added to watchlist`, 'inf');
            const item = S.watchlist.find(w => w.sym === sym);
            if (item) fetchSingle(item.id, S.watchlist);
        } else {
            const qty  = parseFloat(document.getElementById('s-qty').value)  || 0;
            const cost = parseFloat(document.getElementById('s-cost').value) || 0;
            const cur  = guessCur(sym);
            if (activePort() === mainPort()) {
                // Main portfolio = holdings table; the price arrives with the next valuation refresh.
                if (qty <= 0) { toast('Enter a quantity for the main portfolio', 'err'); return; }
                const ok = await yaWrite('holding', 'create', null,
                    { symbol: sym, name, quantity: qty, cost_per_share: cost, cost_currency: cur });
                if (!ok) return;
                closeModal('modal-stock');
                toast(`${sym} added, fetching price…`, 'inf');
                refreshNow();
                return;
            }
            const stk  = { id: uid(), sym, name, cur, qty, cost: cost > 0 ? cost : null, price: null, changePct: null, priceCur: cur, fetching: true, error: false };
            const port = activePort();
            if (!port) return;
            port.stocks.push(stk);
            save(); closeModal('modal-stock'); render();
            toast(`${sym} added, fetching price…`, 'inf');
            fetchSingle(stk.id, port.stocks);
        }
    });

    document.getElementById('form-portfolio').addEventListener('submit', e => {
        e.preventDefault();
        const name   = document.getElementById('p-name').value.trim();
        const isMain = document.getElementById('p-ismain').checked;
        if (!name) return;

        if (editingPortId) {
            const port = S.portfolios.find(p => p.id === editingPortId);
            if (port) {
                port.name = name;
                if (isMain && !port.isMain) {
                    // Transfer main status
                    S.portfolios.forEach(p => { p.isMain = false; });
                    port.isMain = true;
                }
            }
            toast(`${name} updated`, 'ok');
        } else {
            const newPort = { id: uid(), name, isMain: false, stocks: [], portSnaps: [] };
            S.portfolios.push(newPort);
            S.activePortId = newPort.id;
            toast(`${name} created`, 'ok');
        }

        save(); closeModal('modal-portfolio'); render();
    });

    document.getElementById('s-sym').addEventListener('blur', e => {
        const sym = e.target.value.trim();
        if (sym) { const curEl = document.getElementById('s-cur'); if (curEl) curEl.value = guessCur(sym); }
    });

    document.querySelectorAll('.overlay').forEach(o => {
        o.addEventListener('click', e => { if (e.target === o) closeModal(o.id); });
    });
}

// ── Calendar ──────────────────────────────────────────────────────────────────
const calState = { year: new Date().getFullYear(), month: new Date().getMonth(), selectedDate: null };

function p2(n) { return String(n).padStart(2, '0'); }

function renderCalendar() {
    const grid    = document.getElementById('cal-grid');
    const titleEl = document.getElementById('cal-title');
    if (!grid || !titleEl) return;

    const { year, month } = calState;
    const monthNames = ['January','February','March','April','May','June','July','August','September','October','November','December'];
    titleEl.textContent = `${monthNames[month]} ${year}`;

    const eventDates = new Set((S.calendarEvents || []).map(e => e.date));
    const today      = new Date();
    const todayStr   = `${today.getFullYear()}-${p2(today.getMonth()+1)}-${p2(today.getDate())}`;

    // Week starts Monday; getDay() returns 0=Sun
    const firstDow  = new Date(year, month, 1).getDay();
    const offset    = firstDow === 0 ? 6 : firstDow - 1;
    const daysInMo  = new Date(year, month + 1, 0).getDate();

    const dayLabels = ['Mo','Tu','We','Th','Fr','Sa','Su'];
    let html = dayLabels.map(d => `<div class="cal-day-lbl">${d}</div>`).join('');
    for (let i = 0; i < offset; i++) html += `<div class="cal-day cal-day-empty"></div>`;

    for (let d = 1; d <= daysInMo; d++) {
        const ds  = `${year}-${p2(month+1)}-${p2(d)}`;
        const cls = ['cal-day',
            ds === todayStr          ? 'cal-today'     : '',
            ds === calState.selectedDate ? 'cal-selected' : '',
            eventDates.has(ds)       ? 'cal-has-event' : '',
        ].filter(Boolean).join(' ');
        html += `<div class="${cls}" onclick="calSelectDay('${ds}')">${d}</div>`;
    }
    grid.innerHTML = html;
    renderCalEvents();
}

function renderCalEvents() {
    const list = document.getElementById('cal-events-list');
    if (!list) return;
    const events = [...(S.calendarEvents || [])].sort((a, b) => a.date.localeCompare(b.date));
    if (!events.length) { list.innerHTML = ''; return; }
    list.innerHTML = events.map(e => {
        const d     = new Date(e.date + 'T12:00:00');
        const label = d.toLocaleDateString('en-US', { day: 'numeric', month: 'short' });
        return `<div class="cal-event-item">
            <span class="cal-event-dot">•</span>
            <span class="cal-event-date">${label}</span>
            <span class="cal-event-text">${esc(e.text)}</span>
            <button class="cal-event-del" onclick="delCalEvent('${e.id}')">×</button>
        </div>`;
    }).join('');
}

function calSelectDay(ds) {
    calState.selectedDate = ds;
    const d     = new Date(ds + 'T12:00:00');
    const label = d.toLocaleDateString('en-US', { day: 'numeric', month: 'long', year: 'numeric' });
    const selEl = document.getElementById('cal-selected-label');
    if (selEl) selEl.textContent = label;
    const row = document.getElementById('cal-add-row');
    if (row) row.style.display = '';
    const inp = document.getElementById('cal-event-input');
    if (inp) inp.focus();
    renderCalendar();
}

function addCalEvent() {
    const inp = document.getElementById('cal-event-input');
    if (!inp || !calState.selectedDate) return;
    const text = inp.value.trim();
    if (!text) return;
    S.calendarEvents.push({ id: uid(), date: calState.selectedDate, text });
    inp.value = '';
    save();
    renderCalendar();
}

function delCalEvent(id) {
    S.calendarEvents = S.calendarEvents.filter(e => e.id !== id);
    save();
    renderCalendar();
}

function calPrev() {
    calState.month--;
    if (calState.month < 0) { calState.month = 11; calState.year--; }
    renderCalendar();
}

function calNext() {
    calState.month++;
    if (calState.month > 11) { calState.month = 0; calState.year++; }
    renderCalendar();
}

// ── Economic Calendar ──────────────────────────────────────────────────────────
const IMP_COLORS = { Low: '#4a5060', Medium: '#b08030', High: '#c04040' };
let _econAllEvents = [];
const ECON_REGIONS = [
    { cur: 'USD', flag: '🇺🇸', name: 'United States' },
    { cur: 'EUR', flag: '🇪🇺', name: 'Euro Area' },
    { cur: 'GBP', flag: '🇬🇧', name: 'United Kingdom' },
    { cur: 'JPY', flag: '🇯🇵', name: 'Japan' },
    { cur: 'CNY', flag: '🇨🇳', name: 'China' },
    { cur: 'CAD', flag: '🇨🇦', name: 'Canada' },
    { cur: 'AUD', flag: '🇦🇺', name: 'Australia' },
    { cur: 'CHF', flag: '🇨🇭', name: 'Switzerland' },
];
const ECON_FLAG = Object.fromEntries(ECON_REGIONS.map(r => [r.cur, r.flag]));
let _econFilters = new Set(['USD', 'EUR']);
try {   // remembered per browser; a convenience only
    const saved = JSON.parse(localStorage.getItem('ya.econRegions') ?? 'null');
    if (Array.isArray(saved) && saved.length) _econFilters = new Set(saved.filter(c => ECON_FLAG[c]));
} catch {}

// Compact picker: selected flags on a pill; the menu lists every region with a check.
function renderEconRegions() {
    const sel = ECON_REGIONS.filter(r => _econFilters.has(r.cur));
    const flags = document.getElementById('econ-region-flags');
    if (flags) flags.innerHTML = sel.slice(0, 3).map(r => `<span title="${r.name}">${r.flag}</span>`).join('')
        + (sel.length > 3 ? `<em class="econ-region-more">+${sel.length - 3}</em>` : '');
    const menu = document.getElementById('econ-menu');
    if (menu) menu.innerHTML = ECON_REGIONS.map(r => `
        <button class="econ-menu-item${_econFilters.has(r.cur) ? ' on' : ''}" role="menuitemcheckbox"
                aria-checked="${_econFilters.has(r.cur)}" onclick="toggleEconFilter('${r.cur}', event)">
            <span class="flag">${r.flag}</span><span class="name">${r.name}</span>
            <span class="code">${r.cur}</span><span class="check"></span>
        </button>`).join('');
}

function toggleEconMenu(e, open) {
    e?.stopPropagation();
    const menu = document.getElementById('econ-menu'), btn = document.getElementById('econ-region-btn');
    if (!menu) return;
    const show = open ?? !menu.classList.contains('open');
    menu.classList.toggle('open', show);
    btn?.setAttribute('aria-expanded', String(show));
}
document.addEventListener('click', e => { if (!e.target.closest?.('#econ-region')) toggleEconMenu(null, false); });
document.addEventListener('keydown', e => { if (e.key === 'Escape') toggleEconMenu(null, false); });

function toggleEconFilter(cur, e) {
    e?.stopPropagation();   // keep the menu open while picking several regions
    if (_econFilters.has(cur)) {
        if (_econFilters.size === 1) return; // keep at least 1 active
        _econFilters.delete(cur);
    } else {
        _econFilters.add(cur);
    }
    try { localStorage.setItem('ya.econRegions', JSON.stringify([..._econFilters])); } catch {}
    renderEconRegions();
    renderEconCalendar(_econAllEvents);
}

async function fetchEconCalendar() {
    const el = document.getElementById('econ-cal-list');
    if (!el) return;
    el.innerHTML = '<div class="econ-loading">Loading…</div>';

    try {
        const r = await fetch('/api/econ-calendar', { signal: AbortSignal.timeout(15000) });
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        const events = await r.json();
        const valid = events.filter(e => e && e.date && e.title);
        if (valid.length) { _econAllEvents = valid; renderEconCalendar(valid); return; }
        el.innerHTML = '<div class="econ-empty">No events this week</div>';
    } catch (err) {
        console.error('[econ]', err);
        el.innerHTML = '<div class="econ-empty">Could not load — refresh the page</div>';
    }
}

function renderEconCalendar(events) {
    const el = document.getElementById('econ-cal-list');
    if (!el) return;

    // Show from Monday of this week
    const now = new Date();
    const day = now.getDay(); // 0=Pazar
    const diff = day === 0 ? -6 : 1 - day;
    const weekStart = new Date(now);
    weekStart.setDate(now.getDate() + diff);
    weekStart.setHours(0, 0, 0, 0);

    const filtered = events
        .map(e => ({ ...e, _d: new Date(e.date) }))
        .filter(e => !isNaN(e._d) && e._d >= weekStart && _econFilters.has(e.country))
        .sort((a, b) => a._d - b._d);

    if (!filtered.length) { el.innerHTML = '<div class="econ-empty">No events left — the next week calendar is published on Sunday evening</div>'; return; }

    // Group by date
    const byDate = new Map();
    for (const ev of filtered) {
        const key = ev._d.toLocaleDateString('en-US', { weekday: 'short', day: 'numeric', month: 'short' });
        if (!byDate.has(key)) byDate.set(key, []);
        byDate.get(key).push(ev);
    }

    let html = '';
    for (const [label, evs] of byDate) {
        const impDot = ev => {
            const col = IMP_COLORS[ev.impact] || IMP_COLORS.Low;
            return `<span style="color:${col};font-size:9px;flex-shrink:0">●</span>`;
        };
        html += `<div class="econ-date-group">
            <div class="econ-date-label">${label}</div>
            ${evs.map(ev => {
                const newsUrl = `https://news.google.com/search?q=${encodeURIComponent((ev.title || '') + ' ' + (ev.country || ''))}`;
                const name   = esc(ev.title || '—');
                const cntry  = ev.country ? (ECON_FLAG[ev.country]
                    ? `<span class="econ-flag" title="${esc(ev.country)}">${ECON_FLAG[ev.country]}</span>`
                    : `<span class="econ-country">${esc(ev.country)}</span>`) : '';
                const actual = ev.actual   ? `<span class="econ-val actual">${esc(ev.actual)}</span>`
                             : ev.forecast ? `<span class="econ-val">${esc(ev.forecast)}</span>` : '';
                return `<a class="econ-event-item" href="${newsUrl}" target="_blank" rel="noopener">${impDot(ev)}${cntry}<span class="econ-event-name">${name}</span>${actual}</a>`;
            }).join('')}
        </div>`;
    }
    el.innerHTML = html;
}

// ── Moon Phase Clock ──────────────────────────────────────────────────────────
let moonClockFrame = null;

function getMoonPhase() {
    // Reference new moon: Jan 6 2000 18:14 UTC
    const ref = new Date(Date.UTC(2000, 0, 6, 18, 14));
    const cycle = 29.530588853;
    const days = (Date.now() - ref) / 86400000;
    return ((days % cycle) + cycle) % cycle; // 0–29.53
}

function getMoonPhaseName(phase) {
    const p = phase / 29.530588853;
    if (p < 0.025 || p > 0.975) return 'New Moon';
    if (p < 0.24)  return 'Waxing Crescent';
    if (p < 0.27)  return 'First Quarter';
    if (p < 0.49)  return 'Waxing Gibbous';
    if (p < 0.52)  return 'Full Moon';
    if (p < 0.74)  return 'Waning Gibbous';
    if (p < 0.77)  return 'Last Quarter';
    return 'Waning Crescent';
}

// Draw a luxury Dauphine-style hand (diamond profile, tapers to a point)
function drawDauphineHand(ctx, length, tailLen, maxWidth, color, shadowColor) {
    ctx.save();
    ctx.shadowColor = shadowColor || 'rgba(0,0,0,0.6)';
    ctx.shadowBlur  = 4;
    ctx.shadowOffsetX = 1; ctx.shadowOffsetY = 1;

    // Dauphine profile: wide ridge that tapers symmetrically
    const ridgeAt = length * 0.38; // widest point
    ctx.beginPath();
    ctx.moveTo(0, -tailLen);                         // tail tip
    ctx.lineTo(-maxWidth * 0.45, -tailLen * 0.4);   // tail base left
    ctx.lineTo(-maxWidth, ridgeAt * 0.5);            // shoulder left
    ctx.lineTo(0, length);                           // tip
    ctx.lineTo(maxWidth, ridgeAt * 0.5);             // shoulder right
    ctx.lineTo(maxWidth * 0.45, -tailLen * 0.4);     // tail base right
    ctx.closePath();

    // White gold gradient along the length
    const grd = ctx.createLinearGradient(-maxWidth, 0, maxWidth, 0);
    grd.addColorStop(0,    'rgba(0,0,0,0.35)');
    grd.addColorStop(0.28, color);
    grd.addColorStop(0.5,  '#ffffff');
    grd.addColorStop(0.72, color);
    grd.addColorStop(1,    'rgba(0,0,0,0.35)');
    ctx.fillStyle = grd;
    ctx.fill();

    ctx.shadowColor = 'transparent';
    ctx.strokeStyle = 'rgba(0,0,0,0.3)';
    ctx.lineWidth = 0.4;
    ctx.stroke();
    ctx.restore();
}

function drawMoonClock() {
    const canvas = document.getElementById('moon-canvas');
    if (!canvas) return;
    const ctx = canvas.getContext('2d');
    const dpr = window.devicePixelRatio || 1;

    ctx.save();
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);

    const S = 190, cx = 95, cy = 95;
    ctx.clearRect(0, 0, S, S);

    const now  = new Date();
    const hrs  = now.getHours() % 12 + now.getMinutes() / 60 + now.getSeconds() / 3600;
    const mins = now.getMinutes() + now.getSeconds() / 60;
    const secs = now.getSeconds() + now.getMilliseconds() / 1000;

    const phase     = getMoonPhase();
    const phaseFrac = phase / 29.530588853;

    const Rcased = 93;   // outer case edge
    const Rbezel = 88;   // bezel outer
    const Rdial  = 80;   // dial edge
    const moonR  = 66;   // moon disc radius

    // ── 1. Watch case drop shadow
    ctx.save();
    ctx.shadowColor   = 'rgba(0,0,0,0.7)';
    ctx.shadowBlur    = 18;
    ctx.shadowOffsetY = 6;
    ctx.beginPath();
    ctx.arc(cx, cy, Rcased, 0, Math.PI * 2);
    ctx.fillStyle = '#1a1a2e';
    ctx.fill();
    ctx.restore();

    // ── 2. Platinum/white-gold bezel — cool silver multi-stop radial gradient
    const bezG = ctx.createRadialGradient(cx - 20, cy - 20, 10, cx, cy, Rbezel);
    bezG.addColorStop(0,    '#ffffff');
    bezG.addColorStop(0.15, '#dce0e8');
    bezG.addColorStop(0.32, '#a8b0bc');
    bezG.addColorStop(0.52, '#e8ecf2');
    bezG.addColorStop(0.70, '#8090a0');
    bezG.addColorStop(0.86, '#c4ccd8');
    bezG.addColorStop(1,    '#4a5560');
    ctx.beginPath();
    ctx.arc(cx, cy, Rbezel, 0, Math.PI * 2);
    ctx.fillStyle = bezG;
    ctx.fill();

    // Bezel knurling lines (fine engraved look)
    ctx.save();
    ctx.beginPath();
    ctx.arc(cx, cy, Rbezel, 0, Math.PI * 2);
    ctx.clip();
    for (let i = 0; i < 72; i++) {
        const a = (i / 72) * Math.PI * 2;
        ctx.beginPath();
        ctx.moveTo(cx + Math.cos(a) * Rdial,  cy + Math.sin(a) * Rdial);
        ctx.lineTo(cx + Math.cos(a) * Rbezel, cy + Math.sin(a) * Rbezel);
        ctx.strokeStyle = 'rgba(0,0,0,0.18)';
        ctx.lineWidth = 0.5;
        ctx.stroke();
    }
    ctx.restore();

    // Bezel inner shadow ring
    const bezInner = ctx.createRadialGradient(cx, cy, Rdial - 4, cx, cy, Rdial + 1);
    bezInner.addColorStop(0, 'rgba(0,0,0,0.55)');
    bezInner.addColorStop(1, 'rgba(0,0,0,0)');
    ctx.beginPath();
    ctx.arc(cx, cy, Rdial, 0, Math.PI * 2);
    ctx.fillStyle = bezInner;
    ctx.fill();

    // ── 3. Deep navy enamel dial
    ctx.save();
    ctx.beginPath();
    ctx.arc(cx, cy, Rdial, 0, Math.PI * 2);
    ctx.clip();

    const dialG = ctx.createRadialGradient(cx - 18, cy - 18, 5, cx, cy, Rdial);
    dialG.addColorStop(0,   '#1c2a5a');
    dialG.addColorStop(0.4, '#0d1a3e');
    dialG.addColorStop(0.8, '#080f2a');
    dialG.addColorStop(1,   '#050a1c');
    ctx.fillStyle = dialG;
    ctx.fillRect(cx - Rdial, cy - Rdial, Rdial * 2, Rdial * 2);
    ctx.restore();

    // ── 4. Stars — deterministic pseudo-random scatter
    const starCount = 42;
    ctx.save();
    ctx.beginPath();
    ctx.arc(cx, cy, Rdial - 1, 0, Math.PI * 2);
    ctx.clip();
    for (let i = 0; i < starCount; i++) {
        // Keep stars away from center (moon area) and edges
        let sx, sy, dist;
        const seed1 = Math.sin(i * 7.391) * 0.5 + 0.5;
        const seed2 = Math.sin(i * 13.17 + 2.4) * 0.5 + 0.5;
        const angle = seed1 * Math.PI * 2;
        // Distribute radially but only outside moon disc radius
        const minR = moonR + 4;
        const maxR = Rdial - 8;
        dist = minR + seed2 * (maxR - minR);
        sx = cx + Math.cos(angle) * dist;
        sy = cy + Math.sin(angle) * dist;

        const brightness = 0.4 + Math.sin(i * 3.7) * 0.3 + 0.3;
        const size = 0.5 + Math.sin(i * 5.13 + 1) * 0.4 + 0.4;

        // Occasional bigger star with 4-point twinkle
        if (i % 9 === 0) {
            ctx.save();
            ctx.translate(sx, sy);
            ctx.strokeStyle = `rgba(255,230,140,${brightness * 0.7})`;
            ctx.lineWidth = 0.5;
            for (let ray = 0; ray < 4; ray++) {
                ctx.beginPath();
                ctx.rotate(Math.PI / 4);
                ctx.moveTo(0, 0);
                ctx.lineTo(0, size * 2.5);
                ctx.stroke();
            }
            ctx.restore();
        }

        ctx.beginPath();
        ctx.arc(sx, sy, size, 0, Math.PI * 2);
        ctx.fillStyle = `rgba(255,230,140,${brightness})`;
        ctx.fill();
    }
    ctx.restore();

    // ── 5. Moon sphere — 3D golden with phase
    ctx.save();
    ctx.beginPath();
    ctx.arc(cx, cy, moonR, 0, Math.PI * 2);
    ctx.clip();

    // Full sphere base — dark side (space color)
    ctx.fillStyle = '#040913';
    ctx.fillRect(cx - moonR, cy - moonR, moonR * 2, moonR * 2);

    if (phaseFrac > 0.01 && phaseFrac < 0.99) {
        // Lit surface — cool white/silver moon
        const litG = ctx.createRadialGradient(cx - moonR * 0.22, cy - moonR * 0.22, 0, cx, cy, moonR);
        litG.addColorStop(0,    '#ffffff');
        litG.addColorStop(0.22, '#dce8f0');
        litG.addColorStop(0.5,  '#a8bece');
        litG.addColorStop(0.78, '#607888');
        litG.addColorStop(1,    '#283848');
        ctx.fillStyle = litG;

        const PI = Math.PI;
        const cosP  = Math.cos(phaseFrac * PI * 2);
        const termX = moonR * Math.abs(cosP);

        ctx.beginPath();
        if (phaseFrac < 0.5) {
            ctx.arc(cx, cy, moonR, -PI / 2, PI / 2, false);
            ctx.ellipse(cx, cy, termX, moonR, 0, PI / 2, -PI / 2, cosP > 0);
        } else {
            ctx.arc(cx, cy, moonR, -PI / 2, PI / 2, true);
            ctx.ellipse(cx, cy, termX, moonR, 0, PI / 2, -PI / 2, cosP < 0);
        }
        ctx.closePath();
        ctx.fill();

        // Terminator atmospheric glow
        const atmW = moonR * 0.12;
        const atmX = (phaseFrac < 0.5) ? cx + cosP * moonR : cx + cosP * moonR;
        const atmG = ctx.createRadialGradient(atmX, cy, 0, atmX, cy, atmW + moonR * 0.08);
        atmG.addColorStop(0, 'rgba(180,140,60,0.18)');
        atmG.addColorStop(1, 'rgba(180,140,60,0)');
        ctx.fillStyle = atmG;
        ctx.beginPath();
        ctx.arc(cx, cy, moonR, 0, Math.PI * 2);
        ctx.fill();
    }
    ctx.restore(); // end moon clip

    // ── 6. Craters — drawn on top of lit surface only
    const craterDefs = [
        { ox: -0.28, oy: -0.15, r: 0.11 },
        { ox:  0.18, oy: -0.35, r: 0.09 },
        { ox: -0.05, oy:  0.30, r: 0.13 },
        { ox:  0.32, oy:  0.12, r: 0.07 },
        { ox: -0.40, oy:  0.28, r: 0.06 },
        { ox:  0.10, oy: -0.05, r: 0.05 },
        { ox: -0.18, oy:  0.45, r: 0.055 },
        { ox:  0.38, oy: -0.30, r: 0.05 },
    ];
    ctx.save();
    ctx.beginPath();
    ctx.arc(cx, cy, moonR - 0.5, 0, Math.PI * 2);
    ctx.clip();

    // Only render craters in the lit region
    for (const c of craterDefs) {
        const crX = cx + c.ox * moonR;
        const crY = cy + c.oy * moonR;
        const crR = c.r * moonR;

        // Check if crater center is in lit region
        const pi2 = Math.PI * 2;
        const cosP = Math.cos(phaseFrac * pi2);
        const inLit = (phaseFrac < 0.5)
            ? (crX - cx) <= moonR * Math.abs(cosP) + crR
            : (crX - cx) >= -moonR * Math.abs(cosP) - crR;

        if (!inLit && phaseFrac > 0.01 && phaseFrac < 0.99) continue;

        // Crater bowl shadow
        const bowl = ctx.createRadialGradient(crX + crR * 0.2, crY - crR * 0.15, 0, crX, crY, crR);
        bowl.addColorStop(0,   'rgba(10,15,25,0.60)');
        bowl.addColorStop(0.6, 'rgba(10,15,25,0.28)');
        bowl.addColorStop(1,   'rgba(0,0,0,0)');
        ctx.beginPath();
        ctx.arc(crX, crY, crR, 0, Math.PI * 2);
        ctx.fillStyle = bowl;
        ctx.fill();

        // Crater rim highlight
        ctx.beginPath();
        ctx.arc(crX, crY, crR, Math.PI * 1.1, Math.PI * 1.9);
        ctx.strokeStyle = 'rgba(220,235,255,0.35)';
        ctx.lineWidth = crR * 0.28;
        ctx.stroke();
    }
    ctx.restore();

    // ── 7. Moon outer glow ring
    const moonGlow = ctx.createRadialGradient(cx, cy, moonR - 3, cx, cy, moonR + 8);
    moonGlow.addColorStop(0,   'rgba(180,200,240,0.18)');
    moonGlow.addColorStop(0.5, 'rgba(180,200,240,0.06)');
    moonGlow.addColorStop(1,   'rgba(180,200,240,0)');
    ctx.beginPath();
    ctx.arc(cx, cy, moonR + 8, 0, Math.PI * 2);
    ctx.fillStyle = moonGlow;
    ctx.fill();

    // Moon disc edge (thin platinum ring)
    ctx.beginPath();
    ctx.arc(cx, cy, moonR, 0, Math.PI * 2);
    ctx.strokeStyle = 'rgba(200,220,240,0.45)';
    ctx.lineWidth = 1;
    ctx.stroke();

    // ── 8. Hour indices — gold baton marks
    for (let i = 0; i < 12; i++) {
        const a   = (i / 12) * Math.PI * 2 - Math.PI / 2;
        const ro  = Rdial - 4;
        const ri  = Rdial - 12;
        const x1  = cx + Math.cos(a) * ri;
        const y1  = cy + Math.sin(a) * ri;
        const x2  = cx + Math.cos(a) * ro;
        const y2  = cy + Math.sin(a) * ro;

        ctx.save();
        ctx.shadowColor = 'rgba(0,0,0,0.5)';
        ctx.shadowBlur  = 3;
        ctx.beginPath();
        ctx.moveTo(x1, y1);
        ctx.lineTo(x2, y2);
        ctx.strokeStyle = (i % 3 === 0) ? '#ffffff' : 'rgba(200,210,220,0.7)';
        ctx.lineWidth   = (i % 3 === 0) ? 2.5 : 1.2;
        ctx.lineCap = 'round';
        ctx.stroke();
        ctx.restore();
    }

    // Minute dots
    for (let i = 0; i < 60; i++) {
        if (i % 5 === 0) continue;
        const a = (i / 60) * Math.PI * 2 - Math.PI / 2;
        const r = Rdial - 6;
        ctx.beginPath();
        ctx.arc(cx + Math.cos(a) * r, cy + Math.sin(a) * r, 0.7, 0, Math.PI * 2);
        ctx.fillStyle = 'rgba(180,200,220,0.55)';
        ctx.fill();
    }

    // ── 9. Glossy enamel overlay on dial
    const glossG = ctx.createRadialGradient(cx - 28, cy - 32, 0, cx, cy, Rdial);
    glossG.addColorStop(0,    'rgba(255,255,255,0.10)');
    glossG.addColorStop(0.35, 'rgba(255,255,255,0.04)');
    glossG.addColorStop(0.7,  'rgba(0,0,0,0)');
    glossG.addColorStop(1,    'rgba(0,0,0,0.12)');
    ctx.beginPath();
    ctx.arc(cx, cy, Rdial, 0, Math.PI * 2);
    ctx.fillStyle = glossG;
    ctx.fill();

    // ── 10. Crystal reflection — upper-left arc
    ctx.save();
    ctx.beginPath();
    ctx.arc(cx, cy, Rcased, 0, Math.PI * 2);
    ctx.clip();
    const crysG = ctx.createLinearGradient(cx - Rcased, cy - Rcased, cx + Rcased * 0.3, cy + Rcased * 0.3);
    crysG.addColorStop(0,    'rgba(255,255,255,0.13)');
    crysG.addColorStop(0.4,  'rgba(255,255,255,0.04)');
    crysG.addColorStop(1,    'rgba(255,255,255,0)');
    ctx.fillStyle = crysG;
    ctx.fillRect(cx - Rcased, cy - Rcased, Rcased * 2, Rcased * 2);
    ctx.restore();

    // ── 11. Hour hand (Dauphine white gold / platinum)
    const hA = (hrs / 12) * Math.PI * 2 - Math.PI / 2;
    ctx.save();
    ctx.translate(cx, cy);
    ctx.rotate(hA);
    drawDauphineHand(ctx, moonR * 0.56, 9, 3.8, '#c8d4e0', 'rgba(0,0,0,0.6)');
    ctx.restore();

    // ── 12. Minute hand (Dauphine white gold, longer + slimmer)
    const mA = (mins / 60) * Math.PI * 2 - Math.PI / 2;
    ctx.save();
    ctx.translate(cx, cy);
    ctx.rotate(mA);
    drawDauphineHand(ctx, Rdial - 10, 11, 2.8, '#b8c8d8', 'rgba(0,0,0,0.6)');
    ctx.restore();

    // ── 13. Second hand — blued steel with lollipop counterweight
    const sA = (secs / 60) * Math.PI * 2 - Math.PI / 2;
    ctx.save();
    ctx.translate(cx, cy);
    ctx.rotate(sA);
    ctx.save();
    ctx.shadowColor = 'rgba(0,0,0,0.5)'; ctx.shadowBlur = 3;
    // Main shaft
    ctx.beginPath();
    ctx.moveTo(0, -18);
    ctx.lineTo(0, Rdial - 12);
    ctx.strokeStyle = '#e03030';
    ctx.lineWidth = 0.9;
    ctx.lineCap = 'round';
    ctx.stroke();
    // Lollipop counterweight
    ctx.beginPath();
    ctx.arc(0, -13, 4, 0, Math.PI * 2);
    ctx.fillStyle = '#e03030';
    ctx.fill();
    ctx.restore();
    ctx.restore();

    // ── 14. Center jewel cap
    const jewelG = ctx.createRadialGradient(cx - 1.5, cy - 1.5, 0, cx, cy, 5.5);
    jewelG.addColorStop(0,   '#ffffff');
    jewelG.addColorStop(0.4, '#c8d4e0');
    jewelG.addColorStop(0.8, '#7090a8');
    jewelG.addColorStop(1,   '#304050');
    ctx.beginPath();
    ctx.arc(cx, cy, 5.5, 0, Math.PI * 2);
    ctx.fillStyle = jewelG;
    ctx.fill();
    ctx.beginPath();
    ctx.arc(cx, cy, 5.5, 0, Math.PI * 2);
    ctx.strokeStyle = 'rgba(0,0,0,0.4)';
    ctx.lineWidth = 0.5;
    ctx.stroke();
    // Inner ruby
    ctx.beginPath();
    ctx.arc(cx, cy, 2.2, 0, Math.PI * 2);
    ctx.fillStyle = '#c02020';
    ctx.fill();
    ctx.beginPath();
    ctx.arc(cx - 0.6, cy - 0.6, 0.7, 0, Math.PI * 2);
    ctx.fillStyle = 'rgba(255,200,200,0.6)';
    ctx.fill();

    // ── 15. Vignette edge
    const vigG = ctx.createRadialGradient(cx, cy, Rdial * 0.55, cx, cy, Rcased);
    vigG.addColorStop(0, 'rgba(0,0,0,0)');
    vigG.addColorStop(1, 'rgba(0,0,0,0.45)');
    ctx.beginPath();
    ctx.arc(cx, cy, Rcased, 0, Math.PI * 2);
    ctx.fillStyle = vigG;
    ctx.fill();

    ctx.restore(); // main transform

    // ── Text updates
    const timeEl = document.getElementById('moon-time');
    if (timeEl) timeEl.textContent = now.toLocaleTimeString('en-US', { hour: '2-digit', minute: '2-digit', hourCycle: 'h23', second: '2-digit' });

    const phaseEl = document.getElementById('moon-phase-name');
    if (phaseEl) phaseEl.textContent = getMoonPhaseName(phase);

    const dateEl = document.getElementById('moon-date');
    if (dateEl) dateEl.textContent = now.toLocaleDateString('en-US', { weekday: 'short', day: 'numeric', month: 'short' });

    moonClockFrame = requestAnimationFrame(drawMoonClock);
}

function startMoonClock() {
    const canvas = document.getElementById('moon-canvas');
    if (!canvas) return;
    const dpr = window.devicePixelRatio || 1;
    canvas.width  = 190 * dpr;
    canvas.height = 190 * dpr;
    canvas.style.width  = '190px';
    canvas.style.height = '190px';
    if (moonClockFrame) cancelAnimationFrame(moonClockFrame);
    drawMoonClock();
}

// ── PDF Export ─────────────────────────────────────────────────────────────────
async function exportPDF() {
    toast('Preparing PDF…', 'inf');
    try {
        const { jsPDF } = window.jspdf;
        const main = document.querySelector('main.grid');
        const header = document.querySelector('header.header');

        // Capture header + main together
        const wrapper = document.createElement('div');
        wrapper.style.cssText = 'background:#080808;width:' + document.body.scrollWidth + 'px';
        document.body.appendChild(wrapper);
        wrapper.appendChild(header.cloneNode(true));
        wrapper.appendChild(main.cloneNode(true));

        const canvas = await html2canvas(wrapper, {
            backgroundColor: '#080808',
            scale: 1.5,
            useCORS: true,
            allowTaint: true,
            logging: false,
        });
        document.body.removeChild(wrapper);

        const imgData = canvas.toDataURL('image/png');
        const pdf     = new jsPDF({ orientation: 'landscape', unit: 'px', format: [canvas.width / 1.5, canvas.height / 1.5] });
        pdf.addImage(imgData, 'PNG', 0, 0, canvas.width / 1.5, canvas.height / 1.5);

        const dateStr = new Date().toLocaleDateString('en-CA');
        pdf.save(`Can-Oktay-Financials-${dateStr}.pdf`);
        toast('PDF downloaded', 'ok');
    } catch(e) {
        console.error(e);
        toast('Could not create PDF', 'err');
    }
}

// ── Init ──────────────────────────────────────────────────────────────────────
async function init() {
    load();

    // Ensure at least one portfolio exists
    if (S.portfolios.length === 0) {
        const defPort = { id: uid(), name: 'Main Portfolio', isMain: true, stocks: [], portSnaps: [] };
        S.portfolios  = [defPort];
        S.activePortId = defPort.id;
    }

    initForms();
    await yaSync({ quiet: false });
    render();
    renderPortfolioChart(portChartRange);
    renderCalendar();
    renderEconRegions();
    fetchEconCalendar();
    startMoonClock();

    fetchRates();
    startRatesRefresh();

    const allStocks = S.portfolios.flatMap(p => p.stocks);
    if (allStocks.length > 0 || S.watchlist.length > 0) refreshAllPrices();
    startAutoRefresh();
}

document.addEventListener('DOMContentLoaded', async () => {
    // Load from file (pywebview does not keep localStorage)
    try {
        const r = await fetch('/api/data');
        const data = await r.text();
        if (data && data !== 'null') {
            localStorage.setItem('fd3', data);
        }
    } catch {}
    init();
});
