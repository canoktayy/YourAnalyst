"""YA – Signal scan: the Signal Lab page's entry point.

POST /ya-scan  {mode: "rank", universe: demo|dow30|nasdaq100|sp500|nyse, indicators: [...]} -> ranked
               {mode: "single", symbol: "AAPL", indicators: [...]} -> one stock
Same path as the chat: it calls the "YA – Analyse US stock" sub-workflow, so the numbers
come from the Python analysis API, the LLM only explains the top results, and every run
is saved to the analysis table. The page shows the full ranked table plus that summary.
"""
from lib import IS_TRUE, NOT_EMPTY, auth, call_workflow, code, connect, if_node, respond, webhook, write

VALIDATE = r"""// Only the four indicators of the proposal; at least one. Single mode needs a ticker.
const body = $('POST /ya-scan').first().json.body ?? {};
const ALLOWED = ['rsi', 'macd', 'ma_trend', 'volume'];
const errors = [];
const mode = body.mode === 'single' ? 'single' : 'rank';
const indicators = [...new Set((Array.isArray(body.indicators) ? body.indicators : [])
  .map(x => String(x).toLowerCase()).filter(x => ALLOWED.includes(x)))];
if (!indicators.length) errors.push(`indicators: choose at least one of ${ALLOWED.join(', ')}`);
const symbol = String(body.symbol ?? '').trim().toUpperCase().replace(/^\$/, '');
const UNIVERSES = ['demo', 'dow30', 'nasdaq100', 'sp500', 'nyse'];
const universe = UNIVERSES.includes(body.universe) ? body.universe : 'demo';
if (mode === 'single' && !/^[A-Z][A-Z0-9.\-]{0,9}$/.test(symbol)) errors.push('symbol: enter a valid US ticker, e.g. AAPL');
return [{ json: { valid: errors.length === 0, errors, mode, user_id: $('Auth user').first().json.user_id,
  request_id: null, event_id: `scan-${$execution.id}`,
  // top = whole universe: the page shows every ranked row (paged), the LLM sees the top 5.
  analysis: { symbol: mode === 'single' ? symbol : null, indicators, universe, top: 3000 } } }];
"""

RESPONSE = ('={{ { ok: $json.status !== "failed", status: $json.status, intent: $json.intent, '
            'reply: $json.reply, data: $json.data ?? null } }}')

nodes = [
    webhook("POST /ya-scan", [0, 200], "POST", "ya-scan"),
    auth("Auth user", [220, 200]),
    if_node("If: authenticated?", [440, 200], "={{ $json.user_id }}", NOT_EMPTY),
    respond("Respond 401", [660, 380], '={{ { "ok": false, "error": "Missing or invalid X-YA-Token" } }}', 401),
    code("Validate scan", [660, 180], VALIDATE),
    if_node("If: scan valid?", [880, 180], "={{ $json.valid }}", IS_TRUE),
    respond("Respond 400", [1100, 360], '={{ { "ok": false, "error": $json.errors.join("; ") } }}', 400),
    call_workflow("Analyse US stock", [1100, 160], "YAanalyseStock01"),
    respond("Respond scan", [1320, 160], RESPONSE),
]

connections = connect(
    ("POST /ya-scan", "Auth user"),
    ("Auth user", "If: authenticated?"),
    ("If: authenticated?", "Validate scan", 0),
    ("If: authenticated?", "Respond 401", 1),
    ("Validate scan", "If: scan valid?"),
    ("If: scan valid?", "Analyse US stock", 0),
    ("If: scan valid?", "Respond 400", 1),
    ("Analyse US stock", "Respond scan"),
)

write("signal_scan", "YAsignalScan0001", "YA – Signal scan", nodes, connections)
