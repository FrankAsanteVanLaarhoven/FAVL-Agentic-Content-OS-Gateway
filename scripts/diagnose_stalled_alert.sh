#!/usr/bin/env bash
# OBS-001 — is OutboxStalledWhileWriting incorrect, redundant, or merely slow?
#
# The alert never fired during a thirteen-minute injected stall, while the
# expression's arithmetic says it should evaluate true at roughly ten. This
# samples both sides of the expression through a controlled stall so the
# answer comes from measurement rather than from reading the rule.
#
# Prints one row per sample: the left operand, the right operand, whether the
# full expression yields anything, and the rule's reported state.
set -uo pipefail

cd "$(dirname "$0")/.."
DC="docker compose --env-file .env -f deploy/docker-compose.yml"
PROM=http://localhost:9092
SAMPLES=${SAMPLES:-26}
INTERVAL=${INTERVAL:-30}

q() {
  curl -fsS "$PROM/api/v1/query" --data-urlencode "query=$1" 2>/dev/null | python3 -c "
import json, sys
try:
    r = json.load(sys.stdin)['data']['result']
except Exception:
    print('ERR'); raise SystemExit
print(';'.join(f\"{x['metric'].get('service','-')}={x['value'][1]}\" for x in r) or 'EMPTY')
"
}

rule_state() {
  curl -fsS "$PROM/api/v1/rules" 2>/dev/null | python3 -c "
import json, sys
groups = json.load(sys.stdin)['data']['groups']
for g in groups:
    for r in g['rules']:
        if r.get('name') == 'OutboxStalledWhileWriting':
            print(f\"{r.get('state','?')}/{r.get('health','?')}\", end='')
            if r.get('lastError'):
                print(f\" ERR:{r['lastError'][:40]}\", end='')
            print()
            raise SystemExit
print('absent')
"
}

echo "== stalling the orchestrator publisher and staging a backlog =="
OUTBOX_PUBLISHER_ENABLED=false $DC up -d --no-deps orchestrator >/dev/null 2>&1
until [ "$(curl -s -o /dev/null -w '%{http_code}' http://localhost:9080/health/ready)" = "200" ]; do
  sleep 3
done

TOKEN=$($DC exec -T connector-registry python -c "
import httpx
print(httpx.post('http://keycloak:8080/realms/favl/protocol/openid-connect/token',
    data={'client_id':'agentic-content-os','client_secret':'replace-me',
          'username':'demo','password':'demo-password','grant_type':'password'},
    timeout=20).json()['access_token'])" | tr -d '\r\n')

RUN="obs$(date +%s)"
$DC exec -T connector-registry python - "$RUN" "$TOKEN" <<'PYEOF' >/dev/null 2>&1 || true
import asyncio
import sys

import httpx

RUN, TOKEN = sys.argv[1], sys.argv[2]


async def main() -> None:
    async with httpx.AsyncClient(timeout=20) as c:
        for i in range(20):
            await c.post(
                "http://apisix:9080/v1/agents",
                headers={"Authorization": f"Bearer {TOKEN}"},
                json={"name": f"{RUN}-{i:03d}", "connector_ids": []},
            )


asyncio.run(main())
PYEOF

printf '\n%-6s | %-28s | %-34s | %-9s | %s\n' \
  "t+s" "LEFT pending>0" "RIGHT rate(success[5m])>0" "EXPR" "RULE"
printf -- '-%.0s' {1..120}; echo

for i in $(seq 1 "$SAMPLES"); do
  T=$(( (i - 1) * INTERVAL ))
  LEFT=$(q 'sum by (service) (favl_outbox_pending) > 0')
  RIGHT=$(q 'sum by (service) (rate(favl_outbox_publish_total{result="success"}[5m])) > 0')
  EXPR=$(q 'sum by (service) (favl_outbox_pending) > 0 unless sum by (service) (rate(favl_outbox_publish_total{result="success"}[5m])) > 0')
  printf '%-6s | %-28s | %-34s | %-9s | %s\n' \
    "$T" "${LEFT:0:28}" "${RIGHT:0:34}" "$([ "$EXPR" = EMPTY ] && echo no || echo YIELDS)" "$(rule_state)"
  sleep "$INTERVAL"
done

echo
echo "== restoring the publisher =="
$DC up -d --no-deps orchestrator >/dev/null 2>&1
