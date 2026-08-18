#!/usr/bin/env bash
# bootstrap_local.sh — stand up the Veripsa foundation (db/schema.sql) as a runnable instance, with the
# demo account + seats provisioned. The basis for local dev, the MCP/web console, and the gate runner.
#   bash db/bootstrap_local.sh [db_name]
set -euo pipefail
cd "$(dirname "$0")/.."
DB="${1:-veripsa_local}"
ADMIN="${ADMIN_DSN:-postgresql://localhost/postgres}"
echo "=== bootstrap_local: the Veripsa foundation (db=${DB}) ==="
echo "-- roles --"; psql "$ADMIN" -q -f db/roles.sql
echo "-- (re)create db owned by the migrator --"; dropdb "$DB" 2>/dev/null || true; createdb "$DB" -O veripsa_migrator
MIG="postgresql://veripsa_migrator@localhost/${DB}"
echo "-- apply schema.sql --"; psql "$MIG" -v ON_ERROR_STOP=1 -q -f db/schema.sql
echo "-- provision the demo account + seats (frontend / api / owner) --"
psql "$MIG" -v ON_ERROR_STOP=1 -q -c "SET search_path=core;
  SELECT core.provision_seat('ACCT-DEMO','Demo Co','AG-A','frontend','veripsa_demo_agent',NULL,'claude-opus-4-8');
  SELECT core.provision_seat('ACCT-DEMO','Demo Co','AG-B','api','veripsa_demo_agent2',NULL,'claude-sonnet-4-6');
  SELECT core.provision_seat('ACCT-DEMO','Demo Co','AG-S','owner','veripsa_demo_steward');
  SELECT core.provision_seat('ACCT-DEMO','Demo Co','AG-APP','Veripsa App','veripsa_app');"
echo "=== ready: ${DB} (new foundation, demo account ACCT-DEMO provisioned) ==="
echo "  agents connect as veripsa_demo_agent@localhost/${DB}; owner reads as veripsa_demo_steward."
