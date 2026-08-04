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
- NATS JetStream event publishing with server acks
- OpenTelemetry instrumentation hooks
- Prometheus scraping of APISIX, NATS and the collector
- Kubernetes Gateway API examples
- Docker Compose developer environment
- Health/readiness endpoints reporting per-dependency state
- Contract tests
- Secure configuration via environment variables

## Not yet implemented

Named here so the gap is explicit rather than assumed:

- Connector adapters. `POST /internal/connectors/{id}/invoke` validates the
  connector and echoes the request; it does not yet dispatch on `kind` or
  call `base_url`.
- Transactional outbox. The database write and the JetStream publish are
  separate operations, so a crash between them loses the event. Publish
  failures are logged and surfaced via `/health/ready`, never swallowed.
- Workflow engine, model router, policy engine, memory services, billing,
  developer portal, SDKs.

## Repository layout

```text
gateway/
  apisix.yaml
  config.yaml
services/
  orchestrator/          # agents API, JetStream publisher, migrations
  connector-registry/    # connectors API, migrations
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

## Observability

Prometheus runs on `http://localhost:9092`. All three scrape targets should
report `up`:

```bash
curl -s 'http://localhost:9092/api/v1/targets?state=active' \
  | python3 -c 'import json,sys; [print(t["labels"]["job"], t["health"]) for t in json.load(sys.stdin)["data"]["activeTargets"]]'
```

NATS metrics come from `prometheus-nats-exporter` on port 7777. Prometheus
cannot scrape the NATS `/varz` endpoint directly — it serves JSON, not the
Prometheus text format. APISIX serves metrics at
`/apisix/prometheus/metrics`, not `/metrics`.

## Production rules

- Do not use the development Keycloak realm or passwords.
- Use an external PostgreSQL database for Keycloak.
- Use an external secret manager and short-lived workload identity.
- Run at least three NATS JetStream replicas for HA.
- Enable TLS at the load balancer and re-encrypt gateway-to-service traffic.
- Apply Kubernetes NetworkPolicies and Pod Security Standards.
- Pin container image digests before release.
- Configure APISIX routes through GitOps and admission checks.
