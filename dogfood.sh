#!/usr/bin/env bash
# dogfood.sh — develop Veripsa against a REAL repo's code graph (no fabricated demo data).
# Bootstrap a clean instance, ingest the real code graph THROUGH THE GATE (content-free: paths/symbols/
# edges/tables, never bodies), then exercise the GitHub-App brain (main-impact prediction) against it.
#   bash dogfood.sh                       # DB=veripsa_dogfood, repo=. (this repo)
#   bash dogfood.sh <db_name> <repo_path>
set -uo pipefail
cd "$(dirname "$0")"
DB="${1:-veripsa_dogfood}"
REPO="${2:-.}"
WRITER_DSN="postgresql://veripsa_demo_agent@localhost/${DB}"

echo "=== dogfood: Veripsa on a REAL repo (${REPO}) ==="
[ -d "$REPO" ] || { echo "repo not found: ${REPO}"; exit 2; }

echo "-- 1/2 bootstrap (roles + schema + agent seats) --"
bash db/bootstrap_local.sh "$DB" >/tmp/dogfood_boot.log 2>&1 \
  || { echo "bootstrap FAILED — see /tmp/dogfood_boot.log"; tail -8 /tmp/dogfood_boot.log; exit 2; }

echo "-- 2/2 ingest the real code graph through the gate (content-free) --"
VERIPSA_DSN="$WRITER_DSN" python3 code_graph_extract.py "$REPO" --push || { echo "ingest FAILED"; exit 2; }

echo "=== dogfood ready on ${DB} (graph of ${REPO}) ==="
echo "exercise the GitHub-App brain (PR lifecycle on the real gate):"
echo "    python3 github-app/trial_offline.py"
echo "inspect what's heading to main directly:"
echo "    psql \"$WRITER_DSN\" -c \"SET search_path=core; SELECT jsonb_pretty(core.main_impact_surface(''))\""
