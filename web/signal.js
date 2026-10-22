'use strict';

// ── Signal Lab ────────────────────────────────────────────────────────────────
// Pick any of the four indicators of the proposal (RSI, MACD, moving-average trend, volume
// ratio), choose a universe (the proposal's fixed Demo 20, or - an extension - Dow 30,
// Nasdaq 100, S&P 500, all NYSE common stocks) or one ticker, and get a ranking with
// per-indicator contributions, the data source and timestamp, and an AI summary that only
// explains the computed scores.
// Request path: /ya/scan -> n8n "YourAnalyst 6 – Signal Lab page: scan stocks" -> "YourAnalyst 3 – Sub: analyse stocks with indicators" -> Python API.

const SL_INDICATORS = [
    { key: 'rsi',      name: 'RSI',           what: 'Has the stock been sold too much, or bought too much?' },
    { key: 'macd',     name: 'MACD',          what: 'Is the short-term momentum turning up or down?' },
    { key: 'ma_trend', name: 'Trend',         what: 'Is the price above its 50- and 200-day averages?' },
    { key: 'volume',   name: 'Volume',        what: 'Is unusual trading volume backing today\'s move?' },
];
const SL_LABEL = Object.fromEntries(SL_INDICATORS.map(i => [i.key, i.name]));
const SL_UNIVERSES = [
    { key: 'demo',      name: 'Demo 20',    n: 20 },
    { key: 'dow30',     name: 'Dow 30',     n: 31 },
    { key: 'nasdaq100', name: 'Nasdaq 100', n: 101 },
    { key: 'sp500',     name: 'S&P 500',    n: 502 },
    { key: 'nyse',      name: 'NYSE',       n: 2183 },
];
const SL_PAGE = 100;

const _sl = { ready: false, mode: 'rank', universe: 'demo', selected: new Set(['rsi', 'macd', 'ma_trend', 'volume']),
              symbol: '', busy: false, open: null, result: null, shown: SL_PAGE, filter: '', timer: null };
try {
    const saved = JSON.parse(localStorage.getItem('ya.signalLab') ?? 'null');
    if (saved) {
        if (Array.isArray(saved.selected) && saved.selected.length) _sl.selected = new Set(saved.selected);
        if (saved.mode === 'single' || saved.mode === 'rank') _sl.mode = saved.mode;
        if (SL_UNIVERSES.some(u => u.key === saved.universe)) _sl.universe = saved.universe;
        _sl.symbol = saved.symbol ?? '';
    }
} catch {}
const slSave = () => { try { localStorage.setItem('ya.signalLab', JSON.stringify({ selected: [..._sl.selected], mode: _sl.mode, universe: _sl.universe, symbol: _sl.symbol })); } catch {} };

function initSignalLab() {
    if (_sl.ready) return;
    _sl.ready = true;
    const page = document.getElementById('signal-page');
    page.innerHTML = `
      <div class="sl-wrap">
        <header class="sl-head">
          <div>
            <h1 class="sl-title">Signal Lab</h1>
            <p class="sl-sub">Rule-based indicator scan · choose a universe or one stock · daily bars from Yahoo Finance</p>
          </div>
        </header>

        <section class="sl-card sl-controls" aria-label="Scan settings">
          <div class="sl-label">Indicators <span>choose one or more</span></div>
          <div class="sl-inds" id="sl-inds" role="group" aria-label="Indicators">
            ${SL_INDICATORS.map(i => `
              <button class="sl-ind" data-key="${i.key}" aria-pressed="false" onclick="slToggle('${i.key}')">
                <span class="sl-check" aria-hidden="true"></span>
                <span class="sl-ind-name">${i.name}</span>
                <span class="sl-ind-rule">${i.what}</span>
              </button>`).join('')}
          </div>
          <div class="sl-universe" id="sl-universe">
            <div class="sl-label">Universe <span>Demo 20 is the proposal's fixed universe; the others extend it</span></div>
            <div class="sl-seg sl-useg" role="tablist" aria-label="Universe">
              ${SL_UNIVERSES.map(u => `<button data-u="${u.key}" onclick="slUniverse('${u.key}')" role="tab">${u.name}<em>${u.n.toLocaleString('en-US')}</em></button>`).join('')}
            </div>
          </div>
          <div class="sl-actions">
            <div class="sl-seg" role="tablist" aria-label="Mode">
              <button data-mode="rank" onclick="slMode('rank')" role="tab">Scan universe</button>
              <button data-mode="single" onclick="slMode('single')" role="tab">Single stock</button>
            </div>
            <input class="sl-input" id="sl-symbol" placeholder="Ticker, e.g. AAPL" maxlength="10" autocomplete="off"
                   aria-label="Ticker" oninput="_sl.symbol=this.value.toUpperCase();this.value=_sl.symbol;slSave()"
                   onkeydown="if(event.key==='Enter') slRun()">
            <button class="sl-run" id="sl-run" onclick="slRun()">Run scan</button>
            <span class="sl-hint" id="sl-hint"></span>
          </div>
        </section>

        <details class="sl-card sl-guide" id="sl-guide">
          <summary><span>How scoring works</span><span class="sl-guide-hint">a 1-minute read</span></summary>
          <div class="sl-guide-body">
            <p>Each indicator you pick looks at the stock's daily prices and gives it a <b>vote</b>:</p>
            <div class="sl-votes">
              <span><span class="sl-chip p2">+1</span> good sign</span>
              <span><span class="sl-chip p1">+0.5</span> mildly good</span>
              <span><span class="sl-chip z">0</span> neutral</span>
              <span><span class="sl-chip n1">−1</span> warning sign</span>
            </div>
            <p>The <b>score</b> is the average vote on a 0–100 scale: <b>50 is neutral</b>, above 50 the signals lean positive,
               below 50 they lean negative. 100 means every indicator gave +1.</p>
            <div class="sl-guide-grid">
              <div><b>RSI</b><span>A stock that fell a lot recently (oversold) gets a positive vote; one that rose too fast (overbought) gets a warning.</span></div>
              <div><b>MACD</b><span>Positive when short-term momentum is above its average and still improving; negative when it is weak and getting weaker.</span></div>
              <div><b>Trend</b><span>Positive when today's price is above both the 50-day and the 200-day average price; negative when it is below both.</span></div>
              <div><b>Volume</b><span>Only votes on unusual days: heavy volume on a rising day is positive, on a falling day it is a warning.</span></div>
            </div>
            <p class="sl-muted">The ranking compares stocks inside the universe you chose. It describes the recent past and is not a forecast or advice.</p>
          </div>
        </details>

        <div id="sl-result" class="sl-result" aria-live="polite"></div>
      </div>`;
    document.getElementById('sl-symbol').value = _sl.symbol;
    slSync();
    slRenderEmpty();
}

function slSync() {
    document.querySelectorAll('#sl-inds .sl-ind').forEach(b => {
        const on = _sl.selected.has(b.dataset.key);
        b.classList.toggle('on', on);
        b.setAttribute('aria-pressed', String(on));
    });
    document.querySelectorAll('.sl-seg button').forEach(b => {
        const on = b.dataset.mode === _sl.mode;
        b.classList.toggle('on', on);
        b.setAttribute('aria-selected', String(on));
    });
    document.getElementById('sl-symbol').style.display = _sl.mode === 'single' ? '' : 'none';
    document.getElementById('sl-universe').style.display = _sl.mode === 'rank' ? '' : 'none';
    document.querySelectorAll('.sl-useg button').forEach(b => {
        const on = b.dataset.u === _sl.universe;
        b.classList.toggle('on', on);
        b.setAttribute('aria-selected', String(on));
    });
    const run = document.getElementById('sl-run');
    run.textContent = _sl.busy ? (_sl.mode === 'rank' ? 'Scanning…' : 'Analysing…') : (_sl.mode === 'rank' ? 'Run scan' : 'Analyse');
    run.disabled = _sl.busy || !_sl.selected.size;
    document.getElementById('sl-hint').textContent = !_sl.selected.size ? 'Select at least one indicator' : '';
}

function slToggle(key) {
    if (_sl.selected.has(key)) _sl.selected.delete(key); else _sl.selected.add(key);
    slSave(); slSync();
}
function slUniverse(key) { _sl.universe = key; slSave(); slSync(); }
function slMode(mode) {
    _sl.mode = mode; slSave(); slSync();
    if (mode === 'single') document.getElementById('sl-symbol').focus();
}

function slRenderEmpty() {
    document.getElementById('sl-result').innerHTML = `
      <div class="sl-empty">
        <p>Choose indicators and a universe, then run a scan.</p>
        <p class="sl-muted">"Top" means the highest scores under these rules inside the chosen universe, not a forecast.</p>
      </div>`;
}

async function slRun() {
    if (_sl.busy || !_sl.selected.size) return;
    if (_sl.mode === 'single' && !/^[A-Z][A-Z0-9.\-]{0,9}$/.test(_sl.symbol)) {
        document.getElementById('sl-hint').textContent = 'Enter a valid ticker, e.g. AAPL';
        document.getElementById('sl-symbol').focus();
        return;
    }
    _sl.busy = true; _sl.open = null; slSync();
    const out = document.getElementById('sl-result');
    out.innerHTML = slSkeleton();
    const t0 = performance.now();
    try {
        const r = await fetch('/ya/scan', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ mode: _sl.mode, universe: _sl.universe, indicators: [..._sl.selected], symbol: _sl.symbol }),
            signal: AbortSignal.timeout(900000),   // a full NYSE scan takes a few minutes
        });
        const d = await r.json().catch(() => ({ ok: false, error: `HTTP ${r.status}` }));
        if (!r.ok || !d.ok) throw new Error(d.error || d.reply || `HTTP ${r.status}`);
        const a = d.data?.analysis;
        if (!a) throw new Error(d.reply || 'No result');
        const secs = ((performance.now() - t0) / 1000).toFixed(1);
        if (a.type === 'rank') {
            _sl.result = { a, explanation: d.data.explanation, secs }; _sl.shown = SL_PAGE; _sl.filter = '';
            out.innerHTML = slRankHTML(a, d.data.explanation, secs);
            slRenderRows();
        } else {
            out.innerHTML = slSingleHTML(a, d.data.explanation, secs);
        }
    } catch (e) {
        out.innerHTML = `<div class="sl-card sl-error" role="alert"><b>Could not run the scan.</b> ${esc(e.message)}</div>`;
    } finally {
        clearInterval(_sl.timer);
        _sl.busy = false; slSync();
    }
}

function slSkeleton() {
    const rows = _sl.mode === 'rank' ? 8 : 4;
    const u = SL_UNIVERSES.find(x => x.key === _sl.universe);
    const est = _sl.mode === 'rank' ? Math.max(8, Math.round(u.n / 12 * 0.45 + 6)) : 8;   // ~12 parallel fetches
    const t0 = Date.now();
    clearInterval(_sl.timer);
    _sl.timer = setInterval(() => {
        const el = document.getElementById('sl-elapsed');
        if (el) el.textContent = `${Math.round((Date.now() - t0) / 1000)}s`;
    }, 1000);
    return `<div class="sl-card"><div class="sl-loading">
        <span class="sl-spin" aria-hidden="true"></span>
        ${_sl.mode === 'rank' ? `Scanning ${u.n.toLocaleString('en-US')} stocks in ${u.name} and computing scores…` : `Analysing ${esc(_sl.symbol)}…`}
        <span class="sl-elapsed"><b id="sl-elapsed">0s</b> · usually ~${est > 90 ? Math.round(est / 60) + ' min' : est + 's'}</span>
      </div>${Array.from({ length: rows }, () => '<div class="sl-skel"></div>').join('')}</div>`;
}

// ── formatting ──
const slNum = (v, d = 2) => v == null ? '—' : new Intl.NumberFormat('en-US', { minimumFractionDigits: d, maximumFractionDigits: d }).format(v);
const slWhen = iso => new Date(iso).toLocaleString('en-US', { day: 'numeric', month: 'short', year: 'numeric', hour: '2-digit', minute: '2-digit', hourCycle: 'h23', timeZoneName: 'short' });
function slContrib(c) {
    if (c == null) return '<span class="sl-chip na" title="Not enough data">n/a</span>';
    const cls = c >= 1 ? 'p2' : c > 0 ? 'p1' : c < 0 ? 'n1' : 'z';
    return `<span class="sl-chip ${cls}">${c > 0 ? '+' : c < 0 ? '−' : ''}${Math.abs(c)}</span>`;
}
function slValue(key, v) {
    if (!v) return '—';
    if (key === 'rsi') return slNum(v.rsi_14, 1);
    if (key === 'macd') return `${slNum(v.histogram, 2)} ${v.histogram > v.histogram_prev ? '↑' : '↓'}`;
    if (key === 'ma_trend') return v.close > v.sma50 && v.sma50 > v.sma200 ? 'Up' : v.close < v.sma50 && v.sma50 < v.sma200 ? 'Down' : v.close > v.sma200 ? 'Above 200' : 'Mixed';
    if (key === 'volume') return `${slNum(v.ratio, 2)}×`;
    return '—';
}
function slScoreBar(s) {
    const tone = s >= 65 ? 'hi' : s <= 35 ? 'lo' : 'mid';
    return `<div class="sl-score"><span class="sl-score-n mono">${s ?? '—'}</span>
        <span class="sl-score-track"><span class="${tone}" style="width:${Math.max(2, s ?? 0)}%"></span></span></div>`;
}
function slAI(text) {
    return text ? `<section class="sl-card sl-ai"><div class="sl-label">AI summary <span>explains the computed scores only</span></div><p>${esc(text)}</p></section>` : '';
}
function slMeta(a, secs) {
    return `<div class="sl-meta">
        <span><b>${a.type === 'rank' ? `${esc(a.universe_name ?? 'Demo 20')} · ${a.universe_size.toLocaleString('en-US')} stocks scanned` : esc(a.symbol)}</b></span>
        <span>${(a.indicators?.length ? a.indicators : Object.keys(a.indicators ?? {})).map(k => SL_LABEL[k] ?? k).join(' · ')}</span>
        <span>Data as of ${slWhen(a.data_as_of)}</span>
        <span>Yahoo Finance daily bars</span>
        <span>${secs}s</span>
      </div>`;
}

function slRankHTML(a, explanation, secs) {
    const keys = a.indicators;
    const head = `<tr><th>#</th><th>Stock</th><th class="r">Price</th><th>Score</th>${keys.map(k => `<th class="c">${SL_LABEL[k]}</th>`).join('')}<th></th></tr>`;
    const failed = a.failed?.length
        ? `<p class="sl-muted">No data for ${a.failed.length} symbol${a.failed.length > 1 ? 's' : ''}: ${a.failed.slice(0, 12).map(f => esc(f.symbol)).join(', ')}${a.failed.length > 12 ? '…' : ''}</p>` : '';
    return `${slMeta(a, secs)}${slAI(explanation)}
      <section class="sl-card sl-table-card">
        <div class="sl-table-tools">
          <span class="sl-muted" id="sl-count"></span>
          <input class="sl-input sl-filter" placeholder="Find in results…" aria-label="Find in results"
                 oninput="_sl.filter=this.value.trim().toUpperCase(); _sl.shown=${SL_PAGE}; slRenderRows()">
        </div>
        <table class="sl-table"><thead>${head}</thead><tbody id="sl-tbody"></tbody></table>
        <div class="sl-more" id="sl-more"></div>
        ${failed}
      </section>
      <p class="sl-foot">Ranking inside the chosen universe under the scoring rules; not a forecast. Not investment advice.</p>`;
}

// Large universes (S&P 500, NYSE) rank hundreds of rows: render 100 at a time, with a
// search over symbol and name.
function slRenderRows() {
    const { a } = _sl.result ?? {};
    if (!a) return;
    const keys = a.indicators;
    const f = _sl.filter;
    const rows = f ? a.ranked.filter(x => x.symbol.includes(f) || String(x.name ?? '').toUpperCase().includes(f)) : a.ranked;
    const page = rows.slice(0, _sl.shown);
    document.getElementById('sl-tbody').innerHTML = page.map(x => `
      <tr class="sl-row${x.rank <= 5 ? ' top' : ''}" onclick="slToggleRow('${esc(x.symbol)}')" tabindex="0"
          onkeydown="if(event.key==='Enter') slToggleRow('${esc(x.symbol)}')" aria-expanded="false" data-sym="${esc(x.symbol)}">
        <td class="mono sl-rank">${x.rank}${x.rank <= 5 ? '<span class="sl-top">Top 5</span>' : ''}</td>
        <td><b>${esc(x.symbol)}</b><span class="sl-name">${esc(x.name ?? '')}</span></td>
        <td class="r mono">${slNum(x.last_price)} <span class="sl-cur">${esc(x.currency ?? '')}</span></td>
        <td>${slScoreBar(x.score)}</td>
        ${keys.map(k => `<td class="c"><span class="sl-val mono">${slValue(k, x.values?.[k])}</span>${slContrib(x.contributions?.[k])}</td>`).join('')}
        <td class="r"><span class="sl-caret" aria-hidden="true">›</span></td>
      </tr>
      <tr class="sl-detail" data-for="${esc(x.symbol)}" hidden><td colspan="${5 + keys.length}">
        <div class="sl-why">${keys.map(k => `
            <div><span class="sl-why-k">${SL_LABEL[k]}</span>${slContrib(x.contributions?.[k])}
                 <span class="sl-why-r">${esc(x.reasons?.[k] ?? '')}</span></div>`).join('')}
            <button class="sl-chart-btn" onclick="event.stopPropagation(); openCandles('${esc(x.symbol)}')">Open chart</button>
        </div></td></tr>`).join('') || `<tr><td colspan="${5 + keys.length}" class="sl-muted" style="padding:18px 10px">No match for "${esc(f)}".</td></tr>`;
    document.getElementById('sl-count').textContent = f
        ? `${rows.length} of ${a.ranked.length} match` : `Showing ${page.length} of ${a.ranked.length.toLocaleString('en-US')}`;
    const left = rows.length - page.length;
    document.getElementById('sl-more').innerHTML = left > 0
        ? `<button class="sl-chart-btn sl-more-btn" onclick="_sl.shown += ${SL_PAGE}; slRenderRows()">Show ${Math.min(SL_PAGE, left)} more · ${left.toLocaleString('en-US')} left</button>` : '';
}

function slToggleRow(sym) {
    const row = document.querySelector(`.sl-row[data-sym="${CSS.escape(sym)}"]`);
    const det = document.querySelector(`.sl-detail[data-for="${CSS.escape(sym)}"]`);
    if (!row || !det) return;
    det.hidden = !det.hidden;
    row.classList.toggle('open', !det.hidden);
    row.setAttribute('aria-expanded', String(!det.hidden));
}

function slSingleHTML(a, explanation, secs) {
    const inds = Object.entries(a.indicators ?? {});
    const detail = (k, v) => {
        if (v.available === false) return `<span class="sl-muted">${esc(v.reason)}</span>`;
        const x = v.value ?? {};
        if (k === 'rsi') return `RSI-14 <b class="mono">${slNum(x.rsi_14, 1)}</b>`;
        if (k === 'macd') return `Histogram <b class="mono">${slNum(x.histogram, 3)}</b> (prev ${slNum(x.histogram_prev, 3)}) · MACD ${slNum(x.macd, 2)} / signal ${slNum(x.signal, 2)}`;
        if (k === 'ma_trend') return `Close <b class="mono">${slNum(x.close)}</b> · SMA50 ${slNum(x.sma50)} · SMA200 ${slNum(x.sma200)}`;
        if (k === 'volume') return `<b class="mono">${slNum(x.ratio, 2)}×</b> the 20-day average (${new Intl.NumberFormat('en-US').format(x.volume)} vs ${new Intl.NumberFormat('en-US').format(x.avg20)}) on a ${x.up_day ? 'up' : 'down'} day`;
        return '';
    };
    return `${slMeta({ ...a, indicators: inds.map(([k]) => k) }, secs)}
      <section class="sl-card sl-single">
        <div class="sl-single-head">
          <div><div class="sl-sym">${esc(a.symbol)}</div><div class="sl-name">${esc(a.name ?? '')}</div></div>
          <div class="sl-px mono">${slNum(a.last_price)} <span class="sl-cur">${esc(a.currency ?? '')}</span></div>
          <div class="sl-single-score">${slScoreBar(a.score)}<span class="sl-muted">score / 100</span></div>
          <button class="sl-chart-btn" onclick="openCandles('${esc(a.symbol)}')">Open chart</button>
        </div>
        <div class="sl-ind-results">${inds.map(([k, v]) => `
          <div class="sl-ind-res">
            <div class="sl-ind-res-top"><span>${SL_LABEL[k]}</span>${slContrib(v.available === false ? null : v.contribution)}</div>
            <div class="sl-ind-res-val">${detail(k, v)}</div>
            ${v.available === false ? '' : `<div class="sl-why-r">${esc(v.reason)}</div>`}
          </div>`).join('')}</div>
      </section>
      ${slAI(explanation)}
      <p class="sl-foot">Rule-based score, not a forecast. Not investment advice.</p>`;
}
