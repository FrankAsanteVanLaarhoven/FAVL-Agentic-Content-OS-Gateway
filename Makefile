.PHONY: test test-outbox venv up down validate clean-venv

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
