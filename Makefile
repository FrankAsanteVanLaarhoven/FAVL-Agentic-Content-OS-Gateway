.PHONY: test test-outbox test-identity venv up down validate clean-venv

COMPOSE = docker compose --env-file .env -f deploy/docker-compose.yml
VENV    = .venv
# PYTHONPATH is cleared deliberately: this host has ROS 2 Humble on the global
# PYTHONPATH, and pytest would otherwise autoload its plugins into our venv.
# PYTHONNOUSERSITE keeps ~/.local out for the same reason.
PY      = env -u PYTHONPATH PYTHONNOUSERSITE=1 $(VENV)/bin/python

# Repository-local environment. The host base interpreter is deliberately not
# part of the reproducibility contract, so tests never depend on what happens
# to be installed globally.
$(VENV)/.stamp: requirements-dev.txt
	python3 -m venv $(VENV)
	$(PY) -m pip install --quiet --upgrade pip
	$(PY) -m pip install --quiet -r requirements-dev.txt
	touch $@

venv: $(VENV)/.stamp

# Fast, offline: contract tests and outbox logic. No database or broker.
test: venv
	$(PY) -m pytest -q

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

clean-venv:
	rm -rf $(VENV)

# Each service uses the package name `app`, so they cannot be type-checked in
# one invocation; mypy is run once per project root with the shared package on
# MYPYPATH exactly as the containers resolve it.
MYPY = env -u PYTHONPATH MYPYPATH=$(PWD)/packages/favl-outbox $(PWD)/.venv/bin/mypy --config-file $(PWD)/pyproject.toml

.PHONY: lint format typecheck check
lint: venv
	env -u PYTHONPATH .venv/bin/ruff check .
	env -u PYTHONPATH .venv/bin/ruff format --check .

format: venv
	env -u PYTHONPATH .venv/bin/ruff check . --fix
	env -u PYTHONPATH .venv/bin/ruff format .

typecheck: venv
	$(MYPY) packages/favl-outbox/favl_outbox
	cd services/orchestrator && $(MYPY) app
	cd services/connector-registry && $(MYPY) app

# Everything CI runs, in the order CI runs it.
check: lint typecheck test

test-identity:
	bash tests/verify_identity.sh

# Drives an alert through fire-and-clear against the live stack. Slow by
# nature: the rules carry for-clauses measured in minutes.
test-alerts:
	bash tests/verify_alerts.sh

.PHONY: test-identity test-alerts
