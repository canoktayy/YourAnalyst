-- 002: fields the dashboard UI needs. Already part of schema.sql for fresh installs.
alter table asset   add column if not exists color text;
alter table holding add column if not exists name text;
alter table quote   add column if not exists change_pct numeric(10, 4);
