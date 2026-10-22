"""YA – Valuation refresh: quotes + FX rates into Postgres.

Triggers: Schedule (every 30 min), Manual (demo), and POST /ya-refresh (dashboard
refresh button; answers 202 right away, the dashboard re-reads a few seconds later).
"""
from lib import NOT_EMPTY, RETRY, auth, code, connect, if_node, node, respond, sql, webhook, write

VALIDATE_QUOTES = r"""// Turn Yahoo responses into quote rows.
// A failed or implausible quote becomes price = null: the upsert then keeps the
// last good price, which the dashboard shows as 'stale' (or 'unavailable' if none).
const symbols = $('Tracked symbols').all();
const now = Date.now();

const rows = $input.all().map((item, i) => {
  const symbol = symbols[item.pairedItem?.item ?? i].json.symbol;
  const meta = item.json?.chart?.result?.[0]?.meta;
  const price = Number(meta?.regularMarketPrice);
  const ts = Number(meta?.regularMarketTime) * 1000;
  const ok = !!meta
    && String(meta.symbol).toUpperCase() === symbol.toUpperCase()
    && Number.isFinite(price) && price > 0
    && /^[A-Z]{3}$/.test(meta.currency ?? '')
    && Number.isFinite(ts) && ts <= now + 5 * 60 * 1000;
  const prev = Number(meta?.chartPreviousClose ?? meta?.previousClose);
  const err = item.json?.error;
  return {
    symbol,
    price: ok ? price : null,
    change_pct: ok && prev > 0 ? Math.round((price / prev - 1) * 1e6) / 1e4 : null,
    currency: ok ? meta.currency : 'USD',
    as_of: ok ? new Date(ts).toISOString() : null,
    source: 'yahoo',
    error: ok ? null : String(err?.message ?? err ?? 'invalid quote payload'),
  };
});

const failed = rows.filter(r => r.price === null);
return [{ json: {
  rows,
  ok_count: rows.length - failed.length,
  failed: failed.map(r => `${r.symbol}: ${r.error}`),
} }];
"""

BUILD_FX = r"""// One request for all currencies in use (USD is the anchor, always 1).
const currencies = [...new Set(
  $input.all().map(i => String(i.json.currency).trim().toUpperCase())
)].filter(c => /^[A-Z]{3}$/.test(c) && c !== 'USD');

return [{ json: { currencies, symbols: currencies.join(',') } }];
"""

VALIDATE_FX = r"""// Frankfurter returns units per 1 USD; the DB stores USD per 1 unit.
const res = $input.first().json;
const wanted = $('Build FX request').first().json.currencies;

if (res.base !== 'USD') throw new Error(`Unexpected FX base: ${res.base}`);
if (!/^\d{4}-\d{2}-\d{2}$/.test(res.date ?? '')) throw new Error(`Missing FX date: ${res.date}`);

const rates = res.rates ?? {};
const valid = c => Number.isFinite(Number(rates[c])) && Number(rates[c]) > 0;

return [{ json: {
  rows: wanted.filter(valid).map(c => ({
    currency: c,
    usd_per_unit: 1 / Number(rates[c]),
    as_of: `${res.date}T00:00:00Z`,
    source: 'frankfurter (ECB)',
  })),
  missing: wanted.filter(c => !valid(c)),
} }];
"""

UPSERT_QUOTES = """insert into quote (symbol, price, change_pct, currency, as_of, source, fetched_at)
select r.symbol, r.price, r.change_pct, r.currency, r.as_of, r.source, now()
from jsonb_to_recordset($1::jsonb)
     as r(symbol text, price numeric, change_pct numeric, currency char(3), as_of timestamptz, source text)
on conflict (symbol) do update set
    price      = coalesce(excluded.price, quote.price),
    change_pct = case when excluded.price is null then quote.change_pct else excluded.change_pct end,
    currency   = case when excluded.price is null then quote.currency   else excluded.currency   end,
    as_of      = coalesce(excluded.as_of, quote.as_of),
    source     = case when excluded.price is null then quote.source     else excluded.source     end,
    fetched_at = case when excluded.price is null then quote.fetched_at else excluded.fetched_at end
returning symbol, price, as_of;"""

UPSERT_FX = """insert into fx_rate (currency, usd_per_unit, as_of, source, fetched_at)
select r.currency, r.usd_per_unit, r.as_of, r.source, now()
from jsonb_to_recordset($1::jsonb)
     as r(currency char(3), usd_per_unit numeric, as_of timestamptz, source text)
on conflict (currency) do update set
    usd_per_unit = excluded.usd_per_unit,
    as_of        = excluded.as_of,
    source       = excluded.source,
    fetched_at   = excluded.fetched_at
returning currency, usd_per_unit, as_of;"""

# FX fallback: after the retries, keep the last good rates (no upsert). They age past
# 3 days into v_line_item.fx_stale / v_net_worth.fx_stale_items, so the dashboard can label them.
FX_FAILED = r"""const err = $json.error ?? {};
const msg = String(err.message ?? err ?? 'FX request failed').slice(0, 1000);
return [{ json: { log: {
  workflow_name: $workflow.name,
  node_name: 'Fetch FX (Frankfurter)',
  execution_id: String($execution.id),
  event_id: null,
  message: `FX fetch failed after retries, kept last rates (stale after 3 days): ${msg}`,
} } }];
"""

LOG_FX_FAILURE = """insert into error_log (workflow_name, node_name, execution_id, event_id, message)
select l->>'workflow_name', l->>'node_name', l->>'execution_id', l->>'event_id', l->>'message'
from (select $1::jsonb as l) x
returning id;"""

ROWS = "={{ JSON.stringify($json.rows) }}"

nodes = [
    # Quotes every 10 s (portfolio + watchlist only, a handful of symbols); FX rates are
    # daily ECB figures, so every 30 min is plenty.
    node("Every 10 s (quotes)", "n8n-nodes-base.scheduleTrigger", 1.2, [0, -120],
         {"rule": {"interval": [{"field": "seconds", "secondsInterval": 10}]}}),
    node("Every 30 min (FX)", "n8n-nodes-base.scheduleTrigger", 1.2, [0, 360],
         {"rule": {"interval": [{"field": "minutes", "minutesInterval": 30}]}}),
    node("Run now (manual)", "n8n-nodes-base.manualTrigger", 1, [0, 220], {}),
    webhook("POST /ya-refresh", [-660, 560], "POST", "ya-refresh"),
    auth("Auth user", [-440, 560]),
    if_node("If: authenticated?", [-220, 560], "={{ $json.user_id }}", NOT_EMPTY),
    respond("Respond 401", [0, 700], '={{ { "ok": false, "error": "Missing or invalid X-YA-Token" } }}', 401),
    respond("Respond 202", [0, 440], '={{ { "ok": true, "status": "refresh started" } }}', 202),

    sql("Tracked symbols", [260, -100],
        "-- What the dashboard shows. Scan universes are priced by the analysis API.\n"
        "select symbol from holding\nunion\nselect symbol from watchlist\norder by 1;"),
    node("Fetch quote (Yahoo)", "n8n-nodes-base.httpRequest", 4.2, [500, -100],
         {"url": "=https://query1.finance.yahoo.com/v8/finance/chart/{{ encodeURIComponent($json.symbol) }}",
          "sendQuery": True,
          "queryParameters": {"parameters": [{"name": "range", "value": "1d"},
                                             {"name": "interval", "value": "1d"}]},
          "sendHeaders": True,
          "headerParameters": {"parameters": [{"name": "User-Agent", "value": "Mozilla/5.0"}]},
          "options": {"batching": {"batch": {"batchSize": 5, "batchInterval": 500}},
                      "timeout": 10000}},
         onError="continueRegularOutput", **RETRY),
    code("Validate quotes", [740, -100], VALIDATE_QUOTES),
    sql("Upsert quotes", [980, -100], UPSERT_QUOTES, ROWS),

    sql("Tracked currencies", [260, 320],
        "-- Currencies in use, plus common ones so the dashboard can always convert them.\n"
        "select currency from asset\nunion select currency from liability\n"
        "union select cost_currency from holding\nunion select base_currency from app_user\n"
        "union select unnest(array['EUR', 'TRY', 'GBP']);"),
    code("Build FX request", [500, 320], BUILD_FX),
    node("Fetch FX (Frankfurter)", "n8n-nodes-base.httpRequest", 4.2, [740, 320],
         {"url": "https://api.frankfurter.dev/v1/latest",
          "sendQuery": True,
          "queryParameters": {"parameters": [{"name": "base", "value": "USD"},
                                             {"name": "symbols", "value": "={{ $json.symbols }}"}]},
          "options": {"timeout": 10000}},
         onError="continueErrorOutput", **RETRY),
    code("Validate FX", [980, 320], VALIDATE_FX),
    code("FX failed: keep last rates", [980, 520], FX_FAILED),
    sql("Log FX failure", [1220, 520], LOG_FX_FAILURE, "={{ JSON.stringify($json.log) }}"),
    sql("Upsert FX rates", [1220, 320], UPSERT_FX, ROWS),
]

connections = connect(
    ("Every 10 s (quotes)", "Tracked symbols"), ("Every 30 min (FX)", "Tracked currencies"),
    ("Run now (manual)", "Tracked symbols"), ("Run now (manual)", "Tracked currencies"),
    ("POST /ya-refresh", "Auth user"), ("Auth user", "If: authenticated?"),
    ("If: authenticated?", "Respond 202", 0), ("If: authenticated?", "Respond 401", 1),
    ("Respond 202", "Tracked symbols"), ("Respond 202", "Tracked currencies"),
    ("Tracked symbols", "Fetch quote (Yahoo)"),
    ("Fetch quote (Yahoo)", "Validate quotes"),
    ("Validate quotes", "Upsert quotes"),
    ("Tracked currencies", "Build FX request"),
    ("Build FX request", "Fetch FX (Frankfurter)"),
    ("Fetch FX (Frankfurter)", "Validate FX", 0),
    ("Fetch FX (Frankfurter)", "FX failed: keep last rates", 1),
    ("FX failed: keep last rates", "Log FX failure"),
    ("Validate FX", "Upsert FX rates"),
)

write("valuation_refresh", "YAvaluationRfrsh", "YA – Valuation refresh", nodes, connections)
