#!/bin/sh
# Runs once per `docker compose up` in the n8n-init container, before n8n starts:
# creates the two credentials from .env, imports every workflow JSON in /workflows and publishes it.
# A fresh machine needs only `docker compose up -d`; no clicks in the n8n UI.
set -eu

# Credentials: plain values here, n8n encrypts them with N8N_ENCRYPTION_KEY on import.
node -e '
const e = process.env;
const creds = [
  { id: "yaPgLocal0000001", name: "YA Postgres (local)", type: "postgres",
    data: { host: "db", port: 5432, database: "youranalyst", user: e.DB_USER, password: e.DB_PASSWORD, ssl: "disable" } },
  { id: "yaLlmCred0000001", name: "YA LLM", type: "openAiApi",
    data: { apiKey: e.YA_LLM_API_KEY || "lm-studio", url: e.YA_LLM_URL || "http://host.docker.internal:1234/v1" } },
];
require("fs").writeFileSync("/tmp/ya_creds.json", JSON.stringify(creds));
'
n8n import:credentials --input=/tmp/ya_creds.json
rm -f /tmp/ya_creds.json

# Import all first, then publish (same order as deploy.sh).
for f in /workflows/*.json; do
  n8n import:workflow --input="$f"
done
for f in /workflows/*.json; do
  id=$(node -p "require('$f').id")
  n8n publish:workflow --id="$id"
done
echo "YourAnalyst: credentials and $(ls /workflows/*.json | wc -l) workflows ready"
