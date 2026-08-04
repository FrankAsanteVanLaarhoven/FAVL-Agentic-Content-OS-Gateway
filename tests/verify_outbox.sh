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

check() { # check <label> <actual> <expected>
  if [ "$2" = "$3" ]; then
    printf '  PASS  %-52s %s\n' "$1" "$2"; PASS=$((PASS + 1))
  else
    printf '  FAIL  %-52s got=%s want=%s\n' "$1" "$2" "$3"; FAIL=$((FAIL + 1))
  fi
}

banner() { printf '\n== %s ==\n' "$1"; }

# --------------------------------------------------------------------------
banner "1/6  crash between commit and publish loses no event"
# --------------------------------------------------------------------------
OUTBOX_PUBLISHER_ENABLED=false $DC up -d --no-deps orchestrator >/dev/null 2>&1
sleep 10
JS_BEFORE=$(js_count)
RUN="c1$(date +%s)"
$DC exec -T connector-registry python - "$RUN" <<'PY'
import asyncio, sys, httpx
RUN = sys.argv[1]
async def main():
    async with httpx.AsyncClient(timeout=20) as c:
        for i in range(50):
            await c.post("http://orchestrator:8000/v1/agents",
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
JS_BEFORE=$(js_count)
psql_orch "UPDATE outbox_events SET status='pending', next_attempt_at=now(), published_at=NULL
           WHERE id IN (SELECT id FROM outbox_events WHERE status='published' LIMIT 10)" >/dev/null
drain 20
check "stream count unchanged after 10 republished ids" "$(js_count)" "$JS_BEFORE"

# --------------------------------------------------------------------------
banner "4/6  publish failure does not roll back the accepted write"
# --------------------------------------------------------------------------
$DC stop nats >/dev/null 2>&1
sleep 3
CODE=$($DC exec -T connector-registry python - <<'PY'
import asyncio, httpx, uuid
async def main():
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.post("http://orchestrator:8000/v1/agents",
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
  "$($DC exec -T connector-registry python -c "import httpx;print(httpx.get('http://orchestrator:8000/health/ready',timeout=10).json()['outbox']['dead'])" | tr -d ' \r')" "1"
psql_orch "DELETE FROM outbox_events WHERE aggregate_id='poison'" >/dev/null

# --------------------------------------------------------------------------
banner "6/6  hard crashes during load lose and duplicate nothing"
# --------------------------------------------------------------------------
RUN="kc$(date +%s)"
( for i in 1 2 3 4; do
    sleep 7
    docker kill -s KILL deploy-orchestrator-1 >/dev/null 2>&1 && echo "  [killer] SIGKILL #$i" >&2
    sleep 1
    docker start deploy-orchestrator-1 >/dev/null 2>&1
  done ) &
KILLER=$!
timeout 220 $DC exec -T connector-registry python - "$RUN" <<'PY'
import asyncio, sys, httpx
RUN = sys.argv[1]; TOTAL, CONC = 300, 8
accepted = set()
async def one(c, i, sem):
    name = f"{RUN}-{i:04d}"
    async with sem:
        await asyncio.sleep(i * 0.05 / CONC)
        for _ in range(80):
            try:
                r = await c.post("http://orchestrator:8000/v1/agents",
                                 json={"name": name, "connector_ids": []})
                if r.status_code in (201, 409):   # 409 == a prior attempt committed
                    accepted.add(name); return
            except Exception:
                pass
            await asyncio.sleep(0.3)
async def main():
    sem = asyncio.Semaphore(CONC)
    async with httpx.AsyncClient(timeout=5) as c:
        await asyncio.gather(*(one(c, i, sem) for i in range(TOTAL)))
    print(f"  accepted {len(accepted)}/{TOTAL}")
asyncio.run(main())
PY
wait $KILLER 2>/dev/null
docker start deploy-orchestrator-1 >/dev/null 2>&1
drain 30
check "every accepted write committed" \
  "$(psql_orch "SELECT count(*) FROM agents WHERE name LIKE '$RUN-%'")" "300"
check "every committed write produced an event" \
  "$(psql_orch "SELECT count(*) FROM outbox_events WHERE payload->>'name' LIKE '$RUN-%'")" "300"
check "every event was delivered" \
  "$(psql_orch "SELECT count(*) FROM outbox_events WHERE payload->>'name' LIKE '$RUN-%' AND status='published'")" "300"
check "no agent produced two events" \
  "$(psql_orch "SELECT count(*) FROM (SELECT payload->>'name' FROM outbox_events WHERE payload->>'name' LIKE '$RUN-%' GROUP BY 1 HAVING count(*)>1) x")" "0"

printf '\n---------------------------------------------\n'
printf 'passed: %d   failed: %d\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ] || exit 1
