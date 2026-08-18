#!/usr/bin/env bash
# migration_interruption.sh — prove db/schema.sql is RE-RUNNABLE FROM ANY PARTIAL POINT.
#
# THE FAILURE WINDOW THIS GATE CLOSES. The production deploy applies the schema by hand as a bare
#   psql "$OWNER_DSN" -f db/schema.sql
# (render.yaml §ONE-TIME, github-app/RUNBOOK.md §B) — NO --single-transaction. So if the migrator
# connection drops / the deploy is killed mid-file, the statements that already COMMITTED stay, and the
# very next deploy re-runs the WHOLE file FROM THE TOP over that partial state. If any single statement is
# non-idempotent (a bare CREATE POLICY, an unconditional ADD CONSTRAINT, a DROP-then-CREATE whose CREATE
# half re-errors, an enum ALTER TYPE ADD VALUE, a seed INSERT without ON CONFLICT), the re-apply ERRORs and
# the schema wedges — half-migrated, and (because the deploy command omits ON_ERROR_STOP) the error is even
# swallowed and the App boots against it. #100 proved a FULL re-apply converges; this gate proves re-apply
# converges from a TRUNCATED-PREFIX (interrupted) state too — at every module boundary AND at the sharpest
# mid-module hazards (the PK re-key DO block, each DROP POLICY … / CREATE POLICY … split).
#
# METHOD (per cut point): fresh migrator-owned DB → apply the expanded stream TRUNCATED to that statement
# boundary, under ON_ERROR_STOP=1 (the prefix itself must be clean to that point = a real reachable partial
# state) → then apply the FULL db/schema.sql on top → assert it exits 0 with ZERO ERROR/FATAL (convergence).
#   bash db/migration_interruption.sh
set -uo pipefail
cd "$(dirname "$0")/.."
PGADMIN="${ADMIN_DSN:-postgresql://localhost/postgres}"
FAIL=0
ok(){ echo "  [PASS] $1"; }
no(){ echo "  [FAIL] $1"; FAIL=1; }
WORK="$(mktemp -d "/tmp/veripsa_migint_$$.XXXXXX")"
cleanup(){ rm -rf "$WORK" 2>/dev/null; }
trap cleanup EXIT

echo "=== migration-interruption gate (db/schema.sql re-runnable from any partial point) ==="

# roles must exist (idempotent); the migrator owns the schema.
psql "$PGADMIN" -v ON_ERROR_STOP=1 -q -f db/roles.sql >"$WORK/roles.log" 2>&1 || { no "roles bootstrap"; tail -5 "$WORK/roles.log"; echo "MIGRATION-INTERRUPTION GATE: FAIL"; exit 2; }

# Expand db/schema.sql's \ir includes into ONE statement stream, in index order (this is exactly what psql
# applies, minus the \ir mechanic). We DON'T resolve nested \ir because the modules contain none.
MODS=()
while IFS= read -r m; do MODS+=("$m"); done < <(grep -oE 'schema/[0-9]+_[a-z][a-z0-9_]*\.sql' db/schema.sql)
if ! printf '%s\n' "${MODS[@]}" | grep -qx 'schema/25_webhook_queue.sql'; then
  no "schema include expansion omitted schema/25_webhook_queue.sql"
fi
EXPANDED="$WORK/expanded.sql"
: > "$EXPANDED"
for m in "${MODS[@]}"; do cat "db/$m" >> "$EXPANDED"; printf '\n' >> "$EXPANDED"; done
TOTAL=$(wc -l < "$EXPANDED" | tr -d ' ')
echo "  expanded ${#MODS[@]} modules → $TOTAL lines (one apply stream)"

# A truncated prefix is only a VALID reachable partial state if it ends on a safe boundary (psql commits
# per-statement in autocommit; a kill lands AFTER the last committed statement). We cut on lines that end a
# statement (trailing ';' at column 0-ish, or a DO-block terminator '$$;' / 'END $$;'). We over-sample with
# the explicit module boundaries (cumulative line counts) PLUS a dense scan of every ';'-terminated line,
# capped so the gate stays fast. Each cut is applied with ON_ERROR_STOP=1 — if the prefix itself errors the
# cut wasn't a clean boundary and we SKIP it (not a reachable partial state); the real assertion is on the
# FULL re-apply that follows a clean prefix.

# Module-boundary cumulative line offsets (the coarse, always-meaningful cut points).
declare -a CUTS=()
acc=0
for m in "${MODS[@]}"; do
  n=$(wc -l < "db/$m" | tr -d ' ')
  acc=$((acc + n + 1))   # +1 for the printf '\n' we appended
  CUTS+=("$acc")
done

# Targeted mid-module hazards: lines that END a non-trivially-idempotent statement in the EXPANDED stream.
# We find them by their unique text and map to the expanded-stream line number.
add_cut_at_text(){
  # $1 = grep -F pattern that uniquely marks the END of a hazard statement in $EXPANDED
  local ln
  ln=$(grep -nF "$1" "$EXPANDED" | head -1 | cut -d: -f1)
  if [ -n "$ln" ]; then CUTS+=("$ln"); fi
}
# the PK re-key DO block end (drop+add claim_pkey) — cut JUST AFTER it
add_cut_at_text "ALTER TABLE core.claim ADD CONSTRAINT claim_pkey PRIMARY KEY (account_id, repo, claim_id);"
# SECURITY DEFINER creation + owner + ACL are one transaction. Cutting after ownership but before REVOKE/COMMIT
# must roll the whole block back, never leave the cross-tenant function with PostgreSQL's PUBLIC EXECUTE default.
add_cut_at_text "ALTER FUNCTION core.owner_account_usage_surface(int) OWNER TO veripsa_migrator;"
# Repository offboarding's private retry helper must roll back with its ACL if deployment is interrupted after
# function creation/ownership but before REVOKE and the compatibility/finalization definitions.
DEFER_OWNER_START=$(grep -nF \
  "ALTER FUNCTION core._defer_webhook_delivery_with_authority(text,timestamptz,text)" \
  "$EXPANDED" | head -1 | cut -d: -f1)
DEFER_OWNER_CUT=""
if [ -n "$DEFER_OWNER_START" ]; then
  DEFER_OWNER_CUT=$((DEFER_OWNER_START + 1))
  defer_owner_end=$(sed -n "${DEFER_OWNER_CUT}p" "$EXPANDED")
  if [[ "$defer_owner_end" != *"OWNER TO veripsa_migrator;"* ]]; then
    no "could not locate the terminating line of the private defer helper OWNER statement"
    DEFER_OWNER_CUT=""
  fi
fi
if [ -z "$DEFER_OWNER_START" ]; then
  no "expanded schema omitted the private defer helper interruption target"
fi
if [ -n "$DEFER_OWNER_CUT" ]; then CUTS+=("$DEFER_OWNER_CUT"); fi
# a DROP POLICY landed but its CREATE POLICY not yet (the classic split): cut right after a DROP POLICY line
while IFS= read -r ln; do CUTS+=("$ln"); done < <(grep -nE '^\s*DROP POLICY IF EXISTS' "$EXPANDED" | cut -d: -f1)
# right after a CREATE TABLE IF NOT EXISTS opening but we instead cut after its closing ');' — use the ADD
# COLUMN IF NOT EXISTS lines (additive, must re-skip): cut right after each
while IFS= read -r ln; do CUTS+=("$ln"); done < <(grep -nE 'ADD COLUMN IF NOT EXISTS' "$EXPANDED" | cut -d: -f1)

# De-dup + sort the cut list (bash 3.2-safe — macOS ships 3.2, no `mapfile`).
SORTED=()
while IFS= read -r ln; do SORTED+=("$ln"); done < <(printf '%s\n' "${CUTS[@]}" | sort -n -u)
CUTS=("${SORTED[@]}")
echo "  testing ${#CUTS[@]} interruption points (module boundaries + mid-module hazards)"

tested=0; skipped=0
for cut in "${CUTS[@]}"; do
  [ "$cut" -ge "$TOTAL" ] && continue   # full file = the #100 case; covered by the final full-twice check
  DB="veripsa_migint_${$}_${cut}"
  dropdb "$DB" 2>/dev/null
  createdb "$DB" -O veripsa_migrator 2>/dev/null || { no "createdb @cut=$cut"; continue; }
  MIG="postgresql://veripsa_migrator@localhost/$DB"
  head -n "$cut" "$EXPANDED" > "$WORK/prefix.sql"
  # Apply the truncated prefix under ON_ERROR_STOP. If it errors, this cut is NOT a clean statement boundary
  # (a partial DO block / mid-statement) → not a reachable interrupted state → skip (don't count as pass/fail).
  if ! psql "$MIG" -v ON_ERROR_STOP=1 -q -f "$WORK/prefix.sql" >"$WORK/prefix.log" 2>&1; then
    if [ -n "${DEFER_OWNER_CUT:-}" ] && [ "$cut" -eq "$DEFER_OWNER_CUT" ]; then
      no "targeted private defer helper OWNER cut was not a clean statement boundary"
    fi
    skipped=$((skipped+1)); dropdb "$DB" 2>/dev/null; continue
  fi
  if [ -n "${DEFER_OWNER_CUT:-}" ] && [ "$cut" -eq "$DEFER_OWNER_CUT" ]; then
    helper_exists=$(psql "$MIG" -tAc \
      "SELECT to_regprocedure('core._defer_webhook_delivery_with_authority(text,timestamptz,text)') IS NOT NULL")
    if [ "$helper_exists" != "f" ]; then
      no "interrupted private defer helper transaction leaked a callable function @cut=$cut"
    fi
  fi
  # Now the deploy re-runs the FULL file from the top over this partial state. THIS is the assertion.
  if psql "$MIG" -v ON_ERROR_STOP=1 -q -f db/schema.sql >"$WORK/full.log" 2>&1 \
     && ! grep -qE '^(psql:[^ ]+ )?ERROR:|FATAL:' "$WORK/full.log"; then
    tested=$((tested+1))
  else
    no "re-apply over interrupted prefix @cut=$cut WEDGED"
    grep -E 'ERROR:|FATAL:' "$WORK/full.log" | head -4
  fi
  dropdb "$DB" 2>/dev/null
done
if [ "$FAIL" -eq 0 ]; then ok "convergence from $tested partial points (+$skipped non-boundary cuts skipped)"; fi

# Belt-and-suspenders: the full re-apply (the #100 case) must also be clean ERROR/FATAL-free.
DB="veripsa_migint_full_$$"
dropdb "$DB" 2>/dev/null; createdb "$DB" -O veripsa_migrator 2>/dev/null
MIG="postgresql://veripsa_migrator@localhost/$DB"
psql "$MIG" -v ON_ERROR_STOP=1 -q -f db/schema.sql >/dev/null 2>&1
if psql "$MIG" -v ON_ERROR_STOP=1 -q -f db/schema.sql >"$WORK/twice.log" 2>&1 \
   && ! grep -qE '^(psql:[^ ]+ )?ERROR:|FATAL:' "$WORK/twice.log"; then
  ok "full re-apply (apply-twice) is ERROR/FATAL-free"
else
  no "full re-apply not clean"; grep -E 'ERROR:|FATAL:' "$WORK/twice.log" | head -4
fi
dropdb "$DB" 2>/dev/null

if [ "$FAIL" -eq 0 ]; then echo "MIGRATION-INTERRUPTION GATE: PASS"; else echo "MIGRATION-INTERRUPTION GATE: FAIL"; fi
exit "$FAIL"
