.PHONY: test up down validate

test:
	python -m pytest -q

up:
	docker compose --env-file .env -f deploy/docker-compose.yml up --build

down:
	docker compose --env-file .env -f deploy/docker-compose.yml down

validate:
	docker compose --env-file .env -f deploy/docker-compose.yml config >/dev/null
