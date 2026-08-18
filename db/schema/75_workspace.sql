-- PHASE 4c — WORKSPACE / CROSS-TENANT CONSENT (the cross-repo MOAT-CRITICAL layer, SHADOW / default OFF).
-- ============================================================================================================
-- WHAT: the route-tier cross-repo substrate (core._cross_repo_adjacency, 70_social.sql) couples a producer file
-- in repo A to a consumer file in repo B through a shared, repo-stable CONTRACT KEY (route::/api_operation::/…).
-- It is SAME-ACCOUNT-only: both repos are read under ONE p_account, so it can never cross a tenant boundary. THIS
-- file is the safe bridge that lets the SAME signal cross between repos owned by DIFFERENT tenants — and ONLY
-- when BOTH owners have explicitly consented. It is THE highest-stakes surface in the schema: a sloppy
-- cross-tenant read leaks repo B's graph to repo A and kills the content-free / FORCE-RLS moat.
--
-- THE SAFETY MODEL (four load-bearing distinctions — every one is enforced below, then RED-TEAMED by the gate):
--   1. CONSENT-GATED, NOT GUC-gated. The within-account kill switch (veripsa.cross_repo) is a GUC — fine for the
--      same-owner case (a tenant can only ever pin its OWN account). It is NOT sufficient across tenants: a GUC
--      is forgeable by whoever holds the session. So the cross-TENANT read does NOT trust any GUC for the
--      authorization decision — it VERIFIES a bilateral consent FACT (two accepted workspace_member rows) FIRST,
--      reading those rows owner-context (SECURITY DEFINER), and only THEN runs the relaxed adjacency. A forged
--      `SET veripsa.cross_repo=1` (or any other GUC) gains nothing: with no accepted rows, the read returns ∅.
--   2. BILATERAL. A link between repo A (owner X) and repo B (owner Y) is ACTIVE only when X AND Y each hold a
--      workspace_member row with consent_state='accepted' for the SAME workspace, each naming THEIR OWN repo.
--      One-sided consent (only X accepted) → ZERO cross-read. (Mirrors the follow/standing mutual shape +
--      core.grant's bilateral delegation discipline.)
--   3. CONTENT-FREE. The cross-tenant read returns ONLY: the shared contract KEYS, a CONSUMER-side repo NAME, and
--      COUNTS. NEVER repo B's file paths, node/edge bodies, or any slice of B's graph. (A path/node/edge of B is
--      exactly the leak the moat forbids — even one path crossing is a breach.) The same posture as 95_owner.sql
--      (ids+counts only) and the moat-no-counts discipline (no body ever crosses).
--   4. RLS-SAFE / no isolation relaxation. We do NOT touch tenant_isolation, we do NOT drop FORCE RLS, and we do
--      NOT pin a SECOND account's current_account GUC to read across (that would be a same-process cross-tenant
--      pin — the very thing the moat forbids). Instead the consent fn reads each side under its OWN account pin in
--      turn (exactly like owner_cost_surface visits each tenant's wall), and the cross read joins the two
--      already-extracted, content-free key/path sets in memory — never under a shared cross-tenant pin.
--
-- LOCK (the same as 95_owner.sql's deliberate cross-tenant read): every cross-account fn here is SECURITY DEFINER
-- (migrator owner), REVOKEd from PUBLIC + every tenant/seat role, GRANTed ONLY to veripsa_app. A buyer SEAT
-- (veripsa_writer / veripsa_demo_*) calling the cross-tenant read gets permission denied — proven by the gate.
--
-- QUOTA: a workspace stores NO graph — it is a JOIN-AT-QUERY-TIME view over the two tenants' own graphs. So
-- quota stays per-account (the membership rows are negligible; no graph is duplicated into a shared store).
--
-- SHADOW: nothing here is wired into a customer-facing verdict yet. The consent fns + the cross read are reached
-- only by the SHADOW runner / the red-team gate. A future consent UI and the SHADOW→live decision
-- are the remaining steps before this is customer-facing.
--
-- PROD-APPLY (contention-free): the two tables are NEW + FK-FREE (account_id is a bare text key, NOT a FK to
-- core.account — like core.policy / core.installation_account — so CREATE TABLE takes no lock on a hot parent),
-- and every function is CREATE OR REPLACE (no table lock). So this whole file applies on a busy App without an
-- idle window (the FUNCTION-ONLY + new-standalone-table discipline from the RUNBOOK).
-- ============================================================================================================

-- ── workspace: a named cross-repo collaboration space. created_by_account is the tenant that opened it (the
--    initiator), but membership is what GRANTS access — being the creator confers nothing on its own. FK-FREE
--    (created_by_account is a bare text key, like core.policy.account_id) so this stands up contention-free.
CREATE TABLE IF NOT EXISTS core.workspace (
    workspace_id text NOT NULL,
    created_by_account text NOT NULL,
    state text DEFAULT 'active' NOT NULL,
    created_at timestamptz DEFAULT now() NOT NULL,
    CONSTRAINT workspace_pkey PRIMARY KEY (workspace_id),
    CONSTRAINT workspace_state_check CHECK (state = ANY (ARRAY['active','closed'])),
    CONSTRAINT workspace_creator_len CHECK (length(created_by_account) BETWEEN 1 AND 128)
);

-- ── workspace_member: an account opts a SPECIFIC repo of its own into a workspace, with a consent_state. THE
--    bilateral-consent substrate: an A↔B link is ACTIVE only when BOTH accounts hold an 'accepted' row for the
--    SAME workspace (the mutual-consent fn below requires both). Each row is OWNED by account_id — RLS walls each
--    owner to ONLY its own membership rows (an owner can never even SEE the other side's membership, let alone
--    forge it). FK-FREE (account_id is a bare text key) for the same contention-free-apply reason as workspace.
--    NOTE consent_state default 'pending': opting a repo in is an INVITATION; it is not consent until the owner
--    flips it to 'accepted' (so an initiator adding the OTHER side cannot fabricate that side's consent — and in
--    any case RLS forbids writing another account's row at all; default-pending is defense-in-depth).
CREATE TABLE IF NOT EXISTS core.workspace_member (
    workspace_id text NOT NULL,
    account_id text NOT NULL,
    repo text NOT NULL,
    branch text DEFAULT 'main' NOT NULL,
    consent_state text DEFAULT 'pending' NOT NULL,
    consented_at timestamptz,
    joined_at timestamptz DEFAULT now() NOT NULL,
    CONSTRAINT workspace_member_pkey PRIMARY KEY (workspace_id, account_id, repo),
    CONSTRAINT workspace_member_consent_check CHECK (consent_state = ANY (ARRAY['pending','accepted','revoked'])),
    CONSTRAINT workspace_member_repo_len CHECK (length(repo) BETWEEN 1 AND 512),
    CONSTRAINT workspace_member_branch_len CHECK (length(branch) BETWEEN 1 AND 512),
    CONSTRAINT workspace_member_account_len CHECK (length(account_id) BETWEEN 1 AND 128)
);
-- index the "who consented into this workspace" lookup the mutual-consent fn runs (owner-context, per-workspace).
CREATE INDEX CONCURRENTLY IF NOT EXISTS workspace_member_by_ws ON core.workspace_member (workspace_id, consent_state);

-- ── RLS: account_id (workspace_member) / created_by_account (workspace) is the isolation key. FORCE ROW LEVEL
--    SECURITY so even the SECURITY DEFINER owner sees zero rows unless a pin is set (the moat baseline — proven
--    by the 2-tenant red-team gate: a migrator with no pin, or pinned to A, sees ZERO of B's membership rows).
DO $$
DECLARE t text;
BEGIN
  FOREACH t IN ARRAY ARRAY['workspace','workspace_member'] LOOP
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
    IF NOT EXISTS (
      SELECT 1 FROM pg_class c
       WHERE c.oid = format('core.%I', t)::regclass
         AND c.relrowsecurity
    ) THEN
      EXECUTE format('ALTER TABLE core.%I ENABLE ROW LEVEL SECURITY', t);
    END IF;
    IF NOT EXISTS (
      SELECT 1 FROM pg_class c
       WHERE c.oid = format('core.%I', t)::regclass
         AND c.relforcerowsecurity
    ) THEN
      EXECUTE format('ALTER TABLE ONLY core.%I FORCE ROW LEVEL SECURITY', t);
    END IF;
  END LOOP;
END $$;

-- workspace: a tenant manages ONLY the workspaces it created. (Visibility of a workspace it was INVITED into is
-- conveyed by its OWN workspace_member row, which it can read; the workspace row itself is the initiator's.)
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_policy WHERE polrelid = 'core.workspace'::regclass AND polname = 'tenant_isolation') THEN
    CREATE POLICY tenant_isolation ON core.workspace
      USING (created_by_account = current_setting('core.current_account', true))
      WITH CHECK (created_by_account = current_setting('core.current_account', true));
  END IF;
END $$;
-- INVITE-EXISTENCE read: an INVITEE (not the creator) must confirm a workspace before consenting into it, but
-- tenant_isolation hides a workspace it did not create. This permissive SELECT policy (OR'd with tenant_isolation;
-- SELECT-only, so it grants NO write) admits an open workspace BY its unique id. The id is a high-entropy,
-- unguessable token (`WS-`+md5) the creator hands an invitee OUT-OF-BAND — so this reveals only the EXISTENCE of a
-- token you were already given, never the creator account, never any content, never a list (you cannot enumerate;
-- you must already hold the exact id). _workspace_is_open returns a boolean only. The same shape + posture as
-- public_readable (70_social.sql): a tightly-scoped, SELECT-only, content-free cross-account read for an opt-in flow.
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_policy WHERE polrelid = 'core.workspace'::regclass AND polname = 'workspace_invite_read') THEN
    CREATE POLICY workspace_invite_read ON core.workspace FOR SELECT USING (state = 'active');
  END IF;
END $$;

-- workspace_member: a tenant reads + writes ONLY its OWN membership rows (account_id = the pinned account). This
-- is THE wall that makes the consent bilateral and forge-proof: account X can NEVER read account Y's membership
-- row (so it cannot learn whether Y consented except through the owner-context mutual-consent fn, which returns a
-- boolean, never Y's row), and X can NEVER write a row for Y (so X cannot fabricate Y's consent). WITH CHECK on
-- the SAME predicate blocks an INSERT/UPDATE that would land another account's row.
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_policy WHERE polrelid = 'core.workspace_member'::regclass AND polname = 'tenant_isolation') THEN
    CREATE POLICY tenant_isolation ON core.workspace_member
      USING (account_id = current_setting('core.current_account', true))
      WITH CHECK (account_id = current_setting('core.current_account', true));
  END IF;
END $$;

-- RIGHT-TO-DELETION (account erasure) on both tables — the SAME un-forgeable, migrator-armed, account-scoped
-- token shape as follow_erasable / grant_erasable (70_social.sql / 10_substrate.sql). A full account hard-delete
-- must remove BOTH the rows this account owns AND any cross-account references that name it, WITHOUT unsetting RLS.
-- For workspace_member the token names the account whose rows are erased; for workspace it names the creator.
-- These are inert outside an erase (the token is armed only by erase_account_with_authority for the erased
-- account), and DELETE-only — they can never erase a live account's consent. (A FOR DELETE policy alone never
-- SEES an inbound cross-tenant row under tenant_isolation, so each carries a paired FOR SELECT read-visibility
-- policy with the identical predicate — the exact bug #188 fix replicated for completeness of erasure.)
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_policy WHERE polrelid = 'core.workspace_member'::regclass AND polname = 'workspace_member_erasable') THEN
    CREATE POLICY workspace_member_erasable ON core.workspace_member FOR DELETE USING (
      NULLIF(current_setting('core.account_erasure_token', true), '') IS NOT NULL
      AND current_setting('core.account_erasure_token', true) = account_id);
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_policy WHERE polrelid = 'core.workspace_member'::regclass AND polname = 'workspace_member_erasable_read') THEN
    CREATE POLICY workspace_member_erasable_read ON core.workspace_member FOR SELECT USING (
      NULLIF(current_setting('core.account_erasure_token', true), '') IS NOT NULL
      AND current_setting('core.account_erasure_token', true) = account_id);
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_policy WHERE polrelid = 'core.workspace'::regclass AND polname = 'workspace_erasable') THEN
    CREATE POLICY workspace_erasable ON core.workspace FOR DELETE USING (
      NULLIF(current_setting('core.account_erasure_token', true), '') IS NOT NULL
      AND current_setting('core.account_erasure_token', true) = created_by_account);
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_policy WHERE polrelid = 'core.workspace'::regclass AND polname = 'workspace_erasable_read') THEN
    CREATE POLICY workspace_erasable_read ON core.workspace FOR SELECT USING (
      NULLIF(current_setting('core.account_erasure_token', true), '') IS NOT NULL
      AND current_setting('core.account_erasure_token', true) = created_by_account);
  END IF;
END $$;

-- Governed-write forgery block (the moat spine): a direct INSERT/UPDATE is refused unless the gate armed the
-- one-shot token naming THIS table. So the ONLY write path is the consent setters below (record == execution).
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_trigger
     WHERE tgrelid = 'core.workspace'::regclass
       AND tgname = 'trg_governed_workspace'
       AND NOT tgisinternal
  ) THEN
    CREATE TRIGGER trg_governed_workspace BEFORE INSERT OR UPDATE ON core.workspace FOR EACH ROW EXECUTE FUNCTION core.assert_governed_write();
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM pg_trigger
     WHERE tgrelid = 'core.workspace_member'::regclass
       AND tgname = 'trg_governed_workspace_member'
       AND NOT tgisinternal
  ) THEN
    CREATE TRIGGER trg_governed_workspace_member BEFORE INSERT OR UPDATE ON core.workspace_member FOR EACH ROW EXECUTE FUNCTION core.assert_governed_write();
  END IF;
END $$;

-- ============================================================================================================
-- WRITE PATH — the consent setters (governed, RLS-checked, identity from the CONNECTION, never an arg).
-- ============================================================================================================

-- create_workspace_with_authority: the initiator opens a workspace AND opts its OWN first repo in (as 'pending'
-- — see open_repo_for_consent for the accept). Identity (agent+account) is derived from the connecting role via
-- establish_session_write_context (which also pins current_account so RLS WITH CHECK admits the writes), NEVER
-- from an arg — so a tenant can only ever create a workspace owned by ITSELF and seed ITS OWN membership. Returns
-- the new workspace_id. Content-free (a repo name only). The token-armed, RLS-checked write discipline of every
-- other gate fn.
CREATE OR REPLACE FUNCTION core.create_workspace_with_authority(p_repo text, p_branch text DEFAULT 'main')
  RETURNS text LANGUAGE plpgsql SECURITY DEFINER SET search_path TO 'core','pg_catalog' AS $$
DECLARE v_agent text; v_account text; v_ws text;
BEGIN
  IF p_repo IS NULL OR btrim(p_repo) = '' THEN
    RAISE EXCEPTION 'create_workspace: repo must not be empty' USING ERRCODE='22023';
  END IF;
  SELECT agent, account INTO v_agent, v_account FROM core.establish_session_write_context() AS c(agent,account);
  v_ws := 'WS-'||substr(md5(random()::text||clock_timestamp()::text||v_account),1,20);
  PERFORM core.mark_governed_write('workspace');
  INSERT INTO core.workspace(workspace_id, created_by_account, state) VALUES (v_ws, v_account, 'active');
  -- seed the initiator's OWN membership as 'pending' (the initiator still has to ACCEPT — being the creator is
  -- not consent; this keeps the bilateral check honest even for the side that opened the workspace).
  PERFORM core.mark_governed_write('workspace_member');
  INSERT INTO core.workspace_member(workspace_id, account_id, repo, branch, consent_state)
  VALUES (v_ws, v_account, left(p_repo,512), left(COALESCE(NULLIF(btrim(p_branch),''),'main'),512), 'pending')
  ON CONFLICT (workspace_id, account_id, repo) DO NOTHING;
  RETURN v_ws;
END $$;
ALTER FUNCTION core.create_workspace_with_authority(text,text) OWNER TO veripsa_migrator;
REVOKE EXECUTE ON FUNCTION core.create_workspace_with_authority(text,text) FROM PUBLIC;

-- open_repo_for_consent_with_authority: a tenant opts ONE of its OWN repos into an EXISTING workspace AND records
-- consent for it in one call (consent_state='accepted', consented_at=now()). This is the per-account bilateral
-- ACCEPT: account X calls it for repo_A in WS, account Y calls it for repo_B in WS — and ONLY THEN does the
-- mutual-consent fn report the pair ACTIVE. Identity is from the CONNECTION (never an arg) so a caller can only
-- ever consent for ITS OWN repo into the workspace; RLS WITH CHECK (account_id = pinned account) makes a row for
-- any other account impossible. Idempotent upsert on the caller's own row. Content-free (workspace id + repo).
--
-- NOTE this deliberately bundles join+accept: a single human/seat action "share THIS repo into THIS workspace" IS
-- the consent. A separate pending→accept two-step is a UI nicety the platform can layer (write 'pending' then
-- flip), but the substrate's safety does not need it — what matters is that BOTH sides independently accept, each
-- only for their own repo, which this enforces.
CREATE OR REPLACE FUNCTION core.open_repo_for_consent_with_authority(p_workspace text, p_repo text, p_branch text DEFAULT 'main')
  RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path TO 'core','pg_catalog' AS $$
DECLARE v_agent text; v_account text; v_exists boolean;
BEGIN
  IF p_workspace IS NULL OR btrim(p_workspace) = '' THEN
    RAISE EXCEPTION 'open_repo_for_consent: workspace must not be empty' USING ERRCODE='22023';
  END IF;
  IF p_repo IS NULL OR btrim(p_repo) = '' THEN
    RAISE EXCEPTION 'open_repo_for_consent: repo must not be empty' USING ERRCODE='22023';
  END IF;
  SELECT agent, account INTO v_agent, v_account FROM core.establish_session_write_context() AS c(agent,account);
  -- The workspace must EXIST + be active. We read it owner-context (a tenant cannot see a workspace it did not
  -- create under tenant_isolation, but consenting into one it was INVITED to is legitimate), so this existence
  -- check runs through a SECURITY DEFINER helper that bypasses RLS for the existence question ONLY (it returns a
  -- boolean, never the row). A non-existent / closed workspace is rejected so consent can't accrete onto nothing.
  v_exists := core._workspace_is_open(p_workspace);
  IF NOT v_exists THEN
    RAISE EXCEPTION 'open_repo_for_consent: workspace % is not an open workspace', p_workspace USING ERRCODE='22023';
  END IF;
  PERFORM core.mark_governed_write('workspace_member');
  INSERT INTO core.workspace_member(workspace_id, account_id, repo, branch, consent_state, consented_at)
  VALUES (left(p_workspace,128), v_account, left(p_repo,512),
          left(COALESCE(NULLIF(btrim(p_branch),''),'main'),512), 'accepted', now())
  ON CONFLICT (workspace_id, account_id, repo)
    DO UPDATE SET consent_state='accepted', consented_at=now(),
                  branch=EXCLUDED.branch;
END $$;
ALTER FUNCTION core.open_repo_for_consent_with_authority(text,text,text) OWNER TO veripsa_migrator;
REVOKE EXECUTE ON FUNCTION core.open_repo_for_consent_with_authority(text,text,text) FROM PUBLIC;

-- revoke_consent_with_authority: a tenant WITHDRAWS consent for one of its OWN repos in a workspace (sets
-- consent_state='revoked'). The instant EITHER side revokes, the bilateral check fails and the cross-read returns
-- ∅ again — consent is continuously revocable, never a one-way door. Identity from the connection; RLS confines
-- the UPDATE to the caller's own row. Content-free.
CREATE OR REPLACE FUNCTION core.revoke_consent_with_authority(p_workspace text, p_repo text)
  RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path TO 'core','pg_catalog' AS $$
DECLARE v_agent text; v_account text;
BEGIN
  SELECT agent, account INTO v_agent, v_account FROM core.establish_session_write_context() AS c(agent,account);
  PERFORM core.mark_governed_write('workspace_member');
  UPDATE core.workspace_member
     SET consent_state='revoked', consented_at=now()
   WHERE workspace_id=left(p_workspace,128) AND account_id=v_account AND repo=left(p_repo,512);
END $$;
ALTER FUNCTION core.revoke_consent_with_authority(text,text) OWNER TO veripsa_migrator;
REVOKE EXECUTE ON FUNCTION core.revoke_consent_with_authority(text,text) FROM PUBLIC;

-- Grants for the WRITE path: a buyer seat (veripsa_writer, the demo agents inherit it) + the App may open/accept/
-- revoke consent for THEIR OWN account (the identity is from the connection + RLS-walled, so this is safe — a
-- writer can only ever consent for itself). These are NOT the cross-tenant READ (that is veripsa_app-only, below).
GRANT EXECUTE ON FUNCTION core.create_workspace_with_authority(text,text)            TO veripsa_writer, veripsa_app;
GRANT EXECUTE ON FUNCTION core.open_repo_for_consent_with_authority(text,text,text)  TO veripsa_writer, veripsa_app;
GRANT EXECUTE ON FUNCTION core.revoke_consent_with_authority(text,text)              TO veripsa_writer, veripsa_app;

-- ============================================================================================================
-- THE BILATERAL MUTUAL-CONSENT CHECK — owner-context, NEVER a tenant-readable join.
-- ============================================================================================================

-- _workspace_is_open: existence/active check for a workspace, by its unique id. Used by open_repo_for_consent so
-- consent cannot accrete onto a non-existent / closed workspace. workspace wears FORCE RLS keyed by
-- created_by_account, so an owner read sees a row ONLY when pinned to the creator — but an INVITEE (not the
-- creator) legitimately needs to confirm a workspace before consenting into it. The workspace_member_invite_read
-- permissive policy (below) admits exactly an open workspace BY ID for this probe — it reveals nothing but
-- existence (the fn returns a boolean, never the row, never the creator). SECURITY DEFINER owner read; the
-- permissive policy is what makes the existence visible regardless of the caller's pin. REVOKE PUBLIC.
--
-- ADVISORY, not load-bearing: even if this returned a false positive, no leak follows — a stray member row that
-- references a non-existent workspace simply never gets a counterpart, so the bilateral consent check (the REAL
-- gate) returns nothing. This check only keeps the membership table tidy + gives a clear error on a typo'd id.
CREATE OR REPLACE FUNCTION core._workspace_is_open(p_workspace text)
  RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER SET search_path TO 'core','pg_catalog' AS $$
  SELECT EXISTS(SELECT 1 FROM core.workspace WHERE workspace_id = p_workspace AND state = 'active');
$$;
ALTER FUNCTION core._workspace_is_open(text) OWNER TO veripsa_migrator;
REVOKE EXECUTE ON FUNCTION core._workspace_is_open(text) FROM PUBLIC;

-- _mutual_consent_active: THE bilateral-consent decision, owner-context. Returns the set of (workspace_id) for
-- which BOTH p_account_a (with p_repo_a) AND p_account_b (with p_repo_b) hold a consent_state='accepted'
-- workspace_member row — i.e. the workspaces in which X and Y have EACH independently consented THEIR OWN repo.
-- It is a SECURITY DEFINER owner read (the migrator owner, with FORCE RLS on workspace_member temporarily
-- satisfied by reading the two sides EACH under its OWN account pin — never one cross-tenant pin). This is the
-- single place the two sides' membership is compared; a TENANT can NEVER run this join itself (REVOKE PUBLIC; not
-- granted to any seat role — only the cross-tenant read below, which is veripsa_app-only, calls it). Returns
-- workspace ids ONLY — never a row, never the other side's repo/branch/timestamps. content-free.
--
-- WHY owner-context-with-per-side-pin and not a single query: workspace_member is FORCE RLS keyed by account_id.
-- A migrator with no pin sees ZERO rows; pinned to A it sees ONLY A's rows. So to confirm BOTH sides we pin to A,
-- collect A's accepted workspaces for repo_a; pin to B, collect B's accepted workspaces for repo_b; intersect.
-- At NO point is a single statement run under a pin that would expose one tenant's rows to the other tenant's
-- context — each side is read strictly inside its own isolation, then the two id-sets are intersected in memory.
CREATE OR REPLACE FUNCTION core._mutual_consent_active(
    p_account_a text, p_repo_a text, p_account_b text, p_repo_b text)
  RETURNS TABLE(workspace_id text)
  LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path TO 'core','pg_catalog' AS $$
DECLARE v_prev text; v_a_ws text[]; v_b_ws text[];
BEGIN
  -- A↔A self-pairs are nonsensical for a cross-TENANT read; never report consent for them (defense-in-depth —
  -- the caller already excludes same-account, but a mutual-consent fn must not certify a self-link either).
  IF p_account_a IS NULL OR p_account_b IS NULL OR p_account_a = p_account_b THEN
    RETURN;
  END IF;
  v_prev := current_setting('core.current_account', true);
  -- SEQUENTIAL per-side reads with an EXPLICIT pin between them (the owner_cost_surface discipline — NOT a
  -- set_config-in-WHERE trick, whose evaluation order the planner could reorder). Pin A, collect A's accepted
  -- workspaces for repo_a inside A's OWN RLS wall; pin B, collect B's; then intersect the two id arrays in memory.
  -- Each scan runs under exactly one tenant's pin — no statement ever reads one tenant under the other's context.
  PERFORM set_config('core.current_account', p_account_a, true);
  SELECT COALESCE(array_agg(DISTINCT m.workspace_id), ARRAY[]::text[]) INTO v_a_ws
    FROM core.workspace_member m
   WHERE m.account_id = p_account_a AND m.repo = p_repo_a AND m.consent_state = 'accepted';

  PERFORM set_config('core.current_account', p_account_b, true);
  SELECT COALESCE(array_agg(DISTINCT m.workspace_id), ARRAY[]::text[]) INTO v_b_ws
    FROM core.workspace_member m
   WHERE m.account_id = p_account_b AND m.repo = p_repo_b AND m.consent_state = 'accepted';

  -- restore the caller's pin BEFORE returning the intersection (so no per-side pin leaks into the rest of the txn).
  PERFORM set_config('core.current_account', COALESCE(v_prev,''), true);
  -- BILATERAL: a workspace BOTH sides accepted into (the array intersection). Returned as ids ONLY.
  RETURN QUERY SELECT ws FROM unnest(v_a_ws) AS ws WHERE ws = ANY (v_b_ws);
END $$;
ALTER FUNCTION core._mutual_consent_active(text,text,text,text) OWNER TO veripsa_migrator;
REVOKE EXECUTE ON FUNCTION core._mutual_consent_active(text,text,text,text) FROM PUBLIC;

-- ============================================================================================================
-- THE CONSENTED CROSS-TENANT READ — gated on CONSENT (a FACT), not a GUC. The single deliberate cross-tenant
-- read this layer adds, locked exactly like 95_owner.sql's owner_cost_surface.
-- ============================================================================================================

-- cross_tenant_contract_surface: for a pair (repoA under account X, repoB under account Y), return ONLY the
-- contract-key "consumed-by" facts that cross the boundary — the shared contract KEYS, the CONSUMER-side repo
-- NAME, and COUNTS — IFF X and Y have bilateral workspace consent for that exact pair. NEVER a path/node/edge/
-- graph of either side. The flow, in order (the order IS the safety):
--   (a) VERIFY mutual consent FIRST: _mutual_consent_active(X,repoA, Y,repoB) must return ≥1 workspace. If it
--       returns none (no consent, one-sided consent, revoked, or a forged GUC that grants no real row), we RETURN
--       immediately with ZERO rows — the relaxed adjacency is NEVER run, so nothing about B can cross.
--   (b) ONLY THEN run the relaxed coupling. We do NOT call _cross_repo_adjacency (it is same-account: it reads
--       both repos under ONE p_account, which cannot express X≠Y). Instead we read each side's contract-key edge
--       SET under its OWN account pin (X for repoA, Y for repoB) — exactly the per-side-pin discipline of
--       _mutual_consent_active and owner_cost_surface — and join the two key-sets in memory. At no point is one
--       tenant's data read under the other's pin.
--   (c) PROJECT to content-free facts: group by the shared contract KEY + DIRECTION; emit the key, the direction,
--       the CONSUMER repo name, and counts of distinct producer/consumer FILES — never the file PATHS themselves,
--       never a node/edge body. (Counts are the moat-safe summary: "your repo's contract X is consumed by N
--       files in their repo R" — enough to be useful, nothing that reconstructs B's graph.)
--
-- This does NOT relax tenant_isolation, does NOT drop FORCE RLS, and does NOT pin a single cross-tenant
-- current_account (each side read under its own pin). Same-account self-pairs return ∅.
--
-- LOCK: SECURITY DEFINER (migrator), REVOKE FROM PUBLIC + every tenant/seat role, GRANT ONLY to veripsa_app — the
-- DELIBERATE cross-tenant read, locked identically to owner_cost_surface. A buyer SEAT gets permission denied.
-- The veripsa.cross_repo GUC is IRRELEVANT here (it gates the SAME-ACCOUNT _cross_repo_adjacency); this read is
-- gated on CONSENT, never a GUC — a forged GUC changes nothing.
CREATE OR REPLACE FUNCTION core.cross_tenant_contract_surface(
    p_account_a text, p_repo_a text, p_branch_a text,
    p_account_b text, p_repo_b text, p_branch_b text)
  RETURNS TABLE(shared_key text, dir text, consumer_repo text, producer_files int, consumer_files int)
  LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path TO 'core','pg_catalog' AS $$
DECLARE v_prev text; v_consented boolean; v_a jsonb; v_b jsonb;
BEGIN
  -- (0) a cross-TENANT read requires two DIFFERENT accounts. A self-pair is not cross-tenant → ∅.
  IF p_account_a IS NULL OR p_account_b IS NULL OR p_account_a = p_account_b THEN
    RETURN;
  END IF;

  -- (a) VERIFY BILATERAL CONSENT FIRST. If there is no workspace BOTH sides accepted this exact (repo) pair into,
  -- we return immediately — the relaxed coupling below is never reached, so nothing about B can ever cross. This
  -- is THE gate (a FACT in workspace_member, read owner-context per-side), NOT a GUC.
  SELECT EXISTS(
    SELECT 1 FROM core._mutual_consent_active(p_account_a, p_repo_a, p_account_b, p_repo_b)
  ) INTO v_consented;
  IF NOT v_consented THEN
    RETURN;   -- no bilateral consent (none / one-sided / revoked / forged GUC) → ZERO cross-read.
  END IF;

  -- (b) CONSENT HOLDS. COLLECT each side's contract-key edge set under its OWN account pin, SEQUENTIALLY, into a
  -- jsonb array — the owner_cost_surface discipline (an explicit PERFORM set_config BETWEEN the two reads), NOT a
  -- set_config-in-WHERE trick whose evaluation order the planner could reorder so one tenant is read under the
  -- other's pin. Each aggregate below runs under exactly ONE tenant's pin, so a row from B is NEVER read while
  -- pinned to A (and vice-versa). The collected items are content-free contract endpoints (path + key + kind); the
  -- file PATHS are aggregated away in step (c) and NEVER leave this function. (jsonb, not a temp table, so the fn
  -- stays non-VOLATILE — Postgres forbids CREATE TEMP TABLE in a STABLE fn.) Scoped to the cross-repo-stable
  -- contract substrates ONLY (route::/api_*::) so a within-repo file-path `dst` (imports) or a bare table name can
  -- NEVER cross the boundary here — identical key scope to _cross_repo_adjacency.
  v_prev := current_setting('core.current_account', true);

  -- SIDE A: pin X, collect repo A's contract edges inside A's OWN RLS wall.
  PERFORM set_config('core.current_account', p_account_a, true);
  SELECT COALESCE(jsonb_agg(jsonb_build_object(
           'f',ce.src,'ckey',ce.dst,
           'sk',COALESCE(ce.semantic_dst_key,core._semantic_ref_key(ce.dst)),
           'ek',ce.edge_kind
         )), '[]'::jsonb)
    INTO v_a
    FROM core.code_edge ce
   WHERE ce.account_id = p_account_a AND ce.repo = p_repo_a AND ce.branch = p_branch_a
     AND ce.edge_kind IN ('queries','alters') AND ce.reference_status IS NULL
     AND (ce.dst LIKE 'route::%' OR ce.dst LIKE 'api\_operation::%' OR ce.dst LIKE 'api\_schema::%'
          OR ce.dst LIKE 'api\_type::%' OR ce.dst LIKE 'api\_message::%' OR ce.dst LIKE 'api\_service::%');

  -- SIDE B: pin Y, collect repo B's contract edges inside B's OWN RLS wall — never under A's pin.
  PERFORM set_config('core.current_account', p_account_b, true);
  SELECT COALESCE(jsonb_agg(jsonb_build_object(
           'f',ce.src,'ckey',ce.dst,
           'sk',COALESCE(ce.semantic_dst_key,core._semantic_ref_key(ce.dst)),
           'ek',ce.edge_kind
         )), '[]'::jsonb)
    INTO v_b
    FROM core.code_edge ce
   WHERE ce.account_id = p_account_b AND ce.repo = p_repo_b AND ce.branch = p_branch_b
     AND ce.edge_kind IN ('queries','alters') AND ce.reference_status IS NULL
     AND (ce.dst LIKE 'route::%' OR ce.dst LIKE 'api\_operation::%' OR ce.dst LIKE 'api\_schema::%'
          OR ce.dst LIKE 'api\_type::%' OR ce.dst LIKE 'api\_message::%' OR ce.dst LIKE 'api\_service::%');

  -- restore the caller's pin NOW (before the join) — the join is over the already-collected content-free jsonb,
  -- so it needs no pin, and a cross-tenant read must not leave either side's pin armed for the rest of the txn.
  PERFORM set_config('core.current_account', COALESCE(v_prev,''), true);

  -- (c) JOIN + CONTENT-FREE PROJECTION over the collected rows (no pin dependency, no live cross-tenant table read).
  RETURN QUERY
  -- jsonb_to_recordset matches by FIELD NAME: the collected objects use keys f/ckey/ek, so the column list MUST
  -- name them f/ckey/ek (a mismatched name yields NULL → 0 counts). We then alias f→af/bf for the join below.
  WITH a_edges AS (
         SELECT f AS af,ckey,sk,ek
           FROM jsonb_to_recordset(v_a) AS t(f text,ckey text,sk text,ek text)
       ),
       b_edges AS (
         SELECT f AS bf,ckey,sk,ek
           FROM jsonb_to_recordset(v_b) AS t(f text,ckey text,sk text,ek text)
       ),
  -- MULTI-DEFINER guard (mirrors _cross_repo_adjacency / res_adj's res_hubs): a key DEFINED by >1 file in EITHER
  -- repo is ambiguous and anchors no cross-repo coupling — drop it.
  ambig AS (
    SELECT z.sk FROM (
      SELECT sk, count(DISTINCT af) c FROM a_edges WHERE ek='alters' GROUP BY sk
      UNION ALL
      SELECT sk, count(DISTINCT bf) c FROM b_edges WHERE ek='alters' GROUP BY sk
    ) z GROUP BY z.sk HAVING max(z.c) > 1
  ),
  -- producer↔consumer pairs across the boundary, BOTH directions. The file ENDPOINTS are carried only long enough
  -- to COUNT distinct producers/consumers per key — they are aggregated away below, so NO path ever leaves the fn.
  links AS (
    SELECT a.sk, a.ckey AS shared_key, 'A_def->B_use'::text AS dir, p_repo_b AS consumer_repo,
           a.af AS producer_file, b.bf AS consumer_file
      FROM a_edges a JOIN b_edges b ON b.sk=a.sk
     WHERE a.ek='alters' AND b.ek='queries' AND a.sk NOT IN (SELECT sk FROM ambig)
    UNION
    SELECT a.sk, a.ckey AS shared_key, 'B_def->A_use'::text AS dir, p_repo_a AS consumer_repo,
           b.bf AS producer_file, a.af AS consumer_file
      FROM a_edges a JOIN b_edges b ON b.sk=a.sk
     WHERE a.ek='queries' AND b.ek='alters' AND a.sk NOT IN (SELECT sk FROM ambig)
  ),
  display_collisions AS (
    SELECT dl.shared_key,count(DISTINCT dl.sk) AS identities
      FROM links dl
     GROUP BY dl.shared_key
  )
  -- CONTENT-FREE: the shared contract key, the direction, the consumer repo NAME, and COUNTS of distinct
  -- producer/consumer files. NEVER the file paths. Distinct raw keys which sanitize to the same display token
  -- receive a fixed digest suffix instead of being silently merged into one contract count.
  SELECT CASE WHEN dc.identities>1
              THEN l.shared_key||'#ref-'||left(l.sk,12)
              ELSE l.shared_key END AS shared_key,
         l.dir,l.consumer_repo,
         count(DISTINCT l.producer_file)::int AS producer_files,
         count(DISTINCT l.consumer_file)::int AS consumer_files
    FROM links l
    JOIN display_collisions dc ON dc.shared_key=l.shared_key
   GROUP BY l.sk,l.shared_key,dc.identities,l.dir,l.consumer_repo
   ORDER BY 1,l.dir;
END $$;
ALTER FUNCTION core.cross_tenant_contract_surface(text,text,text,text,text,text) OWNER TO veripsa_migrator;
-- LOCK (identical to owner_cost_surface): REVOKE from PUBLIC so no tenant/seat role inherits it; GRANT only to
-- veripsa_app (the host service identity). A buyer SEAT (veripsa_writer / veripsa_demo_*) holds no grant → it
-- gets permission denied when it tries to reach the cross-tenant read AT ALL (proven by the red-team gate).
REVOKE EXECUTE ON FUNCTION core.cross_tenant_contract_surface(text,text,text,text,text,text) FROM PUBLIC;
GRANT  EXECUTE ON FUNCTION core.cross_tenant_contract_surface(text,text,text,text,text,text) TO veripsa_app;  -- CONSENTED CROSS-TENANT READ: contract-key "consumed-by" facts + counts ONLY, bilateral-consent-gated (a buyer seat cannot read across tenants)

-- ============================================================================================================
