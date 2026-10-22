-- 2026-10-17: the fixed USD rate (as_of = when it was seeded) made every USD item look
-- stale after 3 days, and Friday's ECB rate looked stale on Mondays. Same view, new rule.
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
