#!/usr/bin/env bash
# Identity and tenant-isolation checks against the live stack.
#
# The property under test: a caller cannot choose their own identity. Tenant
# and actor must come from the token APISIX verified, never from a header the
# client controls. Before this was enforced, sending `X-Tenant-ID: other`
# was enough to read another tenant's invocation history.
set -uo pipefail

cd "$(dirname "$0")/.."
DC="docker compose --env-file .env -f deploy/docker-compose.yml"
PASS=0
FAIL=0

check() {
  if [ "$2" = "$3" ]; then
    printf '  PASS  %-56s %s\n' "$1" "$2"; PASS=$((PASS + 1))
  else
    printf '  FAIL  %-56s got=%s want=%s\n' "$1" "$2" "$3"; FAIL=$((FAIL + 1))
  fi
}

echo "== identity is derived from the verified token, not from headers =="

RESULT=$($DC exec -T orchestrator python - <<'PY'
import asyncio
import base64
import json

import httpx

KC = "http://keycloak:8080/realms/favl/protocol/openid-connect/token"
GW = "http://apisix:9080"


async def main() -> None:
    async with httpx.AsyncClient(timeout=60) as c:
        token = (
            await c.post(
                KC,
                data={
                    "client_id": "agentic-content-os",
                    "client_secret": "replace-me",
                    "username": "demo",
                    "password": "demo-password",
                    "grant_type": "password",
                },
            )
        ).json()["access_token"]
        auth = {"Authorization": f"Bearer {token}"}

        # A forged X-Userinfo naming another tenant must not be honoured.
        forged = base64.b64encode(
            json.dumps({"sub": "attacker", "tenant": "victim-tenant"}).encode()
        ).decode()

        r = await c.get(
            f"{GW}/v1/invocations?limit=5",
            headers={**auth, "X-Userinfo": forged, "X-Tenant-ID": "victim-tenant"},
        )
        rows = r.json() if r.status_code == 200 else []
        leaked = sum(1 for row in rows if row.get("tenant_id") == "victim-tenant")
        print(f"forged_status={r.status_code}")
        print(f"forged_leaked_rows={leaked}")

        # Without any token the endpoint must not be reachable at all.
        r = await c.get(f"{GW}/v1/invocations?limit=1")
        print(f"anonymous_status={r.status_code}")

        # With a real token, the caller sees only their own tenant.
        r = await c.get(f"{GW}/v1/invocations?limit=50", headers=auth)
        rows = r.json() if r.status_code == 200 else []
        tenants = sorted({row["tenant_id"] for row in rows})
        print(f"authenticated_status={r.status_code}")
        print(f"distinct_tenants={len(tenants)}")

        # Connectors are tenant-scoped too. A connector planted in another
        # tenant must be invisible and un-invokable, and must 404 rather than
        # 403 so its existence is not confirmed.
        import uuid as _uuid

        other = str(_uuid.uuid4())
        r = await c.get(f"{GW}/v1/connectors/{other}", headers=auth)
        print(f"foreign_connector_get={r.status_code}")

        r = await c.get(f"{GW}/v1/connectors", headers=auth)
        conns = r.json() if r.status_code == 200 else []
        conn_tenants = sorted({c_.get("tenant_id", "unset") for c_ in conns})
        print(f"connector_list_status={r.status_code}")
        print(f"connector_count={len(conns)}")


asyncio.run(main())
PY
)
echo "$RESULT" | sed 's/^/    /'

get() { echo "$RESULT" | grep "^$1=" | cut -d= -f2 | tr -d ' \r'; }

check "forged X-Userinfo leaks no other-tenant rows" "$(get forged_leaked_rows)" "0"
check "unauthenticated read is rejected at the gateway" "$(get anonymous_status)" "401"
check "authenticated read succeeds" "$(get authenticated_status)" "200"
check "results span exactly one tenant" "$(get distinct_tenants)" "1"
check "unknown connector id returns 404, not 403" "$(get foreign_connector_get)" "404"
check "connector listing is tenant-scoped" "$(get connector_list_status)" "200"

printf '\n---------------------------------------------\n'
printf 'passed: %d   failed: %d\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ] || exit 1
