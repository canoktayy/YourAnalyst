-- YourAnalyst database schema
-- Plain Postgres: runs on the local dev container and on Supabase (SQL Editor) unchanged.
-- Money is numeric (never float). Every user-owned row carries user_id.

create extension if not exists pgcrypto;

-- ---------------------------------------------------------------------------
-- Users
-- ---------------------------------------------------------------------------
create table if not exists app_user (
    id             uuid primary key default gen_random_uuid(),
    display_name   text not null,
    base_currency  char(3) not null default 'USD',
    -- Webhook auth: the dashboard sends this token in the X-YA-Token header.
    -- Stored as sha256 hex so the raw token never sits in the database.
    api_token_hash text not null unique,
    created_at     timestamptz not null default now()
);

-- ---------------------------------------------------------------------------
-- Financial records
-- ---------------------------------------------------------------------------
-- Cash balances and other manually valued assets (car, gold, deposit, ...).
create table if not exists asset (
    id              uuid primary key default gen_random_uuid(),
    user_id         uuid not null references app_user(id) on delete cascade,
    kind            text not null check (kind in ('cash', 'other')),
    name            text not null,
    amount          numeric(20, 4) not null check (amount >= 0),
    currency        char(3) not null,
    color           text,               -- dashboard dot colour (UI only)
    source_event_id text,               -- request that created it (null = dashboard form)
    created_at      timestamptz not null default now(),
    updated_at      timestamptz not null default now()
);

-- Stock positions. Cost basis is per share, in cost_currency.
create table if not exists holding (
    id              uuid primary key default gen_random_uuid(),
    user_id         uuid not null references app_user(id) on delete cascade,
    symbol          text not null,
    name            text,               -- display name, optional
    quantity        numeric(20, 6) not null check (quantity > 0),
    cost_per_share  numeric(20, 6) not null check (cost_per_share >= 0),
    cost_currency   char(3) not null default 'USD',
    source_event_id text,
    created_at      timestamptz not null default now(),
    updated_at      timestamptz not null default now()
);

-- Symbols the user follows without owning them. Prices come from the same quote table.
-- position orders the list in the dashboard (drag and drop puts a row between two others).
create table if not exists watchlist (
    id         uuid primary key default gen_random_uuid(),
    user_id    uuid not null references app_user(id) on delete cascade,
    symbol     text not null,
    name       text,
    position   double precision not null default extract(epoch from now()),
    created_at timestamptz not null default now(),
    unique (user_id, symbol)
);

-- Money the user owes ("I owe Benjamin EUR 1,000").
create table if not exists liability (
    id              uuid primary key default gen_random_uuid(),
    user_id         uuid not null references app_user(id) on delete cascade,
    counterparty    text not null,
    amount          numeric(20, 4) not null check (amount > 0),
    currency        char(3) not null,
    note            text,
    source_event_id text,
    created_at      timestamptz not null default now(),
    updated_at      timestamptz not null default now()
);

-- ---------------------------------------------------------------------------
-- Request tracking: idempotency + confirmations
-- ---------------------------------------------------------------------------
-- One row per incoming message. (user_id, event_id) is unique, so a replayed
-- delivery hits the conflict and n8n returns the stored response instead of
-- writing again.
create table if not exists request_log (
    id           uuid primary key default gen_random_uuid(),
    user_id      uuid not null references app_user(id) on delete cascade,
    event_id     text not null,
    channel      text not null default 'web' check (channel in ('web', 'form', 'discord', 'schedule', 'manual')),
    input_text   text,
    intent       text,
    status       text not null default 'received'
                 check (status in ('received', 'awaiting_confirmation', 'done', 'failed')),
    response     jsonb,
    error        text,
    attempts     int not null default 0,
    execution_id text,                -- n8n execution; the Error Workflow marks it failed
    created_at   timestamptz not null default now(),
    updated_at   timestamptz not null default now(),
    unique (user_id, event_id)
);

create index if not exists request_log_execution on request_log (execution_id);

-- A record the AI interpreted but the user has not yet confirmed.
-- "yes" in chat -> finance sub-workflow writes payload to the target table.
create table if not exists pending_confirmation (
    id          uuid primary key default gen_random_uuid(),
    user_id     uuid not null references app_user(id) on delete cascade,
    request_id  uuid not null references request_log(id) on delete cascade,
    record_type text not null check (record_type in ('asset', 'holding', 'liability', 'sale')),  -- sale: sell shares into a cash account
    payload     jsonb not null,
    status      text not null default 'pending'
                check (status in ('pending', 'confirmed', 'cancelled', 'expired')),
    expires_at  timestamptz not null default now() + interval '15 minutes',
    created_at  timestamptz not null default now(),
    resolved_at timestamptz
);

-- At most one open confirmation per user keeps "yes"/"no" unambiguous.
create unique index if not exists one_pending_per_user
    on pending_confirmation (user_id) where status = 'pending';

-- A Signal Lab scan being set up in the chat, one per user. Each message fills in what it
-- can; the assistant asks for the rest, runs the scan when complete, then deletes the row.
create table if not exists scan_draft (
    user_id    uuid primary key references app_user(id) on delete cascade,
    mode       text check (mode in ('rank', 'single')),
    universe   text check (universe in ('demo', 'dow30', 'nasdaq100', 'sp500', 'nyse')),
    symbol     text,
    indicators text[],
    top        int,
    updated_at timestamptz not null default now()
);

-- ---------------------------------------------------------------------------
-- Market data (written by the valuation-refresh workflow)
-- ---------------------------------------------------------------------------
create table if not exists quote (
    symbol     text primary key,
    price      numeric(20, 6),           -- null = unavailable
    change_pct numeric(10, 4),           -- day change % from the provider
    currency   char(3) not null default 'USD',
    as_of      timestamptz,              -- market timestamp from the provider
    source     text not null,
    fetched_at timestamptz not null default now()
);

-- usd_per_unit: how many USD one unit of `currency` is worth (USD itself = 1).
-- Converting X -> Y: amount * usd_per_unit(X) / usd_per_unit(Y).
create table if not exists fx_rate (
    currency     char(3) primary key,
    usd_per_unit numeric(20, 10) not null check (usd_per_unit > 0),
    as_of        timestamptz not null,
    source       text not null,
    fetched_at   timestamptz not null default now()
);

-- Fixed demo universe for "top five" screening.
create table if not exists universe_symbol (
    symbol text primary key,
    name   text not null
);

-- ---------------------------------------------------------------------------
-- Analysis history
-- ---------------------------------------------------------------------------
create table if not exists analysis (
    id          uuid primary key default gen_random_uuid(),
    user_id     uuid not null references app_user(id) on delete cascade,
    request_id  uuid references request_log(id) on delete set null,
    kind        text not null check (kind in ('single', 'rank')),
    symbol      text,                     -- null for rank
    indicators  text[] not null,
    metrics     jsonb not null,           -- computed values + per-indicator contributions
    score       numeric(8, 3),
    explanation text,                     -- LLM text, grounded in metrics
    data_as_of  timestamptz,
    created_at  timestamptz not null default now()
);

-- ---------------------------------------------------------------------------
-- Error log (Error Workflow writes here)
-- ---------------------------------------------------------------------------
create table if not exists error_log (
    id            bigserial primary key,
    workflow_name text,
    node_name     text,
    execution_id  text,
    execution_url text,           -- link to the failed run in the n8n editor
    event_id      text,
    message       text not null,
    alert         text,           -- one line: workflow, node, message, execution URL
    created_at    timestamptz not null default now()
);

-- ---------------------------------------------------------------------------
-- Valuation views (read by the dashboard; numbers come from SQL, not the LLM)
-- ---------------------------------------------------------------------------
-- Stale = quote older than 3 days (covers weekends).
create or replace view v_position as
select
    h.id,
    h.user_id,
    h.symbol,
    h.quantity,
    h.cost_per_share,
    h.cost_currency,
    q.price                                   as last_price,
    q.currency                                as price_currency,
    q.as_of                                   as price_as_of,
    case
        when q.price is null then 'unavailable'
        when q.as_of < now() - interval '3 days' then 'stale'
        else 'ok'
    end                                       as price_status,
    h.quantity * q.price                      as position_value,
    h.quantity * h.cost_per_share             as cost_basis,
    h.quantity * q.price
        - h.quantity * h.cost_per_share
              * fc.usd_per_unit / fp.usd_per_unit  as unrealised_pl,  -- in price_currency
    coalesce(h.name, us.name)                 as name,
    q.change_pct
from holding h
left join universe_symbol us on us.symbol = h.symbol
left join quote   q  on q.symbol = h.symbol
left join fx_rate fc on fc.currency = h.cost_currency
left join fx_rate fp on fp.currency = q.currency;

-- Every line item converted to the user's base currency.
create or replace view v_line_item as
with items as (
    select a.user_id, a.kind as category, a.name as label, a.amount, a.currency, 1 as sign
    from asset a
    union all
    select p.user_id, 'stock', p.symbol, p.position_value, p.price_currency, 1
    from v_position p
    union all
    select l.user_id, 'liability', l.counterparty, l.amount, l.currency, -1
    from liability l
)
select
    i.user_id,
    i.category,
    i.label,
    i.amount,
    i.currency,
    u.base_currency,
    i.sign * i.amount * fx_from.usd_per_unit / fx_base.usd_per_unit as base_amount,
    (i.amount is null or fx_from.usd_per_unit is null or fx_base.usd_per_unit is null) as unpriced,
    -- FX older than 4 days (ECB publishes on weekdays, so Friday's rate is normal until
    -- Monday afternoon): the refresh kept the last good rate after a failed fetch.
    -- USD is the fixed 1.0 anchor and never goes stale.
    ((fx_from.source <> 'fixed' and fx_from.as_of < now() - interval '4 days')
     or (fx_base.source <> 'fixed' and fx_base.as_of < now() - interval '4 days')) as fx_stale
from items i
join app_user u           on u.id = i.user_id
left join fx_rate fx_from on fx_from.currency = i.currency
left join fx_rate fx_base on fx_base.currency = u.base_currency;

create or replace view v_net_worth as
select
    u.id                                                         as user_id,
    u.base_currency,
    coalesce(sum(li.base_amount) filter (where li.base_amount > 0), 0)  as total_assets,
    coalesce(-sum(li.base_amount) filter (where li.base_amount < 0), 0) as total_liabilities,
    coalesce(sum(li.base_amount), 0)                             as net_worth,
    count(*) filter (where li.unpriced)                          as unpriced_items,
    count(*) filter (where li.fx_stale)                          as fx_stale_items
from app_user u
left join v_line_item li on li.user_id = u.id
group by u.id, u.base_currency;
