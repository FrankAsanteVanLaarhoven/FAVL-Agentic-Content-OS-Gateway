.PHONY: test test-outbox up down validate

COMPOSE = docker compose --env-file .env -f deploy/docker-compose.yml

# Fast, offline: contract and outbox-logic tests. Needs pydantic, sqlalchemy,
# nats-py and prometheus-client on the host.
test:
	python -m pytest -q

# Slow: exercises the delivery guarantee against the running stack, including
# broker outages and hard kills. Writes test rows into the dev database.
test-outbox:
	bash tests/verify_outbox.sh

up:
	$(COMPOSE) up -d --build

down:
	$(COMPOSE) down

validate:
	$(COMPOSE) config >/dev/null
