#!/usr/bin/env bash
# Alert firing validation against the live stack.
#
# An alert that has never been watched fire is not known to work. Every other
# check in this repository ships with a demonstrated failure; alert rules were
# the last thing validated only by inspection — promtool proves an expression
# parses and scripts/check_alert_metrics.py proves it names a real metric, but
# neither proves it ever evaluates true, nor that it returns to normal.
#
# This drives one alert through a full cycle: healthy -> inject fault -> fires
# -> repair -> clears. The stalled-publisher case is chosen because it is the
# one whose earlier expression could never fire at all, for two independent
# reasons, and was reported green the entire time.
set -uo pipefail

cd "$(dirname "$0")/.."
DC="docker compose --env-file .env -f deploy/docker-compose.yml"
PROM=http://localhost:9092
PASS=0
FAIL=0

check() {
  if [ "$2" = "$3" ]; then
    printf '  PASS  %-52s %s\n' "$1" "$2"; PASS=$((PASS + 1))
  else
    printf '  FAIL  %-52s got=%s want=%s\n' "$1" "$2" "$3"; FAIL=$((FAIL + 1))
  fi
}

# Alert state as Prometheus reports it: firing | pending | inactive.
alert_state() {
  curl -fsS "$PROM/api/v1/alerts" 2>/dev/null | python3 -c "
import json, sys
try:
    alerts = json.load(sys.stdin)['data']['alerts']
except Exception:
    print('unavailable'); raise SystemExit
match = [a for a in alerts if a['labels'].get('alertname') == '$1']
print(match[0]['state'] if match else 'inactive')
"
}

# Waits for a state, returning what it saw. A timeout is reported as the last
# observed state rather than as success.
await_state() {
  local name=$1 want=$2 deadline=$((SECONDS + ${3:-240})) seen=inactive
  while [ $SECONDS -lt $deadline ]; do
    seen=$(alert_state "$name")
    [ "$seen" = "$want" ] && { echo "$seen"; return; }
    sleep 5
  done
  echo "$seen"
}

printf '\n== alert rules are loaded and healthy ==\n'
# `health == ok` is only true once a rule has evaluated at least once, so
# counting it straight after a reload measures timing rather than
# correctness. The property that matters is that none is in error.
BROKEN=$(curl -fsS "$PROM/api/v1/rules" 2>/dev/null | python3 -c "
import json, sys
rules = [r for g in json.load(sys.stdin)['data']['groups'] for r in g['rules']]
print(len([r for r in rules if r.get('health') == 'err']))
")
check "no rule is in an error state" "${BROKEN:-unavailable}" "0"

printf '\n== the running rules match the repository ==\n'
# OBS-001's real finding: the repository held the corrected rule and
# Prometheus held the previous one, because /-/reload was disabled. An edited
# file sat on disk passing promtool and check_alert_metrics.py while the
# running instance evaluated something else — every static check green
# against a rule that was not deployed.
DEPLOYED=$(curl -fsS "$PROM/api/v1/rules" 2>/dev/null | python3 -c "
import json, sys
names = sorted(
    r['name']
    for g in json.load(sys.stdin)['data']['groups']
    for r in g['rules']
    if r.get('name')
)
print(','.join(names))
")
INFILE=$(grep -oP '^\s*- alert:\s*\K\S+' deploy/prometheus-alerts.yml | sort | paste -sd,)
check "the deployed rule set matches the repository" "$DEPLOYED" "$INFILE"

printf '\n== healthy baseline ==\n'
check "OutboxOldestPendingTooOld is inactive" \
  "$(alert_state OutboxOldestPendingTooOld)" "inactive"


printf '\n== inject: publisher stalled with writes arriving ==\n'
# Exactly the incident these two alerts exist for: writes commit, events are
# staged, nothing drains.
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

RUN="alert$(date +%s)"
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

echo "  staged a backlog; waiting for the age threshold plus the 1m for-clause"
STATE=$(await_state OutboxOldestPendingTooOld firing 300)
check "backlog-age alert reaches firing" "$STATE" "firing"

printf '\n== repair: publisher restored ==\n'
$DC up -d --no-deps orchestrator >/dev/null 2>&1
until [ "$(curl -s -o /dev/null -w '%{http_code}' http://localhost:9080/health/ready)" = "200" ]; do
  sleep 3
done

# Clearing matters as much as firing: an alert that latches is a pager that
# stops being read.
CLEARED=$(await_state OutboxOldestPendingTooOld inactive 300)
check "backlog-age alert clears once drained" "$CLEARED" "inactive"


printf '\n---------------------------------------------\n'
printf 'passed: %d   failed: %d\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ] || exit 1
