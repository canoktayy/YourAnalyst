"""YourAnalyst 3 – Sub: analyse stocks with indicators (sub-workflow).

Input item: {mode: single|rank, user_id, request_id, event_id, analysis: {symbol, indicators, top}}
Output item: {reply, status: done|failed, intent, data}

Numbers come from the Python analysis API; the LLM only explains them. If the LLM
fails, the reply still carries the computed values. If the API is down after the
retries, the failure is logged to error_log and the user gets a readable message.
"""
from lib import RETRY, RULE_LABELS_JS, WORDING_CHECK_JS, chat_model, code, connect, connect_ai, llm_chain, node, sql, sub_trigger, switch, write

API = "http://analysis:8000"

def api_call(name, position, path, body_expr, timeout=60000):
    return node(name, "n8n-nodes-base.httpRequest", 4.2, position, {
        "method": "POST", "url": f"{API}{path}",
        "sendBody": True, "specifyBody": "json", "jsonBody": body_expr,
        "options": {"timeout": timeout},
    }, onError="continueErrorOutput", **RETRY)

NORMALISE = RULE_LABELS_JS + r"""// Compact, numbers-only facts for the LLM and the reply (no raw API noise).
const ctx = $('Input: one stock or a ranking').first().json;
const r = $json;
const ind = i => Object.fromEntries(Object.entries(i).map(([k, v]) =>
  [k, v.available ? { value: v.value, contribution: v.contribution, label: RULE_LABELS[k]?.[String(v.contribution)] ?? v.reason,
                      reason: v.reason, rule: v.rule }
                  : { available: false, reason: v.reason }]));
const facts = ctx.mode === 'rank'
  ? { type: 'rank', universe: ctx.analysis.universe ?? 'demo', universe_name: ({ demo: 'Demo 20', dow30: 'Dow 30', nasdaq100: 'Nasdaq 100', sp500: 'S&P 500', nyse: 'NYSE (all listed common stocks)' })[ctx.analysis.universe ?? 'demo'],
      universe_size: r.universe_size, indicators: r.indicators, data_as_of: r.data_as_of,
      note: r.note, failed: r.failed,
      ranked: r.ranked.map((x, i) => ({ rank: i + 1, symbol: x.symbol, name: x.name, score: x.score,
        last_price: x.last_price, currency: x.currency,
        contributions: Object.fromEntries(Object.entries(x.indicators).map(([k, v]) => [k, v.available ? v.contribution : null])),
        values: Object.fromEntries(Object.entries(x.indicators).map(([k, v]) => [k, v.available ? v.value : null])),
        reasons: Object.fromEntries(Object.entries(x.indicators).map(([k, v]) => [k, v.reason])),
        labels: Object.fromEntries(Object.entries(x.indicators).map(([k, v]) =>
          [k, v.available ? (RULE_LABELS[k]?.[String(v.contribution)] ?? v.reason) : 'not available'])) })) }
  : { type: 'single', symbol: r.symbol, name: r.name, last_price: r.last_price, currency: r.currency,
      data_as_of: r.data_as_of, source: r.source, score: r.score, score_rule: r.score_rule, indicators: ind(r.indicators) };
// The LLM explains the top 5 only (a full 20-row table would make the prompt long and slow);
// the reply/table still carries every ranked row.
const llm_facts = facts.type === 'rank'
  ? { ...facts, ranked: facts.ranked.slice(0, 5).map(({ values, last_price, currency, reasons, ...x }) => x) }
  // single: without the rule texts ("...>70 overbought -1"), whose words the 7B model copied
  : { ...facts, indicators: Object.fromEntries(Object.entries(facts.indicators).map(([k, { rule, ...v }]) => [k, v])) };
return [{ json: { ...ctx, facts, llm_facts } }];
"""

EXPLAIN_SYSTEM = (
    "=You explain stock-indicator results to a retail investor.\n"
    "Use ONLY the facts in the JSON you receive. Do not add prices, news, forecasts or any number that is not in it.\n"
    "Mention the data timestamp. For each indicator, give its value and what the rule concluded.\n"
    "Describe every indicator ONLY with its label ('label' for one stock, 'labels' per stock in a ranking), "
    "e.g. 'RSI below 50', 'above SMA200 only'; never upgrade or soften a label, never leave out a negative one, "
    "and in a ranking describe stocks one by one, not as a group.\n"
    "Present the score as a rule-based ranking inside the stated universe, not a prediction.\n"
    "End with one short sentence saying this is not investment advice.\n"
    "Write in English. At most 120 words, plain text, no headings."
)

REPLY = WORDING_CHECK_JS + r"""const ctx = $('Shape API result').first().json;
const f = ctx.facts;
const n = (v, d = 2) => v == null ? '—' : new Intl.NumberFormat('en-US',
  { minimumFractionDigits: d, maximumFractionDigits: d }).format(v);
const when = iso => new Date(iso).toISOString().slice(0, 16).replace('T', ' ') + ' UTC';
const sign = c => (c > 0 ? '+' : '') + c;
const LABEL = { rsi: 'RSI-14', macd: 'MACD', ma_trend: 'Trend (SMA50/200)', volume: 'Volume' };
const valueText = (k, v) => ({
  rsi: () => n(v.rsi_14),
  macd: () => `hist ${n(v.histogram, 4)} (${'prev'} ${n(v.histogram_prev, 4)})`,
  ma_trend: () => `${n(v.close)} / ${n(v.sma50)} / ${n(v.sma200)}`,
  volume: () => `${n(v.ratio)}× ${'20d avg'}`,
}[k] ?? (() => JSON.stringify(v)))();

const lines = [];
if (f.type === 'single') {
  lines.push(`${f.symbol}${f.name ? ` (${f.name})` : ''} · ${'Last price'} ${n(f.last_price)} ${f.currency} · ${'data'}: ${when(f.data_as_of)}`);
  for (const [k, v] of Object.entries(f.indicators)) {
    lines.push(v.available === false
      ? `• ${LABEL[k]}: ${'not available'} (${v.reason})`
      : `• ${LABEL[k]}: ${valueText(k, v.value)} → ${v.reason} (${sign(v.contribution)})`);
  }
  lines.push(`${'Score'}: ${f.score ?? '—'}/100`);
} else {
  const shown = f.ranked.slice(0, 10);
  lines.push(`Top ${shown.length} of ${f.universe_size} stocks in ${f.universe_name} (${f.indicators.join(', ')}) · data: ${when(f.data_as_of)}`);
  // Each contribution with the label of the rule that produced it (RULE_LABELS in Normalise),
  // so the narrator never has to guess what "+0.5" means.
  for (const x of shown) {
    const parts = Object.entries(x.contributions)
      .map(([k, c]) => c == null ? `${LABEL[k]} n/a` : `${x.labels[k]} (${sign(c)})`).join(', ');
    lines.push(`${x.rank}. ${x.symbol} — ${x.score}/100: ${parts}`);
  }
  if (f.failed?.length) lines.push(`Could not analyse ${f.failed.length}: ${f.failed.slice(0, 8).map(x => x.symbol).join(', ')}${f.failed.length > 8 ? '…' : ''}`);
}
// Number check, same rule as the chat narrator: every number in the LLM text must be in the facts.
let explanation = String($json.text ?? '').trim();
{
  const allowed = [...JSON.stringify(f).matchAll(/-?\d+(?:\.\d+)?(?:e-?\d+)?/g)].map(m => Number(m[0]));
  const bad = [...explanation.matchAll(/-?\d[\d,]*(?:\.\d+)?/g)].map(m => m[0]).filter(raw => {
    const n = Number(raw.replace(/,/g, '')), dec = raw.includes('.') ? raw.split('.')[1].length : 0;
    if (Number.isInteger(n) && (Math.abs(n) <= 10 || (n >= 1990 && n <= 2100))) return false;
    const tol = Math.max(0.0051, 0.5 * Math.pow(10, -dec));
    return !allowed.some(a => Math.abs(a - n) <= tol || Math.abs(Math.abs(a) - Math.abs(n)) <= tol);
  });
  if (f.type === 'rank') bad.push(...wordingProblems(explanation, f.ranked.slice(0, 10)));
  else bad.push(...wordingProblems(explanation, [{ symbol: f.symbol, contributions:
    Object.fromEntries(Object.entries(f.indicators).map(([k, v]) => [k, v.available === false ? null : v.contribution])) }], true));
  if (bad.length) explanation = '';
}
lines.push('', explanation || '(No explanation available right now; the values above are the computed results.)');

return [{ json: {
  intent: f.type === 'rank' ? 'rank' : 'analyse', status: 'done', reply: lines.join('\n'),
  data: { analysis: f, explanation: explanation || null },
  row: { user_id: ctx.user_id, request_id: ctx.request_id, kind: f.type, symbol: f.symbol ?? null,
         // big universes: keep the top 50 rows in the history table, not all 2,000
         indicators: ctx.analysis.indicators, metrics: f.type === 'rank' ? { ...f, ranked: f.ranked.slice(0, 50), failed: (f.failed ?? []).slice(0, 50) } : f,
         score: f.score ?? f.ranked?.[0]?.score ?? null,
         explanation: explanation || null, data_as_of: f.data_as_of },
} }];
"""

SAVE = """insert into analysis (user_id, request_id, kind, symbol, indicators, metrics, score, explanation, data_as_of)
select (r->>'user_id')::uuid, (r->>'request_id')::uuid, r->>'kind', r->>'symbol',
       array(select jsonb_array_elements_text(r->'indicators')), r->'metrics',
       (r->>'score')::numeric, r->>'explanation', (r->>'data_as_of')::timestamptz
from (select $1::jsonb as r) x
returning id;"""

DESCRIBE_FAILURE = r"""// HTTP node error output, after its retries.
const ctx = $('Input: one stock or a ranking').first().json;
const e = $json.error ?? {};
const msg = String(e.message ?? e.description ?? JSON.stringify(e)).slice(0, 500);
const code = Number(e.httpCode ?? (msg.match(/^(\d{3})\b/) ?? [])[1]);
const sym = ctx.analysis?.symbol;
let reply;
if (code === 404) reply = `No price history found for ${sym}. Could you check the ticker?`;
else if (code === 400) reply = 'The analysis request was invalid: ' + msg;
else reply = 'The analysis service is unavailable right now (tried 3 times). Please try again shortly.';
return [{ json: {
  intent: ctx.mode === 'rank' ? 'rank' : 'analyse', status: code === 404 || code === 400 ? 'done' : 'failed',
  reply, data: { error: msg, http_code: code || null },
  log: { workflow_name: $workflow.name, node_name: ctx.mode === 'rank' ? 'API: score and rank stocks' : 'API: analyse one stock',
         execution_id: $execution.id, event_id: ctx.event_id, message: msg },
} }];
"""

LOG_FAILURE = """insert into error_log (workflow_name, node_name, execution_id, event_id, message)
select l->>'workflow_name', l->>'node_name', l->>'execution_id', l->>'event_id', l->>'message'
from (select $1::jsonb as l) x
returning id;"""

nodes = [
    sub_trigger("Input: one stock or a ranking", [0, 200], "YAanalyseStock01"),
    switch("Route: one stock or ranking", [220, 200], "={{ $json.mode }}", [("single", "single"), ("rank", "rank")]),
    api_call("API: analyse one stock", [460, 80], "/analyze",
             "={{ JSON.stringify({ symbol: $json.analysis.symbol, indicators: $json.analysis.indicators }) }}"),
    sql("DB: stocks in chosen universe", [460, 320],
        "select json_agg(symbol order by symbol) as symbols from universe_member\n"
        "where universe = coalesce(nullif($1::jsonb->>'universe', ''), 'demo');",
        "={{ JSON.stringify({ universe: $json.analysis.universe ?? 'demo' }) }}"),
    api_call("API: score and rank stocks", [680, 320], "/rank",
             "={{ JSON.stringify({ symbols: $json.symbols, indicators: $('Input: one stock or a ranking').first().json.analysis.indicators, "
             "top: $('Input: one stock or a ranking').first().json.analysis.top }) }}", timeout=900000),
    code("Shape API result", [920, 160], NORMALISE),
    llm_chain("AI: explain top results", [1140, 160], EXPLAIN_SYSTEM, "={{ JSON.stringify($json.llm_facts ?? $json.facts) }}",
              onError="continueRegularOutput", alwaysOutputData=True),
    chat_model("LLM for explanation", [1140, 380], temperature=0.2),
    code("Build analysis reply", [1380, 160], REPLY),
    sql("DB: save analysis", [1600, 160], SAVE, "={{ JSON.stringify($json.row) }}"),
    code("Return result to caller", [1820, 160],
         "const r = $('Build analysis reply').first().json;\n"
         "return [{ json: { intent: r.intent, status: r.status, reply: r.reply, data: { ...r.data, analysis_id: $json.id } } }];"),

    code("Describe API failure", [920, 520], DESCRIBE_FAILURE),
    sql("DB: log API failure", [1140, 520], LOG_FAILURE, "={{ JSON.stringify($json.log) }}"),
    code("Reply: analysis unavailable", [1380, 520],
         "const r = $('Describe API failure').first().json;\n"
         "return [{ json: { intent: r.intent, status: r.status, reply: r.reply, data: { ...r.data, error_log_id: $json.id } } }];"),
]

connections = connect(
    ("Input: one stock or a ranking", "Route: one stock or ranking"),
    ("Route: one stock or ranking", "API: analyse one stock", 0),
    ("Route: one stock or ranking", "DB: stocks in chosen universe", 1),
    ("DB: stocks in chosen universe", "API: score and rank stocks"),
    ("API: analyse one stock", "Shape API result", 0),
    ("API: analyse one stock", "Describe API failure", 1),
    ("API: score and rank stocks", "Shape API result", 0),
    ("API: score and rank stocks", "Describe API failure", 1),
    ("Shape API result", "AI: explain top results"),
    ("AI: explain top results", "Build analysis reply"),
    ("Build analysis reply", "DB: save analysis"),
    ("DB: save analysis", "Return result to caller"),
    ("Describe API failure", "DB: log API failure"),
    ("DB: log API failure", "Reply: analysis unavailable"),
)
connect_ai(connections, "LLM for explanation", "AI: explain top results")

write("analyse_stock", "YAanalyseStock01", "YourAnalyst 3 – Sub: analyse stocks with indicators", nodes, connections)
