-- 003: Error Workflow support + stale FX flag. Already part of schema.sql for fresh installs.
alter table request_log add column if not exists execution_id text;
create index if not exists request_log_execution on request_log (execution_id);
-- Then re-run db/schema.sql (idempotent) to replace v_line_item / v_net_worth with the fx_stale columns.
