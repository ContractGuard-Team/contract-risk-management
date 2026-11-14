#!/bin/sh
set -eu
cd "$(dirname "$0")/../.."
if [ "$#" -ne 1 ]; then
  echo 'Usage: sh infrastructure/ec2/rollback.sh EXISTING_IMAGE_TAG' >&2
  exit 1
fi
case "$1" in ''|*[!a-zA-Z0-9_.-]*) echo 'Invalid image tag' >&2; exit 1;; esac
export IMAGE_TAG="$1"
# Previous images must be available locally or in the configured registry.
docker compose up -d --no-build --wait --wait-timeout 180
docker compose ps
