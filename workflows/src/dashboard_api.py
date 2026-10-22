"""YourAnalyst 7 – Dashboard page: load and save data: GET dashboard data, POST record create/update/delete.

Both webhooks authenticate with the X-YA-Token header (sha256 compared with
app_user.api_token_hash) and answer with the full dashboard payload, so the UI
re-renders from the database after every write.
"""
from lib import IS_TRUE, NOT_EMPTY, auth, code, connect, if_node, respond, sql, switch, webhook, write

VALIDATE_RECORD = r"""// Validate and normalise a dashboard form write before it reaches the database.
const body = $('Trigger: save form (POST /ya-record)').first().json.body ?? {};
const user_id = $('DB: find user by token').first().json.user_id;
const errors = [];

const TYPES = ['asset', 'liability', 'holding', 'watchlist'];
const ACTIONS = ['create', 'update', 'delete'];
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

const type = String(body.type ?? '');
const action = String(body.action ?? '');
const id = body.id ?? null;
const f = body.fields ?? {};

if (!TYPES.includes(type)) errors.push(`type must be one of ${TYPES.join(', ')}`);
if (!ACTIONS.includes(action)) errors.push(`action must be one of ${ACTIONS.join(', ')}`);
if (action !== 'create' && !UUID.test(String(id))) errors.push('id must be a record uuid');

const text = (v, max) => String(v ?? '').trim().slice(0, max);
const cur = v => String(v ?? '').trim().toUpperCase();
const num = v => (typeof v === 'string' ? Number(v.replace(',', '.')) : Number(v));
const checkCur = (c, field) => { if (!/^[A-Z]{3}$/.test(c)) errors.push(`${field} must be a 3-letter currency code`); };

let fields = {};
if (action !== 'delete') {
  if (type === 'asset') {
    fields = {
      kind: f.kind === 'other' ? 'other' : 'cash',
      name: text(f.name, 80),
      amount: num(f.amount),
      currency: cur(f.currency),
      color: /^#[0-9a-f]{6}$/i.test(f.color ?? '') ? f.color : null,
    };
    if (!fields.name) errors.push('name is required');
    if (!(fields.amount >= 0)) errors.push('amount must be a number >= 0');
    checkCur(fields.currency, 'currency');
  } else if (type === 'liability') {
    fields = {
      counterparty: text(f.counterparty, 80),
      amount: num(f.amount),
      currency: cur(f.currency),
      note: text(f.note, 200) || null,
    };
    if (!fields.counterparty) errors.push('counterparty is required');
    if (!(fields.amount > 0)) errors.push('amount must be a number > 0');
    checkCur(fields.currency, 'currency');
  } else if (type === 'holding') {
    fields = {
      symbol: String(f.symbol ?? '').trim().toUpperCase(),
      name: text(f.name, 80) || null,
      quantity: num(f.quantity),
      cost_per_share: num(f.cost_per_share ?? 0),
      cost_currency: cur(f.cost_currency || 'USD'),
    };
    if (!/^[A-Z][A-Z0-9.\-]{0,9}$/.test(fields.symbol)) errors.push('symbol is not a valid ticker');
    if (!(fields.quantity > 0)) errors.push('quantity must be a number > 0');
    if (!(fields.cost_per_share >= 0)) errors.push('cost_per_share must be a number >= 0');
    checkCur(fields.cost_currency, 'cost_currency');
  } else if (type === 'watchlist') {
    // create: symbol (+ optional name); update: position only (drag and drop order).
    if (action === 'create') {
      fields = { symbol: String(f.symbol ?? '').trim().toUpperCase(), name: text(f.name, 80) || null };
      if (!/^[A-Z0-9^][A-Z0-9.\-=^]{0,14}$/.test(fields.symbol)) errors.push('symbol is not a valid ticker');
    } else {
      fields = { position: num(f.position) };
      if (!Number.isFinite(fields.position)) errors.push('position must be a number');
    }
  }
}

return [{ json: { valid: errors.length === 0, errors, type, action, id, user_id, fields } }];
"""

# One statement per table handles create/update/delete. Exactly one CTE matches the
# action; record_id is null when an update/delete id does not belong to the user.
def write_sql(table, columns):
    j = "p.j->'fields'"
    casts = {"amount": "::numeric", "quantity": "::numeric", "cost_per_share": "::numeric"}
    val = lambda c: f"({j}->>'{c}'){casts.get(c, '')}"
    sets = ",\n        ".join(f"{c} = {val(c)}" for c in columns)
    cols = ", ".join(columns)
    vals = ", ".join(val(c) for c in columns)
    owned = f"t.id = (p.j->>'id')::uuid and t.user_id = (p.j->>'user_id')::uuid"
    return f"""with p as (select $1::jsonb as j),
del as (
    delete from {table} t using p
    where p.j->>'action' = 'delete' and {owned}
    returning t.id),
upd as (
    update {table} t set
        {sets},
        updated_at = now()
    from p
    where p.j->>'action' = 'update' and {owned}
    returning t.id),
ins as (
    insert into {table} (user_id, {cols})
    select (p.j->>'user_id')::uuid, {vals}
    from p
    where p.j->>'action' = 'create'
    returning id)
select coalesce((select id from del), (select id from upd), (select id from ins)) as record_id;"""

LOAD_DASHBOARD = """-- Everything the dashboard renders, in one round trip. Totals come from SQL views.
with u as (select * from app_user where id = $1::uuid),
base as (select f.usd_per_unit from fx_rate f join u on f.currency = u.base_currency)
select json_build_object(
    'user', (select json_build_object('id', id, 'name', display_name, 'base_currency', base_currency) from u),
    'totals', (select row_to_json(n) from v_net_worth n where n.user_id = $1::uuid),
    'assets', coalesce((
        select json_agg(a order by a.created_at)
        from (select id, kind, name, amount, currency, color, created_at
              from asset where user_id = $1::uuid) a), '[]'::json),
    'liabilities', coalesce((
        select json_agg(l order by l.created_at)
        from (select id, counterparty, amount, currency, note, created_at
              from liability where user_id = $1::uuid) l), '[]'::json),
    'positions', coalesce((
        select json_agg(p order by p.symbol)
        from (select id, symbol, name, quantity, cost_per_share, cost_currency, last_price,
                     price_currency, change_pct, price_as_of, price_status, position_value,
                     cost_basis, unrealised_pl
              from v_position where user_id = $1::uuid) p), '[]'::json),
    -- rates[c] = units of base currency per 1 unit of c
    'watchlist', coalesce((
        select json_agg(w order by w.position, w.created_at)
        from (select w.id, w.symbol, w.name, w.position, w.created_at, q.price, q.change_pct,
                     q.currency as price_currency, q.as_of as price_as_of
              from watchlist w left join quote q on q.symbol = w.symbol
              where w.user_id = $1::uuid) w), '[]'::json),
    'rates', (select json_object_agg(f.currency, f.usd_per_unit / base.usd_per_unit)
              from fx_rate f, base),
    'fx_as_of', (select max(as_of) from fx_rate where source <> 'fixed'),
    'quotes_as_of', (select max(as_of) from quote)
) as dashboard;"""

# Watchlist: create is idempotent (an existing symbol returns its id), update moves the row.
WRITE_WATCHLIST = """with p as (select $1::jsonb as j),
del as (
    delete from watchlist t using p
    where p.j->>'action' = 'delete' and t.id = (p.j->>'id')::uuid and t.user_id = (p.j->>'user_id')::uuid
    returning t.id),
upd as (
    update watchlist t set position = (p.j->'fields'->>'position')::double precision
    from p
    where p.j->>'action' = 'update' and t.id = (p.j->>'id')::uuid and t.user_id = (p.j->>'user_id')::uuid
    returning t.id),
ins as (
    insert into watchlist (user_id, symbol, name)
    select (p.j->>'user_id')::uuid, p.j->'fields'->>'symbol', p.j->'fields'->>'name'
    from p where p.j->>'action' = 'create'
    on conflict (user_id, symbol) do update set name = coalesce(excluded.name, watchlist.name)
    returning id)
select coalesce((select id from del), (select id from upd), (select id from ins)) as record_id;"""

USER_ID = "={{ $('DB: find user by token').first().json.user_id }}"

nodes = [
    webhook("Trigger: load dashboard (GET /ya-dashboard)", [0, 0], "GET", "ya-dashboard"),
    webhook("Trigger: save form (POST /ya-record)", [0, 240], "POST", "ya-record"),
    auth("DB: find user by token", [240, 120]),
    if_node("Token valid?", [460, 120], "={{ $json.user_id }}", NOT_EMPTY),
    respond("Reply 401: invalid token", [680, 300],
            '={{ { "ok": false, "error": "Missing or invalid X-YA-Token" } }}', 401),
    if_node("Is it a form save?", [680, 100], "={{ $('Trigger: save form (POST /ya-record)').isExecuted }}", IS_TRUE),

    code("Check form fields", [900, 240], VALIDATE_RECORD),
    if_node("Form fields OK?", [1120, 240], "={{ $json.valid }}", IS_TRUE),
    respond("Reply 400: invalid form", [1340, 440],
            '={{ { "ok": false, "error": "Invalid record", "details": $json.errors } }}', 400),
    switch("Route by record type", [1340, 220], "={{ $json.type }}",
           [("asset", "asset"), ("liability", "liability"), ("holding", "holding"), ("watchlist", "watchlist")]),
    sql("DB: save asset", [1580, 80], write_sql("asset", ["kind", "name", "amount", "currency", "color"]),
        "={{ JSON.stringify($json) }}"),
    sql("DB: save debt", [1580, 240], write_sql("liability", ["counterparty", "amount", "currency", "note"]),
        "={{ JSON.stringify($json) }}"),
    sql("DB: save watchlist item", [1580, 560], WRITE_WATCHLIST, "={{ JSON.stringify($json) }}"),
    sql("DB: save stock holding", [1580, 400],
        write_sql("holding", ["symbol", "name", "quantity", "cost_per_share", "cost_currency"]),
        "={{ JSON.stringify($json) }}"),
    if_node("Record found?", [1800, 240], "={{ $json.record_id }}", NOT_EMPTY),
    respond("Reply 404: record not found", [2020, 420],
            '={{ { "ok": false, "error": "Record not found for this user" } }}', 404),

    sql("DB: load dashboard data", [2020, 40], LOAD_DASHBOARD, USER_ID),
    respond("Reply: dashboard data", [2240, 40], "={{ { ok: true, ...$json.dashboard } }}"),
]

connections = connect(
    ("Trigger: load dashboard (GET /ya-dashboard)", "DB: find user by token"),
    ("Trigger: save form (POST /ya-record)", "DB: find user by token"),
    ("DB: find user by token", "Token valid?"),
    ("Token valid?", "Is it a form save?", 0),
    ("Token valid?", "Reply 401: invalid token", 1),
    ("Is it a form save?", "Check form fields", 0),
    ("Is it a form save?", "DB: load dashboard data", 1),
    ("Check form fields", "Form fields OK?"),
    ("Form fields OK?", "Route by record type", 0),
    ("Form fields OK?", "Reply 400: invalid form", 1),
    ("Route by record type", "DB: save asset", 0),
    ("Route by record type", "DB: save debt", 1),
    ("Route by record type", "DB: save stock holding", 2),
    ("Route by record type", "DB: save watchlist item", 3),
    ("DB: save watchlist item", "Record found?"),
    ("DB: save asset", "Record found?"),
    ("DB: save debt", "Record found?"),
    ("DB: save stock holding", "Record found?"),
    ("Record found?", "DB: load dashboard data", 0),
    ("Record found?", "Reply 404: record not found", 1),
    ("DB: load dashboard data", "Reply: dashboard data"),
)

write("dashboard_api", "YAdashboardApi01", "YourAnalyst 7 – Dashboard page: load and save data", nodes, connections)
