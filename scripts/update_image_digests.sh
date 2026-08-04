#!/usr/bin/env bash
# Refresh the digest pins in deploy/docker-compose.yml.
#
# Pins exist so the stack that passed CI is byte-identical to the stack that
# runs. That guarantee only holds if the tag beside each digest is accurate,
# so this script re-resolves both together rather than editing digests alone.
set -euo pipefail

cd "$(dirname "$0")/.."
COMPOSE=deploy/docker-compose.yml

grep -oP 'image: \K[^@\s]+(?=@sha256:)' "$COMPOSE" | sort -u | while read -r ref; do
  echo "resolving $ref"
  docker pull --quiet "$ref" >/dev/null
  digest=$(docker inspect --format='{{index .RepoDigests 0}}' "$ref" | sed 's/.*@//')
  name=${ref%%:*}
  # Match on the repository name so the tag can change too.
  sed -i "s|image: ${name}:[^@]*@sha256:[a-f0-9]*|image: ${ref}@${digest}|" "$COMPOSE"
  echo "  -> ${digest}"
done

echo
echo "Updated. Review the diff, then rebuild:"
echo "  docker compose --env-file .env -f $COMPOSE up -d --build"
