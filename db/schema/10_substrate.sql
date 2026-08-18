-- ============================================================================================
-- Veripsa — the NEW foundation (greenfield re-foundation, PO 2026-06-17).
--
-- WHY THIS FILE EXISTS. The old schema grew from a long inward-DB evolution into a GENERIC abstraction
-- (atom + relation + governed-write machinery underneath records / boxes / relations / governance /
-- a gig-market). Everything concrete had to route through that generality, so "繋ぎが大変" — connecting
-- anything was painful (PO: "根が腐ってるから繋ぎが大変"). This is the purpose-built replacement: the
-- foundation IS the product — TRAFFIC CONTROL (the engine) + SOCIAL (the face) — with concrete entities
-- that connect DIRECTLY. The old repo is reference-only; its generality does not survive.
--
-- WHAT IS KEPT (the moat — the product's worth, carried as PROVEN PATTERNS, not the generic layer):
--   * every table is CONTENT-FREE (ids / paths / counts / short labels — never file bodies),
--   * account-scoped with FORCE ROW LEVEL SECURITY + a tenant_isolation policy,
--   * written ONLY through the gate (SECURITY DEFINER `*_with_authority` fns that arm a forgery token),
--   * ledgers are APPEND-ONLY + immutable (a recorded fact is permanent = record == execution),
--   * identity is pinned by the CONNECTION ROLE, never by an argument (the buyer never holds a key).
--
-- BUILD ORDER (this file grows phase by phase; the old schema.sql keeps the product running until cutover):
--   Phase 1 (here): the moat spine + identity/access substrate (account · agent · credential · grant).
--   Phase 2: traffic-control core (claim/lock · graph · collision held/steered · push · intent).
--   Phase 3: records (statement). Phase 4: social + SaaS. Then cutover + delete the old.
--
-- DESIGNED FOR EXTENSION (PO 2026-06-17: "基盤妥協したら、また同じように燃やすことになる。ある程度拡張
-- される前提は必要"). A too-narrow root burns as fast as a too-generic one. The premise here is growth —
-- but extensibility comes from a uniform PATTERN + clean SEAMS, never from a generic abstraction (that
-- WAS the rot):
--   * The moat is a PATTERN every table wears (content-free · account_id · FORCE RLS · the forgery token
--     · append-only for ledgers). A NEW fact-type = a new concrete table wearing the pattern + a gate fn
--     + a surface — direct and cheap, never routed through a generic atom/relation.
--   * The COORDINATE (repo,branch,path) and the account/agent identity are first-class, reused concepts;
--     new coordinate-keyed / account-scoped things attach without surgery. Crucially the coordinate is
--     first-class so MULTIPLE graphs/branches/coordinates COEXIST from day one (no tenant-wipe — that
--     rigid compromise is exactly what forces a re-found later).
--   * OPEN where it grows (store providers · models · social signals = data, never a frozen enum);
--     CLOSED where the domain is bounded (states, CHECK-pinned).
--
-- NO TABLE 乱立 — the law that keeps it durable (PO 2026-06-17: "テーブル乱立は事前に防げるように。長期運用に
-- 耐える必要。旧db思想は極端だったが汎用性は高かった"). A table-per-fact-type would sprawl over years of
-- AI-driven extension; the old generic DB avoided that (few tables, high versatility) but paid in painful
-- connections. The durable middle:
--   * FEW CONCRETE NOUNS are tables (account · agent · claim · code graph · store_connection · policy …)
--     — distinct shapes, distinct relations.
--   * FACTS are KINDS in ONE append-only `event` ledger (collision_held · push · drift · future signals),
--     with TYPED common columns (account · agent · coordinate · at · visibility) + a few TYPED nullable
--     extension columns. A new signal = a new KIND (a row), NOT a new table. This keeps the old
--     generality's virtue (versatility, few tables) WITHOUT its extreme (no unbounded atom/relation blob;
--     columns stay typed + concrete so connections stay direct). RECORDS (statement) are their own stream;
--     SURFACES are functions — neither grows tables.
--   * GUARDRAIL (事前に防げる, structural): a gate pins the `core` table set to a REGISTERED allowlist
--     (a manifest). A new table FAILS the gate until deliberately registered — an AI cannot quietly
--     sprawl tables; the default path for a new fact is an `event` kind, and adding a NOUN is a conscious,
--     reviewed act. (Enforced by the table-budget MANIFEST in db/smoke.sh — part of the gate suite.)
--
-- VOCAB: words everyone knows stay (agent · collision · claim · push); coin only for a genuinely-new
-- category (PO doctrine). ISOLATION KEY = account_id (the tenant). ACTOR = agent_id.
-- ============================================================================================

CREATE SCHEMA IF NOT EXISTS core;
ALTER SCHEMA core OWNER TO veripsa_migrator;
-- CREATE FUNCTION grants EXECUTE to PUBLIC by default. schema.sql is intentionally applied without one global
-- transaction, so waiting until module 99 to revoke that ambient grant leaves every newly-created SECURITY
-- DEFINER function callable during a deploy and permanently callable if the apply is interrupted. Change the
-- creating role's GLOBAL function default BEFORE the first function is created. A schema-local REVOKE cannot
-- cancel PostgreSQL's global PUBLIC EXECUTE default. Module 99 remains the idempotent backstop for functions that
-- predate this default or were created by an older schema version.
ALTER DEFAULT PRIVILEGES FOR ROLE veripsa_migrator REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC;
-- the buyer's seats reach the gate fns through the schema, but get NO direct table grants (the only
-- write path is the gate; a direct INSERT is refused for lack of a table grant AND by the forgery trigger).
GRANT USAGE ON SCHEMA core TO veripsa_reader, veripsa_writer, veripsa_demo_steward;
-- DEFENSE-IN-DEPTH against a SECURITY DEFINER search_path hijack. Every gate fn is SECURITY DEFINER (runs as
-- the migrator) and pins SET search_path = core, pg_catalog, so it never resolves an unqualified name against
-- the caller's path. The classic privesc — a tenant plants a shadow object in a schema that sits EARLIER on
-- the path (the textbook one being `public`, where PUBLIC historically held CREATE) so the definer fn calls
-- the tenant's code as the owner — is therefore already closed by the pin. We also slam the door at the
-- source: PUBLIC must never be able to CREATE in `public`. On PG15+ this is the default (PUBLIC keeps only
-- USAGE), but stating it makes the posture VERSION-INDEPENDENT (on PG≤14 the default DID grant CREATE to
-- PUBLIC) and explicit. Idempotent; the tamper-privesc gate proves both halves (every SECDEF fn pinned + no
-- tenant can CREATE in public).
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
SET search_path TO core, pg_catalog;

-- HOT-DEPLOY DDL CUT. schema.sql is declarative and is replayed for every new
-- generation while the previous image still writes. PostgreSQL takes
-- AccessExclusiveLock for `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` before the
-- IF NOT EXISTS no-op is known, so replaying an old additive migration can
-- queue behind one live writer and convoy every later writer. Route every
-- additive column/default/not-null ensure through a catalog read first; only a
-- genuinely missing contract executes table DDL. These helpers are owner-only
-- deployment machinery, never an App write surface.
--
-- Publish all four definitions + ownership + ACLs atomically. The global
-- ALTER DEFAULT PRIVILEGES above already removes PUBLIC EXECUTE from a first
-- creation, but CREATE OR REPLACE preserves a pre-existing ACL. Keeping this
-- short catalog-only block in one transaction means neither a historical ACL
-- nor an interrupted apply can expose a migrator-owned dynamic-DDL helper.
BEGIN;
CREATE OR REPLACE FUNCTION core._ensure_column_online(
    p_table text,
    p_column text,
    p_definition text
) RETURNS void
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'core','pg_catalog' AS $$
DECLARE v_table regclass;
BEGIN
  IF p_table IS NULL OR p_column IS NULL OR p_definition IS NULL
     OR length(p_table) > 63 OR length(p_column) > 63
     OR length(p_definition) > 1000 OR p_definition LIKE '%;%' THEN
    RAISE EXCEPTION 'invalid online column contract'
      USING ERRCODE='22023';
  END IF;
  v_table := to_regclass(format('core.%I',p_table));
  IF v_table IS NULL THEN
    RAISE EXCEPTION 'online column contract table core.% does not exist', p_table
      USING ERRCODE='42P01';
  END IF;
  IF NOT EXISTS (
    SELECT 1
      FROM pg_attribute
     WHERE attrelid=v_table
       AND attname=p_column
       AND attnum > 0
       AND NOT attisdropped
  ) THEN
    EXECUTE format(
      'ALTER TABLE core.%I ADD COLUMN %I %s',
      p_table,p_column,p_definition
    );
  END IF;
END $$;
ALTER FUNCTION core._ensure_column_online(text,text,text)
  OWNER TO veripsa_migrator;
REVOKE EXECUTE ON FUNCTION core._ensure_column_online(text,text,text)
  FROM PUBLIC,veripsa_app,veripsa_writer;

CREATE OR REPLACE FUNCTION core._ensure_column_default_online(
    p_table text,
    p_column text,
    p_default text
) RETURNS void
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'core','pg_catalog' AS $$
DECLARE v_table regclass; v_actual text;
BEGIN
  IF p_table IS NULL OR p_column IS NULL OR p_default IS NULL
     OR length(p_table) > 63 OR length(p_column) > 63
     OR length(p_default) > 500 OR p_default LIKE '%;%' THEN
    RAISE EXCEPTION 'invalid online column-default contract'
      USING ERRCODE='22023';
  END IF;
  v_table := to_regclass(format('core.%I',p_table));
  SELECT pg_get_expr(d.adbin,d.adrelid)
    INTO v_actual
    FROM pg_attribute a
    LEFT JOIN pg_attrdef d
      ON d.adrelid=a.attrelid AND d.adnum=a.attnum
   WHERE a.attrelid=v_table
     AND a.attname=p_column
     AND a.attnum > 0
     AND NOT a.attisdropped;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'online default column core.%.% does not exist',
      p_table,p_column USING ERRCODE='42703';
  END IF;
  IF regexp_replace(COALESCE(v_actual,''),'\s','','g')
     IS DISTINCT FROM regexp_replace(p_default,'\s','','g') THEN
    EXECUTE format(
      'ALTER TABLE core.%I ALTER COLUMN %I SET DEFAULT %s',
      p_table,p_column,p_default
    );
  END IF;
END $$;
ALTER FUNCTION core._ensure_column_default_online(text,text,text)
  OWNER TO veripsa_migrator;
REVOKE EXECUTE ON FUNCTION core._ensure_column_default_online(text,text,text)
  FROM PUBLIC,veripsa_app,veripsa_writer;

CREATE OR REPLACE FUNCTION core._ensure_column_not_null_online(
    p_table text,
    p_column text
) RETURNS void
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'core','pg_catalog' AS $$
DECLARE v_table regclass; v_not_null boolean;
BEGIN
  v_table := to_regclass(format('core.%I',p_table));
  SELECT attnotnull
    INTO v_not_null
    FROM pg_attribute
   WHERE attrelid=v_table
     AND attname=p_column
     AND attnum > 0
     AND NOT attisdropped;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'online not-null column core.%.% does not exist',
      p_table,p_column USING ERRCODE='42703';
  END IF;
  IF NOT v_not_null THEN
    EXECUTE format(
      'ALTER TABLE core.%I ALTER COLUMN %I SET NOT NULL',
      p_table,p_column
    );
  END IF;
END $$;
ALTER FUNCTION core._ensure_column_not_null_online(text,text)
  OWNER TO veripsa_migrator;
REVOKE EXECUTE ON FUNCTION core._ensure_column_not_null_online(text,text)
  FROM PUBLIC,veripsa_app,veripsa_writer;

CREATE OR REPLACE FUNCTION core._ensure_constraint_valid_online(
    p_table text,
    p_constraint text
) RETURNS void
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'core','pg_catalog' AS $$
DECLARE v_table regclass; v_valid boolean;
BEGIN
  v_table := to_regclass(format('core.%I',p_table));
  SELECT convalidated
    INTO v_valid
    FROM pg_constraint
   WHERE conrelid=v_table
     AND conname=p_constraint;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'online constraint core.%.% does not exist',
      p_table,p_constraint USING ERRCODE='42704';
  END IF;
  IF NOT v_valid THEN
    EXECUTE format(
      'ALTER TABLE core.%I VALIDATE CONSTRAINT %I',
      p_table,p_constraint
    );
  END IF;
END $$;
ALTER FUNCTION core._ensure_constraint_valid_online(text,text)
  OWNER TO veripsa_migrator;
REVOKE EXECUTE ON FUNCTION core._ensure_constraint_valid_online(text,text)
  FROM PUBLIC,veripsa_app,veripsa_writer;
COMMIT;

-- ────────────────────────────────────────────────────────────────────────────────────────────
-- THE MOAT SPINE (carried over, proven). Every gated write arms a one-shot token naming its table;
-- the forgery trigger refuses any write that did not. Identity comes from the connection role.
-- ────────────────────────────────────────────────────────────────────────────────────────────

-- mark_governed_write: the gate arms this immediately before a privileged INSERT into <tag>.
CREATE OR REPLACE FUNCTION core.mark_governed_write(p_tag text) RETURNS void
    LANGUAGE sql AS $$ SELECT set_config('core.governed_write_token', p_tag, true) $$;
ALTER FUNCTION core.mark_governed_write(text) OWNER TO veripsa_migrator;
-- Defense-in-depth: only the gate (SECURITY DEFINER fns owned by the migrator, which call this as the
-- owner) may arm the forgery token. A tenant has no table GRANTs to exploit a forged token even if armed,
-- but revoking PUBLIC keeps the token-arming surface inside the gate — belt-and-suspenders on the moat.
REVOKE EXECUTE ON FUNCTION core.mark_governed_write(text) FROM PUBLIC;

-- mark_retention_prune: the retention gate arms this (txn-local) with the row-account it is allowed to
-- prune, immediately before the bounded DELETE on the event ledger. assert_append_only permits a DELETE
-- ONLY when this token equals the row's own account_id — so the prune is account-scoped and un-forgeable
-- (a plain DELETE that never armed this token is still refused; the curated history stays append-only/immutable).
CREATE OR REPLACE FUNCTION core.mark_retention_prune(p_account text) RETURNS void
    LANGUAGE sql AS $$ SELECT set_config('core.retention_token', p_account, true) $$;
ALTER FUNCTION core.mark_retention_prune(text) OWNER TO veripsa_migrator;
-- defense-in-depth (parity with the two sibling token-arming primitives above): only the gate (migrator-owned
-- SECURITY DEFINER fns, which call this as the owner) may arm the retention token. A tenant has no DELETE grant
-- on the ledger to exploit a forged token even if armed (assert_append_only still demands token == the row's own
-- account_id), so this is inert today — but keeping the token-arming surface inside the gate is the consistent
-- posture its siblings already hold (mark_governed_write / mark_account_erasure both REVOKE this from PUBLIC).
REVOKE EXECUTE ON FUNCTION core.mark_retention_prune(text) FROM PUBLIC;

-- mark_account_erasure: the RIGHT-TO-DELETION gate arms this (txn-local) with the ONE account being fully
-- erased, immediately before the account-scoped HARD DELETE of every per-account row (erase_account_with_
-- authority). It is the SAME token shape as retention, with a stricter contract: assert_append_only and
-- assert_statement_immutable permit a DELETE on the immutable streams (core.event / core.statement) ONLY
-- when this token equals the row's own account_id — so a GDPR/CCPA "erase this tenant" is account-scoped and
-- un-forgeable, NEVER a trigger-drop (the append-only protection stays live for every OTHER tenant during the
-- erase). A plain DELETE that never armed this token is still refused, so the append-only guarantee on history is
-- preserved for all retained tenants. The token equals the row's account_id, so even an armed erasure can
-- never reach across tenants (crypto-erase / account-scoped purge that preserves OTHER tenants' immutability).
CREATE OR REPLACE FUNCTION core.mark_account_erasure(p_account text) RETURNS void
    LANGUAGE sql AS $$ SELECT set_config('core.account_erasure_token', p_account, true) $$;
ALTER FUNCTION core.mark_account_erasure(text) OWNER TO veripsa_migrator;
-- defense-in-depth: only the gate (migrator-owned SECURITY DEFINER fns) may arm the erasure token.
REVOKE EXECUTE ON FUNCTION core.mark_account_erasure(text) FROM PUBLIC;

-- assert_governed_write: FORGERY BLOCK. A direct INSERT/UPDATE is refused unless the gate armed the
-- token for THIS table. So the only write path is the gate (record == execution, un-forgeable).
CREATE OR REPLACE FUNCTION core.assert_governed_write() RETURNS trigger
    LANGUAGE plpgsql SET search_path TO 'core','pg_catalog' AS $$ BEGIN
  IF current_setting('core.governed_write_token', true) IS DISTINCT FROM TG_TABLE_NAME THEN
    RAISE EXCEPTION 'forgery block: direct writes to % are not allowed; use the gate only.', TG_TABLE_NAME
      USING ERRCODE = '42501';
  END IF; RETURN NEW; END $$;
ALTER FUNCTION core.assert_governed_write() OWNER TO veripsa_migrator;

-- assert_append_only: a recorded fact is PERMANENT. DELETE always blocked; UPDATE blocked except a
-- visibility-scope-only change (the owner may widen who SEES an unchanged record). A table with no
-- 'visibility' column admits no UPDATE at all. One named, account-pinned demo-fixture bypass
-- (core.demo_maintenance_token = the row's account AND that account is the demo account) lets the
-- local fixture be torn down — a real account's history can never be mutated, by anyone, by any form.
-- ONE OTHER named DELETE exception: RETENTION. The ledger grows unbounded (every push/landing forever ×
-- all tenants), so a gated, account-scoped prune of OPERATIONAL TELEMETRY past the window is allowed —
-- and ONLY that. It is NOT tampering: the curated effect records (warn_issued/collision_held) and the
-- statement stream stay immutable; this prune is the same gate/token shape as every other governed write
-- (the prune fn arms core.retention_token = the row's OWN account, then DELETEs; a plain DELETE that did
-- not arm the token is STILL refused, so the append-only guarantee on history is preserved). The token equals the
-- row's account_id, so even an armed prune can never reach across tenants.
-- TRUST MODEL (be precise — append-only ≠ cryptographic tamper-EVIDENCE): this trigger enforces IMMUTABILITY by
-- PREVENTION — it REFUSES every UPDATE/DELETE (and a sibling refuses TRUNCATE), so an in-DB rewrite is blocked
-- outright. It is NOT a hash-chain: rows are not chained by a per-row hash, so there is no way to DETECT an
-- offline rewrite of the storage (a backup edit, or a true superuser dropping the trigger) after the fact. The
-- integrity guarantee therefore terminates at "the DB operator is trusted" — strong inside that boundary, not
-- beyond it. If independently-verifiable records (a buyer can prove their ledger was not altered, even by us)
-- ever become a sold promise, the upgrade is a content-free per-row hash-chain (entry_hash over the content-free
-- columns + the prior row's hash) + a verify function — that is the only thing that makes "tamper-EVIDENT" true.
CREATE OR REPLACE FUNCTION core.assert_append_only() RETURNS trigger
    LANGUAGE plpgsql SET search_path TO 'core','pg_catalog' AS $$
DECLARE v_old jsonb; v_new jsonb; v_has_vis boolean;
BEGIN
  IF current_setting('core.demo_maintenance_token', true) = OLD.account_id
     AND OLD.account_id = 'ACCT-DEMO' THEN
    RETURN CASE WHEN TG_OP = 'DELETE' THEN OLD ELSE NEW END;
  END IF;
  -- RETENTION prune: a DELETE armed by the gate for THIS row's own account is the one controlled erase.
  IF TG_OP = 'DELETE'
     AND current_setting('core.retention_token', true) IS NOT NULL
     AND current_setting('core.retention_token', true) = OLD.account_id THEN
    RETURN OLD;
  END IF;
  -- RIGHT-TO-DELETION (account erasure): the second named DELETE exception. A DELETE armed by the gate
  -- (mark_account_erasure) for THIS row's OWN account is the controlled hard-erase of an offboarding tenant.
  -- Same account-pinned, un-forgeable shape as retention — it can never reach across tenants, so every OTHER
  -- tenant's ledger stays append-only/immutable DURING the erase (no trigger-drop). A plain DELETE that
  -- did not arm this token is STILL refused below.
  IF TG_OP = 'DELETE'
     AND current_setting('core.account_erasure_token', true) IS NOT NULL
     AND current_setting('core.account_erasure_token', true) = OLD.account_id THEN
    RETURN OLD;
  END IF;
  IF TG_OP = 'DELETE' THEN
    RAISE EXCEPTION 'append-only: a recorded fact in % is permanent; it cannot be deleted.', TG_TABLE_NAME
      USING ERRCODE = '42501';
  END IF;
  v_old := to_jsonb(OLD) - 'visibility';
  v_new := to_jsonb(NEW) - 'visibility';
  v_has_vis := (to_jsonb(NEW) ? 'visibility');
  IF v_old IS DISTINCT FROM v_new OR NOT v_has_vis THEN
    RAISE EXCEPTION 'append-only: a recorded fact in % is permanent; only its visibility may change.', TG_TABLE_NAME
      USING ERRCODE = '42501';
  END IF;
  RETURN NEW;
END $$;
ALTER FUNCTION core.assert_append_only() OWNER TO veripsa_migrator;

-- assert_no_truncate: the append-only ROW triggers above are BEFORE DELETE OR UPDATE … FOR EACH ROW, and a
-- row trigger NEVER fires for TRUNCATE (a statement-level op that skips row triggers). So without this guard a
-- single `TRUNCATE core.event` / `core.statement` would WIPE the entire immutable ledger in one statement —
-- zero rows touched by the append-only trigger, zero the append-only guarantee — defeating "a recorded fact is permanent".
-- This is a STATEMENT-level BEFORE TRUNCATE trigger on the immutable streams that ALWAYS raises: the ledger is
-- permanent, and TRUNCATE is merely an un-evidenced bulk DELETE, so it is never a legitimate operation on these
-- tables. It is NOT a substitute for the legit erase paths: retention prune and account-erasure are row-DELETEs
-- routed through assert_append_only / assert_statement_immutable (their own account-pinned, un-forgeable tokens),
-- never TRUNCATE — so blocking TRUNCATE removes a tamper hole without touching any sanctioned deletion.
-- REACH: TRUNCATE already requires the TRUNCATE table privilege, held ONLY by the table OWNER (veripsa_migrator;
-- no tenant role has it) and a superuser. The migrator is NOT a superuser and cannot set session_replication_role
-- to 'replica', so this trigger stops the owner too; the only residual is a real superuser/DBA (trusted at that
-- level, and a superuser can drop any guard regardless). So this shuts the last non-superuser wipe path.
CREATE OR REPLACE FUNCTION core.assert_no_truncate() RETURNS trigger
    LANGUAGE plpgsql SET search_path TO 'core','pg_catalog' AS $$
BEGIN
  RAISE EXCEPTION 'append-only: % is a permanent ledger; TRUNCATE is not allowed (use the account-pinned gate erase for a sanctioned, gated (account-pinned) deletion).', TG_TABLE_NAME
    USING ERRCODE = '42501';
END $$;
ALTER FUNCTION core.assert_no_truncate() OWNER TO veripsa_migrator;

-- ────────────────────────────────────────────────────────────────────────────────────────────
-- IDENTITY / ACCESS SUBSTRATE. account (the tenant) · agent (the actor) · credential (the connection
-- role → identity map the gate trusts) · grant (a direct delegation — agent may act on another
-- account's repo/path with a scope; NOT a generic rule engine).
-- ────────────────────────────────────────────────────────────────────────────────────────────

-- account: one customer / tenant. The isolation key for every other table. Content-free.
CREATE TABLE IF NOT EXISTS core.account (
    account_id text PRIMARY KEY,
    display_name text,
    plan text DEFAULT 'free' NOT NULL,
    account_state text DEFAULT 'active' NOT NULL,
    created_at timestamptz DEFAULT now() NOT NULL,
    CONSTRAINT account_display_len CHECK (display_name IS NULL OR length(display_name) <= 200),
    CONSTRAINT account_plan_len CHECK (length(plan) <= 64),
    CONSTRAINT account_state_check CHECK (account_state = ANY (ARRAY['active','closed']))
);

-- agent: the actor (a vehicle in the traffic-control metaphor). Replaceable (the doer); what persists
-- and is inherited is the WORK + the RECORD, never the agent (the thesis). A traffic cop catalogs a
-- vehicle by AT MOST four things — keep it that lean (per-product/per-model metadata is column-level 乱立):
--   * default_model = the MAKER (which model it runs, e.g. claude-opus-4-8; a push may attribute a
--                     specific model per-action via event.model),
--   * agent_id      = the PLATE (the unique number),
--   * operator      = the RIDER (the human operating it — who's driving; content-free, nullable for an
--                     unattended/autonomous session),
--   * account_id    = the AFFILIATION (the org the rider belongs to).
-- agent_kind/state are the only operational extras. Nothing more is needed for traffic control.
CREATE TABLE IF NOT EXISTS core.agent (
    agent_id text PRIMARY KEY,
    account_id text NOT NULL REFERENCES core.account(account_id),
    display_name text,
    agent_kind text DEFAULT 'ai' NOT NULL,
    default_model text,                 -- the MAKER
    operator text,                      -- the RIDER (the human behind it); content-free, nullable
    agent_state text DEFAULT 'active' NOT NULL,
    created_at timestamptz DEFAULT now() NOT NULL,
    CONSTRAINT agent_display_len CHECK (display_name IS NULL OR length(display_name) <= 200),
    CONSTRAINT agent_kind_check CHECK (agent_kind = ANY (ARRAY['ai','human'])),
    CONSTRAINT agent_model_len CHECK (default_model IS NULL OR length(default_model) <= 128),
    CONSTRAINT agent_operator_len CHECK (operator IS NULL OR length(operator) <= 200),
    CONSTRAINT agent_state_check CHECK (agent_state = ANY (ARRAY['active','stopped','quarantined']))
);
CREATE INDEX CONCURRENTLY IF NOT EXISTS agent_by_account ON core.agent (account_id);

-- credential: the connection ROLE → (agent, account) the gate binds identity to. resolve_session_identity
-- reads this by session_user; the buyer connects as a per-agent role and the gate pins its account by RLS.
CREATE TABLE IF NOT EXISTS core.credential (
    role_name text PRIMARY KEY,
    agent_id text NOT NULL REFERENCES core.agent(agent_id),
    account_id text NOT NULL REFERENCES core.account(account_id),
    credential_state text DEFAULT 'active' NOT NULL,
    created_at timestamptz DEFAULT now() NOT NULL,
    CONSTRAINT credential_state_check CHECK (credential_state = ANY (ARRAY['active','revoked']))
);

-- grant: a DIRECT delegation — agent (grantee) may act on grantor account's repo (optionally a path
-- prefix) with a content-free scope, until it expires or is revoked. No generic rule engine.
CREATE TABLE IF NOT EXISTS core.grant (
    grant_id text NOT NULL,
    grantor_account text NOT NULL REFERENCES core.account(account_id),
    grantee_agent text NOT NULL REFERENCES core.agent(agent_id),
    repo text DEFAULT '' NOT NULL,
    path_prefix text DEFAULT '' NOT NULL,
    scope text[] NOT NULL,
    grant_state text DEFAULT 'active' NOT NULL,
    granted_at timestamptz DEFAULT now() NOT NULL,
    expires_at timestamptz,
    CONSTRAINT grant_pkey PRIMARY KEY (grantor_account, grant_id),
    CONSTRAINT grant_repo_len CHECK (length(repo) <= 512),
    CONSTRAINT grant_prefix_len CHECK (length(path_prefix) <= 1024),
    CONSTRAINT grant_state_check CHECK (grant_state = ANY (ARRAY['active','revoked'])),
    CONSTRAINT grant_scope_ok CHECK (array_length(scope, 1) >= 1)
);
CREATE INDEX CONCURRENTLY IF NOT EXISTS grant_by_grantee ON core.grant (grantee_agent) WHERE grant_state = 'active';

DO $$
DECLARE t text;
BEGIN
  FOREACH t IN ARRAY ARRAY['account','agent','credential','grant'] LOOP
    IF EXISTS (
      SELECT 1
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
       WHERE n.nspname = 'core'
         AND c.relname = t
         AND c.relowner <> 'veripsa_migrator'::regrole
    ) THEN
      EXECUTE format('ALTER TABLE core.%I OWNER TO veripsa_migrator', t);
    END IF;
  END LOOP;
END $$;

-- ── RLS: account_id is the isolation key. Even the SECURITY DEFINER owner sees zero rows unless the gate
--    pins core.current_account for the session. (account itself is pinned by its own id.) ──────────────
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_class WHERE oid = 'core.account'::regclass AND relrowsecurity) THEN
    ALTER TABLE core.account ENABLE ROW LEVEL SECURITY;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_class WHERE oid = 'core.account'::regclass AND relforcerowsecurity) THEN
    ALTER TABLE ONLY core.account FORCE ROW LEVEL SECURITY;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_class WHERE oid = 'core.agent'::regclass AND relrowsecurity) THEN
    ALTER TABLE core.agent ENABLE ROW LEVEL SECURITY;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_class WHERE oid = 'core.agent'::regclass AND relforcerowsecurity) THEN
    ALTER TABLE ONLY core.agent FORCE ROW LEVEL SECURITY;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_class WHERE oid = 'core.grant'::regclass AND relrowsecurity) THEN
    ALTER TABLE core.grant ENABLE ROW LEVEL SECURITY;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_class WHERE oid = 'core.grant'::regclass AND relforcerowsecurity) THEN
    ALTER TABLE ONLY core.grant FORCE ROW LEVEL SECURITY;
  END IF;
END $$;
-- credential is the BOOTSTRAP identity table: resolve_session_identity (SECURITY DEFINER, owner) reads it
-- BEFORE current_account is set (it is what DERIVES the account), so it must NOT be FORCE (the owner must
-- bypass). ENABLE with NO policy = denied to every non-owner role (no GRANT either) AND owner-bypass for
-- the resolver, which itself scopes to role_name = session_user (you only ever resolve your OWN role).
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_class WHERE oid = 'core.credential'::regclass AND relrowsecurity) THEN
    ALTER TABLE core.credential ENABLE ROW LEVEL SECURITY;
  END IF;
END $$;

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_policy WHERE polrelid = 'core.account'::regclass AND polname = 'tenant_isolation') THEN
    CREATE POLICY tenant_isolation ON core.account USING (account_id = current_setting('core.current_account', true))
      WITH CHECK (account_id = current_setting('core.current_account', true));
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_policy WHERE polrelid = 'core.agent'::regclass AND polname = 'tenant_isolation') THEN
    CREATE POLICY tenant_isolation ON core.agent USING (account_id = current_setting('core.current_account', true))
      WITH CHECK (account_id = current_setting('core.current_account', true));
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_policy WHERE polrelid = 'core.grant'::regclass AND polname = 'tenant_isolation') THEN
    CREATE POLICY tenant_isolation ON core.grant USING (grantor_account = current_setting('core.current_account', true))
      WITH CHECK (grantor_account = current_setting('core.current_account', true));
  END IF;
END $$;
-- RIGHT-TO-DELETION on the DELEGATION graph (the GRANTEE side). grant.tenant_isolation keys on grantor_account,
-- so it admits ONLY a tenant's OUTBOUND grants (the rows IT granted). But grant is CROSS-ACCOUNT: grantee_agent
-- REFERENCES core.agent RESTRICT, so a grant ANOTHER tenant issued whose grantee is THIS account's agent pins
-- this account's agent rows. erase_account_with_authority deletes the grantor-side rows by grantor_account, but
-- those INBOUND rows live in the OTHER tenant and stay invisible under tenant_isolation → the agent DELETE then
-- hits the RESTRICT FK → foreign_key_violation → the whole erase ABORTS → the tenant can never be erased
-- (GDPR/CCPA failure). This permissive DELETE policy (OR'd with tenant_isolation) mirrors follow_erasable
-- (70_social.sql) EXACTLY: it admits a row when the erasure token (armed ONLY by erase_account_with_authority
-- for the account being erased) names the account that OWNS the grantee agent — so a full account hard-delete
-- also removes the grants OTHER tenants hold ON this account's agents, clearing the FK that would otherwise
-- block the erase, WITHOUT unsetting RLS or being able to touch any grant that does not name an erased-account
-- agent. DELETE-only; the token is un-forgeable (migrator-armed, account-scoped). Non-erase sessions never arm
-- the token, so it is inert in normal operation. (During the erase current_account = the erased account, so the
-- agent subquery — itself RLS-scoped to current_account — resolves to exactly that account's agents.)
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_policy WHERE polrelid = 'core.grant'::regclass AND polname = 'grant_erasable') THEN
    CREATE POLICY grant_erasable ON core.grant FOR DELETE USING (
      NULLIF(current_setting('core.account_erasure_token', true), '') IS NOT NULL
      AND grantee_agent IN (
        SELECT agent_id FROM core.agent
        WHERE account_id = current_setting('core.account_erasure_token', true))
    );
  END IF;
END $$;
-- READ-VISIBILITY for the erase DELETE (the bug #188 left). Postgres applies SELECT/ALL policies to find the rows a
-- DELETE will lock; a FOR DELETE policy is NOT consulted during that scan. grant.tenant_isolation keys on
-- grantor_account, so the INBOUND grant another tenant issued ON this account's agent (grantor_account<>erased) is
-- invisible to the scan → the grantee-side DELETE in erase_account_with_authority matched 0 rows → the RESTRICT FK
-- grant_grantee_agent_fkey still fired on the agent delete and ABORTED the whole erase (the tenant could never be
-- erased — GDPR/CCPA failure). This paired FOR SELECT policy makes EXACTLY the token-scoped grantee rows READABLE
-- during the erase so the DELETE can lock+remove them. Same un-forgeable migrator-armed account-scoped token +
-- identical predicate as grant_erasable, so it admits nothing the DELETE policy would not and is inert outside an erase.
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_policy WHERE polrelid = 'core.grant'::regclass AND polname = 'grant_erasable_read') THEN
    CREATE POLICY grant_erasable_read ON core.grant FOR SELECT USING (
      NULLIF(current_setting('core.account_erasure_token', true), '') IS NOT NULL
      AND grantee_agent IN (
        SELECT agent_id FROM core.agent
        WHERE account_id = current_setting('core.account_erasure_token', true))
    );
  END IF;
END $$;

-- ── identity resolution: the connection role → (agent, account). The buyer never passes a key; the
--    role IS the identity, and it must map to exactly one active credential (else the self-claim is
--    untrusted). credential is read WITHOUT RLS here via SECURITY DEFINER + a bypass account pin. ──────
CREATE OR REPLACE FUNCTION core.resolve_session_identity(OUT o_agent text, OUT o_account text) RETURNS record
    LANGUAGE plpgsql SECURITY DEFINER SET search_path TO 'core','pg_catalog' AS $$
DECLARE v_count integer; v_inst_account text;
BEGIN
  -- MULTI-TENANT routing FIRST (must be reachable WITHOUT a per-account credential row): veripsa_app is the
  -- SERVICE identity — one DB role acting across ALL tenants. A real hosted deploy creates the role (db/roles.sql)
  -- but NEVER a core.credential row for it (provision_seat(...,'veripsa_app') is local/dogfood-only — the RUNBOOK
  -- + render.yaml forbid it in prod). So per webhook event the server pins core.installation_account (via
  -- enter_installation_with_authority, which looked up / lazily provisioned THIS installation's OWN account), and
  -- THAT — not a credential — is the App's account for the event. Routing here lands the whole event (surfaces +
  -- gate writes) in that account, and account-RLS walls it off from every other tenant. Reached BEFORE the
  -- credential requirement so a clean prod deploy resolves instead of 42501-ing on every event (the deploy-blocker).
  --
  -- TENANT SAFETY: o_account is taken ONLY from core.installation_account, which is session-pinned by
  -- enter_installation_with_authority to exactly one installation's own account — never a value the caller passes,
  -- never cross-tenant. ONLY session_user='veripsa_app' honors it; any other role falls through to its own
  -- credential account below, so the moat (forgery block + per-account RLS) is identical for customer/seat roles.
  --
  -- DEFENSE-IN-DEPTH (slam the source): core.installation_account is a SESSION GUC — a stray/buggy/injected raw
  -- `SET core.installation_account='ACCT-X'` on the App connection would otherwise steer the whole event into an
  -- ARBITRARY (even non-existent) tenant, because this routing trusted the GUC string blindly. The trusted route
  -- (enter_installation_with_authority) always WRITES a core.installation_account ROW before pinning the GUC, so a
  -- GENUINELY-routed value is provably present in that table. We therefore honor the pin ONLY when it corresponds
  -- to a real routed account; a value with no routing row is a FORGERY/MISTAKE → ignore the pin and fall through
  -- to the credential path (which, for veripsa_app in a clean prod deploy with no credential, fails CLOSED with a
  -- 42501 — never a silent cross-tenant or phantom-tenant write). The lookup is the no-RLS routing table read,
  -- safe inside this SECURITY DEFINER (migrator) fn. (This makes the invariant the comment above ASSERTS
  -- structurally enforced, not merely assumed.)
  IF session_user = 'veripsa_app' THEN
    v_inst_account := NULLIF(current_setting('core.installation_account', true), '');
    IF v_inst_account IS NOT NULL
       AND EXISTS (SELECT 1 FROM core.installation_account WHERE account_id = v_inst_account) THEN
      o_account := v_inst_account;
      -- the App is the actor for service-level facts (push/landed records); act_for_claim attributes the CLAIM to
      -- the real PR author instead (agent 'GH-<login>'), so this service agent id never mislabels a reservation.
      -- Prefer the App's own provisioned agent if a credential exists (local/dogfood with an installation pinned);
      -- else the stable service agent id 'veripsa_app' (no core.agent FK on event/claim, so this is safe).
      SELECT agent_id INTO o_agent FROM core.credential
        WHERE role_name = session_user AND credential_state = 'active';
      o_agent := COALESCE(o_agent, 'veripsa_app');
      RETURN;
    END IF;
    -- no VALID installation pinned (the local/dogfood path, OR a GUC value with no routing row = forged/stray)
    -- → fall through to the credential lookup below, which resolves veripsa_app to its provisioned own account
    -- (e.g. ACCT-DEMO). A clean PROD deploy with no veripsa_app credential then fails CLOSED here (42501) rather
    -- than writing into a forged tenant. A clean PROD deploy never lands here on the happy path because the
    -- server pins a REAL routed installation for every webhook event before any gate/surface call.
  END IF;

  -- credential-based identity (genuine seats; and veripsa_app in local/dogfood mode with no installation pinned).
  -- read credential as the owner, unfiltered by RLS. (credential rows are keyed by role_name = session_user.)
  SELECT count(*) INTO v_count FROM core.credential
    WHERE role_name = session_user AND credential_state = 'active';
  IF v_count = 0 THEN
    RAISE EXCEPTION 'authgate: connecting role % has no active credential (not provisioned)', session_user
      USING ERRCODE = '42501';
  END IF;
  IF v_count <> 1 THEN
    RAISE EXCEPTION 'authgate: connecting role % maps to % credentials (not unique)', session_user, v_count
      USING ERRCODE = '42501';
  END IF;
  SELECT agent_id, account_id INTO o_agent, o_account FROM core.credential
    WHERE role_name = session_user AND credential_state = 'active';
END $$;
ALTER FUNCTION core.resolve_session_identity(OUT text, OUT text) OWNER TO veripsa_migrator;

-- establish_session_write_context: resolve identity AND pin core.current_account for the txn, so RLS
-- WITH CHECK admits the gate's writes into the caller's own account. Returns (agent, account).
CREATE OR REPLACE FUNCTION core.establish_session_write_context(OUT o_agent text, OUT o_account text) RETURNS record
    LANGUAGE plpgsql SECURITY DEFINER SET search_path TO 'core','pg_catalog' AS $$
BEGIN
  SELECT agent, account INTO o_agent, o_account FROM core.resolve_session_identity() AS r(agent, account);
  PERFORM set_config('core.current_account', o_account, true);
END $$;
ALTER FUNCTION core.establish_session_write_context(OUT text, OUT text) OWNER TO veripsa_migrator;

-- Forgery block on credential rows is NOT applied (provisioning writes them as the migrator out-of-band,
-- like the role bootstrap). account/agent/grant are written through their gate fns (Phase 1b) which arm
-- the token; their forgery triggers are added with those gates. RLS already walls cross-account reads.

-- ============================================================================================
