#!/usr/bin/env python3
"""MIGRATION SAFETY gate — a PROD REDEPLOY over EXISTING, populated data must be LOSSLESS + IDEMPOTENT.

THE RISK (the real one). Prod runs an OLDER schema and holds LIVE tenant data. The next manual redeploy
re-applies the CURRENT `db/schema.sql` to that already-populated DB (NOT a fresh `createdb` — the prod DB
is never dropped). A single non-idempotent or destructive statement in the accumulated schema (a bare
`CREATE TABLE` that now errors "already exists", an `ADD COLUMN ... NOT NULL` with no DEFAULT, a new
`CHECK`/`UNIQUE` that pre-existing rows violate, an unconditional `DROP`/re-key) would either FAIL the
deploy mid-apply or silently DROP/TRUNCATE tenant data. Property #43 proved this for the one PK-widen;
this gate proves it for EVERYTHING db/schema.sql has grown since (95_owner's owner-cost lens, 30_gate's
enforced-quota helpers, the finer-collision span columns, every policy/trigger/constraint).

WHAT THIS PROVES (a redeploy is known-safe):
  1. Bootstrap a DB with the CURRENT schema, then seed representative LIVE-like data across the durable
     tables through the REAL gate write path (the path prod data actually arrived by): two accounts,
     a code graph (code_node/code_edge/graph_version), an active claim, push/landed/collision_held events,
     plus statement/follow/policy/store_connection/intent. Snapshot exact row counts + a content checksum
     (md5 of every row, ordered) per durable table.
  2. RE-APPLY db/schema.sql to the POPULATED DB as veripsa_migrator (the redeploy). Assert:
       (a) it SUCCEEDS with NO error (every CREATE is OR REPLACE / IF NOT EXISTS, every policy/constraint
           re-applies cleanly);
       (b) ALL seeded data SURVIVES BYTE-FOR-BYTE — identical counts AND identical per-table checksums
           (no loss, no truncation, no silent rewrite);
       (c) the NEW objects exist + work afterward (owner_cost_surface, the _account_over_quota helper,
           the finer-collision span columns, the widened claim PK).
  3. Re-apply a THIRD time → still clean + still byte-identical (idempotency is STABLE, not a one-shot).
  4. THE OLD→NEW TRANSITION the conditional DDL actually guards: take a fresh DB, DOWNGRADE the claim PK to
     the historical (account_id, claim_id) shape + seed a row, then re-apply → assert the guarded re-key
     FIRES (PK becomes the wide (account_id, repo, claim_id)) AND the pre-existing row survives. This is the
     exact prod shape (prod's claim PK predates the widen) — proves the one piece of conditional migration
     DDL is correct on real old data, not just a no-op on an already-current DB.
  5. STATIC HAZARD SCAN of db/schema.sql + every include: assert NO bare CREATE TABLE/TYPE (all IF NOT
     EXISTS), NO ADD COLUMN ... NOT NULL without a DEFAULT, NO unconditional DROP / TRUNCATE / DELETE at
     apply scope (DELETEs inside SECURITY DEFINER fn bodies are runtime gate ops, not apply-time DDL).

Run:  python3 tests/test_migration_safety.py   (needs local Postgres with the veripsa roles)
"""
from __future__ import annotations

import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# PROCESS-UNIQUE (parallel-safe): this gate bootstraps + drops its DBs, so a FIXED name lets concurrent runs
# (parallel CI shards / several agents each running run_gates) drop each other's DB mid-run → "does not exist".
# Per-PID, exactly like db/smoke.sh (veripsa_smoke_$$), run_gates (veripsa_gates_$$), test_app_deploy_resolution.
PID = str(os.getpid())
DB = "veripsa_migsafe_" + PID                 # the main lossless+idempotent DB
DB_OLD = "veripsa_migsafe_old_" + PID         # the old→new PK-transition DB

MIG = f"postgresql://veripsa_migrator@localhost/{DB}"
AGENT = f"postgresql://veripsa_demo_agent@localhost/{DB}"
APP = f"postgresql://veripsa_app@localhost/{DB}"
# A SUPERUSER connection to take the LOSSLESS snapshot: nearly every core table is FORCE ROW LEVEL SECURITY
# (so even the table-owning migrator only sees the ONE pinned tenant). The redeploy puts ALL tenants' rows at
# risk, so the fingerprint must see ALL of them — a superuser bypasses RLS and reads every tenant in one pass.
# This is the SAME admin DSN db/bootstrap_local.sh authenticates the roles bootstrap with (localhost/postgres).
ADMIN = os.environ.get("ADMIN_DSN", "postgresql://localhost/postgres")
SU_USER = ""        # the superuser role name (resolved in main(), reused to build the snapshot DSN)

# the durable tables a buyer/operator would be devastated to lose — every CREATE TABLE in db/schema/*.sql.
DURABLE_TABLES = [
    "account", "agent", "credential", "grant", "installation_account",
    "claim", "code_node", "code_edge", "graph_version", "event", "intent",
    "statement", "follow", "policy", "store_connection", "webhook_delivery",
]


def psql(dsn, sql, ignore_err=False):
    """Run SQL, return (stdout, stderr, rc) all stripped. Raises on error unless ignore_err."""
    r = subprocess.run(["psql", dsn, "-v", "ON_ERROR_STOP=1", "-tAc", sql],
                       cwd=ROOT, capture_output=True, text=True)
    if r.returncode != 0 and not ignore_err:
        raise RuntimeError(f"psql failed:\n  SQL: {sql[:200]}\n  ERR: {(r.stderr or '')[-500:]}")
    return (r.stdout or "").strip(), (r.stderr or "").strip(), r.returncode


def value(dsn, sql):
    """The LAST non-empty output line of a psql call. psql -tAc echoes a line per statement (a `SET` prints
    'SET', set_config prints its value), so when a read must pin core.current_account first, only the final
    SELECT's value is wanted — this strips the preamble lines deterministically."""
    out, _, _ = psql(dsn, sql)
    lines = [ln for ln in out.splitlines() if ln.strip() != ""]
    return lines[-1] if lines else ""


def apply_schema(dsn):
    """Apply db/schema.sql exactly as the redeploy does (psql -f, ON_ERROR_STOP). Return (ok, stderr)."""
    r = subprocess.run(["psql", dsn, "-v", "ON_ERROR_STOP=1", "-q", "-f", "db/schema.sql"],
                       cwd=ROOT, capture_output=True, text=True)
    return r.returncode == 0, (r.stderr or "")[-1500:]


def snapshot(su_dsn):
    """A byte-for-byte fingerprint of every durable table: (row_count, content_md5) keyed by table.

    content_md5 = md5 of every row rendered to text and ORDERED, so it catches loss, truncation, OR a silent
    in-place rewrite — not just a count change. Taken as a SUPERUSER so it bypasses the FORCE-RLS walls and
    sees ALL tenants' rows in one pass (the migrator, though owner, is itself walled by FORCE RLS to a single
    pinned account — it cannot fingerprint the whole DB the redeploy puts at risk).
    """
    snap = {}
    for t in DURABLE_TABLES:
        # to_jsonb(t.*)::text gives a stable, column-complete textual form of each row independent of column
        # order; ORDER BY the whole text so the md5 is deterministic regardless of physical row order.
        sql = (f"SELECT count(*), COALESCE(md5(string_agg(r, E'\\n' ORDER BY r)), '<empty>') "
               f"FROM (SELECT to_jsonb(x.*)::text AS r FROM core.{t} x) s")
        out, _, _ = psql(su_dsn, sql)
        cnt, h = out.split("|", 1)
        snap[t] = (int(cnt), h)
    return snap


def seed_live_like_data():
    """Seed representative LIVE-like data across the durable tables via the REAL gate write path.

    This mirrors how prod data actually arrived (the gate is the only write path), NOT raw INSERTs — so the
    seeded rows carry exactly the shape/constraints a redeploy must preserve.
    """
    # bootstrap = ACCT-DEMO + 4 seats (agent/agent2/steward/app) → account, agent, credential rows.
    r = subprocess.run(["bash", "db/bootstrap_local.sh", DB], cwd=ROOT, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"bootstrap failed:\n{(r.stderr or r.stdout)[-1200:]}")

    # a SECOND account (cross-tenant breadth; lets the social follow have a real target). provision_seat is
    # migrator-only — the same admin op bootstrap uses.
    psql(MIG, "SET search_path=core; SELECT core.provision_seat('ACCT-ACME','Acme Co','AG-ACME','dev','veripsa_acme_agent');")

    # Route GitHub installation 'inst-demo' → the existing ACCT-DEMO (bootstrap leaves the routing map empty).
    # Without this, enter_installation_with_authority would LAZILY mint a fresh ACCT-GH-inst-demo and the App's
    # push/landing facts would land in THAT tenant, not the demo tenant the agent writes populate — so consolidate
    # them onto ACCT-DEMO so all three event kinds (push/landed/collision_held) live in one representative tenant.
    psql(MIG, "SET search_path=core; SELECT set_config('core.current_account','ACCT-DEMO',true); "
              "INSERT INTO core.installation_account(installation_id, account_id) VALUES ('inst-demo','ACCT-DEMO') "
              "ON CONFLICT (installation_id) DO NOTHING;")

    # ── as a buyer agent (veripsa_writer path): graph + claim + records + saas + social ───────────────────
    # a code graph across TWO repos (code_node + code_edge + graph_version, with node_count/edge_count the
    # quota helper reads). ingest_graph_with_authority is the real ingest the App calls per push.
    psql(AGENT, "SET search_path=core; SELECT core.ingest_graph_with_authority("
                "'{\"nodes\":["
                "{\"id\":\"f1\",\"kind\":\"file\",\"path\":\"src/pay.py\",\"language\":\"python\"},"
                "{\"id\":\"d1\",\"kind\":\"def\",\"path\":\"src/pay.py\",\"name\":\"charge\"},"
                "{\"id\":\"f2\",\"kind\":\"file\",\"path\":\"src/api.py\",\"language\":\"python\"},"
                "{\"id\":\"d2\",\"kind\":\"def\",\"path\":\"src/api.py\",\"name\":\"handle\"}],"
                "\"edges\":[{\"src\":\"d2\",\"dst\":\"charge\",\"kind\":\"calls\"}]}'::jsonb,"
                "'acme/app','main','abc123');")
    psql(AGENT, "SET search_path=core; SELECT core.ingest_graph_with_authority("
                "'{\"nodes\":["
                "{\"id\":\"r\",\"kind\":\"file\",\"path\":\"reports.py\",\"language\":\"python\"},"
                "{\"id\":\"m\",\"kind\":\"file\",\"path\":\"db/0007.sql\",\"language\":\"sql\"},"
                "{\"id\":\"t\",\"kind\":\"table\",\"path\":\"db/0007.sql\",\"name\":\"orders\"}],"
                "\"edges\":[{\"src\":\"reports.py\",\"dst\":\"orders\",\"kind\":\"queries\"},"
                "{\"src\":\"db/0007.sql\",\"dst\":\"orders\",\"kind\":\"alters\"}]}'::jsonb,"
                "'acme/schema','main','def456');")
    # an ACTIVE claim (the lock — live mutable state the redeploy must not drop). act_for_claim is the App's
    # per-PR call but is also writer-reachable here for seeding; granted to veripsa_app, so use the App conn.
    psql(APP, "SET search_path=core; SELECT core.enter_installation_with_authority('inst-demo'); "
              "SELECT (core.act_for_claim_with_authority('PR-1:src/pay.py','src/pay.py','acme/app','main','alice')->>'granted');")
    # records / saas / social, all via their real gate fns (veripsa_writer-granted → callable as the agent).
    psql(AGENT, "SET search_path=core; SELECT core.record_statement_with_authority('charge() now idempotent','src/pay.py','acme/app','main');")
    psql(AGENT, "SET search_path=core; SELECT core.connect_store_with_authority('CONN-1','github','acme/app','src/');")
    psql(AGENT, "SET search_path=core; SELECT core.set_policy_with_authority('window_days','30');")
    psql(AGENT, "SET search_path=core; SELECT core.follow_account_with_authority('ACCT-ACME');")

    # ── as the App service identity (veripsa_app): the push + landing facts ───────────────────────────────
    # record_push (a 'push' event) + record_landing (a 'landed' event) are App-only (the App acts for authors).
    # Enter the demo installation first so both events route into ACCT-DEMO (the installation→account routing the
    # live App uses per webhook; inst-demo was mapped to ACCT-DEMO above). NOTE: land_on_main writes a 'push'
    # event (+ releases claims); the distinct 'landed' fact is record_landing's — seed both for ledger breadth.
    psql(APP, "SET search_path=core; SELECT core.enter_installation_with_authority('inst-demo'); "
              "SELECT core.record_push_with_authority('acme/app','main','deadbeef','claude-opus-4-8');")
    psql(APP, "SET search_path=core; SELECT core.enter_installation_with_authority('inst-demo'); "
              "SELECT core.record_landing_with_authority('acme/app','main','cafe01',ARRAY['PR-1'],'alice');")

    # ── collision_held events + an intent: no simple buyer fn (internal kinds), so seed via the migrator with
    #    the account pinned + mark_governed_write — EXACTLY db/smoke.sh's proven seeding pattern. ───────────
    psql(MIG, "SET search_path=core; SELECT set_config('core.current_account','ACCT-DEMO',true); "
              "SELECT core.mark_governed_write('event'); "
              "INSERT INTO core.event(event_id,account_id,kind,agent_id,counterparty_agent,repo,branch,path) VALUES "
              "('CH-1','ACCT-DEMO','collision_held','AG-B','AG-A','acme/app','main','src/pay.py'),"
              "('CH-2','ACCT-DEMO','collision_held','AG-A','AG-B','acme/app','main','src/pay.py');")
    psql(MIG, "SET search_path=core; SELECT set_config('core.current_account','ACCT-DEMO',true); "
              "SELECT core.mark_governed_write('intent'); "
              "INSERT INTO core.intent(intent_id,account_id,agent_id,work_ref,summary,scope_in) VALUES "
              "('IN-1','ACCT-DEMO','AG-A','PR-1','make charge idempotent',ARRAY['src/pay.py']);")
    # The hosted App's operational inbox is also live data: a redeploy must not drop queued accepted deliveries.
    # Seed it through the real App-granted SECURITY DEFINER function, not a raw INSERT.
    psql(APP, "SET search_path=core; "
              "SELECT core.enqueue_webhook_delivery_with_authority("
              "'mig-delivery-1','push','4242','acme/app',"
              "'{\"repository\":{\"full_name\":\"acme/app\"},\"ref\":\"refs/heads/main\",\"after\":\"abc\"}'::jsonb,"
              "5000);")


def seed_old_pk_shape():
    """Stand up a SEPARATE DB whose claim PK is the HISTORICAL (account_id, claim_id) shape + a pre-existing
    row — the exact prod precondition the guarded re-key in 20_core.sql targets. Returns (ok, detail)."""
    r = subprocess.run(["bash", "db/bootstrap_local.sh", DB_OLD], cwd=ROOT, capture_output=True, text=True)
    if r.returncode != 0:
        return False, f"bootstrap_old failed: {(r.stderr or r.stdout)[-400:]}"
    mig_old = f"postgresql://veripsa_migrator@localhost/{DB_OLD}"
    # DOWNGRADE the PK to the old narrow shape (simulate prod, which predates the widen). The unique backstop
    # index claim_one_active stays — only the PK shape is rolled back, exactly what the guard inspects.
    psql(mig_old, "SET search_path=core; "
                  "ALTER TABLE core.claim DROP CONSTRAINT claim_pkey; "
                  "ALTER TABLE core.claim ADD CONSTRAINT claim_pkey PRIMARY KEY (account_id, claim_id);")
    # a pre-existing claim row (must survive the re-key). Insert as the migrator with the account pinned +
    # mark_governed_write (the same governed-write path).
    psql(mig_old, "SET search_path=core; SELECT set_config('core.current_account','ACCT-DEMO',true); "
                  "SELECT core.mark_governed_write('claim'); "
                  "INSERT INTO core.claim(claim_id,account_id,agent_id,repo,branch,target_path) VALUES "
                  "('PR-9:README.md','ACCT-DEMO','AG-A','acme/app','main','README.md');")
    return True, mig_old


# ── STATIC HAZARD SCAN over db/schema.sql + every \ir include ─────────────────────────────────────────────
def schema_files():
    idx = os.path.join(ROOT, "db", "schema.sql")
    files = [idx]
    with open(idx) as f:
        for line in f:
            m = re.match(r"\s*\\ir\s+(\S+)", line)
            if m:
                files.append(os.path.join(ROOT, "db", m.group(1)))
    return files


def scan_hazards():
    """Return a list of (file, lineno, hazard) for migration-unsafe DDL on EXISTING data. Empty = safe."""
    hazards = []
    for path in schema_files():
        with open(path) as f:
            lines = f.readlines()
        rel = os.path.relpath(path, ROOT)
        for i, raw in enumerate(lines, 1):
            line = raw.strip()
            low = line.lower()
            if low.startswith("--"):
                continue
            # bare CREATE TABLE / TYPE (would error "already exists" on a redeploy → deploy FAILS mid-apply).
            if re.search(r"\bcreate\s+(unlogged\s+)?table\b", low) and "if not exists" not in low:
                hazards.append((rel, i, "CREATE TABLE without IF NOT EXISTS"))
            if re.search(r"\bcreate\s+type\b", low):
                # CREATE TYPE has no IF NOT EXISTS in PG; a bare one re-errors on redeploy. Guarded forms wrap
                # it in a DO/EXCEPTION block (none exist today). Any literal CREATE TYPE is a hazard to flag.
                hazards.append((rel, i, "CREATE TYPE (no IF NOT EXISTS in PG → must be DO-guarded)"))
            # CREATE INDEX without IF NOT EXISTS (re-errors on redeploy).
            if re.search(r"\bcreate\s+(unique\s+)?index\b", low) and "if not exists" not in low and "concurrently" not in low:
                hazards.append((rel, i, "CREATE INDEX without IF NOT EXISTS"))
            # ADD COLUMN ... NOT NULL without a DEFAULT (a NOT NULL with no default fails on a populated table).
            if re.search(r"\badd\s+column\b", low) and re.search(r"\bnot\s+null\b", low) and " default " not in low:
                hazards.append((rel, i, "ADD COLUMN ... NOT NULL without DEFAULT (fails on existing rows)"))
    return hazards


def main() -> int:
    global SU_USER
    checks = []  # (name, ok)

    def chk(name, cond):
        checks.append((name, bool(cond)))
        return bool(cond)

    # the superuser role name (db/bootstrap_local.sh runs roles.sql via this same ADMIN DSN). Used to build the
    # RLS-bypassing snapshot DSN against each scratch DB.
    SU_USER = value(ADMIN, "SELECT current_user")
    su = lambda dbname: f"postgresql://{SU_USER}@localhost/{dbname}"

    # ════════ 1. STATIC HAZARD SCAN (cheap, deterministic — read the schema before touching a DB) ════════
    hazards = scan_hazards()
    chk(f"static scan: db/schema.sql + {len(schema_files())-1} includes carry NO migration-unsafe DDL "
        f"(found {len(hazards)})", len(hazards) == 0)
    if hazards:
        for f, ln, h in hazards:
            print(f"    HAZARD {f}:{ln} — {h}")

    # ════════ 2. SEED LIVE-LIKE DATA, then snapshot the byte-for-byte fingerprint ════════
    seed_live_like_data()
    before = snapshot(su(DB))
    # the seed must actually have populated the representative durable tables (else "survives" is vacuous).
    seeded = {t: c for t, (c, _) in before.items() if c > 0}
    chk(f"seed: representative LIVE-like data populated the durable tables "
        f"({len(seeded)}/{len(DURABLE_TABLES)} non-empty: {','.join(sorted(seeded))})",
        before["account"][0] >= 2 and before["code_node"][0] > 0 and before["code_edge"][0] > 0 and
        before["graph_version"][0] > 0 and before["claim"][0] > 0 and before["event"][0] > 0 and
        before["credential"][0] > 0 and before["statement"][0] > 0 and before["follow"][0] > 0 and
        before["policy"][0] > 0 and before["store_connection"][0] > 0 and before["intent"][0] > 0 and
        before["webhook_delivery"][0] > 0)
    # the event ledger holds all three representative kinds (push, landed, collision_held). Read via the
    # superuser (FORCE RLS otherwise hides the ledger from any un-pinned read).
    kinds = value(su(DB), "SELECT string_agg(DISTINCT kind, ',' ORDER BY kind) FROM core.event WHERE account_id='ACCT-DEMO'")
    chk(f"seed: the event ledger carries push + landed + collision_held ({kinds})",
        "push" in kinds and "landed" in kinds and "collision_held" in kinds)

    # ════════ 3. RE-APPLY db/schema.sql to the POPULATED DB (the redeploy) — must succeed, NO error ════════
    ok2, err2 = apply_schema(MIG)
    chk("RE-APPLY #2: db/schema.sql re-applies to the POPULATED DB with NO error (idempotent redeploy)", ok2)
    if not ok2:
        print("    re-apply #2 stderr:\n", err2)

    # ════════ 4. ALL seeded data SURVIVES BYTE-FOR-BYTE (identical counts AND content checksums) ════════
    after = snapshot(su(DB))
    lossless = True
    for t in DURABLE_TABLES:
        if before[t] != after[t]:
            lossless = False
            print(f"    DRIFT in core.{t}: before={before[t]} after={after[t]}")
    chk("LOSSLESS: every durable table is BYTE-FOR-BYTE identical after the redeploy "
        "(same row count AND same content md5 — no loss, no truncation, no silent rewrite)", lossless)

    # ════════ 5. the NEW objects exist + WORK after the redeploy ════════
    # owner cost lens (95_owner) — the founder's cross-tenant read; call it as the App (owner-granted). Enter an
    # installation first so the App has a resolvable service identity (the live per-event shape).
    owner_ok = value(APP, "SET search_path=core; SELECT core.enter_installation_with_authority('inst-demo'); "
                          "SELECT (core.owner_cost_surface() ? 'accounts')::text")
    chk("NEW object: owner_cost_surface() (95_owner cost lens) exists + returns its shape after redeploy",
        owner_ok == "true")
    # the enforced-quota helper (30_gate) — the free-tier wall; returns NULL (under the line) for the demo acct.
    quota_ok = value(MIG, "SET search_path=core; SELECT (core._account_over_quota('ACCT-DEMO') IS NULL)::text")
    chk("NEW object: _account_over_quota() (30_gate enforced free-tier wall) exists + evaluates after redeploy",
        quota_ok == "true")
    # the finer-collision span columns (20_core ADD COLUMN) — present on code_node + claim (information_schema is
    # not RLS-walled, so the plain migrator read is fine here).
    cols = value(MIG, "SELECT count(*) FROM information_schema.columns "
                      "WHERE table_schema='core' AND ((table_name='code_node' AND column_name IN ('start_line','end_line')) "
                      "OR (table_name='claim' AND column_name IN ('touched_ranges','change_id')))")
    chk("NEW columns: finer-collision spans (code_node.start_line/end_line, claim.touched_ranges/change_id) present",
        cols == "4")
    # the widened claim PK is the current (account_id, repo, claim_id) on this freshly-bootstrapped+redeployed DB.
    pk = value(MIG, "SELECT string_agg(a.attname, ',' ORDER BY array_position(c.conkey, a.attnum)) "
                    "FROM pg_constraint c JOIN pg_attribute a ON a.attrelid=c.conrelid AND a.attnum=ANY(c.conkey) "
                    "WHERE c.conrelid='core.claim'::regclass AND c.contype='p'")
    chk(f"NEW shape: claim PK is the widened (account_id, repo, claim_id) after redeploy (got {pk})",
        pk == "account_id,repo,claim_id")

    # ════════ 6. APPLY A THIRD TIME → still clean + still byte-identical (idempotency is STABLE) ════════
    ok3, err3 = apply_schema(MIG)
    chk("RE-APPLY #3: a third apply still succeeds with NO error (idempotency is stable, not one-shot)", ok3)
    if not ok3:
        print("    re-apply #3 stderr:\n", err3)
    after3 = snapshot(su(DB))
    chk("LOSSLESS x3: data still byte-for-byte identical after the THIRD apply",
        all(after3[t] == before[t] for t in DURABLE_TABLES))

    # ════════ 7. THE OLD→NEW PK TRANSITION (the conditional DDL on REAL old data — prod's actual shape) ════
    ok_old, mig_old = seed_old_pk_shape()
    if not ok_old:
        chk("OLD→NEW PK: stood up a DB with the historical (account_id, claim_id) PK + a pre-existing row", False)
        print("   ", mig_old)
    else:
        db_old = DB_OLD  # the old-shape scratch DB name (mig_old is its migrator DSN)
        pk_q = ("SELECT string_agg(a.attname, ',' ORDER BY array_position(c.conkey, a.attnum)) "
                "FROM pg_constraint c JOIN pg_attribute a ON a.attrelid=c.conrelid AND a.attnum=ANY(c.conkey) "
                "WHERE c.conrelid='core.claim'::regclass AND c.contype='p'")
        # the precondition: the PK really is the OLD narrow shape with a row in it (read the row via superuser
        # so FORCE RLS doesn't hide it).
        pk_old = value(mig_old, pk_q)
        row_before = value(su(db_old), "SELECT target_path FROM core.claim WHERE claim_id='PR-9:README.md'")
        chk(f"OLD→NEW PK: DB stands up with the historical narrow PK ({pk_old}) + a pre-existing claim row",
            pk_old == "account_id,claim_id" and row_before == "README.md")
        # RE-APPLY schema.sql → the guarded re-key in 20_core must FIRE (sees the old shape) and WIDEN the PK.
        ok_re, err_re = apply_schema(mig_old)
        chk("OLD→NEW PK: re-applying schema.sql over the OLD shape SUCCEEDS (no error)", ok_re)
        if not ok_re:
            print("    old-shape re-apply stderr:\n", err_re)
        pk_new = value(mig_old, pk_q)
        chk(f"OLD→NEW PK: the guarded re-key FIRED — PK widened to (account_id, repo, claim_id) (got {pk_new})",
            pk_new == "account_id,repo,claim_id")
        # and the pre-existing row SURVIVED the re-key (no data loss during the PK migration).
        row_after = value(su(db_old), "SELECT target_path FROM core.claim WHERE claim_id='PR-9:README.md'")
        chk("OLD→NEW PK: the pre-existing claim row SURVIVED the PK widen (lossless re-key)",
            row_after == "README.md")

    # ════════ verdict ════════
    ok_all = True
    for name, cond in checks:
        print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
        ok_all = ok_all and cond
    print("MIGRATION SAFETY GATE:", "PASS" if ok_all else "FAIL")
    return 0 if ok_all else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    finally:
        subprocess.run(["dropdb", DB], capture_output=True)
        subprocess.run(["dropdb", DB_OLD], capture_output=True)
