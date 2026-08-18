#!/usr/bin/env bash
# smoke.sh — prove the Veripsa foundation (db/schema.sql) end-to-end on a throwaway DB:
# the lock + FIFO queue + collision-as-event + reroute, and the moat (forgery block, append-only,
# no direct table access for seats).
#   bash db/smoke.sh
set -uo pipefail
cd "$(dirname "$0")/.."
TDB="veripsa_smoke_$$"
dropdb "$TDB" 2>/dev/null
createdb "$TDB" -O veripsa_migrator 2>/dev/null || createdb "$TDB"
trap 'dropdb "$TDB" 2>/dev/null' EXIT
M="postgresql://veripsa_migrator@localhost/${TDB}"
A="postgresql://veripsa_demo_agent@localhost/${TDB}"
B="postgresql://veripsa_demo_agent2@localhost/${TDB}"
S="postgresql://veripsa_demo_steward@localhost/${TDB}"
P="postgresql://veripsa_app@localhost/${TDB}"
FAIL=0
chk(){ if echo "$2" | grep -qE "$3"; then echo "  [PASS] $1"; else echo "  [FAIL] $1 — got: $2"; FAIL=1; fi; }

psql "$M" -v ON_ERROR_STOP=1 -f db/schema.sql >/tmp/smoke_apply.log 2>&1 || { echo "[FAIL] schema.sql apply"; tail -15 /tmp/smoke_apply.log; exit 1; }
echo "VERIPSA FOUNDATION SMOKE (db/schema.sql)"
psql "$M" -tAc "SET search_path=core; SELECT core.provision_seat('ACCT-DEMO','Demo Co','AG-A','frontend','veripsa_demo_agent'); SELECT core.provision_seat('ACCT-DEMO','Demo Co','AG-B','api','veripsa_demo_agent2'); SELECT core.provision_seat('ACCT-DEMO','Demo Co','AG-S','owner','veripsa_demo_steward'); SELECT core.provision_seat('ACCT-DEMO','Demo Co','AG-APP','Veripsa App','veripsa_app')" >/dev/null

chk "lock: A is GRANTED src/pay.py@main (free lane)" \
  "$(psql "$A" -tAc "SET search_path=core; SELECT core.declare_claim_with_authority('C-A1','src/pay.py','acme/app','main')" 2>&1)" '"granted" *: *true.*C-A1|C-A1.*"granted" *: *true'
chk "queue: B WAITS IN LINE for the same lane (position 1, holder frontend)" \
  "$(psql "$B" -tAc "SET search_path=core; SELECT core.declare_claim_with_authority('C-B1','src/pay.py','acme/app','main')" 2>&1)" '"queued" *: *true'
chk "queue: B's wait names position 1 + the holder" \
  "$(psql "$B" -tAc "SET search_path=core; SELECT (core.declare_claim_with_authority('C-B1','src/pay.py','acme/app','main')->>'position')||' '||(core.declare_claim_with_authority('C-B1','src/pay.py','acme/app','main')->>'holder')" 2>&1)" "^1 frontend$"
# NO-DOUBLE-GRANT INVARIANT (#6 — the core lane safety under concurrent webhooks): after two agents BOTH declare
# on the SAME lane (A above + B's repeated declares), the gate must guarantee AT MOST ONE active claim on that
# path — NEVER two. The FIFO tests above prove B is *queued*; this is the crisp count-based proof of the
# underlying invariant (claim_one_active is the partial-unique backstop, but assert it at the gate level here).
chk "no-double-grant: exactly ONE active claim on src/pay.py@acme/app@main (concurrent declares can never both win)" \
  "$(psql "$M" -tAc "SET search_path=core; SELECT set_config('core.current_account','ACCT-DEMO',true); SELECT count(*) FROM core.claim WHERE repo='acme/app' AND branch='main' AND target_path='src/pay.py' AND claim_state='active'" 2>&1)" "^1$"
chk "no-double-grant: the single active holder is A (the first declarer); B is NOT active (it waits, never grants)" \
  "$(psql "$M" -tAc "SET search_path=core; SELECT set_config('core.current_account','ACCT-DEMO',true); SELECT agent_id FROM core.claim WHERE repo='acme/app' AND branch='main' AND target_path='src/pay.py' AND claim_state='active'" 2>&1)" "^AG-A$"
chk "collision: B's enqueue auto-recorded a held collision (a prevented clobber)" \
  "$(psql "$M" -tAc "SET search_path=core; SELECT set_config('core.current_account','ACCT-DEMO',true); SELECT count(*) FROM core.event WHERE kind='collision_held'" 2>&1)" "^1$"
chk "steer: B can still take a DIFFERENT free lane while queued (granted)" \
  "$(psql "$B" -tAc "SET search_path=core; SELECT (core.declare_claim_with_authority('C-B2','src/other.py','acme/app','main')->>'granted')" 2>&1)" "^true$"
chk "event ledger: the held collision names blocked + holder" \
  "$(psql "$M" -tAc "SET search_path=core; SELECT set_config('core.current_account','ACCT-DEMO',true); SELECT kind||' '||agent_id||' '||counterparty_agent FROM core.event WHERE kind='collision_held'" 2>&1)" "collision_held AG-B AG-A"
chk "queue: board shows the LINE forming on pay.py (depth 1, waiting api)" \
  "$(psql "$S" -tAc "SET search_path=core; SELECT 'depth='||(b->'queues'->0->>'depth')||' w='||(b->'queues'->0->'waiting'->>0) FROM (SELECT core.board_surface() b) x" 2>&1 | tail -1)" "depth=1 w=api"
chk "moat: a seat's DIRECT insert into claim is refused" \
  "$(psql "$A" -tAc "SET search_path=core; INSERT INTO core.claim(claim_id,account_id,agent_id,target_path) VALUES('X','ACCT-DEMO','AG-A','y')" 2>&1)" "permission denied for table claim|forgery block"
chk "moat: a recorded event cannot be deleted (append-only)" \
  "$(psql "$M" -tAc "SET search_path=core; SELECT set_config('core.current_account','ACCT-DEMO',true); SELECT set_config('core.governed_write_token','event',true); DELETE FROM core.event WHERE account_id='ACCT-DEMO'" 2>&1)" "append-only.*permanent"
chk "moat: a seat cannot read tables directly (only via the gate/surfaces)" \
  "$(psql "$A" -tAc "SET search_path=core; SELECT count(*) FROM core.claim" 2>&1)" "permission denied for table claim"

# read side — the steward reads the board + the hero metric through the surfaces.
psql "$M" -tAc "SET search_path=core; SELECT core.provision_seat('ACCT-DEMO','Demo Co','AG-S','owner','veripsa_demo_steward')" >/dev/null
chk "surface: collision_surface shows held=1 · steered=1" \
  "$(psql "$S" -tAc "SET search_path=core; SELECT 'held='||(s->>'held')||' steered='||(s->>'steered') FROM (SELECT core.collision_surface() s) x" 2>&1)" "held=1 steered=1"
chk "surface: board_surface shows the 2-agent live fleet" \
  "$(psql "$S" -tAc "SET search_path=core; SELECT 'agents='||(core.board_surface()->'summary'->>'agents') FROM (SELECT 1) z" 2>&1)" "agents=2"

# ── ON-RAMP QUEUE: release promotes the next car in line; the owner can break a stalled lane (recovery crew).
chk "queue: A releases pay.py -> the next in line (api) is PROMOTED (FIFO)" \
  "$(psql "$A" -tAc "SET search_path=core; SELECT (core.release_claim_with_authority('C-A1')->>'promoted')" 2>&1)" "^api$"
chk "queue: after promotion the line is empty (api now holds pay.py)" \
  "$(psql "$S" -tAc "SET search_path=core; SELECT 'waiting='||(core.board_surface()->'summary'->>'waiting') FROM (SELECT 1) z" 2>&1 | tail -1)" "waiting=0"
chk "override: frontend queues behind api, the OWNER breaks the lane -> frontend promoted (recovery crew)" \
  "$(psql "$A" -tAc "SET search_path=core; SELECT core.declare_claim_with_authority('C-A2','src/pay.py','acme/app','main')" >/dev/null 2>&1; psql "$S" -tAc "SET search_path=core; SELECT (core.break_lane_with_authority('acme/app','main','src/pay.py')->>'promoted')" 2>&1)" "^frontend$"

chk "records: a statement is recorded + readable" "$(psql "$A" -tAc "SET search_path=core; SELECT core.record_statement_with_authority('Hardening the lock: claims carry a branch coordinate.','src/pay.py','acme/app','main')" 2>&1)" "^ST-"
chk "records: re-statement on the same anchor supersedes (maintained-current)" "$(psql "$A" -tAc "SET search_path=core; SELECT core.record_statement_with_authority('Lock now branch-scoped end to end.','src/pay.py','acme/app','main'); SELECT (core.meaning_surface()->>'total')" 2>&1 | tail -1)" "^1$"
chk "records: a statement cannot be deleted (append-only)" "$(psql "$M" -tAc "SET search_path=core; SELECT set_config('core.current_account','ACCT-DEMO',true); SELECT set_config('core.governed_write_token','statement',true); DELETE FROM core.statement" 2>&1)" "append-only.*permanent"

# ── PHASE 5 WEB SURFACES: the console's read/owner-write path (home tree + Main + account +
#    connections + notifications), all derived FROM existing tables (no new tables — the manifest is fixed).
psql "$A" -tAc "SET search_path=core; SELECT core.ingest_graph_with_authority('{\"nodes\":[{\"id\":\"f1\",\"kind\":\"file\",\"path\":\"src/pay.py\",\"language\":\"python\"},{\"id\":\"d1\",\"kind\":\"def\",\"path\":\"src/pay.py\",\"name\":\"charge\"},{\"id\":\"f2\",\"kind\":\"file\",\"path\":\"src/util.py\",\"language\":\"python\"}],\"edges\":[]}'::jsonb,'acme/app','main','abc123')" >/dev/null
psql "$P" -tAc "SET search_path=core; SELECT core.record_push_with_authority('acme/app','main','deadbeef','claude-opus-4-8')" >/dev/null
psql "$A" -tAc "SET search_path=core; SELECT core.connect_store_with_authority('CN-1','github','acme/app','src/')" >/dev/null
chk "web: directory_surface lists files (auto-coordinate) + marks the live edit" \
  "$(psql "$S" -tAc "SET search_path=core; SELECT 'files='||(core.directory_surface()->>'total_files')||' edited='||((core.directory_surface()->'files'->0->>'claimed')) FROM (SELECT 1) z" 2>&1)" "files=2 edited=true"
chk "web: branch_surface carries the recorded coordinate + push" \
  "$(psql "$S" -tAc "SET search_path=core; SELECT 'coords='||jsonb_array_length(core.branch_surface()->'coordinates')||' pushes='||jsonb_array_length(core.branch_surface()->'pushes')" 2>&1)" "coords=1 pushes=1"
chk "web: list_store_connections shows the attached edge" \
  "$(psql "$S" -tAc "SET search_path=core; SELECT (core.list_store_connections()->0->>'provider')" 2>&1)" "^github$"
chk "web: account_surface carries meta + the 4 seats (frontend·api·owner·App)" \
  "$(psql "$S" -tAc "SET search_path=core; SELECT 'name='||(core.account_surface()->>'display_name')||' seats='||jsonb_array_length(core.account_surface()->'seats')" 2>&1)" "name=Demo Co seats=4"
chk "web: notifications_surface shows the recorded facts (honest, content-free)" \
  "$(psql "$S" -tAc "SET search_path=core; SELECT (core.notifications_surface()->'items'->0->>'kind')" 2>&1)" "collision_held|push"

# ── MAIN-PROTECTION (the GitHub-App brain): main_impact_surface + land_on_main + clusters, on a graph with a
#    real A→B edge (api.handle CALLS pay.charge). Proves serialize (direct), warn (semantic A→B + the impact
#    range), the N-scale contention cluster, the 着地 release+promote, AND that contention_surface still flags
#    contested post-refactor (the shared _claim_adjacency engine — contention had no gate before this).
psql "$A" -tAc "SET search_path=core; SELECT core.ingest_graph_with_authority('{\"nodes\":[{\"id\":\"src/pay.py\",\"kind\":\"file\",\"path\":\"src/pay.py\",\"language\":\"python\"},{\"id\":\"pc\",\"kind\":\"def\",\"path\":\"src/pay.py\",\"name\":\"charge\"},{\"id\":\"src/api.py\",\"kind\":\"file\",\"path\":\"src/api.py\",\"language\":\"python\"},{\"id\":\"ah\",\"kind\":\"def\",\"path\":\"src/api.py\",\"name\":\"handle\"}],\"edges\":[{\"src\":\"src/api.py\",\"dst\":\"charge\",\"kind\":\"calls\"}]}'::jsonb,'acme/impact','main','cab1')" >/dev/null
psql "$A" -tAc "SET search_path=core; SELECT core.declare_claim_with_authority('MI-A','src/pay.py','acme/impact','main')" >/dev/null
psql "$B" -tAc "SET search_path=core; SELECT core.declare_claim_with_authority('MI-B','src/api.py','acme/impact','main')" >/dev/null
chk "main-impact: frontend(pay.py) verdict=warn + impact shows the downstream api.py (A→B blast radius)" \
  "$(psql "$S" -tAc "SET search_path=core; SELECT (c->>'verdict')||' '||(c->'impact'->>0) FROM (SELECT jsonb_array_elements(core.main_impact_surface('acme/impact')->'changes') c) x WHERE c->>'agent'='frontend'" 2>&1)" "^warn src/api.py$"
chk "main-impact: contention_surface still flags a contested file (refactor safety net for _claim_adjacency)" \
  "$(psql "$S" -tAc "SET search_path=core; SELECT (core.contention_surface('acme/impact','main')->>'contested_count')" 2>&1)" "^[1-9]"
psql "$A" -tAc "SET search_path=core; SELECT core.declare_claim_with_authority('MI-A2','src/cfg.py','acme/impact','main')" >/dev/null
psql "$B" -tAc "SET search_path=core; SELECT core.declare_claim_with_authority('MI-B2','src/cfg.py','acme/impact','main')" >/dev/null
chk "main-impact: api waits on src/cfg.py → verdict=serialize, serialize_behind=frontend (待ちを作る)" \
  "$(psql "$S" -tAc "SET search_path=core; SELECT (c->>'verdict')||' '||(c->'serialize_behind'->>0) FROM (SELECT jsonb_array_elements(core.main_impact_surface('acme/impact')->'changes') c) x WHERE c->>'agent'='api'" 2>&1)" "^serialize frontend$"
chk "main-impact: the entangled in-flight PRs form a contention CLUSTER (N-scale neighborhood, not N² pairs)" \
  "$(psql "$S" -tAc "SET search_path=core; SELECT (core.main_impact_surface('acme/impact')->>'cluster_count')" 2>&1)" "^[1-9]"
psql "$A" -tAc "SET search_path=core; SELECT core.ingest_graph_with_authority('{\"nodes\":[{\"id\":\"x\",\"kind\":\"file\",\"path\":\"src/x.py\",\"language\":\"python\"},{\"id\":\"y\",\"kind\":\"file\",\"path\":\"src/y.py\",\"language\":\"python\"}],\"edges\":[{\"src\":\"src/x.py\",\"dst\":\"src/y.py\",\"kind\":\"imports\"}]}'::jsonb,'acme/imp2','main','dd01')" >/dev/null
psql "$A" -tAc "SET search_path=core; SELECT core.declare_claim_with_authority('IMP-A','src/x.py','acme/imp2','main')" >/dev/null
psql "$B" -tAc "SET search_path=core; SELECT core.declare_claim_with_authority('IMP-B','src/y.py','acme/imp2','main')" >/dev/null
chk "graph quality: an IMPORT edge (x imports y, NO shared symbol) makes x↔y contested (file→file dep)" \
  "$(psql "$S" -tAc "SET search_path=core; SELECT (core.contention_surface('acme/imp2','main')->>'contested_count')" 2>&1)" "^[1-9]"
psql "$A" -tAc "SET search_path=core; SELECT core.ingest_graph_with_authority('{\"nodes\":[{\"id\":\"rep\",\"kind\":\"file\",\"path\":\"reports.py\",\"language\":\"python\"},{\"id\":\"mig\",\"kind\":\"file\",\"path\":\"db/0007.sql\",\"language\":\"sql\"},{\"id\":\"t_orders\",\"kind\":\"table\",\"path\":\"db/0007.sql\",\"name\":\"orders\"}],\"edges\":[{\"src\":\"reports.py\",\"dst\":\"orders\",\"kind\":\"queries\"},{\"src\":\"db/0007.sql\",\"dst\":\"orders\",\"kind\":\"alters\"}]}'::jsonb,'acme/schema','main','ee01')" >/dev/null
psql "$A" -tAc "SET search_path=core; SELECT core.declare_claim_with_authority('SC-A','reports.py','acme/schema','main')" >/dev/null
psql "$B" -tAc "SET search_path=core; SELECT core.declare_claim_with_authority('SC-B','db/0007.sql','acme/schema','main')" >/dev/null
chk "schema graph: a migration that ALTERS table orders contends with code that QUERIES orders (no code edge — code-graph blind spot)" \
  "$(psql "$S" -tAc "SET search_path=core; SELECT (core.contention_surface('acme/schema','main')->>'contested_count')" 2>&1)" "^[1-9]"
psql "$A" -tAc "SET search_path=core; SELECT core.ingest_graph_with_authority('{\"nodes\":[{\"id\":\"svc\",\"kind\":\"file\",\"path\":\"svc.py\",\"language\":\"python\"},{\"id\":\"job\",\"kind\":\"file\",\"path\":\"job.py\",\"language\":\"python\"},{\"id\":\"k\",\"kind\":\"config_key\",\"path\":\"prod.yaml\",\"name\":\"db.pool_size\"}],\"edges\":[{\"src\":\"svc.py\",\"dst\":\"db.pool_size\",\"kind\":\"reads_config\"},{\"src\":\"job.py\",\"dst\":\"db.pool_size\",\"kind\":\"reads_config\"}]}'::jsonb,'acme/config','main','ff01')" >/dev/null
psql "$A" -tAc "SET search_path=core; SELECT core.declare_claim_with_authority('CF-A','svc.py','acme/config','main')" >/dev/null
psql "$B" -tAc "SET search_path=core; SELECT core.declare_claim_with_authority('CF-B','job.py','acme/config','main')" >/dev/null
chk "config graph: two files that read the SAME config key (db.pool_size) contend (no code edge — the config blind spot)" \
  "$(psql "$S" -tAc "SET search_path=core; SELECT (core.contention_surface('acme/config','main')->>'contested_count')" 2>&1)" "^[1-9]"
psql "$A" -tAc "SET search_path=core; SELECT core.ingest_graph_with_authority('{\"nodes\":[{\"id\":\"k\",\"kind\":\"file\",\"path\":\"known.py\",\"language\":\"python\"}],\"edges\":[]}'::jsonb,'acme/unknown','main','aa01')" >/dev/null
psql "$A" -tAc "SET search_path=core; SELECT core.declare_claim_with_authority('UK-A','ghost.py','acme/unknown','main')" >/dev/null
chk "unknown-first: a PR touching a file NOT in main's graph → verdict=unknown (honest; never a false 'clear')" \
  "$(psql "$S" -tAc "SET search_path=core; SELECT (core.main_impact_surface('acme/unknown','main')->>'unknown_count')" 2>&1)" "^[1-9]"
chk "main-impact: 着地 — GitHub App lands PR/change MI-A2 → releases that lane + PROMOTES the waiter (api on cfg.py)" \
  "$(psql "$P" -tAc "SET search_path=core; SELECT (core.land_change_on_main_with_authority('MI-A2','acme/impact','beef01')->>'promoted')" 2>&1)" "api"

# ── EFFECT MEASUREMENT (PO: 動くのは分かった、効果は?): the product's own effect_surface — both halves of the
#    value, tallied from FACTS it recorded. serialize half = held clobbers; warn half = predictions on record.
chk "effect: the lock HELD a clobber → effect_surface.prevented_clobbers ≥ 1 (measurable serialize half)" \
  "$(psql "$S" -tAc "SET search_path=core; SELECT (core.effect_surface()->>'prevented_clobbers')" 2>&1)" "^[1-9]"
chk "effect: a recorded warn (prediction) is tallied → warns_issued ≥ 1 (measurable warn half)" \
  "$(psql "$A" -tAc "SET search_path=core; SELECT core.record_warn_with_authority('src/api.py','acme/impact','main','PR-x'); SELECT (core.effect_surface()->>'warns_issued')" 2>&1 | tail -1)" "^[1-9]"

# ── STALLED / NEGLECTED WORK (the handoff signal made ACTIVE): stalled_work_surface lists in-flight work that
#    has stopped moving — 'abandoned' (lease lapsed, never landed = the doer vanished) + 'starved-waiting' (in
#    line past the bounded threshold). Derived PURELY from the existing claim lifecycle (no new table). We set
#    up a fresh active claim, an abandoned one (force its lease into the past WITHOUT a landing, then sweep),
#    a landed one (state→released), and a long-waiting one — only the abandoned + starved must surface.
psql "$A" -tAc "SET search_path=core; SELECT core.declare_claim_with_authority('ST-FRESH','active/now.py','acme/stall','main')" >/dev/null   # fresh active → must NOT appear
psql "$A" -tAc "SET search_path=core; SELECT core.declare_claim_with_authority('ST-ABAND','gone/lost.py','acme/stall','main')" >/dev/null   # will be abandoned
psql "$A" -tAc "SET search_path=core; SELECT core.declare_claim_with_authority('ST-LAND','done/ok.py','acme/stall','main')" >/dev/null      # will land (released)
psql "$P" -tAc "SET search_path=core; SELECT core.land_change_on_main_with_authority('ST-LAND','acme/stall','feed0001')" >/dev/null          # → state='released' (landed)
# abandon: force the lease into the past WITHOUT a landing, then run the same sweep production runs (→ 'expired').
psql "$M" -tAc "SET search_path=core; SELECT set_config('core.current_account','ACCT-DEMO',true); SELECT core.mark_governed_write('claim'); UPDATE core.claim SET lease_expires_at=now()-interval '1 hour', heartbeat_at=now()-interval '1 hour' WHERE account_id='ACCT-DEMO' AND change_id='ST-ABAND'; SELECT core.expire_stale_claims()" >/dev/null
# starve: a queued waiter that has been in line well past the default 60-min threshold (force claimed_at back).
psql "$B" -tAc "SET search_path=core; SELECT core.declare_claim_with_authority('ST-WAIT','gone/lost.py','acme/stall','main')" >/dev/null     # queues behind nobody-live; force it old + waiting
psql "$M" -tAc "SET search_path=core; SELECT set_config('core.current_account','ACCT-DEMO',true); SELECT core.mark_governed_write('claim'); UPDATE core.claim SET claim_state='waiting', claimed_at=now()-interval '2 hours' WHERE account_id='ACCT-DEMO' AND change_id='ST-WAIT'" >/dev/null
chk "stalled: an ABANDONED claim (lease lapsed, never landed) surfaces with reason='abandoned' on the right path" \
  "$(psql "$S" -tAc "SET search_path=core; SELECT (i->>'reason')||' '||(i->>'path') FROM (SELECT jsonb_array_elements(core.stalled_work_surface()->'items') i) x WHERE i->>'change_id'='ST-ABAND'" 2>&1)" "^abandoned gone/lost.py$"
chk "stalled: the abandoned item names the holder + a positive stalled_seconds (how long it's been ignored)" \
  "$(psql "$S" -tAc "SET search_path=core; SELECT (i->>'held_by')||' '||((i->>'stalled_seconds')::int > 0) FROM (SELECT jsonb_array_elements(core.stalled_work_surface()->'items') i) x WHERE i->>'change_id'='ST-ABAND'" 2>&1 | tail -1)" "^frontend true$"
chk "stalled: a fresh ACTIVE claim does NOT appear (still being worked = not neglected)" \
  "$(psql "$S" -tAc "SET search_path=core; SELECT count(*) FROM (SELECT jsonb_array_elements(core.stalled_work_surface()->'items') i) x WHERE i->>'change_id'='ST-FRESH'" 2>&1)" "^0$"
chk "stalled: a LANDED change does NOT appear (it reached main = not abandoned)" \
  "$(psql "$S" -tAc "SET search_path=core; SELECT count(*) FROM (SELECT jsonb_array_elements(core.stalled_work_surface()->'items') i) x WHERE i->>'change_id'='ST-LAND'" 2>&1)" "^0$"
chk "stalled: a long-WAITING claim surfaces with reason='starved-waiting' (the waiter is starving)" \
  "$(psql "$S" -tAc "SET search_path=core; SELECT (i->>'reason') FROM (SELECT jsonb_array_elements(core.stalled_work_surface()->'items') i) x WHERE i->>'change_id'='ST-WAIT'" 2>&1)" "^starved-waiting$"
chk "stalled: counts tally (abandoned_count ≥ 1, starved_count ≥ 1) + thresholds echo the bounded knobs" \
  "$(psql "$S" -tAc "SET search_path=core; SELECT 'a='||((core.stalled_work_surface()->>'abandoned_count')::int >= 1)||' s='||((core.stalled_work_surface()->>'starved_count')::int >= 1)||' wknob='||(core.stalled_work_surface()->'thresholds'->>'waiting_minutes')" 2>&1 | tail -1)" "^a=true s=true wknob=60$"
chk "stalled: the bounded knob is OWNER-TUNABLE but CLAMPED (set waiting_minutes=99999 → clamps to 10080)" \
  "$(psql "$S" -tAc "SET search_path=core; SELECT core.set_policy_with_authority('stalled_waiting_minutes','99999'); SELECT (core.stalled_work_surface()->'thresholds'->>'waiting_minutes')" 2>&1 | tail -1)" "^10080$"
# raising the waiting threshold above the waiter's 2h age must DROP it from starved (proves the knob is live).
chk "stalled: raising waiting_minutes past the waiter's age DROPS it from starved (knob actually gates)" \
  "$(psql "$S" -tAc "SET search_path=core; SELECT count(*) FROM (SELECT jsonb_array_elements(core.stalled_work_surface()->'items') i) x WHERE i->>'change_id'='ST-WAIT'" 2>&1)" "^0$"
psql "$S" -tAc "SET search_path=core; SELECT core.set_policy_with_authority('stalled_waiting_minutes','60')" >/dev/null   # restore default for any later read
# clean up the stall fixtures so they don't skew later global assertions (release the still-active fresh claim).
psql "$A" -tAc "SET search_path=core; SELECT core.release_claim_with_authority('ST-FRESH')" >/dev/null 2>&1

# ── STUCK / RED PRs (the GitHub-inbox signal made ACTIVE): record_pr_failing_with_authority records that a PR is
#    RED — CI failing or merge-conflicting — and stuck_prs_surface LISTS the recent ones so a human/AI is told
#    (nobody watches the inbox). Complement to stalled_work_surface (abandoned/starved lanes). Content-free: the
#    PR change ref (PR-<n>) + repo/branch + head sha + a bounded reason token. NOTIFY-ONLY (never blocks). On a
#    fresh repo it must be HONEST-EMPTY; recording must surface the PR with its reason; a re-record (rerun) must
#    DEDUP to the LATEST reason; a PR that LANDED must drop out; and the bounded window knob must clamp.
chk "stuck-pr: a fresh repo is HONEST-EMPTY (no fabricated stuck PRs before anything is recorded)" \
  "$(psql "$S" -tAc "SET search_path=core; SELECT (core.stuck_prs_surface()->>'stuck_count') FROM (SELECT 1) z" 2>&1 | tail -1)" "^0$"
psql "$P" -tAc "SET search_path=core; SELECT core.record_pr_failing_with_authority('PR-42','acme/stuck','main','abc123','ci_failed')" >/dev/null
psql "$P" -tAc "SET search_path=core; SELECT core.record_pr_failing_with_authority('PR-43','acme/stuck','main','def456','conflict')" >/dev/null
chk "stuck-pr: a recorded failing PR (PR-42, ci_failed) surfaces with its PR ref + reason (content-free)" \
  "$(psql "$S" -tAc "SET search_path=core; SELECT (i->>'pr')||' '||(i->>'reason') FROM (SELECT jsonb_array_elements(core.stuck_prs_surface()->'items') i) x WHERE i->>'pr'='PR-42'" 2>&1)" "^PR-42 ci_failed$"
chk "stuck-pr: a conflicting PR (PR-43) surfaces with reason='conflict' + a positive stuck_seconds" \
  "$(psql "$S" -tAc "SET search_path=core; SELECT (i->>'reason')||' '||((i->>'stuck_seconds')::int >= 0) FROM (SELECT jsonb_array_elements(core.stuck_prs_surface()->'items') i) x WHERE i->>'pr'='PR-43'" 2>&1 | tail -1)" "^conflict true$"
chk "stuck-pr: stuck_count tallies the distinct stuck PRs (2) + echoes the bounded window knob (7 days)" \
  "$(psql "$S" -tAc "SET search_path=core; SELECT 'n='||(core.stuck_prs_surface()->>'stuck_count')||' w='||(core.stuck_prs_surface()->>'window_minutes')" 2>&1)" "^n=2 w=10080$"
# DEDUP: re-record PR-42 with a DIFFERENT reason (a rerun / new push) → still ONE row, now the LATEST reason.
psql "$P" -tAc "SET search_path=core; SELECT core.record_pr_failing_with_authority('PR-42','acme/stuck','main','abc999','conflict')" >/dev/null
chk "stuck-pr: a PR re-recorded (rerun) stays ONE row showing the LATEST reason (dedup per repo/branch/PR)" \
  "$(psql "$S" -tAc "SET search_path=core; SELECT count(*)||' '||max(i->>'reason') FROM (SELECT jsonb_array_elements(core.stuck_prs_surface()->'items') i) x WHERE i->>'pr'='PR-42'" 2>&1)" "^1 conflict$"
# RESOLUTION via landing: PR-43's change ref LANDS on main → it is no longer in-flight → drops out of stuck.
psql "$P" -tAc "SET search_path=core; SELECT core.record_landing_with_authority('acme/stuck','main','1abcd000',ARRAY['PR-43'],'bob')" >/dev/null
chk "stuck-pr: a PR whose change ref LANDED on main drops out (reached main = no longer stuck)" \
  "$(psql "$S" -tAc "SET search_path=core; SELECT count(*) FROM (SELECT jsonb_array_elements(core.stuck_prs_surface()->'items') i) x WHERE i->>'pr'='PR-43'" 2>&1)" "^0$"
chk "stuck-pr: the window knob is OWNER-TUNABLE but CLAMPED (set stuck_pr_window_minutes=999999 → clamps to 43200)" \
  "$(psql "$S" -tAc "SET search_path=core; SELECT core.set_policy_with_authority('stuck_pr_window_minutes','999999'); SELECT (core.stuck_prs_surface()->>'window_minutes')" 2>&1 | tail -1)" "^43200$"
psql "$S" -tAc "SET search_path=core; SELECT core.set_policy_with_authority('stuck_pr_window_minutes','10080')" >/dev/null   # restore default
chk "stuck-pr moat: a buyer writer (NOT the App service identity) canNOT record a pr_failing fact (delegation-only)" \
  "$(psql "$A" -tAc "SET search_path=core; SELECT core.record_pr_failing_with_authority('PR-99','acme/stuck','main','aaaa1111','ci_failed')" 2>&1)" "permission denied"

# ── SPLIT CANDIDATES (PO: ファイル分割を促す): a file agents repeatedly SERIALIZE on (many collision_held events
#    across distinct agents, over a bounded window) is a STRUCTURAL bottleneck → advise splitting it. Derived
#    PURELY from the existing event ledger (no new table; a signal is a read over the KIND). Seed, on a
#    dedicated repo so prior collisions don't leak in: HOT (src/god.py, 4 held collisions by 2 distinct agents
#    — past the default min of 3) and COLD (src/calm.py, 1 held collision — below threshold). HOT must surface
#    as a split candidate; COLD must NOT; and the bounded knob must clamp AND actually gate membership.
psql "$M" -tAc "SET search_path=core; SELECT set_config('core.current_account','ACCT-DEMO',true); SELECT core.mark_governed_write('event');
  INSERT INTO core.event(event_id,account_id,kind,agent_id,counterparty_agent,repo,branch,path) VALUES
   ('SC-H1','ACCT-DEMO','collision_held','AG-B','AG-A','acme/hot','main','src/god.py'),
   ('SC-H2','ACCT-DEMO','collision_held','AG-A','AG-B','acme/hot','main','src/god.py'),
   ('SC-H3','ACCT-DEMO','collision_held','AG-B','AG-A','acme/hot','main','src/god.py'),
   ('SC-H4','ACCT-DEMO','collision_held','AG-A','AG-B','acme/hot','main','src/god.py'),
   ('SC-C1','ACCT-DEMO','collision_held','AG-B','AG-A','acme/hot','main','src/calm.py')" >/dev/null
chk "split: a chronically contested file (4 held collisions ≥ min 3) surfaces as a split candidate" \
  "$(psql "$S" -tAc "SET search_path=core; SELECT (c->>'path')||' '||(c->>'collisions') FROM (SELECT jsonb_array_elements(core.split_candidates('acme/hot','main')->'candidates') c) x WHERE c->>'path'='src/god.py'" 2>&1)" "^src/god.py 4$"
chk "split: the candidate carries distinct_blocked (2 different agents got held) + a content-free split suggestion" \
  "$(psql "$S" -tAc "SET search_path=core; SELECT (c->>'distinct_blocked')||' '||((c->>'suggestion') ~ 'split') FROM (SELECT jsonb_array_elements(core.split_candidates('acme/hot','main')->'candidates') c) x WHERE c->>'path'='src/god.py'" 2>&1 | tail -1)" "^2 true$"
chk "split: a file with few collisions (1 < min 3) does NOT surface (chronic, not one-off)" \
  "$(psql "$S" -tAc "SET search_path=core; SELECT count(*) FROM (SELECT jsonb_array_elements(core.split_candidates('acme/hot','main')->'candidates') c) x WHERE c->>'path'='src/calm.py'" 2>&1)" "^0$"
chk "split: the surface echoes the bounded knobs (window_days=30, min_collisions=3) + candidate_count=1" \
  "$(psql "$S" -tAc "SET search_path=core; SELECT 'w='||(core.split_candidates('acme/hot','main')->>'window_days')||' m='||(core.split_candidates('acme/hot','main')->>'min_collisions')||' n='||(core.split_candidates('acme/hot','main')->>'candidate_count')" 2>&1)" "^w=30 m=3 n=1$"
chk "split: the min_collisions knob is OWNER-TUNABLE but CLAMPED (set 999 → clamps to 100)" \
  "$(psql "$S" -tAc "SET search_path=core; SELECT core.set_policy_with_authority('split_min_collisions','999'); SELECT (core.split_candidates('acme/hot','main')->>'min_collisions')" 2>&1 | tail -1)" "^100$"
# raising the threshold above the hot file's 4 collisions must DROP it (proves the knob actually gates membership).
chk "split: raising min_collisions to 5 (> the file's 4) DROPS it from candidates (the knob gates membership)" \
  "$(psql "$S" -tAc "SET search_path=core; SELECT core.set_policy_with_authority('split_min_collisions','5'); SELECT (core.split_candidates('acme/hot','main')->>'candidate_count')" 2>&1 | tail -1)" "^0$"
psql "$S" -tAc "SET search_path=core; SELECT core.set_policy_with_authority('split_min_collisions','3')" >/dev/null   # restore default for any later read
# ── SPLIT CANDIDATES, STRUCTURAL/PREDICTIVE ARM (PO 2026-06-18: "1ファイルに2本以上依存は効率悪い…お知らせできると良い"):
#    a file need not have COLLIDED yet to be a hotspot — Veripsa's own code graph already knows the blast radius.
#    FAN-IN (distinct files importing it, resolved to internal FILE nodes — not stdlib symbols) × CHURN (lands in
#    the window) PREDICTS the magnet before the collisions pile up. But fan-in ALONE misleads, so the arm needs
#    BOTH. Seed a dedicated repo: HUB (src/hub.py imported by 5 distinct files AND landed 3× → fan_in 5 ≥ 5 AND
#    churn 3 ≥ 3 → MUST surface, basis='structural', zero collisions) and STABLE (src/stable.py imported by 6
#    files but NEVER lands → high fan-in + ~0 churn = good design, NOT a hotspot → MUST NOT surface: this is the
#    honesty the churn gate buys). code_node/code_edge are append-only (no PK) → plain INSERT on the fresh DB.
psql "$M" -tAc "SET search_path=core; SELECT set_config('core.current_account','ACCT-DEMO',true); SELECT core.mark_governed_write('code_node');
  INSERT INTO core.code_node(account_id,repo,branch,node_id,node_kind,path,name,language) VALUES
   ('ACCT-DEMO','acme/struct','main','N-HUB','file','src/hub.py','hub.py','python'),
   ('ACCT-DEMO','acme/struct','main','N-STABLE','file','src/stable.py','stable.py','python')" >/dev/null
psql "$M" -tAc "SET search_path=core; SELECT set_config('core.current_account','ACCT-DEMO',true); SELECT core.mark_governed_write('code_edge');
  INSERT INTO core.code_edge(account_id,repo,branch,src,dst,edge_kind) VALUES
   ('ACCT-DEMO','acme/struct','main','src/a.py','src/hub.py','imports'),('ACCT-DEMO','acme/struct','main','src/b.py','src/hub.py','imports'),
   ('ACCT-DEMO','acme/struct','main','src/c.py','src/hub.py','imports'),('ACCT-DEMO','acme/struct','main','src/d.py','src/hub.py','imports'),
   ('ACCT-DEMO','acme/struct','main','src/e.py','src/hub.py','imports'),('ACCT-DEMO','acme/struct','main','src/hub.py','os','imports'),
   ('ACCT-DEMO','acme/struct','main','src/a.py','src/stable.py','imports'),('ACCT-DEMO','acme/struct','main','src/b.py','src/stable.py','imports'),
   ('ACCT-DEMO','acme/struct','main','src/c.py','src/stable.py','imports'),('ACCT-DEMO','acme/struct','main','src/d.py','src/stable.py','imports'),
   ('ACCT-DEMO','acme/struct','main','src/e.py','src/stable.py','imports'),('ACCT-DEMO','acme/struct','main','src/f.py','src/stable.py','imports')" >/dev/null
psql "$M" -tAc "SET search_path=core; SELECT set_config('core.current_account','ACCT-DEMO',true); SELECT core.mark_governed_write('event');
  INSERT INTO core.event(event_id,account_id,kind,agent_id,repo,branch,path) VALUES
   ('SC-L1','ACCT-DEMO','landed','AG-LAND','acme/struct','main','src/hub.py'),
   ('SC-L2','ACCT-DEMO','landed','AG-LAND','acme/struct','main','src/hub.py'),
   ('SC-L3','ACCT-DEMO','landed','AG-LAND','acme/struct','main','src/hub.py')" >/dev/null
chk "split (structural): a depended-on (fan-in 5) AND frequently-landing (churn 3) file surfaces PREDICTIVELY — before any collision" \
  "$(psql "$S" -tAc "SET search_path=core; SELECT (c->>'basis')||' f='||(c->>'fan_in')||' ch='||(c->>'churn')||' col='||(c->>'collisions') FROM (SELECT jsonb_array_elements(core.split_candidates('acme/struct','main')->'candidates') c) x WHERE c->>'path'='src/hub.py'" 2>&1 | tail -1)" "^structural f=5 ch=3 col=0$"
chk "split (structural): the candidate's suggestion is the predictive (not the reactive) wording + content-free" \
  "$(psql "$S" -tAc "SET search_path=core; SELECT ((c->>'suggestion') ~ 'structural hotspot' AND (c->>'suggestion') ~ 'before collisions') FROM (SELECT jsonb_array_elements(core.split_candidates('acme/struct','main')->'candidates') c) x WHERE c->>'path'='src/hub.py'" 2>&1 | tail -1)" "^t$"
chk "split (structural): a widely-imported (fan-in 6) but NEVER-changing file does NOT surface (high fan-in + 0 churn = good design)" \
  "$(psql "$S" -tAc "SET search_path=core; SELECT count(*) FROM (SELECT jsonb_array_elements(core.split_candidates('acme/struct','main')->'candidates') c) x WHERE c->>'path'='src/stable.py'" 2>&1 | tail -1)" "^0$"
chk "split (structural): the surface echoes the new bounded knobs (min_fanin=5, min_churn=3)" \
  "$(psql "$S" -tAc "SET search_path=core; SELECT 'mf='||(core.split_candidates('acme/struct','main')->>'min_fanin')||' mc='||(core.split_candidates('acme/struct','main')->>'min_churn')" 2>&1 | tail -1)" "^mf=5 mc=3$"
chk "split (structural): the CHURN knob GATES — raise split_min_churn to 4 (> the file's 3 lands) → the hotspot DROPS" \
  "$(psql "$S" -tAc "SET search_path=core; SELECT core.set_policy_with_authority('split_min_churn','4'); SELECT count(*) FROM (SELECT jsonb_array_elements(core.split_candidates('acme/struct','main')->'candidates') c) x WHERE c->>'path'='src/hub.py'" 2>&1 | tail -1)" "^0$"
chk "split (structural): split_min_fanin is OWNER-TUNABLE but CLAMPED (set 9999 → 1000)" \
  "$(psql "$S" -tAc "SET search_path=core; SELECT core.set_policy_with_authority('split_min_fanin','9999'); SELECT (core.split_candidates('acme/struct','main')->>'min_fanin')" 2>&1 | tail -1)" "^1000$"
psql "$S" -tAc "SET search_path=core; SELECT core.set_policy_with_authority('split_min_churn','3'); SELECT core.set_policy_with_authority('split_min_fanin','5')" >/dev/null   # restore defaults
# ── SHARED FOUNDATION in main_impact_surface (PO 2026-06-18 "独り占め禁止" / 触る奴にだけ出す): a PR that RESERVES a
#    load-bearing file (the SAME fan-in×churn gate as split_candidates) is flagged to WHOEVER touches it — not only
#    on collision, and even on an otherwise-CLEAR PR. Reuse the acme/struct graph+churn seed; put ONE in-flight
#    claim on the hub and read the surface. (render then turns shared_foundation into one quiet, content-free line.)
psql "$A" -tAc "SET search_path=core; SELECT core.declare_claim_with_authority('SF-1','src/hub.py','acme/struct','main')" >/dev/null
chk "shared-foundation: an in-flight PR touching a load-bearing file (fan-in 5 × churn 3) carries shared_foundation on its change" \
  "$(psql "$S" -tAc "SET search_path=core; SELECT (sf->>'path')||' f='||(sf->>'fan_in')||' ch='||(sf->>'churn') FROM (SELECT jsonb_array_elements(core.main_impact_surface('acme/struct','main')->'changes') c) x, LATERAL jsonb_array_elements(x.c->'shared_foundation') sf WHERE x.c->>'change_id'='SF-1'" 2>&1 | tail -1)" "^src/hub.py f=5 ch=3$"
chk "shared-foundation: an otherwise-CLEAR PR STILL surfaces the foundation (reaches whoever touches it, not only on collision)" \
  "$(psql "$S" -tAc "SET search_path=core; SELECT (c->>'verdict')||' n='||jsonb_array_length(c->'shared_foundation') FROM (SELECT jsonb_array_elements(core.main_impact_surface('acme/struct','main')->'changes') c) x WHERE c->>'change_id'='SF-1'" 2>&1 | tail -1)" "^clear n=1$"
psql "$A" -tAc "SET search_path=core; SELECT core.release_claim_with_authority('SF-1')" >/dev/null   # release the fixture (don't skew later global assertions)
# ── LEASE TTL = BOUNDED TUNING KNOB (枠は決めて): the lane-reclaim window for a stalled holder is owner-tunable
#    via the 'lease_minutes' policy (set_policy_with_authority) but CLAMPED to a safe frame (5..1440 min, default
#    30) by core._claim_lease_at()→_policy_int. We grant a fresh claim and read its lease as minutes-from-now:
#    default ≈ 30; a small policy (5) is reflected on the NEXT claim; an out-of-range policy (99999) clamps to
#    1440 — never 0 (instant reclaim of a live holder) or years (a dead session squats the lane forever).
# helper: minutes-from-now of a given change's lease (migrator reads the table; account pinned for RLS).
lease_min(){ psql "$M" -tAc "SET search_path=core; SELECT set_config('core.current_account','ACCT-DEMO',true); SELECT round(EXTRACT(EPOCH FROM (lease_expires_at - now()))/60)::int FROM core.claim WHERE account_id='ACCT-DEMO' AND change_id='$1' AND claim_state='active'" 2>&1 | tail -1; }
psql "$A" -tAc "SET search_path=core; SELECT core.declare_claim_with_authority('LS-DEF','lease/def.py','acme/lease','main')" >/dev/null   # default policy
chk "lease knob: a fresh claim's lease is the DEFAULT ≈ now()+30min (no policy set)" "$(lease_min LS-DEF)" "^30$"
psql "$S" -tAc "SET search_path=core; SELECT core.set_policy_with_authority('lease_minutes','5')" >/dev/null   # owner tunes it SHORTER
psql "$A" -tAc "SET search_path=core; SELECT core.declare_claim_with_authority('LS-LOW','lease/low.py','acme/lease','main')" >/dev/null   # new claim reflects it
chk "lease knob: owner-tunable — set lease_minutes=5 → a NEW claim's lease ≈ now()+5min" "$(lease_min LS-LOW)" "^5$"
psql "$S" -tAc "SET search_path=core; SELECT core.set_policy_with_authority('lease_minutes','99999')" >/dev/null   # out of frame → must clamp
psql "$A" -tAc "SET search_path=core; SELECT core.declare_claim_with_authority('LS-HI','lease/hi.py','acme/lease','main')" >/dev/null
chk "lease knob: CLAMPED — set lease_minutes=99999 (out of frame) → a NEW claim's lease clamps to 1440min" "$(lease_min LS-HI)" "^1440$"
psql "$S" -tAc "SET search_path=core; SELECT core.set_policy_with_authority('lease_minutes','30')" >/dev/null   # restore default
# clean up the lease fixtures (release them so they don't skew later global assertions).
psql "$A" -tAc "SET search_path=core; SELECT core.release_claim_with_authority('LS-DEF'); SELECT core.release_claim_with_authority('LS-LOW'); SELECT core.release_claim_with_authority('LS-HI')" >/dev/null 2>&1

# ── UNIFIED LANDING MODEL: record_landing_with_authority (new 'landed' event KIND) + collisions_on_main.
#    Two landings on the SAME path by DIFFERENT authors → collisions_on_main detects it.
#    A single-author landing → zero collisions. Same file, same author twice → zero collisions (self).
psql "$P" -tAc "SET search_path=core; SELECT core.record_landing_with_authority('acme/landing','main','aaaa1111',ARRAY['src/shared.py','src/other.py'],'alice')" >/dev/null
psql "$P" -tAc "SET search_path=core; SELECT core.record_landing_with_authority('acme/landing','main','bbbb2222',ARRAY['src/shared.py'],'bob')" >/dev/null
chk "landing: record_landing_with_authority returns ≥ 1 (paths written as 'landed' events)" \
  "$(psql "$P" -tAc "SET search_path=core; SELECT core.record_landing_with_authority('acme/landing','main','cccc3333',ARRAY['x.py'],'eve')" 2>&1)" "^1$"
chk "landing: two different-author landings on src/shared.py → collisions_on_main.collisions_count ≥ 1" \
  "$(psql "$S" -tAc "SET search_path=core; SELECT (core.collisions_on_main('acme/landing','main','14 days'::interval)->>'collisions_count')" 2>&1)" "^[1-9]"
chk "landing: collisions_on_main.recent names src/shared.py (the colliding path)" \
  "$(psql "$S" -tAc "SET search_path=core; SELECT (core.collisions_on_main('acme/landing','main','14 days'::interval)->'recent'->0->>'path')" 2>&1)" "src/shared.py"
psql "$P" -tAc "SET search_path=core; SELECT core.record_landing_with_authority('acme/solo','main','dd110000',ARRAY['solo.py'],'alice')" >/dev/null
chk "landing: single-author landing → zero collisions (no false positive)" \
  "$(psql "$S" -tAc "SET search_path=core; SELECT (core.collisions_on_main('acme/solo','main','14 days'::interval)->>'collisions_count')" 2>&1)" "^0$"
chk "effect: effect_surface now carries collisions_occurred field (unified landing model)" \
  "$(psql "$S" -tAc "SET search_path=core; SELECT (core.effect_surface() ? 'collisions_occurred')" 2>&1)" "^t$"
chk "effect: effect_surface.collisions_occurred ≥ 1 after two-author landing on same path" \
  "$(psql "$S" -tAc "SET search_path=core; SELECT (core.effect_surface()->>'collisions_occurred')::int >= 1" 2>&1)" "^t$"

# ── EFFECT-METRIC ACCURACY (the product SELLS this number — a miscount is a false-effect / credibility failure).
#    The surfaces above prove the fields EXIST and are ≥ N; these prove they count the EXACT, INTENDED number:
#    not N±1, not doubled, not inflated by events that should not count. prevented_clobbers / collisions_occurred
#    are ACCOUNT-global aggregates (every prior test added to them), so we assert the DELTA across a KNOWN
#    operation on a DEDICATED repo (robust to accumulation) — the honest way to assert exactness on a running
#    aggregate. collisions_on_main takes a repo filter, so it gets ABSOLUTE asserts on dedicated repos.
# helper: the current account-global prevented_clobbers (steward reads the sold surface).
pc(){ psql "$S" -tAc "SET search_path=core; SELECT (core.effect_surface()->>'prevented_clobbers')::int" 2>&1 | tail -1; }
co(){ psql "$S" -tAc "SET search_path=core; SELECT (core.effect_surface()->>'collisions_occurred')::int" 2>&1 | tail -1; }

# (1) EXACT N: three CROSS-AGENT held collisions on three DISTINCT paths must raise prevented_clobbers by EXACTLY 3.
PC0=$(pc)
for p in m1 m2 m3; do
  psql "$A" -tAc "SET search_path=core; SELECT core.declare_claim_with_authority('MET-A-$p','metric/$p.py','acme/metric','main')" >/dev/null  # A holds the lane
  psql "$B" -tAc "SET search_path=core; SELECT core.declare_claim_with_authority('MET-B-$p','metric/$p.py','acme/metric','main')" >/dev/null  # B blocked → 1 cross-agent collision
done
chk "metric-accuracy: 3 cross-agent held collisions on 3 distinct paths raise prevented_clobbers by EXACTLY 3 (not N±1, not doubled)" \
  "$(echo $(( $(pc) - PC0 )))" "^3$"

# (2) SELF-COLLISION IS NOT A PREVENTED CLOBBER. The gate fires a 'collision_held' even when the SAME author opens a
#     SECOND change on a path they already hold (the holder check only excludes same-agent-AND-same-change). That is
#     the doer serializing THEIR OWN two PRs — not a prevented CROSS-agent clobber (the value Veripsa sells). It must
#     NOT inflate prevented_clobbers. Prove BOTH: the raw 'collision_held' row IS written (the gate behaves), yet the
#     SOLD metric does NOT move (the metric correctly excludes agent_id=counterparty_agent).
PC1=$(pc)
psql "$A" -tAc "SET search_path=core; SELECT core.declare_claim_with_authority('SELF-1','solo/self.py','acme/selfcoll','main')" >/dev/null   # A holds
psql "$A" -tAc "SET search_path=core; SELECT core.declare_claim_with_authority('SELF-2','solo/self.py','acme/selfcoll','main')" >/dev/null   # A (different change) blocked by A → SELF collision_held
chk "metric-accuracy: a same-author self-collision DID record a raw collision_held row (agent_id=counterparty_agent — the gate behaves)" \
  "$(psql "$M" -tAc "SET search_path=core; SELECT set_config('core.current_account','ACCT-DEMO',true); SELECT count(*) FROM core.event WHERE kind='collision_held' AND repo='acme/selfcoll' AND agent_id=counterparty_agent" 2>&1 | tail -1)" "^1$"
chk "metric-accuracy: that self-collision raised prevented_clobbers by EXACTLY 0 (a doer colliding with itself is NOT a prevented cross-agent clobber)" \
  "$(echo $(( $(pc) - PC1 )))" "^0$"
chk "metric-accuracy: collision_surface.held excludes the self-collision too (the hero metric agrees with prevented_clobbers, no asymmetry)" \
  "$(psql "$S" -tAc "SET search_path=core; SELECT count(*) FROM (SELECT jsonb_array_elements(core.collision_surface()->'recent') c) x WHERE (c->>'repo')='acme/selfcoll'" 2>&1 | tail -1)" "^0$"
# and a genuine CROSS-agent collision right after STILL counts (the self-exclusion didn't break the real signal).
PC2=$(pc)
psql "$A" -tAc "SET search_path=core; SELECT core.declare_claim_with_authority('XAGT-A','solo/shared.py','acme/selfcoll','main')" >/dev/null
psql "$B" -tAc "SET search_path=core; SELECT core.declare_claim_with_authority('XAGT-B','solo/shared.py','acme/selfcoll','main')" >/dev/null  # B blocked by A → real cross-agent
chk "metric-accuracy: a real cross-agent collision right after the self one STILL raises prevented_clobbers by EXACTLY 1 (the guard kept the real signal)" \
  "$(echo $(( $(pc) - PC2 )))" "^1$"

# (3) collisions_on_main — EXACT counts on dedicated repos: two DIFFERENT authors on the same path = EXACTLY 1
#     (a distinct-path collision, never 2 for the 2 sides); a single author / same-author-twice = EXACTLY 0.
psql "$P" -tAc "SET search_path=core; SELECT core.record_landing_with_authority('acme/cmexact','main','aa00aa00',ARRAY['p/shared.py'],'alice')" >/dev/null
psql "$P" -tAc "SET search_path=core; SELECT core.record_landing_with_authority('acme/cmexact','main','bb11bb11',ARRAY['p/shared.py'],'bob')" >/dev/null
chk "metric-accuracy: collisions_on_main — two DIFFERENT authors on one path is EXACTLY 1 collision (counts the path once, not once per side)" \
  "$(psql "$S" -tAc "SET search_path=core; SELECT (core.collisions_on_main('acme/cmexact','main','14 days'::interval)->>'collisions_count')" 2>&1 | tail -1)" "^1$"
# a THIRD different author on the SAME path is still EXACTLY 1 (it's distinct-PATHS, not pair-count — 3 authors ≠ 3).
psql "$P" -tAc "SET search_path=core; SELECT core.record_landing_with_authority('acme/cmexact','main','cc22cc22',ARRAY['p/shared.py'],'carol')" >/dev/null
chk "metric-accuracy: collisions_on_main — a 3rd author on the SAME path keeps it EXACTLY 1 (distinct contested PATHS, not author-pairs)" \
  "$(psql "$S" -tAc "SET search_path=core; SELECT (core.collisions_on_main('acme/cmexact','main','14 days'::interval)->>'collisions_count')" 2>&1 | tail -1)" "^1$"
psql "$P" -tAc "SET search_path=core; SELECT core.record_landing_with_authority('acme/cmself','main','dd33dd33',ARRAY['p/mine.py'],'alice')" >/dev/null
psql "$P" -tAc "SET search_path=core; SELECT core.record_landing_with_authority('acme/cmself','main','ee44ee44',ARRAY['p/mine.py'],'alice')" >/dev/null
chk "metric-accuracy: collisions_on_main — the SAME author landing twice on one path is EXACTLY 0 (a doer is not colliding with itself)" \
  "$(psql "$S" -tAc "SET search_path=core; SELECT (core.collisions_on_main('acme/cmself','main','14 days'::interval)->>'collisions_count')" 2>&1 | tail -1)" "^0$"

# (4) WEBHOOK REDELIVERY MUST NOT DOUBLE-COUNT. GitHub redelivers at-least-once; the 'landed' ledger is idempotent
#     (deterministic id + ON CONFLICT DO NOTHING). Confirm the METRIC inherits that: re-delivering BOTH landings on
#     acme/cmexact (identical repo/branch/sha/path/author) writes 0 new rows AND leaves collisions_on_main EXACTLY 1.
CO_BEFORE=$(co)
psql "$P" -tAc "SET search_path=core; SELECT core.record_landing_with_authority('acme/cmexact','main','aa00aa00',ARRAY['p/shared.py'],'alice')" >/dev/null  # redelivery of alice's landing
RD=$(psql "$P" -tAc "SET search_path=core; SELECT core.record_landing_with_authority('acme/cmexact','main','bb11bb11',ARRAY['p/shared.py'],'bob')" 2>&1 | tail -1)  # redelivery of bob's
chk "metric-accuracy: a webhook REDELIVERY of an already-recorded landing writes EXACTLY 0 new rows (idempotent ledger)" \
  "$(echo "$RD")" "^0$"
chk "metric-accuracy: after the redelivery collisions_on_main is STILL EXACTLY 1 (the metric inherits idempotency — no double-count)" \
  "$(psql "$S" -tAc "SET search_path=core; SELECT (core.collisions_on_main('acme/cmexact','main','14 days'::interval)->>'collisions_count')" 2>&1 | tail -1)" "^1$"
chk "metric-accuracy: the redelivery did not change the global collisions_occurred tally (delta EXACTLY 0)" \
  "$(echo $(( $(co) - CO_BEFORE )))" "^0$"

# (5) WINDOW / TIME-BOUNDS — events OUTSIDE the window are excluded; ON the boundary there is no off-by-one. The
#     event ledger is append-only (occurred_at can't be UPDATEd — that's the moat), so we SEED two back-dated
#     'landed' facts directly (the migrator's mark_governed_write('event') path, exactly as the split/purge fixtures
#     above seed events), with occurred_at 20 days ago: alice + bob collide on a path 20 days back → a 14-day read is
#     EXACTLY 0; a 30-day read that spans them is EXACTLY 1. Then a third author lands on the same path NOW → the
#     14-day window picks it back up (EXACTLY 1) — the bound actually gates, with no off-by-one at the edge.
psql "$M" -tAc "SET search_path=core; SELECT set_config('core.current_account','ACCT-DEMO',true); SELECT core.mark_governed_write('event');
  INSERT INTO core.event(event_id,account_id,kind,agent_id,repo,branch,path,commit_sha,occurred_at) VALUES
   ('CMW-OLD-A','ACCT-DEMO','landed','GH-alice','acme/cmwin','main','p/win.py','f000f000',now()-interval '20 days'),
   ('CMW-OLD-B','ACCT-DEMO','landed','GH-bob','acme/cmwin','main','p/win.py','f111f111',now()-interval '20 days')" >/dev/null
chk "metric-accuracy: collisions_on_main — a collision 20 days old is EXACTLY 0 inside a 14-day window (out-of-window excluded)" \
  "$(psql "$S" -tAc "SET search_path=core; SELECT (core.collisions_on_main('acme/cmwin','main','14 days'::interval)->>'collisions_count')" 2>&1 | tail -1)" "^0$"
chk "metric-accuracy: collisions_on_main — the SAME collision is EXACTLY 1 inside a 30-day window that spans it (the window is the only difference)" \
  "$(psql "$S" -tAc "SET search_path=core; SELECT (core.collisions_on_main('acme/cmwin','main','30 days'::interval)->>'collisions_count')" 2>&1 | tail -1)" "^1$"
# a collision needs TWO in-window landings by different authors: ONE fresh landing colliding only with an
# OUT-OF-WINDOW one is still EXACTLY 0 (both sides must be in-window — the bound applies to the counterparty too).
psql "$P" -tAc "SET search_path=core; SELECT core.record_landing_with_authority('acme/cmwin','main','f222f222',ARRAY['p/win.py'],'carol')" >/dev/null   # a fresh (in-window) landing by a 3rd author
chk "metric-accuracy: collisions_on_main — ONE fresh landing colliding only with an OUT-OF-window one is EXACTLY 0 (a collision needs TWO in-window sides)" \
  "$(psql "$S" -tAc "SET search_path=core; SELECT (core.collisions_on_main('acme/cmwin','main','14 days'::interval)->>'collisions_count')" 2>&1 | tail -1)" "^0$"
# a SECOND fresh different-author landing in-window completes an in-window pair → the 14-day read is EXACTLY 1 again.
psql "$P" -tAc "SET search_path=core; SELECT core.record_landing_with_authority('acme/cmwin','main','f333f333',ARRAY['p/win.py'],'dave')" >/dev/null    # a 2nd fresh in-window author
chk "metric-accuracy: collisions_on_main — a SECOND fresh in-window author completes an in-window pair → 14-day read is EXACTLY 1 (the bound gates, no off-by-one)" \
  "$(psql "$S" -tAc "SET search_path=core; SELECT (core.collisions_on_main('acme/cmwin','main','14 days'::interval)->>'collisions_count')" 2>&1 | tail -1)" "^1$"
# clean up the metric-accuracy claim fixtures (release the still-active holders so they don't skew later global asserts).
psql "$A" -tAc "SET search_path=core; SELECT core.release_claim_with_authority('MET-A-m1'); SELECT core.release_claim_with_authority('MET-A-m2'); SELECT core.release_claim_with_authority('MET-A-m3'); SELECT core.release_claim_with_authority('SELF-1'); SELECT core.release_claim_with_authority('XAGT-A')" >/dev/null 2>&1

# ── TABLE-BUDGET GUARDRAIL (事前に防げる, structural): the core table set must equal the registered
#    MANIFEST. A NEW table fails here until deliberately registered — an AI cannot quietly sprawl tables;
#    the default path for a new fact is an `event` KIND, and adding a NOUN is a conscious, reviewed act.
# installation_account: deliberately registered NOUN — the multi-tenant routing map (one GitHub installation
# → one isolated tenant account). It is the one table with NO per-account RLS (it routes BETWEEN accounts),
# reachable only through enter_installation_with_authority.
# co_change_seen_commit: deliberately registered NOUN — the content-free FOLDED-COMMIT ledger that makes the
# per-push co-change increment IDEMPOTENT across webhook redeliveries (a fold-once-ever record of commit shas;
# only hashes, no body/message/author). Same moat as co_change: FORCE RLS + the governed-write forgery gate.
# webhook_delivery: deliberately registered NOUN — the OPERATIONAL durability inbox (NOT a product fact ledger):
# a SANITIZED webhook persisted BEFORE the 202 ack so a crash/deploy after the ack can't lose it, then claimed/
# finished by the worker + recovered on boot (db/schema/25_webhook_queue.sql). It is CROSS-TENANT (no per-account
# RLS), reachable ONLY through the *_with_authority SECURITY DEFINER fns granted to the App; content-free.
# webhook_worker_instance: deliberately registered operational NOUN — the worker LIVENESS heartbeat that lets the
# dead-instance lease reaper reclaim an ungracefully-orphaned webhook_delivery 'processing' row in seconds instead
# of the full 1800s stale window (a SIGKILL/OOM/host-loss leaves the row frozen and freezes its whole causal lane).
# One row per live worker process (a per-boot uuid instance id + last_heartbeat); a slow-but-alive worker keeps
# beating so it is never false-reaped. Host-global operator state (no per-account RLS — it is the HOST's worker
# board, keyed by instance id), reached ONLY through the *_with_authority SECURITY DEFINER fns granted to the App;
# content-free (an opaque instance id + timestamps — no repo/account/PR/body).
# account_lifecycle_tombstone: deliberately registered NOUN (audit iter-4 P1) — the content-free RESURRECTION marker
# that an account was UNINSTALL-PURGED or GDPR-ERASED. It is the durable "do NOT silently re-provision this tenant"
# flag that must survive BOTH lifecycle paths, INCLUDING erase (which hard-deletes the account row itself — so a
# deleted_at column could not survive; a separate table can). Like installation_account / webhook_delivery it is
# CROSS-TENANT (no per-account RLS — the account_id match IS the scope), reachable ONLY through the *_with_authority
# SECURITY DEFINER fns (tombstone/reactivate/assert_account_live, REVOKE-from-PUBLIC); content-free (id + reason + ts).
# account_plan_event: deliberately registered NOUN (audit iter-5 P2) — the content-free per-account LAST-APPLIED
# marketplace plan-event timestamp (high-water mark) that makes set_account_plan_with_authority MONOTONIC against
# UNORDERED/re-delivered billing webhooks (a stale cancelled→free can not overwrite a later purchased→pro). A NEW
# FK-free standalone table so the delta applies contention-free on a live prod (no lock on the busy core.account).
# CROSS-TENANT (no per-account RLS — the account_id PK IS the scope), written ONLY through the SECURITY DEFINER plan
# setter (REVOKE-from-PUBLIC); content-free (account id + timestamps). Forgotten on uninstall purge + GDPR erase.
# active_alert: deliberately registered NOUN — the DURABLE, OWNER-READABLE record of which proactive alerts are
# CURRENTLY FIRING (graph_stale / account_over_line / db_usage_high / the DLQ depth alerts). The AlertSink was
# in-memory + stdout/optional-webhook only: with no VERIPSA_ALERT_WEBHOOK_URL configured (the App default), an
# ACTIVE alert lived ONLY in the per-process Render logs — "an alert logged but ignored is meaningless" (the PO's
# principle). The watchdog now persists fire/resolve here so the owner can SEE the live alert set via the gated
# core.active_alerts_with_authority() reader (a notification — email/dashboard — can poll the same surface). It is
# CROSS-TENANT operator state (no per-account RLS — it is the HOST's alert board, keyed by alert key), written ONLY
# through the *_with_authority SECURITY DEFINER fns granted to the App; content-free (key + level + a short label).
# boot_reconcile_state: deliberately registered NOUN (perf follow-up Round-2 — the Render free-tier cold-start cap).
# A single-row kv table that persists the last-run timestamp of boot_reconcile (a kind='boot_reconcile' PK row), so
# wakes that fire close together (a flapping deploy / a planned recycle right after a manual restart) can SKIP the
# re-reconcile when the previous sweep finished within VERIPSA_BOOT_RECONCILE_MIN_INTERVAL_MIN minutes — the live
# webhook path is the primary self-heal, boot_reconcile is the restart safety net. Like active_alert it is
# CROSS-TENANT operator state (no per-account RLS — it is the HOST's throttle, a global one-decision-per-boot),
# written ONLY through the *_with_authority SECURITY DEFINER fns granted to the App; content-free (the timestamp +
# the counts the reconcile returned — repos/installations seen — no repo names, no PR ids).
# github_delivery_recovery_scan / github_delivery_recovery: deliberately registered operational NOUNS. GitHub does
# not automatically retry failed App webhooks; the first single-row table persists the scanner's epoch/high-water/
# opaque cursor/page-tail/global cooldown plus non-paging compacted counters, and the second persists only bounded
# delivery GUID/id/time/status/retry evidence (full unresolved rows expire 30 days after the replay window). Neither
# contains webhook bodies, event/action, repo, account, or installation metadata. Both are host-global and have no
# direct grants; the App reaches them only through SECURITY DEFINER authority functions.
# workspace / workspace_member: deliberately registered NOUNS (Phase 4c, SHADOW) — the CROSS-REPO CONSENT substrate.
# workspace = a named cross-repo collaboration space; workspace_member = an account opting a SPECIFIC repo of its own
# into a workspace with a consent_state. They carry NO graph (a workspace is a JOIN-at-query-time view over the two
# tenants' own graphs, so quota stays per-account); the bilateral-consent FACT in these rows is what gates the
# CONSENTED cross-tenant contract read (db/schema/75_workspace.sql). Each wears FORCE RLS keyed to its owner +
# the governed-write forgery gate, so an owner sees/writes ONLY its own rows (an owner can never read or forge the
# other side's consent). Content-free (workspace ids + repo names + a consent_state, never a path/node/edge/body).
MANIFEST="account account_lifecycle_tombstone account_plan_event active_alert agent boot_reconcile_state claim co_change co_change_seen_commit code_edge code_node credential event follow github_delivery_recovery github_delivery_recovery_scan grant graph_convergence_lease graph_version installation_account intent policy policy_refresh_outbox repository_lifecycle_activation repository_lifecycle_tombstone statement store_connection webhook_delivery webhook_worker_instance workspace workspace_member"
ACTUAL="$(psql "$M" -tAc "SELECT string_agg(tablename,' ' ORDER BY tablename) FROM pg_tables WHERE schemaname='core'")"
EXPECTED="$(echo $MANIFEST | tr ' ' '\n' | sort | tr '\n' ' ' | sed 's/ $//')"
GOT="$(echo $ACTUAL | tr ' ' '\n' | sort | tr '\n' ' ' | sed 's/ $//')"
if [ "$GOT" = "$EXPECTED" ]; then echo "  [PASS] table-budget: core has exactly the $(echo $MANIFEST | wc -w | tr -d ' ') registered tables (no 乱立)";
else echo "  [FAIL] table-budget: core table set drifted from the manifest"; echo "     manifest: $EXPECTED"; echo "     actual:   $GOT"; echo "     -> register a NEW NOUN in the manifest deliberately, or put a new FACT in the event ledger as a KIND."; FAIL=1; fi

# ── DELEGATION (the hosted App's identity model) — LAST, because it adds author agents/claims that would
#    skew the global counts the surface assertions above check. ONE service identity (veripsa_app) reserves on
#    behalf of each PR AUTHOR, so claims are attributed to the real author — not the App; the board/comment
#    name the author. _place_claim stays internal (ungranted) so identity can never be passed in / spoofed.
chk "delegation: App acts-for author 'alice' on a free lane → granted, provisioned as the author" \
  "$(psql "$P" -tAc "SET search_path=core; SELECT (core.act_for_claim_with_authority('AL-1','svc/deleg.py','acme/app','main','alice')->>'granted')" 2>&1)" "^true$"
chk "delegation: App acts-for 'bob' on the SAME lane → queued behind the real author (holder=alice, not the App)" \
  "$(psql "$P" -tAc "SET search_path=core; SELECT (core.act_for_claim_with_authority('BO-1','svc/deleg.py','acme/app','main','bob')->>'holder')" 2>&1)" "^alice$"
chk "delegation moat: the App canNOT call internal _place_claim directly (revoked from PUBLIC → no spoofing)" \
  "$(psql "$P" -tAc "SET search_path=core; SELECT core._place_claim('Z','p','acme/app','main','ACCT-DEMO','GH-evil')" 2>&1)" "permission denied"

# ── CLAIM LIFECYCLE — REOPEN / LEASE-SCOPE / REAL-AUTHOR (regression guards for three lifecycle bugs). All on
#    DEDICATED repos so they never skew the global counts the surface assertions above check.

# (A) REOPENED PR must not crash the worker. A change's claims persist as 'released' after it lands; a `reopened`
#     PR re-declares the SAME claim_id. The free-lane INSERT would collide on claim_pkey and the unique_violation
#     handler's own INSERT would collide AGAIN (uncaught → the worker counts 'failed', posts no check/comment).
#     The fix RECYCLES the released row (UPDATE → active) keyed by claim_pkey, so reopen is idempotent + crash-free.
psql "$P" -tAc "SET search_path=core; SELECT core.act_for_claim_with_authority('RO-1:a.py','a.py','acme/reopen','main','alice')" >/dev/null   # opened → active
psql "$P" -tAc "SET search_path=core; SELECT core.land_change_on_main_with_authority('RO-1','acme/reopen','feed0001')" >/dev/null            # merged → released
chk "reopen: re-declaring a previously-RELEASED claim_id does NOT crash and returns active (no claim_pkey violation)" \
  "$(psql "$P" -tAc "SET search_path=core; SELECT (core.act_for_claim_with_authority('RO-1:a.py','a.py','acme/reopen','main','alice')->>'state')" 2>&1 | tail -1)" "^active$"
chk "reopen: re-declaring AGAIN stays idempotent (still active, no error)" \
  "$(psql "$P" -tAc "SET search_path=core; SELECT (core.act_for_claim_with_authority('RO-1:a.py','a.py','acme/reopen','main','alice')->>'state')" 2>&1 | tail -1)" "^active$"
chk "reopen: the row was RECYCLED, not duplicated (exactly ONE row for the reused claim_id)" \
  "$(psql "$M" -tAc "SET search_path=core; SELECT set_config('core.current_account','ACCT-DEMO',true); SELECT count(*) FROM core.claim WHERE claim_id='RO-1:a.py'" 2>&1 | tail -1)" "^1$"

# (B) LEASE RENEWAL must be SCOPED TO THE DECLARED CHANGE. _place_claim renews the lease as a heartbeat; renewing
#     EVERY active+waiting claim of the author would perpetually refresh a DIFFERENT abandoned PR's lease on any
#     activity → that PR could never expire and never surface 'abandoned'. We give alice TWO changes, force PR-B2's
#     lease into the PAST, then re-declare PR-B1; PR-B2's lease must STAY past (not renewed) → it expires + surfaces.
psql "$P" -tAc "SET search_path=core; SELECT core.act_for_claim_with_authority('LSC-B1:s1.py','s1.py','acme/leasescope','main','alice')" >/dev/null
psql "$P" -tAc "SET search_path=core; SELECT core.act_for_claim_with_authority('LSC-B2:s2.py','s2.py','acme/leasescope','main','alice')" >/dev/null
psql "$M" -tAc "SET search_path=core; SELECT set_config('core.current_account','ACCT-DEMO',true); SELECT core.mark_governed_write('claim'); UPDATE core.claim SET lease_expires_at=now()-interval '1 hour', heartbeat_at=now()-interval '1 hour' WHERE account_id='ACCT-DEMO' AND change_id='LSC-B2'" >/dev/null
psql "$P" -tAc "SET search_path=core; SELECT core.act_for_claim_with_authority('LSC-B1:s1.py','s1.py','acme/leasescope','main','alice')" >/dev/null   # sync PR-B1 (activity on a DIFFERENT PR)
chk "lease-scope: syncing PR-B1 does NOT renew PR-B2's lease (still in the past — the per-change scope holds)" \
  "$(psql "$M" -tAc "SET search_path=core; SELECT set_config('core.current_account','ACCT-DEMO',true); SELECT (round(EXTRACT(EPOCH FROM (lease_expires_at-now()))/60)::int < 0) FROM core.claim WHERE account_id='ACCT-DEMO' AND change_id='LSC-B2'" 2>&1 | tail -1)" "^t$"
psql "$M" -tAc "SET search_path=core; SELECT set_config('core.current_account','ACCT-DEMO',true); SELECT core.expire_stale_claims()" >/dev/null
chk "lease-scope: the genuinely-abandoned PR-B2 now surfaces as 'abandoned' (abandonment detection is not defeated)" \
  "$(psql "$S" -tAc "SET search_path=core; SELECT (i->>'reason') FROM (SELECT jsonb_array_elements(core.stalled_work_surface()->'items') i) x WHERE i->>'change_id'='LSC-B2'" 2>&1 | tail -1)" "^abandoned$"
chk "lease-scope: the freshly-synced PR-B1 does NOT surface as abandoned (its own lease was kept alive)" \
  "$(psql "$S" -tAc "SET search_path=core; SELECT count(*) FROM (SELECT jsonb_array_elements(core.stalled_work_surface()->'items') i) x WHERE i->>'change_id'='LSC-B1'" 2>&1 | tail -1)" "^0$"

# (B2) SELF-EVICTION ON RESYNC — the lease-vs-heartbeat ORDER bug. A holder whose lease JUST LAPSED (it went quiet
#     past the lease) and then PUSHES AGAIN (the synchronize that PROVES it is alive) must KEEP its lane. _place_claim
#     renews THIS change's lease as a heartbeat, but it must renew BEFORE expire_stale_claims() sweeps — else the sweep
#     flips the holder's own lapsed claim to 'expired' and auto-promotes a WAITER onto the lane, and the (now-too-late)
#     renewal scoped to claim_state IN ('active','waiting') no longer matches → the live, actively-pushing holder is
#     demoted to wait behind the very PR it was ahead of. Setup: SELF-A grants a lane, SELF-B queues behind it; force
#     SELF-A's lease into the PAST; SELF-A re-declares (its resync). SELF-A MUST stay 'active'; SELF-B MUST stay 'waiting'.
psql "$P" -tAc "SET search_path=core; SELECT core.act_for_claim_with_authority('SELFA:se.py','se.py','acme/self-evict','main','alice')" >/dev/null   # alice (SELF-A) holds the lane
psql "$P" -tAc "SET search_path=core; SELECT core.act_for_claim_with_authority('SELFB:se.py','se.py','acme/self-evict','main','bob')"   >/dev/null   # bob (SELF-B) queues behind
psql "$M" -tAc "SET search_path=core; SELECT set_config('core.current_account','ACCT-DEMO',true); SELECT core.mark_governed_write('claim'); UPDATE core.claim SET lease_expires_at=now()-interval '5 min', heartbeat_at=now()-interval '40 min' WHERE account_id='ACCT-DEMO' AND change_id='SELFA'" >/dev/null
chk "self-evict: setup — SELF-A's lease is genuinely in the PAST before its resync (the trigger condition)" \
  "$(psql "$M" -tAc "SET search_path=core; SELECT set_config('core.current_account','ACCT-DEMO',true); SELECT (lease_expires_at < now()) FROM core.claim WHERE account_id='ACCT-DEMO' AND change_id='SELFA'" 2>&1 | tail -1)" "^t$"
chk "self-evict: a live holder that resyncs AFTER its lease lapsed KEEPS its lane (renew-before-sweep — no self-eviction)" \
  "$(psql "$P" -tAc "SET search_path=core; SELECT (core.act_for_claim_with_authority('SELFA:se.py','se.py','acme/self-evict','main','alice')->>'state')" 2>&1 | tail -1)" "^active$"
chk "self-evict: the waiter behind it was NOT wrongly auto-promoted (SELF-B stays in line)" \
  "$(psql "$M" -tAc "SET search_path=core; SELECT set_config('core.current_account','ACCT-DEMO',true); SELECT claim_state FROM core.claim WHERE account_id='ACCT-DEMO' AND change_id='SELFB'" 2>&1 | tail -1)" "^waiting$"
chk "self-evict: exactly ONE active holder remains on the lane (no double-grant / split lane)" \
  "$(psql "$M" -tAc "SET search_path=core; SELECT set_config('core.current_account','ACCT-DEMO',true); SELECT count(*) FROM core.claim WHERE account_id='ACCT-DEMO' AND repo='acme/self-evict' AND target_path='se.py' AND claim_state='active'" 2>&1 | tail -1)" "^1$"
# AND the abandonment path is NOT broken by renew-before-sweep: if the holder NEVER resyncs, a competitor's push must
# still YIELD ITS LANE + promote the waiter. SELF-A lapses again; this time BOB (a DIFFERENT change) pushes.
# LEASE×PR-LIFETIME (the lost-serialize fix): the lapsed holder loses its LANE (the waiter is promoted — anti-squat
# intact) but, because a real serialize was in play (SELF-B was queued behind it), SELF-A is RE-QUEUED as a WAITER
# rather than terminated — a lease lapse is NOT proof the PR closed, and a still-open PR's collision must not silently
# vanish. Its lease is left LAPSED (not refreshed) so the row stays self-healing: if SELF-B later lands, SELF-A is
# promoted back still-lapsed and the next sweep re-evaluates it (re-queue or expire) — a dead holder converges to gone.
psql "$M" -tAc "SET search_path=core; SELECT set_config('core.current_account','ACCT-DEMO',true); SELECT core.mark_governed_write('claim'); UPDATE core.claim SET lease_expires_at=now()-interval '5 min', heartbeat_at=now()-interval '40 min' WHERE account_id='ACCT-DEMO' AND change_id='SELFA'" >/dev/null
psql "$P" -tAc "SET search_path=core; SELECT core.act_for_claim_with_authority('SELFB:se.py','se.py','acme/self-evict','main','bob')" >/dev/null   # bob syncs HIS PR (no resync from alice)
chk "self-evict: a lapsed holder with a waiter behind it YIELDS its lane but is RE-QUEUED (collision not silently dropped = lost-serialize fix), not terminated" \
  "$(psql "$M" -tAc "SET search_path=core; SELECT set_config('core.current_account','ACCT-DEMO',true); SELECT claim_state FROM core.claim WHERE account_id='ACCT-DEMO' AND change_id='SELFA'" 2>&1 | tail -1)" "^waiting$"
chk "self-evict: the waiter is correctly promoted onto the freed lane (auto-promote-on-expiry intact)" \
  "$(psql "$M" -tAc "SET search_path=core; SELECT set_config('core.current_account','ACCT-DEMO',true); SELECT claim_state FROM core.claim WHERE account_id='ACCT-DEMO' AND change_id='SELFB'" 2>&1 | tail -1)" "^active$"
chk "self-evict: exactly ONE active holder on the lane after the swap (no double-grant; SELF-B holds, SELF-A waits)" \
  "$(psql "$M" -tAc "SET search_path=core; SELECT set_config('core.current_account','ACCT-DEMO',true); SELECT count(*) FROM core.claim WHERE account_id='ACCT-DEMO' AND repo='acme/self-evict' AND target_path='se.py' AND claim_state='active'" 2>&1 | tail -1)" "^1$"
# NOTE on operator visibility: the 'abandoned' surface is LANE-centric (it excludes a lapsed row when ANOTHER
# live holder is actively working that same lane). After the swap SELF-B IS that live holder, so SELF-A is NOT
# flagged abandoned — and this is UNCHANGED from the old 'expired' behavior (which was equally excluded once
# SELF-B took the lane). So the re-queue introduces NO operator-visibility regression; it only stops the
# still-open collision from being silently CLEARED, which the per-change verdict (above) proves.

# (C) A DELEGATED collision must record the REAL blocked author, not the connecting App identity. Under the hosted
#     path the connection is the App seat (AG-APP / 'Veripsa App') but the agent turned away is the PR's real author.
#     The collision-event recorder must attribute the act_for author as 'blocked' so the buyer's proof-of-value
#     surfaces (collision/effect/notifications) say "carol was held by dave", never "Veripsa App was held by dave".
psql "$P" -tAc "SET search_path=core; SELECT core.act_for_claim_with_authority('DC-1:hot.py','hot.py','acme/deleg-coll','main','dave')" >/dev/null    # dave holds the lane
psql "$P" -tAc "SET search_path=core; SELECT core.act_for_claim_with_authority('DC-2:hot.py','hot.py','acme/deleg-coll','main','carol')" >/dev/null   # carol blocked → collision
chk "deleg-collision: the held collision names the REAL blocked author + holder (GH-carol/GH-dave, not AG-APP)" \
  "$(psql "$M" -tAc "SET search_path=core; SELECT set_config('core.current_account','ACCT-DEMO',true); SELECT agent_id||' '||counterparty_agent FROM core.event WHERE kind='collision_held' AND repo='acme/deleg-coll' AND path='hot.py'" 2>&1 | tail -1)" "^GH-carol GH-dave$"
chk "deleg-collision: collision_surface shows the real author as 'blocked' (proof-of-value is honest, not 'Veripsa App')" \
  "$(psql "$S" -tAc "SET search_path=core; SELECT (c->>'blocked')||' / '||(c->>'holder') FROM (SELECT jsonb_array_elements(core.collision_surface()->'recent') c) x WHERE c->>'repo'='acme/deleg-coll' AND c->>'path'='hot.py'" 2>&1 | tail -1)" "^carol / dave$"

# ── OFFBOARDING / PURGE (#5 — privacy table-stakes): when the App is uninstalled or a repo is removed,
#    purge_repo_with_authority FORGETS the content-free WORKING SET for that repo across all coordinates
#    (code graph nodes/edges/version + live claim/lane state) but RETAINS the append-only EVENT ledger (a
#    recorded fact is permanent — forget-the-working-set vs immutable-audit, by design; see RUNBOOK). On a
#    DEDICATED repo (acme/gone) so it can't skew any global assertion. Seed every working-set table + ONE
#    'landed' audit event, prove they exist, purge, then prove the working set is 0 AND the event survives.
psql "$M" -tAc "SET search_path=core; SELECT set_config('core.current_account','ACCT-DEMO',true); SELECT core.mark_governed_write('code_node');
  INSERT INTO core.code_node(account_id,repo,branch,node_id,node_kind,path,name,language) VALUES
   ('ACCT-DEMO','acme/gone','main','GN-1','file','src/gone.py','gone.py','python')" >/dev/null
psql "$M" -tAc "SET search_path=core; SELECT set_config('core.current_account','ACCT-DEMO',true); SELECT core.mark_governed_write('code_edge');
  INSERT INTO core.code_edge(account_id,repo,branch,src,dst,edge_kind) VALUES
   ('ACCT-DEMO','acme/gone','main','src/gone.py','src/other.py','imports')" >/dev/null
psql "$M" -tAc "SET search_path=core; SELECT set_config('core.current_account','ACCT-DEMO',true); SELECT core.mark_governed_write('graph_version');
  INSERT INTO core.graph_version(account_id,repo,branch,commit_sha,node_count,edge_count,repo_id) VALUES
   ('ACCT-DEMO','acme/gone','main','abc1',1,1,'29002')" >/dev/null
psql "$A" -tAc "SET search_path=core; SELECT core.declare_claim_with_authority('GN-CLAIM','src/gone.py','acme/gone','main')" >/dev/null   # an ACTIVE claim/lane on the repo
psql "$M" -tAc "SET search_path=core; SELECT set_config('core.current_account','ACCT-DEMO',true); SELECT core.mark_governed_write('event');
  INSERT INTO core.event(event_id,account_id,kind,agent_id,repo,branch,path) VALUES
   ('GN-EV','ACCT-DEMO','landed','AG-A','acme/gone','main','src/gone.py')" >/dev/null   # an append-only AUDIT fact
# precondition: the working set + the audit fact are present (so the post-purge zeros are meaningful, not vacuous).
chk "purge precondition: acme/gone has its working set seeded (node+edge+version+active claim all present)" \
  "$(psql "$M" -tAc "SET search_path=core; SELECT set_config('core.current_account','ACCT-DEMO',true); SELECT (SELECT count(*) FROM core.code_node WHERE repo='acme/gone')||' '||(SELECT count(*) FROM core.code_edge WHERE repo='acme/gone')||' '||(SELECT count(*) FROM core.graph_version WHERE repo='acme/gone')||' '||(SELECT count(*) FROM core.claim WHERE repo='acme/gone' AND claim_state='active')" 2>&1 | tail -1)" "^1 1 1 1$"
# Live repository removal requires durable stable-id authority.
psql "$M" -tAc "SET search_path=core; INSERT INTO core.webhook_delivery(
  delivery_key,event_type,account_key,repo,payload,status,attempts,received_at,locked_at)
  VALUES ('D-SMOKE-REPO-REMOVED','installation_repositories','ACCT-DEMO',NULL,
    jsonb_build_object('action','removed','repositories_removed',
      jsonb_build_array(jsonb_build_object('id','29002','full_name','acme/gone'))),
    'processing',1,clock_timestamp(),clock_timestamp())" >/dev/null
chk "purge: durable offboard_repository_with_authority reports it forgot the working set rows" \
  "$(psql "$P" -tAc "SET search_path=core; SELECT (r->'purged'->>'nodes')||' '||(r->'purged'->>'edges')||' '||(r->'purged'->>'versions')||' '||(r->'purged'->>'claims') FROM jsonb_array_elements((SELECT core.offboard_repository_with_authority('acme/gone','29002','installation_removed','D-SMOKE-REPO-REMOVED')->'results')) x(r)" 2>&1 | tail -1)" "^1 1 1 1$"
chk "purge: the content-free WORKING SET is FORGOTTEN — code_node/code_edge/graph_version/claim for acme/gone are all 0" \
  "$(psql "$M" -tAc "SET search_path=core; SELECT set_config('core.current_account','ACCT-DEMO',true); SELECT (SELECT count(*) FROM core.code_node WHERE repo='acme/gone')||' '||(SELECT count(*) FROM core.code_edge WHERE repo='acme/gone')||' '||(SELECT count(*) FROM core.graph_version WHERE repo='acme/gone')||' '||(SELECT count(*) FROM core.claim WHERE repo='acme/gone')" 2>&1 | tail -1)" "^0 0 0 0$"
chk "purge: the append-only AUDIT LEDGER SURVIVES — the 'landed' event for acme/gone is RETAINED (by design)" \
  "$(psql "$M" -tAc "SET search_path=core; SELECT set_config('core.current_account','ACCT-DEMO',true); SELECT count(*) FROM core.event WHERE repo='acme/gone' AND kind='landed'" 2>&1 | tail -1)" "^1$"
chk "purge moat: the App canNOT bypass stable-id/durable offboarding with raw repo purge" \
  "$(psql "$P" -tAc "SET search_path=core; SELECT core.purge_repo_with_authority('acme/app')" 2>&1)" "durable deletion authority"
chk "purge moat: a buyer writer canNOT purge a repo" \
  "$(psql "$A" -tAc "SET search_path=core; SELECT core.purge_repo_with_authority('acme/app')" 2>&1)" "permission denied"

# ── ADVERSARIAL HARDENING (the gate + engine are the product's CORE correctness — a crash, a non-terminating
#    recursive CTE, or a corrupted lane = a core failure). These do not happy-path; they FUZZ the lock invariant
#    + the contention engine with degenerate inputs (re-declares, two-changes-one-path, lease/release races,
#    no-op breaks, empty/oversized/control-char paths) and PATHOLOGICAL graphs (a dependency CYCLE + self-loop,
#    an EMPTY graph, a 40-change fully-entangled ring). PROBED on a scratch DB: every case below is ALREADY
#    handled — so these are REGRESSION assertions that DOCUMENT the robustness, not fabricated fixes. The
#    recursive-CTE tests carry an inline statement_timeout so a future infinite-loop regression FAILS LOUD
#    (a deterministic 'statement timeout' error → the chk regex misses → [FAIL]) instead of hanging the suite.
#    Dedicated adv/* coordinates so nothing here skews the global counts the surface assertions above check.

# GATE INVARIANT 1 — IDEMPOTENT RE-DECLARE: declaring a claim you ALREADY hold (same change, same path) must be
# a no-op that returns the SAME grant — NEVER a 2nd active row, never a crash. (The path the App hits on every
# webhook redelivery / PR synchronize that re-sends an unchanged file.)
psql "$A" -tAc "SET search_path=core; SELECT core.declare_claim_with_authority('AD-RD','adv/dup.py','adv/redecl','main')" >/dev/null
chk "adversarial: re-declaring the SAME change+path returns granted again (idempotent, no crash)" \
  "$(psql "$A" -tAc "SET search_path=core; SELECT (core.declare_claim_with_authority('AD-RD','adv/dup.py','adv/redecl','main')->>'granted')||' '||(core.declare_claim_with_authority('AD-RD','adv/dup.py','adv/redecl','main')->>'granted')" 2>&1 | tail -1)" "^true true$"
chk "adversarial: after 3 re-declares the lane still has EXACTLY ONE active claim (no double-grant)" \
  "$(psql "$M" -tAc "SET search_path=core; SELECT set_config('core.current_account','ACCT-DEMO',true); SELECT count(*) FROM core.claim WHERE repo='adv/redecl' AND target_path='adv/dup.py' AND claim_state='active'" 2>&1 | tail -1)" "^1$"

# GATE INVARIANT 2 — claim_one_active under two DIFFERENT changes on ONE path: exactly one active, the other queued.
psql "$A" -tAc "SET search_path=core; SELECT core.declare_claim_with_authority('AD-T1','adv/same.py','adv/two','main')" >/dev/null
psql "$B" -tAc "SET search_path=core; SELECT core.declare_claim_with_authority('AD-T2','adv/same.py','adv/two','main')" >/dev/null
chk "adversarial: two DIFFERENT changes on one path → exactly 1 active + 1 waiting (claim_one_active holds)" \
  "$(psql "$M" -tAc "SET search_path=core; SELECT set_config('core.current_account','ACCT-DEMO',true); SELECT 'a='||count(*) FILTER (WHERE claim_state='active')||' w='||count(*) FILTER (WHERE claim_state='waiting') FROM core.claim WHERE repo='adv/two' AND target_path='adv/same.py'" 2>&1 | tail -1)" "^a=1 w=1$"

# GATE INVARIANT 3 — LEASE EXPIRY COINCIDENT with release/promote: A holds, B+C wait; force A's lease into the
# past; then BOTH the expire-sweep (run at the top of any declare) AND A's release fire on the same lane. The
# guard in _promote_next_waiter (no promote while a lane has an active holder) must yield EXACTLY ONE active —
# never a double-promote (2 actives = a corrupted lane) and never a lost promote (0 actives with waiters stranded).
psql "$A" -tAc "SET search_path=core; SELECT core.declare_claim_with_authority('AD-LXA','adv/lane.py','adv/lx','main')" >/dev/null
psql "$B" -tAc "SET search_path=core; SELECT core.declare_claim_with_authority('AD-LXB','adv/lane.py','adv/lx','main')" >/dev/null
psql "$A" -tAc "SET search_path=core; SELECT core.declare_claim_with_authority('AD-LXC','adv/lane.py','adv/lx','main')" >/dev/null
psql "$M" -tAc "SET search_path=core; SELECT set_config('core.current_account','ACCT-DEMO',true); SELECT core.mark_governed_write('claim'); UPDATE core.claim SET lease_expires_at=now()-interval '1 hour', heartbeat_at=now()-interval '1 hour' WHERE account_id='ACCT-DEMO' AND change_id='AD-LXA'" >/dev/null
psql "$A" -tAc "SET search_path=core; SELECT core.declare_claim_with_authority('AD-LXD','adv/other.py','adv/lx','main')" >/dev/null   # runs expire_stale_claims → promotes the waiter on the freed lane
psql "$A" -tAc "SET search_path=core; SELECT core.release_claim_with_authority('AD-LXA')" >/dev/null 2>&1                              # the already-expired holder releases → must NOT double-promote
chk "adversarial: lease-expiry coincident with release → EXACTLY 1 active on the lane (no double/lost promote)" \
  "$(psql "$M" -tAc "SET search_path=core; SELECT set_config('core.current_account','ACCT-DEMO',true); SELECT 'a='||count(*) FILTER (WHERE claim_state='active') FROM core.claim WHERE repo='adv/lx' AND target_path='adv/lane.py'" 2>&1 | tail -1)" "^a=1$"

# GATE EDGE — break_lane on a NON-EXISTENT / already-empty lane: a clean no-op (broke:null, promoted:null), not a crash.
chk "adversarial: break_lane on a non-existent/empty lane is a clean no-op (ok:true, broke:null), never a crash" \
  "$(psql "$S" -tAc "SET search_path=core; SELECT (core.break_lane_with_authority('adv/ghost','main','adv/no-such.py')->>'ok')||' broke='||COALESCE(core.break_lane_with_authority('adv/ghost','main','adv/no-such.py')->>'broke','null')" 2>&1 | tail -1)" "^true broke=null$"
# break_lane on a free/empty lane records NO spurious audit event (only a real override on a real holder does).
chk "adversarial: breaking an empty lane records NO 'lane_broken' event (no spurious audit)" \
  "$(psql "$M" -tAc "SET search_path=core; SELECT set_config('core.current_account','ACCT-DEMO',true); SELECT count(*) FROM core.event WHERE kind='lane_broken' AND repo='adv/ghost'" 2>&1 | tail -1)" "^0$"

# GATE EDGE — promote with NO waiters: releasing a solo holder returns promoted:null cleanly.
psql "$A" -tAc "SET search_path=core; SELECT core.declare_claim_with_authority('AD-SOLO','adv/solo.py','adv/solo','main')" >/dev/null
chk "adversarial: releasing a solo holder (no waiters) → promoted is null, ok:true (no crash)" \
  "$(psql "$A" -tAc "SET search_path=core; SELECT (core.release_claim_with_authority('AD-SOLO')->>'ok')||' '||COALESCE(core.release_claim_with_authority('AD-SOLO')->>'promoted','null')" 2>&1 | tail -1)" "^true null$"

# GATE EDGE — reconcile when ALL of a change's files were dropped (empty p_paths): releases EVERYTHING cleanly.
psql "$A" -tAc "SET search_path=core; SELECT core.declare_claim_with_authority('AD-REC:f1.py','f1.py','adv/recon','main')" >/dev/null
psql "$A" -tAc "SET search_path=core; SELECT core.declare_claim_with_authority('AD-REC:f2.py','f2.py','adv/recon','main')" >/dev/null
chk "adversarial: reconcile with EMPTY paths releases all of the change's lanes (the PR dropped every file)" \
  "$(psql "$P" -tAc "SET search_path=core; SELECT jsonb_array_length(core.reconcile_change_claims_with_authority('AD-REC','adv/recon','main',ARRAY[]::text[])->'released')" 2>&1 | tail -1)" "^2$"
chk "adversarial: after the empty-paths reconcile the change has ZERO active/waiting claims (clean release)" \
  "$(psql "$M" -tAc "SET search_path=core; SELECT set_config('core.current_account','ACCT-DEMO',true); SELECT count(*) FROM core.claim WHERE repo='adv/recon' AND change_id='AD-REC' AND claim_state IN ('active','waiting')" 2>&1 | tail -1)" "^0$"

# GATE EDGE — degenerate PATHS: empty / whitespace / oversized are REJECTED (23514), a control-char path is HANDLED.
# NOTE: these three RAISE an exception — psql prints a MULTI-LINE error (ERROR: … then CONTEXT: …). The
# `chk` regex scans the WHOLE blob, so do NOT `tail -1` here (that would keep only the CONTEXT trailer and
# miss the ERROR line). The grant cases above DO `tail -1` because they return a single-line value + a SET echo.
chk "adversarial: an EMPTY target_path is rejected (a claim_id is not enough), never a crash" \
  "$(psql "$A" -tAc "SET search_path=core; SELECT core.declare_claim_with_authority('AD-EMP','','adv/x','main')" 2>&1)" "empty claim rejected|a target_path is required"
chk "adversarial: a whitespace-only target_path is rejected (btrim'd to empty)" \
  "$(psql "$A" -tAc "SET search_path=core; SELECT core.declare_claim_with_authority('AD-WS','   ','adv/x','main')" 2>&1)" "empty claim rejected|a target_path is required"
chk "adversarial: an OVERSIZED target_path (>1024) is rejected (bounded; never an unbounded write)" \
  "$(psql "$A" -tAc "SET search_path=core; SELECT core.declare_claim_with_authority('AD-LONG', repeat('z',2000),'adv/x','main')" 2>&1)" "too long"
chk "adversarial: a CONTROL-CHAR path (tab/newline) is HANDLED (granted), not a crash" \
  "$(psql "$A" -tAc "SET search_path=core; SELECT (core.declare_claim_with_authority('AD-CTRL', E'adv/a\tb\nc.py','adv/ctrl','main')->>'granted')" 2>&1 | tail -1)" "^true$"
chk "adversarial: a control-char-path claim flows through main_impact_surface without breaking the surface" \
  "$(psql "$S" -tAc "SET statement_timeout='15s'; SET search_path=core; SELECT (core.main_impact_surface('adv/ctrl','main')->>'inflight_count')" 2>&1 | tail -1)" "^1$"

# ENGINE PATHOLOGY 1 — DEPENDENCY CYCLE (a imports b, b imports a) + SELF-LOOP (a imports a), two in-flight
# changes across the cycle. The recursive change_reach / cc CTEs MUST terminate (they UNION-dedup, not UNION ALL).
# statement_timeout makes a non-terminating regression FAIL LOUD instead of hanging. The self-loop is dropped by
# the ff<>nb filter; the mutual cycle terminates and yields a sane verdict + a finite suggested_order.
psql "$A" -tAc "SET search_path=core; SELECT core.ingest_graph_with_authority('{\"nodes\":[{\"id\":\"adv/a.py\",\"kind\":\"file\",\"path\":\"adv/a.py\",\"language\":\"python\"},{\"id\":\"adv/b.py\",\"kind\":\"file\",\"path\":\"adv/b.py\",\"language\":\"python\"}],\"edges\":[{\"src\":\"adv/a.py\",\"dst\":\"adv/b.py\",\"kind\":\"imports\"},{\"src\":\"adv/b.py\",\"dst\":\"adv/a.py\",\"kind\":\"imports\"},{\"src\":\"adv/a.py\",\"dst\":\"adv/a.py\",\"kind\":\"imports\"}]}'::jsonb,'adv/cycle','main','c0c0')" >/dev/null
psql "$A" -tAc "SET search_path=core; SELECT core.declare_claim_with_authority('AD-CYA','adv/a.py','adv/cycle','main')" >/dev/null
psql "$B" -tAc "SET search_path=core; SELECT core.declare_claim_with_authority('AD-CYB','adv/b.py','adv/cycle','main')" >/dev/null
chk "adversarial(engine): a dependency CYCLE + self-loop TERMINATES (recursive CTE no infinite-loop) with a sane verdict" \
  "$(psql "$S" -tAc "SET statement_timeout='20s'; SET search_path=core; SELECT 'warn='||(core.main_impact_surface('adv/cycle','main')->>'warn_count')||' inflight='||(core.main_impact_surface('adv/cycle','main')->>'inflight_count')||' cluster='||(core.main_impact_surface('adv/cycle','main')->>'cluster_count')" 2>&1 | tail -1)" "^warn=2 inflight=2 cluster=1$"
chk "adversarial(engine): the cycle's cluster has a FINITE suggested_order (= cluster size 2), never unbounded" \
  "$(psql "$S" -tAc "SET statement_timeout='20s'; SET search_path=core; SELECT jsonb_array_length(c->'suggested_order')||'/'||(c->>'size') FROM jsonb_array_elements(core.main_impact_surface('adv/cycle','main')->'clusters') c" 2>&1 | tail -1)" "^2/2$"

# ENGINE PATHOLOGY 2 — EMPTY graph (no nodes): a claim on a path not in main's graph → honest 'unknown', no crash.
psql "$A" -tAc "SET search_path=core; SELECT core.ingest_graph_with_authority('{\"nodes\":[],\"edges\":[]}'::jsonb,'adv/empty','main','e0e0')" >/dev/null
psql "$A" -tAc "SET search_path=core; SELECT core.declare_claim_with_authority('AD-EG','adv/ghost.py','adv/empty','main')" >/dev/null
chk "adversarial(engine): an EMPTY graph yields honest unknown (never a false 'clear'), no crash" \
  "$(psql "$S" -tAc "SET statement_timeout='15s'; SET search_path=core; SELECT 'unknown='||(core.main_impact_surface('adv/empty','main')->>'unknown_count')||' clear='||(core.main_impact_surface('adv/empty','main')->>'clear_count')" 2>&1 | tail -1)" "^unknown=1 clear=0$"
# a repo with NO graph AND NO claims at all must also not crash (the cold-start read).
chk "adversarial(engine): a repo with NO graph and NO claims returns inflight=0 cleanly (cold-start, no crash)" \
  "$(psql "$S" -tAc "SET statement_timeout='15s'; SET search_path=core; SELECT (core.main_impact_surface('adv/nothing','main')->>'inflight_count')" 2>&1 | tail -1)" "^0$"

# ENGINE PATHOLOGY 3 — LARGE FAN CLUSTER: 40 files in a ring (f0→f1→…→f39→f0), one in-flight change per file =
# a single fully-entangled component. The recursive CTEs must terminate with a BOUNDED result: change_reach AND
# the inlined comp_cc inside main_impact_surface (the component grouping is now computed in-line, reusing the one
# adj/contested/waits_all pass — audit:perf 2026-06-18), PLUS the standalone _inflight_components (still asserted
# directly below, and still byte-equivalent to the inlined version). hub_degree raised so dampening doesn't
# dissolve the ring (we WANT the entanglement here). statement_timeout makes a blow-up FAIL LOUD. Asserts: 1
# cluster of size 40, 40 warns, finite order — AND, equivalence: the inlined comps == standalone _inflight_components.
RING_N=$(psql "$M" -tAc "SELECT jsonb_agg(jsonb_build_object('id','adv/r'||i||'.py','kind','file','path','adv/r'||i||'.py','language','python')) FROM generate_series(0,39) i")
RING_E=$(psql "$M" -tAc "SELECT jsonb_agg(jsonb_build_object('src','adv/r'||i||'.py','dst','adv/r'||((i+1)%40)||'.py','kind','imports')) FROM generate_series(0,39) i")
psql "$A" -tAc "SET search_path=core; SELECT core.ingest_graph_with_authority(jsonb_build_object('nodes','${RING_N}'::jsonb,'edges','${RING_E}'::jsonb),'adv/ring','main','5117')" >/dev/null
psql "$A" -tAc "SET search_path=core; SET veripsa.hub_degree='100'; $(for i in $(seq 0 39); do echo "SELECT core.declare_claim_with_authority('AD-RING$i','adv/r$i.py','adv/ring','main');"; done)" >/dev/null
chk "adversarial(engine): a 40-change fully-entangled RING terminates BOUNDED — 1 cluster, 40 in-flight, 40 warns" \
  "$(psql "$S" -tAc "SET statement_timeout='30s'; SET veripsa.hub_degree='100'; SET search_path=core; SELECT 'inflight='||(core.main_impact_surface('adv/ring','main')->>'inflight_count')||' clusters='||(core.main_impact_surface('adv/ring','main')->>'cluster_count')||' warn='||(core.main_impact_surface('adv/ring','main')->>'warn_count')" 2>&1 | tail -1)" "^inflight=40 clusters=1 warn=40$"
chk "adversarial(engine): the 40-change ring's suggested_order is FINITE (exactly 40 entries = cluster size)" \
  "$(psql "$S" -tAc "SET statement_timeout='30s'; SET veripsa.hub_degree='100'; SET search_path=core; SELECT jsonb_array_length(c->'suggested_order')||'/'||(c->>'size') FROM jsonb_array_elements(core.main_impact_surface('adv/ring','main')->'clusters') c" 2>&1 | tail -1)" "^40/40$"
chk "adversarial(engine): _inflight_components (standalone recursive CTE) collapses the ring to ONE component, terminating" \
  "$(psql "$M" -tAc "SET statement_timeout='30s'; SET veripsa.hub_degree='100'; SET search_path=core; SELECT set_config('core.current_account','ACCT-DEMO',true); SELECT count(DISTINCT comp)::text||'/'||count(*)::text FROM core._inflight_components('ACCT-DEMO','adv/ring','main')" 2>&1 | tail -1)" "^1/40$"
# FIX 1 EQUIVALENCE (audit:perf 2026-06-18): main_impact_surface now computes the component grouping INLINE
# (comp_cc, reusing the single adjacency pass) instead of calling _inflight_components a second time. The two must
# agree exactly. Assert the inlined cluster_count (= size-≥2 components seen by main_impact) EQUALS the size-≥2
# component count computed straight from the standalone helper on the SAME ring — a result-equivalence check, not
# just "both terminate". (Helper rolled up to per-comp sizes, then filtered ≥2, exactly as main_impact's clusters CTE.)
# ROLE SPLIT (the bug that turned the gate red): the two halves MUST run as DIFFERENT roles, so they cannot be one
# query. main_impact_surface resolves identity from the CONNECTION ROLE (resolve_session_identity, line ~4) and
# RAISES for an unprovisioned role — so it must be called as a provisioned seat ($S), NOT the migrator ($M, which
# has no credential → "no active credential"). The standalone helper _inflight_components is REVOKE'd from PUBLIC
# (an internal fn) so only the owner/migrator ($M) may call it. Compute each half with its correct role, then
# compare in shell. (The original single-query form ran the WHOLE thing as $M and 42501'd inside main_impact.)
MIS_CC=$(psql "$S" -tAc "SET statement_timeout='30s'; SET veripsa.hub_degree='100'; SET search_path=core; SELECT (core.main_impact_surface('adv/ring','main')->>'cluster_count')::int" 2>&1 | tail -1)
HELPER_CC=$(psql "$M" -tAc "SET statement_timeout='30s'; SET veripsa.hub_degree='100'; SET search_path=core; SELECT set_config('core.current_account','ACCT-DEMO',true); WITH h AS (SELECT comp, count(*) AS sz FROM core._inflight_components('ACCT-DEMO','adv/ring','main') GROUP BY comp) SELECT count(*)::int FROM h WHERE sz>=2" 2>&1 | tail -1)
chk "audit:perf — INLINED component grouping in main_impact == standalone _inflight_components (equivalent on the ring)" \
  "mis=${MIS_CC} helper=${HELPER_CC}" "^mis=1 helper=1$"
# PERF index assertion (audit:perf 2026-06-18): the src-keyed mirror of code_edge_coord_kind_dst must EXIST so the
# _claim_adjacency src probes (ie.src=…) get an index scan, not a full seq scan, on large graphs. Cheap catalog read.
chk "audit:perf — code_edge_coord_kind_src index EXISTS (serves _claim_adjacency src-keyed joins/anti-joins)" \
  "$(psql "$M" -tAc "SELECT count(*)::text FROM pg_indexes WHERE schemaname='core' AND tablename='code_edge' AND indexname='code_edge_coord_kind_src'" 2>&1 | tail -1)" "^1$"

# ── FREE-TIER WALL (db/schema/30_gate.sql) — the ENFORCED per-account quota at the gate (the single write path).
#    The free caps (free_max_repos / free_max_graph_units / free_max_events, owner-tunable via set_free_line) are no
#    longer merely DISPLAYED on the owner lens — they STOP DB growth past the cap. An account UNDER the line writes
#    normally; an account OVER any one dimension has its DB-growing writes (ingest_graph · patch_graph · record_push
#    · record_landing) REFUSED with a structured quota_exceeded result (no rows added) — advisory, NEVER a raise.
#    Claims are EXEMPT (transient bounded live state + the core lock product). Self-contained on DEDICATED accounts
#    so it cannot skew the rich ACCT-DEMO asserts above. Two write paths are exercised by their REAL grant identity:
#      • ingest_graph / patch_graph — the BUYER WRITER path (veripsa_writer): account Q-ACCT via veripsa_acme_agent.
#      • record_push / record_landing — the App-DELEGATION path (App-only by grant; a buyer writer is REFUSED by
#        design — see the REVOKE FROM veripsa_writer in the GRANTs block: a tenant must not forge a 'push reached
#        main' fact): the App ($P) routed to a fresh installation account via enter_installation_with_authority
#        (exactly the production webhook routing), so its writes land in ACCT-GH-instq, fully isolated.
#    Proves: UNDER → writes succeed; tune a cap LOW → the SAME account is now REFUSED on each dimension (repos /
#    graph_units / events); NO rows added on refusal; record_landing over the line records nothing (graceful 0);
#    fail-safe (the gate returns a clean signal, never an error); content-free.
psql "$M" -tAc "SET search_path=core; SELECT core.provision_seat('Q-ACCT','Quota Co','Q-AG','quota','veripsa_acme_agent')" >/dev/null
Q="postgresql://veripsa_acme_agent@localhost/${TDB}"
# 1) UNDER the line (generous caps) → an ingest SUCCEEDS normally (a real small team is never walled).
psql "$P" -tAc "SET search_path=core; SELECT core.set_free_line_with_authority('free_max_repos',10); SELECT core.set_free_line_with_authority('free_max_graph_units',200000); SELECT core.set_free_line_with_authority('free_max_events',50000)" >/dev/null
chk "quota: UNDER the line → ingest_graph SUCCEEDS (2 nodes; a real small team is not walled)" \
  "$(psql "$Q" -tAc "SET search_path=core; SELECT (core.ingest_graph_with_authority('{\"nodes\":[{\"id\":\"qf1\",\"kind\":\"file\",\"path\":\"q/a.py\"},{\"id\":\"qf2\",\"kind\":\"file\",\"path\":\"q/b.py\"}],\"edges\":[]}'::jsonb,'q/repo','main','aa11')::jsonb->>'nodes')" 2>&1)" "^2$"
# 2) TUNABLE WALL — graph_units: graph_units is now the PER-PLAN HARD line (core._plan_graph_units_limit, NOT
#    _free_line). Q-ACCT is on the 'free' plan, so set the FREE plan's graph_units line BELOW the account's
#    current 2 units → the SAME account is now OVER on graph_units. (Tuned via the per-plan setter.)
psql "$P" -tAc "SET search_path=core; SELECT core.set_plan_graph_units_limit_with_authority('free',1)" >/dev/null
chk "quota: tune graph_units LOW → a previously-OK account is now REFUSED (quota_exceeded, dimension=graph_units)" \
  "$(psql "$Q" -tAc "SET search_path=core; SELECT 'qx='||(core.ingest_graph_with_authority('{\"nodes\":[{\"id\":\"qf3\",\"kind\":\"file\",\"path\":\"q/c.py\"}],\"edges\":[]}'::jsonb,'q/repo','main','aa22')::jsonb->>'quota_exceeded')||' dim='||(core.ingest_graph_with_authority('{\"nodes\":[],\"edges\":[]}'::jsonb,'q/repo','main','aa22')::jsonb->>'dimension')" 2>&1)" "^qx=true dim=graph_units$"
chk "quota: the REFUSED ingest added NO rows (the coordinate still holds exactly the 2 under-the-line nodes)" \
  "$(psql "$M" -tAc "SET search_path=core; SELECT set_config('core.current_account','Q-ACCT',true); SELECT count(*) FROM core.code_node WHERE account_id='Q-ACCT'" 2>&1 | tail -1)" "^2$"
chk "quota: patch_graph is ALSO walled over the line (the incremental path returns quota_exceeded too)" \
  "$(psql "$Q" -tAc "SET search_path=core; SELECT (core.patch_graph_with_authority('{\"extractor_version\":\"cg4\",\"metrics\":{\"schema_contract_version\":2},\"expected_base_sha\":\"aa11\",\"expected_base_revision\":1,\"nodes\":[{\"id\":\"qf9\",\"kind\":\"file\",\"path\":\"q/a.py\"}],\"edges\":[]}'::jsonb,'q/repo','main',ARRAY['q/a.py'],ARRAY[]::text[],'aa44')->>'quota_exceeded')" 2>&1)" "^true$"
# 3) TUNABLE WALL — repos: generous graph_units again (restore the per-plan FREE line so graph_units no longer
#    binds — the graph_units check runs FIRST in _account_over_quota), but cap repos at 0 → ANY repo is over the
#    repos axis. (repos is still a _free_line knob, free-tier only.)
psql "$P" -tAc "SET search_path=core; SELECT core.set_plan_graph_units_limit_with_authority('free',200000); SELECT core.set_free_line_with_authority('free_max_repos',0)" >/dev/null
chk "quota: tune repos=0 → ingest is REFUSED on the repos dimension (the distinct-repo cap binds)" \
  "$(psql "$Q" -tAc "SET search_path=core; SELECT (core.ingest_graph_with_authority('{\"nodes\":[],\"edges\":[]}'::jsonb,'q/repo','main','aa33')::jsonb->>'dimension')" 2>&1)" "^repos$"
# 4) THE App-DELEGATION PATH (record_push / record_landing are App-only by grant). Route the App ($P) to a FRESH
#    installation account exactly as a production webhook does (enter_installation_with_authority pins the session →
#    record_push lands in ACCT-GH-instq). UNDER the generous line → record_push SUCCEEDS (returns an event id).
psql "$P" -tAc "SET search_path=core; SELECT core.set_free_line_with_authority('free_max_repos',10)" >/dev/null
chk "quota: App path UNDER the line → record_push SUCCEEDS (returns an event id, routed to the installation account)" \
  "$(psql "$P" -tAc "SET search_path=core; SELECT core.enter_installation_with_authority('instq'); SELECT core.record_push_with_authority('q/repo','main','beef01')" 2>&1 | tail -1)" "^EV-PUSH-"
# seed that installation account just OVER a low events cap, then prove record_push (App path) is refused on events.
psql "$M" -c "SET search_path=core;
  SELECT set_config('core.current_account','ACCT-GH-instq',true);
  SELECT set_config('core.governed_write_token','event',true);
  INSERT INTO core.event(account_id,event_id,kind,agent_id) SELECT 'ACCT-GH-instq','EV-QS'||g,'push','veripsa_app' FROM generate_series(1,5) g;" >/dev/null
psql "$P" -tAc "SET search_path=core; SELECT core.set_free_line_with_authority('free_max_events',3)" >/dev/null
chk "quota: events cap=3 with >3 events (over) → record_push (App path) REFUSED on the events dimension (bounded probe)" \
  "$(psql "$P" -tAc "SET search_path=core; SELECT core.enter_installation_with_authority('instq'); SELECT (core.record_push_with_authority('q/repo','main','beef03')::jsonb->>'dimension')" 2>&1 | tail -1)" "^events$"
chk "quota: the REFUSED record_push added NO push event for the new sha (no unbounded ledger growth)" \
  "$(psql "$M" -tAc "SET search_path=core; SELECT set_config('core.current_account','ACCT-GH-instq',true); SELECT count(*) FROM core.event WHERE account_id='ACCT-GH-instq' AND commit_sha='beef03'" 2>&1 | tail -1)" "^0$"
chk "quota: record_landing (App path) over the events line records ZERO landings (returns 0 = recorded nothing, graceful)" \
  "$(psql "$P" -tAc "SET search_path=core; SELECT core.enter_installation_with_authority('instq'); SELECT core.record_landing_with_authority('q/repo','main','beef05',ARRAY['q/a.py','q/b.py'],'someone')" 2>&1 | tail -1)" "^0$"
# 5) FAIL-SAFE: the quota check NEVER raises — even refused, the gate returns a clean jsonb signal (ok=false), so a
#    bug in the check can never crash ingestion. (A raise would surface here as an ERROR, not a parseable ok=false.)
chk "quota: FAIL-SAFE — a refused write returns a clean structured result (ok=false), never an ERROR/raise" \
  "$(psql "$P" -tAc "SET search_path=core; SELECT core.enter_installation_with_authority('instq'); SELECT (core.record_push_with_authority('q/repo','main','beef04')::jsonb->>'ok')" 2>&1 | tail -1)" "^false$"

# 6) THE LEDGER-WALL (#95 follow-up — the events-cap EVASION the four primary gate fns left open). The push/landing
#    wall above stops record_push/record_landing/ingest/patch, but the CLAIM path (declare/act_for — EXEMPT, and
#    correctly so for the claim table) and the PR-analysis path EMIT append-only EVENT/STATEMENT rows as a SIDE
#    EFFECT: record_collision (md5(random()) = a FRESH row per call), record_warn / record_prediction /
#    record_advice_outcome, record_pr_failing, record_statement. Those grow core.event / core.statement — the very
#    table free_max_events bounds — yet went through NO quota check, so an account already OVER its events cap could
#    keep filling the largest table by opening fresh PRs / colliding lanes. _ledger_write_blocked closes it: over
#    the EVENTS dimension → these writers record NOTHING (advisory, fail-OPEN). ACCT-GH-instq is still OVER the
#    events cap (5 events > cap 3, set above) and still routed. Seed an ACTIVE holder so record_collision has a
#    real lane to record against (else it RETURN NULLs on no-holder = indistinguishable from the wall), then prove
#    every ledger-writer records ZERO rows while over the cap — and that the SAME writers succeed once back UNDER.
psql "$M" -c "SET search_path=core;
  SELECT set_config('core.current_account','ACCT-GH-instq',true);
  SELECT set_config('core.governed_write_token','claim',true);
  INSERT INTO core.claim(claim_id,account_id,agent_id,change_id,repo,branch,target_path,claim_state)
    VALUES('LW-HOLD','ACCT-GH-instq','veripsa_app','PR-hold','q/repo','main','q/lane.py','active') ON CONFLICT DO NOTHING;" >/dev/null
# drive the ledger writers via the App role ($P, the over-quota routed tenant); read the counts via $M (admin can
# read past RLS by pinning current_account — the same write-via-$P / count-via-$M split the push test above uses).
psql "$P" -tAc "SET search_path=core; SELECT core.enter_installation_with_authority('instq');
  SELECT core.record_collision_with_authority('q/lane.py','q/repo','main','GH-attacker');
  SELECT core.record_warn_with_authority('q/lane.py','q/repo','main','PR-attacker');
  SELECT core.record_prediction_with_authority('PR-attacker','q/repo','main','warn',NULL);
  SELECT core.record_statement_with_authority('owns q/lane.py','q/lane.py','q/repo','main')" >/dev/null 2>&1
chk "ledger-wall: OVER the events cap → record_collision records NOTHING (the md5(random) unbounded-growth path is walled)" \
  "$(psql "$M" -tAc "SET search_path=core; SELECT set_config('core.current_account','ACCT-GH-instq',true); SELECT count(*) FROM core.event WHERE account_id='ACCT-GH-instq' AND kind='collision_held'" 2>&1 | tail -1)" "^0$"
chk "ledger-wall: OVER the events cap → record_warn records NOTHING (per-PR warn_issued no longer evades the cap)" \
  "$(psql "$M" -tAc "SET search_path=core; SELECT set_config('core.current_account','ACCT-GH-instq',true); SELECT count(*) FROM core.event WHERE account_id='ACCT-GH-instq' AND kind='warn_issued'" 2>&1 | tail -1)" "^0$"
chk "ledger-wall: OVER the events cap → record_prediction records NOTHING (answer-check telemetry is walled too)" \
  "$(psql "$M" -tAc "SET search_path=core; SELECT set_config('core.current_account','ACCT-GH-instq',true); SELECT count(*) FROM core.event WHERE account_id='ACCT-GH-instq' AND kind='prediction'" 2>&1 | tail -1)" "^0$"
chk "ledger-wall: OVER the events cap → record_statement records NOTHING (statement_id md5(random) is walled, advisory not raise)" \
  "$(psql "$M" -tAc "SET search_path=core; SELECT set_config('core.current_account','ACCT-GH-instq',true); SELECT count(*) FROM core.statement WHERE account_id='ACCT-GH-instq'" 2>&1 | tail -1)" "^0$"
chk "ledger-wall: FAIL-SAFE — the walled ledger writers RETURN cleanly (no value = skipped), never an ERROR/raise" \
  "$(psql "$P" -tAc "SET search_path=core; SELECT core.enter_installation_with_authority('instq'); SELECT 'ok='||COALESCE(core.record_warn_with_authority('q/lane.py','q/repo','main','PR-x'),'(skipped)')" 2>&1 | tail -1)" "^ok=\(skipped\)$"
# back UNDER the line (generous events cap) → the SAME writers SUCCEED (the wall is a cost guard, never a feature kill).
psql "$P" -tAc "SET search_path=core; SELECT core.set_free_line_with_authority('free_max_events',50000)" >/dev/null
psql "$P" -tAc "SET search_path=core; SELECT core.enter_installation_with_authority('instq'); SELECT core.record_collision_with_authority('q/lane.py','q/repo','main','GH-attacker')" >/dev/null 2>&1
chk "ledger-wall: back UNDER the events cap → record_collision SUCCEEDS again (records exactly ONE collision_held row)" \
  "$(psql "$M" -tAc "SET search_path=core; SELECT set_config('core.current_account','ACCT-GH-instq',true); SELECT count(*) FROM core.event WHERE account_id='ACCT-GH-instq' AND kind='collision_held'" 2>&1 | tail -1)" "^1$"
psql "$M" -c "SET search_path=core; SELECT set_config('core.current_account','ACCT-GH-instq',true); SELECT set_config('core.governed_write_token','claim',true); DELETE FROM core.claim WHERE account_id='ACCT-GH-instq' AND claim_id='LW-HOLD';" >/dev/null

# restore the GENEROUS production default so any later read / the owner block starts from a clean line (the owner
# block re-tunes its own test values explicitly below regardless — this keeps the block order-independent).
psql "$P" -tAc "SET search_path=core; SELECT core.set_free_line_with_authority('free_max_repos',10); SELECT core.set_free_line_with_authority('free_max_graph_units',200000); SELECT core.set_free_line_with_authority('free_max_events',50000)" >/dev/null
# CLEANUP (block isolation): this block registered ACCT-GH-instq in installation_account (the App-routing map the
# owner lens enumerates DISTINCT account_id over). Drop that ROUTING row so the owner block's cross-tenant
# account_count asserts below count ONLY its own OWNER-SMALL/OWNER-BIG (the leftover account/event rows for
# ACCT-GH-instq are append-only + RLS-walled and, crucially, are NEVER enumerated once the routing row is gone — so
# removing the one routing row fully isolates the blocks without touching the immutable ledger). Admin (migrator).
psql "$M" -c "SET search_path=core; DELETE FROM core.installation_account WHERE installation_id='instq';" >/dev/null

# ── OWNER COST/USAGE LENS (db/schema/95_owner.sql) — the founder's cross-tenant DB-growth view + the free line.
#    THE DELIBERATE EXCEPTION to per-tenant RLS: owner-context, sees ALL accounts, locked to the host service
#    identity (veripsa_app) the SAME way the retention sweep + DR export are. LAST + on DEDICATED accounts
#    (OWNER-SMALL / OWNER-BIG, registered in installation_account = the no-RLS tenant routing map the lens
#    enumerates) so it never skews the per-account counts the surfaces above assert. Seeds two footprints (one
#    tiny, one over a LOW test free-line) and proves: per-account counts + footprint_pct + over_free_line flags,
#    db_total_bytes present, biggest-first ordering, the free-line knobs are tunable+clamped, recent (7d) growth,
#    content-free output — AND, critically, a tenant role gets permission denied while the owner succeeds.
# seed the two owner-test tenants (admin: provision + register their installation so the lens enumerates them).
psql "$M" -tAc "SET search_path=core;
  SELECT core.provision_seat('OWNER-SMALL','Small Co','OW-SM','sm','veripsa_acme_agent');
  SELECT core.provision_seat('OWNER-BIG','Big Co','OW-BG','bg','veripsa_demo_agent3');" >/dev/null
psql "$M" -c "SET search_path=core;
  SELECT set_config('core.current_account','OWNER-SMALL',true);
  INSERT INTO core.installation_account(installation_id,account_id) VALUES('inst-ow-small','OWNER-SMALL') ON CONFLICT DO NOTHING;
  SELECT set_config('core.current_account','OWNER-BIG',true);
  INSERT INTO core.installation_account(installation_id,account_id) VALUES('inst-ow-big','OWNER-BIG') ON CONFLICT DO NOTHING;" >/dev/null
# OWNER-SMALL: 1 repo, 2 nodes, 0 edges; 2 events (one OLD = outside the 7d window → events=2 but events_7d=1).
psql "$M" -c "SET search_path=core;
  SELECT set_config('core.current_account','OWNER-SMALL',true);
  SELECT set_config('core.governed_write_token','graph_version',true);
  INSERT INTO core.graph_version(account_id,repo,branch,node_count,edge_count) VALUES('OWNER-SMALL','small/r','main',2,0);
  SELECT set_config('core.governed_write_token','code_node',true);
  INSERT INTO core.code_node(account_id,repo,branch,node_id,node_kind,path) VALUES('OWNER-SMALL','small/r','main','n1','file','a.py'),('OWNER-SMALL','small/r','main','n2','file','b.py');
  SELECT set_config('core.governed_write_token','event',true);
  INSERT INTO core.event(account_id,event_id,kind,agent_id,occurred_at) VALUES('OWNER-SMALL','EV-OLD','push','OW-SM',now()-interval '10 days');
  SELECT set_config('core.governed_write_token','event',true);
  INSERT INTO core.event(account_id,event_id,kind,agent_id,occurred_at) VALUES('OWNER-SMALL','EV-NEW','push','OW-SM',now()-interval '1 day');" >/dev/null
# OWNER-BIG: 5 repos, 12 nodes, 6 edges, 8 events — over the LOW free-line on ALL THREE axes.
psql "$M" -c "SET search_path=core;
  SELECT set_config('core.current_account','OWNER-BIG',true);
  SELECT set_config('core.governed_write_token','graph_version',true);
  INSERT INTO core.graph_version(account_id,repo,branch,node_count,edge_count) SELECT 'OWNER-BIG','big/r'||g,'main',2,1 FROM generate_series(1,5) g;
  SELECT set_config('core.governed_write_token','code_node',true);
  INSERT INTO core.code_node(account_id,repo,branch,node_id,node_kind,path) SELECT 'OWNER-BIG','big/r1','main','bn'||g,'file','f'||g||'.py' FROM generate_series(1,12) g;
  SELECT set_config('core.governed_write_token','code_edge',true);
  INSERT INTO core.code_edge(account_id,repo,branch,src,dst,edge_kind) SELECT 'OWNER-BIG','big/r1','main','f'||g||'.py','f'||(g+1)||'.py','imports' FROM generate_series(1,6) g;
  SELECT set_config('core.governed_write_token','event',true);
  INSERT INTO core.event(account_id,event_id,kind,agent_id) SELECT 'OWNER-BIG','EV-B'||g,'push','OW-BG' FROM generate_series(1,8) g;" >/dev/null
# the owner tunes a LOW test free-line (mechanism, not pricing): repos=3, units=10, events=5. Each call returns
# the CLAMPED effective value (so this also proves the knobs are tunable).
chk "owner: free-line knob is tunable (set max_repos=3 returns the effective 3)" \
  "$(psql "$P" -tAc "SET search_path=core; SELECT core.set_free_line_with_authority('free_max_repos',3)" 2>&1)" "^3$"
chk "owner: free-line knob is CLAMPED high (set max_repos=999999999 → clamps to 100000, never out of frame)" \
  "$(psql "$P" -tAc "SET search_path=core; SELECT core.set_free_line_with_authority('free_max_repos',999999999)" 2>&1)" "^100000$"
chk "owner: free-line knob is CLAMPED low (set max_repos=-5 → clamps to 0, never negative)" \
  "$(psql "$P" -tAc "SET search_path=core; SELECT core.set_free_line_with_authority('free_max_repos',-5)" 2>&1)" "^0$"
chk "owner: an unknown free-line key is rejected (a typo can't create dead config)" \
  "$(psql "$P" -tAc "SET search_path=core; SELECT core.set_free_line_with_authority('free_max_bananas',5)" 2>&1)" "unknown free-line key"
# now set the real test free-line for the count assertions.
psql "$P" -tAc "SET search_path=core; SELECT core.set_free_line_with_authority('free_max_repos',3); SELECT core.set_free_line_with_authority('free_max_graph_units',10); SELECT core.set_free_line_with_authority('free_max_events',5)" >/dev/null
chk "owner: surface reports db_total_bytes (the whole-DB size, the cost denominator) > 0" \
  "$(psql "$P" -tAc "SET search_path=core; SELECT ((core.owner_cost_surface()->>'db_total_bytes')::bigint > 0)" 2>&1)" "^t$"
chk "owner: free_line echoes the tuned knobs (3 / 10 / 5)" \
  "$(psql "$P" -tAc "SET search_path=core; SELECT 'r='||(core.owner_cost_surface()->'free_line'->>'max_repos')||' u='||(core.owner_cost_surface()->'free_line'->>'max_graph_units')||' e='||(core.owner_cost_surface()->'free_line'->>'max_events')" 2>&1)" "^r=3 u=10 e=5$"
chk "owner: account_count = the 2 enumerated tenants, over_line_count = 1 (only BIG is over)" \
  "$(psql "$P" -tAc "SET search_path=core; SELECT 'n='||(core.owner_cost_surface()->>'account_count')||' o='||(core.owner_cost_surface()->>'over_line_count')" 2>&1)" "^n=2 o=1$"
chk "owner: ordering — biggest consumer FIRST (OWNER-BIG at footprint 180% = the worst of its 3 ratios)" \
  "$(psql "$P" -tAc "SET search_path=core; SELECT (core.owner_cost_surface()->'accounts'->0->>'account_id')||' '||(core.owner_cost_surface()->'accounts'->0->>'footprint_pct')" 2>&1)" "^OWNER-BIG 180.0$"
chk "owner: OWNER-BIG per-account counts (5 repos · 12 nodes · 6 edges · 18 units · 8 events) + over_free_line=true" \
  "$(psql "$P" -tAc "SET search_path=core; SELECT 'repos='||(a->>'repos')||' n='||(a->>'graph_nodes')||' e='||(a->>'graph_edges')||' u='||(a->>'graph_units')||' ev='||(a->>'events')||' over='||(a->>'over_free_line') FROM (SELECT e a FROM jsonb_array_elements(core.owner_cost_surface()->'accounts') e WHERE e->>'account_id'='OWNER-BIG') x" 2>&1)" "^repos=5 n=12 e=6 u=18 ev=8 over=true$"
# OWNER-SMALL is UNDER the line on every axis (repos 1/3=33.3% · units 2/10=20% · events 2/5=40%); footprint_pct =
# the MAX of the three = 40.0 (the events axis binds), and over_free_line=false (40% < 100%). This proves the
# footprint is the worst-case ratio, not an average — a single hot dimension is what flags an account.
chk "owner: OWNER-SMALL is UNDER the line (footprint = max-of-3-ratios = 40.0%, the events axis) + over_free_line=false" \
  "$(psql "$P" -tAc "SET search_path=core; SELECT 'pct='||(a->>'footprint_pct')||' over='||(a->>'over_free_line') FROM (SELECT e a FROM jsonb_array_elements(core.owner_cost_surface()->'accounts') e WHERE e->>'account_id'='OWNER-SMALL') x" 2>&1)" "^pct=40.0 over=false$"
chk "owner: events_7d is RECENT growth only (OWNER-SMALL events=2 total but events_7d=1; the 10-day-old one ages out)" \
  "$(psql "$P" -tAc "SET search_path=core; SELECT 'ev='||(a->>'events')||' 7d='||(a->>'events_7d') FROM (SELECT e a FROM jsonb_array_elements(core.owner_cost_surface()->'accounts') e WHERE e->>'account_id'='OWNER-SMALL') x" 2>&1)" "^ev=2 7d=1$"
chk "owner: output is CONTENT-FREE (account ids + counts only — no path/branch/repo name leaks)" \
  "$(psql "$P" -tAc "SET search_path=core; SELECT core.owner_cost_surface()" 2>&1 | grep -oE 'small/r|big/r|a\.py|f1\.py|\"main\"' | head -1)" "^$"
# CRITICAL — the owner-only lock. A tenant role MUST be refused; only the host service identity may read across tenants.
chk "owner LOCK: a tenant WRITER (veripsa_demo_agent) calling owner_cost_surface → permission denied (owner-only)" \
  "$(psql "$A" -tAc "SET search_path=core; SELECT core.owner_cost_surface()" 2>&1)" "permission denied for function owner_cost_surface"
chk "owner LOCK: a tenant READER seat (veripsa_demo_steward) is ALSO refused (the lens is not a buyer surface)" \
  "$(psql "$S" -tAc "SET search_path=core; SELECT core.owner_cost_surface()" 2>&1)" "permission denied for function owner_cost_surface"
chk "owner LOCK: a tenant cannot TUNE the free line either (set_free_line is owner-only)" \
  "$(psql "$A" -tAc "SET search_path=core; SELECT core.set_free_line_with_authority('free_max_repos',999)" 2>&1)" "permission denied for function set_free_line_with_authority"
chk "owner LOCK: the OWNER (veripsa_app, the host service identity) SUCCEEDS where the tenants were refused" \
  "$(psql "$P" -tAc "SET search_path=core; SELECT ((core.owner_cost_surface()->>'account_count')::int >= 2)" 2>&1)" "^t$"

# ── SEAT (value) LINE — the PLG conversion proxy. A "seat" = a HUMAN agent active in 30d; AI agents are FREE.
#    Seed humans into the two owner-test tenants + edge cases (an AI agent, a '[bot]'-suffixed human, an INACTIVE
#    human >30d) and prove: only active humans count, AI/bots/aged-out are excluded, paid_seats = max(0, seats −
#    free_seat_line), over_seat_line at/over the line, and the top-level free_seat_line / paid_seats_total /
#    over_seat_line_count rollups. (OWNER-SMALL/OWNER-BIG's existing OW-SM/OW-BG agents are agent_kind='ai' from
#    provision_seat → they already contribute ZERO seats, so this is purely additive to the count assertions above.)
# OWNER-SMALL: exactly 1 active human (HU-SM-1) → at-or-under a 2-seat line (paid_seats=0, not a candidate).
psql "$M" -c "SET search_path=core;
  SELECT set_config('core.current_account','OWNER-SMALL',true);
  SELECT set_config('core.governed_write_token','agent',true);
  INSERT INTO core.agent(agent_id,account_id,display_name,agent_kind) VALUES('HU-SM-1','OWNER-SMALL','Alice','human') ON CONFLICT (agent_id) DO NOTHING;
  SELECT set_config('core.governed_write_token','event',true);
  INSERT INTO core.event(account_id,event_id,kind,agent_id,occurred_at) VALUES('OWNER-SMALL','EV-HS1','push','HU-SM-1',now()-interval '2 days');" >/dev/null
# OWNER-BIG: 3 active humans (HU-BG-1 via an event, HU-BG-2 via a CLAIM, HU-BG-3 via an event) → over a 2-seat line
#  (paid_seats=1). Plus three EXCLUDED rows that must NOT count: AI-BG (agent_kind='ai'), HU-BOT[bot] (a human-kind
#  but '[bot]'-suffixed login = the defensive exclusion), HU-OLD (a human whose ONLY activity is >30d ago = aged out).
psql "$M" -c "SET search_path=core;
  SELECT set_config('core.current_account','OWNER-BIG',true);
  SELECT set_config('core.governed_write_token','agent',true);
  INSERT INTO core.agent(agent_id,account_id,display_name,agent_kind) VALUES
    ('HU-BG-1','OWNER-BIG','Bob','human'),('HU-BG-2','OWNER-BIG','Carol','human'),('HU-BG-3','OWNER-BIG','Dave','human'),
    ('AI-BG','OWNER-BIG','Helper','ai'),('HU-BOT','OWNER-BIG','renovate[bot]','human'),('HU-OLD','OWNER-BIG','Eve','human')
    ON CONFLICT (agent_id) DO NOTHING;
  SELECT set_config('core.governed_write_token','event',true);
  INSERT INTO core.event(account_id,event_id,kind,agent_id,occurred_at) VALUES
    ('OWNER-BIG','EV-HB1','push','HU-BG-1',now()-interval '1 day'),
    ('OWNER-BIG','EV-HB3','push','HU-BG-3',now()-interval '5 days'),
    ('OWNER-BIG','EV-AIB','push','AI-BG',now()-interval '1 day'),
    ('OWNER-BIG','EV-BOT','push','HU-BOT',now()-interval '1 day'),
    ('OWNER-BIG','EV-OLD','push','HU-OLD',now()-interval '40 days');
  SELECT set_config('core.governed_write_token','claim',true);
  INSERT INTO core.claim(account_id,claim_id,agent_id,target_path,repo,branch,claimed_at)
    VALUES('OWNER-BIG','CL-HB2','HU-BG-2','f1.py','big/r1','main',now()-interval '3 days');" >/dev/null
# the owner tunes a 2-seat test line (mechanism, not pricing) — and prove it's tunable + CLAMPED like the others.
chk "owner SEAT: free_max_seats is tunable (set 2 returns the effective 2)" \
  "$(psql "$P" -tAc "SET search_path=core; SELECT core.set_free_line_with_authority('free_max_seats',2)" 2>&1)" "^2$"
chk "owner SEAT: free_max_seats is CLAMPED high (set 999999999 → clamps to 100000)" \
  "$(psql "$P" -tAc "SET search_path=core; SELECT core.set_free_line_with_authority('free_max_seats',999999999)" 2>&1)" "^100000$"
chk "owner SEAT: free_max_seats is CLAMPED low (set -3 → clamps to 0)" \
  "$(psql "$P" -tAc "SET search_path=core; SELECT core.set_free_line_with_authority('free_max_seats',-3)" 2>&1)" "^0$"
psql "$P" -tAc "SET search_path=core; SELECT core.set_free_line_with_authority('free_max_seats',2)" >/dev/null
chk "owner SEAT: free_line echoes the tuned seat line (max_seats=2)" \
  "$(psql "$P" -tAc "SET search_path=core; SELECT core.owner_cost_surface()->'free_line'->>'max_seats'" 2>&1)" "^2$"
chk "owner SEAT: top-level free_seat_line=2 + paid_seats_total=1 (only OWNER-BIG's 3rd human is billable)" \
  "$(psql "$P" -tAc "SET search_path=core; SELECT 'fsl='||(core.owner_cost_surface()->>'free_seat_line')||' pst='||(core.owner_cost_surface()->>'paid_seats_total')" 2>&1)" "^fsl=2 pst=1$"
chk "owner SEAT: over_seat_line_count=1 (OWNER-BIG is AT/over the line = a conversion candidate; SMALL is not)" \
  "$(psql "$P" -tAc "SET search_path=core; SELECT core.owner_cost_surface()->>'over_seat_line_count'" 2>&1)" "^1$"
chk "owner SEAT: OWNER-BIG active_agents=3 (humans via event+claim; AI + '[bot]' + 40-day-old all EXCLUDED), paid_seats=1, over=true" \
  "$(psql "$P" -tAc "SET search_path=core; SELECT 'a='||(a->>'active_agents')||' p='||(a->>'paid_seats')||' o='||(a->>'over_seat_line') FROM (SELECT e a FROM jsonb_array_elements(core.owner_cost_surface()->'accounts') e WHERE e->>'account_id'='OWNER-BIG') x" 2>&1)" "^a=3 p=1 o=true$"
chk "owner SEAT: OWNER-SMALL active_agents=1 (one active human; the OW-SM AI agent doesn't count), paid_seats=0, over=false (under the line)" \
  "$(psql "$P" -tAc "SET search_path=core; SELECT 'a='||(a->>'active_agents')||' p='||(a->>'paid_seats')||' o='||(a->>'over_seat_line') FROM (SELECT e a FROM jsonb_array_elements(core.owner_cost_surface()->'accounts') e WHERE e->>'account_id'='OWNER-SMALL') x" 2>&1)" "^a=1 p=0 o=false$"
chk "owner SEAT: a tenant cannot tune the seat line either (set_free_line free_max_seats is owner-only)" \
  "$(psql "$A" -tAc "SET search_path=core; SELECT core.set_free_line_with_authority('free_max_seats',9)" 2>&1)" "permission denied for function set_free_line_with_authority"
chk "owner SEAT: still CONTENT-FREE with the seat dimension (no human display_name leaks — Alice/Bob/Carol/renovate)" \
  "$(psql "$P" -tAc "SET search_path=core; SELECT core.owner_cost_surface()" 2>&1 | grep -oE 'Alice|Bob|Carol|Dave|renovate|Eve|Helper' | head -1)" "^$"

echo "------------------------------------------------------------"
if [ "$FAIL" -eq 0 ]; then echo "NEW FOUNDATION SMOKE: PASS"; else echo "NEW FOUNDATION SMOKE: FAIL"; exit 1; fi
