"""YA – Error handler: the Error Workflow of every other YA workflow.

Runs when an execution fails after its node-level retries. Writes one error_log row and,
for a chat request, marks request_log.status = 'failed' (matched on the n8n execution id
stored by the router), so a replay of that event_id returns a readable failure instead
of hanging in 'received'.
Test: open it in the editor and run it with the Error Trigger's sample data, or stop
Postgres-dependent services and send a chat message.
"""
from lib import code, connect, node, sql, write

DESCRIBE = r"""// Error Trigger payload: { execution: { id, url, error, lastNodeExecuted, mode }, workflow: { id, name } }
// A trigger-level failure has { trigger: { error } } and no execution id.
const e = $json;
const err = e.execution?.error ?? e.trigger?.error ?? {};
const message = String(err.message ?? 'Unknown error').slice(0, 2000)
  + (err.description ? ` (${String(err.description).slice(0, 500)})` : '');
const workflow_name = e.workflow?.name ?? null;
const node_name = e.execution?.lastNodeExecuted ?? err.node?.name ?? null;
const execution_id = e.execution?.id != null ? String(e.execution.id) : null;
// n8n fills execution.url from N8N_EDITOR_BASE_URL; build it ourselves if it is missing.
const execution_url = e.execution?.url
  ?? (execution_id && e.workflow?.id ? `${$env.N8N_EDITOR_BASE_URL || 'http://localhost:5678'}/workflow/${e.workflow.id}/executions/${execution_id}` : null);
// One-line alert, stored in error_log and shown by tests/demo_check.sh.
const alert = `${workflow_name ?? 'Unknown workflow'} failed at "${node_name ?? '?'}": ${message.slice(0, 200)} | ${execution_url ?? 'no execution URL'}`;
return [{ json: { workflow_name, node_name, execution_id, execution_url, alert, message } }];
"""

LOG = """-- One error_log row; the matching chat request (if any) becomes 'failed' with a stored reply,
-- so the dedup replay path answers with this message.
with l as (select $1::jsonb as l),
logged as (
    insert into error_log (workflow_name, node_name, execution_id, execution_url, event_id, message, alert)
    select l->>'workflow_name', l->>'node_name', l->>'execution_id', l->>'execution_url',
           (select event_id from request_log where execution_id = l->>'execution_id' limit 1),
           l->>'message', l->>'alert'
    from l
    returning id, event_id),
failed as (
    update request_log r set
        status     = 'failed',
        error      = (select l->>'message' from l),
        response   = coalesce(r.response, jsonb_build_object(
                       'ok', true, 'status', 'failed', 'intent', r.intent, 'event_id', r.event_id,
                       'reply', 'Something went wrong on our side and nothing was saved. Please try again in a minute.')),
        updated_at = now()
    where r.execution_id = (select l->>'execution_id' from l)
      and r.status <> 'done'
    returning r.id)
select (select id from logged) as error_log_id,
       (select event_id from logged) as event_id,
       (select count(*) from failed) as requests_failed;"""

nodes = [
    node("On workflow error", "n8n-nodes-base.errorTrigger", 1, [0, 0], {}),
    code("Describe error", [220, 0], DESCRIBE),
    sql("Log error + fail request", [440, 0], LOG, "={{ JSON.stringify($json) }}"),
]

connections = connect(
    ("On workflow error", "Describe error"),
    ("Describe error", "Log error + fail request"),
)

write("error_handler", "YAerrorHandler01", "YA – Error handler", nodes, connections)
