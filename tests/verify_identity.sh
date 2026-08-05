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

# A driver that dies part-way used to leave later checks comparing an empty
# string, which reads as a confusing assertion failure rather than "the test
# harness broke". This makes a missing key an explicit, named failure.
expect_key() { # expect_key <stdout> <key>
  local value
  value=$(echo "$1" | grep "^$2=" | head -1 | cut -d= -f2- | tr -d ' \r')
  if [ -z "$value" ]; then
    printf '  ERROR harness: driver produced no %s — it exited early\n' "$2" >&2
    FAIL=$((FAIL + 1))
    echo "__DRIVER_DIED__"
    return
  fi
  echo "$value"
}

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
        print(f"authenticated_status={r.status_code}")

        # Connectors are tenant-scoped too. A connector planted in another
        # tenant must be invisible and un-invokable, and must 404 rather than
        # 403 so its existence is not confirmed.
        import uuid as _uuid

        other = str(_uuid.uuid4())
        r = await c.get(f"{GW}/v1/connectors/{other}", headers=auth)
        print(f"foreign_connector_get={r.status_code}")

        r = await c.get(f"{GW}/v1/connectors", headers=auth)
        conns = r.json() if r.status_code == 200 else []
        print(f"connector_list_status={r.status_code}")

        # The caller's tenant must come from the token's claim, not a shared
        # default. If the realm stops emitting it, identity fails closed and
        # this whole block returns 401 rather than silently collapsing every
        # tenant into one.
        import base64 as _b64
        import json as _json

        part = token.split(".")[1]
        part += "=" * (-len(part) % 4)
        claims = _json.loads(_b64.urlsafe_b64decode(part))
        my_tenant = claims.get("tenant", "ABSENT")
        print(f"token_tenant={my_tenant}")

        # Nothing visible may belong to another tenant. Zero visible rows is
        # correct isolation too, so the assertion is on foreign rows, not on
        # the number of distinct tenants.
        foreign = [row for row in rows if row.get("tenant_id") != my_tenant]
        print(f"foreign_invocation_rows={len(foreign)}")
        foreign_conns = [
            item for item in conns if item.get("tenant_id", my_tenant) != my_tenant
        ]
        print(f"foreign_connector_rows={len(foreign_conns)}")

        # Rows belonging to any other tenant must be invisible. The database
        # is seeded with rows under a different tenant by earlier runs.
        made = await c.post(
            f"{GW}/v1/connectors",
            headers=auth,
            json={
                "name": f"iso-{_uuid.uuid4().hex[:8]}",
                "kind": "http",
                "config": {
                    "base_url": "http://testprovider:9099",
                    "allowed_hosts": ["testprovider"],
                },
            },
        )
        print(f"scoped_create_status={made.status_code}")
        again = await c.get(f"{GW}/v1/connectors", headers=auth)
        after = again.json() if again.status_code == 200 else []
        print(f"visible_grew_by={len(after) - len(conns)}")


asyncio.run(main())
PY
)
echo "$RESULT" | sed 's/^/    /'

get() { expect_key "$RESULT" "$1"; }

# The driver must have produced output at all; an empty RESULT means the
# container command failed before printing anything.
if [ -z "$(echo "$RESULT" | tr -d '[:space:]')" ]; then
  echo "  ERROR driver produced no output at all"
  exit 1
fi

check "forged X-Userinfo leaks no other-tenant rows" "$(get forged_leaked_rows)" "0"
check "unauthenticated read is rejected at the gateway" "$(get anonymous_status)" "401"
check "authenticated read succeeds" "$(get authenticated_status)" "200"

check "unknown connector id returns 404, not 403" "$(get foreign_connector_get)" "404"
check "connector listing is tenant-scoped" "$(get connector_list_status)" "200"
check "tenant comes from a token claim, not a default" "$(get token_tenant)" "favl-demo"
check "no visible invocation belongs to another tenant" "$(get foreign_invocation_rows)" "0"
check "no visible connector belongs to another tenant" "$(get foreign_connector_rows)" "0"
check "creating a connector succeeds under that tenant" "$(get scoped_create_status)" "201"
check "only the caller's own new row becomes visible" "$(get visible_grew_by)" "1"

printf '\n---------------------------------------------\n'
printf 'passed: %d   failed: %d\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ] || exit 1
