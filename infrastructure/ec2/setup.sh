#!/bin/sh
set -eu
cd "$(dirname "$0")/../.."
command -v docker >/dev/null
docker version >/dev/null
# docker-compose.yml uses env_file {path, required}: Compose v2.24 or newer.
version=$(docker compose version --short | sed 's/^v//')
echo "Docker Compose $version"
echo "$version" | awk -F. '{ exit !($1 > 2 || ($1 == 2 && $2 >= 24)) }' || {
  echo 'Docker Compose >= 2.24 is required (install the docker-compose-plugin).' >&2; exit 1; }
if [ ! -f .env ]; then
  echo 'Create a host-only root .env from .env.example before deploying.' >&2
  exit 1
fi
value() { grep "^$1=" .env | tail -n 1 | cut -d= -f2- | tr -d '"'"'"; }
if [ "$(value APP_ENV)" = production ]; then
  [ -n "$(value AWS_REGION)" ] || { echo 'Set AWS_REGION in .env for production.' >&2; exit 1; }
  [ "$(value MODEL_PROVIDER)" = openai ] || { echo 'Production requires MODEL_PROVIDER=openai.' >&2; exit 1; }
  # Keys come from AWS Secrets Manager (contract-risk/prod/api-keys) through the Config service.
  [ -n "$(value API_KEYS_SECRET_ID)" ] || { echo 'Set API_KEYS_SECRET_ID (AWS Secrets Manager) in .env.' >&2; exit 1; }
  for name in OPENAI_API_KEY TAVILY_API_KEY QDRANT_URL QDRANT_API_KEY SUPABASE_URL SUPABASE_PUBLISHABLE_KEY               SUPABASE_SECRET_KEY LANGFUSE_PUBLIC_KEY LANGFUSE_SECRET_KEY MCP_API_TOKEN INTERNAL_API_TOKEN; do
    grep -q "^$name=" .env && { echo "Remove $name from .env: API keys live only in Secrets Manager." >&2; exit 1; }
  done
  for name in CONFIG_TOKEN_WORKER CONFIG_TOKEN_INPUT_PROCESSING CONFIG_TOKEN_AGENT_1 CONFIG_TOKEN_AGENT_2               CONFIG_TOKEN_AGENT_3 CONFIG_TOKEN_LANGGRAPH CONFIG_TOKEN_MONITORING CONFIG_TOKEN_MCP; do
    [ "$(value "$name" | wc -c)" -gt 32 ] || { echo "Set $name in .env (random, >= 32 characters)." >&2; exit 1; }
  done
fi
[ -d models ] && [ -n "$(ls -A models 2>/dev/null)" ] || {
  echo 'models/ is missing or empty: run python scripts/download_models.py (or copy models/ with the repo).' >&2; exit 1; }
docker compose config --quiet
echo 'Docker and Compose configuration available. AWS resources and IAM must already be configured.'
