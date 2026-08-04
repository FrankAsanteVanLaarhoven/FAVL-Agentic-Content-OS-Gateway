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
- Contract tests, run in a repository-local `.venv`
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

Named here so the gap is explicit rather than assumed:

- MCP and A2A adapters, OAuth-heavy SaaS connectors, plugin execution.
- Hard deletion. `DELETE` performs a soft transition to
  `deletion_requested`; physical removal is a later privileged operation with
  its own preconditions.
- Workflow engine, model router, policy engine, memory services, billing,
  developer portal, SDKs.

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

## Repository layout

```text
gateway/
  apisix.yaml
  config.yaml
packages/
  favl-outbox/           # transactional outbox library, shared by services
services/
  orchestrator/          # agents API, outbox publisher, migrations
  connector-registry/    # connectors API, outbox publisher, migrations
deploy/
  docker-compose.yml
  postgres/
  prometheus.yml
  kubernetes/
tests/
```

## Local start

1. Copy the environment template:

```bash
cp .env.example .env
```

   Then set a real `POSTGRES_PASSWORD`; `change-me` is a placeholder.

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
