#!/usr/bin/env bash
# Build workflow JSON from workflows/src/*.py, import into n8n, publish, restart.
#   ./deploy.sh                      all workflows
#   ./deploy.sh dashboard_api ...    only these
# Re-import overwrites the workflow with the same id: edits made in the n8n UI are lost,
# so port UI changes back into src/ first.
set -euo pipefail
cd "$(dirname "$0")"
names=("$@")
[ ${#names[@]} -eq 0 ] && names=($(ls src/*.py | xargs -n1 basename | sed 's/\.py$//' | grep -v '^lib$'))

dc() { docker compose -f ../docker-compose.yml "$@"; }

# Import everything first, then publish: publishing in the same loop as the imports
# occasionally left a workflow unpublished.
for n in "${names[@]}"; do
  (cd src && python3 "$n.py")
  dc exec -T n8n n8n import:workflow --input="/workflows/$n.json" 2>&1 | grep -E "Successfully|Error|error" || true
done
for n in "${names[@]}"; do
  id=$(python3 -c "import json; print(json.load(open('$n.json'))['id'])")
  dc exec -T n8n n8n publish:workflow --id="$id" 2>&1 | grep -E "^Publishing|rror" || true
done

dc restart n8n >/dev/null
# healthz answers before the webhooks are registered; wait until ya-chat stops returning 404.
for i in $(seq 1 60); do
  code=$(curl -s -o /dev/null -w "%{http_code}" -X POST localhost:5678/webhook/ya-chat -d '{}' || true)
  [ "$code" != 000 ] && [ "$code" != 404 ] && echo "n8n up, webhooks registered" && exit 0
  sleep 2
done
echo "n8n did not come back" >&2; exit 1
