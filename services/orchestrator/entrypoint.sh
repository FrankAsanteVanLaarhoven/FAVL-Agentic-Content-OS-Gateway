#!/bin/sh
# Migrations run before the app accepts traffic. Single replica per service
# today; with multiple replicas this must move to a Kubernetes Job or an
# init container so concurrent instances cannot race on the alembic lock.
set -eu

echo "entrypoint: applying migrations"
alembic upgrade head

echo "entrypoint: starting uvicorn"
exec uvicorn app.main:app --host 0.0.0.0 --port 8000
