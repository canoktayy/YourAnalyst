# YourAnalyst: Build Plan

Based on the course brief *Session 3 / Choose your subject* (Level 3, advanced) and `../YourAnalyst_Proposal.pdf`.

## 1. Course rules this plan follows

| Course rule | How YourAnalyst meets it |
|---|---|
| **A trigger you control**: fire it on demand | Web chat Webhook (approved) + a saved test payload in `tests/payloads/` resent with `tests/send.sh`. A Manual Trigger sits next to every Schedule Trigger. |
| **Data you can reach**: public API, no paid plan | Prices: Yahoo chart endpoint, called directly from n8n (no dependency on `server.py`). Fallback if blocked: a small fixed CSV/JSON of demo quotes, so the block still "reads data" and the architecture is unchanged. FX: free no-key API (e.g. frankfurter.app, ECB rates). |
| **Boring failure**: nobody is hurt if it runs twice | A tracking and research tool only. No trades, no money movement, no messages to real people. Duplicate runs are blocked by `request_log` (user_id, event_id). |
| **Level 3** = sub-workflows, error handling, multi-step logic, state, "survives a dependency going down" | 2 sub-workflows, Error Workflow, retries, persisted state (records, pending confirmations, request status), dedup. |
| **Build L2 first**, then add retries / error workflow / dedup | Phases A → B below. If time runs out after Phase A, it still demos as a working Level 2. |
| **Check what you extracted** before the real work | `If: parse valid?` → false branch replies with a usage/clarifying message, never falls through. |
| Message trigger: **not** the n8n *Chat Message* trigger | Standard **Webhook** node from the website chat (teacher approved). The Chat Trigger is never used. |

## 2. Minimum technical requirements → concrete nodes

| Requirement | Node(s) | Workflow |
|---|---|---|
| If or Set node | `If: token valid?`, `If: parse valid?`, `If: confirmation pending?`, `Set: build reply` | Main router, Finance |
| Code node | `Code: validate AI JSON + normalise symbol/currency`, `Code: format reply`, `Code: validate quotes` | Main router, Refresh |
| Router / Switch | `Route by intent` → record / confirm / list / analyse / rank / help | Main router |
| API / MCP connection | HTTP Request → Python analysis API, price proxy, FX API | Analysis, Refresh |
| Message trigger | Webhook `POST /webhook/ya-chat` (approved substitute for Telegram/Discord) | Main router |
| Database persistence | Postgres node (local Docker now, Supabase later) | All |
| AI node | LLM Chain with Structured Output Parser (intent extraction); LLM Chain (explanation grounded in computed metrics) | Main router, Analysis |
| Sub-workflow | Execute Workflow → `YA 2 – Sub: save a financial record`, `YA 3 – Sub: analyse stocks with indicators`, `YA 4 – Sub: add or remove watchlist stocks`, `YA 5 – Sub: answer a question about my money`, each with defined inputs (`lib.SUB_INPUTS`: mode, user_id, request_id, event_id + its payload) | Main router |
| Error handling + retries | Retry On Fail (3 tries, 2 s) on every HTTP/DB node; error outputs on AI parse; `YA 9 – Error handler: log every failure` Error Workflow → `error_log` (workflow, node, execution URL, one-line alert) + `request_log.status='failed'` | All |

## 3. Workflows

1. **YA 1 – Chat: understand and route a message** (Webhook) — auth → dedup → AI parse → If valid → Switch → sub-workflow → reply.
2. **YA 2 – Sub: save a financial record** (sub-workflow) — create `pending_confirmation` → on "yes" write asset/holding/liability → return new net worth from `v_net_worth`.
3. **YA 3 – Sub: analyse stocks with indicators** (sub-workflow) — HTTP to the Python API (RSI, MACD, MA trend, volume ratio + score contributions) → save `analysis` → LLM explanation using only those numbers.
4. **YA 8 – Refresh prices and exchange rates** (Schedule every 30 min + Manual Trigger) — tracked symbols → quotes → FX → validate → upsert `quote` / `fx_rate`.
5. **YA 4 – Sub: add or remove watchlist stocks** (sub-workflow) — the LLM only copies the names/tickers the user wrote (a 7B model guessed palantir → PAL); each one is searched on Yahoo (as written, without spaces, doubled letters collapsed) → every ticker is checked against a real Yahoo quote → `watchlist` + `quote` rows. No confirmation (not a financial record; adding twice is harmless).
6. **YA 5 – Sub: answer a question about my money** (sub-workflow) — second layer of the two-layer AI design. The router's LLM only picks the tool (`question` / `what_if`); here SQL reads a portfolio snapshot, code computes every figure (weights, P/L, what-if net worth), the LLM words it, and `Check answer numbers (fallback)` rejects any number not in the computed facts (then the plain table is shown).
7. **YA 6 – Signal Lab page: scan stocks** (Webhook `ya-scan`) — the Signal Lab page: validates the chosen indicators (RSI, MACD, MA trend, volume) and mode (20-stock universe or one ticker), then calls `YA 3 – Sub: analyse stocks with indicators`; the page shows the full ranked table with per-indicator values and contributions, data timestamp/source, and the AI summary.
8. **YA 9 – Error handler: log every failure** (Error Trigger) — log to `error_log`, mark the request failed (`request_log.execution_id`).

## 4. Build order (cut line built in)

**Phase A: Level 2 happy path, end to end**
1. ✅ Valuation refresh (no LLM needed): quotes and FX in the DB, `v_net_worth` correct. Tested 2026-10-14, including an invalid symbol → `unavailable`.
1b. ✅ Dashboard data layer: `YA 7 – Dashboard page: load and save data` (GET `ya-dashboard`, POST `ya-record`), `ya-refresh` webhook on the valuation workflow, `server.py` proxy `/ya/*` (token added server-side), `app.js` accounts/debts/main portfolio read and write Postgres. Tested 2026-10-14: auth 401, validation 400, unknown id 404, UI create/delete → DB → net worth.
2. ✅ Python analysis API (`analysis/api.py`, `analysis/indicators.py`, Docker service `analysis:8000`): `POST /analyze`, `POST /rank`, transparent rules, same RSI/MACD definitions as `bist100/feature_engineering.py`. The LightGBM ranking is not used (on the cut list). Unit tests run in the image build.
3. ✅ `YA 1 – Chat: understand and route a message` + sub-workflows `YA 2 – Sub: save a financial record`, `YA 3 – Sub: analyse stocks with indicators`. LLM: OpenAI-compatible credential "YA LLM" → LM Studio (`host.docker.internal:1234`), model `YA_LLM_MODEL` = `qwen2.5-7b-instruct` (MLX 4-bit, ~4 GB, 1–12 s per message). Tried: bonsai-27b (1-bit, invented an amount: "Apple 1.00 USD"), qwen2.5-14b (accurate but 9 GB; with Docker on a 16 GB Mac it swapped, 1–4 min per message). `YA_LLM_EXTRA_BODY` passes provider-specific fields (`{"reasoning_effort":"none"}` for thinking models, `{}` otherwise). OpenRouter for a hosted demo: change the credential URL/key and the model id. Re-tested 2026-10-14 with qwen2.5-7b: demo 1-6 pass, plus watchlist add/remove ("data dog", "cloudfflare", unknown names), multi-record and mixed messages → clarify, "net worth in TRY".
4. ✅ Dashboard (English only): assistant embedded as the right-hand column under Allocation (`web/chat.js`, `#ya-chat-slot`; goals and market sentiment removed) + net worth / holdings / liabilities cards that read the DB. Tested 2026-10-14 in the browser: TR record → confirm buttons → "hayır" cancels; TR analysis with grounded explanation.

**Phase B: Level 3 robustness**
5. ✅ Dedup on `event_id` (replay returns the stored response). Built into the router.
6. ✅ Two-layer AI (2026-10-16). Every chat reply is worded by the narrator LLM from the tool's computed result (`Collect tool result` → `AI 2: write the reply` → `Check reply numbers (fallback)`: numbers must exist in the result, else the tool text is used; trailing filler questions and markdown are trimmed). Tools: orchestrator LLM → JSON tool choice (`record`, `sell`, `question`, `what_if`, `watchlist`, `list`, `analyse`, `rank`); it sees owned symbols and account names, never amounts. `sell` = confirmed sale: code computes quantity × latest price and FX, confirm → shares removed FIFO, proceeds added to the account. Tested: "net worth if AAPL reaches 1000", "if palantir drops 20%", "how much of my portfolio is PLTR", sale proposal + FIFO SQL in a rolled-back transaction.
6b. ✅ Confirmation step for every financial write; clarifying question on ambiguous input. Grounding check in `Check AI JSON against the message`: every amount/quantity/price in a record must appear in the user's message (1,000 · 1.000 · 2.5k forms accepted), otherwise the bot asks for it. One record per message; several → clarify.
7. ✅ Retries on HTTP/DB nodes, invalid-JSON recovery, Error Workflow. `YA 9 – Error handler: log every failure` (Error Trigger) is the `errorWorkflow` of every other workflow (set in `lib.write`). It writes `error_log` and, matched on `request_log.execution_id`, marks the chat request `failed` with a stored readable reply, so the chat's "Tekrar dene" (same `event_id`) shows it. FX fetch has an error output: after 3 tries it logs to `error_log` and keeps the last rates (`v_line_item.fx_stale` / `v_net_worth.fx_stale_items` once older than 3 days). Tested 2026-10-14: `analysis` table renamed → DB: save analysis fails → 2 `error_log` rows, request `failed`, replay returns the readable reply; Frankfurter URL broken → logged, EUR rate unchanged.
8. ✅ Failure demo: stop the Python API → bounded retries → readable reply + `error_log` row.

**Cut first, in order** (from the proposal): conversation memory → 20-stock scan (keep single-stock) → alerts/ML/Monte Carlo → extra asset categories.
**Never cut:** totals, confirmed entry, persistence, one grounded analysis, web chat, sub-workflows, retries + dedup.

## 5. Demo script (each line has a payload in `tests/payloads/`)

| # | Input | Expected |
|---|---|---|
| 1 | `I owe Benjamin EUR 1,000` | Reply: ask yes or no → "yes" → saved, net worth updated |
| 2 | `I own 10 AAPL shares at USD 180` | Confirmation → saved; price, value, unrealised P/L |
| 3 | `Analyse AAPL using RSI and MACD` | Values + data timestamp + explanation tied to those two |
| 4 | `Top five in the demo universe` | Ranked list, rules, reason per score |
| 5 | `I owe him some money` | Clarifying question, nothing written |
| 6 | Payload 1 resent with the same `event_id` | Same reply, no second row |
| 7 | Python API stopped, then `Analyse MSFT` | 3 retries, readable failure, `error_log` row |

## 6. Before the demo

Everything below is done (2026-10-17). The day itself: follow `DEMO.md` (rehearsal sheet: timings, exact messages, fallbacks, likely questions).

- `tests/demo_check.sh [--chat]`: containers, n8n, analysis API, web server, LM Studio model, 9 published workflows, dashboard webhook, demo data = seed, FX freshness, quote age, Yahoo, recent errors; `--chat` sends one real message.
- `db/reset_demo.sh` before and after the demo (also clears chat memory).
- Desktop app: `/Applications/YourAnalyst.app` (`desktop/build_app.sh`) starts Docker, LM Studio and the web server itself.
- LLM: LM Studio on this Mac. For a hosted demo, switch the credential to OpenRouter (see step 3).
- Public tunnel: the `server.py` proxy adds the token itself, so anyone with a tunnel URL can use the dashboard whatever the token is. Share a tunnel only with the teacher and stop it afterwards. n8n (5678) is never exposed.

### Guard fixes found in the demo rehearsal (2026-10-17)

- **Currency:** "YA 5 – Sub: answer a question about my money" showed the USD position value as "107,065 EUR". The LLM now sees every amount as text with its currency (`"107,065.00 USD"`, `value_in_base`), and both `Check answer numbers (fallback)` and `Check reply numbers (fallback)` reject an amount whose currency differs from the computed one.
- **Indicator wording:** in rankings the narrator called stocks "uptrend" or "neutral RSI" when they were not (only the top 5 had reasons; the rest were guessed). Every ranked row now carries the rule's label (`RSI overbought (above 70)`, `above SMA200 only`…), the explain LLM gets labels instead of rule texts, and a shared `wordingProblems()` check (lib.py) runs on the explanation and the narration: contradictions, groups described as one, and hidden negative indicators are rejected (negations like "not in a clear uptrend" are ignored). Single-stock analysis is checked too.
- **Actions:** after a confirmed new debt the narrator said "paid off". The saved reply now says what happened ("Added a new debt…") and `Check reply numbers (fallback)` rejects actions the tool did not report (paid off, sold, removed…).
- **Claims beyond the result:** "the rest of the Dow 30 also score 75" is rejected.
- **Stale FX:** the fixed USD rate (as_of = seed date) made every USD item "stale" after 3 days. The `fixed` source is now excluded and the window is 4 days, so Friday's ECB rate isn't flagged on Mondays (migration 008).
- Tests: `python3 tests/test_checks.py` (real wrong answers from testing as cases).

## 7. Signal Lab vs. proposal (2026-10-16)

Kept (proposal §04): single-stock analysis, fixed 20-stock universe, selectable RSI / MACD / MA trend / volume ratio, transparent rules with per-indicator contributions, "top" = highest scores in the universe, data source and timestamp shown, LLM explains computed results only (number-checked).
Removed (proposal "cut first" #2 and #3): S&P 500 / Nasdaq / all-market scans, LightGBM ranking, Monte Carlo, Kelly sizing, backtests, paper P&L tracking, BIST. Files deleted: `analysis/bist100/`, `web/run_*.py`, `web/get_signal.py`, pywebview launcher, `.venv`.

### Scan universes (extension, 2026-10-16)

The proposal's minimum scope is the fixed Demo 20; on request Signal Lab and the chat's ranking tool also scan Dow 30, Nasdaq 100, S&P 500 and every NYSE-listed common stock (2,183, from nasdaqtrader.com `otherlisted.txt`, without ETFs/preferreds/warrants/units). Lists live in `universe_member` (`db/universes.sql`, migration 006). The analysis API caches daily bars 15 min, fetches 12 in parallel and backs off on Yahoo 429. Measured: Dow 30 8 s, S&P 500 7–15 s (481 of 502 priced; 21 index changes since the list), NYSE 28 s (2,180 of 2,183). The page pages results 100 at a time with a search box; only the top 5 go to the LLM and the top 50 to the `analysis` history.

Live data: n8n refreshes portfolio/watchlist quotes every 10 s (FX every 30 min); the dashboard re-reads everything every 10 s.

### Signal Lab in the chat + conversation memory (2026-10-17)

Scope change, on request: the proposal listed conversation memory as "cut first". Now:
- **Memory:** the orchestrator sees the last 6 finished exchanges (from `request_log`) and any scan being set up, so short answers ("NVDA", "dow, all four") are read as answers to its last question.
- **`scan` tool:** analysing one stock or ranking a universe, as in Signal Lab. Details collected across messages live in `scan_draft` (migration 007, 20 min); the assistant asks only for what is missing (mode, universe or ticker, indicators; top defaults to 5), then runs `YA 3 – Sub: analyse stocks with indicators` and the narrator words the result. "cancel" / "never mind" drops the draft.
- **Grounding:** every scan detail (mode, universe, ticker, indicators, top) counts only if the current message says it, so a 7B model cannot copy choices from older turns; record/sale numbers may come from the user's recent messages. Scan questions must keep the app's options (no "P/E ratio"); "confirm/save" sentences are removed unless something awaits confirmation.
- Tested: "can you analyse a stock for me?" → ticker+indicators asked → "NVDA" → indicators asked → "all of them" → analysis; "show me the top 3 stocks" → universe+indicators asked → "dow, all four" → Dow 30 top 3; "nasdaq" → "rsi and trend" → Nasdaq 100 ranking; "never mind" cancels.
