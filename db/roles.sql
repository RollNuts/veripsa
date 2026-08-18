-- Veripsa — database roles bootstrap (idempotent)
--
-- Veripsa's trust model lives in the database: every write passes through the gate,
-- and *which* Postgres role a connection uses is what the gate binds identity and tenant to.
--
-- THE MODEL (two groups — don't confuse them):
--
--   A. PRIVILEGE CLASSES (the real, product roles — a connection authenticates as ONE of these):
--        veripsa_app        — THE PRODUCT. The hosted GitHub App's service identity, and the ONLY role the
--                             live App ever connects as. It = a writer (below) + the act_for delegation gate,
--                             so it reserves/lands on behalf of each PR AUTHOR inside that author's tenant.
--        veripsa_writer     — the base "may write through the gate" capability. veripsa_app inherits it; so
--                             would a (deferred) web-console seat. "writer" is NOT a person and NOT separate
--                             from the App — it is the capability the App is built on.
--        veripsa_reader     — tenant-scoped read-only product surfaces. No writes and no cross-tenant DR export.
--        veripsa_migrator   — owns the schema + objects; runs migrations. Admin, never a buyer.
--        veripsa_owner      — object-ownership / privileged maintenance.
--        veripsa_backup     — dedicated cross-tenant DR exporter. NO table privileges and NO live-App
--                             inheritance; its only schema privilege is USAGE and its only callable surface is
--                             core.export_durable_rows_with_authority(text). LOGIN/credential are operator-set,
--                             rotated out of band, and never reused by restore (restore uses the migrator).
--        example_platform_reader — the SEPARATE example platform service (the dashboard) connects as this to read
--                             content-free installation/platform state only. LEAST PRIVILEGE: its ENTIRE reachable
--                             surface is USAGE on schema core + EXECUTE on the intended platform functions:
--                             core.effect_for_installation(text), core.list_installation_ids(),
--                             core.installation_is_live(text), core.repos_for_installation(text),
--                             core.repo_insights_for_installation(text,text,int),
--                             core.file_insights_for_installation(text,text,text), and
--                             core.now_for_installation(text). NO write, NO graph table, NO broad SELECT. The moat
--                             (code graph / file contents / writes) is unreachable by construction.
--        veripsa_billing    — the SEPARATE web platform's future Marketplace/entitlement handler connects as
--                             this to flip ONE customer's Core plan after it has VERIFIED the billing authority and
--                             resolved (org/install → gh_account_id, plan). LEAST PRIVILEGE — the MIRROR of example_platform_reader
--                             on the WRITE side: its ENTIRE reachable surface is core.set_account_plan_with_authority(text,text,timestamptz)
--                             + its installation-keyed sibling (USAGE on schema core + EXECUTE on those setters) — NO table access (no SELECT/INSERT/
--                             UPDATE/DELETE on any core table), NO other *_with_authority fn (it cannot transfer a repo,
--                             set a plan-file limit, ingest, land, …). The setter is SECURITY DEFINER (runs as the
--                             migrator owner, pins the account + arms the governed write internally), so EXECUTE on it
--                             is the ONLY privilege the handler needs to set a plan — and the most it can ever do.
--
--   B. LOCAL FIXTURES (NOT product roles — only the gate tests + the local dogfood board connect as these;
--      a real deployment never creates them). They exist so the demo board can show real multi-agent
--      contention. Named veripsa_demo_* / veripsa_acme_* so they can never be mistaken for a privilege class:
--        veripsa_demo_agent, _agent2, _agent3  — three demo agents in ACCT-DEMO (one is a vanished seat whose
--                             held lane can be inherited/broken). Each inherits veripsa_writer.
--        veripsa_acme_agent — a demo agent in a SECOND account (ACCT-ACME), to prove cross-tenant isolation.
--        veripsa_demo_steward — the demo account's steward seat: reads + breaks lanes, never edits files.
--
-- Run this BEFORE db/schema.sql, as a superuser (or a role with CREATEROLE):
--     psql "$ADMIN_DSN" -f db/roles.sql
--     createdb veripsa -O veripsa_migrator
--     psql "postgresql://veripsa_migrator@localhost/veripsa" -f db/schema.sql
--
-- Roles are created NOLOGIN by default (no way in until you say so). For each role a connection will actually
-- use, grant LOGIN + a password yourself. Never hand a buyer the migrator/owner role.

-- CONCURRENCY: roles are CLUSTER-GLOBAL, so several bootstraps running at once (parallel CI shards / several
-- agents each running run_gates) all touch the SAME pg_authid rows → "tuple concurrently updated" on the
-- CREATE/ALTER/GRANT below. Serialize the whole role section on a transaction-scoped advisory lock (a fixed
-- arbitrary key) so concurrent bootstraps run it one-at-a-time; idempotent + transactional, so re-runs are safe.
BEGIN;
SELECT pg_advisory_xact_lock(7261197);  -- "veripsa-roles" — any fixed key, shared by every concurrent bootstrap

DO $$
DECLARE
  r text;
BEGIN
  FOREACH r IN ARRAY ARRAY[
    -- A. privilege classes
    'veripsa_migrator',     -- owns the schema + objects; runs migrations (admin, not a buyer)
    'veripsa_owner',        -- object-ownership / privileged maintenance role
    'veripsa_backup',       -- dedicated cross-tenant DR export principal (only the export gate; no table grants)
    'veripsa_reader',       -- tenant-scoped read-only product surfaces (never the cross-tenant DR export)
    'veripsa_writer',       -- base write-through-the-gate capability (the App inherits this)
    'veripsa_app',          -- THE PRODUCT: the hosted GitHub App's service identity (writer + act_for delegation)
    'example_platform_reader', -- the SEPARATE platform/dashboard service: reads a tenant's EFFECT only (least privilege)
    'veripsa_billing',      -- the SEPARATE web platform's Marketplace/entitlement seam: sets ONE customer's plan only (least privilege, WRITE side)
    -- B. local fixtures (gate tests + dogfood board only; a real deployment never creates these)
    'veripsa_demo_agent',   -- demo agent in ACCT-DEMO
    'veripsa_demo_agent2',  -- a 2nd demo agent in ACCT-DEMO → the board shows real same-lane contention
    'veripsa_demo_agent3',  -- a 3rd demo agent in ACCT-DEMO: a stopped/vanished seat (lane inherited or broken)
    'veripsa_acme_agent',   -- a demo agent in a SECOND account (ACCT-ACME) → proves cross-tenant isolation
    'veripsa_demo_steward'  -- the demo account's steward seat: reads + breaks lanes, never edits files
  ]
  LOOP
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = r) THEN
      EXECUTE format('CREATE ROLE %I NOLOGIN', r);
    END IF;
  END LOOP;
END $$;

-- the base writer capability flows to the App (the product) and to every demo-fixture agent
GRANT veripsa_writer TO veripsa_app;          -- the App = a writer + (schema-granted) act_for delegation
GRANT veripsa_writer TO veripsa_demo_agent;
GRANT veripsa_writer TO veripsa_demo_agent2;
GRANT veripsa_writer TO veripsa_demo_agent3;
GRANT veripsa_writer TO veripsa_acme_agent;

-- LOGIN for the LOCAL FIXTURES only. The gate tests + the dogfood board connect AS these directly (peer auth
-- on localhost, no password), so they need LOGIN — they are local-only (a real deployment never creates them).
-- The privilege CLASSES (veripsa_writer/reader/owner) stay NOLOGIN: they are capabilities,
-- reached by inheritance, never connected-as. veripsa_app + veripsa_migrator + veripsa_backup +
-- example_platform_reader + veripsa_billing are granted LOGIN + a password by the operator at deploy time
-- (the header note), not here.
-- (example_platform_reader and veripsa_billing ARE each connected-as by a separate service — the dashboard reads,
-- the web's Marketplace/entitlement handler sets a plan — but with a password set OUT-OF-BAND on prod, never here, so the
-- role ships locked; the gate ALTERs it LOGIN in its own ephemeral DB to exercise the deny/allow surface.)
-- Idempotent.
ALTER ROLE veripsa_demo_agent    LOGIN;
ALTER ROLE veripsa_demo_agent2   LOGIN;
ALTER ROLE veripsa_demo_agent3   LOGIN;
ALTER ROLE veripsa_acme_agent    LOGIN;
ALTER ROLE veripsa_demo_steward  LOGIN;

COMMIT;  -- releases the advisory lock; the next concurrent bootstrap now runs the (idempotent) role section
