#!/usr/bin/env bash
# run_gates.sh — Veripsa release gates (lean, on db/schema.sql). One command answers "is this releasable?".
# Proves: the FOUNDATION (lock · on-ramp queue · the moat · main-impact prediction · contention clusters ·
# schema-graph coupling · the no-乱立 table budget), the EXTRACTOR (import→file resolution · multi-language ·
# schema DDL/DML), and the GITHUB-APP BRAIN (a real PR lifecycle: predict → serialize/warn → land → promote).
#   bash run_gates.sh
# Each gate runs as a step; any failure flips FAIL=1, and the final line prints RELEASE GATES: PASS/FAIL.
set -uo pipefail
cd "$(dirname "$0")"
DB="veripsa_gates_$$"
# PER-RUN log dir: each gate's stdout went to a FIXED $LOGD/<x>.log, so concurrent runs (parallel CI
# shards / several agents each running run_gates) clobbered each other's logs → one run greps another's
# half-written log for the PASS marker → spurious FAIL. Per-PID (like the scratch DB) makes runs independent.
LOGD="/tmp/veripsa_gates_$$"
mkdir -p "$LOGD"
FAIL=0
# Exit convention: any failing gate flips FAIL=1 (via no()) → script exits non-zero (1, or 2 for bootstrap); exit 0 ONLY when every gate passed.
ok(){ echo "  [PASS] $1"; }
no(){ echo "  [FAIL] $1"; FAIL=1; }

# PRIVATE, EPHEMERAL POSTGRES per run. The per-PID scratch DBs ($DB here, veripsa_smoke_$$ in db/smoke.sh,
# veripsa_*test_<pid> in tests/*.py) used to land on the developer's SHARED postmaster → parallel agents
# contended, scratch DBs leaked when a run was killed, and load-induced flakes looked like real FAILs. Now
# each run gets its OWN throwaway cluster (own datadir + port + socket), exported via PGHOST/PGPORT so every
# downstream gate (shell + python) lands on it WITHOUT editing a single source DSN. Torn down on exit (below),
# so concurrent runs never collide or leak. Opt out with VERIPSA_EPHEMERAL_PG=0 (use the ambient postmaster).
source "$(dirname "$0")/db/_ephemeral_pg.sh"
# Compose teardowns into ONE EXIT trap: drop the (now ephemeral) scratch DB + per-run logs, then nuke the
# whole private cluster. ephemeral_pg_teardown is idempotent + never errors, so a kill mid-run still cleans up.
cleanup(){ dropdb "$DB" 2>/dev/null; rm -rf "$LOGD" 2>/dev/null; ephemeral_pg_teardown; }
# EXIT-only (not INT/TERM/HUP): bash still runs the EXIT trap when the shell dies from an uncaught fatal
# signal, so a `kill` of run_gates still tears the cluster down — while NOT firing prematurely on a stray
# harmless signal mid-run. (The ephemeral helper arms its own EXIT trap too; this composed one supersedes it.)
trap cleanup EXIT
if ! ephemeral_pg_start; then
  echo "RELEASE GATES: FAIL (could not stand up the ephemeral test Postgres)"; exit 2
fi

echo "=== Veripsa release gates (scratch DB: $DB) ==="
# --- standard-gate registry (AUTO-DISCOVERED — no more append-hotspot) ---------------------------------------
# Previously every gate was a hand-written `if ... grep ... then ok ... else no ...; tail` block appended to THIS
# file, so two concurrent PRs each adding a gate edited adjacent lines = a self-inflicted merge conflict on the
# registration list (the exact low-value-collision pattern Veripsa itself softens). Now every STANDARD gate (one
# `python3 <test>` whose success is a `<MARKER>` line in its log) lives as ONE tiny file under gates.d/ that calls
# register_gate. run_gates.sh globs gates.d/*.gate in sorted order, sources them, and runs each through the SAME
# proven check. A new gate = a NEW FILE in gates.d/; two PRs adding two gates add two DISTINCT files = git
# auto-merges, zero false conflict. The handful of NON-standard gates (bootstrap / smoke / migration-interruption
# / extractor / brain / whole-graph N-scale — bash-invoked, multi-marker, or arg'd) stay inline below.
# register_gate <test_path> <pass_marker> <name> <ok_blurb> <desc> [tail_lines] [timeout_seconds]
# Standard gates default to five minutes. A wedged DB/query must fail this gate, not consume the workflow's
# 55-minute outer budget and hide every later result.
_G_TEST=(); _G_MARK=(); _G_NAME=(); _G_OK=(); _G_DESC=(); _G_TAIL=(); _G_TIMEOUT=()
register_gate(){
  _G_TEST+=("$1"); _G_MARK+=("$2"); _G_NAME+=("$3"); _G_OK+=("$4"); _G_DESC+=("$5")
  _G_TAIL+=("${6:-16}"); _G_TIMEOUT+=("${7:-300}")
}
run_std_gates(){
  local n=${#_G_TEST[@]} i logf
  for ((i=0; i<n; i++)); do
    echo "-- ${_G_DESC[$i]} --"
    logf="$LOGD/std_$i.log"
    if python3 scripts/run_with_timeout.py "${_G_TIMEOUT[$i]}" -- python3 "${_G_TEST[$i]}" >"$logf" 2>&1 \
       && grep -q "${_G_MARK[$i]}" "$logf"; then
      ok "${_G_OK[$i]}"
    else
      no "${_G_NAME[$i]}"; tail -"${_G_TAIL[$i]}" "$logf"
    fi
  done
}

echo "-- bootstrap --"
if bash db/bootstrap_local.sh "$DB" >$LOGD/boot.log 2>&1; then
  ok "bootstrap (roles + schema.sql + agent seats)"
else
  no "bootstrap"; tail -10 $LOGD/boot.log; echo "RELEASE GATES: FAIL (bootstrap)"; exit 2
fi

echo "-- foundation smoke (lock · queue · moat · main-impact · clusters · schema-graph · table-budget) --"
if bash db/smoke.sh >$LOGD/smoke.log 2>&1; then
  ok "foundation smoke (db/smoke.sh)"
else
  no "foundation smoke"; tail -24 $LOGD/smoke.log
fi

echo "-- migration-interruption (db/schema.sql re-runnable FROM ANY PARTIAL POINT: an interrupted/killed migration's truncated prefix, then a full re-apply, must converge — no statement half-applies and wedges the re-run) --"
if bash db/migration_interruption.sh >$LOGD/migint.log 2>&1 && grep -q "MIGRATION-INTERRUPTION GATE: PASS" $LOGD/migint.log; then
  ok "migration-interruption (partial-apply convergence + apply-twice idempotence)"
else
  no "migration-interruption"; tail -16 $LOGD/migint.log
fi

echo "-- extractor (import→file resolution + multi-language + schema graph) --"
if python3 tests/test_extractor.py >$LOGD/extract.log 2>&1; then
  ok "extractor (imports + go/java/ruby + schema DDL/DML)"
else
  no "extractor"; tail -12 $LOGD/extract.log
fi

echo "-- github-app brain (PR lifecycle: predict → serialize/warn → land → promote, on the real gate) --"
if python3 github-app/trial_offline.py >$LOGD/app.log 2>&1 \
   && grep -q "MERGED" $LOGD/app.log \
   && grep -q "Heads up" $LOGD/app.log && grep -q "Wait in line" $LOGD/app.log; then
  ok "github-app brain (offline lifecycle trial)"
else
  no "github-app brain"; tail -14 $LOGD/app.log
fi

# --- standard gates: discover gates.d/*.gate, then run them all -------------------------------------------
# Sorted glob = stable, reviewable run order (NN- prefix). A missing gates.d/ would silently run ZERO standard
# gates and still print PASS, so a guard fails LOUD if the directory or its files vanished (recall-safety: the
# suite must never go green by forgetting its own gates).
shopt -s nullglob
_gate_files=(gates.d/*.gate)
shopt -u nullglob
if [ "${#_gate_files[@]}" -eq 0 ]; then
  no "gate auto-discovery (gates.d/*.gate found NONE — the standard gates would silently NOT run)"
  echo "RELEASE GATES: FAIL (no gates discovered)"; exit 2
fi
for gf in "${_gate_files[@]}"; do
  # shellcheck source=/dev/null
  source "$gf"
done
run_std_gates

echo "-- whole-graph N-scale test (10 in-flight PRs on a real polyglot app: calls·imports·schema·config·direct·unknown) --"
if python3 tests/dogfood_nscale.py --repo tests/fixtures/sample_app --spec tests/fixtures/sample_app_prs.json --assert-whole-graph >$LOGD/nscale.log 2>&1 \
   && grep -q "WHOLE-GRAPH TEST: PASS" $LOGD/nscale.log; then
  ok "whole-graph N-scale (every graph dimension fires end-to-end)"
else
  no "whole-graph N-scale"; tail -16 $LOGD/nscale.log
fi

echo "------------------------------------------------------------"
if [ "$FAIL" -eq 0 ]; then echo "RELEASE GATES: PASS"; else echo "RELEASE GATES: FAIL"; exit 1; fi
