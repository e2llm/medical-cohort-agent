#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

DATA_DIR="sample_data"
EMBED=1

usage() {
  cat <<'EOF'
Usage: bash scripts/demo_setup.sh [options]

Options:
  --no-embed        Skip embeddings (strict pass only, no kNN semantic matching)
  --data-dir DIR    NDJSON directory to bulk index (default: sample_data)
  -h, --help        Show help

Environment:
  Reads configuration from .env (see .env.example).
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --no-embed) EMBED=0; shift ;;
    --data-dir) DATA_DIR="${2:-}"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage; exit 2 ;;
  esac
done

if [[ ! -f .env ]]; then
  echo ".env not found. Run: cp .env.example .env"
  echo "Edit .env (set ELASTIC_PASSWORD, KIBANA_PASSWORD, OPENAI_API_KEY) then re-run."
  exit 1
fi

set -a; source .env; set +a

require_var() { [[ -n "${!1:-}" ]] || { echo "Error: $1 not set in .env" >&2; exit 1; }; }

require_var ELASTIC_PASSWORD
require_var KIBANA_PASSWORD

ES_PORT="${ES_PORT:-9200}"
KIBANA_PORT="${KIBANA_PORT:-5601}"
OLLAMA_URL="${OLLAMA_URL:-http://ollama:11434}"
OLLAMA_HOST_URL="${OLLAMA_HOST_URL:-http://localhost:11434}"
EMBED_MODEL="${EMBED_MODEL:-qllama/multilingual-e5-large}"

COMPOSE=()
if command -v podman-compose >/dev/null 2>&1; then
  COMPOSE=(podman-compose)
elif command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
  COMPOSE=(docker compose)
elif command -v docker-compose >/dev/null 2>&1; then
  COMPOSE=(docker-compose)
else
  echo "Need podman-compose or docker compose." >&2
  exit 1
fi

echo "Starting Elastic Stack + Ollama..."
"${COMPOSE[@]}" up -d

echo "Waiting for Elasticsearch on :$ES_PORT ..."
for _ in {1..60}; do
  if curl -sf -u "elastic:${ELASTIC_PASSWORD}" "http://localhost:${ES_PORT}/_cluster/health" >/dev/null; then
    break
  fi
  sleep 2
done

echo "Setting kibana_system password..."
curl -sf -u "elastic:${ELASTIC_PASSWORD}" -X POST \
  "http://localhost:${ES_PORT}/_security/user/kibana_system/_password" \
  -H 'Content-Type: application/json' \
  -d "{\"password\": \"${KIBANA_PASSWORD}\"}" >/dev/null

if [[ "$EMBED" -eq 1 ]]; then
  echo "Waiting for Ollama container..."
  for _ in {1..30}; do
    if curl -sf "${OLLAMA_HOST_URL}/api/tags" >/dev/null 2>&1; then
      break
    fi
    sleep 2
  done

  echo "Pulling E5 embedding model (${EMBED_MODEL}) — runs on CPU, may take a few minutes..."
  curl -sf "${OLLAMA_HOST_URL}/api/pull" -d "{\"name\": \"${EMBED_MODEL}\"}" | while IFS= read -r line; do
    status=$(echo "$line" | python3 -c "import sys,json; d=json.loads(sys.stdin.read()); print(d.get('status',''))" 2>/dev/null)
    [[ -n "$status" ]] && printf "\r  %s" "$status"
  done
  echo

  echo "Creating E5 embedding inference endpoint (e5_embedder)..."
  curl -sf -u "elastic:${ELASTIC_PASSWORD}" -X PUT \
    "http://localhost:${ES_PORT}/_inference/text_embedding/e5_embedder" \
    -H 'Content-Type: application/json' -d "{
    \"service\": \"openai\",
    \"service_settings\": {
      \"api_key\": \"not-needed\",
      \"url\": \"${OLLAMA_URL}/v1/embeddings\",
      \"model_id\": \"${EMBED_MODEL}\",
      \"dimensions\": 1024
    }
  }" >/dev/null
else
  echo "Embeddings disabled (--no-embed). Strict pass only."
fi

echo "Waiting for Kibana on :$KIBANA_PORT ..."
for _ in {1..90}; do
  if curl -sf -u "elastic:${ELASTIC_PASSWORD}" "http://localhost:${KIBANA_PORT}/api/status" >/dev/null; then
    break
  fi
  sleep 2
done

echo "Installing Python deps..."
python3 -m pip install -r requirements.txt >/dev/null

echo "Indexing data from ${DATA_DIR} ..."
python3 elasticsearch/generate_templates.py >/dev/null
if [[ "$EMBED" -eq 1 ]]; then
  python3 elasticsearch/bulk_index.py --data-dir "${DATA_DIR}" --recreate --embed
else
  python3 elasticsearch/bulk_index.py --data-dir "${DATA_DIR}" --recreate
fi

python3 agent/setup.py

echo
echo "Done."
echo "Kibana: http://localhost:${KIBANA_PORT} (user: elastic)"
echo "Agent Builder: open Kibana → Agent Builder → Medical Cohort Agent"
