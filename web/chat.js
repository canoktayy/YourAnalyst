'use strict';

// ── YourAnalyst assistant ─────────────────────────────────────────────────────
// Embedded in the dashboard (#ya-chat-slot); falls back to a floating drawer on pages
// without the slot. Messages go to /ya/chat (server.py → n8n webhook ya-chat). Every
// message gets an event_id; "Retry" resends the SAME id, so a retry after a timeout can never
// create a second record (n8n returns the stored reply instead).

const YA_REFRESH_INTENTS = new Set(['confirm', 'cancel', 'list', 'watchlist']);

function chatEl(tag, cls, text) {
    const el = document.createElement(tag);
    if (cls) el.className = cls;
    if (text != null) el.textContent = text;
    return el;
}

function chatScroll() {
    const log = document.getElementById('ya-chat-log');
    log.scrollTop = log.scrollHeight;
}

function toggleChat(open) {
    const panel = document.getElementById('ya-chat');
    const show = open ?? !panel.classList.contains('open');
    panel.classList.toggle('open', show);
    document.getElementById('ya-chat-toggle').style.display = show ? 'none' : '';
    if (show) document.getElementById('ya-chat-input').focus();
}

function newEventId() {
    return 'web-' + (crypto.randomUUID ? crypto.randomUUID() : Date.now().toString(36) + Math.random().toString(36).slice(2));
}

async function sendChat(text, eventId = newEventId(), bubble = null) {
    text = text.trim();
    if (!text) return;
    const log = document.getElementById('ya-chat-log');
    if (!bubble) log.appendChild(chatEl('div', 'ya-msg user', text));

    const reply = bubble ?? chatEl('div', 'ya-msg bot pending');
    reply.className = 'ya-msg bot pending';
    reply.innerHTML = '<span class="ya-typing" aria-label="Assistant is typing"><i></i><i></i><i></i></span>';
    if (!bubble) log.appendChild(reply);
    chatScroll();
    setChatBusy(true);

    try {
        const r = await fetch('/ya/chat', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ event_id: eventId, text }),
            signal: AbortSignal.timeout(180000),
        });
        const data = await r.json().catch(() => ({ ok: false, error: `HTTP ${r.status}` }));
        // n8n answers 500 "Error in workflow" when a run crashes; the Error Workflow has then
        // stored a readable reply for this event_id, so "Tekrar dene" (same id) shows it.
        if (r.status >= 500) throw new Error('a server-side error occurred, nothing was saved');
        if (!r.ok || !data.ok) throw new Error(data.error || data.message || `HTTP ${r.status}`);
        renderReply(reply, data);
        if (YA_REFRESH_INTENTS.has(data.intent) && typeof yaSync === 'function') yaSync();
    } catch (e) {
        reply.className = 'ya-msg bot failed';
        reply.textContent = `Could not reach the assistant: ${e.message}`;
        const actions = chatEl('div', 'ya-msg-actions');
        const retry = chatEl('button', '', 'Retry');
        retry.onclick = () => sendChat(text, eventId, reply);   // same event_id: safe to retry
        actions.appendChild(retry);
        reply.appendChild(actions);
    } finally {
        setChatBusy(false);
        chatScroll();
    }
}

function renderReply(bubble, data) {
    bubble.className = 'ya-msg bot' + (data.status === 'failed' ? ' failed' : '');
    bubble.textContent = data.reply;

    if (data.status === 'awaiting_confirmation') {
        const actions = chatEl('div', 'ya-msg-actions');
        const yes = chatEl('button', 'primary', 'Yes, save');
        const no = chatEl('button', '', 'No');
        yes.onclick = () => { yes.disabled = no.disabled = true; sendChat('yes'); };
        no.onclick = () => { yes.disabled = no.disabled = true; sendChat('no'); };
        actions.append(yes, no);
        bubble.appendChild(actions);
    }
    if (data.replayed) bubble.appendChild(chatEl('div', 'ya-msg-meta', 'resent: stored reply'));
}

// Called by app.js once the dashboard (with app_user.display_name) has loaded.
function setChatGreeting(name) {
    const el = document.getElementById('ya-chat-hello');
    const first = String(name ?? '').trim().split(/\s+/)[0];
    if (el) el.textContent = first ? `Hi ${first}, how can I help you today?` : 'Hi! How can I help you today?';
}

function setChatBusy(busy) {
    document.getElementById('ya-chat-send').disabled = busy;
    document.getElementById('ya-chat-box')?.classList.toggle('busy', busy);   // faster border while thinking
}

function initChat() {
    const slot = document.getElementById('ya-chat-slot');
    const head = slot
        ? `<span class="ya-chat-title">YourAnalyst</span>
           <span class="ya-chat-status"><span class="dot"></span>n8n</span>`
        : `<span class="ya-chat-title">YourAnalyst Assistant</span>
           <button class="ya-chat-close" onclick="toggleChat(false)" title="Close">✕</button>`;
    const html = `
        <aside class="ya-chat${slot ? ' embedded' : ''}" id="ya-chat" aria-label="YourAnalyst assistant">
            <div class="ya-chat-head">${head}</div>
            <div class="ya-chat-box" id="ya-chat-box">
            <div class="ya-chat-log" id="ya-chat-log"></div>
            <form class="ya-chat-form" id="ya-chat-form">
                <div class="ya-chat-field">
                    <input class="ya-chat-input" id="ya-chat-input" maxlength="1000" autocomplete="off"
                           placeholder="Ask anything about your money…" aria-label="Message the assistant">
                    <button class="ya-chat-send" id="ya-chat-send" type="submit" title="Send" aria-label="Send">
                        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><path d="M12 19V5"/><path d="M5 12l7-7 7 7"/></svg>
                    </button>
                </div>
            </form>
            </div>
        </aside>`;
    if (slot) {
        slot.innerHTML = html;
    } else {
        document.body.insertAdjacentHTML('beforeend',
            '<button class="ya-chat-toggle" id="ya-chat-toggle" onclick="toggleChat(true)"><span class="dot"></span>Assistant</button>' + html);
    }

    const log = document.getElementById('ya-chat-log');
    const hello = chatEl('div', 'ya-msg bot', 'Hi! How can I help you today?');
    hello.id = 'ya-chat-hello';
    log.appendChild(hello);

    document.getElementById('ya-chat-form').addEventListener('submit', e => {
        e.preventDefault();
        const input = document.getElementById('ya-chat-input');
        const text = input.value;
        input.value = '';
        sendChat(text);
    });
}

// ── Starlight canvas behind the conversation (Rolls-Royce headliner style) ──
// Hundreds of tiny stars of different size and brightness on black; some twinkle slowly
// and a faint shooting star crosses now and then. Static when the user prefers reduced
// motion; requestAnimationFrame already pauses in background tabs.
function initStarfield(box) {
    if (!box || box.querySelector('.ya-stars')) return;
    const canvas = document.createElement('canvas');
    canvas.className = 'ya-stars';
    canvas.setAttribute('aria-hidden', 'true');
    box.prepend(canvas);
    const ctx = canvas.getContext('2d');
    const still = window.matchMedia?.('(prefers-reduced-motion: reduce)').matches;
    let W = 0, H = 0, stars = [], shooter = null, nextShot = performance.now() + 6000;

    function seed() {
        const dpr = window.devicePixelRatio || 1;
        W = box.clientWidth; H = box.clientHeight;
        canvas.width = W * dpr; canvas.height = H * dpr;
        canvas.style.width = W + 'px'; canvas.style.height = H + 'px';
        ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
        const n = Math.round(W * H / 380);   // density
        stars = Array.from({ length: n }, () => {
            const big = Math.random() < 0.06;
            return {
                x: Math.random() * W, y: Math.random() * H,
                r: big ? 0.9 + Math.random() * 0.6 : 0.25 + Math.random() * 0.55,
                a: big ? 0.55 + Math.random() * 0.4 : 0.12 + Math.random() * 0.45,
                tw: Math.random() < 0.35 ? 0.4 + Math.random() * 1.2 : 0,   // twinkle speed
                ph: Math.random() * Math.PI * 2,
                warm: Math.random() < 0.15,   // a few slightly warm stars, like fibre-optic ends
            };
        });
    }

    function draw(t) {
        ctx.clearRect(0, 0, W, H);
        for (const s of stars) {
            const a = s.tw ? s.a * (0.55 + 0.45 * Math.sin(t / 1000 * s.tw + s.ph)) : s.a;
            ctx.fillStyle = s.warm ? `rgba(255,236,210,${a})` : `rgba(235,240,255,${a})`;
            ctx.beginPath(); ctx.arc(s.x, s.y, s.r, 0, Math.PI * 2); ctx.fill();
            if (s.r > 0.9) {   // soft halo on the brightest ones
                ctx.fillStyle = `rgba(235,240,255,${a * 0.12})`;
                ctx.beginPath(); ctx.arc(s.x, s.y, s.r * 3.2, 0, Math.PI * 2); ctx.fill();
            }
        }
        if (!still) {
            if (!shooter && t > nextShot) {
                shooter = { x: Math.random() * W * 0.7, y: Math.random() * H * 0.4, t0: t,
                            dx: 0.55 + Math.random() * 0.25, dy: 0.22 + Math.random() * 0.15 };
            }
            if (shooter) {
                const k = (t - shooter.t0) / 900;   // 0.9 s flight
                if (k >= 1) { shooter = null; nextShot = t + 9000 + Math.random() * 12000; }
                else {
                    const len = 70, hx = shooter.x + shooter.dx * k * W * 0.6, hy = shooter.y + shooter.dy * k * W * 0.6;
                    const g = ctx.createLinearGradient(hx, hy, hx - shooter.dx * len, hy - shooter.dy * len);
                    const alpha = Math.sin(Math.PI * k) * 0.55;
                    g.addColorStop(0, `rgba(255,255,255,${alpha})`); g.addColorStop(1, 'rgba(255,255,255,0)');
                    ctx.strokeStyle = g; ctx.lineWidth = 1;
                    ctx.beginPath(); ctx.moveTo(hx, hy); ctx.lineTo(hx - shooter.dx * len, hy - shooter.dy * len); ctx.stroke();
                }
            }
        }
    }

    seed();
    if (still) { draw(0); }
    else { const loop = t => { draw(t); requestAnimationFrame(loop); }; requestAnimationFrame(loop); }
    new ResizeObserver(() => { seed(); if (still) draw(0); }).observe(box);
}

document.addEventListener('DOMContentLoaded', () => {
    initChat();
    initStarfield(document.querySelector('.ya-chat.embedded .ya-chat-box'));
});
