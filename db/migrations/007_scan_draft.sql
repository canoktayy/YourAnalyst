-- 007: chat scans set up over several messages. Already part of schema.sql.
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
