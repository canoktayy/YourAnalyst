"""YA 1 – Chat: understand and route a message: POST /webhook/ya-chat {event_id, text} from the dashboard chat panel.

auth -> validate -> register event_id (replay => stored reply, no second write)
     -> pending yes/no handled without the LLM
     -> otherwise LLM parses the message to JSON -> Code validates it
     -> Switch intent -> sub-workflows / list / help / clarify
     -> request_log updated with status + response -> reply
"""
from lib import (WORDING_CHECK_JS, IS_TRUE, NOT_EMPTY, auth, call_workflow, chat_model, code, connect, connect_ai,
                 if_node, llm_chain, respond, sql, switch, webhook, write)

VALIDATE_MESSAGE = r"""const b = $('Trigger: chat message (POST /ya-chat)').first().json.body ?? {};
const event_id = String(b.event_id ?? '');
const text = String(b.text ?? '').trim();
const errors = [];
if (!/^[A-Za-z0-9_.:\-]{1,100}$/.test(event_id)) errors.push('event_id must be 1-100 chars of [A-Za-z0-9_.:-]');
if (!text || text.length > 1000) errors.push('text must be 1-1000 characters');
return [{ json: { valid: errors.length === 0, errors, event_id, text, user_id: $json.user_id, base_currency: $json.base_currency } }];
"""

REGISTER = """-- Idempotency: a replayed event_id hits the conflict, returns no request_id and the
-- previous status/response instead (same statement snapshot, so a new row is never "prior").
with j as (select $1::jsonb as j),
ins as (
    insert into request_log (user_id, event_id, channel, input_text, execution_id)
    select (j->>'user_id')::uuid, j->>'event_id', 'web', j->>'text', j->>'execution_id' from j
    on conflict (user_id, event_id) do nothing
    returning id)
select (select id from ins) as request_id, r.status as prior_status, r.response as prior_response
from j
left join request_log r on r.user_id = (j.j->>'user_id')::uuid and r.event_id = j.j->>'event_id';"""

REPLAY = r"""const m = $('Check message format').first().json;
const r = $json;
if (r.prior_response) return [{ json: { ...r.prior_response, replayed: true } }];
return [{ json: { ok: true, replayed: true, status: r.prior_status, intent: null, data: null,
  reply: 'This message is already being processed.' } }];
"""

LOAD_CONTEXT = """-- What the router needs besides the message: an open confirmation, the user's stocks
-- (summed per symbol, with the latest price), cash accounts and FX rates. Only symbol and
-- account NAMES are shown to the LLM; the numbers are used by code (sale proposal).
with u as (select ($1::jsonb->>'user_id')::uuid as id)
select (
    select row_to_json(p) from (
        select record_type, payload, expires_at from pending_confirmation, u
        where user_id = u.id and status = 'pending' and expires_at > now()
    ) p) as pending,
    coalesce((
        select json_agg(o order by o.symbol) from (
            select v.symbol, sum(v.quantity) as quantity, max(v.last_price) as price,
                   max(v.price_currency) as price_currency, max(v.price_status) as price_status
            from v_position v, u where v.user_id = u.id group by v.symbol) o), '[]'::json) as owned,
    coalesce((
        select json_agg(json_build_object('id', a.id, 'name', a.name, 'currency', a.currency, 'kind', a.kind)
                        order by a.created_at)
        from asset a, u where a.user_id = u.id), '[]'::json) as accounts,
    (select json_object_agg(currency, usd_per_unit) from fx_rate) as fx,
    -- Conversation memory: the last 6 finished exchanges (the current one is still 'received').
    coalesce((
        select json_agg(h order by h.created_at) from (
            select r.input_text, left(r.response->>'reply', 280) as reply, r.created_at
            from request_log r, u
            where r.user_id = u.id and r.status <> 'received' and r.input_text is not null
            order by r.created_at desc limit 6) h), '[]'::json) as history,
    (select row_to_json(d) from scan_draft d, u
     where d.user_id = u.id and d.updated_at > now() - interval '20 minutes') as scan_draft;"""

PRE_ROUTE = r"""// Deterministic routing for confirmation answers; everything else goes to the LLM.
const m = $('Check message format').first().json;
const request_id = $('DB: log message (dedup by event_id)').first().json.request_id;
const pending = $json.pending;
const t = m.text.toLowerCase().replace(/[.!?\s]+$/, '');
const YES = /^(yes|y|ok|okay|confirm|sure|save)$/;
const NO = /^(no|n|cancel|nope)$/;
const draft = $json.scan_draft ?? null;
let route = 'ai';
// yes/no answer a pending confirmation; with a scan being set up they go to the LLM instead
if (YES.test(t)) route = pending ? 'confirm' : draft ? 'ai' : 'no_pending';
else if (NO.test(t)) route = pending ? 'cancel' : draft ? 'ai' : 'no_pending';
// The orchestrator sees which stocks and accounts exist (names only), so "sell my
// Apple shares" maps to AAPL and "if Apple hits 1000" needs no follow-up question.
const owned = $json.owned ?? [], accounts = $json.accounts ?? [];
// Memory: recent exchanges and any scan being set up, so short answers ("S&P", "all of
// them", "10") are read as answers to the assistant's last question.
const history = $json.history ?? [];
const convo = history.map(h => `User: ${h.input_text}\nAssistant: ${String(h.reply ?? '').replace(/\s+/g, ' ')}`).join('\n');
const draftLine = draft
  ? `Scan being set up: mode=${draft.mode ?? '?'}, universe=${draft.universe ?? '?'}, symbol=${draft.symbol ?? '?'}, `
    + `indicators=${draft.indicators?.join('+') || '?'}, top=${draft.top ?? '?'}\n` : '';
const llm_input = (convo ? `Recent conversation (oldest first):\n${convo}\n\n` : '') + draftLine
  + `Owned stocks: ${owned.map(o => o.symbol).join(', ') || 'none'}\n`
  + `Accounts: ${accounts.map(a => a.name).join(', ') || 'none'}\n`
  + `Message: ${m.text}`;
return [{ json: { route, request_id, event_id: m.event_id, user_id: m.user_id, text: m.text, llm_input,
  pending, owned, accounts, fx: $json.fx ?? {}, history, scan_draft: draft } }];
"""

PARSE_SYSTEM = r"""You are the orchestrator of a personal-finance assistant. You read ONE chat message and choose the ONE tool that handles it, with its arguments, as JSON. You never answer the question and never calculate: tools do that. Reply with a single JSON object only: no prose, no code fences.

The input may start with the recent conversation and a scan being set up, then lists the user's owned stocks and account names (no amounts), then the new message. Use the conversation to understand short answers to the assistant's last question.

Schema:
{
  "intent": "record" | "sell" | "question" | "what_if" | "watchlist" | "list" | "scan" | "chat" | "help" | "clarify",
  "record": {
    "type": "liability" | "holding" | "asset",
    "counterparty": string | null,
    "name": string | null,
    "kind": "cash" | "other" | null,
    "symbol": string | null,
    "quantity": number | null,
    "price_per_share": number | null,
    "amount": number | null,
    "currency": string | null
  },
  "watchlist": { "action": "add" | "remove", "items": [string] },
  "list": { "currency": string | null },
  "scan": { "mode": "rank" | "single" | null, "universe": "demo" | "dow30" | "nasdaq100" | "sp500" | "nyse" | null, "symbol": string | null, "indicators": ["rsi" | "macd" | "ma_trend" | "volume"] | null, "all_indicators": boolean, "top": number | null, "cancel": boolean },
  "sell": { "symbol": string | null, "quantity": number | null, "all": boolean, "price": number | null, "account": string | null },
  "what_if": [ { "symbol": string, "price": number | null, "change_pct": number | null } ],
  "analysis": { "symbol": string | null, "indicators": ["rsi" | "macd" | "ma_trend" | "volume"], "top": number | null, "universe": "demo" | "dow30" | "nasdaq100" | "sp500" | "nyse" | null },
  "question": string | null
}
Include "record" only for intent "record", "sell" only for "sell", "what_if" only for "what_if", "watchlist" only for "watchlist", "list" only for "list", "scan" only for "scan", "question" only for "clarify".

Rules:
- Copy numbers exactly. NEVER invent an amount, currency, ticker, name or quantity. If something required is missing or vague, use intent "clarify" and ask for exactly what is missing. Always write the question in English.
- sell = the user sold (or wants to record selling) shares they own and the money goes to one of their accounts. symbol must be one of the owned stocks; "all"/"every"/"my X shares" -> all true and quantity null; price null means "at the latest price". account = the account name as listed. Several stocks at once -> clarify and ask for one stock at a time.
- scan = the Signal Lab tool: analysing one stock with indicators (mode "single", symbol) or ranking/screening a universe of stocks (mode "rank": "best stocks", "top 10 in the S&P", "scan the Dow"). Fill ONLY what the user said in this message, or what this message answers from the assistant's last scan question; leave everything else null (the app keeps earlier answers and asks for what is missing). Universes: Dow / Dow Jones -> "dow30", Nasdaq -> "nasdaq100", S&P / S&P 500 -> "sp500", NYSE / New York Stock Exchange -> "nyse", demo / Demo 20 -> "demo". Indicators: RSI -> rsi, MACD -> macd, trend / moving averages -> ma_trend, volume -> volume; "all", "all four", "every indicator" -> all_indicators true. "cancel" / "never mind" while a scan is being set up -> cancel true.
- question = a question about the user's OWN finances that needs their numbers: share of the portfolio, biggest/most profitable position, profit or loss, how much cash, what is left after paying debts. The tool reads the database; you only pick it.
- what_if = a hypothetical price ("if AAPL reaches 1000", "if PLTR drops 20%") and its effect on their money. price = the new price, or change_pct = the % change (negative for a drop).
- watchlist = following stocks without owning them ("add X to my watchlist", "watch X", "remove X from my watchlist"). Put every company or ticker the user named in "items", copied exactly as written (do NOT convert names to tickers; the app looks them up). Never turn a watchlist request into a record.
- record is for ONE item per message. If the message lists several debts/holdings/assets, use "clarify" and ask the user to send them one at a time.
- liability = money the user owes someone (needs counterparty, amount, currency).
- holding = shares the user owns (needs symbol, quantity, price_per_share; currency if stated).
- asset = cash in a bank/wallet (kind "cash") or another valued item like a car or gold (kind "other"); needs name, amount, currency.
- Company name -> ticker only when certain (Apple -> AAPL, Microsoft -> MSFT, Nvidia -> NVDA, Palantir -> PLTR, Datadog -> DDOG, Cloudflare -> NET). Otherwise clarify.
- Never fill a number the user did not write. No amount or price in the message -> leave it null.
- Indicators are only listed when the user names them; never fill in indicators the user did not choose.
- "show my debts / net worth / portfolio / assets" -> intent "list"; if a currency is asked for ("in TRY", "in dollars"), put it in list.currency.
- "what can you do" / "help" -> intent "help".
- chat = greetings, small talk, thanks, and general questions that do NOT need the user's own numbers (e.g. "how are you", "what is RSI?", "what does diversification mean?"). Questions about the user's own money are "question", never "chat".
- TL / ₺ -> TRY, € -> EUR, $ -> USD.

Examples:
I owe Benjamin EUR 1,000 -> {"intent":"record","record":{"type":"liability","counterparty":"Benjamin","amount":1000,"currency":"EUR"}}
I own 10 AAPL shares at USD 180 -> {"intent":"record","record":{"type":"holding","symbol":"AAPL","quantity":10,"price_per_share":180,"currency":"USD"}}
I have 25,000 TL in my Garanti account -> {"intent":"record","record":{"type":"asset","kind":"cash","name":"Garanti","amount":25000,"currency":"TRY"}}
Analyse AAPL using RSI and MACD -> {"intent":"scan","scan":{"mode":"single","symbol":"AAPL","indicators":["rsi","macd"],"all_indicators":false,"universe":null,"top":null,"cancel":false}}
Show me the best stocks -> {"intent":"scan","scan":{"mode":"rank","universe":null,"symbol":null,"indicators":null,"all_indicators":false,"top":null,"cancel":false}}
(assistant just asked which universe and indicators) Message: S&P, all of them -> {"intent":"scan","scan":{"mode":null,"universe":"sp500","symbol":null,"indicators":null,"all_indicators":true,"top":null,"cancel":false}}
(assistant just asked which indicators) Message: just RSI and volume -> {"intent":"scan","scan":{"mode":null,"universe":null,"symbol":null,"indicators":["rsi","volume"],"all_indicators":false,"top":null,"cancel":false}}
Top five in the Dow -> {"intent":"scan","scan":{"mode":"rank","universe":"dow30","symbol":null,"indicators":null,"all_indicators":false,"top":5,"cancel":false}}
Add Apple, nvidia and data dog to my watchlist -> {"intent":"watchlist","watchlist":{"action":"add","items":["Apple","nvidia","data dog"]}}
Stop watching TSLA -> {"intent":"watchlist","watchlist":{"action":"remove","items":["TSLA"]}}
I own some Apple shares -> {"intent":"clarify","question":"How many Apple shares do you own, and at what price per share?"}
Owned stocks: AAPL, PLTR | Message: Can you calculate my net worth if Apple reaches 1000 usd -> {"intent":"what_if","what_if":[{"symbol":"AAPL","price":1000,"change_pct":null}]}
Owned stocks: AAPL, PLTR | Message: what happens if palantir falls 20% -> {"intent":"what_if","what_if":[{"symbol":"PLTR","price":null,"change_pct":-20}]}
Owned stocks: AAPL, PLTR | Accounts: Revolut | Message: I sold all my AAPL at the current price, add it to Revolut -> {"intent":"sell","sell":{"symbol":"AAPL","quantity":null,"all":true,"price":null,"account":"Revolut"}}
Owned stocks: AAPL | Accounts: Revolut | Message: sold 5 AAPL at 350 into revolut -> {"intent":"sell","sell":{"symbol":"AAPL","quantity":5,"all":false,"price":350,"account":"Revolut"}}
Message: How much of my portfolio is PLTR? -> {"intent":"question"}
Message: Which of my stocks made the most money? -> {"intent":"question"}
I owe him some money -> {"intent":"clarify","question":"Who do you owe, and how much (with the currency)?"}
hi how are you -> {"intent":"chat"}
What does MACD mean? -> {"intent":"chat"}
What is my net worth? -> {"intent":"list"}
What is my net worth in TRY? -> {"intent":"list","list":{"currency":"TRY"}}"""

VALIDATE_AI = r"""// Validate the LLM's JSON. Anything missing or invalid becomes a clarifying question:
// the LLM proposes, this code decides what is allowed to reach the database.
const ctx = $('Build AI input, detect yes/no').first().json;
// Small models sometimes return several JSON objects back to back (one per item);
// split them at the top level instead of failing on the whole string.
const objects = [];
{
  const t = String($json.text ?? '').replace(/```(?:json)?/gi, '');
  let depth = 0, start = -1, inStr = false, esc = false;
  for (let i = 0; i < t.length; i++) {
    const ch = t[i];
    if (inStr) { if (esc) esc = false; else if (ch === '\\') esc = true; else if (ch === '"') inStr = false; continue; }
    if (ch === '"') inStr = true;
    else if (ch === '{') { if (depth++ === 0) start = i; }
    else if (ch === '}' && depth > 0 && --depth === 0) {
      try { objects.push(JSON.parse(t.slice(start, i + 1))); } catch {}
    }
  }
}
let p = objects[0] ?? null;

const out = (intent, extra = {}) => [{ json: { ...ctx, intent, ...extra } }];
const clarify = q => out('clarify', { question: q });

// Several requests in one message: a record needs its own confirmation, so ask for
// one request at a time instead of silently doing only the first.
const intents = objects.map(o => o?.intent).filter(i => i && i !== 'help' && i !== 'clarify');
const writes = intents.filter(i => i === 'record' || i === 'sell');
if (writes.length > 1) {
  return clarify('I can save one record at a time. Could you send them one by one? For example: "I have USD 500 in Wise".');
}
if (writes.length && new Set(intents).size > 1) {
  return clarify('That message asks for more than one thing. Could you send the record on its own, and the rest in a separate message?');
}

if ((!p || typeof p !== 'object') && ctx.scan_draft) p = { intent: 'help' };   // let the scan fallback below try
if (!p || typeof p !== 'object') {
  return out('clarify', { parse_error: $json.error ? String($json.error.message ?? $json.error) : 'invalid JSON from model',
    question: 'Sorry, I could not understand that. Try e.g. "I owe Benjamin EUR 1,000" or "Analyse AAPL using RSI and MACD".' });
}

const str = v => (typeof v === 'string' ? v.trim().slice(0, 80) : '');
const ALIAS = { TL: 'TRY', '₺': 'TRY', '€': 'EUR', '$': 'USD', '£': 'GBP' };
const cur = v => { const c = str(v).toUpperCase(); const k = ALIAS[c] ?? c; return /^[A-Z]{3}$/.test(k) ? k : null; };
const num = v => {
  if (typeof v === 'number') return Number.isFinite(v) ? v : null;
  let s = String(v ?? '').replace(/\s/g, '');
  if (/^\d{1,3}(\.\d{3})+(,\d+)?$/.test(s)) s = s.replace(/\./g, '').replace(',', '.');
  else if (/^\d{1,3}(,\d{3})+(\.\d+)?$/.test(s)) s = s.replace(/,/g, '');
  else s = s.replace(',', '.');
  const n = Number(s);
  return s !== '' && Number.isFinite(n) ? n : null;
};
// Grounding: every number in a record must appear in the user's own message, so a model
// that fills a gap with an invented value ("1 USD") is caught here, not saved.
// The user's own words: this message plus their recent messages (split conversations:
// "I sold AAPL" -> "how many?" -> "10").
const userWords = [ctx.text, ...(ctx.history ?? []).map(h => h.input_text ?? '')].join(' \n ');
const writtenNumbers = (() => {
  const out = new Set();
  for (const m of userWords.matchAll(/(\d[\d.,\s]*\d|\d)\s*(k|m|bn)?\b/gi)) {
    const raw = m[1].replace(/\s/g, '');
    const mult = { k: 1e3, m: 1e6, bn: 1e9 }[String(m[2] ?? '').toLowerCase()] ?? 1;
    const variants = [raw.replace(/,/g, ''), raw.replace(/\./g, '').replace(',', '.'), raw.replace(',', '.')];
    variants.map(Number).filter(Number.isFinite).forEach(n => out.add(Math.round(n * mult * 1e6) / 1e6));
  }
  // Each number on its own too, in case two numbers were joined by a space above.
  for (const m of userWords.matchAll(/\d+(?:[.,]\d+)*/g)) {
    [m[0].replace(/,/g, ''), m[0].replace(/\./g, '').replace(',', '.'), m[0].replace(',', '.')]
      .map(Number).filter(Number.isFinite).forEach(n => out.add(Math.round(n * 1e6) / 1e6));
  }
  return out;
})();
const grounded = n => n == null || writtenNumbers.has(Math.round(n * 1e6) / 1e6);
const TICKER = /^[A-Z][A-Z0-9.\-]{0,9}$/;
const ALL = ['rsi', 'macd', 'ma_trend', 'volume'];
const indicators = list => { const l = (Array.isArray(list) ? list : []).map(x => String(x).toLowerCase()).filter(x => ALL.includes(x)); return l.length ? [...new Set(l)] : ALL; };

// While a scan is being set up, a short answer ("NVDA", "S&P", "RSI and MACD", "all") is
// the answer to the assistant's question even if the model routed it elsewhere.
// Also: a request to analyse or rank stocks that the model answered with its own question
// ("which ratios?") or a help text goes to the scan tool, which asks with the app's options.
const SCAN_WORDS = /\b(analy[sz]e|analysis|rank|ranking|scan|screen|best stocks?|top \d+|indicators?|rsi|macd)\b/i;
const toolIntents = ['record', 'sell', 'watchlist', 'question', 'what_if', 'list', 'scan', 'analyse', 'rank'];
// The 7B model drifts between names ("analysis", "analyze_stock", "ranking"); map them to
// the tool. An intent that is still unknown but whose message asks for analysis goes to
// the scan fallback below instead of the help text.
const INTENT_ALIAS = { analysis: 'analyse', analyze: 'analyse', analyse_stock: 'analyse', analyze_stock: 'analyse',
  stock_analysis: 'analyse', technical_analysis: 'analyse', ranking: 'rank', rank_stocks: 'rank', screen: 'scan',
  screener: 'scan', stock_scan: 'scan', signal: 'scan', signal_lab: 'scan', net_worth: 'list', balance: 'list' };
if (typeof p.intent === 'string') p.intent = INTENT_ALIAS[p.intent.toLowerCase()] ?? p.intent.toLowerCase();
const KNOWN = [...toolIntents, 'chat', 'help', 'clarify'];
if (!KNOWN.includes(p.intent) && SCAN_WORDS.test(ctx.text)) p.intent = 'help';   // → the scan fallback
if ((ctx.scan_draft && !toolIntents.includes(p.intent))
    || (['clarify', 'help', 'chat'].includes(p.intent) && SCAN_WORDS.test(ctx.text))) {
  const t = ctx.text.trim();
  const words = t.toLowerCase();
  const slot = { mode: null, universe: null, symbol: null, indicators: null, all_indicators: false, top: null, cancel: false };
  const U = [[/\bdow\b/, 'dow30'], [/nasdaq/, 'nasdaq100'], [/s\s*&\s*p|sp\s*500|s and p/, 'sp500'], [/nyse|new york stock exchange/, 'nyse'], [/demo/, 'demo']];
  for (const [re, key] of U) if (re.test(words)) { slot.universe = key; break; }
  const I = [[/\brsi\b/, 'rsi'], [/\bmacd\b/, 'macd'], [/trend|moving average|\bsma\b/, 'ma_trend'], [/volume/, 'volume']];
  const found = I.filter(([re]) => re.test(words)).map(([, k]) => k);
  if (found.length) slot.indicators = found;
  if (/\ball\b|every|all four/.test(words)) slot.all_indicators = true;
  if (/^\$?[a-z]{1,5}([.-][a-z])?$/i.test(t) && !/^(all|yes|no|ok|rsi|macd|dow|nyse)$/i.test(t)) slot.symbol = t.replace(/^\$/, '').toUpperCase();
  if (/^(cancel|never ?mind|stop|forget it)\b/.test(words)) slot.cancel = true;
  if (/\b(rank|ranking|scan|screen|best stocks?|top \d+)\b/.test(words)) slot.mode = 'rank';
  else if (/\b(analy[sz]e|analysis)\b/.test(words) && !slot.universe) slot.mode = 'single';
  const tick = t.match(/\b[A-Z]{2,5}\b/g)?.find(w => !['RSI', 'MACD', 'NYSE', 'SMA', 'USD', 'EUR', 'TRY'].includes(w));
  if (!slot.symbol && slot.mode === 'single' && tick) slot.symbol = tick;
  if (slot.mode || slot.universe || slot.indicators || slot.all_indicators || slot.symbol || slot.cancel) p = { intent: 'scan', scan: slot };
}

switch (p.intent) {
  case 'record': {
    const r = p.record ?? {};
    const missing = [];
    let fields;
    if (r.type === 'liability') {
      fields = { counterparty: str(r.counterparty), amount: num(r.amount), currency: cur(r.currency), note: null };
      if (!fields.counterparty) missing.push('who you owe');
      if (!(fields.amount > 0)) missing.push('the amount');
      if (!fields.currency) missing.push('the currency');
    } else if (r.type === 'holding') {
      const symbol = str(r.symbol).toUpperCase();
      fields = { symbol, quantity: num(r.quantity), cost_per_share: num(r.price_per_share), cost_currency: cur(r.currency) ?? 'USD' };
      if (!TICKER.test(symbol)) missing.push('the ticker');
      if (!(fields.quantity > 0)) missing.push('the number of shares');
      if (!(fields.cost_per_share >= 0) || fields.cost_per_share === null) missing.push('the purchase price per share');
    } else if (r.type === 'asset') {
      fields = { kind: r.kind === 'other' ? 'other' : 'cash', name: str(r.name), amount: num(r.amount), currency: cur(r.currency) };
      if (!fields.name) missing.push('the account or asset name');
      if (!(fields.amount >= 0) || fields.amount === null) missing.push('the amount');
      if (!fields.currency) missing.push('the currency');
    } else {
      missing.push('the record type (debt, shares or asset)');
    }
    const LABEL = { amount: 'the amount', quantity: 'the number of shares', cost_per_share: 'the purchase price per share' };
    for (const k of Object.keys(LABEL)) {
      if (k in fields && fields[k] != null && !grounded(fields[k]) && !missing.includes(LABEL[k])) missing.push(LABEL[k]);
    }
    if (missing.length) return clarify(`Before I save this, could you tell me ${missing.join(', ')}?`);
    return out('record', { record: { type: r.type, fields } });
  }
  case 'scan':
  case 'analyse':
  case 'rank': {
    // Signal Lab in the chat. This message's details are merged into the scan being set up
    // (scan_draft); anything still missing is asked for; when complete the scan runs.
    const d = ctx.scan_draft ?? {};
    const n = { ...(p.scan ?? p.analysis ?? {}) };
    // Slot grounding: a detail counts only if THIS message says it (small models copy
    // indicators or universes from older turns). Otherwise the draft keeps or asks for it.
    const msg = ctx.text.toLowerCase();
    const IND_WORDS = { rsi: /\brsi\b/, macd: /\bmacd\b/, ma_trend: /trend|moving average|\bsma\b|\bma\b/, volume: /volume/ };
    // "all four (indicators)", "every indicator", or just "all" as an answer: the model may
    // list the four indicators instead of setting all_indicators, so either counts.
    const modelAll = n.all_indicators === true
      || (Array.isArray(n.indicators) && new Set(n.indicators.map(k => String(k).toLowerCase()).filter(k => ALL.includes(k))).size === ALL.length);
    const saysAll = /\b(all|every|each)\b[\w\s]{0,15}\b(indicators?|four|4|of them)\b|\bfour indicators\b|^\W*(all|everything)\W*$/.test(msg);
    n.all_indicators = saysAll || (modelAll && /\ball\b|every|four|each/.test(msg));
    if (Array.isArray(n.indicators)) n.indicators = n.indicators.filter(k => IND_WORDS[String(k).toLowerCase()]?.test(msg));
    // indicators named in the message count even if the model left them out
    const namedInds = Object.keys(IND_WORDS).filter(k => IND_WORDS[k].test(msg));
    if (namedInds.length) n.indicators = [...new Set([...(Array.isArray(n.indicators) ? n.indicators : []), ...namedInds])];
    const UNI_WORDS = { dow30: /\bdow\b/, nasdaq100: /nasdaq/, sp500: /s\s*&\s*p|sp\s*500|s and p|standard/, nyse: /nyse|new york/, demo: /demo/ };
    if (n.universe && !UNI_WORDS[n.universe]?.test(msg)) n.universe = null;
    if (n.top != null && !String(ctx.text).includes(String(Math.round(num(n.top) ?? -1)))) n.top = null;
    if (n.symbol) {
      const symTxt = String(n.symbol).toUpperCase().replace(/^\$/, '');
      const named = new RegExp(`\\b${symTxt.replace(/[.-]/g, '[.-]')}\\b`, 'i').test(ctx.text);
      // a company name ("analyse apple") is fine only in a message that is about one stock
      if (!named && !/\b(analy[sz]e|look at|check|stock|shares?)\b/.test(msg)) n.symbol = null;
    }
    // Mode, grounded the same way: "rank" needs ranking words or a universe in this message;
    // a ticker or "analyse" without them means one stock.
    const rankish = /\b(rank|ranking|scan|screen|best|top \d+|universe|stocks)\b/.test(msg) || !!n.universe;
    const singleish = /\b(analy[sz]e|analysis|look at|check)\b/.test(msg);
    if (n.mode === 'rank' && !rankish) n.mode = null;
    if (n.mode === 'single' && !(singleish || n.symbol)) n.mode = null;
    if (!n.mode && (n.symbol || singleish) && !rankish) n.mode = 'single';
    if (n.cancel === true) return out('scan_cancel');
    const UNIS = ['demo', 'dow30', 'nasdaq100', 'sp500', 'nyse'];
    const pick = l => { const x = (Array.isArray(l) ? l : []).map(v => String(v).toLowerCase()).filter(v => ALL.includes(v)); return x.length ? [...new Set(x)] : null; };
    const sym = str(n.symbol).toUpperCase().replace(/^\$/, '');
    const symbol = TICKER.test(sym) ? sym : (d.symbol ?? null);
    const universe = UNIS.includes(n.universe) ? n.universe : (d.universe ?? null);
    const inds = n.all_indicators === true ? ALL : (pick(n.indicators) ?? (d.indicators?.length ? d.indicators : null));
    const topN = num(n.top);
    const top = topN > 0 ? Math.min(20, Math.round(topN)) : (d.top ?? 5);
    let mode = n.mode === 'single' || n.mode === 'rank' ? n.mode
             : p.intent === 'analyse' ? 'single' : p.intent === 'rank' ? 'rank' : null;
    if (!mode && TICKER.test(sym)) mode = 'single';
    if (!mode && UNIS.includes(n.universe)) mode = 'rank';
    mode = mode ?? d.mode ?? null;
    const draft = { mode, universe, symbol, indicators: inds, top };
    const ask = [];
    if (!mode) ask.push('whether to rank a group of stocks (Demo 20, Dow 30, Nasdaq 100, S&P 500 or all NYSE stocks) or to analyse one stock');
    else if (mode === 'rank' && !universe) ask.push('which universe to scan: Demo 20, Dow 30, Nasdaq 100, S&P 500 or all NYSE stocks');
    else if (mode === 'single' && !symbol) ask.push('which stock (ticker) to analyse');
    if (!inds) ask.push('which indicators to use: RSI, MACD, Trend, Volume, or all four');
    if (ask.length) return out('scan_ask', { scan_draft: draft, question: `To run the scan I still need to know ${ask.join(', and ')}.` });
    return out('scan', { analysis: { mode, symbol: mode === 'single' ? symbol : null,
                                     universe: mode === 'rank' ? universe : null, indicators: inds, top } });
  }
  case 'watchlist': {
    const w = p.watchlist ?? {};
    const action = w.action === 'remove' ? 'remove' : 'add';
    // Items stay as the user wrote them; the sub-workflow resolves them on Yahoo.
    // Older-style "symbols" output is accepted too.
    const raw = [...(Array.isArray(w.items) ? w.items : []), ...(Array.isArray(w.symbols) ? w.symbols : [])];
    const items = [...new Set(raw.map(x => str(x).replace(/^\$/, '').slice(0, 40))
      .filter(x => x && !/^(unknown|none|null|n\/?a|tbd)$/i.test(x)))].slice(0, 20);
    if (!items.length) return clarify(`Which stocks should I ${action} ${action === 'add' ? 'to' : 'from'} your watchlist?`);
    return out('watchlist', { watchlist: { action, items } });
  }
  case 'question':
    // The answer sub-workflow reads the database and computes; nothing to validate here.
    return out('question', { question: { text: ctx.text, scenario: [] } });
  case 'what_if': {
    const list = (Array.isArray(p.what_if) ? p.what_if : p.what_if ? [p.what_if] : []).slice(0, 5);
    const scenario = [];
    for (const w of list) {
      const symbol = str(w?.symbol).toUpperCase().replace(/^\$/, '');
      const price = num(w?.price), change_pct = num(w?.change_pct);
      if (!TICKER.test(symbol)) continue;
      if (price != null && price > 0 && grounded(price)) scenario.push({ symbol, price, change_pct: null });
      else if (change_pct != null && grounded(Math.abs(change_pct))) scenario.push({ symbol, price: null, change_pct });
    }
    if (!scenario.length) return clarify('Which stock and which price (or % change) should I use? For example: "What if AAPL reaches 400 USD?"');
    return out('what_if', { question: { text: ctx.text, scenario } });
  }
  case 'sell': {
    // The code, not the LLM, works out quantity, price, proceeds and the FX conversion.
    const w = p.sell ?? {};
    const symbol = str(w.symbol).toUpperCase().replace(/^\$/, '');
    const pos = (ctx.owned ?? []).find(o => o.symbol === symbol);
    const ownedList = (ctx.owned ?? []).map(o => o.symbol).join(', ') || 'none';
    if (!pos) return clarify(symbol ? `You don't hold ${symbol}. Your stocks: ${ownedList}. Which one did you sell?`
                                    : `Which stock did you sell? Your stocks: ${ownedList}. One stock at a time, please.`);
    const held = Number(pos.quantity);
    let quantity = num(w.quantity);
    const all = w.all === true || quantity == null;
    if (all) quantity = held;
    else if (!grounded(quantity)) return clarify(`How many ${symbol} shares did you sell? You hold ${held}.`);
    if (!(quantity > 0) || quantity > held) return clarify(`You hold ${held} ${symbol}; how many did you sell?`);
    let price = num(w.price), price_source = 'given';
    if (price != null && !grounded(price)) price = null;
    if (price == null) {
      if (pos.price == null || pos.price_status === 'unavailable') return clarify(`I have no current price for ${symbol}. At what price per share did you sell?`);
      price = Number(pos.price); price_source = 'current';
    }
    const price_currency = pos.price_currency ?? 'USD';
    const accounts = ctx.accounts ?? [];
    const wanted = str(w.account).toLowerCase();
    const acc = wanted ? accounts.find(a => a.name.toLowerCase() === wanted)
                         ?? accounts.find(a => a.name.toLowerCase().includes(wanted) || wanted.includes(a.name.toLowerCase()))
                       : null;
    if (!acc) return clarify(`Which account should the money go to? ${accounts.length ? 'Your accounts: ' + accounts.map(a => a.name).join(', ') : 'Add an account first.'}`);
    const fx = ctx.fx ?? {};
    const round2 = n => Math.round(n * 100) / 100;
    const proceeds = round2(quantity * price);
    const rate = acc.currency === price_currency ? 1 : (fx[price_currency] && fx[acc.currency] ? fx[price_currency] / fx[acc.currency] : null);
    if (rate == null) return clarify(`I have no ${price_currency}→${acc.currency} exchange rate yet. Please try again after the next price refresh.`);
    return out('sell', { record: { type: 'sale', fields: {
      symbol, quantity, all: quantity === held, price, price_currency, price_source, proceeds,
      account_id: acc.id, account_name: acc.name, account_currency: acc.currency,
      proceeds_account: round2(proceeds * rate) } } });
  }
  case 'list':
    return out('list', { list_currency: cur(p.list?.currency) });
  case 'chat':
    return out('chat');
  case 'help':
    return out(p.intent);
  case 'clarify':
    return clarify(str(p.question) || 'Could you give me a bit more detail?');
  default:
    return out('help');
}
"""

TO_SUB = r"""// Shape the item for a sub-workflow (its trigger receives it as-is).
const c = $json;
const mode = { record: 'propose', sell: 'propose', analyse: 'single', rank: 'rank', watchlist: 'watchlist',
               question: 'question', what_if: 'what_if' }[c.intent] ?? c.route; // confirm | cancel
return [{ json: { mode,
  user_id: c.user_id, request_id: c.request_id, event_id: c.event_id,
  record: c.record, analysis: c.analysis, watchlist: c.watchlist, question: c.question } }];
"""

DIRECT_REPLY = r"""const c = $json;
if (c.route === 'no_pending') {
  return [{ json: { intent: 'confirm', status: 'done', data: null,
    reply: 'There is nothing waiting for confirmation.' } }];
}
if (c.intent === 'clarify') {
  return [{ json: { intent: 'clarify', status: 'done', data: c.parse_error ? { parse_error: c.parse_error } : null, reply: c.question } }];
}
return [{ json: { intent: 'help', status: 'done', data: null, reply: `I can help with:
• Records: "I owe Benjamin EUR 1,000", "I own 10 AAPL shares at USD 180", "I have USD 2,000 in Revolut"
• Watchlist: "Add Nvidia, Palantir and Cloudflare to my watchlist", "Remove TSLA from my watchlist"
• Questions: "How much of my portfolio is PLTR?", "What if AAPL reaches 400 USD?"
• Sales: "I sold all my AAPL at the latest price, add it to Revolut"
• Listing: "What is my net worth?"
• Analysis: "Analyse AAPL using RSI and MACD" (RSI, MACD, moving-average trend, volume)
• Ranking: "Top five in the demo universe"
I always ask before saving a record. This is a tracking and research tool, not investment advice.` } }];
"""

# Layer 2 for every tool result: the tools compute; this LLM tells the user in its own
# words. It sees the user's message and the tool's computed result (a plain-text draft
# with all figures). "Check reply numbers (fallback)" rejects any number that is not in that result.
NARRATE_SYSTEM = """You are YourAnalyst, the assistant of a personal-finance dashboard. The app has already done the work; you tell the user the result in your own words.
Rules:
- Use only facts and numbers that appear in RESULT. Copy numbers exactly as written there (same digits, same currency). Never calculate, round differently or add a number.
- Answer what the user asked first, naturally and warmly, like a person, not a report. Vary your wording; no fixed phrases.
- Keep the important figures (amounts, totals, prices, scores, percentages). For long lists, mention the most relevant items and the total.
- STATUS awaiting_confirmation: say exactly what you are about to save (with its numbers) and ask the user to confirm with yes or no. Nothing is saved yet.
- STATUS failed: explain briefly that it did not work and what they can do.
- If RESULT asks the user a question, ask it in your own words and keep exactly the options it lists; never suggest other options.
- Never ask the user to confirm or save anything unless STATUS is awaiting_confirmation.
- If RESULT says "not investment advice", keep that idea in one short phrase.
- Scores are points out of 100 under the stated rules, never a rank or position.
- For stock rankings, talk only about the stocks listed in RESULT; never claim anything about the other stocks of the universe ("the rest also score...").
- For stock rankings, describe each stock only with the indicator labels written next to it in RESULT (e.g. "RSI overbought (above 70)", "above SMA200 only"); never upgrade or soften them, and never leave out a negative one.
- Do not end with a question unless you need a confirmation or a missing detail. No "Is this what you wanted?"-style endings.
- English only, plain text, at most 90 words; a short list only if there are more than four items to name."""

NARRATE_INPUT = ("={{ 'USER MESSAGE: ' + $('Build AI input, detect yes/no').first().json.text + '\\nTOOL: ' + $json.intent + "
                 "'\\nSTATUS: ' + $json.status + '\\nRESULT:\\n' + $json.reply }}")

CHECK_NARRATION = WORDING_CHECK_JS + r"""// Keep the LLM's wording only if every number in it exists in the tool result (or the
// user's message). Otherwise, or if the model failed, fall back to the tool's own text.
const tool = $('Collect tool result').first().json;
// Tidy the model's wording: no markdown, and no trailing "Is this what you wanted?" when
// nothing needs answering (only confirmations and clarifications may end in a question).
let text = String($json.text ?? '').trim().replace(/\*\*(.+?)\*\*/g, '$1').replace(/^\s*[*-]\s+/gm, '• ');
const mayAsk = tool.status === 'awaiting_confirmation' || tool.intent === 'clarify' || /\?\s*$/.test(String(tool.reply ?? ''));
// A scan question must offer exactly the app's options (no "P/E ratio" or "dividend yield").
if (tool.data?.scan_draft) {
  const opts = ['RSI', 'MACD', 'Trend', 'Volume', 'Demo 20', 'Dow 30', 'Nasdaq 100', 'S&P 500', 'NYSE']
    .filter(o => String(tool.reply).includes(o));
  const offTopic = /earnings|dividend|p\/?e\b|price-to|ratio|fundamental|revenue|market cap/i.test(text);
  if (offTopic || !opts.every(o => text.toLowerCase().includes(o.toLowerCase()))) text = '';
}
// Nothing is waiting for a yes/no: drop invented "confirm / save this" sentences.
if (tool.status !== 'awaiting_confirmation') {
  text = text.split(/(?<=[.!?])\s+/).filter(sn => !/\b(confirm|save (this|the|it)|want to save)\b/i.test(sn)).join(' ').trim() || text;
}
if (!mayAsk) {
  for (let i = 0; i < 2 && /\?\s*$/.test(text); i++) {
    const cut = text.replace(/\s*[^.!?\n]*\?\s*$/, '').trim();
    if (!cut || cut === text) break;
    text = cut;
  }
}
const source = String(tool.reply ?? '') + ' ' + $('Build AI input, detect yes/no').first().json.text;
// Numbers with their currency: "107,065.00 USD" in the result may not become "107,065 EUR".
const MULT = { k: 1e3, thousand: 1e3, m: 1e6, million: 1e6, bn: 1e9, billion: 1e9 };
const SYM = { '€': 'EUR', '$': 'USD', '£': 'GBP', '₺': 'TRY' };
const NUM = /([€$£₺])?\s?(\d[\d,]*(?:\.\d+)?)(?:\s*(thousand|million|billion|bn|k|m)\b)?(?:\s*(EUR|USD|TRY|GBP)\b)?/gi;
const parse = m => {
  const raw = m[2], n = Number(raw.replace(/,/g, ''));
  const mult = MULT[String(m[3] ?? '').toLowerCase()] ?? 1;
  return { raw, n, mult, value: n * mult, cur: (m[4] ?? '').toUpperCase() || SYM[m[1]] || null,
           dec: raw.includes('.') ? raw.split('.')[1].length : 0 };
};
const allowed = [...source.matchAll(NUM)].map(parse);
const bad = [];
for (const m of text.matchAll(NUM)) {
  const t = parse(m);
  if (!Number.isFinite(t.n)) continue;
  if (!t.cur && t.mult === 1 && Number.isInteger(t.n) && (t.n <= 10 || (t.n >= 1990 && t.n <= 2100))) continue;   // small counts, years
  const tol = Math.max(0.0051, 0.5 * Math.pow(10, -t.dec)) * t.mult;
  const same = allowed.filter(a => Math.abs(a.value - t.value) <= tol);
  // A currency in the narration must match the result's currency for that number
  // (numbers the result writes without a currency accept any).
  const ok = same.length > 0 && (!t.cur || same.some(a => !a.cur || a.cur === t.cur));
  if (!ok) bad.push(m[0].trim());
}
// Help, chat and clarify answers run nothing: no "Let's analyze DDOG…" promise in their wording.
if (['help', 'chat', 'clarify'].includes(tool.intent) &&
    /\b(let'?s|let me|i('ll| will| am going to)|i'?m going to)\s+(analy[sz]e|scan|rank|check|run|look)/i.test(text))
  bad.push('promises an action that did not run');
// The narration may not claim an action the tool did not do (a new debt is not "paid off").
const ACTIONS = /\b(paid off|repaid|paid back|settled|cleared|no longer owe|removed|deleted|sold|cancel+ed)\b/gi;
for (const m of text.matchAll(ACTIONS)) {
  const stem = m[1].toLowerCase().slice(0, 4);
  if (!String(tool.reply ?? '').toLowerCase().includes(stem)) bad.push(`claims "${m[1]}" but the tool did not`);
}
// Scan rankings: the words used about each stock must match its computed contributions.
const ranked = tool.data?.analysis?.type === 'rank' ? (tool.data.analysis.ranked ?? []).slice(0, 10) : [];
bad.push(...wordingProblems(text, ranked));
const one = tool.data?.analysis?.type === 'single' ? tool.data.analysis : null;
if (one?.indicators) bad.push(...wordingProblems(text, [{ symbol: one.symbol, contributions:
  Object.fromEntries(Object.entries(one.indicators).map(([k, v]) => [k, v.available === false ? null : v.contribution])) }], true));
if (ranked.length && /\b(the rest|remaining|all (the )?other|others)\b[^.]{0,60}\b(score|scores|have|has|show|are)\b/i.test(text))
  bad.push('claims about stocks that are not in the result');
const ok = text.length > 0 && !$json.error && bad.length === 0;
return [{ json: { ...tool, reply: ok ? text : tool.reply,
  data: { ...(tool.data ?? {}), narration: { by_llm: ok, rejected_numbers: bad } } } }];
"""

SAVE_DRAFT = """with j as (select $1::jsonb as j)
insert into scan_draft (user_id, mode, universe, symbol, indicators, top, updated_at)
select (j->>'user_id')::uuid, j->'d'->>'mode', j->'d'->>'universe', j->'d'->>'symbol',
       case when jsonb_typeof(j->'d'->'indicators') = 'array'
            then array(select jsonb_array_elements_text(j->'d'->'indicators')) end,
       (j->'d'->>'top')::int, now()
from j
on conflict (user_id) do update set mode = excluded.mode, universe = excluded.universe, symbol = excluded.symbol,
    indicators = excluded.indicators, top = excluded.top, updated_at = now()
returning user_id;"""

DROP_DRAFT = """delete from scan_draft where user_id = ($1::jsonb->>'user_id')::uuid returning user_id;"""

PREPARE_SCAN = r"""const c = $('Check AI JSON against the message').first().json;
return [{ json: { mode: c.analysis.mode, user_id: c.user_id, request_id: c.request_id, event_id: c.event_id,
  analysis: c.analysis } }];
"""

ASK_SCAN = r"""const c = $('Check AI JSON against the message').first().json;
return [{ json: { intent: 'clarify', status: 'done', reply: c.question, data: { scan_draft: c.scan_draft } } }];
"""

SCAN_CANCELLED = r"""return [{ json: { intent: 'scan', status: 'done', reply: 'Okay, I dropped that scan. Nothing was run.', data: null } }];
"""

# Small talk and general questions. The model gets no user data here, so it is told not to
# state the user's figures and to point them to the questions that compute them.
CHAT_SYSTEM = """You are YourAnalyst, a friendly assistant inside a personal-finance dashboard.
Reply to the user's message naturally and briefly (at most 50 words), in English, warm but professional.
- Small talk: answer like a person would, then offer help in one short phrase.
- General finance or market concepts (RSI, MACD, diversification, net worth...): explain simply in 1-3 sentences.
- You cannot see the user's accounts here. Never state or guess their balances, holdings or prices; if they ask about their own money, tell them to ask directly, e.g. "What is my net worth?".
- No investment advice, no buy/sell recommendations. No lists, no headings."""

FORMAT_CHAT = r"""// Plain reply; if the model failed, a short friendly fallback.
const text = String($json.text ?? '').trim();
const reply = text && !$json.error ? text : "I'm here! Ask me about your accounts, stocks or a what-if, e.g. \"What is my net worth?\"";
return [{ json: { intent: 'chat', status: 'done', reply, data: null } }];
"""

LIST = """with j as (select ($1::jsonb->>'user_id')::uuid as user_id)
select (select row_to_json(n) from v_net_worth n, j where n.user_id = j.user_id) as totals,
       (select json_object_agg(currency, usd_per_unit) from fx_rate) as usd_per_unit,
       coalesce((select json_agg(li order by li.category, li.label)
                 from v_line_item li, j where li.user_id = j.user_id), '[]'::json) as items;"""

FORMAT_LIST = r"""const c = $('Check AI JSON against the message').first().json;
const money = (n, cur) => new Intl.NumberFormat('en-US',
  { minimumFractionDigits: 2, maximumFractionDigits: 2 }).format(Number(n)) + ' ' + cur;
const { totals, items, usd_per_unit: fx } = $json;
// Optional display currency ("net worth in TRY"): convert the base-currency totals.
const want = c.list_currency && fx?.[c.list_currency] && fx?.[totals.base_currency] ? c.list_currency : null;
const conv = n => want ? Number(n) * fx[totals.base_currency] / fx[want] : Number(n);
const show = want ?? totals.base_currency;
const NAMES = { cash: 'Cash', other: 'Other asset', stock: 'Stock', liability: 'Debt' };
const lines = items.length
  ? items.map(i => `• ${NAMES[i.category] ?? i.category}: ${i.label} — ${i.unpriced ? 'no price' : money(i.amount, i.currency)}`
      + (i.currency !== i.base_currency && !i.unpriced ? ` (≈ ${money(Math.abs(i.base_amount), i.base_currency)})` : ''))
  : ['No records yet.'];
lines.push('', `Assets ${money(conv(totals.total_assets), show)} − Liabilities ${money(conv(totals.total_liabilities), show)} = Net worth ${money(conv(totals.net_worth), show)}`);
if (c.list_currency && !want) lines.push(`(No exchange rate for ${c.list_currency}; shown in ${totals.base_currency}.)`);
return [{ json: { intent: 'list', status: 'done', reply: lines.join('\n'), data: { totals, items } } }];
"""

FINISH = """update request_log set
    status = $1::jsonb->>'status',
    intent = $1::jsonb->>'intent',
    response = $1::jsonb->'response',
    updated_at = now()
where id = ($1::jsonb->>'request_id')::uuid
returning response;"""

FINISH_PARAM = ("={{ JSON.stringify({ request_id: $('Build AI input, detect yes/no').first().json.request_id, status: $json.status, "
                "intent: $json.intent, response: { ok: true, event_id: $('Build AI input, detect yes/no').first().json.event_id, "
                "intent: $json.intent, status: $json.status, reply: $json.reply, data: $json.data ?? null } }) }}")

nodes = [
    webhook("Trigger: chat message (POST /ya-chat)", [0, 300], "POST", "ya-chat"),
    auth("DB: find user by token", [220, 300]),
    if_node("Token valid?", [440, 300], "={{ $json.user_id }}", NOT_EMPTY),
    respond("Reply 401: invalid token", [660, 480], '={{ { "ok": false, "error": "Missing or invalid X-YA-Token" } }}', 401),
    code("Check message format", [660, 280], VALIDATE_MESSAGE),
    if_node("Message format OK?", [880, 280], "={{ $json.valid }}", IS_TRUE),
    respond("Reply 400: invalid message", [1100, 480], '={{ { "ok": false, "error": "Invalid message", "details": $json.errors } }}', 400),
    sql("DB: log message (dedup by event_id)", [1100, 260], REGISTER,
        "={{ JSON.stringify({ ...$json, execution_id: $execution.id }) }}"),
    if_node("New message, not a duplicate?", [1320, 260], "={{ $json.request_id }}", NOT_EMPTY),
    code("Get stored reply of duplicate", [1540, 460], REPLAY),
    respond("Reply: stored answer again", [1760, 460], "={{ $json }}"),

    sql("DB: load chat memory and context", [1540, 240], LOAD_CONTEXT,
        "={{ JSON.stringify({ user_id: $('Check message format').first().json.user_id }) }}"),
    code("Build AI input, detect yes/no", [1760, 240], PRE_ROUTE),
    switch("Route: yes / no / new request", [1980, 240], "={{ $json.route }}",
           [("ai", "ai"), ("confirm", "confirm"), ("cancel", "cancel"), ("no_pending", "no_pending")]),

    llm_chain("AI 1: choose the tool (JSON)", [2220, 40], PARSE_SYSTEM, "={{ $json.llm_input }}",
              onError="continueRegularOutput", alwaysOutputData=True),
    chat_model("LLM for AI 1", [2220, 260]),
    code("Check AI JSON against the message", [2460, 40], VALIDATE_AI),
    switch("Route by intent", [2700, 40], "={{ $json.intent }}",
           [("record", "record"), ("analyse", "analyse"), ("rank", "rank"),
            ("list", "list"), ("help", "help"), ("clarify", "clarify"), ("watchlist", "watchlist"),
            ("sell", "sell"), ("question", "question"), ("what_if", "what_if"), ("chat", "chat"),
            ("scan", "scan"), ("scan_ask", "scan_ask"), ("scan_cancel", "scan_cancel")]),

    code("Prepare record input", [2960, -160], TO_SUB),
    call_workflow("Run sub: save financial record", [3200, -160], "YArecordEntry001"),
    code("Prepare analysis input", [2960, 40], TO_SUB),
    call_workflow("Run sub: analyse stocks", [3200, 40], "YAanalyseStock01"),
    sql("DB: list accounts, debts, stocks", [2960, 220], LIST, "={{ JSON.stringify({ user_id: $json.user_id }) }}"),
    code("Format the list", [3200, 220], FORMAT_LIST),
    code("Reply text for help / clarify", [3200, 400], DIRECT_REPLY),
    llm_chain("AI: small-talk reply", [2960, 920], CHAT_SYSTEM, "={{ $('Build AI input, detect yes/no').first().json.text }}",
              onError="continueRegularOutput", alwaysOutputData=True),
    chat_model("LLM for small talk", [2960, 1120], temperature=0.6),
    code("Format small-talk reply", [3200, 920], FORMAT_CHAT),
    code("Prepare watchlist input", [2960, 560], TO_SUB),
    code("Prepare question input", [2960, 740], TO_SUB),
    call_workflow("Run sub: answer question", [3200, 740], "YAanswerQuest001"),
    call_workflow("Run sub: update watchlist", [3200, 560], "YAwatchlist00001"),
    code("Prepare yes/no for pending record", [2220, 440], TO_SUB),

    # Signal Lab in the chat (scan): complete -> run; incomplete -> save the draft and ask.
    sql("DB: clear finished scan draft", [2960, 1300], DROP_DRAFT, "={{ JSON.stringify({ user_id: $json.user_id }) }}",
        alwaysOutputData=True),
    code("Prepare scan input", [3200, 1300], PREPARE_SCAN),
    sql("DB: save unfinished scan", [2960, 1480], SAVE_DRAFT,
        "={{ JSON.stringify({ user_id: $json.user_id, d: $json.scan_draft }) }}"),
    code("Ask what the scan still needs", [3200, 1480], ASK_SCAN),
    sql("DB: delete cancelled scan", [2960, 1660], DROP_DRAFT, "={{ JSON.stringify({ user_id: $json.user_id }) }}",
        alwaysOutputData=True),
    code("Reply: scan cancelled", [3200, 1660], SCAN_CANCELLED),

    code("Collect tool result", [3460, 160], "return $input.all();   // every tool result passes through here"),
    llm_chain("AI 2: write the reply", [3680, 160], NARRATE_SYSTEM, NARRATE_INPUT,
              onError="continueRegularOutput", alwaysOutputData=True),
    chat_model("LLM for AI 2", [3680, 380], temperature=0.4),
    code("Check reply numbers (fallback)", [3900, 160], CHECK_NARRATION),
    sql("DB: save reply, mark done", [4120, 160], FINISH, FINISH_PARAM),
    respond("Reply to chat", [4340, 160], "={{ $json.response }}"),
]

connections = connect(
    ("Trigger: chat message (POST /ya-chat)", "DB: find user by token"),
    ("DB: find user by token", "Token valid?"),
    ("Token valid?", "Check message format", 0),
    ("Token valid?", "Reply 401: invalid token", 1),
    ("Check message format", "Message format OK?"),
    ("Message format OK?", "DB: log message (dedup by event_id)", 0),
    ("Message format OK?", "Reply 400: invalid message", 1),
    ("DB: log message (dedup by event_id)", "New message, not a duplicate?"),
    ("New message, not a duplicate?", "DB: load chat memory and context", 0),
    ("New message, not a duplicate?", "Get stored reply of duplicate", 1),
    ("Get stored reply of duplicate", "Reply: stored answer again"),
    ("DB: load chat memory and context", "Build AI input, detect yes/no"),
    ("Build AI input, detect yes/no", "Route: yes / no / new request"),
    ("Route: yes / no / new request", "AI 1: choose the tool (JSON)", 0),
    ("Route: yes / no / new request", "Prepare yes/no for pending record", 1),
    ("Route: yes / no / new request", "Prepare yes/no for pending record", 2),
    ("Route: yes / no / new request", "Reply text for help / clarify", 3),
    ("AI 1: choose the tool (JSON)", "Check AI JSON against the message"),
    ("Check AI JSON against the message", "Route by intent"),
    ("Route by intent", "Prepare record input", 0),
    ("Route by intent", "Prepare analysis input", 1),
    ("Route by intent", "Prepare analysis input", 2),
    ("Route by intent", "DB: list accounts, debts, stocks", 3),
    ("Route by intent", "Reply text for help / clarify", 4),
    ("Route by intent", "AI: small-talk reply", 10),
    ("Route by intent", "DB: clear finished scan draft", 11),
    ("DB: clear finished scan draft", "Prepare scan input"),
    ("Prepare scan input", "Run sub: analyse stocks"),
    ("Route by intent", "DB: save unfinished scan", 12),
    ("DB: save unfinished scan", "Ask what the scan still needs"),
    ("Ask what the scan still needs", "Collect tool result"),
    ("Route by intent", "DB: delete cancelled scan", 13),
    ("DB: delete cancelled scan", "Reply: scan cancelled"),
    ("Reply: scan cancelled", "Collect tool result"),
    ("AI: small-talk reply", "Format small-talk reply"),
    ("Format small-talk reply", "DB: save reply, mark done"),
    ("Collect tool result", "AI 2: write the reply"),
    ("AI 2: write the reply", "Check reply numbers (fallback)"),
    ("Check reply numbers (fallback)", "DB: save reply, mark done"),
    ("Route by intent", "Reply text for help / clarify", 5),
    ("Route by intent", "Prepare watchlist input", 6),
    ("Route by intent", "Prepare record input", 7),
    ("Route by intent", "Prepare question input", 8),
    ("Route by intent", "Prepare question input", 9),
    ("Prepare question input", "Run sub: answer question"),
    ("Run sub: answer question", "Collect tool result"),
    ("Prepare watchlist input", "Run sub: update watchlist"),
    ("Run sub: update watchlist", "Collect tool result"),
    ("Prepare record input", "Run sub: save financial record"),
    ("Prepare yes/no for pending record", "Run sub: save financial record"),
    ("Prepare analysis input", "Run sub: analyse stocks"),
    ("DB: list accounts, debts, stocks", "Format the list"),
    ("Run sub: save financial record", "Collect tool result"),
    ("Run sub: analyse stocks", "Collect tool result"),
    ("Format the list", "Collect tool result"),
    ("Reply text for help / clarify", "Collect tool result"),
    ("DB: save reply, mark done", "Reply to chat"),
)
connect_ai(connections, "LLM for AI 1", "AI 1: choose the tool (JSON)")
connect_ai(connections, "LLM for small talk", "AI: small-talk reply")
connect_ai(connections, "LLM for AI 2", "AI 2: write the reply")

write("chat_router", "YAchatRouter0001", "YA 1 – Chat: understand and route a message", nodes, connections)
