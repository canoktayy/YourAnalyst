'use strict';

// ── Hide amounts ──────────────────────────────────────────────────────────────
// One switch (the eye next to the logo) masks every money amount on the page:
// "€528,424.47" → "€••••••". It works on the rendered text itself, not on a list of
// CSS classes, so cards, heatmap, donut, chart tooltip, chat messages and anything added
// later are covered: while the mode is on, a MutationObserver masks new or updated text
// (the dashboard re-renders every 10 s); switching it off puts the original text back.
// Market data stays visible: watchlist, share prices, Signal Lab and the candle charts.
// Elements with [data-money] (amounts written without a currency, e.g. the header's
// "594,637" under "USD") are masked as a whole. The choice is remembered per browser.

const HIDE_KEY   = 'ya.hideAmounts';
const MASK       = '••••••';
const PUBLIC     = '#watchlist-list, .stk-price, #modal-candle, #signal-page, script, style, textarea, input';
const CUR        = '[€$£₺]|EUR|USD|TRY|GBP';
// "€528,424.47", "€574.9K", "₺500,000.00", "528,424.47 EUR" (a sentence's full stop stays)
const MONEY_RX   = /([€$£₺])\s?[+\-−]?\d[\d,]*(?:\.\d+)?(?:\s?[KMB])?(?![\w%])|[+\-−]?\d[\d,]*(?:\.\d+)?(?:\s?[KMB])?\s?(EUR|USD|TRY|GBP)\b/g;
const HAS_MONEY  = new RegExp(`(${CUR})`);

let amountsHidden = false;
let masked = new WeakMap();                 // Text node / element → { orig, shown }
const observer = new MutationObserver(onMutations);

const maskMoney = s => s.replace(MONEY_RX, (m, sym, code) => sym ? `${sym}${MASK}` : `${MASK} ${code}`);
const isPublic  = el => !el || !!el.closest(PUBLIC);

function maskTextNode(t) {
    const el = t.parentElement;
    if (isPublic(el)) return;
    const v = t.nodeValue, rec = masked.get(t);
    if (rec && v === rec.shown) return;                       // our own change
    const whole = el.closest('[data-money]');
    const m = whole ? (/\d/.test(v) ? v.replace(/[+\-−]?\d[\d,.\s]*[KMB]?/g, MASK) : v)
                    : (HAS_MONEY.test(v) ? maskMoney(v) : v);
    if (m === v) { masked.delete(t); return; }
    masked.set(t, { orig: v, shown: m });
    t.nodeValue = m;
}

function maskTitle(el) {
    const v = el.getAttribute('title');
    if (!v || isPublic(el)) return;
    const rec = masked.get(el);
    if (rec && v === rec.shown) return;
    const m = HAS_MONEY.test(v) ? maskMoney(v) : v;
    if (m === v) return;
    masked.set(el, { orig: v, shown: m });
    el.setAttribute('title', m);
}

function maskTree(root) {
    if (root.nodeType === Node.TEXT_NODE) return maskTextNode(root);
    if (root.nodeType !== Node.ELEMENT_NODE) return;
    if (root.hasAttribute('title')) maskTitle(root);
    root.querySelectorAll('[title]').forEach(maskTitle);
    const w = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
    for (let t = w.nextNode(); t; t = w.nextNode()) maskTextNode(t);
}

function restoreAll() {
    const w = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
    for (let t = w.nextNode(); t; t = w.nextNode()) {
        const rec = masked.get(t);
        if (rec && t.nodeValue === rec.shown) t.nodeValue = rec.orig;
    }
    document.querySelectorAll('[title]').forEach(el => {
        const rec = masked.get(el);
        if (rec && el.getAttribute('title') === rec.shown) el.setAttribute('title', rec.orig);
    });
    masked = new WeakMap();
}

function onMutations(list) {
    for (const m of list) {
        if (m.type === 'characterData') maskTextNode(m.target);
        else if (m.type === 'attributes') maskTitle(m.target);
        else m.addedNodes.forEach(maskTree);
    }
}

function setAmountsHidden(on) {
    amountsHidden = on;
    document.body.classList.toggle('amounts-hidden', on);
    const btn = document.getElementById('hide-amounts-btn');
    if (btn) {
        btn.setAttribute('aria-pressed', String(on));
        btn.title = on ? 'Show amounts' : 'Hide amounts';
        btn.setAttribute('aria-label', btn.title);
    }
    if (on) {
        maskTree(document.body);
        observer.observe(document.body, { childList: true, subtree: true, characterData: true,
                                          attributes: true, attributeFilter: ['title'] });
    } else {
        observer.disconnect();
        restoreAll();
    }
    try { localStorage.setItem(HIDE_KEY, on ? '1' : '0'); } catch {}
}

function toggleAmounts() { setAmountsHidden(!amountsHidden); }

// Restore the last choice as soon as the page exists (before the first data arrives).
(() => {
    let on = false;
    try { on = localStorage.getItem(HIDE_KEY) === '1'; } catch {}
    const start = () => setAmountsHidden(on);
    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start);
    else start();
})();
