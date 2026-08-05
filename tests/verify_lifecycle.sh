#!/usr/bin/env bash
# Connector lifecycle against the live stack, through APISIX, with a real token.
#
# The unit tests in tests/test_lifecycle.py prove the PREDICATE — that
# `is_executable` returns False for a revoked connector. They cannot prove the
# PATH: that the invocation endpoint actually consults it, against a freshly
# read row, on the very next request after revocation. That is what this does.
#
# The milestone gate is section 3: a connector is revoked while an invocation
# is in flight, and the next invocation must be refused. No cache flush, no
# consumer convergence, no worker restart.
#
# Section 3 also asserts the other half of the policy — that the in-flight
# invocation COMPLETES. Both directions matter: a revocation that stopped
# running work would be just as much a departure from ADR 0002 as one that
# kept serving new requests.
set -uo pipefail

cd "$(dirname "$0")/.."
DC="docker compose --env-file .env -f deploy/docker-compose.yml"
PASS=0
FAIL=0

check() {
  if [ "$2" = "$3" ]; then
    printf '  PASS  %-58s %s\n' "$1" "$2"; PASS=$((PASS + 1))
  else
    printf '  FAIL  %-58s got=%s want=%s\n' "$1" "$2" "$3"; FAIL=$((FAIL + 1))
  fi
}

# A driver that dies part-way leaves later checks comparing empty strings,
# which reads as a confusing assertion failure rather than "the harness broke".
expect_key() { # expect_key <stdout> <key>
  local value
  # Strips carriage returns and surrounding whitespace only. Deleting every
  # space turned "credential suspected compromised" into one word and failed
  # an assertion that was actually correct.
  value=$(echo "$1" | grep "^$2=" | head -1 | cut -d= -f2- | tr -d '\r' \
    | sed 's/^[[:space:]]*//; s/[[:space:]]*$//')
  if [ -z "$value" ]; then
    printf '  ERROR harness: driver produced no %s — it exited early\n' "$2" >&2
    FAIL=$((FAIL + 1))
    echo "__DRIVER_DIED__"
    return
  fi
  echo "$value"
}

echo "== connector lifecycle: states, transitions, and revocation immediacy =="

# The driver runs INSIDE the network so the token's issuer matches what APISIX
# validates. Run from the host and every request 401s on an issuer mismatch,
# which looks exactly like a broken assertion.
OUT=$($DC exec -T orchestrator python - <<'PY' 2>&1
import asyncio
import time
import uuid

import httpx

KC = "http://keycloak:8080/realms/favl/protocol/openid-connect/token"
GW = "http://apisix:9080"
SUFFIX = uuid.uuid4().hex[:8]


async def main() -> None:
    async with httpx.AsyncClient(timeout=120) as c:
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

        provider = {
            "base_url": "http://testprovider:9099",
            "allowed_hosts": ["testprovider"],
        }

        async def create(name: str, status: str = "draft") -> tuple[int, dict]:
            r = await c.post(
                f"{GW}/v1/connectors",
                headers=auth,
                json={
                    "name": name,
                    "kind": "http",
                    "base_url": "http://testprovider:9099",
                    "config": provider,
                    "status": status,
                },
            )
            return r.status_code, (r.json() if r.status_code < 300 else {})

        async def step(cid: str, action: str, **body):
            r = await c.post(
                f"{GW}/v1/connectors/{cid}/{action}", headers=auth, json=body
            )
            return r

        async def invoke(cid: str, seconds: float = 0, timeout: float = 30.0):
            # `operation` is the PATH, appended to the connector's base_url by
            # the HTTP adapter. It is not "METHOD path" — the method comes from
            # config. Sending "POST /echo" builds the URL ".../POST /echo",
            # which 404s and surfaces as a 502 that looks like a broken gate.
            path = f"slow?seconds={seconds}" if seconds else "echo"
            return await c.post(
                f"{GW}/v1/connectors/{cid}/invoke",
                headers=auth,
                json={
                    "operation": path,
                    "payload": {"probe": "lifecycle"},
                    "idempotency_key": uuid.uuid4().hex,
                    "timeout_seconds": timeout,
                },
            )

        # ---- 1. the happy path, one transition at a time ----------------
        code, conn = await create(f"life-{SUFFIX}")
        print(f"create_status={code}")
        print(f"create_state={conn.get('status')}")
        cid = conn.get("id", "")
        print(f"connector_id={cid}")

        # Enabling straight from draft must be refused, and the refusal must
        # tell the caller where they can actually go.
        r = await step(cid, "enable")
        print(f"enable_from_draft={r.status_code}")
        detail = r.json().get("detail", {}) if r.status_code == 409 else {}
        print(f"enable_from_draft_code={detail.get('error_code')}")
        print(f"enable_from_draft_targets={','.join(detail.get('permitted_targets', []))}")

        for action, expect in (
            ("install", "installed"),
            ("configure", "configured"),
        ):
            r = await step(cid, action)
            print(f"{action}_status={r.status_code}")
            print(f"{action}_state={r.json().get('status') if r.status_code == 200 else '-'}")

        # Enabling an unvalidated configuration must still be refused.
        r = await step(cid, "enable")
        print(f"enable_unvalidated={r.status_code}")

        r = await c.post(f"{GW}/v1/connectors/{cid}/validate", headers=auth)
        print(f"validate_status={r.status_code}")
        print(f"validate_valid={r.json().get('valid') if r.status_code == 200 else '-'}")
        r = await c.get(f"{GW}/v1/connectors/{cid}", headers=auth)
        print(f"state_after_validate={r.json().get('status')}")

        r = await step(cid, "enable")
        print(f"enable_status={r.status_code}")
        print(f"enable_state={r.json().get('status') if r.status_code == 200 else '-'}")

        r = await invoke(cid)
        print(f"invoke_while_enabled={r.status_code}")

        # ---- 2. reasons are enforced where the machine demands them -----
        r = await step(cid, "disable")
        print(f"disable_without_reason={r.status_code}")
        rd = r.json().get("detail", {}) if r.status_code == 422 else {}
        print(f"disable_without_reason_code={rd.get('error_code')}")

        r = await step(cid, "disable", reason="operator test")
        print(f"disable_with_reason={r.status_code}")
        # Repeating a suspension is a success, not a conflict.
        r = await step(cid, "disable", reason="operator test")
        print(f"disable_repeated={r.status_code}")

        r = await invoke(cid)
        print(f"invoke_while_disabled={r.status_code}")
        body = r.json() if r.status_code >= 400 else {}
        print(f"invoke_while_disabled_code={(body.get('detail') or body).get('error_code')}")

        r = await step(cid, "enable")
        print(f"re_enable_status={r.status_code}")

        # ---- 3. THE GATE: revoke mid-flight, next call refused ----------
        # A slow invocation is started and left running. Revocation happens
        # while it is on the wire, so no request boundary tidies up for us.
        inflight = asyncio.create_task(invoke(cid, seconds=6, timeout=30.0))
        await asyncio.sleep(1.5)  # let it reach the provider

        r = await c.get(f"{GW}/v1/connectors/{cid}", headers=auth)
        print(f"state_before_revoke={r.json().get('status')}")

        revoked_at = time.monotonic()
        r = await step(cid, "revoke", reason="credential suspected compromised")
        print(f"revoke_status={r.status_code}")
        print(f"revoke_state={r.json().get('status') if r.status_code == 200 else '-'}")

        # The very next invocation. No sleep, no cache flush, no restart.
        r = await invoke(cid)
        elapsed_ms = int((time.monotonic() - revoked_at) * 1000)
        print(f"invoke_after_revoke={r.status_code}")
        body = r.json() if r.status_code >= 400 else {}
        print(f"invoke_after_revoke_code={(body.get('detail') or body).get('error_code')}")
        print(f"refusal_latency_ms={elapsed_ms}")

        # Standard revocation lets running work finish under the authority
        # snapshot pinned at acceptance (ADR 0002). Terminating it would risk
        # partial external side effects and audit records that cannot answer
        # "did this run?" — and would report success while remote execution
        # continued. Emergency revocation, which does request cancellation,
        # is not implemented.
        try:
            done = await asyncio.wait_for(inflight, timeout=40)
            print(f"inflight_status={done.status_code}")
        except (TimeoutError, asyncio.TimeoutError):
            print("inflight_status=timeout")

        # Revocation is one-way.
        r = await step(cid, "enable")
        print(f"enable_after_revoke={r.status_code}")
        r = await step(cid, "configure")
        print(f"configure_after_revoke={r.status_code}")

        r = await step(cid, "revoke", reason="again")
        print(f"revoke_repeated={r.status_code}")

        # ---- 4. archival retains identity and audit --------------------
        r = await step(cid, "archive")
        print(f"archive_status={r.status_code}")
        r = await invoke(cid)
        print(f"invoke_after_archive={r.status_code}")

        # ---- 5. the audit trail -----------------------------------------
        r = await c.get(f"{GW}/v1/connectors/{cid}/audit", headers=auth)
        print(f"audit_status={r.status_code}")
        trail = r.json() if r.status_code == 200 else []
        print(f"audit_events={','.join(e['event'].split('.')[-1] for e in trail)}")
        versions = [e["aggregate_version"] for e in trail]
        print(f"audit_versions_monotonic={int(versions == sorted(set(versions)))}")
        revocations = [e for e in trail if e["event"] == "connector.revoked"]
        print(f"audit_revoke_count={len(revocations)}")
        print(f"audit_revoke_reason={revocations[0]['reason'] if revocations else '-'}")
        print(f"audit_revoke_actor_present={int(bool(revocations and revocations[0]['actor_id']))}")

        # ---- 6. creation cannot skip the machine ------------------------
        for forbidden in ("revoked", "archived", "deleted"):
            code, _ = await create(f"bad-{forbidden}-{SUFFIX}", status=forbidden)
            print(f"create_as_{forbidden}={code}")
        code, _ = await create(f"ok-enabled-{SUFFIX}", status="enabled")
        print(f"create_as_enabled={code}")


asyncio.run(main())
PY
)

if echo "$OUT" | grep -q "Traceback"; then
  echo "  ERROR harness: driver raised" >&2
  echo "$OUT" | tail -20 >&2
  exit 1
fi

echo
echo "-- 1. the machine refuses what it should, in order --"
check "create lands in draft"            "$(expect_key "$OUT" create_state)"            "draft"
check "enable from draft refused"        "$(expect_key "$OUT" enable_from_draft)"       "409"
check "refusal names the error"          "$(expect_key "$OUT" enable_from_draft_code)"  "TRANSITION_NOT_PERMITTED"
check "refusal names where to go"        "$(expect_key "$OUT" enable_from_draft_targets)" "installed,revoked"
check "install advances"                 "$(expect_key "$OUT" install_state)"           "installed"
check "configure advances"               "$(expect_key "$OUT" configure_state)"         "configured"
check "enable before validation refused" "$(expect_key "$OUT" enable_unvalidated)"      "409"
check "validate reports valid"           "$(expect_key "$OUT" validate_valid)"          "True"
check "validate moves the state"         "$(expect_key "$OUT" state_after_validate)"    "validated"
check "enable succeeds once validated"   "$(expect_key "$OUT" enable_state)"            "enabled"
check "enabled connector serves"         "$(expect_key "$OUT" invoke_while_enabled)"    "200"

echo
echo "-- 2. reasons and idempotency --"
check "disable without reason refused"   "$(expect_key "$OUT" disable_without_reason)"      "422"
check "refusal names the missing reason" "$(expect_key "$OUT" disable_without_reason_code)" "REASON_REQUIRED"
check "disable with reason succeeds"     "$(expect_key "$OUT" disable_with_reason)"         "200"
check "repeating a disable is a success" "$(expect_key "$OUT" disable_repeated)"            "200"
check "disabled connector refuses"       "$(expect_key "$OUT" invoke_while_disabled)"       "409"
check "refusal distinguishes disabled"   "$(expect_key "$OUT" invoke_while_disabled_code)"  "CONNECTOR_DISABLED"
check "disabled may be re-enabled"       "$(expect_key "$OUT" re_enable_status)"            "200"

echo
echo "-- 3. THE GATE: revocation takes effect on the next request --"
check "connector was live before revoke" "$(expect_key "$OUT" state_before_revoke)"        "enabled"
check "revoke succeeds mid-flight"       "$(expect_key "$OUT" revoke_status)"              "200"
check "state is revoked"                 "$(expect_key "$OUT" revoke_state)"               "revoked"
check "NEXT invocation is refused"       "$(expect_key "$OUT" invoke_after_revoke)"        "403"
check "refusal distinguishes revoked"    "$(expect_key "$OUT" invoke_after_revoke_code)"   "CONNECTOR_REVOKED"
check "revoked cannot be re-enabled"     "$(expect_key "$OUT" enable_after_revoke)"        "409"
check "revoked cannot be reconfigured"   "$(expect_key "$OUT" configure_after_revoke)"     "409"
check "repeating a revoke is a success"  "$(expect_key "$OUT" revoke_repeated)"            "200"
printf '  INFO  %-58s %s ms\n' "refusal observed after revoke" "$(expect_key "$OUT" refusal_latency_ms)"
# Asserted, not reported, since ADR 0002. Standard revocation lets running
# work finish under the authority snapshot pinned at acceptance. If someone
# later makes revocation terminate in-flight invocations, this is the line
# that will tell them the ADR needs amending first.
check "in-flight invocation completes (ADR 0002)" \
  "$(expect_key "$OUT" inflight_status)" "200"

echo
echo "-- 4. archival --"
check "archive succeeds"                 "$(expect_key "$OUT" archive_status)"             "200"
check "archived connector refuses"       "$(expect_key "$OUT" invoke_after_archive)"       "410"

echo
echo "-- 5. the audit trail is complete and ordered --"
check "audit readable"                   "$(expect_key "$OUT" audit_status)"               "200"
check "every transition recorded"        "$(expect_key "$OUT" audit_events)" \
  "created,installed,configured,validation_succeeded,enabled,disabled,enabled,revoked,archived"
check "versions strictly increasing"     "$(expect_key "$OUT" audit_versions_monotonic)"   "1"
check "one revocation recorded"          "$(expect_key "$OUT" audit_revoke_count)"         "1"
check "revocation carries its reason"    "$(expect_key "$OUT" audit_revoke_reason)" \
  "credential suspected compromised"
check "revocation names an actor"        "$(expect_key "$OUT" audit_revoke_actor_present)" "1"

echo
echo "-- 6. creation cannot enter a state the machine guards --"
check "cannot create as revoked"         "$(expect_key "$OUT" create_as_revoked)"          "422"
check "cannot create as archived"        "$(expect_key "$OUT" create_as_archived)"         "422"
check "cannot create as deleted"         "$(expect_key "$OUT" create_as_deleted)"          "422"
check "may create as enabled (validated on create)" "$(expect_key "$OUT" create_as_enabled)" "201"

echo
echo "-- 7. the audit table is append-only in the database --"
# Enforced by trigger, not by application code: a later code change must not
# be able to rewrite history, and "we only ever INSERT" is a claim about code.
CID=$(expect_key "$OUT" connector_id)
UPD=$($DC exec -T postgres psql -U favl -d favl_connectors -tAc \
  "UPDATE connector_audit SET reason='tampered' WHERE connector_id='$CID'" 2>&1)
DEL=$($DC exec -T postgres psql -U favl -d favl_connectors -tAc \
  "DELETE FROM connector_audit WHERE connector_id='$CID'" 2>&1)
# psql prints an ERROR line and often a DETAIL line, both matching. Counting
# lines and demanding exactly one fails on a constraint that is working.
matched() { if echo "$1" | grep -qE "$2"; then echo yes; else echo no; fi; }
check "UPDATE is refused"  "$(matched "$UPD" 'append-only')" "yes"
check "DELETE is refused"  "$(matched "$DEL" 'append-only')" "yes"

# And the connector row cannot be removed out from under its own audit trail.
DROP=$($DC exec -T postgres psql -U favl -d favl_connectors -tAc \
  "DELETE FROM connectors WHERE id='$CID'" 2>&1)
check "connector row protected by audit FK" \
  "$(matched "$DROP" 'violates foreign key|still referenced')" "yes"

echo
echo "== $PASS passed, $FAIL failed =="
[ "$FAIL" -eq 0 ]
