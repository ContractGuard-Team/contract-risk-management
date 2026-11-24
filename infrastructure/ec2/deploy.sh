#!/bin/sh
set -eu
cd "$(dirname "$0")/../.."
sh infrastructure/ec2/setup.sh
docker compose build
if [ "$(grep '^APP_ENV=' .env | tail -n 1 | cut -d= -f2-)" = production ]; then
  sh infrastructure/ec2/preflight.sh
fi
docker compose up -d --wait --wait-timeout 180
# One test event with the deployed keys/region: a silent Langfuse shows up here, not after a day of missing logs.
docker compose exec -T monitoring python -m monitoring.langfuse_exporter || \
  echo 'WARNING: Langfuse does not receive events (reason above). Contracts are processed anyway.' >&2
docker compose ps
