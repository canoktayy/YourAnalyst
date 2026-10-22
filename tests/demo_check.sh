#!/usr/bin/env bash
# Pre-demo check: is everything the demo needs running and in the demo state?
#   tests/demo_check.sh          checks only (read-only)
#   tests/demo_check.sh --chat   also sends one chat message end to end (adds one request_log row)
# Exit code = number of failed checks. Warnings do not fail.
cd "$(dirname "$0")/.."
set -a; . ./.env; set +a
TOKEN=${YA_TOKEN:-demo-token-change-me}
fails=0
ok()   { printf '  \033[32m✓\033[0m %s\n' "$1"; }
bad()  { printf '  \033[31m✗\033[0m %s\n      → %s\n' "$1" "$2"; fails=$((fails+1)); }
warn() { printf '  \033[33m!\033[0m %s\n      → %s\n' "$1" "$2"; }
code() { curl -s -o /dev/null -w '%{http_code}' --max-time "${2:-5}" "$1"; }
psql_q() { docker compose exec -T db psql -U "$DB_USER" -d youranalyst -Atc "$1" 2>/dev/null; }

echo "Services"
if docker info >/dev/null 2>&1; then ok "Docker is running"; else bad "Docker is not running" "open -a Docker, then docker compose up -d"; fi
for s in db n8n analysis; do
  st=$(docker compose ps --format '{{.Service}} {{.State}}' 2>/dev/null | awk -v s=$s '$1==s{print $2}')
  [ "$st" = running ] && ok "container $s" || bad "container $s is '${st:-missing}'" "docker compose up -d"
done
[ "$(code http://localhost:5678/healthz)" = 200 ] && ok "n8n (5678)" || bad "n8n does not answer" "docker compose restart n8n"
[ "$(code http://localhost:8000/health)" = 200 ] && ok "analysis API (8000)" || bad "analysis API does not answer" "docker compose restart analysis"
[ "$(code http://localhost:3500/)" = 200 ] && ok "web server (3500)" \
  || bad "web server is down" "open /Applications/YourAnalyst.app (or: python3 web/server.py)"

echo "AI model"
models=$(curl -s --max-time 5 http://localhost:1234/v1/models)
if [ -z "$models" ]; then bad "LM Studio server is not running" "~/.lmstudio/bin/lms server start"
elif grep -q "\"$YA_LLM_MODEL\"" <<<"$models"; then ok "LM Studio serves $YA_LLM_MODEL"
else bad "$YA_LLM_MODEL is not available in LM Studio" "~/.lmstudio/bin/lms load $YA_LLM_MODEL"; fi
loaded=$(~/.lmstudio/bin/lms ps 2>/dev/null)
if [ -n "$loaded" ] && ! grep -qi "${YA_LLM_MODEL%%-instruct*}" <<<"$loaded"; then
  warn "model not loaded in memory yet" "the first message will be slow; ~/.lmstudio/bin/lms load $YA_LLM_MODEL"
fi

echo "Workflows"
active=$(docker compose exec -T n8n n8n list:workflow --active=true 2>/dev/null | grep -c '|YourAnalyst [0-9] –')
[ "$active" -ge 9 ] && ok "$active YourAnalyst workflows published" || bad "only $active of 9 YA workflows published" "workflows/deploy.sh"
dash=$(curl -s --max-time 15 http://localhost:3500/ya/dashboard)
grep -q '"ok":\s*true\|"ok": true' <<<"$dash" && ok "dashboard webhook answers" || bad "dashboard webhook failed" "workflows/deploy.sh, then check n8n executions"

echo "Data"
read -r accounts debts holdings watch <<<"$(psql_q "select (select count(*) from asset), (select count(*) from liability), (select count(*) from holding), (select count(*) from watchlist)" | tr '|' ' ')"
if [ "$accounts/$debts/$holdings/$watch" = "3/3/5/5" ]; then ok "demo data: 3 accounts, 3 debts, 5 holdings, 5 watchlist"
else warn "data differs from the demo seed ($accounts accounts, $debts debts, $holdings holdings, $watch watchlist)" "db/reset_demo.sh"; fi
read -r nw stale unpriced <<<"$(psql_q "select round(net_worth::numeric, 2), fx_stale_items, unpriced_items from v_net_worth" | tr '|' ' ')"
[ -n "$nw" ] && ok "net worth ${nw} (base currency)" || bad "v_net_worth is empty" "db/reset_demo.sh"
[ "${stale:-0}" = 0 ] && ok "FX rates fresh" || warn "$stale items use stale FX" "run the FX refresh: curl -X POST localhost:5678/webhook/ya-refresh -H 'X-YA-Token: $TOKEN'"
[ "${unpriced:-0}" = 0 ] && ok "every item priced" || warn "$unpriced items have no price" "check the watchlist/holdings symbols"
age=$(psql_q "select round(extract(epoch from now() - max(fetched_at)) / 60) from quote")
[ -n "$age" ] && [ "${age%.*}" -le 30 ] && ok "quotes updated ${age%.*} min ago" || warn "quotes are ${age:-?} min old" "the 10 s refresh is not running: check YourAnalyst 8 – Refresh prices and exchange rates"
q=$(curl -s --max-time 10 "http://localhost:3500/api/quote?symbol=AAPL")
grep -q '"ok": *true' <<<"$q" && ok "Yahoo quotes reachable" || warn "Yahoo quote proxy failed" "network/Yahoo rate limit; prices stay at the last values"
errs=$(psql_q "select count(*) from error_log where created_at > now() - interval '1 hour'")
last_alert=$(psql_q "select alert from error_log order by created_at desc limit 1")
[ "${errs:-0}" = 0 ] && ok "no errors in the last hour" || warn "$errs errors in error_log in the last hour" "latest: $last_alert"

if [ "${1:-}" = "--chat" ]; then
  echo "Chat (end to end, ~10-20 s)"
  t0=$(date +%s)
  r=$(curl -s --max-time 120 -X POST http://localhost:3500/ya/chat -H 'Content-Type: application/json' \
       -d "{\"event_id\":\"check-$(date +%s)\",\"text\":\"What's my net worth?\"}")
  if grep -q '"status": *"done"' <<<"$r"; then ok "chat answered in $(( $(date +%s) - t0 )) s"
  else bad "chat did not answer" "${r:0:200}"; fi
fi

echo
[ $fails = 0 ] && echo "All checks passed." || echo "$fails check(s) failed."
exit $fails
