-- 2026-10-22: the Error Workflow also stores the failed run's URL and a one-line alert.
alter table error_log add column if not exists execution_url text;
alter table error_log add column if not exists alert text;
