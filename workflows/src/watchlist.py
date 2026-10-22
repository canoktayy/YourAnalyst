"""YourAnalyst 4 – Sub: add or remove watchlist stocks (sub-workflow, called by the chat router).

Input item: {user_id, request_id, event_id, watchlist: {action: add|remove, items: [..]}}
Output item: {intent, status, reply, data}

The LLM only extracts what the user wrote ("palantir", "data dog", "TSLA"); it never
picks tickers, because small models guess wrong ones (palantir -> PAL). Each item is
resolved with Yahoo search, then checked against a real Yahoo quote before it is saved. A watchlist entry is not a financial record, so there is no confirmation
step; adding twice is harmless (unique (user_id, symbol)).
"""
from lib import RETRY, code, connect, node, sql, sub_trigger, switch, write

# Every item is looked up on Yahoo search, trying a few spellings: as written, without
# spaces, and with doubled letters collapsed ("data dog" -> datadog, "cloudfflare" -> cloudflare).
SPLIT_NAMES = r"""const names = $('Input: add or remove').first().json.watchlist.items;
const items = [];
names.forEach((name, n) => {
  const base = name.toLowerCase().trim();
  const variants = [...new Set([base, base.replace(/\s+/g, ''), base.replace(/(.)\1+/g, '$1'),
                                base.replace(/\s+/g, '').replace(/(.)\1+/g, '$1')])];
  variants.forEach((q, v) => items.push({ json: { n, name, v, q } }));
});
return items;
"""

US_EXCHANGES = "['NMS', 'NYQ', 'NGM', 'NCM', 'ASE', 'PCX', 'BTS', 'NAS']"

MERGE_RESOLVED = r"""// For each name, take the first spelling that finds a US-listed equity.
const asked = $('One item per name').all().map(i => i.json);
const US = """ + US_EXCHANGES + r""";
const best = {};
$input.all().forEach((item, i) => {
  const a = asked[item.pairedItem?.item ?? i];
  const quotes = (item.json?.quotes ?? []).filter(q => q.quoteType === 'EQUITY' || q.quoteType === 'ETF');
  const hit = quotes.find(q => US.includes(q.exchange)) ?? null;
  if (hit && (!best[a.n] || a.v < best[a.n].v)) best[a.n] = { v: a.v, name: a.name, symbol: String(hit.symbol).toUpperCase() };
});
const names = $('Input: add or remove').first().json.watchlist.items;
return [{ json: {
  resolved: names.map((_, n) => best[n]).filter(Boolean).map(({ name, symbol }) => ({ name, symbol })),
  unknown: names.filter((_, n) => !best[n]),
} }];
"""

SPLIT = r"""// One item per symbol so the HTTP node checks each ticker (an empty list still
// yields one placeholder item, so the sub-workflow always answers).
const w = $('Input: add or remove').first().json.watchlist;
const merged = $('Merge found tickers').first().json;
const symbols = [...new Set(merged.resolved.map(r => r.symbol))];
return symbols.length ? symbols.map(symbol => ({ json: { symbol } })) : [{ json: { symbol: null } }];
"""

VALIDATE = r"""// Keep only symbols Yahoo knows. Its own symbol and name win over the LLM's guess.
const w = $('Input: add or remove').first().json.watchlist;
const merged = $('Merge found tickers').first().json;
const asked = $('One item per ticker').all().map(i => i.json.symbol);
const now = Date.now();
const found = [], missing = [];
$input.all().forEach((item, i) => {
  const symbol = asked[item.pairedItem?.item ?? i];
  if (!symbol) return;
  const meta = item.json?.chart?.result?.[0]?.meta;
  const price = Number(meta?.regularMarketPrice);
  const ts = Number(meta?.regularMarketTime) * 1000;
  if (!meta || String(meta.symbol).toUpperCase() !== symbol || !(price > 0)) { missing.push(symbol); return; }
  const prev = Number(meta.chartPreviousClose ?? meta.previousClose);
  found.push({
    symbol,
    name: String(meta.longName ?? meta.shortName ?? '').slice(0, 80) || null,
    price,
    change_pct: prev > 0 ? Math.round((price / prev - 1) * 1e6) / 1e4 : null,
    currency: /^[A-Z]{3}$/.test(meta.currency ?? '') ? meta.currency : 'USD',
    as_of: Number.isFinite(ts) && ts <= now + 300000 ? new Date(ts).toISOString() : new Date(now).toISOString(),
  });
});
return [{ json: { user_id: $('Input: add or remove').first().json.user_id, found, missing,
  resolved: merged.resolved, unknown: merged.unknown } }];
"""

SAVE = """-- Add the verified symbols and store their quotes, in one statement.
with j as (select $1::jsonb as j),
rows as (
    select r.* from j, jsonb_to_recordset(j.j->'found')
        as r(symbol text, name text, price numeric, change_pct numeric, currency char(3), as_of timestamptz)),
q as (
    insert into quote (symbol, price, change_pct, currency, as_of, source, fetched_at)
    select symbol, price, change_pct, currency, as_of, 'yahoo', now() from rows
    on conflict (symbol) do update set
        price = excluded.price, change_pct = excluded.change_pct, currency = excluded.currency,
        as_of = excluded.as_of, source = excluded.source, fetched_at = excluded.fetched_at
    returning symbol),
ins as (
    insert into watchlist (user_id, symbol, name)
    select (j.j->>'user_id')::uuid, rows.symbol, rows.name from j, rows
    on conflict (user_id, symbol) do nothing
    returning symbol)
select coalesce((select json_agg(symbol) from ins), '[]'::json) as added,
       (select count(*) from q) as quotes;"""

# Remove matches an item against the user's own list: exact ticker, or part of the name.
REMOVE = """with j as (select $1::jsonb as j),
items as (select lower(trim(x)) as item from j, jsonb_array_elements_text(j.j->'watchlist'->'items') x),
del as (
    delete from watchlist w using j
    where w.user_id = (j.j->>'user_id')::uuid
      and exists (select 1 from items i
                  where lower(w.symbol) = i.item
                     or (length(i.item) >= 3 and lower(coalesce(w.name, '')) like '%' || i.item || '%'))
    returning w.symbol, w.name)
select coalesce((select json_agg(json_build_object('symbol', symbol, 'name', name)) from del), '[]'::json) as removed;"""

ADDED_REPLY = r"""const v = $('Keep only valid tickers').first().json;
const added = new Set($json.added);
const money = (n, c) => new Intl.NumberFormat('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 }).format(n) + ' ' + c;
const said = Object.fromEntries(v.resolved.map(r => [r.symbol, r.name]));
const fmt = f => `${f.symbol}${f.name ? ` (${f.name})` : ''}: ${money(f.price, f.currency)}`
  + (f.change_pct != null ? ` (${f.change_pct >= 0 ? '+' : ''}${f.change_pct.toFixed(2)}%)` : '')
  + (said[f.symbol] && said[f.symbol].toUpperCase() !== f.symbol ? `  ← "${said[f.symbol]}"` : '');
const lines = [];
const newOnes = v.found.filter(f => added.has(f.symbol));
const already = v.found.filter(f => !added.has(f.symbol));
if (newOnes.length) lines.push('Added to your watchlist:', ...newOnes.map(f => '• ' + fmt(f)));
if (already.length) lines.push(`Already on your watchlist: ${already.map(f => f.symbol).join(', ')}`);
if (v.missing.length) lines.push(`No quote found for: ${v.missing.join(', ')}. Could you check the ticker?`);
if (v.unknown.length) lines.push(`I could not find a US stock for: ${v.unknown.join(', ')}. Which ticker did you mean?`);
if (!lines.length) lines.push('Nothing to add.');
return [{ json: { intent: 'watchlist', status: 'done', reply: lines.join('\n'),
  data: { added: [...added], resolved: v.resolved, already: already.map(f => f.symbol), missing: v.missing, unknown: v.unknown } } }];
"""

REMOVED_REPLY = r"""const asked = $('Input: add or remove').first().json.watchlist.items;
const removed = $json.removed;
const hit = item => removed.some(r => r.symbol.toLowerCase() === item.toLowerCase()
  || (item.length >= 3 && String(r.name ?? '').toLowerCase().includes(item.toLowerCase())));
const notThere = asked.filter(i => !hit(i));
const lines = [];
if (removed.length) lines.push(`Removed from your watchlist: ${removed.map(r => r.symbol).join(', ')}`);
if (notThere.length) lines.push(`Not on your watchlist: ${notThere.join(', ')}`);
return [{ json: { intent: 'watchlist', status: 'done', reply: lines.join('\n') || 'Nothing to remove.',
  data: { removed: removed.map(r => r.symbol), not_there: notThere } } }];
"""

nodes = [
    sub_trigger("Input: add or remove", [0, 200], "YAwatchlist00001"),
    switch("Route: add or remove", [220, 200], "={{ $json.watchlist.action }}", [("add", "add"), ("remove", "remove")]),
    code("One item per name", [700, -80], SPLIT_NAMES),
    node("API: find ticker by name (Yahoo)", "n8n-nodes-base.httpRequest", 4.2, [940, -80],
         {"url": "https://query2.finance.yahoo.com/v1/finance/search",
          "sendQuery": True,
          "queryParameters": {"parameters": [{"name": "q", "value": "={{ $json.q }}"},
                                             {"name": "quotesCount", "value": "6"},
                                             {"name": "newsCount", "value": "0"}]},
          "sendHeaders": True,
          "headerParameters": {"parameters": [{"name": "User-Agent", "value": "Mozilla/5.0"}]},
          "options": {"batching": {"batch": {"batchSize": 4, "batchInterval": 300}}, "timeout": 10000}},
         onError="continueRegularOutput", **RETRY),
    code("Merge found tickers", [1180, -80], MERGE_RESOLVED),
    code("One item per ticker", [1420, 80], SPLIT),
    node("API: check ticker exists (Yahoo)", "n8n-nodes-base.httpRequest", 4.2, [1660, 80],
         {"url": "=https://query1.finance.yahoo.com/v8/finance/chart/{{ encodeURIComponent($json.symbol) }}",
          "sendQuery": True,
          "queryParameters": {"parameters": [{"name": "range", "value": "1d"}, {"name": "interval", "value": "1d"}]},
          "sendHeaders": True,
          "headerParameters": {"parameters": [{"name": "User-Agent", "value": "Mozilla/5.0"}]},
          "options": {"batching": {"batch": {"batchSize": 5, "batchInterval": 300}}, "timeout": 10000}},
         onError="continueRegularOutput", **RETRY),
    code("Keep only valid tickers", [1900, 80], VALIDATE),
    sql("DB: add to watchlist", [2140, 80], SAVE, "={{ JSON.stringify($json) }}"),
    code("Reply: added + prices", [2380, 80], ADDED_REPLY),
    sql("DB: remove from watchlist", [460, 320], REMOVE, "={{ JSON.stringify($json) }}"),
    code("Reply: removed", [700, 320], REMOVED_REPLY),
]

connections = connect(
    ("Input: add or remove", "Route: add or remove"),
    ("Route: add or remove", "One item per name", 0),
    ("One item per name", "API: find ticker by name (Yahoo)"),
    ("API: find ticker by name (Yahoo)", "Merge found tickers"),
    ("Merge found tickers", "One item per ticker"),
    ("Route: add or remove", "DB: remove from watchlist", 1),
    ("One item per ticker", "API: check ticker exists (Yahoo)"),
    ("API: check ticker exists (Yahoo)", "Keep only valid tickers"),
    ("Keep only valid tickers", "DB: add to watchlist"),
    ("DB: add to watchlist", "Reply: added + prices"),
    ("DB: remove from watchlist", "Reply: removed"),
)

write("watchlist", "YAwatchlist00001", "YourAnalyst 4 – Sub: add or remove watchlist stocks", nodes, connections)
