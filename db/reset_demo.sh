#!/usr/bin/env bash
# Put the dashboard back to the default demo state from db/seed.sql:
# deletes the demo user's accounts, debts, holdings, watchlist, pending confirmations,
# analyses and chat requests, then re-runs the seed. Quotes, FX and error_log are kept.
# The dashboard's local UI file (web/userdata.json) is reset too. Reload open tabs afterwards.
#   ./db/reset_demo.sh
set -euo pipefail
cd "$(dirname "$0")/.."
set -a; . ./.env; set +a
docker compose exec -T db psql -v ON_ERROR_STOP=1 -q -U "$DB_USER" -d youranalyst <<'SQL'
begin;
delete from pending_confirmation; delete from analysis; delete from request_log;
delete from holding; delete from liability; delete from asset; delete from watchlist;
commit;
SQL
docker compose exec -T db psql -v ON_ERROR_STOP=1 -q -U "$DB_USER" -d youranalyst < db/seed.sql
python3 - <<'PY'
import json, uuid
p = "web/userdata.json"
d = json.load(open(p))
pid = uuid.uuid4().hex[:13]
d.update({"accounts": [], "debts": [], "watchlist": [], "priceHistory": {}, "calendarEvents": [], "goals": [],
          "portfolios": [{"id": pid, "name": "Main Portfolio", "isMain": True, "stocks": [], "portSnaps": []}],
          "activePortId": pid})
d.pop("serverTotals", None)
json.dump(d, open(p, "w"))
PY
curl -s -X POST localhost:5678/webhook/ya-refresh -H "X-YA-Token: ${YA_TOKEN:-demo-token-change-me}" >/dev/null || true
echo "Demo data restored. Reload the dashboard."
