"""Helpers for building n8n workflow JSON from Python.

Each workflows/src/<name>.py builds one workflow and writes workflows/<name>.json.
Run them all with workflows/deploy.sh, which also imports and publishes them.
"""
import json
import pathlib
import uuid

OUT_DIR = pathlib.Path(__file__).resolve().parent.parent

PG = {"postgres": {"id": "yaPgLocal0000001", "name": "YA Postgres (local)"}}
RETRY = {"retryOnFail": True, "maxTries": 3, "waitBetweenTries": 2000}


def node(name, type_, version, position, params, **extra):
    n = {
        "parameters": params,
        "name": name,
        # Stable ids keep re-imports from creating duplicate nodes.
        "id": str(uuid.uuid5(uuid.NAMESPACE_URL, f"youranalyst/{name}")),
        "type": type_,
        "typeVersion": version,
        "position": position,
    }
    n.update(extra)
    return n


def code(name, position, js, **extra):
    return node(name, "n8n-nodes-base.code", 2, position, {"jsCode": js}, **extra)


def sql(name, position, query, replacement=None, **extra):
    """Postgres executeQuery. Pass parameters as ONE expression (usually a JSON string)."""
    options = {"queryReplacement": replacement} if replacement else {}
    return node(name, "n8n-nodes-base.postgres", 2.6, position,
                {"operation": "executeQuery", "query": query, "options": options},
                credentials=PG, **RETRY, **extra)


def webhook(name, position, method, path):
    return node(name, "n8n-nodes-base.webhook", 2, position,
                {"httpMethod": method, "path": path, "responseMode": "responseNode", "options": {}},
                webhookId=str(uuid.uuid5(uuid.NAMESPACE_URL, f"youranalyst/webhook/{path}")))


# Token check shared by every authenticated webhook: one JSON param, so a missing
# header cannot drop the parameter. Returns user_id, or no row (alwaysOutputData).
AUTH_SQL = """select id as user_id, base_currency
from app_user
where api_token_hash = encode(digest($1::jsonb->>'token', 'sha256'), 'hex');"""
AUTH_PARAM = "={{ JSON.stringify({ token: $json.headers['x-ya-token'] ?? null }) }}"


def auth(name, position):
    return sql(name, position, AUTH_SQL, AUTH_PARAM, alwaysOutputData=True)


def respond(name, position, body_expr, status=200):
    return node(name, "n8n-nodes-base.respondToWebhook", 1.1, position,
                {"respondWith": "json", "responseBody": body_expr,
                 "options": {"responseCode": status}})


def if_node(name, position, left, operator, right=None):
    """Single-condition If. operator e.g. {"type": "string", "operation": "notEmpty", "singleValue": True}."""
    cond = {"id": str(uuid.uuid5(uuid.NAMESPACE_URL, f"youranalyst/if/{name}")),
            "leftValue": left, "operator": operator}
    if right is not None:
        cond["rightValue"] = right
    return node(name, "n8n-nodes-base.if", 2.2, position, {
        "conditions": {"options": {"caseSensitive": True, "typeValidation": "loose", "version": 2},
                       "conditions": [cond], "combinator": "and"},
        "looseTypeValidation": True, "options": {}})


def switch(name, position, value_expr, outputs):
    """Switch on a string value; outputs = list of (output name, value)."""
    rules = [{
        "conditions": {
            "options": {"caseSensitive": True, "typeValidation": "strict", "version": 2},
            "conditions": [{"id": str(uuid.uuid5(uuid.NAMESPACE_URL, f"youranalyst/sw/{name}/{key}")),
                            "leftValue": value_expr, "rightValue": val,
                            "operator": {"type": "string", "operation": "equals"}}],
            "combinator": "and"},
        "renameOutput": True, "outputKey": key,
    } for key, val in outputs]
    return node(name, "n8n-nodes-base.switch", 3.2, position,
                {"rules": {"values": rules}, "options": {"fallbackOutput": "extra"}})


IS_TRUE = {"type": "boolean", "operation": "true", "singleValue": True}
NOT_EMPTY = {"type": "string", "operation": "notEmpty", "singleValue": True}


def connect(*pairs):
    """pairs: (source, target) or (source, target, source_output_index)."""
    conns = {}
    for p in pairs:
        src, dst, idx = (p + (0,))[:3]
        outs = conns.setdefault(src, {"main": []})["main"]
        while len(outs) <= idx:
            outs.append([])
        outs[idx].append({"node": dst, "type": "main", "index": 0})
    return conns


# Every workflow reports unhandled failures to "YA – Error handler" (error_handler.py).
ERROR_WORKFLOW_ID = "YAerrorHandler01"


def write(file_name, wf_id, name, nodes, connections, settings=None):
    base = {"executionOrder": "v1"}
    if wf_id != ERROR_WORKFLOW_ID:
        base["errorWorkflow"] = ERROR_WORKFLOW_ID
    wf = {"id": wf_id, "name": name, "active": False, "nodes": nodes,
          "connections": connections, "pinData": {},
          "settings": {**base, **(settings or {})}}
    path = OUT_DIR / f"{file_name}.json"
    path.write_text(json.dumps(wf, indent=2, ensure_ascii=False) + "\n")
    print(f"wrote {path.name}")


# ── AI + sub-workflow helpers ────────────────────────────────────────────────
# The chat model is OpenAI-compatible: LM Studio locally, OpenRouter (free model) for
# the demo. Switching = change the "YA LLM" credential URL/key and YA_LLM_MODEL in .env.
LLM_CRED = {"openAiApi": {"id": "yaLlmCred0000001", "name": "YA LLM"}}


def chat_model(name, position, temperature=0):
    return node(name, "@n8n/n8n-nodes-langchain.lmChatOpenAi", 1.2, position, {
        "model": {"__rl": True, "mode": "id", "value": "={{ $env.YA_LLM_MODEL }}"},
        # YA_LLM_EXTRA_BODY: provider-specific request fields. Local LM Studio with a thinking
        # model (bonsai-27b) needs {"reasoning_effort":"none"}, otherwise it reasons for
        # minutes and the explanation times out. Set it to {} for OpenRouter.
        "options": {"temperature": temperature, "timeout": 120000, "maxRetries": 2,
                    "extraBody": "={{ $env.YA_LLM_EXTRA_BODY || '{}' }}"},
    }, credentials=LLM_CRED)


def llm_chain(name, position, system, text_expr, **extra):
    """Basic LLM Chain without an output parser: the Code node after it validates the JSON,
    which survives small/free models better than a strict parser."""
    return node(name, "@n8n/n8n-nodes-langchain.chainLlm", 1.7, position, {
        "promptType": "define", "text": text_expr, "hasOutputParser": False,
        "messages": {"messageValues": [{"message": system}]},
    }, **RETRY, **extra)


# Defined inputs of each sub-workflow: (name, type). The caller maps exactly these
# fields and the sub-workflow's trigger declares them, so the contract is visible in n8n.
# "any" where a field can be null (request_id for Signal Lab, the payload of confirm/cancel).
_BASE = [("mode", "string"), ("user_id", "string"), ("request_id", "any"), ("event_id", "string")]
SUB_INPUTS = {
    "YArecordEntry001": _BASE + [("record", "any")],
    "YAanalyseStock01": _BASE + [("analysis", "object")],
    "YAwatchlist00001": _BASE + [("watchlist", "object")],
    "YAanswerQuest001": _BASE + [("question", "any")],
}


def call_workflow(name, position, workflow_id):
    """Execute Sub-workflow with its defined inputs (SUB_INPUTS), each mapped from the same field."""
    inputs = SUB_INPUTS[workflow_id]
    return node(name, "n8n-nodes-base.executeWorkflow", 1.2, position, {
        "source": "database",
        "workflowId": {"__rl": True, "value": workflow_id, "mode": "id"},
        "workflowInputs": {
            "mappingMode": "defineBelow",
            "value": {f: f"={{{{ $json.{f} }}}}" for f, _ in inputs},
            "matchingColumns": [],
            "schema": [{"id": f, "displayName": f, "required": False, "defaultMatch": False,
                        "display": True, "canBeUsedToMatch": True, "type": t} for f, t in inputs],
            "attemptToConvertTypes": False,
            "convertFieldsToString": False,
        },
        "options": {},
    })


def sub_trigger(name, position, workflow_id):
    """Execute Workflow Trigger that declares the sub-workflow's inputs."""
    return node(name, "n8n-nodes-base.executeWorkflowTrigger", 1.1, position, {
        "inputSource": "workflowInputs",
        "workflowInputs": {"values": [{"name": f, "type": t} for f, t in SUB_INPUTS[workflow_id]]},
    })


def connect_ai(conns, model, chain):
    conns.setdefault(model, {})["ai_languageModel"] = [[{"node": chain, "type": "ai_languageModel", "index": 0}]]
    return conns


# Labels of the Signal Lab rules (analysis/indicators.py), keyed by contribution. The reply
# shows them next to every ranked stock and the LLMs must describe stocks with them.
RULE_LABELS_JS = r"""const RULE_LABELS = {
  rsi:      { 1: 'RSI oversold (below 30)', 0.5: 'RSI below 50', 0: 'RSI 50-70', '-1': 'RSI overbought (above 70)' },
  macd:     { 1: 'MACD positive and rising', 0.5: 'MACD positive but fading', 0: 'MACD negative but improving', '-1': 'MACD negative and falling' },
  ma_trend: { 1: 'uptrend', 0.5: 'above SMA200 only', 0: 'no clear trend', '-1': 'downtrend' },
  volume:   { 1: 'heavy volume on an up day', 0: 'normal volume', '-1': 'heavy volume on a down day' },
};
"""

# Wording check for ranked stocks (or one analysed stock with single=true), shared by "Explain results" (analyse_stock) and
# "Check narration" (chat_router). The text is read sentence by sentence (and line by
# line); words about a stock must match its contributions. A group such as "AMZN, JPM and
# MSFT score 68.8 with neutral RSI" is checked for every stock in the group.
WORDING_CHECK_JS = r"""function wordingProblems(text, ranked, single = false) {
  const problems = [];
  if (!text || !ranked?.length) return problems;
  const esc = s => s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  const IND = /rsi|macd|trend|volume|sma|overbought|oversold/;
  // "not in a clear uptrend", "isn't oversold": negated phrases say nothing about the stock.
  const NEG = /\b(not|no|never|isn['’]t|aren['’]t|without|lacks?)\s+(yet\s+)?(a\s+|an\s+|in\s+a\s+)?(clear\s+|clean\s+|strong\s+|unusually\s+)?(up-?trend|down-?trend|overbought|oversold|(heavy|high|strong)\s+volume)/g;
  const contradicts = (seg, c) => (seg = seg.replace(NEG, ' '),
    (/\bup-?trend/.test(seg) && c.ma_trend !== 1) ||
    (/\bdown-?trend/.test(seg) && c.ma_trend !== -1) ||
    (/overbought/.test(seg) && c.rsi !== -1) ||
    (/oversold/.test(seg) && c.rsi !== 1) ||
    (/neutral to strong|50-70|50–70/.test(seg) && c.rsi !== 0) ||
    (/rsi (over|above) 50/.test(seg) && c.rsi > 0) ||
    (/rsi below 50|below neutral/.test(seg) && c.rsi <= 0) ||
    (/(negative|falling|bearish)[^,;]{0,12}macd|macd[^,;]{0,25}(negative|falling|bearish)/.test(seg) && c.macd > 0) ||
    (/(heavy|high|strong) volume/.test(seg) && !c.volume) ||
    // a stock described by its indicators must not hide a negative one
    (IND.test(seg) && ((c.rsi === -1 && !/overbought|above 70/.test(seg)) ||
                       (c.macd === -1 && !/macd/.test(seg)) ||
                       (c.ma_trend === -1 && !/downtrend/.test(seg)) ||
                       (c.volume === -1 && !/down day/.test(seg)))));
  // One analysed stock: the whole text is about it, symbol or not.
  if (single) {
    if (contradicts(text.toLowerCase(), ranked[0].contributions ?? {}))
      problems.push(`${ranked[0].symbol}: wording does not match its indicators`);
    return problems;
  }
  const units = text.split(/\n+|(?<=[.!?])\s+(?=[A-Z0-9])/);
  for (const unit of units) {
    const hits = ranked.map(x => ({ x, i: unit.search(new RegExp(`\\b${esc(x.symbol)}\\b`)) }))
      .filter(h => h.i >= 0).sort((a, b) => a.i - b.i);
    const segs = hits.map((h, j) => unit.slice(h.i, j + 1 < hits.length ? hits[j + 1].i : unit.length).toLowerCase());
    for (let j = 0; j < hits.length; j++) {
      // "AMZN, JPM and MSFT ...": a stock without its own words shares the next description
      let k = j;
      while (k < hits.length - 1 && !IND.test(segs[k])) k++;
      const c = hits[j].x.contributions ?? {};
      if (contradicts(segs[k], c)) problems.push(`${hits[j].x.symbol}: wording does not match its indicators`);
    }
  }
  return [...new Set(problems)];
}
"""
