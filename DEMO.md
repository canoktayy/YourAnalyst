# Demo day: rehearsal sheet

Slides: https://claude.ai/artifact/CjgwjHKvRD2HXyz68uWkM9 (speaker notes under every slide; share it with the teacher from its Share menu).
Total ≈ 9–10 min: slides 1–11 ≈ 6 min, live demo ≈ 3 min, questions after.

## 30 minutes before

1. Open **Docker** and **LM Studio**, and load `qwen2.5-7b-instruct` in LM Studio.
2. Reset the demo data. This also clears chat memory. It deletes anything added since:
   ```bash
   db/reset_demo.sh
   ```
3. Check everything. All lines must be ✓; `--chat` sends one real message and also warms up the model:
   ```bash
   tests/demo_check.sh --chat
   ```
4. Open **YourAnalyst** from the Dock. It starts anything that is still missing.
5. Open the slides in presenter view. Keep only the deck and the app open; the screen should be clean.

## Live demo: type exactly this

Each AI step takes about 5–15 s. Talk while it thinks (the "say" lines).

| # | Type in the YourAnalyst chat | You should see | Say |
|---|---|---|---|
| 1 | `I owe Alex 200 euros` | "You owe Alex 200.00 EUR. Shall I save this?…" | "Every write waits for a yes; the amount must appear in my own words." |
| 1b | `yes` | "You've added a new debt of 200.00 EUR to Alex… net worth 528,224.47 EUR". The **Debts** card updates | "SQL computed the new net worth, the AI only worded it." |
| 2 | `What's my net worth and how much debt do I have?` | 528,224.47 EUR and 46,700.00 EUR | "First AI chose the tool `list`, SQL did the totals." |
| 3 | `Can you analyse a stock for me?` | asks for ticker and indicators | "It remembers the conversation and asks for what's missing." |
| 3b | `NVDA` | asks which indicators | |
| 3c | `all of them` | NVDA 50/100: RSI below 50, MACD negative and falling, above SMA200 only, normal volume | "The indicators come from my Python API; a code check rejects any wording that doesn't match them." |
| 4 | **Signal Lab** tab → S&P 500 → tick RSI + MACD only → **Run scan** | about 500 stocks ranked in ~15 s, AI summary on top | "Same sub-workflow as the chat. The AI only explains the top 5." |

Numbers in the table are from the reset data on 17 Oct; prices move, so yours will differ slightly.

**Optional, if there is time:** the failure demo. Run `docker compose stop analysis`, then type `Analyse MSFT with RSI`. You get a readable "service unavailable" reply and an `error_log` row. Restart with `docker compose start analysis`.

## If something goes wrong

| Problem | Do this |
|---|---|
| Chat says "n8n unreachable" or nothing comes back | `tests/demo_check.sh` shows what is down. Meanwhile continue on the slides: they show the real n8n workflows. |
| First AI answer takes 30+ s | The model was not loaded. Say "the local model is warming up" and wait; later answers are faster. |
| The AI gives a plain list instead of a sentence | That is the guard working: the AI's text failed a check, so the computed result is shown. Say exactly that. |
| Wrong data on the dashboard | Run `db/reset_demo.sh`, then reload the app with ⌘R (or close and reopen it). |
| Yahoo prices don't update | The last prices stay in Postgres; nothing breaks. |

## Likely questions

- **Why a Webhook and not the Chat Trigger?** The brief bans the Chat Message trigger. The teacher approved a standard Webhook from my own web chat instead of Telegram or Discord.
- **How do you stop the AI inventing numbers?** Two layers. The first AI only returns JSON (which tool), and a Code node checks it; amounts must appear in the user's words. Tools compute everything with SQL, code or Python. The second AI only words the result, and Check narration rejects it if a number, a currency or an indicator label does not match. Then the plain computed result is shown.
- **Why a local 7B model?** It's free, private (my finances never leave the laptop) and fast enough (5–15 s). The weakness: it guesses, so every guess got a code check.
- **What makes it Level 3?** 4 sub-workflows, an Error Workflow on all 9 workflows, retries on every HTTP and DB node, failure branches, dedup on `event_id`, state in Postgres, confirmation before writes, stale-FX fallback.
- **Why 9 workflows?** One per job, matching the brief's sub-workflow requirement: router, record, analyse, watchlist, answer, scan page, dashboard API, valuation refresh, error handler.
- **How is the score computed?** Each indicator gives +1, +0.5, 0 or −1 by a fixed rule. Score = 50 + 50 × the average. For example, AAPL: RSI 0, MACD +1, trend +1, volume +1 → 87.5.
- **What happens if the same message arrives twice?** `request_log` has a unique (user, event_id). The second one replays the stored reply and nothing is written again.
- **What did you cut?** ML ranking, alerts and Monte Carlo, all on the proposal's own cut list.

## After the demo

```bash
db/reset_demo.sh
```
