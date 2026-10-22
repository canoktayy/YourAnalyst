-- 005: "sell my AAPL into Revolut" goes through the same confirmation step. Already in schema.sql.
alter table pending_confirmation drop constraint if exists pending_confirmation_record_type_check;
alter table pending_confirmation add constraint pending_confirmation_record_type_check
    check (record_type in ('asset', 'holding', 'liability', 'sale'));
