'use strict';

// ── Portfolio heatmap (treemap) ───────────────────────────────────────────────
// Each tile is one stock of the active portfolio (rows summed per symbol): its AREA is
// the position value in the base currency, its COLOUR is today's change (diverging:
// red ← neutral grey → green, saturating at ±3%). Hover shows the details, click opens
// the candlestick chart (candles.js). The list view stays available for add/delete.

const HM_SAT = 3;                                  // % change at full colour
const HM_NEUTRAL = [44, 44, 50], HM_UP = [34, 122, 82], HM_DOWN = [150, 58, 58];
let _portView = 'heatmap';
try { _portView = localStorage.getItem('ya.portView') || 'heatmap'; } catch {}

function setPortView(view) {
    _portView = view === 'list' ? 'list' : 'heatmap';
    try { localStorage.setItem('ya.portView', _portView); } catch {}
    renderPortHeatmap();
}

// Tile amounts are compact ("€140.9K") so they stay readable inside small tiles; the
// tooltip shows the exact figure.
function hmMoney(v) {
    try {
        return new Intl.NumberFormat('en-US', { style: 'currency', currency: BASE, notation: 'compact', maximumFractionDigits: 1 }).format(v);
    } catch { return fmtBase(v); }
}

function hmColor(chg) {
    if (chg == null || !isFinite(chg)) return `rgb(${HM_NEUTRAL})`;
    const t = Math.min(Math.abs(chg) / HM_SAT, 1);
    const pole = chg >= 0 ? HM_UP : HM_DOWN;
    return `rgb(${HM_NEUTRAL.map((n, i) => Math.round(n + (pole[i] - n) * t))})`;
}

// Squarified treemap (Bruls, Huizing & van Wijk): rows of tiles whose aspect ratios stay
// close to 1, so small positions remain readable squares instead of thin slivers.
function squarify(items, x, y, w, h) {
    const total = items.reduce((s, it) => s + it.v, 0);
    const scale = (w * h) / total;
    const nodes = items.map(it => ({ ...it, a: it.v * scale }));
    const out = [];
    const worst = (row, side) => {
        const sum = row.reduce((s, n) => s + n.a, 0);
        const max = Math.max(...row.map(n => n.a)), min = Math.min(...row.map(n => n.a));
        return Math.max((side * side * max) / (sum * sum), (sum * sum) / (side * side * min));
    };
    let row = [];
    const layoutRow = () => {
        const sum = row.reduce((s, n) => s + n.a, 0);
        if (w >= h) {                       // column on the left
            const cw = sum / h; let cy = y;
            row.forEach(n => { const nh = n.a / cw; out.push({ ...n, x, y: cy, w: cw, h: nh }); cy += nh; });
            x += cw; w -= cw;
        } else {                            // row on top
            const rh = sum / w; let cx = x;
            row.forEach(n => { const nw = n.a / rh; out.push({ ...n, x: cx, y, w: nw, h: rh }); cx += nw; });
            y += rh; h -= rh;
        }
        row = [];
    };
    for (const n of nodes) {
        const side = Math.min(w, h);
        if (!row.length || worst([...row, n], side) <= worst(row, side)) row.push(n);
        else { layoutRow(); row.push(n); }
    }
    if (row.length) layoutRow();
    return out;
}

function portHeatmapItems() {
    const by = {};
    (activePort()?.stocks ?? []).forEach(st => {
        if (!(st.qty > 0) || st.price == null) return;
        const cur = st.priceCur || st.cur || 'USD';
        const value = st.qty * st.price;
        const cost = st.cost ? st.qty * st.cost : null;
        const it = by[st.sym] ??= { sym: st.sym, name: st.name || '', cur, qty: 0, price: st.price,
                                    chg: st.changePct, value: 0, cost: 0, hasCost: true, v: 0 };
        it.qty += st.qty; it.value += value; it.v += toBase(value, cur);
        if (cost == null) it.hasCost = false; else it.cost += cost;
    });
    return Object.values(by).sort((a, b) => b.v - a.v);
}

function renderPortHeatmap() {
    const box = document.getElementById('port-heatmap'), list = document.getElementById('stocks-list');
    if (!box || !list) return;
    document.querySelectorAll('.view-toggle .vt-btn').forEach(b => {
        b.classList.toggle('active', b.dataset.view === _portView);
        b.setAttribute('aria-selected', String(b.dataset.view === _portView));
    });
    const items = portHeatmapItems();
    const show = _portView === 'heatmap' && items.length > 0;
    box.hidden = !show;
    list.style.display = show ? 'none' : '';
    if (!show) return;

    const host = document.getElementById('hm-tiles');
    const W = host.clientWidth, H = host.clientHeight;
    if (!W || !H) { requestAnimationFrame(renderPortHeatmap); return; }
    const total = items.reduce((s, it) => s + it.v, 0);
    const GAP = 2;
    host.innerHTML = squarify(items, 0, 0, W, H).map(t => {
        const w = Math.max(0, t.w - GAP), h = Math.max(0, t.h - GAP);
        const big = w > 58 && h > 46, mid = w > 40 && h > 28;
        const fs = Math.max(11, Math.min(22, Math.sqrt(w * h) / 6.5));
        const chg = t.chg == null ? '—' : `${t.chg >= 0 ? '+' : ''}${t.chg.toFixed(2)}%`;
        return `<button class="hm-tile" style="left:${t.x}px;top:${t.y}px;width:${w}px;height:${h}px;background:${hmColor(t.chg)}"
                    data-sym="${esc(t.sym)}" onclick="openCandles('${esc(t.sym)}')" aria-label="${esc(t.sym)} ${chg}, ${(t.v / total * 100).toFixed(1)}% of portfolio">
            ${mid ? `<span class="hm-sym" style="font-size:${fs}px">${esc(t.sym)}</span>
                     <span class="hm-chg mono" style="font-size:${Math.max(9, fs * 0.62)}px">${chg}</span>` : ''}
            ${big ? `<span class="hm-val mono leg-amt" style="font-size:${Math.max(10.5, fs * 0.6)}px">${hmMoney(t.v)}</span>` : ''}
        </button>`;
    }).join('');

    // Tooltip follows the pointer inside the box.
    const tip = document.getElementById('hm-tip');
    const bySym = Object.fromEntries(items.map(it => [it.sym, it]));
    host.querySelectorAll('.hm-tile').forEach(el => {
        el.onmousemove = e => {
            const it = bySym[el.dataset.sym];
            const pl = it.hasCost ? it.value - it.cost : null;
            const money = (n, c) => new Intl.NumberFormat('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 }).format(n) + ' ' + c;
            tip.innerHTML = `<b>${esc(it.sym)}</b> <span class="hm-tip-name">${esc(it.name)}</span>
                <div><span>Today</span><span class="${(it.chg ?? 0) >= 0 ? 'up' : 'down'}">${it.chg == null ? '—' : (it.chg >= 0 ? '+' : '') + it.chg.toFixed(2) + '%'}</span></div>
                <div><span>Position</span><span>${it.qty} × ${money(it.price, it.cur)}</span></div>
                <div><span>Value</span><span class="leg-amt">${fmtBase(it.v)} · ${(it.v / total * 100).toFixed(1)}%</span></div>
                ${pl != null ? `<div><span>Total P/L</span><span class="${pl >= 0 ? 'up' : 'down'}">${pl >= 0 ? '+' : ''}${money(pl, it.cur)} (${(pl / it.cost * 100).toFixed(1)}%)</span></div>` : ''}`;
            tip.style.display = 'block';
            const r = box.getBoundingClientRect();
            let lx = e.clientX - r.left + 14, ly = e.clientY - r.top + 14;
            if (lx + tip.offsetWidth > r.width) lx = e.clientX - r.left - tip.offsetWidth - 14;
            if (ly + tip.offsetHeight > r.height) ly = e.clientY - r.top - tip.offsetHeight - 14;
            tip.style.left = Math.max(0, lx) + 'px'; tip.style.top = Math.max(0, ly) + 'px';
        };
        el.onmouseleave = () => { tip.style.display = 'none'; };
    });
}

document.addEventListener('DOMContentLoaded', () => {
    const host = document.getElementById('hm-tiles');
    if (host) new ResizeObserver(() => renderPortHeatmap()).observe(host);
});
