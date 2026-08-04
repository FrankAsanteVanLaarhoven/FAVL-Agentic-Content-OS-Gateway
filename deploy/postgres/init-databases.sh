#!/bin/bash
# Creates one database per service. Service boundaries are enforced at the
# database level so no service can read another's tables.
set -euo pipefail

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<-EOSQL
	CREATE DATABASE favl_orchestrator;
	CREATE DATABASE favl_connectors;
EOSQL
