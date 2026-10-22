"""YourAnalyst 5 – Sub: answer a question about my money (sub-workflow, called by the chat router).

Input item: {user_id, request_id, event_id, mode: question|what_if,
             question: {text, scenario: [{symbol, price|null, change_pct|null}]}}
Output item: {intent, status, reply, data}

Second layer of the two-layer design: the router's LLM only picked this tool. Here SQL
reads the user's numbers, code computes every figure (shares, weights, P/L, the what-if
scenario), the LLM only puts those figures into words, and a final check rejects any
number in the LLM's text that is not in the computed facts.
"""
from lib import chat_model, code, connect, connect_ai, llm_chain, sql, sub_trigger, write

SNAPSHOT = """-- Everything about the user's money in base currency, positions summed per symbol.
with u as (select id, base_currency from app_user where id = ($1::jsonb->>'user_id')::uuid),
fx as (select currency, usd_per_unit from fx_rate),
b as (select fx.usd_per_unit as base_usd from fx, u where fx.currency = u.base_currency),
pos as (
    select v.symbol, max(coalesce(v.name, v.symbol)) as name, sum(v.quantity) as quantity,
           max(v.last_price) as price, max(v.price_currency) as price_currency,
           max(v.price_status) as price_status, max(v.price_as_of) as price_as_of,
           sum(v.position_value) as value, sum(v.unrealised_pl) as unrealised_pl
    from v_position v, u where v.user_id = u.id group by v.symbol)
select
    (select base_currency from u) as base_currency,
    (select row_to_json(n) from v_net_worth n, u where n.user_id = u.id) as totals,
    coalesce((select json_agg(json_build_object(
        'symbol', p.symbol, 'name', p.name, 'quantity', p.quantity, 'price', p.price,
        'price_currency', p.price_currency, 'price_status', p.price_status, 'price_as_of', p.price_as_of,
        'value', p.value, 'unrealised_pl', p.unrealised_pl,
        'fx_to_base', f.usd_per_unit / b.base_usd) order by p.symbol)
      from pos p join fx f on f.currency = p.price_currency, b), '[]'::json) as positions,
    coalesce((select json_agg(json_build_object('name', label, 'kind', category, 'amount', amount,
                                                'currency', currency, 'base_amount', base_amount))
      from v_line_item li, u where li.user_id = u.id and li.category in ('cash', 'other')), '[]'::json) as accounts,
    coalesce((select json_agg(json_build_object('name', label, 'amount', amount,
                                                'currency', currency, 'base_amount', -base_amount))
      from v_line_item li, u where li.user_id = u.id and li.category = 'liability'), '[]'::json) as liabilities;"""

COMPUTE = r"""// Every number the answer may use is computed here; the LLM gets only these facts.
const start = $('Input: question').first().json;
const snap = $json;
const base = snap.base_currency;
const r2 = n => Math.round(Number(n) * 100) / 100;
const r1 = n => Math.round(Number(n) * 10) / 10;
const t = snap.totals ?? {};
const total_assets = Number(t.total_assets ?? 0);

const positions = snap.positions.map(p => {
  const value = Number(p.value ?? 0), pl = Number(p.unrealised_pl ?? 0), fx = Number(p.fx_to_base);
  const cost = value - pl;
  return {
    symbol: p.symbol, name: p.name, quantity: Number(p.quantity), price: r2(p.price), currency: p.price_currency,
    value: r2(value), value_base: r2(value * fx), cost: r2(cost),
    profit: r2(pl), profit_base: r2(pl * fx), profit_pct: cost > 0 ? r1(pl / cost * 100) : null,
    share_of_assets_pct: total_assets > 0 ? r1(value * fx / total_assets * 100) : null,
    price_status: p.price_status, _fx: fx,
  };
}).sort((a, b) => b.value_base - a.value_base);

const accounts = snap.accounts.map(a => ({ name: a.name, amount: r2(a.amount), currency: a.currency,
  amount_base: r2(a.base_amount), share_of_assets_pct: total_assets > 0 ? r1(a.base_amount / total_assets * 100) : null }));
const liabilities = snap.liabilities.map(l => ({ name: l.name, amount: r2(l.amount), currency: l.currency, amount_base: r2(l.base_amount) }));
const cash_base = r2(accounts.reduce((s, a) => s + a.amount_base, 0));
const stocks_base = r2(positions.reduce((s, p) => s + p.value_base, 0));

const facts = {
  base_currency: base,
  net_worth: r2(t.net_worth ?? 0), total_assets: r2(total_assets), total_liabilities: r2(t.total_liabilities ?? 0),
  cash_and_other_assets: cash_base, stocks_value: stocks_base,
  net_worth_if_all_debts_paid_from_cash: r2(cash_base - Number(t.total_liabilities ?? 0)),
  positions: positions.map(({ _fx, ...p }) => p), accounts, liabilities,
};
if (positions.length) {
  facts.largest_position = positions[0].symbol;
  const best = [...positions].sort((a, b) => b.profit_base - a.profit_base)[0];
  facts.most_profitable_position = best.symbol;
}

// What-if: change one or more prices, recompute only what depends on them.
let scenario = null;
if (start.mode === 'what_if') {
  let delta = 0;
  const changes = start.question.scenario.map(s => {
    const p = positions.find(x => x.symbol === s.symbol);
    if (!p) return { symbol: s.symbol, held: false, note: `${s.symbol} is not in the portfolio, so net worth does not change` };
    const new_price = s.price != null ? Number(s.price) : p.price * (1 + Number(s.change_pct) / 100);
    const new_value = p.quantity * new_price;
    const d = (new_value - p.value) * p._fx;
    delta += d;
    return { symbol: p.symbol, held: true, quantity: p.quantity, current_price: p.price, new_price: r2(new_price),
             currency: p.currency, change_pct: s.change_pct ?? r1((new_price / p.price - 1) * 100),
             current_value: p.value, new_value: r2(new_value), value_change_base: r2(d) };
  });
  const new_net = facts.net_worth + delta;
  scenario = { changes, net_worth_now: facts.net_worth, net_worth_after: r2(new_net), net_worth_change: r2(delta),
               net_worth_change_pct: facts.net_worth ? r1(delta / Math.abs(facts.net_worth) * 100) : null };
  facts.scenario = scenario;
}

// The LLM sees every amount as text with its currency ("107,065.00 USD"), so it cannot put a
// USD figure next to "EUR"; Check numbers knows the currency of every amount it may quote.
const fmt2 = n => new Intl.NumberFormat('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 }).format(n);
const amounts = [];   // [value, currency]
const lab = (n, cur) => {
  if (n == null || !Number.isFinite(Number(n))) return null;
  amounts.push([Number(n), cur]);
  return `${fmt2(n)} ${cur}`;
};
const llm_facts = {
  base_currency: base,
  net_worth: lab(facts.net_worth, base), total_assets: lab(facts.total_assets, base),
  total_liabilities: lab(facts.total_liabilities, base), cash_and_other_assets: lab(facts.cash_and_other_assets, base),
  stocks_value: lab(facts.stocks_value, base),
  net_worth_if_all_debts_paid_from_cash: lab(facts.net_worth_if_all_debts_paid_from_cash, base),
  positions: positions.map(p => ({ symbol: p.symbol, name: p.name, quantity: p.quantity,
    price: lab(p.price, p.currency), value: lab(p.value, p.currency), value_in_base: lab(p.value_base, base),
    cost: lab(p.cost, p.currency), profit: lab(p.profit, p.currency), profit_in_base: lab(p.profit_base, base),
    profit_pct: p.profit_pct, share_of_assets_pct: p.share_of_assets_pct, price_status: p.price_status })),
  accounts: accounts.map(a => ({ name: a.name, amount: lab(a.amount, a.currency), amount_in_base: lab(a.amount_base, base),
    share_of_assets_pct: a.share_of_assets_pct })),
  liabilities: liabilities.map(l => ({ name: l.name, amount: lab(l.amount, l.currency), amount_in_base: lab(l.amount_base, base) })),
  largest_position: facts.largest_position, most_profitable_position: facts.most_profitable_position,
};
if (scenario) llm_facts.scenario = {
  changes: scenario.changes.map(c => c.held ? { symbol: c.symbol, held: true, quantity: c.quantity,
    current_price: lab(c.current_price, c.currency), new_price: lab(c.new_price, c.currency), change_pct: c.change_pct,
    current_value: lab(c.current_value, c.currency), new_value: lab(c.new_value, c.currency),
    value_change_in_base: lab(c.value_change_base, base) } : c),
  net_worth_now: lab(scenario.net_worth_now, base), net_worth_after: lab(scenario.net_worth_after, base),
  net_worth_change: lab(scenario.net_worth_change, base), net_worth_change_pct: scenario.net_worth_change_pct,
};

// Plain-text answer built from the same facts: shown above the LLM text for what-if, and
// on its own whenever the LLM text fails the number check.
const money = (n, c) => new Intl.NumberFormat('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 }).format(n) + ' ' + c;
const sign = n => (n >= 0 ? '+' : '−') ;
let block;
if (scenario) {
  block = [
    ...scenario.changes.map(c => c.held
      ? `${c.symbol}: ${c.quantity} shares × ${money(c.new_price, c.currency)} = ${money(c.new_value, c.currency)} (now ${money(c.current_value, c.currency)})`
      : c.note),
    `Net worth now: ${money(scenario.net_worth_now, base)}`,
    `Net worth in this scenario: ${money(scenario.net_worth_after, base)} (${sign(scenario.net_worth_change)}${money(Math.abs(scenario.net_worth_change), base)}`
      + (scenario.net_worth_change_pct != null ? `, ${sign(scenario.net_worth_change)}${Math.abs(scenario.net_worth_change_pct)}%)` : ')'),
  ].join('\n');
} else {
  block = [
    ...positions.map(p => `• ${p.symbol}: ${p.quantity} × ${money(p.price, p.currency)} = ${money(p.value_base, base)} · ${p.share_of_assets_pct}% of assets · P/L ${sign(p.profit_base)}${money(Math.abs(p.profit_base), base)}${p.profit_pct != null ? ` (${sign(p.profit_pct)}${Math.abs(p.profit_pct)}%)` : ''}`),
    ...accounts.map(a => `• ${a.name}: ${money(a.amount_base, base)} · ${a.share_of_assets_pct}% of assets`),
    `Assets ${money(facts.total_assets, base)} − Liabilities ${money(facts.total_liabilities, base)} = Net worth ${money(facts.net_worth, base)}`,
  ].join('\n');
}
return [{ json: { mode: start.mode, question: start.question.text, facts, llm_facts, amounts, block } }];
"""

EXPLAIN_SYSTEM = """You explain a user's own finances in plain English. You receive their QUESTION and FACTS (JSON) computed by the app.
Rules:
- Answer the question using ONLY numbers that appear in FACTS. Copy them; never add, subtract, multiply or round to new values.
- Every amount in FACTS is written with its currency, e.g. "107,065.00 USD". Always copy the amount AND its currency together; never change the currency.
- Prefer the fields ending in _in_base (the user's base currency) for values, profits and totals; the native-currency figure may follow in brackets.
- If FACTS do not contain what is needed, say which information is missing.
- For a what-if scenario, state the new net worth and the change, then one sentence of context.
- At most 80 words, plain text, no headings, no bullet lists.
- End with: "Not investment advice."
"""

CHECK = r"""// Number check: every number in the LLM's text must exist in FACTS (or in the question),
// and an amount written with a currency must have that currency in FACTS (a USD value
// called "EUR" is rejected). Allowed tolerance follows what was written: "2,130,335" may
// round to an integer, "45.3%" to one decimal. Anything else means the model invented a value.
const c = $('Compute answer in code').first().json;
const text = String($json.text ?? '').trim();

const allowed = c.amounts.map(([v, cur]) => [v, cur]);             // amounts with their currency
(function walk(v) {                                               // percentages, quantities: no currency
  if (typeof v === 'number' && Number.isFinite(v)) allowed.push([v, null], [Math.abs(v), null]);
  else if (v && typeof v === 'object') Object.values(v).forEach(walk);
})(c.llm_facts);
c.amounts.forEach(([v, cur]) => allowed.push([Math.abs(v), cur]));
for (const m of c.question.matchAll(/\d+(?:[.,]\d+)*/g)) allowed.push([Number(m[0].replace(/,/g, '')), null]);

const MULT = { k: 1e3, thousand: 1e3, m: 1e6, million: 1e6, bn: 1e9, billion: 1e9 };
const SYM = { '€': 'EUR', '$': 'USD', '£': 'GBP', '₺': 'TRY' };
const bad = [];
for (const m of text.matchAll(/([€$£₺])?\s?(\d[\d,]*(?:\.\d+)?)(?:\s*(thousand|million|billion|bn|k|m)\b)?(?:\s*(EUR|USD|TRY|GBP)\b)?/gi)) {
  const raw = m[2];
  const n = Number(raw.replace(/,/g, ''));
  if (!Number.isFinite(n)) continue;
  const decimals = raw.includes('.') ? raw.split('.')[1].length : 0;
  const mult = MULT[String(m[3] ?? '').toLowerCase()] ?? 1;
  const value = n * mult;
  const cur = (m[4] ?? '').toUpperCase() || SYM[m[1]] || null;
  if (!cur && mult === 1 && Number.isInteger(n) && (n <= 10 || (n >= 1990 && n <= 2100))) continue; // small counts, years
  const tol = Math.max(0.0051, 0.5 * Math.pow(10, -decimals)) * mult;
  const ok = allowed.some(([a, ac]) => Math.abs(a - value) <= tol && (!cur || ac === null || ac === cur));
  if (!ok) bad.push(m[0].trim());
}

const ok = text.length > 0 && !$json.error && bad.length === 0;
const reply = c.mode === 'what_if'
  ? c.block + (ok ? '\n\n' + text : '')
  : (ok ? text : c.block);
return [{ json: {
  intent: c.mode, status: 'done', reply,
  data: { facts: c.facts, explanation_checked: ok, rejected_numbers: bad, llm_error: $json.error ? String($json.error.message ?? $json.error) : null },
} }];
"""

nodes = [
    sub_trigger("Input: question", [0, 200], "YAanswerQuest001"),
    sql("DB: portfolio snapshot", [220, 200], SNAPSHOT, "={{ JSON.stringify({ user_id: $json.user_id }) }}"),
    code("Compute answer in code", [460, 200], COMPUTE),
    llm_chain("AI: word the answer", [700, 200], EXPLAIN_SYSTEM,
              "={{ 'QUESTION: ' + $json.question + '\\n\\nFACTS: ' + JSON.stringify($json.llm_facts) }}",
              onError="continueRegularOutput", alwaysOutputData=True),
    chat_model("LLM for answer", [700, 420], temperature=0.2),
    code("Check answer numbers (fallback)", [960, 200], CHECK),
]

connections = connect(
    ("Input: question", "DB: portfolio snapshot"),
    ("DB: portfolio snapshot", "Compute answer in code"),
    ("Compute answer in code", "AI: word the answer"),
    ("AI: word the answer", "Check answer numbers (fallback)"),
)
connect_ai(connections, "LLM for answer", "AI: word the answer")

write("answer_question", "YAanswerQuest001", "YourAnalyst 5 – Sub: answer a question about my money", nodes, connections)
