#!/usr/bin/env bash
# Resend a saved chat payload to the n8n webhook.
#   ./send.sh 01_liability             -> event_id from the file (resend = replay test)
#   RUN=t2 ./send.sh 01_liability      -> event_id suffixed with "-t2" (a fresh message)
#   TEST=1 ./send.sh 01_liability      -> test URL (after "Listen for test event" in the editor)
# Demo 6 (replay): send the same payload twice with the same RUN. The second reply is
# the stored one ("replayed": true) and no new row is written.
set -euo pipefail
cd "$(dirname "$0")"
base="${N8N_URL:-http://localhost:5678}"
path=$([ "${TEST:-}" = 1 ] && echo webhook-test || echo webhook)
body=$(python3 -c "import json,sys; d=json.load(open('payloads/$1.json')); r='${RUN:-}'; d['event_id']+=('-'+r if r else ''); print(json.dumps(d))")
curl -sS -X POST "$base/$path/ya-chat" \
  -H "Content-Type: application/json" \
  -H "X-YA-Token: ${YA_TOKEN:-demo-token-change-me}" \
  --data "$body"
echo
