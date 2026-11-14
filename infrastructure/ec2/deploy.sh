#!/bin/sh
set -eu
cd "$(dirname "$0")/../.."
sh infrastructure/ec2/setup.sh
docker compose build
if [ "$(grep '^APP_ENV=' .env | tail -n 1 | cut -d= -f2-)" = production ]; then
  sh infrastructure/ec2/preflight.sh
fi
docker compose up -d --wait --wait-timeout 180
docker compose ps
