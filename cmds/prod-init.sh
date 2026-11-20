#! /bin/bash

# Initialize a production argonath environment. Baked into the argonath-worker
# image (on PATH as `argonath-prod-init`) and meant to be run as a k8s Job, see
# argonath-deployment's kustomize/init and cmds/prod-init.sh. Every step is
# safe to re-run.
#
# Expected env (all provided by the init Job):
#   CASKFS_PG_HOST, POSTGRES_USER, POSTGRES_PASSWORD - shared postgres instance
#   CASKFS_PG_PASSWORD                               - cask cli direct-pg access
#   DIGTK_POSTGRES_* / DIGTK_REPORTING_PG_*          - digtk schemas
#   CASK_ADMIN_USERS                                 - optional, comma separated
#                                                      list of cask admin users

set -e

PG_HOST=${CASKFS_PG_HOST:-postgres}
PG_PORT=${CASKFS_PG_PORT:-5432}
PG_USER=${POSTGRES_USER:-postgres}
PG_DATABASE=${CASKFS_PG_DATABASE:-postgres}
AUTO_PATH_RULES=${AUTO_PATH_RULES:-/opt/argonath/cask/auto-path-rules.json}
SERVICE_ADMIN_USERS="dagster dcs-gateway"

export PGPASSWORD=${POSTGRES_PASSWORD:-$CASKFS_PG_PASSWORD}

echo "Initializing argonath (postgres: $PG_USER@$PG_HOST:$PG_PORT/$PG_DATABASE)"

# wait for the shared postgres instance to accept connections
echo "Waiting for postgres"
ATTEMPTS=0
until pg_isready -h $PG_HOST -p $PG_PORT -U $PG_USER > /dev/null 2>&1; do
  ATTEMPTS=$((ATTEMPTS+1))
  if [[ $ATTEMPTS -ge 60 ]]; then
    echo "Error: postgres at $PG_HOST:$PG_PORT not ready after 120s"
    exit 1
  fi
  sleep 2
done

# create the per-app databases and schemas that share the main postgres
# instance. Mirrors argonath-deployment's compose/local-dev/initdb sql, minus
# the dagster database (prod dagster uses its own dagster-postgres instance,
# which creates it via POSTGRES_DB). Safe to re-run.
echo "Creating databases and schemas"
psql -v ON_ERROR_STOP=1 -h $PG_HOST -p $PG_PORT -U $PG_USER -d $PG_DATABASE <<'EOSQL'
SELECT 'CREATE DATABASE superset'
WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'superset')\gexec

SELECT 'CREATE DATABASE dcf'
WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'dcf')\gexec

CREATE SCHEMA IF NOT EXISTS auth_gateway;
EOSQL

# install the pg cask tables. With no cask env file the cli talks directly to
# postgres using the CASKFS_PG_* env. Safe to re-run.
echo "Initializing cask"
cask init-pg

# grant the cask admin role to the service users, plus any users listed in
# CASK_ADMIN_USERS (comma separated). Safe to re-run.
IFS=',' read -ra HUMAN_ADMIN_USERS <<< "$CASK_ADMIN_USERS"
for ADMIN_USER in $SERVICE_ADMIN_USERS "${HUMAN_ADMIN_USERS[@]}"; do
  ADMIN_USER=$(echo "$ADMIN_USER" | xargs)
  if [[ -z "$ADMIN_USER" ]]; then
    continue
  fi
  echo "Granting cask admin role to: $ADMIN_USER"
  cask -i admin acl user-role-set "$ADMIN_USER" admin
done

# install digtk's pg schemas: collection_registry (collection registrations,
# the queue/results store Dagster's item jobs use) and the reporting schema
# every derivative step writes to (--reporting-job-id). Both are safe to
# re-run.
echo "Initializing digtk schemas"
digtk state init-pg
digtk reporting init-db

# load the cask auto-path (partition key) rules. Safe to re-run.
echo "Loading cask auto-path rules: $AUTO_PATH_RULES"
cask -i admin auto-path load $AUTO_PATH_RULES

echo "Done initializing argonath"
