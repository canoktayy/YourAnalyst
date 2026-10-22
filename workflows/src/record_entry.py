"""YA – Record financial entry (sub-workflow).

Input item: {mode: propose|confirm|cancel, user_id, request_id, event_id, record?}
Output item: {reply, status: awaiting_confirmation|done, intent, data}

propose  -> store the interpreted record in pending_confirmation and ask "yes/no"
confirm  -> consume the pending row and write it to asset/liability/holding in ONE
            statement (a second "yes" finds nothing pending, so no duplicate write)
cancel   -> mark the pending row cancelled
"""
from lib import code, connect, sql, sub_trigger, switch, write

START = "$('Start').first().json"
FMT = r"""
const money = (n, c) => new Intl.NumberFormat('en-US',
  { minimumFractionDigits: 2, maximumFractionDigits: 2 }).format(Number(n)) + ' ' + c;
const describe = (type, f) => {
  if (type === 'liability') return `Debt to ${f.counterparty}: ${money(f.amount, f.currency)}`;
  if (type === 'holding') return `Holding: ${f.quantity} ${f.symbol} at ${money(f.cost_per_share, f.cost_currency)} per share (cost ${money(f.quantity * f.cost_per_share, f.cost_currency)})`;
  if (type === 'sale') {
    const into = f.account_currency !== f.price_currency
      ? `${money(f.proceeds_account, f.account_currency)} (${money(f.proceeds, f.price_currency)})`
      : money(f.proceeds, f.price_currency);
    return `Sale: ${f.quantity} ${f.symbol}${f.all ? ' (all)' : ''} at ${money(f.price, f.price_currency)} per share`
      + (f.price_source === 'current' ? ' (latest price)' : '') + `\nAdd ${into} to ${f.account_name}`;
  }
  const kind = f.kind === 'cash' ? 'cash' : 'other';
  return `Asset (${kind}): ${f.name}: ${money(f.amount, f.currency)}`;
};
"""

CREATE_CONFIRMATION = """insert into pending_confirmation (user_id, request_id, record_type, payload)
select (j->>'user_id')::uuid, (j->>'request_id')::uuid, j->'record'->>'type', j->'record'->'fields'
from (select $1::jsonb as j) p
returning id, record_type, payload, expires_at;"""

CONFIRM_AND_SAVE = """-- Consume the pending row and insert the record in one statement.
with j as (select $1::jsonb as j),
p as (
    update pending_confirmation pc set status = 'confirmed', resolved_at = now()
    from j
    where pc.user_id = (j.j->>'user_id')::uuid and pc.status = 'pending' and pc.expires_at > now()
    returning pc.record_type, pc.payload, pc.user_id, j.j->>'event_id' as event_id),
a as (
    insert into asset (user_id, kind, name, amount, currency, source_event_id)
    select user_id, payload->>'kind', payload->>'name', (payload->>'amount')::numeric, payload->>'currency', event_id
    from p where record_type = 'asset' returning id),
l as (
    insert into liability (user_id, counterparty, amount, currency, note, source_event_id)
    select user_id, payload->>'counterparty', (payload->>'amount')::numeric, payload->>'currency', payload->>'note', event_id
    from p where record_type = 'liability' returning id),
h as (
    insert into holding (user_id, symbol, quantity, cost_per_share, cost_currency, source_event_id)
    select user_id, payload->>'symbol', (payload->>'quantity')::numeric, (payload->>'cost_per_share')::numeric,
           payload->>'cost_currency', event_id
    from p where record_type = 'holding' returning id),
-- Sale: take the shares out first-in first-out (whole rows deleted, the boundary row
-- reduced), then add the proceeds, already converted by the router, to the cash account.
lots as (
    select h.id, h.quantity,
           sum(h.quantity) over (order by h.created_at, h.id) as cum,
           (p.payload->>'quantity')::numeric as sold
    from holding h join p on p.record_type = 'sale'
    where h.user_id = p.user_id and h.symbol = p.payload->>'symbol'),
sold_rows as (
    delete from holding h using lots
    where h.id = lots.id and lots.cum <= lots.sold
    returning h.id),
part_row as (
    update holding h set quantity = lots.cum - lots.sold, updated_at = now()
    from lots
    where h.id = lots.id and lots.cum > lots.sold and lots.cum - lots.quantity < lots.sold
    returning h.id),
s as (
    update asset t set amount = t.amount + (p.payload->>'proceeds_account')::numeric, updated_at = now()
    from p
    where p.record_type = 'sale' and t.id = (p.payload->>'account_id')::uuid and t.user_id = p.user_id
    returning t.id)
select (select record_type from p) as record_type,
       (select payload from p) as payload,
       coalesce((select id from a), (select id from l), (select id from h), (select id from s)) as record_id,
       (select count(*) from sold_rows) + (select count(*) from part_row) as lots_changed;"""

LOAD_TOTALS = """with j as (select $1::jsonb as j)
select (select row_to_json(n) from v_net_worth n, j where n.user_id = (j.j->>'user_id')::uuid) as totals,
       (select row_to_json(p) from v_position p, j where p.id = (j.j->>'record_id')::uuid) as position;"""

CANCEL = """with c as (
    update pending_confirmation set status = 'cancelled', resolved_at = now()
    where user_id = ($1::jsonb->>'user_id')::uuid and status = 'pending'
    returning id)
select count(*)::int as cancelled from c;"""

CONFIRMATION_QUESTION = FMT + r"""
const row = $json;
return [{ json: {
  intent: 'record', status: 'awaiting_confirmation',
  reply: `Shall I save this?\n${describe(row.record_type, row.payload)}\nReply "yes" to confirm or "no" to cancel.`,
  data: { pending: { record_type: row.record_type, fields: row.payload, expires_at: row.expires_at } },
} }];
"""

SAVED_REPLY = FMT + r"""
const saved = $('Confirm and save').first().json;
if (!saved.record_id) {
  return [{ json: { intent: 'confirm', status: 'done', data: null,
    reply: 'There is nothing waiting for confirmation; it may have expired (15 min). Please send the record again.' } }];
}
const { totals, position: p } = $json;
// Say plainly what changed, so the narrator cannot read "Debt to Alex" as a repayment.
const DONE = { liability: 'Added a new debt (you owe more now)', holding: 'Added shares to your portfolio',
               sale: 'Sold shares; the proceeds went to your account', asset: 'Added an asset' };
const lines = [`Saved ✓ ${DONE[saved.record_type] ?? 'Saved'}: ${describe(saved.record_type, saved.payload)}`];
if (p) {
  if (p.price_status === 'unavailable') {
    lines.push('No price yet; it arrives with the next price refresh.');
  } else {
    const when = new Date(p.price_as_of).toISOString().slice(0, 16).replace('T', ' ') + ' UTC';
    lines.push(`Latest price ${money(p.last_price, p.price_currency)} (${when}${p.price_status === 'stale' ? ', stale' : ''}) · Value ${money(p.position_value, p.price_currency)} · Unrealised P/L ${p.unrealised_pl >= 0 ? '+' : ''}${money(p.unrealised_pl, p.price_currency)}`);
  }
}
lines.push(`Net worth: ${money(totals.net_worth, totals.base_currency)}`);
return [{ json: { intent: 'confirm', status: 'done', reply: lines.join('\n'),
  data: { record_type: saved.record_type, record_id: saved.record_id, totals, position: p } } }];
"""

CANCELLED_REPLY = FMT + r"""
const n = $json.cancelled;
return [{ json: { intent: 'cancel', status: 'done', data: { cancelled: n },
  reply: n ? "OK, I didn't save it."
           : 'There was nothing waiting to cancel.' } }];
"""

J = "={{ JSON.stringify(" + START + ") }}"

nodes = [
    sub_trigger("Start", [0, 200], "YArecordEntry001"),
    switch("Switch: mode", [220, 200], "={{ $json.mode }}",
           [("propose", "propose"), ("confirm", "confirm"), ("cancel", "cancel")]),
    # One open confirmation per user: a new proposal replaces the previous one.
    sql("Cancel open confirmation", [460, 0],
        "update pending_confirmation set status = 'cancelled', resolved_at = now()\n"
        "where user_id = ($1::jsonb->>'user_id')::uuid and status = 'pending';", J,
        alwaysOutputData=True, executeOnce=True),
    sql("Create confirmation", [680, 0], CREATE_CONFIRMATION, J),
    code("Confirmation question", [900, 0], CONFIRMATION_QUESTION),

    sql("Confirm and save", [460, 200], CONFIRM_AND_SAVE, J),
    sql("Load totals", [680, 200], LOAD_TOTALS,
        "={{ JSON.stringify({ user_id: " + START + ".user_id, record_id: $json.record_id }) }}"),
    code("Saved reply", [900, 200], SAVED_REPLY),

    sql("Cancel confirmation", [460, 400], CANCEL, J),
    code("Cancelled reply", [680, 400], CANCELLED_REPLY),
]

connections = connect(
    ("Start", "Switch: mode"),
    ("Switch: mode", "Cancel open confirmation", 0),
    ("Switch: mode", "Confirm and save", 1),
    ("Switch: mode", "Cancel confirmation", 2),
    ("Cancel open confirmation", "Create confirmation"),
    ("Create confirmation", "Confirmation question"),
    ("Confirm and save", "Load totals"),
    ("Load totals", "Saved reply"),
    ("Cancel confirmation", "Cancelled reply"),
)

write("record_entry", "YArecordEntry001", "YA – Record financial entry", nodes, connections)
