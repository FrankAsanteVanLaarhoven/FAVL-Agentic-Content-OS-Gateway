# FAVL Agentic Content OS Gateway

Production-oriented reference implementation for a unified API/AI gateway.

## Architecture

Client -> Apache APISIX -> OIDC/Keycloak -> Agent Orchestrator -> Connectors
                                  |               |        |
                                  |               |        -> PostgreSQL
                                  |               -> NATS JetStream
                                  -> OpenTelemetry -> Collector -> Prometheus

Each service owns its own database (`favl_orchestrator`, `favl_connectors`)
on a shared PostgreSQL instance, so no service can read another's tables.

## What is implemented

- APISIX declarative routes
- OIDC authentication hook
- Per-route rate limiting
- Request correlation IDs
- Agent orchestration API, persisted in PostgreSQL
- Connector registry API, persisted in PostgreSQL
- Alembic migrations, applied on service start
- Transactional outbox: domain row and event committed together, published
  by a background worker with JetStream acks, retries, dead-lettering and
  deduplication
- OpenTelemetry instrumentation hooks
- Prometheus scraping of APISIX, NATS, the collector and both services,
  with outbox alert rules
- Kubernetes Gateway API examples
- Docker Compose developer environment
- Health/readiness endpoints reporting per-dependency state
- Operator console (`apps/console`), wired to real data only
- Tenant isolation: identity derived from the gateway-verified token
- SSRF controls with address normalisation and an operator-owned policy
- Contract tests, run in a repository-local `.venv`
- CI: lint, `mypy --strict`, tests, image scanning, SBOM, secret scanning
- Secure configuration via environment variables

## Connector runtime

Three adapter kinds are registered: `internal`, `http`, `webhook`. Dispatch is
registry-based and exhaustive — an unregistered kind is rejected at
registration, and there is no echo fallback, because a silent no-op reporting
success is the worst failure mode a connector runtime can have.

Every invocation is a persisted state machine (`accepted` → `running` →
terminal), not a request/response pair, so it survives the process for replay
and audit. Idempotency is enforced by a unique constraint on
`(tenant_id, connector_id, idempotency_key)`:

| Prior invocation | Repeat behaviour |
|---|---|
| succeeded | stored result returned, `200`, provider not called again |
| still running | `202` with the existing invocation id |
| failed terminally | stored terminal result returned |
| failed retryably | retried only per the explicit retry policy |

`idempotency_mode` on each connector states what the gateway can actually
promise — `provider_key`, `read_only`, `gateway_dedup_only` or `unsupported`.
`gateway_dedup_only` cannot guarantee exactly-once side effects if the process
dies after the provider accepts but before the local result commits.

### Outbound security

The HTTP adapter is an SSRF primitive without controls, so every outbound
request is guarded: allowlisted host, HTTPS by default, resolution of the
hostname with **every** returned address validated, and the connection pinned
to a validated address so a second lookup cannot rebind it. Loopback,
link-local, multicast and cloud metadata endpoints stay blocked even when a
connector explicitly permits private addresses — `allow_private_addresses`
means the internal network, not the service's own admin surface. Redirects
are followed manually and revalidated per hop; responses are size- and
content-type-capped; inbound `Authorization`, `Cookie` and `X-API-Key` are
never forwarded.

Secrets are referenced (`env:NAME`), resolved at the moment of use, and never
returned through the API, written to an event, or logged.

## Not yet implemented

Named here so the gap is explicit rather than assumed. The console shows the
same list as disabled navigation entries.

- MCP and A2A adapters, OAuth-heavy SaaS connectors, plugin execution.
- Hard deletion. `DELETE` performs a soft transition to
  `deletion_requested`; physical removal is a later privileged operation with
  its own preconditions.
- Workflow engine, model router, policy engine, memory services, billing,
  developer portal, SDKs.
- **Multi-tenancy is single-claim only.** Connectors, agents and invocations
  are all tenant-scoped (migrations 0007 and orchestrator 0005), and the
  internal service path enforces the caller's tenant rather than trusting the
  mesh token. What is missing is tenant administration: there is no way to
  create a tenant, no per-tenant quota, and the claim comes from a single
  Keycloak user attribute.
- **Production identity.** Keycloak runs in development mode against H2 with
  a repository realm. Secrets resolve from environment variables. Both are
  M1.7.
- **Event streaming to the console is polled, not streamed.** The event
  console refetches every three seconds and says so; a NATS-to-SSE bridge is
  the next step.

## Delivery guarantee

At-least-once, deduplicated at the stream. `packages/favl-outbox` stages an
event on the same session as the domain row, so one commit covers both:

```text
API request ──▶ single DB transaction ──┬──▶ domain row
                                        └──▶ outbox row
                                                │
                             outbox publisher ──┴──▶ JetStream ──▶ consumers
```

The publisher claims rows with `SELECT ... FOR UPDATE SKIP LOCKED` and
publishes inside the claiming transaction. If the process dies after the
broker acks but before the commit, the row returns to `pending` and is
published again — carrying the same `Nats-Msg-Id` (the outbox row id), so
JetStream collapses it inside a two-hour duplicate window. That is why the
guarantee is at-least-once plus dedup rather than exactly-once: a database
commit and a broker publish cannot be made atomic without a distributed
transaction.

Failures retry with jittered exponential backoff to a 300s cap. After
`max_attempts` (default 8) a row becomes `dead` and stops retrying, so a
poison event is visible in metrics rather than looping forever.

`SKIP LOCKED` means multiple replicas can run the publisher concurrently
without double-claiming a row.

### The duplicate-window invariant is release-blocking

Deduplication holds only while every retry lands inside the duplicate window.
That is a property of deployed configuration, so it is checked at startup
against the values the service actually runs with — not against constants
copied into a test. A service whose configuration breaches it fails to boot:

```text
DuplicateWindowTooSmall: outbox retry horizon exceeds the safe share of the
JetStream duplicate window: horizon=222389s window=7200s limit=5400s (75%).
```

The horizon counts more than the backoff curve. Each attempt also pays the
database lock wait, broker connect timeout, publish timeout, poll interval
and a process-restart budget — operational delay can push a row past the
window even when the retry loop alone would not. Current utilisation is
exported as `favl_outbox_duplicate_window_utilisation` and alerts above 0.75.

### Consumers must still deduplicate

The duplicate window is a safety net with an expiry, not a guarantee. A
consumer can see an event twice when the window lapses, a stream is replayed
or restored, an ack is lost, a subject is mirrored, or an operator
republishes. Every event therefore carries:

```json
{
  "event_id": "outbox-row-uuid",
  "event_type": "agent.created",
  "aggregate_type": "agent",
  "aggregate_id": "...",
  "aggregate_version": 1,
  "occurred_at": "2026-08-04T05:47:12.859514+00:00",
  "schema_version": 1,
  "data": { }
}
```

Deduplicate on `event_id` — it is stable across every republication and is
the same value used as `Nats-Msg-Id`. Use `aggregate_version` to reject stale
events and detect gaps; do not infer either from stream order.

## Outbound security

The HTTP adapter is an SSRF primitive without controls, so two rules govern
every outbound request.

**Reach is operator policy, not connector configuration.** `allow_private_
addresses`, `allowed_schemes` and `allow_plaintext_acknowledged` are read from
the deployment environment and rejected if they appear in a connector record.
They were previously caller-supplied, which let anyone who could create a
connector point a hostname at RFC1918 space and turn the gateway into an
authenticated proxy into the internal network. A destination must never be
able to vouch for itself; connector config may narrow the policy, never widen
it.

**Addresses are normalised before they are classified.**
`::ffff:169.254.169.254` reaches the cloud metadata service, but on Python
3.11 — what the containers run — `is_link_local` is False, because the stdlib
only began unwrapping v4-mapped forms in 3.12.4. The test venv runs 3.13, so
the suite passed while production was exposed. Every embedded-IPv4 form
(v4-mapped, 6to4, Teredo, NAT64) is unwrapped first, and forbidden ranges are
declared as explicit networks rather than inferred from `is_*` properties that
change between releases.

Beyond that: allowlisted hosts, HTTPS by default, every resolved address
validated, the connection pinned to a validated address to close the DNS
rebinding window, redirects revalidated per hop, response size and
content-type capped, and inbound `Authorization` / `Cookie` / `X-API-Key`
never forwarded. Connector configuration is redacted on every read path.

## Identity

Tenant and actor come from the token APISIX verified, never from a request
header. The gateway strips inbound `X-Tenant-ID` and `X-Actor-ID`; the service
reads claims from `X-Userinfo` and fails closed without them. Invocation reads
are tenant-scoped and return 404 rather than 403 on a mismatch, since
confirming that an id exists in another tenant is itself a disclosure.
`tests/verify_identity.sh` asserts a forged `X-Userinfo` leaks nothing.

## Operator console

`apps/console` — Next.js 15, React 19, Tailwind v4. Run it with the stack and
open `http://localhost:3100`.

The browser never holds a credential: route handlers attach a server-side
token and call through APISIX, so the console exercises the same authenticated
path an external client would.

Colour carries state and nothing else — there is no brand accent, so anything
coloured on screen means something. Steady state is still; only state
transitions animate, and `prefers-reduced-motion` is honoured.

6 sections are backed by real data: Workspace, Agents, Connectors,
Observability, Audit and Settings. The other 8 appear in the
navigation, disabled, each naming the milestone that delivers it. Nothing in
the console is mock data.

## Repository layout

```text
gateway/
  apisix.yaml            # routes, OIDC, identity header stripping
  config.yaml
packages/
  favl-outbox/           # transactional outbox library, shared by services
services/
  orchestrator/          # agents API, outbox publisher, migrations
  connector-registry/    # connectors API, adapters, invocations, migrations
apps/
  console/               # FAVL Command Center
deploy/
  docker-compose.yml     # images pinned by tag@digest
  kubernetes/            # probes, PDBs, migration Job, non-root workloads
  prometheus.yml, prometheus-alerts.yml
scripts/                 # build-gating checkers
tests/
```

## Development

```bash
make check          # ruff + format + mypy --strict + the Python suite
make test-outbox    # delivery guarantee, kills containers under load
make test-identity  # tenant isolation
```

CI runs the same commands, plus image scanning, SBOM generation and a
full-history secret scan. Two custom checkers gate the build and were each
validated against a deliberately introduced fault: `check_alert_metrics.py`
fails when an alert references a metric no code path emits, and
`check_image_pins.py` fails on any image reference without a digest.

## Local start

1. Copy the environment template:

```bash
cp .env.example .env
```

   Then set real values for `POSTGRES_PASSWORD`, `KEYCLOAK_CLIENT_SECRET`
   and `INTERNAL_SERVICE_TOKEN`. All three ship as placeholders, and the
   client is confidential, so the token request below fails without the
   secret.

2. Start the stack:

```bash
docker compose --env-file .env -f deploy/docker-compose.yml up -d --build
```

   Migrations run automatically from each service's entrypoint. PostgreSQL is
   published on `127.0.0.1:5435` for local inspection only.

3. Obtain a Keycloak token:

```bash
curl -s \
  -d "client_id=agentic-content-os" \
  -d "client_secret=$KEYCLOAK_CLIENT_SECRET" \
  -d "username=demo" \
  -d "password=demo-password" \
  -d "grant_type=password" \
  http://localhost:8080/realms/favl/protocol/openid-connect/token
```

4. Call the gateway:

```bash
curl -H "Authorization: Bearer $TOKEN" \
  http://localhost:9080/v1/agents
```

## Verifying persistence

Records must outlive their process. Create an agent, restart the services,
and confirm it is still there:

```bash
docker compose --env-file .env -f deploy/docker-compose.yml \
  restart orchestrator connector-registry
curl -s http://localhost:9080/health/ready
```

Readiness reports each dependency separately:

```json
{"status": "ready", "database_connected": true, "nats_connected": true}
```

Published events are durable and replayable from the `FAVL_EVENTS` stream:

```bash
curl -s "http://localhost:8222/jsz?streams=1"
```

## Verifying the outbox

`tests/verify_outbox.sh` exercises the delivery guarantee against the running
stack. It disables the publisher to create a committed-but-unpublished
backlog, restarts it, replays event ids to prove deduplication, takes the
broker down mid-write, injects a poison event, and hard-kills the
orchestrator four times during a 300-write load:

```bash
docker compose --env-file .env -f deploy/docker-compose.yml up -d --build
bash tests/verify_outbox.sh
```

It takes several minutes and asserts 15 conditions. It writes test rows into
the development database, so do not point it at anything you care about.

## Where this is

M1.4 (connector lifecycle) is complete and gated. The next milestone,
**M1.5 — Credential and Installation Lifecycle**, is briefed in
`docs/M1.5-PLAN.md` with its core invariant, the design decisions already
settled, and the seven gates it must pass. Start there rather than from this
README.

## Connector lifecycle

A connector is a state machine, not a row with an `enabled` flag. The states
and the edges between them live in `services/connector-registry/app/lifecycle.py`;
nothing else is permitted to write `connectors.status`.

```
draft -> installed -> configured -> validated -> enabled <-> disabled
                          ^                                    |
                          +------- reconfigure -----------------+

any live state -> revoked        (one-way, reason required)
disabled|revoked -> deletion_requested -> archived -> deleted (privileged)
```

Two rules carry the security weight:

- **Only `enabled` may serve an invocation.** `EXECUTABLE_STATES` is a set of
  exactly one, so a state added later is non-executable until somebody puts it
  there deliberately. The check reads the connector row inside the transaction
  that is about to use it — there is no cache to invalidate.
- **Revocation is immediate and one-way.** A connector revoked between two
  requests cannot serve the second. It cannot be re-enabled, only replaced,
  because restoring it would return a compromised credential to service
  without rotating it.

Every transition writes an immutable `connector_audit` row — actor, reason,
from-state, to-state, aggregate version — in the same transaction as the state
change and the outbox event. The table is append-only at the database level
via trigger, so a later code change cannot rewrite history, and the connector
row is FK-protected against deletion out from under its own trail.

```bash
curl -X POST -H "Authorization: Bearer $TOKEN" \
  -d '{"reason": "credential suspected compromised"}' \
  http://localhost:9080/v1/connectors/$ID/revoke

curl -H "Authorization: Bearer $TOKEN" \
  http://localhost:9080/v1/connectors/$ID/audit
```

`tests/verify_lifecycle.sh` asserts 41 conditions against the running stack,
including the one that matters: it revokes a connector **while an invocation
is in flight** and proves the next call is refused with 403
`CONNECTOR_REVOKED`. Evidence, and the run where that gate was watched
failing on purpose, are in `gates/G3/`.

```bash
make test-lifecycle
```

Revocation blocks new use. An invocation already accepted runs to completion
under the authority snapshot pinned when it was accepted — that is the
decision, not an oversight, and `verify_lifecycle.sh` asserts it. The
reasoning, the emergency-revocation design that is **not yet built**, and the
limits of what the platform can honestly promise are in
`docs/adr/0002-connector-revocation-semantics.md`.

The guarantee is deliberately narrow:

> The platform ceases authorising further connector actions as soon as the
> revocation becomes authoritative.

Not "all activity stops". Provider-side work already accepted remotely is not
ours to cancel, and claiming otherwise would mislead exactly the operator who
most needs the truth.

## Observability

Prometheus runs on `http://localhost:9092`. All five scrape targets should
report `up`:

```bash
curl -s 'http://localhost:9092/api/v1/targets?state=active' \
  | python3 -c 'import json,sys; [print(t["labels"]["job"], t["health"]) for t in json.load(sys.stdin)["data"]["activeTargets"]]'
```

NATS metrics come from `prometheus-nats-exporter` on port 7777. Prometheus
cannot scrape the NATS `/varz` endpoint directly — it serves JSON, not the
Prometheus text format. APISIX serves metrics at
`/apisix/prometheus/metrics`, not `/metrics`.

Alert rules live in `deploy/prometheus-alerts.yml`. The primary outbox signal
is backlog *age*, not size: a large backlog that is draining is healthy,
while one row stuck for a minute is not.

## Health probes

Two distinct contracts, because conflating them turns a dependency outage
into a restart storm:

| Endpoint | Checks | Use for |
|---|---|---|
| `/livez` | process event loop only | liveness probe |
| `/readyz` | database, NATS, stream, outbox backlog | readiness probe, load-balancer gating |

`/health/live` and `/health/ready` remain as aliases for the existing APISIX
`/health/*` route.

## Production rules

- Do not use the development Keycloak realm or passwords.
- Use an external PostgreSQL database for Keycloak.
- Use an external secret manager and short-lived workload identity.
- Run at least three NATS JetStream replicas for HA.
- Enable TLS at the load balancer and re-encrypt gateway-to-service traffic.
- Apply Kubernetes NetworkPolicies and Pod Security Standards.
- Pin container image digests before release.
- Configure APISIX routes through GitOps and admission checks.
