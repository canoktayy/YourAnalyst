-- 004: watchlist in the database (chat + dashboard). Already part of schema.sql for fresh installs.
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
