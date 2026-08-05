#!/usr/bin/env bash
# M1.2 exit-criteria verification for the transactional outbox.
#
# Runs against the live stack. Requires it to be up:
#   docker compose --env-file .env -f deploy/docker-compose.yml up -d --build
#
# The load driver runs inside the connector-registry container so it survives
# the orchestrator being killed, and reaches it over the compose network.
set -uo pipefail

cd "$(dirname "$0")/.."
DC="docker compose --env-file .env -f deploy/docker-compose.yml"
PASS=0
FAIL=0

psql_orch() { $DC exec -T postgres psql -U favl -d favl_orchestrator -tAc "$1" | tr -d ' \r'; }
js_count() { curl -fsS "http://localhost:8222/jsz?streams=1" | python3 -c "import json,sys; print(json.load(sys.stdin)['messages'])"; }
drain() { sleep "${1:-20}"; }

# A driver that dies part-way used to leave later checks comparing an empty
# string, which reads as a confusing assertion failure rather than "the test
# harness broke". This makes a missing key an explicit, named failure.
expect_key() { # expect_key <stdout> <key>
  local value
  value=$(echo "$1" | grep "^$2=" | head -1 | cut -d= -f2- | tr -d ' \r')
  if [ -z "$value" ]; then
    printf '  ERROR %-56s driver produced no %s — it exited early\n' "harness" "$2"
    FAIL=$((FAIL + 1))
    echo "__MISSING__"
    return
  fi
  echo "$value"
}

check() { # check <label> <actual> <expected>
  if [ "$2" = "$3" ]; then
    printf '  PASS  %-52s %s\n' "$1" "$2"; PASS=$((PASS + 1))
  else
    printf '  FAIL  %-52s got=%s want=%s\n' "$1" "$2" "$3"; FAIL=$((FAIL + 1))
  fi
}

banner() { printf '\n== %s ==\n' "$1"; }

# Sections run back to back; a restart from the previous one can still be in
# flight. Waiting on readiness keeps a 503 from being reported as a failure
# of the property under test.
await_ready() {
  local deadline=$((SECONDS + ${1:-60}))
  while [ $SECONDS -lt $deadline ]; do
    if [ "$(curl -s -o /dev/null -w '%{http_code}' http://localhost:9080/health/ready)" = "200" ]; then
      return 0
    fi
    sleep 2
  done
  echo "  ERROR harness: orchestrator did not become ready" >&2
  return 1
}

# --------------------------------------------------------------------------
banner "1/6  crash between commit and publish loses no event"
# --------------------------------------------------------------------------
OUTBOX_PUBLISHER_ENABLED=false $DC up -d --no-deps orchestrator >/dev/null 2>&1
sleep 10
await_ready 90 || true
JS_BEFORE=$(js_count)
RUN="c1$(date +%s)"
$DC exec -T connector-registry python - "$RUN" <<'PY'
import asyncio, sys, httpx
RUN = sys.argv[1]
KC = "http://keycloak:8080/realms/favl/protocol/openid-connect/token"
# Calls go through APISIX, not straight to the service. The orchestrator now
# derives tenant and actor from gateway-verified claims and fails closed
# without them, so a direct call is a 401 by design.
GW = "http://apisix:9080"


async def bearer(c: httpx.AsyncClient) -> dict[str, str]:
    r = await c.post(
        KC,
        data={
            "client_id": "agentic-content-os",
            "client_secret": "replace-me",
            "username": "demo",
            "password": "demo-password",
            "grant_type": "password",
        },
    )
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


async def main():
    async with httpx.AsyncClient(timeout=20) as c:
        auth = await bearer(c)
        for i in range(50):
            await c.post(f"{GW}/v1/agents", headers=auth,
                         json={"name": f"{RUN}-{i:03d}", "connector_ids": []})
asyncio.run(main())
PY
sleep 2
check "committed rows with publisher off" \
  "$(psql_orch "SELECT count(*) FROM agents WHERE name LIKE '$RUN-%'")" "50"
check "events staged but unpublished" \
  "$(psql_orch "SELECT count(*) FROM outbox_events WHERE payload->>'name' LIKE '$RUN-%' AND status='pending'")" "50"
check "stream unchanged while publisher is off" "$(js_count)" "$JS_BEFORE"

# --------------------------------------------------------------------------
banner "2/6  restarting the publisher delivers every pending event"
# --------------------------------------------------------------------------
$DC up -d --no-deps orchestrator >/dev/null 2>&1
drain 20
check "all staged events published after restart" \
  "$(psql_orch "SELECT count(*) FROM outbox_events WHERE payload->>'name' LIKE '$RUN-%' AND status='published'")" "50"
check "no pending backlog remains" \
  "$(psql_orch "SELECT count(*) FROM outbox_events WHERE status='pending'")" "0"

# --------------------------------------------------------------------------
banner "3/6  republication produces no duplicate downstream effect"
# --------------------------------------------------------------------------
# Deduplication is bounded by the stream's duplicate window (2h), so this
# check is only meaningful on rows published inside it. An earlier version
# selected arbitrary rows with `LIMIT 10` and no ORDER BY, which picked
# whatever Postgres returned first — on a long-lived database those were
# hours old and legitimately outside the window, so the test failed while the
# system was behaving exactly as designed. It scopes to the rows step 2 just
# published, and asserts that precondition rather than assuming it.
JS_BEFORE=$(js_count)
OLDEST=$(psql_orch "SELECT COALESCE(MAX(EXTRACT(EPOCH FROM now() - published_at))::int, 0)
                    FROM outbox_events
                    WHERE payload->>'name' LIKE '$RUN-%' AND status='published'")
check "republished rows are inside the duplicate window" \
  "$([ "${OLDEST:-99999}" -lt 7200 ] && echo yes || echo no)" "yes"

psql_orch "UPDATE outbox_events SET status='pending', next_attempt_at=now(), published_at=NULL
           WHERE id IN (SELECT id FROM outbox_events
                        WHERE payload->>'name' LIKE '$RUN-%' AND status='published'
                        ORDER BY created_at DESC LIMIT 10)" >/dev/null
drain 20
check "stream count unchanged after 10 republished ids" "$(js_count)" "$JS_BEFORE"

# --------------------------------------------------------------------------
banner "4/6  publish failure does not roll back the accepted write"
# --------------------------------------------------------------------------
await_ready 90
$DC stop nats >/dev/null 2>&1
sleep 3
CODE=$($DC exec -T connector-registry python - <<'PY'
import asyncio, httpx, uuid
KC = "http://keycloak:8080/realms/favl/protocol/openid-connect/token"
# Calls go through APISIX, not straight to the service. The orchestrator now
# derives tenant and actor from gateway-verified claims and fails closed
# without them, so a direct call is a 401 by design.
GW = "http://apisix:9080"


async def bearer(c: httpx.AsyncClient) -> dict[str, str]:
    r = await c.post(
        KC,
        data={
            "client_id": "agentic-content-os",
            "client_secret": "replace-me",
            "username": "demo",
            "password": "demo-password",
            "grant_type": "password",
        },
    )
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


async def main():
    async with httpx.AsyncClient(timeout=30) as c:
        auth = await bearer(c)
        r = await c.post(f"{GW}/v1/agents", headers=auth,
                         json={"name": f"outage-{uuid.uuid4().hex[:8]}", "connector_ids": []})
        print(r.status_code)
asyncio.run(main())
PY
)
check "write accepted while broker is down" "$(echo "$CODE" | tr -d ' \r')" "201"
$DC start nats >/dev/null 2>&1
drain 30
check "backlog drains once the broker returns" \
  "$(psql_orch "SELECT count(*) FROM outbox_events WHERE status='pending'")" "0"

# --------------------------------------------------------------------------
banner "5/6  poison events become visible instead of retrying forever"
# --------------------------------------------------------------------------
psql_orch "DELETE FROM outbox_events WHERE aggregate_id='poison'" >/dev/null
# A subject containing spaces is invalid at the protocol level and can never
# succeed, so this is a true poison event rather than a transient failure.
psql_orch "INSERT INTO outbox_events (id, aggregate_type, aggregate_id, subject, payload,
             status, attempts, max_attempts, next_attempt_at, created_at)
           VALUES (gen_random_uuid(), 'agent', 'poison', 'bad subject with spaces',
             '{\"poison\":true}'::jsonb, 'pending', 0, 3, now(), now())" >/dev/null
for _ in $(seq 1 12); do
  sleep 5
  [ "$(psql_orch "SELECT status FROM outbox_events WHERE aggregate_id='poison'")" = "dead" ] && break
done
check "poison event reaches dead state" \
  "$(psql_orch "SELECT status FROM outbox_events WHERE aggregate_id='poison'")" "dead"
A1=$(psql_orch "SELECT attempts FROM outbox_events WHERE aggregate_id='poison'")
sleep 12
check "dead event stops consuming retries" \
  "$(psql_orch "SELECT attempts FROM outbox_events WHERE aggregate_id='poison'")" "$A1"
check "dead count exposed in readiness" \
  "$($DC exec -T connector-registry python -c "import httpx;print(httpx.get('http://orchestrator:8000/readyz',timeout=10).json()['outbox']['dead'])" | tr -d ' \r')" "1"
psql_orch "DELETE FROM outbox_events WHERE aggregate_id='poison'" >/dev/null

# --------------------------------------------------------------------------
banner "6/6  hard crashes during load lose and duplicate nothing"
# --------------------------------------------------------------------------
await_ready 90
RUN="kc$(date +%s)"
# Resolve the container from compose rather than hardcoding the project
# name: with COMPOSE_PROJECT_NAME set, every kill would silently no-op
# and the section would still report green on a run with no crashes.
ORCH_ID=$($DC ps -q orchestrator)
KILL_LOG=$(mktemp)
( for i in 1 2 3 4; do
    sleep 13
    if docker kill -s KILL "$ORCH_ID" >/dev/null 2>&1; then
      echo kill >> "$KILL_LOG"
      echo "  [killer] SIGKILL #$i" >&2
    fi
    sleep 1
    docker start "$ORCH_ID" >/dev/null 2>&1
  done ) &
KILLER=$!
# The driver runs on the HOST, not inside a container. When it ran via
# `docker compose exec`, the restarts this section deliberately causes
# cancelled the exec itself, so the driver's output — including the accepted
# count the assertions depend on — was lost.
# Minted in-network so the issuer matches APISIX's discovery URL; a token
# obtained from the host would carry iss=localhost and be rejected.
TOKEN=$($DC exec -T orchestrator python -c "
import httpx
print(httpx.post('http://keycloak:8080/realms/favl/protocol/openid-connect/token',
    data={'client_id':'agentic-content-os','client_secret':'replace-me',
          'username':'demo','password':'demo-password','grant_type':'password'},
    timeout=20).json()['access_token'])" | tr -d '\r\n')

if [ -z "$TOKEN" ]; then
  echo "  ERROR harness: could not mint a token for the load phase" >&2
  FAIL=$((FAIL + 1))
fi

# The driver only generates load; nothing is read back from its stdout. The
# assertions below measure the database and the stream directly, so a driver
# that dies early shows up as too little load rather than as a vacuous pass.
# Runs in connector-registry, NOT the orchestrator: the orchestrator is the
# container this section kills, so a driver hosted there dies with the first
# SIGKILL and the load stops after a few seconds.
$DC exec -T connector-registry python - "$RUN" "$TOKEN" <<'PYEOF' || true
import asyncio
import sys

import httpx

RUN, TOKEN = sys.argv[1], sys.argv[2]
TOTAL, CONC = 150, 4
GW = "http://apisix:9080"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


async def one(c, i, sem):
    name = f"{RUN}-{i:04d}"
    async with sem:
        # Stagger so issuance spans the kill window and stays inside the
        # gateway's 300-per-60s budget for this route.
        await asyncio.sleep(i * 0.4)
        for _ in range(120):
            try:
                r = await c.post(
                    f"{GW}/v1/agents",
                    headers=AUTH,
                    json={"name": name, "connector_ids": []},
                )
                if r.status_code in (201, 409):
                    return
                if r.status_code == 429:
                    await asyncio.sleep(5.0)
                    continue
            except Exception:
                pass
            await asyncio.sleep(0.3)


async def main() -> None:
    sem = asyncio.Semaphore(CONC)
    async with httpx.AsyncClient(timeout=8) as c:
        await asyncio.gather(*(one(c, i, sem) for i in range(TOTAL)))


asyncio.run(main())
PYEOF

wait $KILLER 2>/dev/null
docker start "$ORCH_ID" >/dev/null 2>&1
drain 40

COMMITTED=$(psql_orch "SELECT count(*) FROM agents WHERE name LIKE '$RUN-%'")
KILLS=$(wc -l < "$KILL_LOG" 2>/dev/null || echo 0)

# The crash injection must actually have happened: `docker kill` failing is
# silent, so without this the section degrades to a plain load test.
check "the orchestrator was hard-killed four times" "$KILLS" "4"
# Anti-vacuity floor, not a throughput target: with zero load every equality
# below holds trivially, so the run must have committed real work. Four
# restart windows legitimately swallow a large share of the 150 attempts —
# observed runs land 75-150 — so the floor is set well under that. Raising it
# to chase a number would make the suite flaky without testing anything more.
check "the load phase committed a meaningful number of writes" \
  "$([ "${COMMITTED:-0}" -ge 50 ] && echo yes || echo no)" "yes"
check "every committed write produced an event" \
  "$(psql_orch "SELECT count(*) FROM outbox_events WHERE payload->>'name' LIKE '$RUN-%'")" "$COMMITTED"
check "every event was delivered" \
  "$(psql_orch "SELECT count(*) FROM outbox_events WHERE payload->>'name' LIKE '$RUN-%' AND status='published'")" "$COMMITTED"
check "no agent produced two events" \
  "$(psql_orch "SELECT count(*) FROM (SELECT payload->>'name' FROM outbox_events WHERE payload->>'name' LIKE '$RUN-%' GROUP BY 1 HAVING count(*)>1) x")" "0"

printf '\n---------------------------------------------\n'
printf 'passed: %d   failed: %d\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ] || exit 1
