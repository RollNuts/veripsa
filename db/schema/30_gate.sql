-- PHASE 2 — THE GATE (the only write path). Provisioning + the lock + coexistence ingest + the
-- fact recorders (each writes ONE event KIND). SECURITY DEFINER; identity from the connection role;
-- each arms the forgery token for exactly the table it writes.
-- ============================================================================================

-- provision_seat: admin op (migrator) — create/ensure an account + agent + the connection-role credential
-- the gate binds identity to. Owner-bypass on credential (ENABLE-not-FORCE); account/agent admitted by
-- pinning current_account. Idempotent.
CREATE OR REPLACE FUNCTION core.provision_seat(p_account text, p_account_name text, p_agent text, p_agent_name text, p_role text, p_operator text DEFAULT NULL, p_model text DEFAULT NULL)
    RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path TO 'core','pg_catalog' AS $$
BEGIN
  PERFORM set_config('core.current_account', p_account, true);
  PERFORM core.mark_governed_write('account');
  INSERT INTO core.account(account_id, display_name) VALUES (p_account, p_account_name) ON CONFLICT (account_id) DO NOTHING;
  PERFORM core.mark_governed_write('agent');
  INSERT INTO core.agent(agent_id, account_id, display_name, operator, default_model)
  VALUES (p_agent, p_account, p_agent_name, p_operator, p_model) ON CONFLICT (agent_id) DO NOTHING;
  INSERT INTO core.credential(role_name, agent_id, account_id) VALUES (p_role, p_agent, p_account) ON CONFLICT (role_name) DO NOTHING;
END $$;
ALTER FUNCTION core.provision_seat(text,text,text,text,text,text,text) OWNER TO veripsa_migrator;

-- ── MULTI-TENANT routing: one GitHub installation = one isolated tenant ACCOUNT. ────────────────────────
-- installation_account: the service's installation→account map. NOT per-account (it routes BETWEEN accounts),
-- so it carries NO RLS; it is reachable ONLY through the SECURITY DEFINER function below (no table grants).
CREATE TABLE IF NOT EXISTS core.installation_account (
    installation_id text PRIMARY KEY,
    account_id text NOT NULL REFERENCES core.account(account_id),
    bound_at timestamptz DEFAULT now() NOT NULL
);
DO $$
BEGIN
  IF EXISTS (
    SELECT 1
      FROM pg_class c
      JOIN pg_namespace n ON n.oid = c.relnamespace
     WHERE n.nspname = 'core'
       AND c.relname = 'installation_account'
       AND c.relowner <> 'veripsa_migrator'::regrole
  ) THEN
    ALTER TABLE core.installation_account OWNER TO veripsa_migrator;
  END IF;
END $$;
-- HOT-DEPLOY EXPAND PHASE. Render applies this module while the old image is
-- still serving. Relation DDL must therefore commit one short statement at a
-- time before the function-publication transaction starts; otherwise an
-- AccessExclusive lock acquired here would be retained through hundreds of
-- function/ACL statements and stop unrelated live-account DML.
--
-- These columns are additive and old workers ignore them. The functions that
-- attach meaning to them remain atomic below.
-- LIVENESS STATE (commercial-completeness — the no-billing-without-a-LIVE-link invariant). The row above is the
-- installation→account MAP, but it carried NO liveness: uninstall (purge_account_working_set_with_authority,
-- 35_lifecycle.sql) and suspend (release_account_claims_with_authority) deliberately KEEP the row (uninstall keeps
-- it because the append-only ledger is retained + a reinstall re-binds the SAME id; suspend is reversible), while
-- a hard account erase deletes the complete account scope. So a bare SELECT (list_installation_ids, 40_surfaces.sql) returned an UNINSTALLED/
-- SUSPENDED installation as if it were still live — the exact read seam the no-billing-without-a-live-link gates
-- depend on, and the seam graph freshness walks to decide which installs to keep warm. revoked_at makes the dead
-- state EXPLICIT + queryable: it is STAMPED now() by the uninstall + suspend handlers and CLEARED back to NULL by
-- reactivate (a real reinstall / unsuspend / repos-added), so "revoked_at IS NULL" = the install is live. NULLABLE
-- (a live install has no revoke time); ADD-COLUMN-IF-NOT-EXISTS of a nullable column with NO default = a catalog-
-- only change (no table rewrite, no backfill, brief lock) → contention-free on a busy prod, the prod-apply
-- discipline. Content-free: a timestamp on the routing row — never a path/name/body. Idempotent (re-apply no-ops).
SELECT core._ensure_column_online(
  'installation_account','revoked_at','timestamptz');
-- Public GitHub account metadata for owner/admin display. These are mutable
-- labels (login/type), while account_id remains the stable tenant key. Stored
-- here so the platform admin does not hard-code stale GitHub usernames after an
-- account rename. Content-free: public GitHub account metadata only, never repo
-- contents, paths, graph data, or customer source.
SELECT core._ensure_column_online(
  'installation_account','account_login','text');
SELECT core._ensure_column_online(
  'installation_account','account_type','text');
SELECT core._ensure_column_online(
  'installation_account','account_seen_at','timestamptz');
-- The real GitHub App installation generation currently authoritative for this stable owning account.  The
-- routing PK intentionally remains the stable owner id; this bounded generation id is only a lifecycle fence so
-- a delayed delete for installation A cannot purge a replacement installation B.
SELECT core._ensure_column_online(
  'installation_account','github_installation_id','text');
-- Immutable creation time returned by the authenticated GitHub installation point-read.  The id alone distinguishes
-- generations, while this timestamp is the monotonic proof that prevents a delayed proof for old generation A from
-- replacing an already-recorded newer generation B.
SELECT core._ensure_column_online(
  'installation_account','github_installation_created_at','timestamptz');

-- ── UNINSTALL RESURRECTION TOMBSTONE (audit iter-4 P1) ───────────────────────────────────────────────────
-- Expand the durable "do not process old work after uninstall" boundary before
-- any function that reads it is published.
CREATE TABLE IF NOT EXISTS core.account_lifecycle_tombstone (
    account_id text PRIMARY KEY,
    reason text NOT NULL,
    tombstoned_at timestamptz DEFAULT now() NOT NULL,
    active boolean DEFAULT true NOT NULL,
    last_event_received_at timestamptz DEFAULT now() NOT NULL,
    last_delivery_key text DEFAULT '' NOT NULL,
    blocked_installation_id text,
    -- Keep the legacy gdpr_erase shape through this rolling release. The old worker still routes ordinary events
    -- through the provisioning entry point until the new non-provisioning runtime is fully promoted.
    CONSTRAINT account_tombstone_reason_check CHECK (
      reason = ANY (ARRAY['uninstall_purge','gdpr_erase']))
);
SELECT core._ensure_column_online(
  'account_lifecycle_tombstone','active',
  'boolean DEFAULT true NOT NULL');
SELECT core._ensure_column_online(
  'account_lifecycle_tombstone','last_event_received_at','timestamptz');
UPDATE core.account_lifecycle_tombstone
   SET last_event_received_at=tombstoned_at
 WHERE last_event_received_at IS NULL;
SELECT core._ensure_column_default_online(
  'account_lifecycle_tombstone','last_event_received_at','now()');
-- Avoid replaying SET NOT NULL (and its AccessExclusive lock) on every deploy.
-- A validated check lets PostgreSQL prove a legacy conversion without a second
-- table scan while the stronger lock is held.
DO $$
BEGIN
  IF EXISTS (
    SELECT 1
      FROM pg_attribute a
      JOIN pg_class c ON c.oid=a.attrelid
      JOIN pg_namespace n ON n.oid=c.relnamespace
     WHERE n.nspname='core' AND c.relname='account_lifecycle_tombstone'
       AND a.attname='last_event_received_at' AND NOT a.attnotnull
  ) AND NOT EXISTS (
    SELECT 1
      FROM pg_constraint q
      JOIN pg_class c ON c.oid=q.conrelid
      JOIN pg_namespace n ON n.oid=c.relnamespace
     WHERE n.nspname='core' AND c.relname='account_lifecycle_tombstone'
       AND q.conname='account_lifecycle_last_event_received_at_nn'
  ) THEN
    ALTER TABLE core.account_lifecycle_tombstone
      ADD CONSTRAINT account_lifecycle_last_event_received_at_nn
      CHECK (last_event_received_at IS NOT NULL) NOT VALID;
  END IF;
END $$;
DO $$
BEGIN
  IF EXISTS (
    SELECT 1
      FROM pg_constraint q
      JOIN pg_class c ON c.oid=q.conrelid
      JOIN pg_namespace n ON n.oid=c.relnamespace
     WHERE n.nspname='core' AND c.relname='account_lifecycle_tombstone'
       AND q.conname='account_lifecycle_last_event_received_at_nn'
       AND NOT q.convalidated
  ) THEN
    ALTER TABLE core.account_lifecycle_tombstone
      VALIDATE CONSTRAINT account_lifecycle_last_event_received_at_nn;
  END IF;
END $$;
SELECT core._ensure_column_not_null_online(
  'account_lifecycle_tombstone','last_event_received_at');
SELECT core._ensure_column_online(
  'account_lifecycle_tombstone','last_delivery_key',
  'text DEFAULT '''' NOT NULL');
SELECT core._ensure_column_online(
  'account_lifecycle_tombstone','blocked_installation_id','text');
-- Do NOT remove or narrow legacy reason='gdpr_erase' rows in this schema-first release. Render applies schema.sql
-- while the origin/main worker is still serving, and that worker uses enter_installation_with_authority for ordinary
-- events. The retained row is therefore the only rolling-upgrade fence that prevents its lazy provisioning path
-- from resurrecting an erased account.
DO $$
BEGIN
  IF EXISTS (
    SELECT 1
      FROM pg_class c
      JOIN pg_namespace n ON n.oid = c.relnamespace
     WHERE n.nspname = 'core'
       AND c.relname = 'account_lifecycle_tombstone'
       AND c.relowner <> 'veripsa_migrator'::regrole
  ) THEN
    ALTER TABLE core.account_lifecycle_tombstone OWNER TO veripsa_migrator;
  END IF;
END $$;
REVOKE ALL ON TABLE core.account_lifecycle_tombstone FROM PUBLIC;

-- Publish every function which consumes the expanded lifecycle shape as one
-- catalog boundary. This transaction contains no table/index/trigger DDL or
-- top-level data mutation, so it cannot retain a live-table DML lock.
BEGIN;

-- installation_is_live: the platform's per-installation liveness read — true iff the installation is mapped AND not
-- revoked (uninstalled/suspended). This is the READ the no-billing-without-a-live-link gates (the platform's pre-
-- checkout / webhook-in-flight / post-purchase checks) call to confirm an installation is still live before they
-- let a charge or a plan-apply proceed, so a customer is never billed against a link that has gone dead. An UNKNOWN
-- installation (no row) is NOT live (false) — same fail-closed posture as the billing setters that mint nothing for
-- an unknown id. SECURITY DEFINER (the routing table has no grants), STABLE, pinned search_path. Content-free: it
-- takes the installation id and returns a boolean — no account id, no count, nothing else. Granted to the platform
-- reader + the App (the same surface as list_installation_ids / effect_for_installation).
CREATE OR REPLACE FUNCTION core.installation_is_live(p_installation_id text) RETURNS boolean
    LANGUAGE sql STABLE SECURITY DEFINER SET search_path TO 'core','pg_catalog' AS $$
  SELECT EXISTS (
    SELECT 1 FROM core.installation_account
     WHERE installation_id = left(NULLIF(btrim(COALESCE(p_installation_id,'')),''),64)
       AND revoked_at IS NULL)
$$;
ALTER FUNCTION core.installation_is_live(text) OWNER TO veripsa_migrator;
REVOKE EXECUTE ON FUNCTION core.installation_is_live(text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION core.installation_is_live(text) TO example_platform_reader, veripsa_app;

-- enter_installation_with_authority: the App calls this ONCE per webhook event with the delivery's
-- installation id. It looks up (or LAZILY PROVISIONS, on first sight) that installation's OWN account, and
-- pins core.installation_account for the connection so the rest of the event runs in that tenant — every gate
-- write and every surface read is then walled to that account by RLS. Returns the account id. NULL/empty
-- installation → returns NULL and pins nothing (the local/dogfood path keeps using the role's own account).
CREATE OR REPLACE FUNCTION core.enter_installation_with_authority(p_installation_id text)
    RETURNS text LANGUAGE plpgsql SECURITY DEFINER SET search_path TO 'core','pg_catalog' AS $$
DECLARE v_inst text; v_account text;
BEGIN
  v_inst := left(NULLIF(btrim(COALESCE(p_installation_id,'')),''),64);
  IF v_inst IS NULL THEN RETURN NULL; END IF;
  SELECT account_id INTO v_account FROM core.installation_account WHERE installation_id = v_inst;
  IF v_account IS NULL THEN                              -- first event for this installation → its own account
    v_account := 'ACCT-GH-' || v_inst;
    -- RESURRECTION GUARD (audit iter-4 P1): if this account was UNINSTALL-PURGED, a TOMBSTONE row
    -- (core.account_lifecycle_tombstone, written by purge below) survives that lifecycle event. A BACKGROUND
    -- per-repo writer (co-change populate, self_heal_main_graph, boot-reconcile) racing an in-flight task across the
    -- account-wide uninstall reaches HERE on a purged account and would otherwise continue writing. So when a
    -- tombstone exists we DO NOT
    -- re-provision the account/installation rows: we still pin the GUC to the resolved account id (so a downstream
    -- core.assert_account_live_with_authority can resolve it and raise a clean, content-free fail-closed), but we
    -- mint NOTHING. A GENUINE re-install (installation.created / repos-added) explicitly clears the tombstone via
    -- core.reactivate_account_with_authority() in the onboarding handler BEFORE its first write, so a real reinstall
    -- re-provisions and proceeds — only an unwanted background resurrection is blocked. (A purge keeps the account
    -- row, so the live-writer guard below covers the retained purged route.)
    IF NOT EXISTS (
      SELECT 1 FROM core.account_lifecycle_tombstone t
       WHERE t.account_id = v_account
         -- Rolling-safe while `active` is being added below: an old row shape is conservatively tombstoned.
         AND COALESCE((to_jsonb(t)->>'active')::boolean,true)
    ) THEN
      PERFORM set_config('core.current_account', v_account, true);  -- so the new-account INSERT passes account-RLS WITH CHECK
      PERFORM core.mark_governed_write('account');
      INSERT INTO core.account(account_id, display_name) VALUES (v_account, 'gh-installation-'||v_inst) ON CONFLICT (account_id) DO NOTHING;
      INSERT INTO core.installation_account(installation_id, account_id) VALUES (v_inst, v_account) ON CONFLICT (installation_id) DO NOTHING;
      SELECT account_id INTO v_account FROM core.installation_account WHERE installation_id = v_inst;
    END IF;
    -- (tombstoned-and-absent is a legacy-safe edge: pin the bounded id but create nothing.)
  END IF;
  PERFORM set_config('core.installation_account', v_account, false);   -- session-level: routes this whole event
  RETURN v_account;
END $$;
ALTER FUNCTION core.enter_installation_with_authority(text) OWNER TO veripsa_migrator;
REVOKE EXECUTE ON FUNCTION core.enter_installation_with_authority(text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION core.enter_installation_with_authority(text) TO veripsa_app;

-- Non-provisioning route for ordinary webhooks and background work. Only an authenticated activation event may
-- use enter_installation_with_authority to create a tenant. A delayed event after hard erasure, or work for a
-- revoked installation, therefore cannot recreate an account merely by being routed. This structural split makes
-- account erasure complete without retaining a raw GitHub account/installation tombstone forever.
CREATE OR REPLACE FUNCTION core.enter_existing_installation_with_authority(p_installation_id text)
    RETURNS text LANGUAGE plpgsql SECURITY DEFINER SET search_path TO 'core','pg_catalog' AS $$
DECLARE v_inst text; v_account text;
BEGIN
  v_inst := left(NULLIF(btrim(COALESCE(p_installation_id,'')),''),64);
  IF v_inst IS NULL THEN RETURN NULL; END IF;
  SELECT account_id INTO v_account
    FROM core.installation_account
   WHERE installation_id=v_inst AND revoked_at IS NULL;
  IF v_account IS NULL THEN RETURN NULL; END IF;
  PERFORM set_config('core.installation_account',v_account,false);
  RETURN v_account;
END $$;
ALTER FUNCTION core.enter_existing_installation_with_authority(text) OWNER TO veripsa_migrator;
REVOKE EXECUTE ON FUNCTION core.enter_existing_installation_with_authority(text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION core.enter_existing_installation_with_authority(text) TO veripsa_app;

-- note_installation_account_metadata_with_authority: after the trusted
-- installation route pins core.installation_account, record the current public
-- GitHub account login/type seen in the webhook payload. This is deliberately a
-- separate app-only helper, not part of the tenant key: login is mutable, id is
-- stable. The WHERE clause ties the update to the pinned account so a stray
-- caller cannot annotate another tenant's routing row.
CREATE OR REPLACE FUNCTION core.note_installation_account_metadata_with_authority(
    p_installation_id text,
    p_account_login text DEFAULT NULL,
    p_account_type text DEFAULT NULL
) RETURNS void
    LANGUAGE plpgsql SECURITY DEFINER SET search_path TO 'core','pg_catalog' AS $$
DECLARE
  v_inst text;
  v_account text;
  v_login text;
  v_type text;
BEGIN
  v_inst := left(NULLIF(btrim(COALESCE(p_installation_id,'')),''),64);
  v_account := NULLIF(current_setting('core.installation_account', true), '');
  IF v_inst IS NULL OR v_account IS NULL THEN RETURN; END IF;

  v_login := left(NULLIF(btrim(COALESCE(p_account_login,'')),''),80);
  v_type := left(NULLIF(btrim(COALESCE(p_account_type,'')),''),40);
  IF v_login IS NULL AND v_type IS NULL THEN RETURN; END IF;

  UPDATE core.installation_account
     SET account_login = COALESCE(v_login, account_login),
         account_type = COALESCE(v_type, account_type),
         account_seen_at = now()
   WHERE installation_id = v_inst
     AND account_id = v_account
     AND revoked_at IS NULL
     -- This is display/last-seen metadata, not event authority.  Avoid one
     -- physical update per webhook: that shared hot row otherwise serializes
     -- independent repositories for the same account.  A rename/type change
     -- remains immediate; unchanged last-seen evidence is refreshed at most
     -- once per five minutes.
     AND (
       (v_login IS NOT NULL AND account_login IS DISTINCT FROM v_login)
       OR (v_type IS NOT NULL AND account_type IS DISTINCT FROM v_type)
       OR account_seen_at IS NULL
       OR account_seen_at < now() - interval '5 minutes'
     );
END $$;
ALTER FUNCTION core.note_installation_account_metadata_with_authority(text,text,text) OWNER TO veripsa_migrator;
REVOKE EXECUTE ON FUNCTION core.note_installation_account_metadata_with_authority(text,text,text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION core.note_installation_account_metadata_with_authority(text,text,text) TO veripsa_app;

-- The uninstall fence relation was expanded before BEGIN. Keeping only
-- functions in this transaction is the hot-deploy liveness boundary.

-- One account lifecycle lock serializes long-running background convergence with uninstall/reactivation/erase.
-- Destructive/generation-changing work takes the exclusive form; read-only same-generation admission and boot
-- convergence take the shared form.  That lets live work for the current generation proceed while boot reads, but
-- still makes uninstall/erase/reactivation wait for every older writer to finish.  A hash collision can only
-- over-serialize unrelated tenants; it cannot mix their data.
CREATE OR REPLACE FUNCTION core._take_account_lifecycle_xact_lock(p_account text)
    RETURNS void LANGUAGE sql VOLATILE SECURITY DEFINER SET search_path TO 'core','pg_catalog' AS $$
  SELECT pg_advisory_xact_lock(hashtext('core.account_lifecycle'), hashtext(COALESCE(p_account,'')))
$$;
ALTER FUNCTION core._take_account_lifecycle_xact_lock(text) OWNER TO veripsa_migrator;
REVOKE EXECUTE ON FUNCTION core._take_account_lifecycle_xact_lock(text) FROM PUBLIC, veripsa_writer, veripsa_app;

CREATE OR REPLACE FUNCTION core._take_account_lifecycle_xact_lock_shared(p_account text)
    RETURNS void LANGUAGE sql VOLATILE SECURITY DEFINER SET search_path TO 'core','pg_catalog' AS $$
  SELECT pg_advisory_xact_lock_shared(hashtext('core.account_lifecycle'), hashtext(COALESCE(p_account,'')))
$$;
ALTER FUNCTION core._take_account_lifecycle_xact_lock_shared(text) OWNER TO veripsa_migrator;
REVOKE EXECUTE ON FUNCTION core._take_account_lifecycle_xact_lock_shared(text)
  FROM PUBLIC, veripsa_writer, veripsa_app;

-- tombstone_account_with_authority: RECORD the tombstone for the CONNECTION-resolved account (identity from the
-- write context — NEVER a caller argument, so a tenant can only tombstone its OWN account, the SAME un-forgeable
-- discipline the uninstall purge uses). purge_account_working_set_with_authority calls this so
-- a later background writer racing the uninstall can be refused. Idempotent (ON CONFLICT updates the reason/time;
-- a replay refreshes the boundary). Hard account erasure deletes this row; it must never create a gdpr tombstone
-- containing the identifiers it just promised to remove. Content-free. App-delegation only.
CREATE OR REPLACE FUNCTION core.tombstone_account_with_authority(p_reason text)
    RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path TO 'core','pg_catalog' AS $$
DECLARE v_agent text; v_account text; v_reason text;
BEGIN
  SELECT agent, account INTO v_agent, v_account FROM core.establish_session_write_context() AS c(agent,account);
  IF v_account IS NULL OR v_account = '' THEN RAISE EXCEPTION 'tombstone needs a resolved account' USING ERRCODE='23514'; END IF;
  PERFORM core._take_account_lifecycle_xact_lock(v_account);
  IF p_reason IS DISTINCT FROM 'uninstall_purge' THEN
    RAISE EXCEPTION 'account tombstone is uninstall-only' USING ERRCODE='23514';
  END IF;
  v_reason := 'uninstall_purge';
  INSERT INTO core.account_lifecycle_tombstone(
      account_id, reason, active, last_event_received_at, last_delivery_key, blocked_installation_id)
  VALUES (v_account, v_reason, true, clock_timestamp(), '', NULL)
  ON CONFLICT (account_id) DO UPDATE
    SET reason = EXCLUDED.reason, tombstoned_at = now(), active = true,
        last_event_received_at = EXCLUDED.last_event_received_at, last_delivery_key = '',
        blocked_installation_id = NULL;
END $$;
ALTER FUNCTION core.tombstone_account_with_authority(text) OWNER TO veripsa_migrator;
REVOKE EXECUTE ON FUNCTION core.tombstone_account_with_authority(text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION core.tombstone_account_with_authority(text) TO veripsa_app;

-- reactivate_account_with_authority: a GENUINE re-install/onboard (installation.created / unsuspend /
-- new_permissions_accepted / installation_repositories.added) CLEARS the tombstone for the connection-resolved
-- account, so the tenant is live again and its writers proceed. This is the EXPLICIT "a real reinstall reactivates
-- the tenant" step the onboarding handler runs as its first action — distinguishing a legitimate reinstall (which
-- must work, history preserved) from a background writer racing the uninstall (which must NOT resurrect). It also
-- RE-PROVISIONS the account/installation rows after a hard erase when a newly App-JWT-verified activation arrives,
-- so an erased-then-reinstalled account onboards cleanly. The account is already pinned by enter_installation, so provisioning passes
-- account-RLS. Identity from the write context (never a caller arg). Idempotent (a no-op on a live account).
CREATE OR REPLACE FUNCTION core.reactivate_account_with_authority(
    p_delivery_key text, p_installation_proof jsonb)
    RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path TO 'core','pg_catalog' AS $$
DECLARE v_account text; v_cleared int := 0; v_inst text; v_relived int := 0;
        v_delivery_key text; v_delivery_received_at timestamptz;
        v_delivery_installation_id text; v_delivery_account_id text;
        v_was_active boolean; v_last_received_at timestamptz; v_last_delivery_key text;
        v_blocked_installation_id text;
        v_proof_installation_id text; v_proof_account_id text;
        v_proof_created_at timestamptz; v_proof_suspended boolean;
        v_current_installation_id text; v_current_installation_created_at timestamptz;
        v_has_lifecycle boolean := false;
BEGIN
  -- An erase deliberately removes installation_account, so the ordinary identity resolver cannot authorize the
  -- first reinstall: it rejects a session pin with no surviving route.  Resolve that special edge from the pin
  -- established by activation-only enter_installation plus the immutable processing delivery below. A live/no-tombstone account
  -- still requires a real routing row, preserving the raw-GUC forgery guard in resolve_session_identity.
  v_account := NULLIF(current_setting('core.installation_account',true),'');
  IF v_account IS NULL THEN
    RAISE EXCEPTION 'reactivate needs an installation account pin' USING ERRCODE='42501';
  END IF;
  PERFORM core._take_account_lifecycle_xact_lock(v_account);

  SELECT active,last_event_received_at,last_delivery_key,blocked_installation_id
    INTO v_was_active,v_last_received_at,v_last_delivery_key,v_blocked_installation_id
    FROM core.account_lifecycle_tombstone WHERE account_id=v_account;
  v_has_lifecycle := FOUND;

  -- Every activation, including the first one for an account, must bind an App-JWT point read to its exact durable
  -- delivery.  This both closes legacy receipt-loss replays and records the real current installation generation
  -- below.  Consequently the proof-less /1 rolling bridge aborts before an old worker can create an unfenced live
  -- generation; the durable row is retried by the new worker after rollout.
  v_delivery_key := NULLIF(left(COALESCE(p_delivery_key,''),200),'');
  IF v_delivery_key IS NOT NULL THEN
    SELECT d.received_at,
           NULLIF(left(COALESCE(d.payload->'installation'->>'id',''),64),''),
           NULLIF(left(COALESCE(d.payload->'installation'->'account'->>'id',''),64),'')
      INTO v_delivery_received_at,v_delivery_installation_id,v_delivery_account_id
      FROM core.webhook_delivery d
     WHERE d.delivery_key=v_delivery_key
       AND d.status='processing'
       AND (d.account_key=v_account
            OR (v_account LIKE 'ACCT-GH-%' AND d.account_key=substr(v_account,9)))
       AND ((d.event_type='installation'
             AND d.payload->>'action' IN ('created','unsuspend','new_permissions_accepted'))
            OR (d.event_type='installation_repositories' AND d.payload->>'action'='added'));
  END IF;
  IF v_delivery_received_at IS NULL THEN
    RETURN jsonb_build_object('ok',true,'reactivated',false,'account',v_account,
                              'reason','missing durable activation authority');
  END IF;
  IF jsonb_typeof(COALESCE(p_installation_proof,'null'::jsonb)) <> 'object' THEN
    RETURN jsonb_build_object('ok',true,'reactivated',false,'account',v_account,
                              'reason','live installation generation verification required');
  END IF;
  v_proof_installation_id := NULLIF(left(COALESCE(p_installation_proof->>'installation_id',''),64),'');
  v_proof_account_id := NULLIF(left(COALESCE(p_installation_proof->>'account_id',''),64),'');
  BEGIN
    v_proof_created_at := NULLIF(p_installation_proof->>'created_at','')::timestamptz;
  EXCEPTION WHEN invalid_datetime_format OR datetime_field_overflow THEN
    v_proof_created_at := NULL;
  END;
  v_proof_suspended := COALESCE(p_installation_proof->>'suspended','true') <> 'false';
  IF v_proof_installation_id IS NULL OR v_proof_account_id IS NULL OR v_proof_created_at IS NULL
     OR v_proof_suspended
     OR v_delivery_installation_id IS NULL OR v_delivery_account_id IS NULL
     OR v_proof_installation_id<>v_delivery_installation_id
     OR v_proof_account_id<>v_delivery_account_id
     OR NOT (v_proof_account_id=v_account OR 'ACCT-GH-'||v_proof_account_id=v_account) THEN
    RETURN jsonb_build_object('ok',true,'reactivated',false,'account',v_account,
                              'reason','live installation generation verification required');
  END IF;

  -- The App point-read may complete long before this transaction gets the account lock.  Compare its immutable
  -- generation creation time with the durable account high-water before changing either liveness or tombstone
  -- state.  A different id at the same/older time is not provably newer and therefore fails closed; a same-id replay
  -- is idempotent and may only fill/advance a missing timestamp.
  SELECT github_installation_id,github_installation_created_at
    INTO v_current_installation_id,v_current_installation_created_at
    FROM core.installation_account
   WHERE account_id=v_account
   ORDER BY github_installation_created_at DESC NULLS LAST,installation_id
   LIMIT 1;
  IF v_current_installation_id IS NOT NULL
     AND v_current_installation_id<>v_proof_installation_id
     AND v_current_installation_created_at IS NOT NULL
     AND v_proof_created_at<=v_current_installation_created_at THEN
    RETURN jsonb_build_object('ok',true,'reactivated',false,'account',v_account,
                              'reason','older installation generation proof');
  END IF;

  IF v_has_lifecycle AND COALESCE(v_was_active,true) THEN
    -- Local receive order is not generation order.  A different current installation is authoritative even when
    -- its create was queued before a delayed old delete; keep the tuple high-water monotonic but open the new
    -- generation. GitHub installation ids are generation identities, so a blocked id is always refused; only a
    -- legacy/manual boundary with no captured id falls back to immutable created_at after the boundary.
    IF (v_blocked_installation_id IS NULL AND v_proof_created_at<=v_last_received_at)
       OR (v_blocked_installation_id IS NOT NULL
           AND v_proof_installation_id=v_blocked_installation_id) THEN
      RETURN jsonb_build_object('ok',true,'reactivated',false,'account',v_account,
                                'reason','live installation generation verification required');
    END IF;
    UPDATE core.account_lifecycle_tombstone
       SET active=false,
           last_event_received_at=CASE
             WHEN (v_delivery_received_at,v_delivery_key)>(v_last_received_at,v_last_delivery_key)
             THEN v_delivery_received_at ELSE v_last_received_at END,
           last_delivery_key=CASE
             WHEN (v_delivery_received_at,v_delivery_key)>(v_last_received_at,v_last_delivery_key)
             THEN v_delivery_key ELSE v_last_delivery_key END
     WHERE account_id=v_account;
    v_cleared := 1;
  ELSIF NOT v_has_lifecycle AND NOT EXISTS (
    SELECT 1 FROM core.installation_account WHERE account_id=v_account
  ) THEN
    RAISE EXCEPTION 'reactivate pin is not a routed installation account' USING ERRCODE='42501';
  END IF;
  -- ENSURE the identity rows exist (an erase hard-deleted them; a purge kept them). current_account is pinned by
  -- the validated durable activation (or a surviving real route), so the account-RLS WITH CHECK admits the lazy
  -- re-provision for THIS tenant only. installation_account is re-bound for the GH-derived account id.
  PERFORM set_config('core.current_account',v_account,true);
  PERFORM core.mark_governed_write('account');
  INSERT INTO core.account(account_id, display_name) VALUES (v_account, v_account) ON CONFLICT (account_id) DO NOTHING;
  IF v_account LIKE 'ACCT-GH-%' THEN
    v_inst := substr(v_account, 9);
    INSERT INTO core.installation_account(installation_id, account_id) VALUES (v_inst, v_account) ON CONFLICT (installation_id) DO NOTHING;
  END IF;
  -- INSTALLATION LIVENESS (commercial-completeness — the no-billing-without-a-LIVE-link invariant). A real reinstall
  -- / unsuspend / repos-added must bring the install back to LIVE: clear the revoked_at the uninstall or suspend
  -- handler stamped (35_lifecycle.sql), the explicit counterpart to the tombstone clear above, so the install is
  -- again reported live by installation_is_live / the live-only enumerator. The ON CONFLICT DO NOTHING above keeps a
  -- KEPT row's stale revoked_at, so this UPDATE is what re-lives it (covers both the erase-then-reprovision and the
  -- purge/suspend-kept-row paths). RLS does not gate this routing table; the account_id match IS the scope.
  UPDATE core.installation_account SET revoked_at = NULL
   WHERE account_id = v_account AND revoked_at IS NOT NULL;  GET DIAGNOSTICS v_relived = ROW_COUNT;
  UPDATE core.installation_account
     SET github_installation_id = v_proof_installation_id,
         github_installation_created_at = CASE
           WHEN github_installation_id=v_proof_installation_id
             THEN GREATEST(github_installation_created_at,v_proof_created_at)
           ELSE v_proof_created_at
         END
   WHERE account_id = v_account
     AND (github_installation_id IS DISTINCT FROM v_proof_installation_id
          OR github_installation_created_at IS DISTINCT FROM
             GREATEST(github_installation_created_at,v_proof_created_at));
  RETURN jsonb_build_object('ok', true, 'reactivated', true, 'account', v_account, 'tombstone_cleared', v_cleared,
                            'installations_relived', v_relived);
END $$;
ALTER FUNCTION core.reactivate_account_with_authority(text,jsonb) OWNER TO veripsa_migrator;
REVOKE EXECUTE ON FUNCTION core.reactivate_account_with_authority(text,jsonb) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION core.reactivate_account_with_authority(text,jsonb) TO veripsa_app;

-- General signed-event generation admission.  Runtime callers pass only values obtained from their authenticated
-- GitHub App installation point-read.  The account lock turns that proof into a durable monotonic fence shared by
-- activation, delete and suspend: current/same is admitted, a provably older different id is denied, and a newer
-- generation is recorded.  Tombstoned accounts never reopen through an ordinary event; a revoked current generation
-- also remains denied until the explicit activation path clears lifecycle state.
CREATE OR REPLACE FUNCTION core.admit_event_installation_generation_with_authority(
    p_actual_installation_id text, p_created_at timestamptz)
    RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path TO 'core','pg_catalog' AS $$
DECLARE
  v_agent text; v_account text; v_actual text;
  v_current text; v_current_created_at timestamptz; v_revoked_at timestamptz;
  v_tombstoned boolean; v_advanced boolean := false;
BEGIN
  v_actual := NULLIF(left(btrim(COALESCE(p_actual_installation_id,'')),64),'');
  IF v_actual IS NULL THEN
    RAISE EXCEPTION 'event generation admission needs installation id' USING ERRCODE='23514';
  END IF;
  SELECT agent,account INTO v_agent,v_account
    FROM core.establish_session_write_context() AS c(agent,account);
  IF v_account IS NULL OR v_account='' THEN
    RAISE EXCEPTION 'event generation admission needs a resolved account' USING ERRCODE='23514';
  END IF;
  -- NULL proof is a read-only current-generation check and may coexist with boot convergence.  A proof-bearing
  -- call can advance the durable generation, so it remains exclusive with activation/deletion/erase.  Translate
  -- only contention on THIS advisory boundary to a fixed diagnostic marker; the durable worker may defer that
  -- expected wait without misclassifying unrelated row-lock/body 55P03 errors as harmless contention.
  BEGIN
    IF p_created_at IS NULL THEN
      PERFORM core._take_account_lifecycle_xact_lock_shared(v_account);
    ELSE
      PERFORM core._take_account_lifecycle_xact_lock(v_account);
    END IF;
  EXCEPTION WHEN lock_not_available THEN
    RAISE EXCEPTION 'account lifecycle advisory lock is busy'
      USING ERRCODE='55P03', CONSTRAINT='veripsa_account_lifecycle_advisory_timeout';
  END;
  SELECT EXISTS (
    SELECT 1 FROM core.account_lifecycle_tombstone
     WHERE account_id=v_account AND active) INTO v_tombstoned;
  SELECT github_installation_id,github_installation_created_at,revoked_at
    INTO v_current,v_current_created_at,v_revoked_at
    FROM core.installation_account
   WHERE account_id=v_account
   ORDER BY github_installation_created_at DESC NULLS LAST,installation_id
   LIMIT 1;
  IF NOT FOUND THEN
    RETURN jsonb_build_object('ok',true,'admitted',false,'proof_required',true,
                              'reason','installation route absent');
  END IF;
  IF v_tombstoned THEN
    RETURN jsonb_build_object('ok',true,'admitted',false,'proof_required',false,
                              'reason','account lifecycle tombstoned');
  END IF;
  -- Fast hot-path preflight.  A caller which already carries the exact durable generation id does not need an
  -- App-JWT point read merely to prove what the DB already knows.  NULL created_at is therefore read-only: it may
  -- admit only the same current live generation, and can never initialize or change the durable generation.
  IF p_created_at IS NULL THEN
    IF v_current IS NULL OR v_current<>v_actual THEN
      RETURN jsonb_build_object('ok',true,'admitted',false,'proof_required',true,
                                'reason','installation generation proof required');
    END IF;
    IF v_revoked_at IS NOT NULL THEN
      RETURN jsonb_build_object('ok',true,'admitted',false,'proof_required',false,
                                'reason','installation generation revoked');
    END IF;
    RETURN jsonb_build_object('ok',true,'admitted',true,'proof_required',false,
                              'advanced',false,'installation_id',v_actual);
  END IF;
  IF v_current IS NOT NULL AND v_current<>v_actual
     AND v_current_created_at IS NOT NULL AND p_created_at<=v_current_created_at THEN
    RETURN jsonb_build_object('ok',true,'admitted',false,'proof_required',false,
                              'reason','older installation generation');
  END IF;
  IF v_current IS DISTINCT FROM v_actual OR v_current_created_at IS NULL
     OR p_created_at>v_current_created_at THEN
    UPDATE core.installation_account
       SET github_installation_id=v_actual,
           github_installation_created_at=CASE
             WHEN github_installation_id=v_actual
               THEN GREATEST(github_installation_created_at,p_created_at)
             ELSE p_created_at
           END
     WHERE account_id=v_account;
    v_advanced := true;
  END IF;
  IF v_revoked_at IS NOT NULL THEN
    RETURN jsonb_build_object('ok',true,'admitted',false,'proof_required',false,'advanced',v_advanced,
                              'reason','installation generation revoked');
  END IF;
  RETURN jsonb_build_object('ok',true,'admitted',true,'proof_required',false,'advanced',v_advanced,
                            'installation_id',v_actual);
END $$;
ALTER FUNCTION core.admit_event_installation_generation_with_authority(text,timestamptz)
  OWNER TO veripsa_migrator;
REVOKE EXECUTE ON FUNCTION core.admit_event_installation_generation_with_authority(text,timestamptz)
  FROM PUBLIC,veripsa_writer;
GRANT EXECUTE ON FUNCTION core.admit_event_installation_generation_with_authority(text,timestamptz)
  TO veripsa_app;

-- Schema-first rolling bridge.  The prior worker can authenticate the durable delivery but cannot perform the
-- App-JWT installation point-read added with /2, so it supplies no generation proof and an active tombstone stays
-- closed until the new worker retries the durable event.
CREATE OR REPLACE FUNCTION core.reactivate_account_with_authority(p_delivery_key text)
    RETURNS jsonb LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path TO 'core','pg_catalog' AS $$
DECLARE v_result jsonb;
BEGIN
  v_result := core.reactivate_account_with_authority(p_delivery_key,NULL::jsonb);
  IF NOT COALESCE((v_result->>'reactivated')::boolean,false) THEN
    RAISE EXCEPTION 'rolling account reactivation requires new-worker generation proof: %',
      COALESCE(v_result->>'reason','lifecycle authority required') USING ERRCODE='55000';
  END IF;
  RETURN v_result;
END
$$;
ALTER FUNCTION core.reactivate_account_with_authority(text) OWNER TO veripsa_migrator;
REVOKE EXECUTE ON FUNCTION core.reactivate_account_with_authority(text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION core.reactivate_account_with_authority(text) TO veripsa_app;

-- Rolling/offline compatibility: an old caller can supply the delivery through the established transaction-local
-- context.  Old workers ignore this function's JSON result, so a refused activation MUST abort their transaction;
-- returning `reactivated:false` would let the old handler continue and repopulate a tombstoned account.
CREATE OR REPLACE FUNCTION core.reactivate_account_with_authority()
    RETURNS jsonb LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path TO 'core','pg_catalog' AS $$
DECLARE v_result jsonb;
BEGIN
  v_result := core.reactivate_account_with_authority(
    NULLIF(current_setting('core.current_delivery_key',true),''));
  IF NOT COALESCE((v_result->>'reactivated')::boolean,false) THEN
    RAISE EXCEPTION 'legacy account reactivation refused: %',
      COALESCE(v_result->>'reason','lifecycle authority required') USING ERRCODE='55000';
  END IF;
  RETURN v_result;
END
$$;
ALTER FUNCTION core.reactivate_account_with_authority() OWNER TO veripsa_migrator;
REVOKE EXECUTE ON FUNCTION core.reactivate_account_with_authority() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION core.reactivate_account_with_authority() TO veripsa_app;

COMMIT;

-- assert_account_live_with_authority: the per-repo BACKGROUND-WRITER revalidation (audit iter-4 P1, part (b)). A
-- background writer (co-change populate/per-push, self_heal_main_graph, boot-reconcile) takes the ordered repo
-- advisory lock and pins the tenant, then joins the shared account-lifecycle fence used by every live writer.
-- Uninstall/GDPR takes the same fence exclusively. After acquiring those locks + pinning the tenant and BEFORE
-- writing, the writer calls THIS: it resolves the account from the connection context (the SAME identity
-- its gated write would use) and RAISES (fail closed) if a tombstone exists — so a purged tenant (whose account row
-- still EXISTS, so RLS alone would NOT block the write) is not silently re-populated. The writers catch the raise
-- and skip the write content-free. Returns the live account id on success. Identity from the connection (never a
-- caller arg). App-delegation only.
CREATE OR REPLACE FUNCTION core.assert_account_live_with_authority()
    RETURNS text LANGUAGE plpgsql SECURITY DEFINER SET search_path TO 'core','pg_catalog' AS $$
DECLARE v_agent text; v_account text;
BEGIN
  SELECT agent, account INTO v_agent, v_account FROM core.establish_session_write_context() AS c(agent,account);
  IF v_account IS NULL OR v_account = '' THEN
    RAISE EXCEPTION 'account-live check: no resolved account' USING ERRCODE='42501';
  END IF;
  -- The shared xact lock makes this read guard atomic with destructive lifecycle work while allowing ordinary
  -- current-generation live work to proceed beside boot. Long autocommit workflows hold the identical shared
  -- session key around every subsequent statement.
  PERFORM core._take_account_lifecycle_xact_lock_shared(v_account);
  IF EXISTS (
    SELECT 1 FROM core.account_lifecycle_tombstone
     WHERE account_id = v_account AND active
  ) THEN
    -- an uninstall-purged tenant — refuse the write so a background writer cannot repopulate it. Content-free: the
    -- account id is the tenant's own routing key (no path/name/body). 42501 = the gate's "not authorized" class.
    RAISE EXCEPTION 'account-live check: account is uninstall-tombstoned — refusing background write'
      USING ERRCODE='42501';
  END IF;
  RETURN v_account;
END $$;
ALTER FUNCTION core.assert_account_live_with_authority() OWNER TO veripsa_migrator;
REVOKE EXECUTE ON FUNCTION core.assert_account_live_with_authority() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION core.assert_account_live_with_authority() TO veripsa_app;

-- ── MARKETPLACE/BILLING PLAN-EVENT ORDERING (audit iter-5 P2) ────────────────────────────────────────────
-- account_plan_event: the LAST-APPLIED marketplace plan-event timestamp, per account. THE DEFECT it closes:
-- set_account_plan_with_authority is VALUE-idempotent, but GitHub Marketplace webhook delivery is UNORDERED and
-- RE-DELIVERABLE — so a re-delivered `cancelled`(→free) event can land AFTER a later `purchased`(→pro), writing
-- the STALE plan and wrongly throttling a paying customer (a re-erected quota wall = a billing DoS). The graph
-- ingest path already guards exactly this shape with captured_at MONOTONICITY (30_gate.sql ~1124: refuse to
-- overwrite a graph captured at a strictly-newer commit time); this is the COMMERCIAL-path mirror — persist the
-- plan event's effective time and REFUSE a plan write whose event is OLDER than the last applied. Stored in a
-- SEPARATE FK-free table (NOT an ALTER-TABLE column on core.account) on purpose: a new standalone table + a
-- CREATE-OR-REPLACE function take NO lock on the busy core.account table, so this delta applies contention-free
-- on a live, App-loaded prod (the prod-apply discipline). Like account_lifecycle_tombstone / installation_account
-- it is a CROSS-TENANT operational table, written ONLY through the SECURITY DEFINER plan setter (REVOKE ALL from
-- PUBLIC; no per-account RLS — the account_id PK match IS the scope). Content-free: account id + a timestamp only.
CREATE TABLE IF NOT EXISTS core.account_plan_event (
    account_id text PRIMARY KEY,
    last_effective_at timestamptz NOT NULL,
    last_applied_at timestamptz DEFAULT now() NOT NULL
);
DO $$
BEGIN
  IF EXISTS (
    SELECT 1
      FROM pg_class c
      JOIN pg_namespace n ON n.oid = c.relnamespace
     WHERE n.nspname = 'core'
       AND c.relname = 'account_plan_event'
       AND c.relowner <> 'veripsa_migrator'::regrole
  ) THEN
    ALTER TABLE core.account_plan_event OWNER TO veripsa_migrator;
  END IF;
END $$;
REVOKE ALL ON TABLE core.account_plan_event FROM PUBLIC;

-- set_account_plan_with_authority: the App calls this from the GitHub Marketplace `marketplace_purchase` webhook
-- to map a GitHub ACCOUNT's purchased plan onto its Veripsa account's core.account.plan. The plan label then drives
-- core._account_over_quota: the PER-PLAN graph_units HARD wall reads core._plan_graph_units_limit(plan) so each tier
-- is capped at its commercial line (a non-'free' plan no longer skips the wall — it is bounded at its tier), while a
-- 'free' plan additionally keeps the free repos/events caps. One place, no other caller change.
--
-- TENANT-PINNED + GATED (the SAME discipline as enter_installation_with_authority + the other *_with_authority
-- fns): SECURITY DEFINER (runs as the migrator owner), REVOKE-from-PUBLIC + GRANT to veripsa_app ONLY (the App
-- service identity — never a buyer seat), pinned search_path. The account is resolved from the GitHub account id
-- the IDENTICAL way enter_installation_with_authority resolves an installation: 'ACCT-GH-'||<id>. We pin
-- core.current_account to that account FIRST so the account-RLS WITH CHECK admits the row (the account table is
-- FORCE-RLS; an unpinned UPDATE/INSERT sees + writes nothing), then LAZILY PROVISION the account row on first
-- sight (a purchase can arrive before any installation/push for that account — ON CONFLICT DO NOTHING, idempotent)
-- and set its plan. CONTENT-FREE: it stores ONLY the plan LABEL + the account id — never any customer data. The
-- plan string is bounded (left(...,64), matching account_plan_len) and an empty/NULL plan normalizes to 'free'
-- (a 'cancelled' purchase, or a malformed payload, must DOWNGRADE to the free wall, never leave a stale paid plan).
-- Tenant isolation: it writes EXACTLY the one resolved account's row (RLS-walled to current_account) — one
-- account's plan change can never touch another's. Returns the account id (the App logs it; content-free).
-- EVENT-ORDERING (audit iter-5 P2): a trailing p_effective_at (the marketplace event's effective_date, content-
-- free billing metadata) makes the setter MONOTONIC against UNORDERED/RE-DELIVERED webhooks — it REFUSES a plan
-- write whose event is STRICTLY OLDER than the last applied for this account (so a re-delivered cancelled→free
-- can not overwrite a later purchased→pro = wrongly throttle a paying customer). This MIRRORS the graph-ingest
-- captured_at monotonicity below (~line 1124). The guard is GUARDED: inert when p_effective_at is NULL (a writer
-- that does not thread the time, or a payload without one — ingest normally, value-idempotency still holds), and
-- only refuses on a STRICTLY-older event (an equal/newer one applies + advances the high-water mark). Adding the
-- trailing arg via CREATE OR REPLACE would leave the OLD 2-arg overload behind on a re-apply (an ambiguous call
-- for the existing 2-arg callers); DROP it first (mirrors the patch_graph/record_collision signature-change
-- pattern). Function-only DDL → contention-free on a live prod (no lock on the busy core.account table).
DROP FUNCTION IF EXISTS core.set_account_plan_with_authority(text,text);
CREATE OR REPLACE FUNCTION core.set_account_plan_with_authority(p_gh_account_id text, p_plan text, p_effective_at timestamptz DEFAULT NULL)
    RETURNS text LANGUAGE plpgsql SECURITY DEFINER SET search_path TO 'core','pg_catalog' AS $$
DECLARE v_id text; v_account text; v_plan text; v_pinned_account text;
BEGIN
  v_id := left(NULLIF(btrim(COALESCE(p_gh_account_id,'')),''),64);
  IF v_id IS NULL THEN RETURN NULL; END IF;            -- no GitHub account id → nothing to map (clean no-op)
  v_account := 'ACCT-GH-' || v_id;                     -- SAME resolution as enter_installation_with_authority
  -- OWNERSHIP AUTHORITY (moat red-team F1): the GitHub account id arrives in the marketplace_purchase PAYLOAD, so
  -- a validly-SIGNED event (HMAC proves GitHub sent it, NOT that the purchaser owns p_gh_account_id) carrying a
  -- VICTIM's account.id would otherwise flip the VICTIM's plan here — a force-downgrade re-erects the victim's
  -- quota wall (DoS). Re-pin the authority from the CONNECTION, exactly as resolve_session_identity does: the App
  -- pins core.installation_account (a SESSION GUC) per webhook event, but ONLY enter_installation_with_authority —
  -- the trusted route — also WRITES a core.installation_account ROW, so a GENUINELY-routed pin is provably present
  -- in that table (an unrouted/forged raw `SET` is ignored, same defense-in-depth as resolve_session_identity).
  -- INVARIANT: IF this connection resolves to a routed account, the payload's account MUST agree with it — refuse
  -- on MISMATCH (a cross-tenant plan write is impossible). IF there is NO such context (v_pinned_account IS NULL),
  -- ALLOW: a marketplace purchase legitimately PRECEDES any installation (no connection/tenant pinned yet — the
  -- live handler does NOT call enter_installation for a marketplace_purchase, since it carries no
  -- installation/repository/organization, so _event_account_key is NULL and nothing is pinned), and GitHub's HMAC
  -- is the authority for that first purchase. Content-free: the refusal logs/returns nothing but a clean no-op.
  v_pinned_account := NULLIF(current_setting('core.installation_account', true), '');
  IF v_pinned_account IS NOT NULL
     AND EXISTS (SELECT 1 FROM core.installation_account WHERE account_id = v_pinned_account)
     AND v_pinned_account <> v_account THEN
    RAISE WARNING 'set_account_plan: payload account does not match the connection-routed tenant — refused (no-op)';
    RETURN NULL;                                        -- MISMATCH ⇒ refuse: never write another tenant's plan
  END IF;
  -- normalize the plan label to CANONICAL LOWERCASE: empty/NULL (a cancelled purchase or a malformed payload) ⇒
  -- 'free' (downgrade to the wall — never leave a stale paid plan), bounded to the account_plan_len cap so a long
  -- label can never overflow. LOWERCASE is load-bearing: the abuse-wall free-detection (core._account_over_quota)
  -- compares the stored plan to the literal 'free', and the NUDGE meter (core._plan_file_limit) already lowercases
  -- before bucketing — so a label that differs only in CASE (GitHub lists the FREE plan as "Free") must canonicalize
  -- HERE, or the two halves disagree on the same stored value and "Free" wrongly skips the free-tier wall (unlimited
  -- free-subscriber writes — the launch-blocking billing hole). Storage is canonical lowercase from every entry point.
  v_plan := left(lower(COALESCE(NULLIF(btrim(COALESCE(p_plan,'')),''),'free')),64);
  -- EVENT-ORDERING MONOTONICITY (audit iter-5 P2): marketplace webhooks are UNORDERED + RE-DELIVERABLE, so a
  -- re-delivered OLDER event (e.g. a cancelled→free) can arrive AFTER a later one (a purchased→pro). REFUSE to
  -- apply a plan write whose effective time is STRICTLY OLDER than the last one we applied for this account (the
  -- stale event keeps the newer plan, no write happens) — the EXACT shape the graph-ingest captured_at guard
  -- (~line 1124) refuses for reordered pushes. GUARDED: only when p_effective_at is provided AND a strictly-newer
  -- event was already applied. account_plan_event has NO per-account RLS (account_id PK is the scope), so this
  -- read needs no pin. A clean no-op return of the account id (idempotent: the caller sees the account, no error).
  IF p_effective_at IS NOT NULL THEN
    PERFORM 1 FROM core.account_plan_event
      WHERE account_id = v_account AND last_effective_at > p_effective_at;
    IF FOUND THEN
      RAISE WARNING 'set_account_plan: event older than the last applied for this account — refused (stale/reordered redelivery)';
      RETURN v_account;                                  -- STALE ⇒ keep the newer plan; do not regress it
    END IF;
  END IF;
  -- pin THIS account so the FORCE-RLS account table admits the lazy provision + the plan write (and walls the
  -- write to this one account — cross-tenant isolation). is_local=true: txn-local, reverts if the body rolls back.
  PERFORM set_config('core.current_account', v_account, true);
  PERFORM core.mark_governed_write('account');
  INSERT INTO core.account(account_id, display_name) VALUES (v_account, 'gh-account-'||v_id) ON CONFLICT (account_id) DO NOTHING;
  UPDATE core.account SET plan = v_plan WHERE account_id = v_account;
  -- ADVANCE the per-account high-water mark so a LATER re-delivery of an OLDER event is refused above. Only when
  -- p_effective_at is known; keep the MAX (a same-time re-apply or an equal event does not move it backward).
  -- FK-free cross-tenant table (no per-account RLS, no governed-write token) — the account_id PK is the scope.
  IF p_effective_at IS NOT NULL THEN
    INSERT INTO core.account_plan_event(account_id, last_effective_at)
    VALUES (v_account, p_effective_at)
    ON CONFLICT (account_id) DO UPDATE
      SET last_effective_at = GREATEST(core.account_plan_event.last_effective_at, EXCLUDED.last_effective_at),
          last_applied_at = now();
  END IF;
  RETURN v_account;
END $$;
ALTER FUNCTION core.set_account_plan_with_authority(text,text,timestamptz) OWNER TO veripsa_migrator;
REVOKE EXECUTE ON FUNCTION core.set_account_plan_with_authority(text,text,timestamptz) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION core.set_account_plan_with_authority(text,text,timestamptz) TO veripsa_app;
-- BILLING SEAM (least privilege): the SEPARATE web platform's future Marketplace/entitlement handler — after
-- it has VERIFIED the billing authority and resolved (org/install → gh_account_id, plan) — connects as veripsa_billing and
-- calls ONLY this setter to flip the customer's Core plan, so the coverage/abuse gate (_account_over_quota /
-- _plan_file_limit) matches the VERIFIED entitlement. EXECUTE here is its ENTIRE WRITE REACH: the setter is SECURITY
-- DEFINER (runs as the migrator owner — pins the account + arms the governed write internally), so veripsa_billing
-- needs NOTHING else (no table grant, no other authority fn). The App keeps its OWN path (the grant above is
-- unchanged); this ADDS the billing role alongside. Mirrors the platform-reader idiom on the WRITE side. The
-- matching USAGE on schema core is granted once, below.
GRANT EXECUTE ON FUNCTION core.set_account_plan_with_authority(text,text,timestamptz) TO veripsa_billing;
-- veripsa_billing needs USAGE on the schema to reach the setter at all (granted once here; NO table grants, NO
-- other EXECUTE — set_account_plan_with_authority(text,text,timestamptz) + the _for_installation variant below are
-- its entire reachable surface, the WRITE-side mirror of the platform reader's two read fns).
GRANT USAGE ON SCHEMA core TO veripsa_billing;

-- set_account_plan_for_installation_with_authority: the SAME billing plan-set, keyed by the GitHub App INSTALLATION
-- id instead of the numeric GitHub ACCOUNT id. THE NEED: the separate web platform holds the org's *installation* id
-- (what GitHub hands it when the org installs the App + what every webhook delivery carries), NOT the numeric account
-- id that 'ACCT-GH-'||<id> is built from. Core OWNS the authoritative installation→account map (core.installation_account,
-- written ONLY by the trusted enter_installation_with_authority route), so Core — not the web — must resolve which
-- account an installation bills. The web verifies its purchase, resolves (org → installation_id, plan), connects as
-- veripsa_billing, and calls THIS; Core resolves the account and flips its plan.
--
-- RESOLUTION (authoritative, the IDENTICAL read enter_installation_with_authority does at line 43): bound the id the
-- SAME way (left(NULLIF(btrim(...)),64) = installation_id_len) then SELECT account_id FROM core.installation_account
-- WHERE installation_id = <bounded> AND revoked_at IS NULL. This is a billing op, NOT onboarding: an UNKNOWN or
-- REVOKED installation (no live row) is a clean no-op — return NULL and MINT NOTHING. We deliberately do NOT call
-- enter_installation_with_authority, which would LAZILY PROVISION a phantom 'ACCT-GH-'||<installation_id> account
-- + map row for an id we cannot bill (a purchase must never conjure a tenant). ONLY a genuinely-installed, live org
-- — one enter_installation_with_authority has already routed, so a real unrevoked row exists — is billable here.
--
-- SINGLE SETTER PATH: once resolved we strip the 'ACCT-GH-' prefix back to the gh_account_id and DELEGATE to
-- set_account_plan_with_authority(gh_account_id, plan, effective_at) — so EVERY plan write goes through ONE gated
-- code path: the #401 lower(btrim(plan)) canonicalization (capital 'Pro' → 'pro', so the _account_over_quota /
-- _plan_file_limit free-detection agrees on the stored value), the P2 event-ordering monotonicity, the
-- current_account pin, mark_governed_write('account'), the lazy account-row provision + the UPDATE core.account.plan,
-- AND the F1 ownership-authority re-pin all run there, unchanged and untouched (the delegated gh_account_id
-- reconstructs the EXACT account this installation resolved to, so any connection-routed tenant check agrees).
-- Returns the resolved account_id, or NULL when the installation is unknown/revoked. Same discipline as its sibling:
-- SECURITY DEFINER (migrator owner), pinned search_path, REVOKE PUBLIC + GRANT the App and the billing role. The trailing
-- p_effective_at (audit iter-5 P2) is threaded into the delegated setter so the installation-keyed billing path gets
-- the SAME ordering protection; DROP the old 2-arg overload first (signature-change pattern). CONTENT-FREE: it
-- reads/returns only the installation id, the plan LABEL, the effective time, and the account id.
DROP FUNCTION IF EXISTS core.set_account_plan_for_installation_with_authority(text,text);
CREATE OR REPLACE FUNCTION core.set_account_plan_for_installation_with_authority(p_installation_id text, p_plan text, p_effective_at timestamptz DEFAULT NULL)
    RETURNS text LANGUAGE plpgsql SECURITY DEFINER SET search_path TO 'core','pg_catalog' AS $$
DECLARE v_inst text; v_account text; v_gh_id text;
BEGIN
  v_inst := left(NULLIF(btrim(COALESCE(p_installation_id,'')),''),64);   -- SAME bounding as enter_installation_with_authority
  IF v_inst IS NULL THEN RETURN NULL; END IF;            -- no installation id → nothing to map (clean no-op)
  -- AUTHORITATIVE live resolution. LAZILY PROVISION ON PURPOSE OMITTED: an unknown/revoked installation is NOT minted
  -- or billed here (billing, not onboarding).
  SELECT account_id INTO v_account FROM core.installation_account
   WHERE installation_id = v_inst AND revoked_at IS NULL;
  IF v_account IS NULL THEN                              -- UNKNOWN/REVOKED installation ⇒ unresolved no-op
    -- content-free + SILENT: an unknown or revoked installation is a ROUTINE billing condition (a purchase webhook
    -- may name an org that has not installed the App, or one that uninstalled/suspended before apply), NOT a security
    -- event — return a clean NULL and mint/change nothing. (Contrast the sibling's RAISE WARNING, which fires on a
    -- cross-tenant MISMATCH — a genuine refusal worth surfacing.)
    RETURN NULL;
  END IF;
  -- resolved → strip 'ACCT-GH-' back to the gh_account_id and DELEGATE to the ONE plan-set path (which re-derives the
  -- SAME 'ACCT-GH-'||<id> account, lower()-normalizes the plan, applies the P2 ordering guard, pins current_account,
  -- arms the governed write, and writes core.account.plan). left(8) drops the literal 'ACCT-GH-' prefix that
  -- enter_installation_with_authority sets; p_effective_at flows through so the installation path is ordered too.
  v_gh_id := substr(v_account, 9);                        -- 'ACCT-GH-' is 8 chars; the remainder is the gh_account_id
  RETURN core.set_account_plan_with_authority(v_gh_id, p_plan, p_effective_at);
END $$;
ALTER FUNCTION core.set_account_plan_for_installation_with_authority(text,text,timestamptz) OWNER TO veripsa_migrator;
REVOKE EXECUTE ON FUNCTION core.set_account_plan_for_installation_with_authority(text,text,timestamptz) FROM PUBLIC;
-- the App keeps a path; the billing role (the web platform's entitlement seam) gets the installation-keyed setter alongside
-- the account-keyed one — both are SECURITY DEFINER + delegate to the single write path, so EXECUTE is the whole reach.
GRANT EXECUTE ON FUNCTION core.set_account_plan_for_installation_with_authority(text,text,timestamptz) TO veripsa_app;
GRANT EXECUTE ON FUNCTION core.set_account_plan_for_installation_with_authority(text,text,timestamptz) TO veripsa_billing;

-- _claim_lease_at: the lease backstop for a freshly granted/refreshed claim — now() + a BOUNDED, owner-tunable
-- window. The lease is how long a stalled (un-heartbeated) holder keeps the lane before expire_stale_claims
-- reclaims it for the next car in line. An operator tunes 'lease_minutes' via set_policy_with_authority to fit
-- their fleet's heartbeat cadence, but it is CLAMPED to a safe frame (5..1440 min) via core._policy_int — never
-- 0 (instant reclaim of a live holder) or 10 years (a dead session squats the lane forever). Default 30 min.
-- Tenant-pinned: _policy_int reads core.current_account, which the gate fns pin before every call site here.
-- (core._policy_int lives in 40_surfaces.sql, applied AFTER this module; plpgsql resolves it at call time, by
-- which point both exist — same late-binding the gate already relies on for its other cross-module callees.)
CREATE OR REPLACE FUNCTION core._claim_lease_at() RETURNS timestamptz
    LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path TO 'core','pg_catalog' AS $$
BEGIN
  RETURN now() + make_interval(mins => core._policy_int('lease_minutes', 30, 5, 1440));
END $$;
ALTER FUNCTION core._claim_lease_at() OWNER TO veripsa_migrator;
-- INTERNAL ONLY: a lease helper called inside the migrator-owned gate fns. Postgres grants EXECUTE to PUBLIC by
-- default — revoke it (mirrors _place_claim). No buyer role needs it directly.
REVOKE ALL ON FUNCTION core._claim_lease_at() FROM PUBLIC;

-- ── THE HARD FREE-TIER WALL — an ENFORCED per-account quota at the gate (the single write path). ───────────
-- Marginal storage is cheap, but UNBOUNDED abuse (many accounts × infinite pushes) is still unbounded → it
-- fills the small DB tier and costs money, and a manual purge can never keep up with automated abuse. The free
-- caps already exist as TUNABLE owner policy (free_max_repos / free_max_graph_units / free_max_events, read
-- through core._free_line in 95_owner.sql) but were only DISPLAYED on the owner lens; here they become a WALL.
-- The DB-growing gate fns (ingest_graph · patch_graph · record_push · record_landing) call _account_over_quota
-- at the top and, if the account is over the line on ANY dimension, REFUSE the write (insert nothing) and return
-- a structured quota_exceeded result the App detects + handles gracefully. It is ADVISORY — it NEVER raises (a
-- refusal is a clean signal, not a crash). Claims (declare_claim / act_for_claim) are EXEMPT: they are transient,
-- bounded live state (released on land, expired on lease) and are the core lock/serialize product — walling them
-- would break coordination for a legit team that merely outgrew the GRAPH cap, while never being an abuse vector
-- (a claim row is reclaimed automatically). The cap applies to ALL accounts,
-- including future paid plans; each plan has a finite graph-units ceiling.
--
-- THE CAP NUMBERS are TUNABLE owner policy and live in ONE place — the owner
-- module in 95_owner.sql (this gate only READS + ENFORCES it, the single source of truth).
-- They must be GENEROUS enough that a real small team never hits them and a WALL for abuse. The TWO caps that
-- actually bound STORAGE are graph_units + events; repo COUNT is a weak proxy (an empty repo costs ~nothing), so
-- it is a HIGH sanity ceiling, not the storage wall. The default free posture:
-- free_max_repos 1000 (a sanity ceiling — never walls a legit org, kept well above the App's own onboard fan-out _ONBOARD_REPO_CAP),
-- free_max_graph_units ~200000 (≈ a few hundred-file repos — the primary cap), free_max_events ~50000. The owner
-- sets free-line values with one call each, e.g.
--   SELECT core.set_free_line_with_authority('free_max_repos',1000);
--   SELECT core.set_free_line_with_authority('free_max_graph_units',200000);
--   SELECT core.set_free_line_with_authority('free_max_events',50000);
-- (The _free_line() literals are the unset-fallback DEFAULTS, owned by 95_owner.sql; this enforcement reads the
-- EFFECTIVE clamped line, so a tuned value or the default both flow through here unchanged — no number is
-- duplicated in this file, so the line can never drift between display and enforcement.)
--
-- Future Marketplace billing sets the account plan through the billing seam above.
-- _account_over_quota then enforces that plan's finite graph-units limit; there
-- is no unlimited paid bypass in the default launch posture.

-- _quota_exceeded_result: the ONE structured sentinel every DB-growing gate fn returns when it refuses a write
-- over the free line. A single shape so the App (github-app/server.py) has ONE thing to detect. Content-free
-- (the over dimension + the cap/usage numbers — never a path/branch/repo). IMMUTABLE (pure shape builder).
CREATE OR REPLACE FUNCTION core._quota_exceeded_result(p_dimension text, p_limit bigint, p_usage bigint) RETURNS jsonb
    LANGUAGE sql IMMUTABLE AS $$
  SELECT jsonb_build_object(
    'ok', false,
    'quota_exceeded', true,
    'dimension', p_dimension,     -- 'repos' | 'graph_units' | 'events'
    'limit', p_limit,
    'usage', p_usage,
    'reason', 'early-access limit reached for this account — write refused (advisory; reduce scope or contact support)')
$$;
ALTER FUNCTION core._quota_exceeded_result(text,bigint,bigint) OWNER TO veripsa_migrator;
REVOKE ALL ON FUNCTION core._quota_exceeded_result(text,bigint,bigint) FROM PUBLIC;

-- PER-PAYLOAD INBOUND GRAPH CAP (audit:scale — the DB is the authoritative boundary). The free-tier quota
-- check (_account_over_quota) is a PRE-WRITE "are you ALREADY over?" test — it has NO per-payload ceiling, so
-- a SINGLE ingest_graph / patch_graph call can ship an arbitrarily large {nodes:[…],edges:[…]} and overshoot
-- free_max_graph_units in ONE write (the over-quota test only fires on the NEXT call, after the footprint is
-- already inflated). On the buyer's OWN-WRITER `--push` path the App-side tarball/file caps do NOT apply
-- (the buyer runs code_graph_extract directly against their DSN) — so the DB must bound the inbound array
-- lengths itself, exactly as _ranges_from_jsonb caps the inbound ranges array at 512. ABOVE the cap we REFUSE
-- the whole call (insert nothing) and return this bounded sentinel — content-free (the cap + the over-cap
-- count of array ELEMENTS, never a path/name/body). It carries `quota_exceeded:true` so the App's EXISTING
-- refusal detector (server._quota_result) stops ingesting for the account with no App change, plus an explicit
-- `payload_too_large:true` + `dimension:'graph_payload'` so the distinction (a per-call cap, not an account
-- footprint) is honest in the logs. Cheap O(1) jsonb_array_length checks before any DELETE/INSERT touches a row.
CREATE OR REPLACE FUNCTION core._graph_too_large_result(p_limit bigint, p_elements bigint) RETURNS jsonb
    LANGUAGE sql IMMUTABLE AS $$
  SELECT jsonb_build_object(
    'ok', false,
    'quota_exceeded', true,        -- reuse the App's existing refusal detector (server._quota_result) — no App change
    'payload_too_large', true,     -- but mark this as a PER-CALL payload cap, distinct from the account-footprint quota
    'dimension', 'graph_payload',  -- content-free: the over dimension (NOT a path/name/body)
    'limit', p_limit,              -- the per-call element ceiling (nodes OR edges)
    'usage', p_elements,           -- the offending inbound array length (content-free: a count)
    'reason', 'inbound graph payload exceeds the per-call element cap — write refused (advisory; split the ingest)')
$$;
ALTER FUNCTION core._graph_too_large_result(bigint,bigint) OWNER TO veripsa_migrator;
REVOKE ALL ON FUNCTION core._graph_too_large_result(bigint,bigint) FROM PUBLIC;

-- _account_over_quota: compare an account's CURRENT footprint to the free line; return the OVER dimension (or
-- NULL when under the line = allow). Assumes the caller has ALREADY pinned core.current_account to p_account
-- (every gate write fn does, via establish_session_write_context) so the RLS-walled reads below see THIS
-- account's rows only — the same per-tenant-isolation read pattern owner_cost_surface uses.
--
-- EFFICIENT (never a count(*) over the huge node/edge tables on every write):
--   • repos       = count(DISTINCT repo) over graph_version (ONE small row per (repo,branch) — bounded by the
--                   account's repo×branch count, not by graph size).
--   • graph_units = SUM(node_count + edge_count) over graph_version (the ALREADY-STORED per-coordinate sums —
--                   the ingest maintains them; no scan of code_node/code_edge).
--   • events      = a BOUNDED cap-probe: EXISTS(SELECT 1 FROM event … OFFSET cap) — true iff there are MORE than
--                   `cap` rows, so we read at most cap+1 rows, NEVER count the whole (largest, append-only) table.
--                   The exact total is irrelevant — only "is it past the cap" decides the wall. (cap+1 capped at
--                   a sane ceiling so a fat-fingered 10^9 cap can't make the probe itself expensive.)
--
-- FAIL-SAFE: ANY error in the check (a missing dependency, a transient read failure, anything) is swallowed and
-- the fn returns NULL = UNDER the line = ALLOW. A bug in the quota check must NEVER break ingestion for everyone
-- (a wrongly-refused legit write is far worse than a missed cap; the owner lens still SEES every over-line account
-- and a manual purge remains the backstop). The wall is a cost guard, not a correctness gate — it fails OPEN.
--
-- DEV-ONLY EXEMPTION (the PO's own dogfood/demo accounts): the FIRST thing this fn checks (before the plan read /
-- the wall) is whether p_account is on the owner-configurable dev allowlist (core._dev_exempt_account_ids,
-- 95_owner.sql) → exempt accounts RETURN NULL = allow. This is a DEV/OWNER convenience, explicitly NOT a product
-- tier. CRUCIALLY it is FAIL-CLOSED (the OPPOSITE direction to the outer handler): the membership test sits in its
-- OWN sub-block, so a broken allowlist read leaves the account NON-exempt = the wall stays enforced. An exemption
-- is granted ONLY by a successful read that positively lists the id — a read failure can never open the wall.
--
-- PER-PLAN graph_units HARD WALL (Marketplace billing): graph_units is the COMMERCIAL line, capped PER PLAN for
-- EVERY tier via core._plan_graph_units_limit(plan) — free 6000 · starter 30000 · pro 80000 · scale 200000 ·
-- enterprise 500000 (the for-now cost-safety ceiling; unmapped → 500000). A non-'free' plan is NO LONGER an
-- unconditional bypass — it is bounded at its tier. The plan is read from core.account.plan (set by the
-- marketplace_purchase webhook → set_account_plan_with_authority; 'free' is the DEFAULT). graph_units is single-
-- sourced from _plan_graph_units_limit for ALL plans, so _free_line's graph_units is superseded for the wall;
-- _free_line still drives the repos/events caps, applied to FREE accounts only. One place, no caller changes.
--
-- FAIL-SAFE DIRECTION (the only direction that matters here): the plan read runs in its OWN sub-block — ANY error
-- reading it (a transient failure, a missing row) falls through to v_plan='free' = the STRICTEST line. And the
-- per-plan-LINE read itself fails toward ENFORCEMENT (_plan_graph_units_limit returns 3000 on error, NEVER
-- unlimited — see 95_owner.sql), so a bug can only ever wall an account too tightly (recoverable: the PO raises
-- the line), NEVER open the cost wall (the abuse hole). The OUTER EXCEPTION still fails OPEN for DoS-safety.
-- Reads THIS account's own row only (RLS): the caller has already pinned core.current_account to p_account (every
-- gate write fn does, via establish_session_write_context) — the same per-tenant-isolation read the cap math below
-- relies on — so one tenant's plan can never be read for, or applied to, another (cross-tenant isolation holds).
CREATE OR REPLACE FUNCTION core._account_over_quota(p_account text) RETURNS text
    LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path TO 'core','pg_catalog' AS $$
DECLARE
  v_free jsonb; v_max_repos bigint; v_max_events bigint;
  v_repos bigint; v_units bigint; v_probe bigint; v_events_over boolean;
  v_plan text; v_is_free boolean; v_exempt boolean;
BEGIN
  -- ── DEV-ONLY EXEMPTION (the PO's own dogfood/demo accounts) — checked BEFORE the wall, FAIL-CLOSED. ──────────
  -- The PO's dogfood/demo accounts (core._dev_exempt_account_ids in 95_owner.sql — an ADJUSTABLE owner-policy
  -- allowlist, default the two real GitHub installs + ACCT-DEMO) bypass the wall entirely so Veripsa can index its
  -- OWN Core repeatedly (the dogfood loop, which 实测 blows past any free line on the FIRST ingest). This is a
  -- DEV/OWNER convenience, explicitly NOT a product tier (a tier is core.account.plan; this is a named-id list).
  -- *** FAIL-CLOSED: the membership test runs in its OWN sub-block — ANY error reading the allowlist leaves
  -- v_exempt=false = NOT exempt = the wall STAYS ENFORCED (the inverse of the OUTER handler's fail-OPEN). An
  -- account is exempt ONLY via a SUCCESSFUL read that POSITIVELY lists it; a broken read NEVER opens the wall. ***
  -- Exact-match against the comma-split list (the same membership a CSV allowlist implies; ids are trimmed by the
  -- setter on write). On a match we RETURN NULL = allow, short-circuiting every cap below.
  v_exempt := false;
  BEGIN
    SELECT p_account = ANY (string_to_array(core._dev_exempt_account_ids(), ',')) INTO v_exempt;
  EXCEPTION WHEN OTHERS THEN
    v_exempt := false;   -- FAIL-CLOSED: a broken allowlist read is NOT an exemption — keep the wall on
  END;
  IF v_exempt THEN RETURN NULL; END IF;   -- dev/dogfood account → bypass the wall (allow)

  -- PLAN READ (default-deny): read the plan in its own sub-block so ANY read error falls through to 'free' = the
  -- STRICTEST treatment (fail toward ENFORCEMENT, never toward a free bypass). RLS scopes the read to THIS account
  -- (current_account is pinned by the caller). CASE-INSENSITIVE 'free' detection (defense-in-depth): the setter
  -- now stores the plan canonically lowercased, but a value written before that fix (e.g. "Free" — GitHub lists
  -- the FREE plan with a capital F) could still sit in core.account.plan; lower(...) re-arms the wall for any
  -- already-stored capitalized 'free' (a case-SENSITIVE compare would treat "Free" as paid).
  BEGIN
    SELECT plan INTO v_plan FROM core.account WHERE account_id = p_account;
  EXCEPTION WHEN OTHERS THEN
    v_plan := 'free';   -- a broken plan read must NEVER weaken the wall — keep enforcing the strictest line
  END;
  IF v_plan IS NULL THEN v_plan := 'free'; END IF;   -- no row ⇒ free (strictest)
  v_is_free := (lower(v_plan) = 'free');

  -- ── GRAPH_UNITS — the HARD COMMERCIAL QUOTA WALL, ENFORCED FOR EVERY PLAN (the #95→per-plan change). ─────────
  -- BEFORE: a non-'free' plan returned NULL here unconditionally = UNLIMITED on every dimension (the paid bypass).
  -- NOW: graph_units is capped PER PLAN at its line via core._plan_graph_units_limit(plan) — free 6000 · starter
  -- 30000 · pro 80000 · scale 200000 · enterprise 500000 (the for-now cost-safety ceiling; unmapped → 500000;
  -- a broken line read fails toward 6000, NEVER unlimited — see _plan_graph_units_limit in 95_owner.sql). So a
  -- PREVIOUSLY-UNLIMITED paid account is now bounded at its tier. graph_units = the ALREADY-STORED per-coordinate
  -- sums (the ingest maintains node_count/edge_count; no scan of code_node/code_edge). This SUPERSEDES _free_line's
  -- graph_units for the wall on ALL plans — _free_line still drives repos/events for FREE accounts (below); the
  -- graph_units axis is single-sourced from _plan_graph_units_limit so the two can never double-enforce/disagree.
  SELECT COALESCE(sum(COALESCE(node_count,0)+COALESCE(edge_count,0)),0)
    INTO v_units
    FROM core.graph_version WHERE account_id = p_account;
  IF v_units > core._plan_graph_units_limit(v_plan) THEN RETURN 'graph_units'; END IF;

  -- ── FREE-TIER repos + events — the existing storage guards, FREE ACCOUNTS ONLY (unchanged). ─────────────────
  -- A paid plan is bounded ONLY by its graph_units line above (repos is a weak proxy; events scale with paid use);
  -- these two free-tier caps come from _free_line as before and apply solely when the account is on 'free'. (A
  -- per-plan repos/events line can drop in later the same way graph_units did; not needed now.)
  IF v_is_free THEN
    v_free       := core._free_line();
    v_max_repos  := (v_free->>'max_repos')::bigint;
    v_max_events := (v_free->>'max_events')::bigint;

    -- repos from the SMALL, already-maintained per-coordinate version rows (no huge-table scan).
    SELECT COALESCE(count(DISTINCT repo),0) INTO v_repos FROM core.graph_version WHERE account_id = p_account;
    IF v_repos > v_max_repos THEN RETURN 'repos'; END IF;

    -- events: a BOUNDED over-cap probe — read at most (cap+1) rows, never count the whole append-only ledger. The
    -- probe ceiling guards the cost of the probe itself if the cap is huge (the wall still fires on real abuse).
    v_probe := LEAST(v_max_events, 10000000);
    SELECT EXISTS (SELECT 1 FROM core.event WHERE account_id = p_account OFFSET v_probe LIMIT 1) INTO v_events_over;
    IF v_events_over THEN RETURN 'events'; END IF;
  END IF;

  RETURN NULL;   -- under the line on every applicable axis → allow
EXCEPTION WHEN OTHERS THEN
  -- OUTER FAIL-SAFE (DoS-safety, DELIBERATELY OPEN — unchanged): a broken check must never wall off ingestion for
  -- EVERY tenant on a bug (a wrongly-refused legit write across the whole fleet is far worse than a missed cap;
  -- the owner lens still SEES every over-line account + a manual purge backstops). This is the only place the wall
  -- fails OPEN. The narrower per-plan-LINE read fails toward ENFORCEMENT in its OWN fn (_plan_graph_units_limit
  -- returns 3000, never unlimited, on error) — so a line-read bug walls tightly, while a TOTAL check failure here
  -- still admits writes rather than DoS the product. Two different failure scopes, two correct directions.
  RETURN NULL;
END $$;
ALTER FUNCTION core._account_over_quota(text) OWNER TO veripsa_migrator;
-- INTERNAL ONLY: called inside the migrator-owned gate fns (it trusts the caller's account pin). Revoke PUBLIC.
REVOKE ALL ON FUNCTION core._account_over_quota(text) FROM PUBLIC;

-- _ledger_write_blocked: the SAME free-tier wall, for the APPEND-ONLY LEDGER side-effect writers that the four
-- primary gate fns do NOT cover. The #95 wall sat on ingest_graph / patch_graph / record_push / record_landing,
-- and exempted the CLAIM coordination path (claims are transient + bounded + recycle — correctly exempt). But the
-- claim path and the PR-analysis path EMIT append-only EVENT/STATEMENT rows as a SIDE EFFECT — record_collision
-- (called from declare/act_for on every contended lane), record_warn / record_prediction / record_advice_outcome
-- (per PR open/sync/close), record_pr_failing, record_statement. Those rows land in core.event / core.statement —
-- the UNBOUNDED, append-only ledger the free_max_events cap exists to bound — yet went through NO quota check. An
-- account already OVER its events cap could keep growing the largest table through these unwalled paths (worst
-- case record_collision, whose id is md5(random()) = a FRESH row every call): the events dimension was evadable.
--
-- This guard closes that gap with the IDENTICAL discipline as the primary wall: reuse _account_over_quota (the
-- bounded cap-probe — never a count(*) over the ledger), block ONLY the EVENTS dimension (these writers grow the
-- event/statement ledger, never graph_units/repos — those are walled at ingest already, and blocking a warn on a
-- graph_units overage would be the wrong axis), and FAIL-SAFE: any error → false = ALLOW (a broken guard must
-- never silence legit telemetry; absence of proof of over-quota is allow, same fail-OPEN as #95). The CLAIM /
-- coordination writes themselves still proceed unconditionally (the lock product never stops); only the unbounded
-- LEDGER side-effect they emit is skipped over the cap. ADVISORY — the callers PERFORM/skip, never RAISE.
CREATE OR REPLACE FUNCTION core._ledger_write_blocked(p_account text) RETURNS boolean
    LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path TO 'core','pg_catalog' AS $$
BEGIN
  RETURN core._account_over_quota(p_account) = 'events';   -- over the EVENTS cap → block the ledger append
EXCEPTION WHEN OTHERS THEN
  RETURN false;   -- FAIL-SAFE: a broken guard must never silence legit telemetry (fail OPEN, same as #95)
END $$;
ALTER FUNCTION core._ledger_write_blocked(text) OWNER TO veripsa_migrator;
-- INTERNAL ONLY: called inside the migrator-owned record fns (it trusts the caller's account pin). Revoke PUBLIC.
REVOKE ALL ON FUNCTION core._ledger_write_blocked(text) FROM PUBLIC;

-- _refuse_if_over_quota: the ONE line each DB-growing gate fn runs after pinning identity — returns the structured
-- quota_exceeded jsonb (with the live cap/usage for the over dimension) when over the line, else NULL (proceed).
-- Keeps the enforcement identical + content-free across all four write fns (no copy-paste of the cap math).
CREATE OR REPLACE FUNCTION core._refuse_if_over_quota(p_account text) RETURNS jsonb
    LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path TO 'core','pg_catalog' AS $$
DECLARE v_dim text; v_free jsonb; v_limit bigint; v_usage bigint; v_plan text;
BEGIN
  v_dim := core._account_over_quota(p_account);
  IF v_dim IS NULL THEN RETURN NULL; END IF;     -- under the line → proceed
  v_free := core._free_line();
  IF v_dim = 'repos' THEN
    v_limit := (v_free->>'max_repos')::bigint;
    SELECT COALESCE(count(DISTINCT repo),0) INTO v_usage FROM core.graph_version WHERE account_id = p_account;
  ELSIF v_dim = 'graph_units' THEN
    -- graph_units is the PER-PLAN HARD line now (NOT _free_line) — report the SAME limit the wall enforced, so the
    -- structured result is honest for a paid tier (e.g. a 'pro' account walled at 80000, not the free 6000). Read
    -- the plan the same default-deny way the wall does (any error ⇒ 'free' ⇒ the strictest line is reported).
    BEGIN
      SELECT plan INTO v_plan FROM core.account WHERE account_id = p_account;
    EXCEPTION WHEN OTHERS THEN v_plan := 'free'; END;
    v_limit := core._plan_graph_units_limit(COALESCE(v_plan,'free'));
    SELECT COALESCE(sum(COALESCE(node_count,0)+COALESCE(edge_count,0)),0) INTO v_usage FROM core.graph_version WHERE account_id = p_account;
  ELSE  -- events: report the cap as the limit; usage is "> cap" (we never counted it exactly, by design)
    v_limit := (v_free->>'max_events')::bigint;
    v_usage := v_limit;   -- a content-free "at/over the cap" marker (the exact total is deliberately not computed)
  END IF;
  RETURN core._quota_exceeded_result(v_dim, v_limit, v_usage);
END $$;
ALTER FUNCTION core._refuse_if_over_quota(text) OWNER TO veripsa_migrator;
REVOKE ALL ON FUNCTION core._refuse_if_over_quota(text) FROM PUBLIC;

-- _promote_next_waiter: on-ramp metering — if a lane has NO active holder, promote its OLDEST waiting
-- claim to active (FIFO, the next car in line gets the lane). No-op if the lane is still held or empty.
-- Returns the promoted agent_id (NULL if none). Account already pinned by the caller.
CREATE OR REPLACE FUNCTION core._promote_next_waiter(p_account text, p_repo text, p_branch text, p_path text) RETURNS text
    LANGUAGE plpgsql SECURITY DEFINER SET search_path TO 'core','pg_catalog' AS $$
DECLARE v_next text; v_agent text;
BEGIN
  IF EXISTS (SELECT 1 FROM core.claim WHERE account_id=p_account AND repo=p_repo AND branch=p_branch
              AND target_path=p_path AND claim_state='active') THEN
    RETURN NULL;  -- lane still taken; nobody is promoted
  END IF;
  SELECT claim_id, agent_id INTO v_next, v_agent FROM core.claim
   WHERE account_id=p_account AND repo=p_repo AND branch=p_branch AND target_path=p_path AND claim_state='waiting'
   ORDER BY claimed_at ASC LIMIT 1;
  IF v_next IS NULL THEN RETURN NULL; END IF;
  PERFORM core.mark_governed_write('claim');
  UPDATE core.claim SET claim_state='active', claimed_at=now(), heartbeat_at=now(),
         lease_expires_at=core._claim_lease_at()
   WHERE account_id=p_account AND repo=p_repo AND claim_id=v_next;   -- claim_id is per-repo: pin the repo too
  RETURN v_agent;
END $$;
ALTER FUNCTION core._promote_next_waiter(text,text,text,text) OWNER TO veripsa_migrator;

-- expire_stale_claims: sweep crashed claims (lease lapsed, no heartbeat) before any lock read/decision,
-- so a dead session's footprint is never shown as a live edit / phantom collision. AND auto-promote the
-- next waiter on every lane that just freed (highway: an accident clears, the next car moves up — no
-- infinite wait). Account already pinned. Returns the number of stale holders swept.
--
-- LEASE×PR-LIFETIME (audit fix — the false-clear / lost-serialize this closes): the lease is the backstop for
-- a holder that went SILENT — a crashed session, a dropped 'closed' webhook, an abandoned PR. But a REAL PR can
-- stay OPEN for DAYS, and NOTHING renews a quiet open PR's lease between pushes (only a synchronize event — via
-- _place_claim's self-heartbeat — or an App restart's backfill_open_prs does). So a genuinely-live holder that
-- nobody pushed to for the lease window had its 'active' claim flipped to 'expired' AND its waiter promoted →
-- the still-open, still-in-flight collision SILENTLY stopped being held: the promoted PR was told 'clear, land
-- freely' while the original PR was still open and would clobber it (a false clear = a LOST SERIALIZE, the exact
-- silent miss Veripsa sells against). The lease could not tell 'session crashed' (reclaim is right) from 'PR is
-- open but quiet' (reclaim must NOT drop the collision).
--
-- THE FIX (recall-first, content-free, anti-squat preserved): a lapsed holder STILL yields its LANE — the
-- waiter is still promoted, so a genuinely-dead holder never blocks a live PR forever (the lease's whole point).
-- But the swept holder is NOT silently terminated when a waiter takes its place: if a waiter was promoted onto
-- the lane (= a real serialize was in play, someone is still in line for THIS path), the lapsed holder is
-- RE-QUEUED as a 'waiting' claim (its original claimed_at preserved, so it is the OLDEST waiter and is promoted
-- back FIRST when the lane next clears — see the timestamp note below) instead of left 'expired'. The collision
-- therefore stays SURFACED and serialized (recall held) — it is never silently dropped — and the next push from
-- EITHER side reconciles it through the normal _place_claim path (the same correct re-queue a live resync already
-- produces). When there is NO waiter on the freed lane (nobody is being falsely cleared), the
-- holder stays 'expired' exactly as before: the lane is genuinely free, nothing is dropped, and a re-push
-- reactivates the row idempotently. So the change is surgical — it only fires where a real collision would
-- otherwise vanish, and it reuses the FIFO re-queue semantics the resync path already proves sound.
CREATE OR REPLACE FUNCTION core.expire_stale_claims() RETURNS integer
    LANGUAGE plpgsql SECURITY DEFINER SET search_path TO 'core','pg_catalog' AS $$
DECLARE v_account text; v_n integer := 0; r record; v_promoted text;
BEGIN
  v_account := current_setting('core.current_account', true);
  IF v_account IS NULL OR v_account = '' THEN RETURN 0; END IF;
  PERFORM core.mark_governed_write('claim');
  FOR r IN
    -- sweep the lapsed holders, but CARRY each one's identity (claim_id) so a still-open holder can be re-queued
    -- behind the waiter it is yielding to, instead of silently vanishing while its PR is still open.
    WITH expired AS (
      UPDATE core.claim SET claim_state='expired', released_at=now()
       WHERE account_id=v_account AND claim_state='active' AND lease_expires_at < now()
      RETURNING claim_id, repo, branch, target_path)
    SELECT claim_id, repo, branch, target_path FROM expired
  LOOP
    v_n := v_n + 1;
    -- promote the next car in line (anti-squat: a freed lane never strands its queue).
    v_promoted := core._promote_next_waiter(v_account, r.repo, r.branch, r.target_path);
    -- RECALL GUARD: a waiter just took the lane → a real serialize was in play for THIS path. The lapsed holder's
    -- PR may still be OPEN (lease lapse ≠ PR closed — only land/withdraw concludes a change). Re-queue it as a
    -- WAITER so the collision stays held + surfaced, never silently cleared. No waiter promoted → leave it
    -- 'expired' (the lane is truly free; nothing is being falsely cleared; a re-push reactivates the row idempotently).
    --
    -- BOTH TIMESTAMPS ARE DELIBERATELY PRESERVED (the UPDATE flips only the state):
    --   • lease_expires_at stays IN THE PAST. The re-queued holder is NOT handed a fresh lease — that would dress a
    --     possibly-dead PR up as freshly-alive. Keeping it lapsed makes the row SELF-HEALING: if the promoted waiter
    --     later LANDS, this oldest waiter is promoted back to 'active' still lapsed, so the very next sweep evaluates
    --     it again (re-queue if another waiter exists, else 'expired') — a genuinely-dead holder converges to gone
    --     without ever silently clearing a live one. A real resync renews it via _place_claim's heartbeat, like any
    --     other lapsed-but-alive PR. (The 'abandoned' operator surface is lane-centric — it already excludes a lapsed
    --     row while another LIVE holder works that lane — so this re-queue changes its output for NEITHER a live nor a
    --     dead holder vs. the old 'expired' path: no operator-visibility regression, by construction.)
    --   • claimed_at is UNCHANGED, so the honest "in line since" age is preserved AND FIFO is honored (it was the
    --     original holder = the oldest, so when the promoted waiter lands it is promoted FIRST — correct order).
    -- NO OSCILLATION: the promoted waiter is now 'active' with a FRESH lease, so _promote_next_waiter (which only
    -- promotes when a lane has NO active holder) never re-promotes this re-queued row while that waiter holds the
    -- lane; the sweep itself only ever touches 'active' rows, never this 'waiting' one. Stable in one pass.
    IF v_promoted IS NOT NULL THEN
      UPDATE core.claim SET claim_state='waiting', released_at=NULL
       WHERE account_id=v_account AND repo=r.repo AND claim_id=r.claim_id AND claim_state='expired';
    END IF;
  END LOOP;
  RETURN v_n;
END $$;
ALTER FUNCTION core.expire_stale_claims() OWNER TO veripsa_migrator;

-- declare_claim_with_authority: THE LOCK + the ON-RAMP QUEUE. declare-before-edit on a lane (coordinate).
-- A free lane is GRANTED (active). A lane a DIFFERENT agent holds is NOT bounced — the caller WAITS IN
-- LINE (a 'waiting' claim, FIFO) and the prevented clobber is recorded (a held collision). Idempotent:
-- re-declaring returns your existing grant or your place in line. Returns jsonb {granted|queued, position,
-- holder}. The unique partial index (active only) is the structural backstop; a race re-reads and queues.
-- _place_claim: THE LOCK + ON-RAMP QUEUE logic, parameterized by (account, agent) — the SINGLE source of the
-- queue truth. Both the buyer's own writer (declare_claim_with_authority) and the App reserving on a PR
-- author's behalf (act_for_claim_with_authority) funnel through it, so the two paths can never drift. Assumes
-- core.current_account is ALREADY pinned by the caller (the wrappers resolve identity, which pins it). NOT
-- granted to any buyer role — identity is never passed in by an untrusted caller, only by the gate wrappers.
CREATE OR REPLACE FUNCTION core._place_claim(p_claim_id text, p_target_path text, p_repo text, p_branch text, p_account text, p_agent text)
    RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path TO 'core','pg_catalog' AS $$
DECLARE v_agent text; v_account text; v_repo text; v_branch text; v_change text; v_existing text; v_state text; v_holder text; v_pos int;
BEGIN
  v_account := p_account; v_agent := p_agent;
  IF p_claim_id IS NULL OR btrim(p_claim_id)='' THEN RAISE EXCEPTION 'empty claim rejected: a claim_id is required' USING ERRCODE='23514'; END IF;
  IF p_target_path IS NULL OR btrim(p_target_path)='' THEN RAISE EXCEPTION 'empty claim rejected: a target_path is required' USING ERRCODE='23514'; END IF;
  IF length(p_target_path)>1024 THEN RAISE EXCEPTION 'target_path too long (max 1024)' USING ERRCODE='23514'; END IF;
  v_repo := left(COALESCE(p_repo,''),512); v_branch := left(COALESCE(p_branch,''),512);
  v_change := left(CASE WHEN position(':' in p_claim_id) > 0 THEN split_part(p_claim_id, ':', 1) ELSE p_claim_id END, 200);
  PERFORM core.mark_governed_write('claim');
  -- KEEP THE LEASE ALIVE for THIS change only (declaring/syncing PR-A is a heartbeat for PR-A's lanes). Scoping
  -- to change_id is load-bearing: renewing EVERY active+waiting claim of this agent would perpetually refresh a
  -- DIFFERENT, genuinely-abandoned PR's lease on any activity by the same author — so expire_stale_claims +
  -- stalled_work_surface's 'abandoned' signal could never fire for a multi-PR author. One PR's pulse is not another's.
  -- ORDER IS LOAD-BEARING: this self-heartbeat MUST run BEFORE expire_stale_claims(). If we swept first, a holder
  -- whose lease JUST lapsed (quiet >lease, then pushes again — the synchronize that PROVES it is alive) would have
  -- its OWN active claim flipped to 'expired' and a waiter auto-promoted onto its lane, and the renewal below —
  -- scoped to claim_state IN ('active','waiting') — would then no longer match the now-'expired' row. The result
  -- was the live, actively-pushing holder being demoted to wait behind the very PR it was ahead of (a self-eviction
  -- on resync). Renewing FIRST refreshes this change's lease so the sweep that follows can never expire it; only a
  -- holder that did NOT resync (never reaches here) lapses. The change_id scope still lets the later sweep expire a
  -- DIFFERENT, genuinely-abandoned PR by the same author.
  UPDATE core.claim SET heartbeat_at=now(), lease_expires_at=core._claim_lease_at()
   WHERE account_id=v_account AND agent_id=v_agent AND change_id=v_change AND claim_state IN ('active','waiting');
  PERFORM core.expire_stale_claims();  -- THEN free OTHER stale holders + auto-promote their waiters (this change's lease is already refreshed above, so it is never the one swept)
  -- already in this lane (granted or in line)? return that, idempotently.
  SELECT claim_id, claim_state INTO v_existing, v_state FROM core.claim
   WHERE account_id=v_account AND agent_id=v_agent AND change_id=v_change AND repo=v_repo AND branch=v_branch AND target_path=p_target_path
     AND claim_state IN ('active','waiting') LIMIT 1;
  IF v_existing IS NOT NULL THEN
    IF v_state='active' THEN RETURN jsonb_build_object('ok',true,'granted',true,'claim_id',v_existing,'change_id',v_change,'state','active'); END IF;
    SELECT agent_id INTO v_holder FROM core.claim WHERE account_id=v_account AND repo=v_repo AND branch=v_branch AND target_path=p_target_path AND claim_state='active' LIMIT 1;
    v_pos := 1 + (SELECT count(*) FROM core.claim w WHERE w.account_id=v_account AND w.repo=v_repo AND w.branch=v_branch AND w.target_path=p_target_path AND w.claim_state='waiting'
                    AND w.claimed_at < (SELECT claimed_at FROM core.claim WHERE account_id=v_account AND repo=v_repo AND claim_id=v_existing));
    RETURN jsonb_build_object('ok',true,'granted',false,'queued',true,'claim_id',v_existing,'change_id',v_change,'state','waiting','position',v_pos,'holder',core.agent_name(v_holder));
  END IF;
  -- RE-DECLARED A PREVIOUSLY-CONCLUDED CLAIM (a REOPENED PR): a row with THIS (repo, claim_id) already exists but
  -- is released/expired (the PR landed or withdrew, then was reopened — rows persist; PK is (account_id, repo,
  -- claim_id)). We must NOT fall to the free-lane INSERT: it would collide on claim_pkey, and the unique_violation
  -- handler's own INSERT would collide on claim_pkey AGAIN, uncaught → the worker crashes (no analysis, stale
  -- comment). RECYCLE the row keyed by claim_pkey instead (UPDATE → active/waiting with a fresh lease), so reopen
  -- is idempotent. (claim_id is per-repo — see the PK widening in 20_core.sql — so every lookup pins repo too.)
  SELECT claim_id INTO v_existing FROM core.claim
   WHERE account_id=v_account AND repo=v_repo AND claim_id=p_claim_id AND claim_state IN ('released','expired') LIMIT 1;
  IF v_existing IS NOT NULL THEN
    -- is the lane now held by a DIFFERENT in-flight change? (another car took it while this PR was closed)
    SELECT agent_id INTO v_holder FROM core.claim
     WHERE account_id=v_account AND repo=v_repo AND branch=v_branch AND target_path=p_target_path AND claim_state='active'
       AND NOT (agent_id=v_agent AND change_id=v_change) LIMIT 1;
    PERFORM core.mark_governed_write('claim');
    IF v_holder IS NOT NULL THEN
      -- lane taken → REACTIVATE this row as a WAITER (back in line) + record the prevented clobber.
      UPDATE core.claim SET claim_state='waiting', agent_id=v_agent, change_id=v_change, repo=v_repo, branch=v_branch,
             target_path=p_target_path, claimed_at=now(), heartbeat_at=now(), released_at=NULL,
             lease_expires_at=core._claim_lease_at()
       WHERE account_id=v_account AND repo=v_repo AND claim_id=v_existing;   -- claim_id is per-repo: pin the repo
      PERFORM core.record_collision_with_authority(p_target_path, v_repo, v_branch, v_agent, v_change);
      v_pos := 1 + (SELECT count(*) FROM core.claim w WHERE w.account_id=v_account AND w.repo=v_repo AND w.branch=v_branch AND w.target_path=p_target_path AND w.claim_state='waiting' AND w.claim_id<>v_existing);
      RETURN jsonb_build_object('ok',true,'granted',false,'queued',true,'claim_id',v_existing,'change_id',v_change,'state','waiting','position',v_pos,'holder',core.agent_name(v_holder));
    END IF;
    -- free lane → REACTIVATE this row as the active holder (the reopened PR gets its lane back). On a race
    -- (someone grabbed the lane between the read above and here) the claim_one_active index fires → fall to waiting.
    BEGIN
      UPDATE core.claim SET claim_state='active', agent_id=v_agent, change_id=v_change, repo=v_repo, branch=v_branch,
             target_path=p_target_path, claimed_at=now(), heartbeat_at=now(), released_at=NULL,
             lease_expires_at=core._claim_lease_at()
       WHERE account_id=v_account AND repo=v_repo AND claim_id=v_existing;   -- claim_id is per-repo: pin the repo
    EXCEPTION WHEN unique_violation THEN
      SELECT agent_id INTO v_holder FROM core.claim WHERE account_id=v_account AND repo=v_repo AND branch=v_branch AND target_path=p_target_path AND claim_state='active' LIMIT 1;
      PERFORM core.mark_governed_write('claim');
      UPDATE core.claim SET claim_state='waiting', agent_id=v_agent, change_id=v_change, repo=v_repo, branch=v_branch,
             target_path=p_target_path, claimed_at=now(), heartbeat_at=now(), released_at=NULL,
             lease_expires_at=core._claim_lease_at()
       WHERE account_id=v_account AND repo=v_repo AND claim_id=v_existing;   -- claim_id is per-repo: pin the repo
      PERFORM core.record_collision_with_authority(p_target_path, v_repo, v_branch, v_agent, v_change);
      v_pos := 1 + (SELECT count(*) FROM core.claim w WHERE w.account_id=v_account AND w.repo=v_repo AND w.branch=v_branch AND w.target_path=p_target_path AND w.claim_state='waiting' AND w.claim_id<>v_existing);
      RETURN jsonb_build_object('ok',true,'granted',false,'queued',true,'claim_id',v_existing,'change_id',v_change,'state','waiting','position',v_pos,'holder',core.agent_name(v_holder));
    END;
    RETURN jsonb_build_object('ok',true,'granted',true,'claim_id',v_existing,'change_id',v_change,'state','active');
  END IF;
  -- lane held by another in-flight change (even from the same agent/author) → WAIT IN LINE.
  SELECT agent_id INTO v_holder FROM core.claim
   WHERE account_id=v_account AND repo=v_repo AND branch=v_branch AND target_path=p_target_path AND claim_state='active'
     AND NOT (agent_id=v_agent AND change_id=v_change) LIMIT 1;
  IF v_holder IS NOT NULL THEN
    PERFORM core.mark_governed_write('claim');
    INSERT INTO core.claim(claim_id, account_id, agent_id, change_id, repo, branch, target_path, claim_state)
    VALUES (p_claim_id, v_account, v_agent, v_change, v_repo, v_branch, p_target_path, 'waiting');
    PERFORM core.record_collision_with_authority(p_target_path, v_repo, v_branch, v_agent, v_change);
    v_pos := 1 + (SELECT count(*) FROM core.claim w WHERE w.account_id=v_account AND w.repo=v_repo AND w.branch=v_branch AND w.target_path=p_target_path AND w.claim_state='waiting' AND w.claim_id<>p_claim_id);
    RETURN jsonb_build_object('ok',true,'granted',false,'queued',true,'claim_id',p_claim_id,'change_id',v_change,'state','waiting','position',v_pos,'holder',core.agent_name(v_holder));
  END IF;
  -- free lane → GRANT (active). On a race (someone grabbed it), re-read and queue.
  -- lease_expires_at is set EXPLICITLY from the bounded knob (NOT the table's hardcoded 30m DEFAULT), so the
  -- owner's tuned lease_minutes actually governs a freshly granted lane — the column DEFAULT only ever applies
  -- to a row the gate forgot to set, which would silently ignore the knob.
  PERFORM core.mark_governed_write('claim');
  BEGIN
    INSERT INTO core.claim(claim_id, account_id, agent_id, change_id, repo, branch, target_path, lease_expires_at)
    VALUES (p_claim_id, v_account, v_agent, v_change, v_repo, v_branch, p_target_path, core._claim_lease_at());
  EXCEPTION WHEN unique_violation THEN
    SELECT agent_id INTO v_holder FROM core.claim WHERE account_id=v_account AND repo=v_repo AND branch=v_branch AND target_path=p_target_path AND claim_state='active' LIMIT 1;
    PERFORM core.mark_governed_write('claim');
    INSERT INTO core.claim(claim_id, account_id, agent_id, change_id, repo, branch, target_path, claim_state)
    VALUES (p_claim_id, v_account, v_agent, v_change, v_repo, v_branch, p_target_path, 'waiting');
    PERFORM core.record_collision_with_authority(p_target_path, v_repo, v_branch, v_agent, v_change);
    v_pos := 1 + (SELECT count(*) FROM core.claim w WHERE w.account_id=v_account AND w.repo=v_repo AND w.branch=v_branch AND w.target_path=p_target_path AND w.claim_state='waiting' AND w.claim_id<>p_claim_id);
    RETURN jsonb_build_object('ok',true,'granted',false,'queued',true,'claim_id',p_claim_id,'change_id',v_change,'state','waiting','position',v_pos,'holder',core.agent_name(v_holder));
  END;
  RETURN jsonb_build_object('ok',true,'granted',true,'claim_id',p_claim_id,'change_id',v_change,'state','active');
END $$;
ALTER FUNCTION core._place_claim(text,text,text,text,text,text) OWNER TO veripsa_migrator;
-- INTERNAL ONLY: it takes (account, agent) as params, so PUBLIC execute would let a caller spoof identity.
-- Postgres grants EXECUTE to PUBLIC by default — revoke it. Only the gate wrappers (owner-context) call it.
REVOKE ALL ON FUNCTION core._place_claim(text,text,text,text,text,text) FROM PUBLIC;

-- _ranges_from_jsonb: convert a content-free jsonb array of [start,end] line pairs (the changed-line ranges
-- the App parsed from a PR diff's HUNK HEADERS) into a sanitized int4range[]. Each pair is accepted ONLY when
-- well-formed (two JSON numbers, 1-based, ordered, bounded); a junk pair is skipped (never a bad range).
-- Half-open [s, e+1) so an inclusive [s,e] line span is represented natively (int4range && for overlap).
-- Returns NULL when nothing valid → the claim stays file-level (the safety net). Line numbers only; IMMUTABLE.
-- RANGES CAP (audit:dos — pathological-input DoS). A crafted PR file with THOUSANDS of tiny disjoint diff
-- hunks parses to a huge [[s,e],…] array → a touched_ranges int4range[] with 10k+ elements. The finer-collision
-- engine (_file_pair_symbol_overlap) then unnests both sides' ranges and joins each against EVERY file symbol
-- (containment <@) = O(ranges × symbols) per file-pair — measured at 10k ranges × 5k symbols: ONE
-- main_impact_surface call took ~15 s. The fix is honest DEGRADE: a change with more than this many ranges is
-- NOT confidently finer-mappable anyway, so above the cap we DROP the ranges entirely (return NULL) → the claim
-- stays FILE-LEVEL (the recall safety net — over-flag, never miss), and the engine never runs the O(ranges×syms)
-- join. A real PR touches a handful of hunks; thousands is machine-emitted / adversarial. Bound at the
-- authoritative DB boundary so even a client that calls declare_claim directly (bypassing the App parser) is
-- capped. Content-free (line numbers only). Cap = 512 (a real PR has a handful of hunks; thousands = crafted).
CREATE OR REPLACE FUNCTION core._ranges_from_jsonb(p jsonb) RETURNS int4range[]
    LANGUAGE plpgsql IMMUTABLE AS $$
DECLARE v int4range[]; el jsonb; s int; e int; v_cap CONSTANT int := 512;
BEGIN
  IF p IS NULL OR jsonb_typeof(p) <> 'array' THEN RETURN NULL; END IF;
  -- DoS cap: a pathological hunk-count (thousands of tiny disjoint ranges) makes the engine's per-pair
  -- O(ranges × symbols) containment join blow up. Above the cap, drop ALL ranges → file-level fallback (the
  -- recall safety net, never a missed collision). Cheap O(1) length check before the per-element loop.
  IF jsonb_array_length(p) > v_cap THEN RETURN NULL; END IF;
  v := ARRAY[]::int4range[];
  FOR el IN SELECT * FROM jsonb_array_elements(p) LOOP
    CONTINUE WHEN jsonb_typeof(el) <> 'array' OR jsonb_array_length(el) < 2;
    CONTINUE WHEN jsonb_typeof(el->0) <> 'number' OR jsonb_typeof(el->1) <> 'number';
    BEGIN s := (el->0)::text::int; e := (el->1)::text::int; EXCEPTION WHEN others THEN CONTINUE; END;
    CONTINUE WHEN s < 1 OR e < s OR e > 100000000;
    v := v || int4range(s, e + 1, '[)');   -- inclusive [s,e] → half-open [s, e+1)
  END LOOP;
  IF array_length(v,1) IS NULL THEN RETURN NULL; END IF;
  RETURN v;
END $$;
ALTER FUNCTION core._ranges_from_jsonb(jsonb) OWNER TO veripsa_migrator;

-- _set_claim_ranges: record THIS change's content-free changed-line ranges on the claim row the gate just
-- placed/recycled (finer collision). Scoped to (account, agent, change_id, repo, branch, target_path) so it
-- only ever annotates THIS author's THIS change's claim on THIS path — never another's. NULL/empty ranges
-- CLEAR prior geometry: a synchronize can keep the same path while its current diff loses hunk evidence, and
-- retaining the old head's ranges would falsely bind stale line numbers to the new analyzed head. NULL is the
-- file-level fallback (the safety net). Line numbers only; never code.
CREATE OR REPLACE FUNCTION core._set_claim_ranges(p_account text, p_agent text, p_change text, p_repo text, p_branch text, p_path text, p_ranges int4range[])
    RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path TO 'core','pg_catalog' AS $$
BEGIN
  PERFORM core.mark_governed_write('claim');
  UPDATE core.claim SET touched_ranges = CASE WHEN array_length(p_ranges,1) IS NULL THEN NULL ELSE p_ranges END
   WHERE account_id=p_account AND agent_id=p_agent AND change_id=p_change AND repo=p_repo AND branch=p_branch
     AND target_path=p_path AND claim_state IN ('active','waiting')
     AND touched_ranges IS DISTINCT FROM CASE WHEN array_length(p_ranges,1) IS NULL THEN NULL ELSE p_ranges END;
END $$;
ALTER FUNCTION core._set_claim_ranges(text,text,text,text,text,text,int4range[]) OWNER TO veripsa_migrator;
REVOKE ALL ON FUNCTION core._set_claim_ranges(text,text,text,text,text,text,int4range[]) FROM PUBLIC;

-- _set_claim_base_hash: record THIS change's content-free BASE CONTENT HASH (git-blob-sha at the PR's base)
-- on this path's claim — the freshness key the engine needs to PROVE the diff line numbers map to the right
-- symbols before it demotes a file-level collision. Same (account, agent, change, repo, branch, path) scoping
-- as _set_claim_ranges so it only ever annotates THIS author's THIS change's claim on THIS path. A NULL/junk
-- hash CLEARS any prior head's value → unknown → file-level fallback (the recall-safe default). It is
-- SEPARATE from _set_claim_ranges (not folded in) because a claim can carry ranges but no base hash, or a base
-- hash but no ranges, and each independently degrades to the safe fallback. A hash is a fingerprint, not code.
CREATE OR REPLACE FUNCTION core._set_claim_base_hash(p_account text, p_agent text, p_change text, p_repo text, p_branch text, p_path text, p_base_hash text)
    RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path TO 'core','pg_catalog' AS $$
DECLARE v_hash text;
BEGIN
  v_hash := core._clean_hash(p_base_hash);
  PERFORM core.mark_governed_write('claim');
  UPDATE core.claim SET base_content_hash = v_hash
   WHERE account_id=p_account AND agent_id=p_agent AND change_id=p_change AND repo=p_repo AND branch=p_branch
     AND target_path=p_path AND claim_state IN ('active','waiting')
     AND base_content_hash IS DISTINCT FROM v_hash;
END $$;
ALTER FUNCTION core._set_claim_base_hash(text,text,text,text,text,text,text) OWNER TO veripsa_migrator;
REVOKE ALL ON FUNCTION core._set_claim_base_hash(text,text,text,text,text,text,text) FROM PUBLIC;

-- declare_claim_with_authority: the buyer's OWN writer reserves a lane (identity = the connecting seat).
-- DROP the old 4-arg signature first (CREATE OR REPLACE cannot add the trailing p_ranges param without leaving
-- the old overload behind — mirrors record_collision_with_authority's pattern). p_ranges is OPTIONAL (DEFAULT
-- NULL) so every existing caller is unchanged + back-compatible; passing ranges enables the finer collision.
DROP FUNCTION IF EXISTS core.declare_claim_with_authority(text,text,text,text);
DROP FUNCTION IF EXISTS core.declare_claim_with_authority(text,text,text,text,jsonb);
CREATE OR REPLACE FUNCTION core.declare_claim_with_authority(p_claim_id text, p_target_path text, p_repo text DEFAULT '', p_branch text DEFAULT '', p_ranges jsonb DEFAULT NULL, p_base_hash text DEFAULT NULL)
    RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path TO 'core','pg_catalog' AS $$
DECLARE v_agent text; v_account text; v_res jsonb; v_change text;
BEGIN
  SELECT agent, account INTO v_agent, v_account FROM core.establish_session_write_context() AS c(agent,account);
  v_res := core._place_claim(p_claim_id, p_target_path, p_repo, p_branch, v_account, v_agent);
  v_change := v_res->>'change_id';
  -- finer-collision: record the change's content-free changed-line ranges (jsonb [[s,e],…] → int4range[]).
  PERFORM core._set_claim_ranges(v_account, v_agent, v_change, left(COALESCE(p_repo,''),512), left(COALESCE(p_branch,''),512), p_target_path, core._ranges_from_jsonb(p_ranges));
  -- freshness key: the file's content hash AT THE PR'S BASE (the staleness-gated demotion's proof of validity).
  PERFORM core._set_claim_base_hash(v_account, v_agent, v_change, left(COALESCE(p_repo,''),512), left(COALESCE(p_branch,''),512), p_target_path, p_base_hash);
  RETURN v_res;
END $$;
ALTER FUNCTION core.declare_claim_with_authority(text,text,text,text,jsonb,text) OWNER TO veripsa_migrator;

-- act_for_claim_with_authority: THE DELEGATION GATE (the hosted App's identity model). ONE service identity
-- (role veripsa_app) reserves on behalf of each PR's AUTHOR, so the claim is attributed to the real author
-- (agent 'GH-<login>'), NOT the App — that is what lets the PR comment name the actual author and lets two
-- authors' PRs contend correctly. The author's agent is provisioned on first sight (content-free: the GitHub
-- login only). Authorization: the connecting role must itself be a provisioned identity (resolve_session →
-- its account is the tenant); it can only ever act WITHIN its own account (current_account is pinned to it,
-- and FORCE-RLS WITH CHECK refuses any row in another tenant). Same queue truth as declare_claim (_place_claim).
-- DROP the old 5-arg signature first so the trailing p_ranges param (DEFAULT NULL, back-compatible) does not
-- leave the old overload behind (same pattern as declare_claim_with_authority above).
-- DROP the old 5-arg AND 6-arg signatures first: the trailing p_base_hash (DEFAULT NULL, back-compatible) adds
-- a 7th param, and CREATE OR REPLACE cannot add a param without leaving the prior overload behind (same pattern
-- as the p_ranges add above). Every existing caller (5-arg, 6-arg) still resolves — the new params DEFAULT NULL.
-- DROP the 7-arg too: the trailing p_author_is_bot (DEFAULT false, back-compatible) adds an 8th param so the
-- author's agent is stamped HUMAN (a seat) vs AI (free) at creation. Every prior overload (5/6/7-arg) still
-- resolves — the new param DEFAULTs false (= human, the common case; the App passes true only for a Bot author).
DROP FUNCTION IF EXISTS core.act_for_claim_with_authority(text,text,text,text,text);
DROP FUNCTION IF EXISTS core.act_for_claim_with_authority(text,text,text,text,text,jsonb);
DROP FUNCTION IF EXISTS core.act_for_claim_with_authority(text,text,text,text,text,jsonb,text);
CREATE OR REPLACE FUNCTION core.act_for_claim_with_authority(p_claim_id text, p_target_path text, p_repo text, p_branch text, p_author text, p_ranges jsonb DEFAULT NULL, p_base_hash text DEFAULT NULL, p_author_is_bot boolean DEFAULT false)
    RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path TO 'core','pg_catalog' AS $$
DECLARE v_app_agent text; v_account text; v_agent text; v_login text; v_res jsonb; v_change text; v_kind text;
BEGIN
  SELECT agent, account INTO v_app_agent, v_account FROM core.establish_session_write_context() AS c(agent,account);
  v_login := left(regexp_replace(COALESCE(p_author,''), '[^A-Za-z0-9_.\-]', '', 'g'), 64);  -- content-free login
  IF v_login = '' THEN RAISE EXCEPTION 'act_for needs a non-empty author login' USING ERRCODE='23514'; END IF;
  v_agent := 'GH-'||v_login;
  -- SEAT METERING: a human PR author IS a seat (the PLG value line); a Bot author is free (the AI-fleet wedge).
  -- The login is sanitized to [A-Za-z0-9_.-] BEFORE storage (the '[bot]' marker is stripped), so the bot signal
  -- can NOT be recovered from the stored login — it MUST come from the webhook (pull_request.user.type=='Bot').
  v_kind := CASE WHEN COALESCE(p_author_is_bot, false) THEN 'ai' ELSE 'human' END;
  -- provision the author's agent in THIS account on first sight (agent table is not governed-trigger'd, like
  -- provision_seat; RLS WITH CHECK admits it because current_account is pinned to v_account). SELF-HEAL: an author
  -- already stored under the OLD default ('ai') is CORRECTED to the right kind on its next claim — but ONLY for a
  -- 'GH-' author agent (never the App/seat agents AG-*: this fn only ever writes a 'GH-' id, so the conflict target
  -- is always an author row; the guard documents intent + is a belt for any future caller).
  PERFORM core.mark_governed_write('agent');
  INSERT INTO core.agent(agent_id, account_id, display_name, agent_kind) VALUES (v_agent, v_account, v_login, v_kind)
  ON CONFLICT (agent_id) DO NOTHING;
  -- SELF-HEAL (RLS-SAFE, audit r2): correct an author stored under a stale kind via a SEPARATE account-scoped
  -- UPDATE — NOT `ON CONFLICT DO UPDATE`. core.agent's PK is the GLOBAL agent_id, so a GH login shared across orgs
  -- (a bot like dependabot, or a human in two orgs) is ONE row owned by whichever tenant saw it first. A DO UPDATE
  -- from the SECOND tenant tries to write a row behind another tenant's RLS wall → Postgres raises 42501 → the
  -- whole event fails → GitHub redelivers → raises again = a deterministic POISON-PILL (that tenant's PRs never get
  -- a check). Scoping the UPDATE to account_id=v_account makes a cross-tenant same-login row 0 rows = a clean no-op.
  UPDATE core.agent SET agent_kind = v_kind
   WHERE agent_id = v_agent AND account_id = v_account AND agent_id LIKE 'GH-%' AND agent_kind <> v_kind;
  v_res := core._place_claim(p_claim_id, p_target_path, p_repo, p_branch, v_account, v_agent);
  v_change := v_res->>'change_id';
  -- finer-collision: record the change's content-free changed-line ranges (jsonb [[s,e],…] → int4range[]).
  PERFORM core._set_claim_ranges(v_account, v_agent, v_change, left(COALESCE(p_repo,''),512), left(COALESCE(p_branch,''),512), p_target_path, core._ranges_from_jsonb(p_ranges));
  -- freshness key: record this file's content hash AT THE PR'S BASE so the engine can PROVE the ranges map to
  -- the right symbols (graph hash == base hash) before it dares demote the file-level collision (staleness gate).
  PERFORM core._set_claim_base_hash(v_account, v_agent, v_change, left(COALESCE(p_repo,''),512), left(COALESCE(p_branch,''),512), p_target_path, p_base_hash);
  RETURN v_res;
END $$;
ALTER FUNCTION core.act_for_claim_with_authority(text,text,text,text,text,jsonb,text,boolean) OWNER TO veripsa_migrator;

-- heartbeat / release: keep the lease alive while still editing; release when done (frees the lane).
CREATE OR REPLACE FUNCTION core.heartbeat_claim_with_authority(p_claim_id text) RETURNS timestamptz
    LANGUAGE plpgsql SECURITY DEFINER SET search_path TO 'core','pg_catalog' AS $$
DECLARE v_agent text; v_account text; v_hb timestamptz;
BEGIN
  SELECT agent, account INTO v_agent, v_account FROM core.establish_session_write_context() AS c(agent,account);
  PERFORM core.mark_governed_write('claim');
  UPDATE core.claim SET heartbeat_at=now(), lease_expires_at=core._claim_lease_at()
   WHERE account_id=v_account AND agent_id=v_agent AND claim_id=p_claim_id AND claim_state='active'
   RETURNING heartbeat_at INTO v_hb;
  RETURN v_hb;
END $$;
ALTER FUNCTION core.heartbeat_claim_with_authority(text) OWNER TO veripsa_migrator;

-- release: leave the lane (or leave the line). On releasing the ACTIVE holder, the next car in line is
-- promoted (FIFO). Returns jsonb {ok, claim_id, promoted}. Releasing a 'waiting' claim just leaves the queue.
CREATE OR REPLACE FUNCTION core.release_claim_with_authority(p_claim_id text) RETURNS jsonb
    LANGUAGE plpgsql SECURITY DEFINER SET search_path TO 'core','pg_catalog' AS $$
DECLARE v_agent text; v_account text; v_id text; v_repo text; v_branch text; v_path text; v_promoted text;
BEGIN
  SELECT agent, account INTO v_agent, v_account FROM core.establish_session_write_context() AS c(agent,account);
  PERFORM core.mark_governed_write('claim');
  UPDATE core.claim SET claim_state='released', released_at=now()
   WHERE account_id=v_account AND agent_id=v_agent AND claim_id=p_claim_id AND claim_state IN ('active','waiting')
   RETURNING claim_id, repo, branch, target_path INTO v_id, v_repo, v_branch, v_path;
  IF v_id IS NULL THEN RETURN jsonb_build_object('ok',false,'error','no such open claim'); END IF;
  v_promoted := core._promote_next_waiter(v_account, v_repo, v_branch, v_path);
  RETURN jsonb_build_object('ok',true,'claim_id',v_id,'promoted',core.agent_name(v_promoted));
END $$;
ALTER FUNCTION core.release_claim_with_authority(text) OWNER TO veripsa_migrator;

-- break_lane_with_authority: the OWNER override (the highway recovery crew). Force-clear the active holder
-- on a lane (e.g. it accident-stalled) and promote the next in line. Owner action; granted to the steward.
CREATE OR REPLACE FUNCTION core.break_lane_with_authority(p_repo text, p_branch text, p_path text) RETURNS jsonb
    LANGUAGE plpgsql SECURITY DEFINER SET search_path TO 'core','pg_catalog' AS $$
DECLARE v_agent text; v_account text; v_repo text; v_branch text; v_broke text; v_promoted text; v_id text;
BEGIN
  IF p_path IS NULL OR btrim(p_path)='' THEN RAISE EXCEPTION 'break_lane needs a path' USING ERRCODE='23514'; END IF;
  v_repo := left(COALESCE(p_repo,''),512); v_branch := left(COALESCE(p_branch,''),512);
  SELECT agent, account INTO v_agent, v_account FROM core.establish_session_write_context() AS c(agent,account);
  PERFORM core.mark_governed_write('claim');
  UPDATE core.claim SET claim_state='expired', released_at=now()
   WHERE account_id=v_account AND repo=v_repo AND branch=v_branch AND target_path=p_path AND claim_state='active'
   RETURNING agent_id INTO v_broke;
  -- ACCOUNTABILITY: a privileged override that force-clears a holder's active lane MUST leave a trace in the
  -- audit ledger (the product's whole audit story is core.event). Record WHO broke WHOSE lane, when — but ONLY
  -- if there really was an active holder (v_broke); breaking a free/empty lane records no spurious event.
  IF v_broke IS NOT NULL THEN
    v_id := 'EV-'||substr(md5(random()::text||clock_timestamp()::text||v_agent),1,16);
    PERFORM core.mark_governed_write('event');
    INSERT INTO core.event(event_id, account_id, kind, agent_id, counterparty_agent, repo, branch, path)
    VALUES (v_id, v_account, 'lane_broken', v_agent, v_broke, v_repo, v_branch, p_path);
  END IF;
  v_promoted := core._promote_next_waiter(v_account, v_repo, v_branch, p_path);
  RETURN jsonb_build_object('ok',true,'lane',p_path,'broke',core.agent_name(v_broke),'promoted',core.agent_name(v_promoted));
END $$;
ALTER FUNCTION core.break_lane_with_authority(text,text,text) OWNER TO veripsa_migrator;

-- _clean_span: a CONTENT-FREE symbol span sanitizer. Takes two jsonb scalars (a node's start_line/end_line)
-- and returns a (start_line int, end_line int) row that is EITHER both-NULL (no/invalid span → that symbol
-- resolves at FILE level, the safety net) OR a well-formed 1-based ordered bounded pair. It is the single
-- place that decides a span is trustworthy, so every INSERT can store the result without re-validating (and
-- the table CHECK is a belt-and-suspenders backstop). Line numbers only — never code. IMMUTABLE (pure).
CREATE OR REPLACE FUNCTION core._clean_span(p_start jsonb, p_end jsonb)
    RETURNS TABLE(start_line int, end_line int) LANGUAGE plpgsql IMMUTABLE AS $$
DECLARE s int; e int;
BEGIN
  -- accept only a JSON NUMBER (never a string/object/array); anything else → no span.
  IF p_start IS NULL OR p_end IS NULL OR jsonb_typeof(p_start)<>'number' OR jsonb_typeof(p_end)<>'number' THEN
    RETURN QUERY SELECT NULL::int, NULL::int; RETURN;
  END IF;
  BEGIN
    s := (p_start)::text::int; e := (p_end)::text::int;
  EXCEPTION WHEN others THEN
    RETURN QUERY SELECT NULL::int, NULL::int; RETURN;   -- non-integer number (e.g. 1.5) / overflow → no span
  END;
  IF s >= 1 AND e >= s AND e <= 100000000 THEN
    RETURN QUERY SELECT s, e;
  ELSE
    RETURN QUERY SELECT NULL::int, NULL::int;           -- inverted / out-of-bounds → no span (file-level)
  END IF;
END $$;
ALTER FUNCTION core._clean_span(jsonb,jsonb) OWNER TO veripsa_migrator;

-- _clean_hash: the CONTENT-FREE freshness-key sanitizer. Takes a candidate content hash (git-blob-sha) and
-- returns it LOWER-CASED iff it is a clean hex string of bounded length (≤64 — git-blob-sha is 40, sha256 is
-- 64), else NULL. This is the single place that decides a hash is well-formed, so both sides of the demotion
-- equality test compare normalized values (case-insensitive). NULL out = unknown = file-level fallback (the
-- recall-safe default — a junk/oversized/non-hex value can never be stored and thus can never spoof a match).
-- A hash is a fingerprint, never the bytes (content-free). IMMUTABLE (pure).
CREATE OR REPLACE FUNCTION core._clean_hash(p_hash text)
    RETURNS text LANGUAGE sql IMMUTABLE AS $$
  SELECT CASE
    WHEN p_hash IS NULL THEN NULL
    WHEN length(btrim(p_hash)) BETWEEN 1 AND 64 AND btrim(p_hash) ~ '^[0-9a-fA-F]+$' THEN lower(btrim(p_hash))
    ELSE NULL
  END
$$;
ALTER FUNCTION core._clean_hash(text) OWNER TO veripsa_migrator;

-- _safe_ref_token: the CONTENT-FREE SHAPE filter for a stored/displayed reference string. The lossless graph
-- resolver identity is persisted separately as a one-way SHA-256 semantic key, so this presentation value can
-- keep the existing single-line/XSS-safe contract without corrupting adjacency.
--   KEEP:  letters/digits + . / : :: - _ # $ @ [ ] * and SPACES and every other printable
--          (e.g. Rust crate::lexical::num, C# App.Services, @scope/pkg, a/b.hpp, a path with spaces).
--   STRIP: EXACTLY the egress-significant chars and nothing else -- POSIX [:cntrl:] (the C0 control range
--          0x00-0x1F + DEL 0x7F, which covers NEWLINE/CR/TAB/FF/VT = the multi-line body shape), the C1
--          control range 0x80-0x9F, and the two HTML-significant angle brackets < and > (the stored-XSS shape).
-- Each stripped run becomes ONE space (a multi-line value flattens to one line without fusing two tokens), then
-- internal whitespace runs collapse to a single space and the ends are trimmed -> one single-line token. A value
-- that was ONLY stripped chars collapses to '' -> NULL (a name with no reference content is no name). This reads
-- the bytes ONLY as a shape (which chars are reference-shaped), never as data -- still content-free.
-- IMMUTABLE (pure). NULL in -> NULL out (a NULL name stays NULL; existing behavior is preserved).
CREATE OR REPLACE FUNCTION core._safe_ref_token(p_val text)
    RETURNS text LANGUAGE sql IMMUTABLE AS $SAFEREF$
  SELECT NULLIF(
    btrim(regexp_replace(
      regexp_replace(p_val, '[[:cntrl:]\u0080-\u009F<>]+', ' ', 'g'),
      '\s+', ' ', 'g')),
    '')
$SAFEREF$;
ALTER FUNCTION core._safe_ref_token(text) OWNER TO veripsa_migrator;

-- _clean_arity: content-free COUNT sanitizer for the signature-shape arity columns. Accepts ONLY a plain
-- jsonb number whose canonical text is 1–3 digits AND within the schema bound (0..512 — matches
-- code_node_shape_ok); anything else (absent key, jsonb null, a string, a float, a huge/negative number, an
-- object) degrades to NULL = unknown shape, NEVER an error — a malformed inbound shape field must not abort
-- the shared per-event ingest transaction. The digit-regex guard runs BEFORE the ::int cast, so an
-- out-of-range numeric (jsonb renders e.g. 1e20 as its full digit string) can never raise on the cast.
-- IMMUTABLE (pure). Recall-safe sibling of _clean_span/_clean_hash.
CREATE OR REPLACE FUNCTION core._clean_arity(p_val jsonb)
    RETURNS int LANGUAGE sql IMMUTABLE AS $$
  SELECT CASE
    WHEN p_val IS NULL OR jsonb_typeof(p_val) <> 'number' THEN NULL
    WHEN p_val::text !~ '^[0-9]{1,3}$' THEN NULL
    WHEN (p_val::text)::int > 512 THEN NULL
    ELSE (p_val::text)::int
  END
$$;
ALTER FUNCTION core._clean_arity(jsonb) OWNER TO veripsa_migrator;

-- _clean_flag: content-free FLAG sanitizer for the signature-shape boolean columns. Accepts ONLY a jsonb
-- boolean; anything else (absent, jsonb null, a string 'yes', a number 1) degrades to NULL = unknown, never
-- an error. IMMUTABLE (pure).
CREATE OR REPLACE FUNCTION core._clean_flag(p_val jsonb)
    RETURNS boolean LANGUAGE sql IMMUTABLE AS $$
  SELECT CASE WHEN p_val IS NOT NULL AND jsonb_typeof(p_val) = 'boolean' THEN (p_val::text)::boolean ELSE NULL END
$$;
ALTER FUNCTION core._clean_flag(jsonb) OWNER TO veripsa_migrator;

-- _clean_name_array: content-free parameter-NAME-list sanitizer for the signature-shape array columns.
-- Accepts ONLY a jsonb array within the schema's CARDINALITY bound (<=128 elements — matches
-- code_node_shape_ok); anything else (absent, jsonb null, a string, an object, an oversized array) degrades
-- to NULL = unknown, never an error. Each element is length-capped then passed through the SAME
-- `_safe_ref_token` shape filter as code_node.name (strip control chars / angle brackets, flatten to one
-- safe line — a parameter NAME is exactly the reference class that filter guards). A poisoned element
-- sanitizes IN PLACE (order preserved — positional meaning survives; an all-junk element becomes a NULL
-- element, never a silent shift). An EMPTY inbound array stays '{}' — a KNOWN zero-parameter shape, which is
-- NOT the same as NULL (= shape unknown). IMMUTABLE (pure).
CREATE OR REPLACE FUNCTION core._clean_name_array(p_val jsonb)
    RETURNS text[] LANGUAGE sql IMMUTABLE AS $$
  SELECT CASE
    WHEN p_val IS NULL OR jsonb_typeof(p_val) <> 'array' THEN NULL
    WHEN jsonb_array_length(p_val) > 128 THEN NULL
    ELSE (SELECT COALESCE(array_agg(core._safe_ref_token(left(e.val, 512)) ORDER BY e.ord), '{}'::text[])
            FROM jsonb_array_elements_text(p_val) WITH ORDINALITY AS e(val, ord))
  END
$$;
ALTER FUNCTION core._clean_name_array(jsonb) OWNER TO veripsa_migrator;

-- cg4 GRAPH SCHEMA INVENTORY — the executable persistence/adjacency contract. This constant read surface is
-- intentionally explicit: extractor and DB kind drift is a deploy failure, never a silent filter. Column edges
-- are first-class persisted/hash evidence but remain OUT of effective adjacency during this compatibility-safe
-- rollout; table-level queries/alters continue to drive the existing review behavior.
CREATE OR REPLACE FUNCTION core.graph_schema_inventory()
    RETURNS jsonb LANGUAGE sql IMMUTABLE AS $$
  SELECT jsonb_build_object(
    'schema_contract_version', 2,
    'extractor_version', 'cg4',
    'semantic_ref_version', 1,
    'semantic_identity_columns',jsonb_build_object(
      'node','semantic_key','edge','semantic_dst_key','algorithm','sha256-utf8'
    ),
    'node_analysis_statuses',to_jsonb(ARRAY['failed','ambiguous','incomplete']::text[]),
    'edge_reference_statuses',to_jsonb(ARRAY['ambiguous','unresolved']::text[]),
    'effective_adjacency_reference_status','resolved_only',
    'extractor_node_kinds', to_jsonb(ARRAY[
      'file','def','class','table','column','config_file','config_key',
      'iac_resource','k8s_resource','api_type','api_message','api_service','api_operation','api_schema',
      'ci_script','app_command','job_task','job_queue','sibling_stem','role_feature'
    ]::text[]),
    'extractor_edge_kinds', to_jsonb(ARRAY[
      'contains','calls','imports','queries','alters','reads_config','alters_col','queries_col'
    ]::text[]),
    'db_node_kinds', to_jsonb(ARRAY[
      'file','def','class','table','column','config_file','config_key',
      'iac_resource','k8s_resource','api_type','api_message','api_service','api_operation','api_schema',
      'ci_script','app_command','job_task','job_queue','sibling_stem','role_feature'
    ]::text[]),
    'db_edge_kinds', to_jsonb(ARRAY[
      'contains','calls','imports','queries','alters','reads_config','alters_col','queries_col'
    ]::text[]),
    'resource_node_kinds', to_jsonb(ARRAY[
      'table','column','config_key','iac_resource','k8s_resource','api_type','api_message','api_service',
      'api_operation','api_schema','ci_script','app_command','job_task','job_queue','sibling_stem','role_feature'
    ]::text[]),
    'effective_adjacency_node_kinds', to_jsonb(ARRAY['file','def','class']::text[]),
    'effective_adjacency_edge_kinds', to_jsonb(ARRAY['calls','imports','queries','alters','reads_config']::text[]),
    'evidence_only_edge_kinds', to_jsonb(ARRAY['alters_col','queries_col']::text[]),
    'persistence_losses', '[]'::jsonb,
    'observability_substrates', to_jsonb(ARRAY[
      'code_structure','imports','calls','database','config','terraform','kubernetes','graphql','protobuf',
      'openapi','routes','ci_script','tauri','celery','bullmq','sibling_stem','role_feature',
      'ambiguous','unresolved'
    ]::text[]),
    'observability_fallback_reason_codes', to_jsonb(ARRAY[
      'force_push','explicit_full_rebuild','unpatchable_changed_set','changed_set_base_mismatch',
      'compare_history_unproven','graph_baseline_changed','incremental_change_cap_exceeded',
      'target_sha_reader_unavailable','no_stored_graph_baseline','file_deleted','file_relocated',
      'changed_path_absent_from_persisted_universe',
      'resource_catalog_unavailable','resource_catalog_malformed','resource_definition_evidence_missing',
      'resource_definition_changed','reference_conditioned_consumer_changed','symmetric_pairing_member_changed',
      'bidirectional_import_member_changed','resolution_context_cap_exceeded','inert_import_activated',
      'unsafe_context_path','context_file_missing','contract_manifest_changed','sibling_or_role_candidate_added',
      'ci_script_changed','celery_task_changed','bullmq_queue_changed','job_queue_probe_failed',
      'tauri_command_changed','tauri_probe_failed','route_changed','route_probe_failed',
      'target_resource_identity_changed','extractor_version_mismatch','target_tree_mode_unverified',
      'semantic_reference_version_mismatch','stored_graph_uncertainty',
      'ambiguous_reference_detected','extractor_file_failed','extractor_file_incomplete',
      'incremental_internal_error'
    ]::text[])
  )
$$;
ALTER FUNCTION core.graph_schema_inventory() OWNER TO veripsa_migrator;

-- A fixed-length, content-free equality key for an exact extractor reference.
-- `sha256` is collision-resistant enough for graph identity while preserving the existing rule that raw
-- control/HTML-shaped payloads do not land in the display columns. NULL remains unknown.
CREATE OR REPLACE FUNCTION core._semantic_ref_key(p_val text)
    RETURNS text LANGUAGE sql IMMUTABLE AS $$
  SELECT CASE WHEN p_val IS NULL THEN NULL
              ELSE encode(sha256(convert_to(p_val,'UTF8')),'hex') END
$$;
ALTER FUNCTION core._semantic_ref_key(text) OWNER TO veripsa_migrator;

-- Non-null display representation for an exact non-empty reference. A value consisting only of characters
-- removed by `_safe_ref_token` gets a deterministic digest label instead of causing its semantic Edge/Resource
-- to be dropped. The graph joins on the full semantic digest, never this abbreviated presentation surrogate.
CREATE OR REPLACE FUNCTION core._safe_ref_display(p_val text)
    RETURNS text LANGUAGE sql IMMUTABLE AS $$
  SELECT CASE
    WHEN p_val IS NULL THEN NULL
    ELSE COALESCE(
      core._safe_ref_token(p_val),
      'ref#' || left(core._semantic_ref_key(p_val),12)
    )
  END
$$;
ALTER FUNCTION core._safe_ref_display(text) OWNER TO veripsa_migrator;

-- Resource node_id and edge dst are not universally identical. Produce the display-safe canonical resolver key
-- from the extractor's explicit value when supplied, otherwise from the cg1-compatible node shape.
CREATE OR REPLACE FUNCTION core._resource_canonical_key(
    p_kind text, p_id text, p_name text, p_explicit text DEFAULT NULL)
    RETURNS text LANGUAGE sql IMMUTABLE AS $$
  SELECT core._safe_ref_display(CASE
    WHEN p_explicit IS NOT NULL THEN p_explicit
    WHEN p_kind IN ('table','column','iac_resource','k8s_resource')
         AND p_id LIKE p_kind || '::%' THEN substr(p_id, length(p_kind) + 3)
    WHEN p_kind='config_key' THEN COALESCE(p_name, p_id)
    WHEN p_kind IN (
      'api_type','api_message','api_service','api_operation','api_schema','ci_script','app_command',
      'job_task','job_queue','sibling_stem','role_feature'
    ) THEN p_id
    ELSE NULL
  END)
$$;
ALTER FUNCTION core._resource_canonical_key(text,text,text,text) OWNER TO veripsa_migrator;

-- The exact Resource resolver identity. This hashes the pre-sanitized producer value, so a legal key such as
-- `service<x>` cannot alias the display-safe `service x` spelling. The same function is applied to Edge.dst.
CREATE OR REPLACE FUNCTION core._resource_semantic_key(
    p_kind text, p_id text, p_name text, p_explicit text DEFAULT NULL)
    RETURNS text LANGUAGE sql IMMUTABLE AS $$
  SELECT core._semantic_ref_key(CASE
    WHEN p_explicit IS NOT NULL THEN p_explicit
    WHEN p_kind IN ('table','column','iac_resource','k8s_resource')
         AND p_id LIKE p_kind || '::%' THEN substr(p_id, length(p_kind) + 3)
    WHEN p_kind='config_key' THEN COALESCE(p_name, p_id)
    WHEN p_kind IN (
      'api_type','api_message','api_service','api_operation','api_schema','ci_script','app_command',
      'job_task','job_queue','sibling_stem','role_feature'
    ) THEN p_id
    ELSE NULL
  END)
$$;
ALTER FUNCTION core._resource_semantic_key(text,text,text,text) OWNER TO veripsa_migrator;

-- One Node-side resolver key covers every current lookup family: document path for imports, symbol name for
-- calls, and canonical Resource identity for queries/alters/config edges.
CREATE OR REPLACE FUNCTION core._node_semantic_key(
    p_kind text, p_id text, p_path text, p_name text, p_canonical_key text DEFAULT NULL)
    RETURNS text LANGUAGE sql IMMUTABLE AS $$
  SELECT CASE
    WHEN p_kind IN ('file','config_file') THEN core._semantic_ref_key(p_path)
    WHEN p_kind IN ('def','class') THEN core._semantic_ref_key(p_name)
    ELSE core._resource_semantic_key(p_kind,p_id,p_name,p_canonical_key)
  END
$$;
ALTER FUNCTION core._node_semantic_key(text,text,text,text,text) OWNER TO veripsa_migrator;

-- Every production equality read uses the same COALESCE expression so a pre-cg3 row remains readable until its
-- safe full rebuild. Index that effective identity rather than only the nullable stored digest; otherwise the
-- compatibility expression would hide semantic_key/semantic_dst_key from the planner and turn hub/fan-in joins
-- back into coordinate scans. These are additive, idempotent and explicitly concurrent because the production
-- manifest runner applies schema.sql in psql autocommit mode; they do not block live graph writes while building.
-- A canceled/lock-timed-out concurrent build leaves an invalid catalog shell.
-- The central 05 registry handles it before every module, with one exact set of
-- table/uniqueness/constraint protections.
CREATE INDEX CONCURRENTLY IF NOT EXISTS code_node_coord_kind_effective_semantic
  ON core.code_node (
    account_id,repo,branch,node_kind,
    (COALESCE(
      semantic_key,
      core._node_semantic_key(node_kind,node_id,path,name,canonical_key)
    ))
  );
CREATE INDEX CONCURRENTLY IF NOT EXISTS code_edge_coord_kind_effective_semantic_dst
  ON core.code_edge (
    account_id,repo,branch,edge_kind,
    (COALESCE(semantic_dst_key,core._semantic_ref_key(dst)))
  );
-- Freshness and impact reads ask whether a coordinate contains ANY uncertain
-- evidence.  The normal answer is no; without sparse indexes PostgreSQL must
-- scan every row in a large coordinate to prove that negative.  Index only
-- the exceptional rows so ordinary writes pay no entry cost and the hot
-- all-clear probe remains proportional to uncertainty, not graph size.
CREATE INDEX CONCURRENTLY IF NOT EXISTS code_node_coord_uncertain
  ON core.code_node (account_id,repo,branch)
  WHERE analysis_status IS NOT NULL;
CREATE INDEX CONCURRENTLY IF NOT EXISTS code_edge_coord_uncertain
  ON core.code_edge (account_id,repo,branch)
  WHERE reference_status IS NOT NULL;
DO $$
DECLARE v_bad text[];
BEGIN
  SELECT array_agg(c.relname ORDER BY c.relname)
    INTO v_bad
    FROM pg_class c
    JOIN pg_namespace n ON n.oid=c.relnamespace
    JOIN pg_index i ON i.indexrelid=c.oid
   WHERE n.nspname='core'
     AND c.relname IN (
       'code_node_coord_kind_effective_semantic',
       'code_edge_coord_kind_effective_semantic_dst',
       'code_node_coord_uncertain',
       'code_edge_coord_uncertain'
     )
     AND (NOT i.indisvalid OR NOT i.indisready);
  IF v_bad IS NOT NULL THEN
    RAISE EXCEPTION
      'semantic reference index build is incomplete: %; drop the named invalid index concurrently and reapply',
      v_bad USING ERRCODE='55000';
  END IF;
  IF (
    SELECT count(*) FROM pg_class c
    JOIN pg_namespace n ON n.oid=c.relnamespace
    JOIN pg_index i ON i.indexrelid=c.oid
     WHERE n.nspname='core'
       AND c.relname IN (
         'code_node_coord_kind_effective_semantic',
         'code_edge_coord_kind_effective_semantic_dst',
         'code_node_coord_uncertain',
         'code_edge_coord_uncertain'
       )
       AND i.indisvalid AND i.indisready
  ) <> 4 THEN
    RAISE EXCEPTION 'graph semantic/uncertainty indexes are missing after schema apply'
      USING ERRCODE='55000';
  END IF;
END $$;

-- Deterministic canonical hash over the ACTUALLY PERSISTED coordinate. Row order, DB ids and timestamps are
-- excluded. Every persisted node field (including resource metadata) and every edge is represented as canonical
-- jsonb text, sorted, row-hashed, then folded into one SHA-256 fingerprint using PostgreSQL 16's built-in
-- sha256(bytea) (no pgcrypto/implicit extension dependency).
CREATE OR REPLACE FUNCTION core._coordinate_graph_hash(p_account text, p_repo text, p_branch text)
    RETURNS text LANGUAGE sql STABLE SET search_path TO 'core','pg_catalog' AS $$
  WITH node_rows AS (
    SELECT jsonb_build_array(
      node_id,node_kind,path,name,language,start_line,end_line,content_hash,
      required_arity,optional_arity,has_varargs,has_kwargs,param_names,kwonly_names,shape_fingerprint,
      canonical_key,resource_scope,extractor,confidence,provenance,semantic_key,analysis_status
    )::text AS row_text
      FROM core.code_node
     WHERE account_id=p_account AND repo=p_repo AND branch=p_branch
  ), edge_rows AS (
    SELECT jsonb_build_array(src,dst,edge_kind,semantic_dst_key,reference_status)::text AS row_text
      FROM core.code_edge
     WHERE account_id=p_account AND repo=p_repo AND branch=p_branch
  )
  SELECT encode(sha256(convert_to(
    'cg4-semantic-v2:nodes:' ||
    COALESCE((SELECT string_agg(encode(sha256(convert_to(row_text,'UTF8')),'hex'), '' ORDER BY row_text)
                FROM node_rows), '') ||
    ':edges:' ||
    COALESCE((SELECT string_agg(encode(sha256(convert_to(row_text,'UTF8')),'hex'), '' ORDER BY row_text)
                FROM edge_rows), ''),
    'UTF8'
  )),'hex')
$$;
ALTER FUNCTION core._coordinate_graph_hash(text,text,text) OWNER TO veripsa_migrator;

-- HASH-GENERATION ROLLOUT (cg3 -> cg4). Generation 15 adds the two uncertainty
-- fields to the persisted canonical projection above. A hash written before
-- this schema apply therefore does not identify the rows under cg4's
-- algorithm, even when its coordinate and commit are otherwise current.
-- Historical/version-unknown coordinates are already freshness-behind; clear
-- both copies of their pre-cg4 hash rather than presenting a stale digest as
-- authoritative. The DB-generated persistence marker distinguishes those
-- historical rows from a legacy-compatible FULL ingest performed by this
-- already-cg4 writer: producer version NULL/cg1/cg2/cg3 alone cannot identify
-- the hash algorithm. Repeated schema applies therefore leave every hash
-- computed by the current writer untouched. The predicate is idempotent and
-- the governed-write token keeps the migration inside the same protected table
-- boundary as product writes.
DO $$
DECLARE
  v_account text;
  v_previous_account text := current_setting('core.current_account',true);
BEGIN
  -- graph_version is FORCE-RLS, including for its owner. Pin each tenant
  -- explicitly; an unpinned cross-tenant UPDATE would silently see zero rows
  -- and leave old hashes advertised after a successful schema apply.
  -- These are the same two no-FORCE identity registries used by the
  -- cross-tenant retention/export gates: installation_account discovers
  -- hosted GitHub-App tenants (which need not have a credential), while
  -- credential discovers local/API tenants. account itself is FORCE-RLS, so
  -- it cannot enumerate tenants before a pin.
  FOR v_account IN
    SELECT identities.account_id
      FROM (
        SELECT account_id FROM core.installation_account
        UNION
        SELECT account_id FROM core.credential
      ) identities
     ORDER BY identities.account_id
  LOOP
    PERFORM set_config('core.current_account',v_account,true);
    PERFORM core.mark_governed_write('graph_version');
    UPDATE core.graph_version
       SET graph_hash=NULL,
           observability=CASE
             WHEN jsonb_typeof(observability)='object'
               THEN observability - 'persisted_graph_hash'
             ELSE observability
           END
     WHERE account_id=v_account
       AND extractor_version IS DISTINCT FROM 'cg4'
       AND observability#>>'{persistence,graph_hash_contract}'
             IS DISTINCT FROM 'cg4-semantic-v2'
       AND (
         graph_hash IS NOT NULL
         OR (
           jsonb_typeof(observability)='object'
           AND observability ? 'persisted_graph_hash'
         )
       );
  END LOOP;
  -- Do not leak the last tenant pin to a surrounding schema transaction.
  -- Empty is the honest restoration when the apply entered unpinned.
  PERFORM set_config(
    'core.current_account',COALESCE(v_previous_account,''),true
  );
END $$;

-- _validated_graph_observability: the untrusted graph payload may cross the buyer-writer boundary, but source
-- and diff bodies may not.  Persist ONLY this closed, content-free wire contract:
--   * bounded non-negative integer counters;
--   * exact-key counter maps whose values are bounded non-negative integers;
--   * fixed enums/booleans and 64-hex graph fingerprints; and
--   * a bounded array of fixed full-rebuild reason CODES (never exception text, paths, logs, source or diffs).
-- Unknown keys, arbitrary strings and arbitrary nesting fail closed.  The nested `persistence` object later
-- added by the writer is trusted DB-generated evidence and is deliberately NOT accepted from the caller.
-- Keeping this validation in one private function prevents the full and patch writers from drifting.
CREATE OR REPLACE FUNCTION core._validated_graph_observability(
    p_observability jsonb, p_metrics jsonb, p_expected_mode text)
    RETURNS jsonb LANGUAGE plpgsql IMMUTABLE SET search_path TO 'pg_catalog' AS $$
DECLARE
  v_payload jsonb;
  v_key text;
  v_value jsonb;
  v_map_key text;
  v_map_value jsonb;
  v_reason jsonb;
  v_member_count int;
  v_result jsonb := '{}'::jsonb;
  v_inventory jsonb;
  v_max_count CONSTANT bigint := 5000000;
  v_integer_keys CONSTANT text[] := ARRAY[
    'input_file_count','persistence_excluded_nodes','persistence_excluded_edges',
    'unresolved_reference_count','ambiguous_reference_count','schema_contract_version',
    'resolution_context_file_count'
  ];
  v_count_map_keys CONSTANT text[] := ARRAY[
    'node_kind_counts','edge_kind_counts','nodes_by_substrate','edges_by_substrate',
    'persistence_exclusion_reasons'
  ];
  v_allowed_node_kinds text[];
  v_allowed_edge_kinds text[];
  v_allowed_substrates text[];
  v_allowed_fallback_codes text[];
BEGIN
  IF p_expected_mode IS NULL OR p_expected_mode NOT IN ('full','patch') THEN
    RAISE EXCEPTION 'graph observability validator mode is unsupported'
      USING ERRCODE='22023';
  END IF;
  -- `observability` was a second caller-controlled alias for the same stored object.  No product producer uses
  -- it; accepting two aliases creates ambiguous last-writer-wins semantics.  Reserve it for DB-generated output
  -- and accept only absent/null/{} on input.  `metrics` is the single canonical producer field.
  IF p_observability IS NOT NULL AND jsonb_typeof(p_observability)<>'null' THEN
    IF jsonb_typeof(p_observability)<>'object' THEN
      RAISE EXCEPTION 'graph observability is reserved for writer output'
        USING ERRCODE='22023';
    END IF;
    IF p_observability<>'{}'::jsonb THEN
      RAISE EXCEPTION 'graph observability is reserved for writer output'
        USING ERRCODE='22023';
    END IF;
  END IF;
  v_inventory := core.graph_schema_inventory();
  SELECT array_agg(kind ORDER BY ord) INTO v_allowed_node_kinds
    FROM jsonb_array_elements_text(v_inventory->'extractor_node_kinds')
         WITH ORDINALITY AS allowed(kind,ord);
  SELECT array_agg(kind ORDER BY ord) INTO v_allowed_edge_kinds
    FROM jsonb_array_elements_text(v_inventory->'extractor_edge_kinds')
         WITH ORDINALITY AS allowed(kind,ord);
  SELECT array_agg(substrate ORDER BY ord) INTO v_allowed_substrates
    FROM jsonb_array_elements_text(v_inventory->'observability_substrates')
         WITH ORDINALITY AS allowed(substrate,ord);
  SELECT array_agg(code ORDER BY ord) INTO v_allowed_fallback_codes
    FROM jsonb_array_elements_text(v_inventory->'observability_fallback_reason_codes')
         WITH ORDINALITY AS allowed(code,ord);
  FOREACH v_payload IN ARRAY ARRAY[p_observability,p_metrics] LOOP
    IF v_payload IS NULL OR jsonb_typeof(v_payload)='null' THEN
      CONTINUE;
    END IF;
    IF jsonb_typeof(v_payload)<>'object' THEN
      RAISE EXCEPTION 'graph observability must be an object'
        USING ERRCODE='22023';
    END IF;
    IF octet_length(v_payload::text)>65536 THEN
      RAISE EXCEPTION 'graph observability exceeds 65536 bytes'
        USING ERRCODE='54000';
    END IF;
    SELECT count(*) INTO v_member_count FROM jsonb_object_keys(v_payload);
    IF v_member_count>17 THEN
      RAISE EXCEPTION 'graph observability contains too many metric keys'
        USING ERRCODE='22023';
    END IF;

    FOR v_key,v_value IN SELECT key,value FROM jsonb_each(v_payload) LOOP
      IF v_key=ANY(v_integer_keys) THEN
        IF jsonb_typeof(v_value)<>'number' THEN
          RAISE EXCEPTION 'graph observability integer metric has an invalid type or value'
            USING ERRCODE='22023';
        END IF;
        IF v_value::text !~ '^(0|[1-9][0-9]{0,9})$' THEN
          RAISE EXCEPTION 'graph observability integer metric has an invalid type or value'
            USING ERRCODE='22023';
        END IF;
        IF (v_value::text)::bigint>v_max_count THEN
          RAISE EXCEPTION 'graph observability integer metric has an invalid type or value'
            USING ERRCODE='22023';
        END IF;
        -- The validator accepts the immediately historical contract so a
        -- schema-first cg4 rollout can continue to receive complete cg3 FULL
        -- writes. The governed caller below binds the validated contract to
        -- the declared producer: cg3->v1 and cg4->v2.
        IF v_key='schema_contract_version'
           AND v_value::text NOT IN ('1','2') THEN
          RAISE EXCEPTION 'graph observability schema_contract_version is unsupported'
            USING ERRCODE='22023';
        END IF;
        IF v_key IN ('persistence_excluded_nodes','persistence_excluded_edges')
           AND v_value::text<>'0' THEN
          RAISE EXCEPTION 'graph observability producer persistence exclusions must be zero'
            USING ERRCODE='22023';
        END IF;
        IF v_key='resolution_context_file_count' AND p_expected_mode<>'patch' THEN
          RAISE EXCEPTION 'graph observability resolution context is patch-only'
            USING ERRCODE='22023';
        END IF;

      ELSIF v_key=ANY(v_count_map_keys) THEN
        IF jsonb_typeof(v_value)<>'object' THEN
          RAISE EXCEPTION 'graph observability count map must be an object'
            USING ERRCODE='22023';
        END IF;
        SELECT count(*) INTO v_member_count FROM jsonb_object_keys(v_value);
        IF v_member_count>28 THEN
          RAISE EXCEPTION 'graph observability count map has too many keys'
            USING ERRCODE='22023';
        END IF;
        IF v_key='persistence_exclusion_reasons' AND v_member_count<>0 THEN
          RAISE EXCEPTION 'graph observability producer persistence exclusion reasons must be empty'
            USING ERRCODE='22023';
        END IF;
        FOR v_map_key,v_map_value IN SELECT key,value FROM jsonb_each(v_value) LOOP
          IF jsonb_typeof(v_map_value)<>'number' THEN
            RAISE EXCEPTION 'graph observability count map contains a non-integer value'
              USING ERRCODE='22023';
          END IF;
          IF v_map_value::text !~ '^(0|[1-9][0-9]{0,9})$' THEN
            RAISE EXCEPTION 'graph observability count map contains a non-integer value'
              USING ERRCODE='22023';
          END IF;
          IF (v_map_value::text)::bigint>v_max_count THEN
            RAISE EXCEPTION 'graph observability count map contains a non-integer value'
              USING ERRCODE='22023';
          END IF;
          IF (v_key='node_kind_counts' AND NOT (v_map_key=ANY(v_allowed_node_kinds)))
             OR (v_key='edge_kind_counts' AND NOT (v_map_key=ANY(v_allowed_edge_kinds)))
             OR (v_key IN ('nodes_by_substrate','edges_by_substrate')
                 AND NOT (v_map_key=ANY(v_allowed_substrates)))
             OR v_key='persistence_exclusion_reasons' THEN
            RAISE EXCEPTION 'graph observability count map contains an unsupported key'
              USING ERRCODE='22023';
          END IF;
        END LOOP;

      ELSIF v_key='fallback_full_rebuild_reasons' THEN
        IF jsonb_typeof(v_value)<>'array' THEN
          RAISE EXCEPTION 'graph observability fallback reasons must be a bounded code array'
            USING ERRCODE='22023';
        END IF;
        IF jsonb_array_length(v_value)>16 THEN
          RAISE EXCEPTION 'graph observability fallback reasons must be a bounded code array'
            USING ERRCODE='22023';
        END IF;
        IF p_expected_mode='patch' AND jsonb_array_length(v_value)<>0 THEN
          RAISE EXCEPTION 'graph observability patch fallback reason array must be empty'
            USING ERRCODE='22023';
        END IF;
        FOR v_reason IN SELECT value FROM jsonb_array_elements(v_value) LOOP
          IF jsonb_typeof(v_reason)<>'string'
             OR NOT ((v_reason#>>'{}')=ANY(v_allowed_fallback_codes)) THEN
            RAISE EXCEPTION 'graph observability fallback reason contains an unsupported code'
              USING ERRCODE='22023';
          END IF;
        END LOOP;

      ELSIF v_key='mode' THEN
        IF jsonb_typeof(v_value)<>'string' OR (v_value#>>'{}')<>p_expected_mode THEN
          RAISE EXCEPTION 'graph observability mode contains an unsupported token'
            USING ERRCODE='22023';
        END IF;

      ELSIF v_key='extraction_graph_hash' THEN
        IF jsonb_typeof(v_value)<>'string' OR (v_value#>>'{}') !~ '^[0-9a-f]{64}$' THEN
          RAISE EXCEPTION 'graph observability extraction_graph_hash must be 64 lowercase hex characters'
            USING ERRCODE='22023';
        END IF;

      ELSIF v_key='ambiguity_detection_scope' THEN
        IF jsonb_typeof(v_value)<>'string'
           OR (v_value#>>'{}') NOT IN (
             'retained multi-definer resources, emitted canonical-key collisions, and resolved import fan-out',
             'retained multi-definer resources, emitted canonical-key collisions, and local import candidate ambiguity'
           ) THEN
          RAISE EXCEPTION 'graph observability ambiguity_detection_scope contains an unsupported token'
            USING ERRCODE='22023';
        END IF;

      ELSIF v_key='over_cap' THEN
        IF p_expected_mode<>'full'
           OR jsonb_typeof(v_value)<>'boolean'
           OR v_value::text<>'true' THEN
          RAISE EXCEPTION 'graph observability over_cap must be true on a full ingest'
            USING ERRCODE='22023';
        END IF;

      ELSE
        -- Do not interpolate the attacker-controlled key into the exception:
        -- even error logs must not become a source/diff-body side channel.
        RAISE EXCEPTION 'graph observability contains an unsupported metric key'
          USING ERRCODE='22023';
      END IF;
    END LOOP;
    v_result := v_result || v_payload;
  END LOOP;
  RETURN v_result;
END $$;
ALTER FUNCTION core._validated_graph_observability(jsonb,jsonb,text) OWNER TO veripsa_migrator;

-- _assert_unique_graph_identities: the governed SQL writers accept the same
-- semantic set contract as cg_schema_contract.validate_graph. Node identity is
-- the complete (id,kind,path) tuple (a generated Resource id may also be a
-- legal repository path); Edge identity is (src,dst,kind). Reject duplicate
-- facts before either writer looks up a tenant or mutates a coordinate. A
-- physical UNIQUE index is deliberately not required: historical coordinates
-- may predate this payload contract, while every new full/patch write is
-- guarded at the authority boundary.
CREATE OR REPLACE FUNCTION core._assert_unique_graph_identities(
    p_nodes jsonb, p_edges jsonb)
    RETURNS void LANGUAGE plpgsql IMMUTABLE SET search_path TO 'pg_catalog' AS $$
BEGIN
  IF EXISTS (
    SELECT 1
      FROM jsonb_array_elements(COALESCE(p_nodes,'[]'::jsonb)) AS item(node)
     WHERE jsonb_typeof(node)='object'
       AND jsonb_typeof(node->'id')='string'
       AND btrim(node->>'id')<>''
       AND jsonb_typeof(node->'kind')='string'
       AND jsonb_typeof(node->'path')='string'
       AND btrim(node->>'path')<>''
     GROUP BY node->>'id',node->>'kind',node->>'path'
    HAVING count(*)>1
  ) THEN
    RAISE EXCEPTION 'duplicate graph Node identity (id,kind,path)'
      USING ERRCODE='22023';
  END IF;

  IF EXISTS (
    SELECT 1
      FROM jsonb_array_elements(COALESCE(p_edges,'[]'::jsonb)) AS item(edge)
     WHERE jsonb_typeof(edge)='object'
       AND jsonb_typeof(edge->'src')='string'
       AND btrim(edge->>'src')<>''
       AND jsonb_typeof(edge->'dst')='string'
       AND btrim(edge->>'dst')<>''
       AND jsonb_typeof(edge->'kind')='string'
     GROUP BY edge->>'src',edge->>'dst',edge->>'kind'
    HAVING count(*)>1
  ) THEN
    RAISE EXCEPTION 'duplicate graph Edge identity (src,dst,kind)'
      USING ERRCODE='22023';
  END IF;
END $$;
ALTER FUNCTION core._assert_unique_graph_identities(jsonb,jsonb)
  OWNER TO veripsa_migrator;

-- current_extractor_version: THE CURRENT extraction-logic version TOKEN (content-free — a bounded token, never
-- bodies/counts). This is the SINGLE SOURCE OF TRUTH for "which version of code_graph_extract produced a stored
-- graph": a producer explicitly DECLARES it in the payload, the writer validates that declaration against the
-- known/current token set, and coordinate_graph_sha RETURNS the persisted producer stamp so graph_freshness can
-- compare it with this function (a mismatch/NULL ⇒ behind, the G3 fix). A version-absent legacy producer is
-- accepted during a schema-first rollout but stamps NULL; the DB generation can therefore never impersonate the
-- extractor generation that actually produced the bytes.
--
-- BUMP THIS (cg1 → cg2 → …) — AND db/schema_generation, so the change deploys via predeploy-apply → deploy —
-- WHENEVER code_graph_extract's EXTRACTION SEMANTICS change (a new/changed edge kind, node kind, resolver, or
-- signature-shape that would make the NEW extractor find a coupling the OLD one missed). Do NOT bump it for an
-- unrelated schema change (e.g. a webhook_delivery column) — that would needlessly force a full re-ingest of every
-- coordinate. A bump makes every stored graph read `behind` until its next FULL re-ingest re-stamps the new token
-- (recall-safe: behind → self-heal re-ingest → the G1 withhold covers a failed re-ingest as `unknown`).
CREATE OR REPLACE FUNCTION core.current_extractor_version()
    RETURNS text LANGUAGE sql IMMUTABLE AS $$ SELECT 'cg4'::text $$;
ALTER FUNCTION core.current_extractor_version() OWNER TO veripsa_migrator;

-- Semantic resolver-key storage generation. Version 1 stores exact-reference SHA-256 keys beside the existing
-- shape-sanitized display columns. A legacy 0 coordinate must be rebuilt in full before any path-local patch.
CREATE OR REPLACE FUNCTION core.current_semantic_ref_version()
    RETURNS smallint LANGUAGE sql IMMUTABLE AS $$ SELECT 1::smallint $$;
ALTER FUNCTION core.current_semantic_ref_version() OWNER TO veripsa_migrator;

-- ingest_graph_with_authority: replace the graph FOR THIS COORDINATE only (account,repo,branch) — other
-- coordinates coexist untouched (no tenant-wipe). Content-free (ids/paths/names/edges, never bodies).
CREATE OR REPLACE FUNCTION core.ingest_graph_with_authority(p_graph jsonb, p_repo text DEFAULT '', p_branch text DEFAULT '', p_commit_sha text DEFAULT NULL, p_captured_at timestamptz DEFAULT NULL)
    RETURNS text LANGUAGE plpgsql SECURITY DEFINER SET search_path TO 'core','pg_catalog' AS $$
DECLARE v_agent text; v_account text; v_repo text; v_branch text; v_sha text; v_nodes int; v_edges int; v_quota jsonb;
        v_graph_revision bigint;
        v_n_in bigint; v_e_in bigint; v_input_files bigint; v_max_elems CONSTANT bigint := 5000000;
        v_unknown_node_kinds text[]; v_unknown_edge_kinds text[];
        v_metric_counts_invalid boolean;
        v_metric_document_count bigint;
        v_producer_version text;
        v_hash text; v_metrics_obs jsonb; v_extractor_obs jsonb; v_observability jsonb; v_persistence_obs jsonb;
        v_node_exclusion_reasons jsonb; v_edge_exclusion_reasons jsonb;
        v_node_kind_counts jsonb; v_edge_kind_counts jsonb;
        v_allowed_node_kinds CONSTANT text[] := ARRAY[
          'file','def','class','table','column','config_file','config_key',
          'iac_resource','k8s_resource','api_type','api_message','api_service','api_operation','api_schema',
          'ci_script','app_command','job_task','job_queue','sibling_stem','role_feature'
        ];
        v_allowed_edge_kinds CONSTANT text[] := ARRAY[
          'contains','calls','imports','queries','alters','reads_config','alters_col','queries_col'
        ];
        v_resource_kinds CONSTANT text[] := ARRAY[
          'table','column','config_key','iac_resource','k8s_resource','api_type','api_message','api_service',
          'api_operation','api_schema','ci_script','app_command','job_task','job_queue','sibling_stem','role_feature'
        ];
BEGIN
  IF p_graph IS NULL OR jsonb_typeof(p_graph)<>'object' THEN RAISE EXCEPTION 'graph must be an object {nodes,edges}' USING ERRCODE='22023'; END IF;
  IF p_graph ? 'nodes' AND jsonb_typeof(p_graph->'nodes')<>'array' THEN
    RAISE EXCEPTION 'graph.nodes must be an array' USING ERRCODE='22023';
  END IF;
  IF p_graph ? 'edges' AND jsonb_typeof(p_graph->'edges')<>'array' THEN
    RAISE EXCEPTION 'graph.edges must be an array' USING ERRCODE='22023';
  END IF;
  -- PRODUCER VERSION, NOT DB VERSION, is the only honest stamp for the bytes in this payload. During a
  -- schema-first deploy (or an image rollback), a still-serving historical worker can call this newly-installed
  -- function. Stamping core.current_extractor_version() here would mislabel that legacy graph as current and
  -- suppress the version-behind self-heal indefinitely. Version-absent is the legacy cg1 wire shape: accept for
  -- rollout compatibility but stamp NULL (= unknown/behind). Published historical tokens stay explicit in the
  -- allowlist so the next schema-first rollout can safely accept the immediately-prior producer and mark it
  -- behind; any other/future token is rejected before the coordinate DELETE.
  IF p_graph ? 'extractor_version'
     AND jsonb_typeof(p_graph->'extractor_version') NOT IN ('string','null') THEN
    RAISE EXCEPTION 'graph.extractor_version must be a string token' USING ERRCODE='22023';
  END IF;
  v_producer_version := CASE
    WHEN jsonb_typeof(p_graph->'extractor_version')='string' THEN p_graph->>'extractor_version'
    ELSE NULL
  END;
  IF v_producer_version IS NOT NULL
     AND v_producer_version NOT IN ('cg1','cg2','cg3',core.current_extractor_version()) THEN
    RAISE EXCEPTION 'unsupported graph extractor_version: %',left(v_producer_version,64)
      USING ERRCODE='22023';
  END IF;
  IF v_producer_version IS NOT NULL
     AND (length(v_producer_version)>32 OR v_producer_version !~ '^[A-Za-z0-9_.:-]+$') THEN
    RAISE EXCEPTION 'graph.extractor_version token out of bounds' USING ERRCODE='22023';
  END IF;
  -- cg2 workers already in flight during their schema-first rollout emitted human fallback prose in the
  -- otherwise current metrics shape. Preserve that older compatibility case without reopening a free-form
  -- persistence channel:
  -- for a version-absent/cg1/cg2 FULL writer only, accept a bounded array of strings and collapse every non-empty
  -- legacy array to the one fixed broad code.  The original strings are never compared, logged by SQL, or stored.
  -- cg3+ always goes through the strict fixed-code contract unchanged. A malformed/oversized legacy field is
  -- deliberately left untouched so the closed validator below rejects it before identity lookup or mutation.
  v_metrics_obs := p_graph->'metrics';
  IF (v_producer_version IS NULL OR v_producer_version IN ('cg1','cg2'))
     AND jsonb_typeof(v_metrics_obs)='object'
     AND octet_length(v_metrics_obs::text)<=65536
     AND jsonb_typeof(v_metrics_obs->'fallback_full_rebuild_reasons')='array'
     AND jsonb_array_length(v_metrics_obs->'fallback_full_rebuild_reasons')<=16
     AND NOT EXISTS (
       SELECT 1
         FROM jsonb_array_elements(v_metrics_obs->'fallback_full_rebuild_reasons') reason
        WHERE jsonb_typeof(reason)<>'string'
     ) THEN
    v_metrics_obs := jsonb_set(
      v_metrics_obs,
      '{fallback_full_rebuild_reasons}',
      CASE
        WHEN jsonb_array_length(v_metrics_obs->'fallback_full_rebuild_reasons')=0
          THEN '[]'::jsonb
        ELSE '["incremental_internal_error"]'::jsonb
      END
    );
  END IF;
  -- Validate and normalize both untrusted observability aliases before identity lookup, quota checks, or the
  -- coordinate DELETE below.  A rejected payload therefore cannot mutate an otherwise valid graph.
  v_extractor_obs := core._validated_graph_observability(
    p_graph->'observability',v_metrics_obs,'full');
  -- Strict producer/contract pairing closes both rollout directions. The
  -- deployed historical cg3 worker is allowed to complete a v1 FULL write and
  -- is stamped behind current cg4; it may not claim cg4's uncertainty-aware
  -- v2 contract. Conversely a current cg4 payload must prove v2 and may not
  -- omit or downgrade the contract marker. Earlier/versionless FULL writers
  -- may omit the marker, but when present they can only claim historical v1.
  IF v_producer_version=core.current_extractor_version() THEN
    IF v_extractor_obs->>'schema_contract_version' IS DISTINCT FROM '2' THEN
      RAISE EXCEPTION
        'graph schema_contract_version does not match extractor_version'
        USING ERRCODE='22023';
    END IF;
  ELSIF v_producer_version='cg3' THEN
    IF v_extractor_obs->>'schema_contract_version' IS DISTINCT FROM '1'
       AND NOT (
         NOT (v_extractor_obs ? 'schema_contract_version')
         AND NOT (v_extractor_obs ? 'ambiguity_detection_scope')
         AND v_extractor_obs->>'over_cap'='true'
         AND v_extractor_obs ? 'mode'
         AND v_extractor_obs ? 'input_file_count'
         AND v_extractor_obs ? 'fallback_full_rebuild_reasons'
         AND (
           SELECT count(*) FROM jsonb_object_keys(v_extractor_obs)
         )=4
         AND jsonb_typeof(p_graph->'nodes')='array'
         AND jsonb_array_length(p_graph->'nodes')=0
         AND jsonb_typeof(p_graph->'edges')='array'
         AND jsonb_array_length(p_graph->'edges')=0
       ) THEN
      RAISE EXCEPTION
        'graph schema_contract_version does not match extractor_version'
        USING ERRCODE='22023';
    END IF;
  ELSIF v_extractor_obs ? 'schema_contract_version'
        AND v_extractor_obs->>'schema_contract_version'<>'1' THEN
    RAISE EXCEPTION
      'graph schema_contract_version does not match extractor_version'
      USING ERRCODE='22023';
  END IF;
  -- The immediately historical cg3 extractor emitted the old fixed
  -- ambiguity-scope token.  Accept that exact closed token only for historical
  -- producers during schema-first rollout; a current cg4 producer may claim
  -- only the v2 token when the field is present.  The markerless cg3 over-cap
  -- wire above has no scope field by construction.
  IF v_extractor_obs ? 'ambiguity_detection_scope' THEN
    IF (
      v_producer_version=core.current_extractor_version()
      AND v_extractor_obs->>'ambiguity_detection_scope'<>
        'retained multi-definer resources, emitted canonical-key collisions, and local import candidate ambiguity'
    ) OR (
      v_producer_version IS DISTINCT FROM core.current_extractor_version()
      AND v_extractor_obs->>'ambiguity_detection_scope'<>
        'retained multi-definer resources, emitted canonical-key collisions, and resolved import fan-out'
    ) THEN
      RAISE EXCEPTION
        'graph ambiguity_detection_scope does not match extractor_version'
        USING ERRCODE='22023';
    END IF;
  END IF;
  v_sha := NULLIF(btrim(COALESCE(p_commit_sha,'')),'');
  IF v_sha IS NOT NULL AND (length(v_sha)>64 OR v_sha !~ '^[0-9a-fA-F]+$') THEN RAISE EXCEPTION 'commit_sha out of bounds' USING ERRCODE='23514'; END IF;
  v_repo := left(COALESCE(p_repo,''),512); v_branch := left(COALESCE(p_branch,''),512);
  -- PER-PAYLOAD INBOUND CAP (audit:scale): the over-quota check below is a PRE-WRITE "already over?" test with
  -- NO per-call ceiling, so ONE call could ship an unbounded {nodes,edges} and overshoot the footprint in a
  -- single write (the buyer's own-writer --push path has no App-side tarball cap — the DB is the authoritative
  -- boundary). Cap the inbound array LENGTHS first (cheap O(1), mirrors _ranges_from_jsonb's 512 cap): above
  -- the cap REFUSE the whole call (insert nothing) + return the bounded sentinel. Only well-formed arrays are
  -- measured; a non-array nodes/edges is treated as 0 here and filtered to nothing by the INSERTs below.
  v_n_in := CASE WHEN jsonb_typeof(p_graph->'nodes')='array' THEN jsonb_array_length(p_graph->'nodes') ELSE 0 END;
  v_e_in := CASE WHEN jsonb_typeof(p_graph->'edges')='array' THEN jsonb_array_length(p_graph->'edges') ELSE 0 END;
  IF v_n_in > v_max_elems OR v_e_in > v_max_elems THEN
    RETURN core._graph_too_large_result(v_max_elems, GREATEST(v_n_in, v_e_in))::text;
  END IF;
  -- Match the producer-side semantic-set contract before any identity lookup,
  -- quota check, lock or coordinate mutation. Silently persisting duplicate
  -- payload facts would make DB counts/hash differ from canonical extraction.
  PERFORM core._assert_unique_graph_identities(
    COALESCE(p_graph->'nodes','[]'::jsonb),
    COALESCE(p_graph->'edges','[]'::jsonb)
  );
  -- KIND DRIFT IS A HARD ERROR. A new extractor kind must expand the schema contract first; no row may vanish
  -- behind an IN-list filter. The exception is raised before any coordinate DELETE.
  SELECT array_agg(k ORDER BY k) INTO v_unknown_node_kinds
    FROM (
      SELECT DISTINCT left(COALESCE(core._safe_ref_token(n->>'kind'),'<null>'),64) AS k
        FROM jsonb_array_elements(COALESCE(p_graph->'nodes','[]'::jsonb)) n
       WHERE n->>'kind' IS NULL OR NOT (n->>'kind' = ANY(v_allowed_node_kinds))
    ) q;
  IF COALESCE(array_length(v_unknown_node_kinds,1),0) > 0 THEN
    RAISE EXCEPTION 'unknown graph node kind(s): %', array_to_string(v_unknown_node_kinds,',')
      USING ERRCODE='22023';
  END IF;
  SELECT array_agg(k ORDER BY k) INTO v_unknown_edge_kinds
    FROM (
      SELECT DISTINCT left(COALESCE(core._safe_ref_token(e->>'kind'),'<null>'),64) AS k
        FROM jsonb_array_elements(COALESCE(p_graph->'edges','[]'::jsonb)) e
       WHERE e->>'kind' IS NULL OR NOT (e->>'kind' = ANY(v_allowed_edge_kinds))
    ) q;
  IF COALESCE(array_length(v_unknown_edge_kinds,1),0) > 0 THEN
    RAISE EXCEPTION 'unknown graph edge kind(s): %', array_to_string(v_unknown_edge_kinds,',')
      USING ERRCODE='22023';
  END IF;
  -- UNCERTAINTY IS A CLOSED, FIRST-CLASS CONTRACT. Unknown tokens and markers attached to non-document nodes
  -- are rejected before any coordinate mutation; they are never silently normalized to NULL (= falsely known).
  IF EXISTS (
    SELECT 1 FROM jsonb_array_elements(COALESCE(p_graph->'nodes','[]'::jsonb)) n
     WHERE n ? 'analysis_status'
       AND NOT (
         jsonb_typeof(n->'analysis_status')='null'
         OR (
           jsonb_typeof(n->'analysis_status')='string'
           AND n->>'analysis_status' IN ('failed','ambiguous','incomplete')
           AND n->>'kind' IN ('file','config_file')
         )
       )
  ) THEN
    RAISE EXCEPTION 'invalid node analysis_status (expected null|failed|ambiguous|incomplete on file/config_file)'
      USING ERRCODE='22023';
  END IF;
  IF EXISTS (
    SELECT 1 FROM jsonb_array_elements(COALESCE(p_graph->'edges','[]'::jsonb)) e
     WHERE e ? 'reference_status'
       AND NOT (
         jsonb_typeof(e->'reference_status')='null'
         OR (
           jsonb_typeof(e->'reference_status')='string'
           AND e->>'reference_status' IN ('ambiguous','unresolved')
         )
       )
  ) THEN
    RAISE EXCEPTION 'invalid edge reference_status (expected null|ambiguous|unresolved)'
      USING ERRCODE='22023';
  END IF;
  -- When producer kind-count maps are present, they are complete and must describe this exact inbound payload.
  -- This prevents a caller from persisting forged DB-knowable counts while still keeping metrics optional for
  -- the schema-first legacy producer.
  IF v_extractor_obs ? 'node_kind_counts' THEN
    SELECT
      (SELECT count(*) FROM jsonb_object_keys(v_extractor_obs->'node_kind_counts'))
        <> cardinality(v_allowed_node_kinds)
      OR EXISTS (
        SELECT 1 FROM jsonb_each_text(v_extractor_obs->'node_kind_counts') AS metric(kind,count_text)
         WHERE count_text::bigint <> (
           SELECT count(*) FROM jsonb_array_elements(COALESCE(p_graph->'nodes','[]'::jsonb)) n
            WHERE n->>'kind'=metric.kind
         )
      )
      INTO v_metric_counts_invalid;
    IF v_metric_counts_invalid THEN
      RAISE EXCEPTION 'graph observability node_kind_counts do not match graph.nodes'
        USING ERRCODE='22023';
    END IF;
  END IF;
  IF v_extractor_obs ? 'edge_kind_counts' THEN
    SELECT
      (SELECT count(*) FROM jsonb_object_keys(v_extractor_obs->'edge_kind_counts'))
        <> cardinality(v_allowed_edge_kinds)
      OR EXISTS (
        SELECT 1 FROM jsonb_each_text(v_extractor_obs->'edge_kind_counts') AS metric(kind,count_text)
         WHERE count_text::bigint <> (
           SELECT count(*) FROM jsonb_array_elements(COALESCE(p_graph->'edges','[]'::jsonb)) e
            WHERE e->>'kind'=metric.kind
         )
      )
      INTO v_metric_counts_invalid;
    IF v_metric_counts_invalid THEN
      RAISE EXCEPTION 'graph observability edge_kind_counts do not match graph.edges'
        USING ERRCODE='22023';
    END IF;
  END IF;
  IF v_extractor_obs ? 'nodes_by_substrate' THEN
    SELECT COALESCE(sum((value::text)::bigint),0)<>v_n_in
      INTO v_metric_counts_invalid
      FROM jsonb_each(v_extractor_obs->'nodes_by_substrate');
    IF v_metric_counts_invalid THEN
      RAISE EXCEPTION 'graph observability nodes_by_substrate total does not match graph.nodes'
        USING ERRCODE='22023';
    END IF;
  END IF;
  IF v_extractor_obs ? 'edges_by_substrate' THEN
    SELECT COALESCE(sum((value::text)::bigint),0)<>v_e_in
      INTO v_metric_counts_invalid
      FROM jsonb_each(v_extractor_obs->'edges_by_substrate');
    IF v_metric_counts_invalid THEN
      RAISE EXCEPTION 'graph observability edges_by_substrate total does not match graph.edges'
        USING ERRCODE='22023';
    END IF;
  END IF;
  -- Full extraction normally has one document node per analyzed input path.  The deliberate over-cap empty
  -- graph is the sole exception: it records the measured input count while persisting no partial nodes.
  IF v_extractor_obs ? 'input_file_count'
     AND COALESCE((v_extractor_obs->>'over_cap')::boolean,false)=false THEN
    SELECT count(DISTINCT n->>'path') INTO v_metric_document_count
      FROM jsonb_array_elements(COALESCE(p_graph->'nodes','[]'::jsonb)) n
     WHERE n->>'kind' IN ('file','config_file') AND n->>'path' IS NOT NULL;
    IF (v_extractor_obs->>'input_file_count')::bigint<>v_metric_document_count THEN
      RAISE EXCEPTION 'graph observability input_file_count does not match document paths'
        USING ERRCODE='22023';
    END IF;
  END IF;
  IF (v_extractor_obs ? 'unresolved_reference_count'
      AND (v_extractor_obs->>'unresolved_reference_count')::bigint>v_n_in+(2*v_e_in))
     OR (v_extractor_obs ? 'ambiguous_reference_count'
      AND (v_extractor_obs->>'ambiguous_reference_count')::bigint>v_n_in+(2*v_e_in)) THEN
    RAISE EXCEPTION 'graph observability reference count exceeds the payload evidence bound'
      USING ERRCODE='22023';
  END IF;
  -- Resource metadata is optional, but when supplied it is type/size checked and rejected loudly. Missing cg1
  -- metadata is derived/null-compatible below; malformed current-version metadata is never silently erased.
  IF EXISTS (
    SELECT 1 FROM jsonb_array_elements(COALESCE(p_graph->'nodes','[]'::jsonb)) n
     WHERE n->>'kind' = ANY(v_resource_kinds)
       AND (
         (n ? 'canonical_key' AND jsonb_typeof(n->'canonical_key') NOT IN ('string','null'))
         OR (jsonb_typeof(n->'canonical_key')='string'
             AND (length(n->>'canonical_key')=0
                  OR length(n->>'canonical_key')>1600))
         OR (n ? 'resource_scope' AND jsonb_typeof(n->'resource_scope') NOT IN ('string','null'))
         OR (jsonb_typeof(n->'resource_scope')='string'
             AND (core._safe_ref_token(n->>'resource_scope') IS NULL
                  OR length(core._safe_ref_token(n->>'resource_scope'))>1024))
         OR (n ? 'scope' AND jsonb_typeof(n->'scope') NOT IN ('string','null'))
         OR (jsonb_typeof(n->'scope')='string'
             AND (core._safe_ref_token(n->>'scope') IS NULL
                  OR length(core._safe_ref_token(n->>'scope'))>1024))
         OR (n ? 'extractor' AND jsonb_typeof(n->'extractor') NOT IN ('string','null'))
         OR (jsonb_typeof(n->'extractor')='string'
             AND (core._safe_ref_token(n->>'extractor') IS NULL
                  OR length(core._safe_ref_token(n->>'extractor'))>128))
         OR (n ? 'confidence' AND jsonb_typeof(n->'confidence') NOT IN ('number','null'))
         OR (jsonb_typeof(n->'confidence')='number'
             AND ((n->>'confidence')::numeric < 0 OR (n->>'confidence')::numeric > 1))
         OR (n ? 'provenance' AND jsonb_typeof(n->'provenance') NOT IN ('object','null'))
         OR (jsonb_typeof(n->'provenance')='object' AND octet_length((n->'provenance')::text)>8192)
       )
  ) THEN
    RAISE EXCEPTION 'invalid resource metadata (expected bounded canonical_key/scope/extractor, confidence 0..1, object provenance)'
      USING ERRCODE='22023';
  END IF;
  -- A payload which claims the current extractor version must carry the complete first-class resource contract.
  -- Legacy/version-absent payloads remain readable during rollout but are stamped behind/unknown below; they
  -- cannot impersonate the current producer by relying on SQL's compatibility derivations.
  IF v_producer_version IN ('cg3',core.current_extractor_version()) AND EXISTS (
    SELECT 1 FROM jsonb_array_elements(COALESCE(p_graph->'nodes','[]'::jsonb)) n
     WHERE n->>'kind'=ANY(v_resource_kinds)
       AND (
         jsonb_typeof(n->'canonical_key') IS DISTINCT FROM 'string'
         OR length(n->>'canonical_key')=0
         OR jsonb_typeof(n->'extractor') IS DISTINCT FROM 'string'
         OR core._safe_ref_token(n->>'extractor') IS NULL
         OR jsonb_typeof(n->'confidence') IS DISTINCT FROM 'number'
         OR jsonb_typeof(n->'provenance') IS DISTINCT FROM 'object'
       )
  ) THEN
    RAISE EXCEPTION 'cg3+ resource nodes require canonical_key, extractor, confidence and provenance'
      USING ERRCODE='22023';
  END IF;
  SELECT count(DISTINCT n->>'path') INTO v_input_files
    FROM jsonb_array_elements(COALESCE(p_graph->'nodes','[]'::jsonb)) n
   WHERE n->>'kind' IN ('file','config_file') AND n->>'path' IS NOT NULL;
  SELECT agent, account INTO v_agent, v_account FROM core.establish_session_write_context() AS c(agent,account);
  -- THE WALL: if this account is over the free line, REFUSE (insert nothing) + return the structured signal.
  -- Advisory (never raises); the App detects quota_exceeded and stops ingesting for the account. (Account pinned.)
  v_quota := core._refuse_if_over_quota(v_account);
  IF v_quota IS NOT NULL THEN RETURN v_quota::text; END IF;
  -- GRAPH MUTATION LOCK ORDER: stable-id (authenticated identity flows only, when present) → repo →
  -- account(shared/exclusive, when needed) → coordinate (full/patch only). This generic writer owns the
  -- repo→account(shared/live)→coordinate suffix. Repo purge/rename/transfer and cold retention take the same repo
  -- key before graph DELETE (retention also re-checks freshness after acquiring it); account-wide purge/erase
  -- takes the account fence exclusively. Not every path needs every tier, but no path may take a used tier out of
  -- order. Without the shared repo tier, lifecycle DELETE can land after patch CAS and before its writes, stamping
  -- a falsely partial graph. Hash collisions only over-serialize.
  PERFORM pg_advisory_xact_lock(
    hashtext(CASE WHEN v_account LIKE 'ACCT-GH-%' THEN substr(v_account,9) ELSE v_account END),
    hashtext(v_repo)
  );
  PERFORM core.assert_account_live_with_authority();
  -- Full and patch share this coordinate key as the final, narrow lock tier.
  PERFORM pg_advisory_xact_lock(
    hashtext('core.graph_coordinate'),
    hashtext(v_account||chr(31)||v_repo||chr(31)||v_branch)
  );
  -- DELIVERY-ORDER MONOTONICITY: GitHub does NOT guarantee webhook ORDER (and re-delivers), so two pushes to the
  -- same coordinate can arrive REORDERED — an OLDER tree AFTER a newer one. Overwriting the stored graph with the
  -- older tree would REGRESS the prediction baseline into the past. captured_at carries the head COMMIT's time
  -- (content-free git metadata) for exactly this: REFUSE to overwrite when the stored graph was captured at a
  -- STRICTLY NEWER commit time than this ingest (a stale/reordered retry) — the graph keeps the newer tree, no
  -- writes happen, a 'stale' signal is returned (the caller skips the heavy clone too). GUARDED: only when BOTH
  -- times are known AND this ingest is for a DIFFERENT sha (a same-sha re-ingest at the same/older time is a
  -- harmless refresh, allowed). No timestamps → the guard is inert (ingest normally). Not for a tag/odd push.
  IF p_captured_at IS NOT NULL THEN
    PERFORM 1 FROM core.graph_version
      WHERE account_id=v_account AND repo=v_repo AND branch=v_branch
        AND captured_at IS NOT NULL AND captured_at > p_captured_at
        AND commit_sha IS DISTINCT FROM v_sha;
    IF FOUND THEN
      RETURN json_build_object('ok',true,'stale',true,'skipped','older commit than stored graph (reordered delivery)',
                               'repo',v_repo,'branch',v_branch)::text;
    END IF;
  END IF;
  -- ABA-SAFE WRITE IDENTITY: commit SHA can return to the same value after two intervening graph writes
  -- (P→B→P), and a coordinate-local counter can repeat after graph_version deletion/recreation.  Stamp the
  -- global NO-CYCLE sequence before any graph DELETE. Sequence values are never reused, including on rollback.
  v_graph_revision := nextval('core.graph_revision_seq');
  -- Exact, mutually-exclusive reasons for every structurally invalid row that will not reach persistence.
  -- Unknown kinds and malformed resource metadata already raised above, so they can never appear as exclusions.
  SELECT COALESCE(jsonb_object_agg(reason,cnt ORDER BY reason),'{}'::jsonb)
    INTO v_node_exclusion_reasons
    FROM (
      SELECT reason,count(*) AS cnt FROM (
        SELECT CASE
          WHEN n->>'id' IS NULL THEN 'missing_id'
          WHEN n->>'path' IS NULL THEN 'missing_path'
          WHEN length(n->>'path')>1024 THEN 'path_too_long'
          WHEN length(n->>'id')>1600 THEN 'id_too_long'
          WHEN length(n->>'name')>512 THEN 'name_too_long'
          ELSE NULL END AS reason
        FROM jsonb_array_elements(COALESCE(p_graph->'nodes','[]'::jsonb)) n
      ) classified WHERE reason IS NOT NULL GROUP BY reason
    ) counts;
  SELECT COALESCE(jsonb_object_agg(reason,cnt ORDER BY reason),'{}'::jsonb)
    INTO v_edge_exclusion_reasons
    FROM (
      SELECT reason,count(*) AS cnt FROM (
        SELECT CASE
          WHEN e->>'src' IS NULL THEN 'missing_src'
          WHEN e->>'dst' IS NULL THEN 'missing_dst'
          WHEN length(e->>'dst')=0 THEN 'empty_dst'
          WHEN length(e->>'src')>1024 THEN 'src_too_long'
          WHEN length(e->>'dst')>1600 THEN 'dst_too_long'
          ELSE NULL END AS reason
        FROM jsonb_array_elements(COALESCE(p_graph->'edges','[]'::jsonb)) e
      ) classified WHERE reason IS NOT NULL GROUP BY reason
    ) counts;
  PERFORM core.mark_governed_write('code_node');
  DELETE FROM core.code_node WHERE account_id=v_account AND repo=v_repo AND branch=v_branch;
  INSERT INTO core.code_node(account_id, repo, branch, node_id, node_kind, path, name, language, start_line, end_line, content_hash,
                             required_arity, optional_arity, has_varargs, has_kwargs, param_names, kwonly_names, shape_fingerprint,
                             canonical_key, resource_scope, extractor, confidence, provenance, semantic_key, analysis_status)
  -- Store the established presentation-safe name and a separate exact resolver digest.
  SELECT v_account, v_repo, v_branch, n->>'id', n->>'kind', n->>'path', core._safe_ref_token(n->>'name'), left(n->>'language',32),
         -- CONTENT-FREE SPAN: line numbers only, accepted only when WELL-FORMED (both present, 1-based,
         -- ordered, bounded). A junk/partial span is dropped to NULL → that symbol resolves at file level
         -- (the safety net), never a stored bad span. (`core._clean_span` returns NULL unless valid.)
         (core._clean_span(n->'start_line', n->'end_line')).start_line,
         (core._clean_span(n->'start_line', n->'end_line')).end_line,
         -- FRESHNESS KEY (content-free): a FILE node's content hash (git-blob-sha — a fingerprint, not bytes),
         -- accepted ONLY when it is a clean lower-cased hex string of bounded length; anything else → NULL =
         -- unknown = file-level fallback (a bad/spoofed hash can never trick the demotion into trusting it).
         -- `_clean_hash` lowercases so the App's hash and the graph's hash compare case-insensitively.
         core._clean_hash(n->>'content_hash'),
         -- COMPATIBILITY SIGNATURE SHAPE (content-free): the def node's extractor-emitted signature metadata
         -- (#831) — parameter names + arity counts + varargs/kwargs flags + a hash fingerprint, NEVER
         -- defaults/annotations/bodies. Each field is INDEPENDENTLY validated and degrades to NULL on ANY
         -- malformed input (wrong type, out of bounds, junk) — never an error, so a crafted shape field can
         -- never abort the shared ingest transaction. A non-def node carries none of these keys → all seven
         -- NULL, exactly the pre-shape row. `_clean_hash` bounds the fingerprint to lower-cased hex (same
         -- family as content_hash).
         core._clean_arity(n->'required_arity'), core._clean_arity(n->'optional_arity'),
         core._clean_flag(n->'has_varargs'), core._clean_flag(n->'has_kwargs'),
         core._clean_name_array(n->'param_names'), core._clean_name_array(n->'kwonly_names'),
         core._clean_hash(n->>'shape_fingerprint'),
         core._resource_canonical_key(n->>'kind',n->>'id',n->>'name',n->>'canonical_key'),
         CASE WHEN n->>'kind'=ANY(v_resource_kinds)
              THEN core._safe_ref_token(COALESCE(n->>'resource_scope',n->>'scope')) ELSE NULL END,
         CASE WHEN n->>'kind'=ANY(v_resource_kinds)
              THEN COALESCE(core._safe_ref_token(n->>'extractor'),core._safe_ref_token(left(n->>'language',128)))
              ELSE NULL END,
         CASE WHEN n->>'kind'=ANY(v_resource_kinds) AND jsonb_typeof(n->'confidence')='number'
              THEN (n->>'confidence')::double precision ELSE NULL END,
         CASE WHEN n->>'kind'=ANY(v_resource_kinds) AND jsonb_typeof(n->'provenance')='object'
              THEN n->'provenance' ELSE NULL END,
         core._node_semantic_key(
           n->>'kind',n->>'id',n->>'path',n->>'name',n->>'canonical_key'
         ),
         n->>'analysis_status'
  FROM jsonb_array_elements(COALESCE(p_graph->'nodes','[]'::jsonb)) n
  WHERE n->>'id' IS NOT NULL AND n->>'kind'=ANY(v_allowed_node_kinds)
    AND n->>'path' IS NOT NULL AND length(n->>'path')<=1024 AND length(n->>'id')<=1600
    AND (n->>'name' IS NULL OR length(n->>'name')<=512);
  GET DIAGNOSTICS v_nodes = ROW_COUNT;
  PERFORM core.mark_governed_write('code_edge');
  DELETE FROM core.code_edge WHERE account_id=v_account AND repo=v_repo AND branch=v_branch;
  -- Keep the existing display-safe dst while hashing the exact extractor endpoint for graph joins.
  INSERT INTO core.code_edge(account_id, repo, branch, src, dst, edge_kind, semantic_dst_key, reference_status)
  SELECT v_account, v_repo, v_branch, e->>'src',
         COALESCE(core._safe_ref_token(e->>'dst'),'ref#'||left(core._semantic_ref_key(e->>'dst'),12)),
         e->>'kind',
         core._semantic_ref_key(e->>'dst'),
         e->>'reference_status'
  FROM jsonb_array_elements(COALESCE(p_graph->'edges','[]'::jsonb)) e
  WHERE e->>'src' IS NOT NULL AND e->>'dst' IS NOT NULL AND length(e->>'dst')>0
    AND e->>'kind'=ANY(v_allowed_edge_kinds)
    AND length(e->>'src')<=1024 AND length(e->>'dst')<=1600;
  GET DIAGNOSTICS v_edges = ROW_COUNT;
  SELECT COALESCE(jsonb_object_agg(node_kind,cnt ORDER BY node_kind),'{}'::jsonb)
    INTO v_node_kind_counts
    FROM (SELECT node_kind,count(*) AS cnt FROM core.code_node
           WHERE account_id=v_account AND repo=v_repo AND branch=v_branch GROUP BY node_kind) q;
  SELECT COALESCE(jsonb_object_agg(edge_kind,cnt ORDER BY edge_kind),'{}'::jsonb)
    INTO v_edge_kind_counts
    FROM (SELECT edge_kind,count(*) AS cnt FROM core.code_edge
           WHERE account_id=v_account AND repo=v_repo AND branch=v_branch GROUP BY edge_kind) q;
  v_hash := core._coordinate_graph_hash(v_account,v_repo,v_branch);
  v_persistence_obs := jsonb_build_object(
    'mode','full',
    'graph_hash_contract','cg4-semantic-v2',
    'graph_revision',v_graph_revision,
    'semantic_ref_version',1,
    'input_files',v_input_files,
    'nodes_input',v_n_in,
    'edges_input',v_e_in,
    'nodes_persisted',v_nodes,
    'edges_persisted',v_edges,
    'producer_extractor_version',v_producer_version,
    'node_counts_by_kind',v_node_kind_counts,
    'edge_counts_by_kind',v_edge_kind_counts,
    'exclusions',jsonb_build_object(
      'nodes',jsonb_build_object('count',v_n_in-v_nodes,'reasons',v_node_exclusion_reasons),
      'edges',jsonb_build_object('count',v_e_in-v_edges,'reasons',v_edge_exclusion_reasons)
    ),
    'evidence_only_edge_kinds',jsonb_build_array('alters_col','queries_col')
  );
  v_observability := v_extractor_obs || jsonb_build_object(
    'persistence',v_persistence_obs,
    'persisted_graph_hash',v_hash
  );
  PERFORM core.mark_governed_write('graph_version');
  -- A graph payload proves the coordinate's CONTENT, never the GitHub object's stable IDENTITY.  Clear any
  -- previous repository.id on every successful replacement; the authenticated App path re-stamps the current
  -- id in the SAME transaction via reconcile_repo_identity_with_authority.  Generic writers deliberately leave
  -- NULL, so an old transfer/delete can never mistake a same-name replacement graph for the former object.
  -- EXTRACTOR VERSION STAMP (G3): a FULL ingest re-extracts the WHOLE coordinate with the PAYLOAD'S producer
  -- version. Only an explicit current-version payload can stamp cg4. A schema-first cg3 worker is accepted only
  -- with contract v1 and remains honestly historical/behind, so the next new-worker freshness check self-heals.
  -- Version-absent/cg1/cg2 writers retain their older compatibility behavior and can never impersonate cg4.
  -- CLOCK IS NEVER ERASED (delivery-order guard, #965-followup). `captured_at` is the ONLY delivery-order clock
  -- this coordinate has, and the reordered-delivery guard above is armed ONLY when BOTH the incoming and the
  -- stored clock are non-NULL. A payload-less writer (the self-heal re-ingest, backfill) has no push payload and
  -- passes p_captured_at=NULL — with a bare EXCLUDED.captured_at that write DISARMED the guard for every later
  -- push, so a backlogged OLDER push then overwrote the just-healed HEAD, the next event read `behind` again, and
  -- the coordinate paid another whole-repo re-ingest. A closed waste loop (measured: the same HEAD fully
  -- re-ingested 4x in 90 minutes). COALESCE keeps the previous clock when the writer has none: it can only make
  -- the guard fire MORE often, never less, so it strictly strengthens graph monotonicity.
  INSERT INTO core.graph_version(account_id, repo, branch, commit_sha, captured_at, ingested_at, node_count, edge_count,
                                 repo_id, extractor_version, graph_hash, observability, graph_revision,
                                 semantic_ref_version)
  VALUES (v_account, v_repo, v_branch, v_sha, p_captured_at, now(), v_nodes, v_edges,
          NULL, v_producer_version, v_hash, v_observability, v_graph_revision, 1)
  ON CONFLICT (account_id, repo, branch) DO UPDATE SET commit_sha=EXCLUDED.commit_sha,
    captured_at=COALESCE(EXCLUDED.captured_at, core.graph_version.captured_at),
    ingested_at=EXCLUDED.ingested_at, node_count=EXCLUDED.node_count, edge_count=EXCLUDED.edge_count, repo_id=NULL,
    extractor_version=EXCLUDED.extractor_version, graph_hash=EXCLUDED.graph_hash,
    observability=EXCLUDED.observability, graph_revision=EXCLUDED.graph_revision,
    semantic_ref_version=EXCLUDED.semantic_ref_version;
  -- COLD-STATS SIGNAL (#169): this just BULK DELETE+INSERTed the whole coordinate into code_node/code_edge, so
  -- the planner stats are now stale and the FIRST main_impact_surface would mis-plan the O(edges) adjacency into
  -- a multi-minute hang until autovacuum's ANALYZE lags in. ANALYZE cannot run in this txn (it is a transaction
  -- block), so flag the SESSION (is_local=false → survives this txn's COMMIT, REVERTS on its rollback) and let
  -- the App run a targeted ANALYZE AFTER commit (make_db_processor._refresh_graph_stats_if_bulk_loaded). We use
  -- a synchronous session flag, NOT pg_stat n_mod_since_analyze, because the stats collector updates
  -- ASYNCHRONOUSLY (~1s lag) — read immediately post-commit it still shows 0, so it would miss the ANALYZE.
  PERFORM set_config('core.graph_bulk_loaded', '1', false);
  RETURN json_build_object('ok',true,'nodes',v_nodes,'edges',v_edges,'repo',v_repo,'branch',v_branch,
                           'graph_revision',v_graph_revision,
                           'semantic_ref_version',1,
                           'graph_hash',v_hash,'observability',v_observability)::text;
END $$;
ALTER FUNCTION core.ingest_graph_with_authority(jsonb,text,text,text,timestamptz) OWNER TO veripsa_migrator;

-- ============================================================================================
-- refresh_graph_stats — the COLD-STATS maintenance hook (#169). After a bulk graph load the FIRST
-- main_impact_surface plans the O(edges) recursive adjacency over code_node/code_edge; on EMPTY/STALE planner
-- stats (a fresh/large ingest, before autovacuum's delayed ANALYZE lands) it mis-plans into a multi-minute
-- CPU-bound scan (measured ~121s vs ~0.5s post-ANALYZE on a 13.6k-node / 18.4k-edge graph — byte-identical
-- result). This refreshes the two graph tables' stats so the first brain call plans correctly.
--
-- WHY A SECURITY DEFINER *PROCEDURE* (three constraints met at once):
--   1. TRANSACTION: ANALYZE cannot run inside a transaction block, and the per-event ingest runs in ONE txn
--      (#106). A plpgsql FUNCTION body is a transaction block (ANALYZE there raises); a PROCEDURE invoked via
--      `CALL` on an AUTOCOMMIT connection is NOT — so ANALYZE runs cleanly. The App CALLs this AFTER the body
--      txn commits, on a fresh autocommit connection (make_db_processor._refresh_graph_stats_if_bulk_loaded).
--   2. PRIVILEGE (the moat is preserved): the App connects as the least-privilege veripsa_app (NEVER the DB
--      owner) — and on PG16 ANALYZE requires the table OWNER (no MAINTAIN grant until PG17). SECURITY DEFINER
--      runs the ANALYZE with the OWNER's (veripsa_migrator) authority, so veripsa_app can trigger the refresh
--      without being granted ownership / bypassing per-tenant FORCE RLS.
--   3. FRESH-BACKEND VISIBILITY: the caller runs this on a NEW short-lived connection, because a same-backend
--      ANALYZE immediately after that backend's own INSERT reads a STALE 0-page relation size and records the
--      table as empty (reltuples stays -1) — a new backend sees the true heap.
-- Content-free (touches only Postgres' own statistics, never row contents). The whole-table stats it refreshes
-- are global per table (these are non-partitioned heaps), exactly what the planner uses for the adjacency.
-- ============================================================================================
CREATE OR REPLACE PROCEDURE core.refresh_graph_stats()
    LANGUAGE plpgsql SECURITY DEFINER SET search_path TO 'core','pg_catalog' AS $$
BEGIN
  ANALYZE core.code_node, core.code_edge;
END $$;
ALTER PROCEDURE core.refresh_graph_stats() OWNER TO veripsa_migrator;
REVOKE ALL ON PROCEDURE core.refresh_graph_stats() FROM PUBLIC;
GRANT EXECUTE ON PROCEDURE core.refresh_graph_stats() TO veripsa_app, veripsa_writer;

-- coordinate_file_paths: the content-free file-path UNIVERSE for a coordinate (every retained source/config file's
-- path). The incremental-ingest path reads this so a changed file's imports re-resolve against UNCHANGED
-- files (resolution is path-based, never bodies). Read-only; identity from the connection role.
CREATE OR REPLACE FUNCTION core.coordinate_file_paths(p_repo text, p_branch text)
    RETURNS text[] LANGUAGE plpgsql SECURITY DEFINER SET search_path TO 'core','pg_catalog' AS $$
DECLARE v_account text; v_paths text[];
BEGIN
  SELECT account INTO v_account FROM core.resolve_session_identity() AS r(agent,account);
  PERFORM set_config('core.current_account', v_account, true);   -- pin RLS for the owner read
  SELECT array_agg(DISTINCT path) INTO v_paths FROM core.code_node
   WHERE account_id=v_account AND repo=left(COALESCE(p_repo,''),512) AND branch=left(COALESCE(p_branch,''),512)
     AND node_kind IN ('file','config_file');
  RETURN COALESCE(v_paths, '{}');
END $$;
ALTER FUNCTION core.coordinate_file_paths(text,text) OWNER TO veripsa_migrator;

-- coordinate_resource_catalog: retained first-class resource identity plus the bounded path context needed to
-- make an incremental decision without reconstructing known resources from changed files alone.
--
-- Shape is stable:
--   {resources:[{id,kind,canonical_key,semantic_key,path,scope,extractor,confidence,provenance,name,language,
--                definition_paths,reference_paths}],
--    context_paths:[], definition_paths:[], reference_conditioned_paths:[],
--    pairing_paths:[], bidirectional_import_paths:[]}
--
-- pairing_paths is intentionally LIMITED to query-only pair substrates (sibling_stem/role_feature), where
-- changing either side changes the other side's generated relationship. Table/config/API consumers use the
-- resource catalog + normal context instead. Column edges are available as persisted evidence here while they
-- remain outside effective adjacency.
--
-- definition_paths includes BOTH explicit alters/alters_col sources and each persisted resource node's own
-- path.  Some extractors intentionally retain a resource node without an alters edge (for example an ambiguous
-- OpenAPI declaration), while config_key has no definer edge at all.  Omitting node.path would let removal of
-- the last declaration leave stale edges in a path-local patch.
--
-- reference_conditioned_paths records consumers for substrates whose resource node exists only while a live
-- producer/consumer pair exists.  Changing or removing the last Celery/BullMQ/CI reference can remove a node
-- and an alters edge owned by an unchanged definition file, so these paths must conservatively full-rebuild.
CREATE OR REPLACE FUNCTION core.coordinate_resource_catalog(p_repo text, p_branch text)
    RETURNS jsonb LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path TO 'core','pg_catalog' AS $$
DECLARE v_account text; v_repo text; v_branch text; v_out jsonb;
BEGIN
  SELECT account INTO v_account FROM core.resolve_session_identity() AS r(agent,account);
  PERFORM set_config('core.current_account', v_account, true);
  v_repo := left(COALESCE(p_repo,''),512);
  v_branch := left(COALESCE(p_branch,''),512);

  WITH resource_nodes AS (
    SELECT n.*,
           COALESCE(n.canonical_key,
                    core._resource_canonical_key(n.node_kind,n.node_id,n.name,NULL)) AS resolved_key,
           COALESCE(
             n.semantic_key,
             core._node_semantic_key(
               n.node_kind,n.node_id,n.path,n.name,n.canonical_key
             )
           ) AS resolved_semantic_key
      FROM core.code_node n
     WHERE n.account_id=v_account AND n.repo=v_repo AND n.branch=v_branch
       AND n.node_kind IN (
         'table','column','config_key','iac_resource','k8s_resource','api_type','api_message','api_service',
         'api_operation','api_schema','ci_script','app_command','job_task','job_queue','sibling_stem','role_feature'
       )
  ), edge_paths AS (
    SELECT e.dst,e.edge_kind,
           COALESCE(e.semantic_dst_key,core._semantic_ref_key(e.dst)) AS resolved_dst_key,
           COALESCE((
             SELECT min(sn.path) FROM core.code_node sn
              WHERE sn.account_id=e.account_id AND sn.repo=e.repo AND sn.branch=e.branch
                AND sn.node_id=e.src
                AND sn.node_kind IN ('file','config_file')
           ),e.src) AS src_path
      FROM core.code_edge e
     WHERE e.account_id=v_account AND e.repo=v_repo AND e.branch=v_branch
  ), resources_json AS (
    SELECT COALESCE(jsonb_agg(
      jsonb_build_object(
        'id',rn.node_id,
        'kind',rn.node_kind,
        'canonical_key',rn.resolved_key,
        'semantic_key',rn.resolved_semantic_key,
        'path',rn.path,
        'scope',rn.resource_scope,
        'extractor',rn.extractor,
        'confidence',rn.confidence,
        'provenance',rn.provenance,
        'name',rn.name,
        'language',rn.language,
        'definition_paths',to_jsonb(ARRAY(
          SELECT DISTINCT ep.src_path FROM edge_paths ep
           WHERE ep.resolved_dst_key=rn.resolved_semantic_key
             AND ep.edge_kind IN ('alters','alters_col')
           ORDER BY ep.src_path
        )),
        'reference_paths',to_jsonb(ARRAY(
          SELECT DISTINCT ep.src_path FROM edge_paths ep
           WHERE ep.resolved_dst_key=rn.resolved_semantic_key
             AND ep.edge_kind IN ('queries','queries_col','reads_config')
           ORDER BY ep.src_path
        ))
      ) ORDER BY rn.node_kind,rn.resolved_key,rn.path,rn.node_id
    ),'[]'::jsonb) AS value
      FROM resource_nodes rn
  ), definition_paths AS (
    SELECT DISTINCT path FROM resource_nodes
    UNION
    SELECT DISTINCT src_path AS path FROM edge_paths WHERE edge_kind IN ('alters','alters_col')
  ), reference_conditioned_paths AS (
    SELECT DISTINCT ep.src_path AS path
      FROM edge_paths ep
      JOIN resource_nodes rn
        ON rn.resolved_semantic_key=ep.resolved_dst_key
     WHERE rn.node_kind IN ('ci_script','job_task','job_queue')
       AND ep.edge_kind='queries'
  ), pairing_paths AS (
    SELECT DISTINCT ep.src_path AS path
      FROM edge_paths ep
      JOIN resource_nodes rn
        ON rn.resolved_semantic_key=ep.resolved_dst_key
     WHERE rn.node_kind IN ('sibling_stem','role_feature') AND ep.edge_kind='queries'
  ), import_paths AS (
    SELECT DISTINCT ep.src_path,n.path AS dst_path
      FROM edge_paths ep
      JOIN core.code_node n
        ON n.account_id=v_account AND n.repo=v_repo AND n.branch=v_branch
       AND n.node_kind='file'
       AND COALESCE(
             n.semantic_key,
             core._node_semantic_key(
               n.node_kind,n.node_id,n.path,n.name,n.canonical_key
             )
           )=ep.resolved_dst_key
     WHERE ep.edge_kind='imports'
  ), bidirectional_import_paths AS (
    SELECT DISTINCT a.src_path AS path
      FROM import_paths a JOIN import_paths b
        ON a.src_path=b.dst_path AND a.dst_path=b.src_path
    UNION
    SELECT DISTINCT a.dst_path AS path
      FROM import_paths a JOIN import_paths b
        ON a.src_path=b.dst_path AND a.dst_path=b.src_path
  ), context_paths AS (
    SELECT path FROM resource_nodes
    UNION
    SELECT path FROM core.code_node
     WHERE account_id=v_account AND repo=v_repo AND branch=v_branch AND node_kind='config_file'
    UNION
    SELECT path FROM core.code_node
     WHERE account_id=v_account AND repo=v_repo AND branch=v_branch
       AND node_kind IN ('file','config_file')
       AND (
         lower(path) ~ '(^|/)(package\.json|cargo\.toml|pyproject\.toml|setup\.py|setup\.cfg|go\.mod)$'
         OR lower(path) ~ '(^|/)tsconfig[^/]*\.json$'
       )
    UNION
    SELECT src_path FROM edge_paths
     WHERE edge_kind IN ('queries','alters','reads_config','queries_col','alters_col')
    UNION SELECT path FROM definition_paths
    UNION SELECT path FROM reference_conditioned_paths
    UNION SELECT path FROM pairing_paths
    UNION SELECT path FROM bidirectional_import_paths
  )
  SELECT jsonb_build_object(
    'resources',(SELECT value FROM resources_json),
    'context_paths',to_jsonb(ARRAY(SELECT DISTINCT path FROM context_paths WHERE path IS NOT NULL AND path<>'' ORDER BY path)),
    'definition_paths',to_jsonb(ARRAY(SELECT DISTINCT path FROM definition_paths WHERE path IS NOT NULL AND path<>'' ORDER BY path)),
    'reference_conditioned_paths',to_jsonb(ARRAY(
      SELECT DISTINCT path FROM reference_conditioned_paths WHERE path IS NOT NULL AND path<>'' ORDER BY path
    )),
    'pairing_paths',to_jsonb(ARRAY(SELECT DISTINCT path FROM pairing_paths WHERE path IS NOT NULL AND path<>'' ORDER BY path)),
    'bidirectional_import_paths',to_jsonb(ARRAY(
      SELECT DISTINCT path FROM bidirectional_import_paths WHERE path IS NOT NULL AND path<>'' ORDER BY path
    ))
  ) INTO v_out;
  RETURN v_out;
END $$;
ALTER FUNCTION core.coordinate_resource_catalog(text,text) OWNER TO veripsa_migrator;

-- coordinate_graph_sha: the as-of FRESHNESS of a coordinate's STORED main graph — the commit_sha the graph
-- was last (re)ingested at, plus when. The self-heal + freshness signal read this back: the App compares the
-- stored commit_sha against main's CURRENT HEAD (fetched from GitHub) and re-ingests if they differ, so a
-- MISSED push self-heals on the next PR instead of letting predictions run against a stale graph. Read-only;
-- content-free (a commit sha is public git metadata + a timestamp — never code/paths/bodies). Identity from
-- the connection role (tenant-pinned). Returns a JSON object
-- {commit_sha, graph_revision, ingested_at, node_count, edge_count, graph_hash,
--  extractor_version, current_extractor_version,
--  semantic_ref_version, current_semantic_ref_version,
--  has_graph_uncertainty, observability}
-- or {commit_sha:null,...} when this coordinate has NEVER been ingested (a fresh repo) — the caller treats a
-- null/absent stored sha as "behind everything" → a self-heal ingest (the cold-start). A single scalar (the
-- App's db() runner returns row[0] of one row).
CREATE OR REPLACE FUNCTION core.coordinate_graph_sha(p_repo text, p_branch text)
    RETURNS jsonb LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path TO 'core','pg_catalog' AS $$
DECLARE v_account text; v_repo text; v_branch text; v_out jsonb;
BEGIN
  SELECT account INTO v_account FROM core.resolve_session_identity() AS r(agent,account);
  PERFORM set_config('core.current_account', v_account, true);   -- pin RLS for the owner read
  v_repo := left(COALESCE(p_repo,''),512); v_branch := left(COALESCE(p_branch,''),512);
  -- EXTRACTOR VERSION (G3): return the STORED stamp AND the CURRENT token so graph_freshness can compare them
  -- (stored != current OR stored NULL ⇒ behind). current_extractor_version is ALWAYS present on the NEW schema —
  -- its ABSENCE in the returned object is precisely how the app detects the OLD (pre-predeploy) schema and keeps
  -- the version check INERT (gen-agnostic): a legacy row (extractor_version NULL) is behind, but the whole check
  -- is skipped when current_extractor_version is absent. Content-free (a bounded token).
  SELECT jsonb_build_object(
           'commit_sha', gv.commit_sha, 'ingested_at', gv.ingested_at, 'captured_at', gv.captured_at,
           'node_count', gv.node_count, 'edge_count', gv.edge_count,
           'graph_revision', gv.graph_revision,
           'semantic_ref_version', gv.semantic_ref_version,
           'current_semantic_ref_version', core.current_semantic_ref_version(),
           'has_graph_uncertainty',
             EXISTS (
               SELECT 1
                 FROM core.code_node n
                WHERE n.account_id=gv.account_id AND n.repo=gv.repo AND n.branch=gv.branch
                  AND n.analysis_status IS NOT NULL
             )
             OR EXISTS (
               SELECT 1
                 FROM core.code_edge e
                WHERE e.account_id=gv.account_id AND e.repo=gv.repo AND e.branch=gv.branch
                  AND e.reference_status IS NOT NULL
             ),
           'graph_hash', gv.graph_hash, 'observability', gv.observability,
           'extractor_version', gv.extractor_version,
           'current_extractor_version', core.current_extractor_version())
    INTO v_out
    FROM core.graph_version gv
   WHERE gv.account_id=v_account AND gv.repo=v_repo AND gv.branch=v_branch;
  -- NEVER ingested for this coordinate → an honest "unknown stored sha" object (the caller self-heals/cold-starts).
  RETURN COALESCE(v_out, jsonb_build_object('commit_sha', NULL, 'ingested_at', NULL, 'captured_at', NULL,
                                            'node_count', NULL, 'edge_count', NULL,
                                            'graph_revision', NULL,
                                            'semantic_ref_version', NULL,
                                            'current_semantic_ref_version', core.current_semantic_ref_version(),
                                            'has_graph_uncertainty', NULL,
                                            'graph_hash', NULL, 'observability', NULL,
                                            'extractor_version', NULL,
                                            'current_extractor_version', core.current_extractor_version()));
END $$;
ALTER FUNCTION core.coordinate_graph_sha(text,text) OWNER TO veripsa_migrator;

-- coordinate_inert_imports: the retained INERT `imports` edges of a coordinate — an `imports` edge whose dst
-- is a BARE MODULE NAME that names NO file in the graph (so the adjacency engine joins dst→file_node and finds
-- nothing). These are the edges a future ADD can turn LIVE: if a push ADDS a file whose path the resolver would
-- match to one of these module strings, a full re-ingest re-points that UNCHANGED importer to a real file→file
-- edge. The incremental extractor now rebuilds the bounded retained target-SHA context, but its persistence slice
-- still owns changed paths only; it must never silently omit that unchanged importer's changed edge. The server
-- reads these + re-runs the REAL resolver (code_graph_extract._resolve_imports) against the new universe and takes
-- the full-rebuild path when one becomes live (an added path absent from the persisted universe is independently
-- full-only as well). Read-only; content-free (the src is a file path, the dst a stored module token). Identity
-- comes from the connection role.
-- Returns a JSON array of [src, dst] pairs (a single scalar — the App's db() runner returns row[0] of one row).
CREATE OR REPLACE FUNCTION core.coordinate_inert_imports(p_repo text, p_branch text)
    RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path TO 'core','pg_catalog' AS $$
DECLARE v_account text; v_repo text; v_branch text; v_out jsonb;
BEGIN
  SELECT account INTO v_account FROM core.resolve_session_identity() AS r(agent,account);
  PERFORM set_config('core.current_account', v_account, true);   -- pin RLS for the owner read
  v_repo := left(COALESCE(p_repo,''),512); v_branch := left(COALESCE(p_branch,''),512);
  SELECT COALESCE(jsonb_agg(jsonb_build_array(e.src, e.dst)), '[]'::jsonb) INTO v_out
    FROM core.code_edge e
   WHERE e.account_id=v_account AND e.repo=v_repo AND e.branch=v_branch AND e.edge_kind='imports'
     AND NOT EXISTS (                                            -- dst names no file in the coordinate = INERT
           SELECT 1 FROM core.code_node n
            WHERE n.account_id=v_account AND n.repo=v_repo AND n.branch=v_branch
              AND n.node_kind='file'
              AND COALESCE(
                    n.semantic_key,
                    core._node_semantic_key(
                      n.node_kind,n.node_id,n.path,n.name,n.canonical_key
                    )
                  )=COALESCE(e.semantic_dst_key,core._semantic_ref_key(e.dst)));
  RETURN v_out;
END $$;
ALTER FUNCTION core.coordinate_inert_imports(text,text) OWNER TO veripsa_migrator;

-- patch_graph_with_authority: INCREMENTAL ingest — replace ONLY the touched paths' subgraph, keep the rest.
-- A push that changes a few files re-extracts just those (content-free) and patches them in, instead of a
-- full re-clone + whole-coordinate DELETE+reINSERT (the "コスト爆発" guard for busy long-running repos).
-- Correctness vs a full re-ingest holds because the model is path-keyed: a file node's id IS its path (so an
-- INCOMING `imports` edge to a CHANGED file stays valid — the file node is recreated with the same id), and
-- cross-file `calls`/`queries`/`reads_config` edges carry a BARE NAME resolved at query time (so they adapt
-- to the new node set automatically). We (1) drop the touched paths' nodes, (2) drop their OUTGOING edges +
-- any dangling INCOMING import into a REMOVED file, (3) insert the re-extracted changed-file subgraph (its
-- imports already resolved against the full universe by the caller), (4) recompute totals + bump the version.
-- ADD a trailing p_captured_at (the head-commit time, for the delivery-order monotonicity guard). Adding a param
-- via CREATE OR REPLACE would leave the OLD 6-arg overload behind on a re-apply (dead code + an ambiguous call);
-- DROP it first (mirrors the record_collision signature-change pattern).
DROP FUNCTION IF EXISTS core.patch_graph_with_authority(jsonb,text,text,text[],text[],text);
CREATE OR REPLACE FUNCTION core.patch_graph_with_authority(p_subgraph jsonb, p_repo text, p_branch text, p_changed_paths text[], p_removed_paths text[], p_commit_sha text DEFAULT NULL, p_captured_at timestamptz DEFAULT NULL)
    RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path TO 'core','pg_catalog' AS $$
DECLARE v_agent text; v_account text; v_repo text; v_branch text; v_sha text; v_expected_base_sha text;
        v_expected_base_revision bigint; v_graph_revision bigint;
        v_semantic_ref_version smallint;
        v_touched text[]; v_removed_semantic_keys text[];
        v_ndel int; v_edel int; v_nins int; v_eins int; v_nodes int; v_edges int; v_quota jsonb;
        v_n_in bigint; v_e_in bigint; v_input_files bigint; v_max_elems CONSTANT bigint := 5000000;
        v_unknown_node_kinds text[]; v_unknown_edge_kinds text[];
        v_metric_counts_invalid boolean;
        v_metric_document_count bigint;
        v_producer_version text;
        v_hash text; v_extractor_obs jsonb; v_observability jsonb; v_persistence_obs jsonb;
        v_node_exclusion_reasons jsonb; v_edge_exclusion_reasons jsonb;
        v_node_kind_counts jsonb; v_edge_kind_counts jsonb;
        v_allowed_node_kinds CONSTANT text[] := ARRAY[
          'file','def','class','table','column','config_file','config_key',
          'iac_resource','k8s_resource','api_type','api_message','api_service','api_operation','api_schema',
          'ci_script','app_command','job_task','job_queue','sibling_stem','role_feature'
        ];
        v_allowed_edge_kinds CONSTANT text[] := ARRAY[
          'contains','calls','imports','queries','alters','reads_config','alters_col','queries_col'
        ];
        v_resource_kinds CONSTANT text[] := ARRAY[
          'table','column','config_key','iac_resource','k8s_resource','api_type','api_message','api_service',
          'api_operation','api_schema','ci_script','app_command','job_task','job_queue','sibling_stem','role_feature'
        ];
BEGIN
  IF p_subgraph IS NULL OR jsonb_typeof(p_subgraph)<>'object' THEN RAISE EXCEPTION 'subgraph must be an object {nodes,edges}' USING ERRCODE='22023'; END IF;
  IF p_subgraph ? 'nodes' AND jsonb_typeof(p_subgraph->'nodes')<>'array' THEN
    RAISE EXCEPTION 'subgraph.nodes must be an array' USING ERRCODE='22023';
  END IF;
  IF p_subgraph ? 'edges' AND jsonb_typeof(p_subgraph->'edges')<>'array' THEN
    RAISE EXCEPTION 'subgraph.edges must be an array' USING ERRCODE='22023';
  END IF;
  v_sha := NULLIF(btrim(COALESCE(p_commit_sha,'')),'');
  IF v_sha IS NOT NULL AND (length(v_sha)>64 OR v_sha !~ '^[0-9a-fA-F]+$') THEN RAISE EXCEPTION 'commit_sha out of bounds' USING ERRCODE='23514'; END IF;
  v_repo := left(COALESCE(p_repo,''),512); v_branch := left(COALESCE(p_branch,''),512);
  -- Patch semantics are current-only. Historical/version-absent FULL writers remain rollout-compatible and are
  -- stamped behind, but a partial historical write cannot prove retained files were extracted under today's
  -- rules. Every non-current patch is rejected before DELETE so the App takes the coherent full path.
  -- Unknown/future tokens also fail before any mutation.
  IF p_subgraph ? 'extractor_version'
     AND jsonb_typeof(p_subgraph->'extractor_version') NOT IN ('string','null') THEN
    RAISE EXCEPTION 'subgraph.extractor_version must be a string token' USING ERRCODE='22023';
  END IF;
  v_producer_version := CASE
    WHEN jsonb_typeof(p_subgraph->'extractor_version')='string' THEN p_subgraph->>'extractor_version'
    ELSE NULL
  END;
  IF v_producer_version IS NOT NULL
     AND v_producer_version NOT IN ('cg1','cg2','cg3',core.current_extractor_version()) THEN
    RAISE EXCEPTION 'unsupported graph extractor_version: %',left(v_producer_version,64)
      USING ERRCODE='22023';
  END IF;
  IF v_producer_version IS NOT NULL
     AND (length(v_producer_version)>32 OR v_producer_version !~ '^[A-Za-z0-9_.:-]+$') THEN
    RAISE EXCEPTION 'subgraph.extractor_version token out of bounds' USING ERRCODE='22023';
  END IF;
  -- PATCH is current-only. Reject a cg3/legacy producer before validating
  -- baseline tokens or touching rows; retained files cannot be upgraded from
  -- v1 extraction semantics by a path-local write.
  IF v_producer_version IS DISTINCT FROM core.current_extractor_version() THEN
    RAISE EXCEPTION 'patch graph extractor_version does not match stored coordinate'
      USING ERRCODE='22023';
  END IF;
  -- The same closed metric contract as full ingest, evaluated before any
  -- touched-path DELETE. Current cg4 PATCH payloads must explicitly prove the
  -- uncertainty-aware v2 contract.
  v_extractor_obs := core._validated_graph_observability(
    p_subgraph->'observability',p_subgraph->'metrics','patch');
  IF v_extractor_obs->>'schema_contract_version' IS DISTINCT FROM '2' THEN
    RAISE EXCEPTION
      'graph schema_contract_version does not match extractor_version'
      USING ERRCODE='22023';
  END IF;
  IF v_extractor_obs ? 'ambiguity_detection_scope'
     AND v_extractor_obs->>'ambiguity_detection_scope'<>
       'retained multi-definer resources, emitted canonical-key collisions, and local import candidate ambiguity' THEN
    RAISE EXCEPTION
      'graph ambiguity_detection_scope does not match extractor_version'
      USING ERRCODE='22023';
  END IF;
  -- A patch is derived from retained nodes, resource catalogs and path universes read from one stored
  -- coordinate.  Carry that coordinate's commit identity AND monotonic revision into the write so a concurrent
  -- full ingest cannot turn a path-local patch into a hybrid graph.  SHA alone is insufficient: P→B→P is an
  -- ABA sequence which returns to the same SHA after changing the graph twice.  These tokens live inside
  -- p_subgraph to preserve the public seven-argument function signature across rollouts.
  IF NOT (p_subgraph ? 'expected_base_sha')
     OR jsonb_typeof(p_subgraph->'expected_base_sha')<>'string' THEN
    RAISE EXCEPTION 'patch graph requires expected_base_sha'
      USING ERRCODE='22023';
  END IF;
  v_expected_base_sha := p_subgraph->>'expected_base_sha';
  IF length(v_expected_base_sha)<1 OR length(v_expected_base_sha)>64
     OR v_expected_base_sha !~ '^[0-9a-fA-F]+$' THEN
    RAISE EXCEPTION 'patch graph expected_base_sha out of bounds'
      USING ERRCODE='22023';
  END IF;
  IF NOT (p_subgraph ? 'expected_base_revision')
     OR jsonb_typeof(p_subgraph->'expected_base_revision')<>'number'
     OR (p_subgraph->>'expected_base_revision') !~ '^[1-9][0-9]{0,17}$' THEN
    RAISE EXCEPTION 'patch graph requires bounded integer expected_base_revision'
      USING ERRCODE='22023';
  END IF;
  v_expected_base_revision := (p_subgraph->>'expected_base_revision')::bigint;
  -- PER-PAYLOAD INBOUND CAP (audit:scale): same authoritative bound as ingest_graph — cap the inbound subgraph
  -- array LENGTHS (cheap O(1)) so ONE patch call can't ship an unbounded {nodes,edges} and overshoot the
  -- footprint in a single write. Above the cap REFUSE the whole call (patch nothing) + return the bounded sentinel.
  v_n_in := CASE WHEN jsonb_typeof(p_subgraph->'nodes')='array' THEN jsonb_array_length(p_subgraph->'nodes') ELSE 0 END;
  v_e_in := CASE WHEN jsonb_typeof(p_subgraph->'edges')='array' THEN jsonb_array_length(p_subgraph->'edges') ELSE 0 END;
  IF v_n_in > v_max_elems OR v_e_in > v_max_elems THEN
    RETURN core._graph_too_large_result(v_max_elems, GREATEST(v_n_in, v_e_in));
  END IF;
  -- Same semantic-set wall as the full writer, and deliberately before
  -- establish_session_write_context or every touched-path DELETE.
  PERFORM core._assert_unique_graph_identities(
    COALESCE(p_subgraph->'nodes','[]'::jsonb),
    COALESCE(p_subgraph->'edges','[]'::jsonb)
  );
  SELECT array_agg(k ORDER BY k) INTO v_unknown_node_kinds
    FROM (
      SELECT DISTINCT left(COALESCE(core._safe_ref_token(n->>'kind'),'<null>'),64) AS k
        FROM jsonb_array_elements(COALESCE(p_subgraph->'nodes','[]'::jsonb)) n
       WHERE n->>'kind' IS NULL OR NOT (n->>'kind'=ANY(v_allowed_node_kinds))
    ) q;
  IF COALESCE(array_length(v_unknown_node_kinds,1),0)>0 THEN
    RAISE EXCEPTION 'unknown graph node kind(s): %',array_to_string(v_unknown_node_kinds,',')
      USING ERRCODE='22023';
  END IF;
  SELECT array_agg(k ORDER BY k) INTO v_unknown_edge_kinds
    FROM (
      SELECT DISTINCT left(COALESCE(core._safe_ref_token(e->>'kind'),'<null>'),64) AS k
        FROM jsonb_array_elements(COALESCE(p_subgraph->'edges','[]'::jsonb)) e
       WHERE e->>'kind' IS NULL OR NOT (e->>'kind'=ANY(v_allowed_edge_kinds))
    ) q;
  IF COALESCE(array_length(v_unknown_edge_kinds,1),0)>0 THEN
    RAISE EXCEPTION 'unknown graph edge kind(s): %',array_to_string(v_unknown_edge_kinds,',')
      USING ERRCODE='22023';
  END IF;
  IF EXISTS (
    SELECT 1 FROM jsonb_array_elements(COALESCE(p_subgraph->'nodes','[]'::jsonb)) n
     WHERE n ? 'analysis_status'
       AND NOT (
         jsonb_typeof(n->'analysis_status')='null'
         OR (
           jsonb_typeof(n->'analysis_status')='string'
           AND n->>'analysis_status' IN ('failed','ambiguous','incomplete')
           AND n->>'kind' IN ('file','config_file')
         )
       )
  ) THEN
    RAISE EXCEPTION 'invalid node analysis_status (expected null|failed|ambiguous|incomplete on file/config_file)'
      USING ERRCODE='22023';
  END IF;
  IF EXISTS (
    SELECT 1 FROM jsonb_array_elements(COALESCE(p_subgraph->'edges','[]'::jsonb)) e
     WHERE e ? 'reference_status'
       AND NOT (
         jsonb_typeof(e->'reference_status')='null'
         OR (
           jsonb_typeof(e->'reference_status')='string'
           AND e->>'reference_status' IN ('ambiguous','unresolved')
         )
       )
  ) THEN
    RAISE EXCEPTION 'invalid edge reference_status (expected null|ambiguous|unresolved)'
      USING ERRCODE='22023';
  END IF;
  IF v_extractor_obs ? 'node_kind_counts' THEN
    SELECT
      (SELECT count(*) FROM jsonb_object_keys(v_extractor_obs->'node_kind_counts'))
        <> cardinality(v_allowed_node_kinds)
      OR EXISTS (
        SELECT 1 FROM jsonb_each_text(v_extractor_obs->'node_kind_counts') AS metric(kind,count_text)
         WHERE count_text::bigint <> (
           SELECT count(*) FROM jsonb_array_elements(COALESCE(p_subgraph->'nodes','[]'::jsonb)) n
            WHERE n->>'kind'=metric.kind
         )
      )
      INTO v_metric_counts_invalid;
    IF v_metric_counts_invalid THEN
      RAISE EXCEPTION 'graph observability node_kind_counts do not match subgraph.nodes'
        USING ERRCODE='22023';
    END IF;
  END IF;
  IF v_extractor_obs ? 'edge_kind_counts' THEN
    SELECT
      (SELECT count(*) FROM jsonb_object_keys(v_extractor_obs->'edge_kind_counts'))
        <> cardinality(v_allowed_edge_kinds)
      OR EXISTS (
        SELECT 1 FROM jsonb_each_text(v_extractor_obs->'edge_kind_counts') AS metric(kind,count_text)
         WHERE count_text::bigint <> (
           SELECT count(*) FROM jsonb_array_elements(COALESCE(p_subgraph->'edges','[]'::jsonb)) e
            WHERE e->>'kind'=metric.kind
         )
      )
      INTO v_metric_counts_invalid;
    IF v_metric_counts_invalid THEN
      RAISE EXCEPTION 'graph observability edge_kind_counts do not match subgraph.edges'
        USING ERRCODE='22023';
    END IF;
  END IF;
  IF v_extractor_obs ? 'nodes_by_substrate' THEN
    SELECT COALESCE(sum((value::text)::bigint),0)<>v_n_in
      INTO v_metric_counts_invalid
      FROM jsonb_each(v_extractor_obs->'nodes_by_substrate');
    IF v_metric_counts_invalid THEN
      RAISE EXCEPTION 'graph observability nodes_by_substrate total does not match subgraph.nodes'
        USING ERRCODE='22023';
    END IF;
  END IF;
  IF v_extractor_obs ? 'edges_by_substrate' THEN
    SELECT COALESCE(sum((value::text)::bigint),0)<>v_e_in
      INTO v_metric_counts_invalid
      FROM jsonb_each(v_extractor_obs->'edges_by_substrate');
    IF v_metric_counts_invalid THEN
      RAISE EXCEPTION 'graph observability edges_by_substrate total does not match subgraph.edges'
        USING ERRCODE='22023';
    END IF;
  END IF;
  IF v_extractor_obs ? 'input_file_count' THEN
    SELECT count(DISTINCT n->>'path') INTO v_metric_document_count
      FROM jsonb_array_elements(COALESCE(p_subgraph->'nodes','[]'::jsonb)) n
     WHERE n->>'kind' IN ('file','config_file') AND n->>'path' IS NOT NULL;
    IF (v_extractor_obs->>'input_file_count')::bigint<>v_metric_document_count THEN
      RAISE EXCEPTION 'graph observability input_file_count does not match patch document paths'
        USING ERRCODE='22023';
    END IF;
  END IF;
  IF (v_extractor_obs ? 'unresolved_reference_count'
      AND (v_extractor_obs->>'unresolved_reference_count')::bigint>v_n_in+(2*v_e_in))
     OR (v_extractor_obs ? 'ambiguous_reference_count'
      AND (v_extractor_obs->>'ambiguous_reference_count')::bigint>v_n_in+(2*v_e_in)) THEN
    RAISE EXCEPTION 'graph observability reference count exceeds the payload evidence bound'
      USING ERRCODE='22023';
  END IF;
  IF EXISTS (
    SELECT 1 FROM jsonb_array_elements(COALESCE(p_subgraph->'nodes','[]'::jsonb)) n
     WHERE n->>'kind'=ANY(v_resource_kinds)
       AND (
         (n ? 'canonical_key' AND jsonb_typeof(n->'canonical_key') NOT IN ('string','null'))
         OR (jsonb_typeof(n->'canonical_key')='string'
             AND (length(n->>'canonical_key')=0
                  OR length(n->>'canonical_key')>1600))
         OR (n ? 'resource_scope' AND jsonb_typeof(n->'resource_scope') NOT IN ('string','null'))
         OR (jsonb_typeof(n->'resource_scope')='string'
             AND (core._safe_ref_token(n->>'resource_scope') IS NULL
                  OR length(core._safe_ref_token(n->>'resource_scope'))>1024))
         OR (n ? 'scope' AND jsonb_typeof(n->'scope') NOT IN ('string','null'))
         OR (jsonb_typeof(n->'scope')='string'
             AND (core._safe_ref_token(n->>'scope') IS NULL
                  OR length(core._safe_ref_token(n->>'scope'))>1024))
         OR (n ? 'extractor' AND jsonb_typeof(n->'extractor') NOT IN ('string','null'))
         OR (jsonb_typeof(n->'extractor')='string'
             AND (core._safe_ref_token(n->>'extractor') IS NULL
                  OR length(core._safe_ref_token(n->>'extractor'))>128))
         OR (n ? 'confidence' AND jsonb_typeof(n->'confidence') NOT IN ('number','null'))
         OR (jsonb_typeof(n->'confidence')='number'
             AND ((n->>'confidence')::numeric<0 OR (n->>'confidence')::numeric>1))
         OR (n ? 'provenance' AND jsonb_typeof(n->'provenance') NOT IN ('object','null'))
         OR (jsonb_typeof(n->'provenance')='object' AND octet_length((n->'provenance')::text)>8192)
       )
  ) THEN
    RAISE EXCEPTION 'invalid resource metadata (expected bounded canonical_key/scope/extractor, confidence 0..1, object provenance)'
      USING ERRCODE='22023';
  END IF;
  IF v_producer_version IN ('cg3',core.current_extractor_version()) AND EXISTS (
    SELECT 1 FROM jsonb_array_elements(COALESCE(p_subgraph->'nodes','[]'::jsonb)) n
     WHERE n->>'kind'=ANY(v_resource_kinds)
       AND (
         jsonb_typeof(n->'canonical_key') IS DISTINCT FROM 'string'
         OR length(n->>'canonical_key')=0
         OR jsonb_typeof(n->'extractor') IS DISTINCT FROM 'string'
         OR core._safe_ref_token(n->>'extractor') IS NULL
         OR jsonb_typeof(n->'confidence') IS DISTINCT FROM 'number'
         OR jsonb_typeof(n->'provenance') IS DISTINCT FROM 'object'
       )
  ) THEN
    RAISE EXCEPTION 'cg3+ resource nodes require canonical_key, extractor, confidence and provenance'
      USING ERRCODE='22023';
  END IF;
  SELECT count(DISTINCT n->>'path') INTO v_input_files
    FROM jsonb_array_elements(COALESCE(p_subgraph->'nodes','[]'::jsonb)) n
   WHERE n->>'kind' IN ('file','config_file') AND n->>'path' IS NOT NULL;
  -- Never alias an overlong caller path onto a different stored path.  The
  -- historical left(...,1024) silently changed the patch target and could
  -- delete a valid node whose path happened to equal that prefix.
  IF EXISTS (
    SELECT 1
      FROM unnest(
        COALESCE(p_changed_paths,'{}'::text[])
        || COALESCE(p_removed_paths,'{}'::text[])
      ) AS p
     WHERE p IS NOT NULL AND btrim(p)<>'' AND length(p)>1024
  ) THEN
    RAISE EXCEPTION 'patch graph touched path exceeds 1024 characters'
      USING ERRCODE='22023';
  END IF;
  v_touched := ARRAY(
    SELECT DISTINCT p
      FROM unnest(
        COALESCE(p_changed_paths,'{}'::text[])
        || COALESCE(p_removed_paths,'{}'::text[])
      ) AS p
     WHERE p IS NOT NULL AND btrim(p)<>''
  );
  v_removed_semantic_keys := ARRAY(
    SELECT DISTINCT core._semantic_ref_key(p)
      FROM unnest(COALESCE(p_removed_paths,'{}'::text[])) AS p
     WHERE p IS NOT NULL AND btrim(p)<>''
  );
  SELECT agent, account INTO v_agent, v_account FROM core.establish_session_write_context() AS c(agent,account);
  IF array_length(v_touched,1) IS NULL THEN
    SELECT graph_hash,observability,semantic_ref_version
      INTO v_hash,v_observability,v_semantic_ref_version
      FROM core.graph_version
     WHERE account_id=v_account AND repo=v_repo AND branch=v_branch;
    RETURN jsonb_build_object('ok',true,'noop',true,'repo',v_repo,'branch',v_branch,
                              'semantic_ref_version',v_semantic_ref_version,
                              'graph_hash',v_hash,'observability',v_observability);
  END IF;
  -- THE WALL: over the free line → REFUSE (patch nothing) + return the structured signal. Advisory (never raises).
  v_quota := core._refuse_if_over_quota(v_account);
  IF v_quota IS NOT NULL THEN RETURN v_quota; END IF;
  -- Same stable-id → repo → account(shared) → coordinate order as full ingest and lifecycle. The account-live
  -- guard both takes the shared account fence and prevents a writer which waited behind uninstall from
  -- repopulating the just-purged graph.
  PERFORM pg_advisory_xact_lock(
    hashtext(CASE WHEN v_account LIKE 'ACCT-GH-%' THEN substr(v_account,9) ELSE v_account END),
    hashtext(v_repo)
  );
  PERFORM core.assert_account_live_with_authority();
  PERFORM pg_advisory_xact_lock(
    hashtext('core.graph_coordinate'),
    hashtext(v_account||chr(31)||v_repo||chr(31)||v_branch)
  );
  -- Compare-and-swap the exact baseline after serializing coordinate writers and before every DELETE.  Matching
  -- both tokens closes the SHA ABA race.  A mismatch is not a partial success: the App rolls back its
  -- incremental savepoint and performs a coherent full build.
  PERFORM 1 FROM core.graph_version
    WHERE account_id=v_account AND repo=v_repo AND branch=v_branch
      AND commit_sha=v_expected_base_sha
      AND graph_revision=v_expected_base_revision
      AND semantic_ref_version=1;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'patch graph baseline coordinate does not match stored SHA/revision/semantic version'
      USING ERRCODE='40001';
  END IF;
  -- The replacement receives a never-reused token rather than expected+1: graph_version may be deleted and
  -- recreated by lifecycle operations, and a row-local counter would then permit a stale patch to ABA-match.
  v_graph_revision := nextval('core.graph_revision_seq');
  -- DELIVERY-ORDER MONOTONICITY (same guard as ingest_graph): refuse to PATCH the stored graph BACKWARDS to an
  -- OLDER commit than the stored one (a reordered/retried delivery). The patch's changed-file list describes the
  -- OLD tree, so applying it would corrupt the newer graph — refuse outright (no writes), return 'stale'.
  IF p_captured_at IS NOT NULL THEN
    PERFORM 1 FROM core.graph_version
      WHERE account_id=v_account AND repo=v_repo AND branch=v_branch
        AND captured_at IS NOT NULL AND captured_at > p_captured_at
        AND commit_sha IS DISTINCT FROM v_sha;
    IF FOUND THEN
      RETURN jsonb_build_object('ok',true,'stale',true,'skipped','older commit than stored graph (reordered delivery)',
                                'repo',v_repo,'branch',v_branch);
    END IF;
  END IF;
  -- A path-local patch cannot change the extraction semantics of retained files. Require one non-NULL producer
  -- version shared by BOTH the stored whole coordinate and this payload. In particular, a cg4 patch may not land
  -- atop a cg3 baseline: reject before the touched-path DELETE and let the App rebuild the whole coordinate.
  IF v_producer_version IS DISTINCT FROM core.current_extractor_version() THEN
    RAISE EXCEPTION 'patch graph extractor_version does not match stored coordinate'
      USING ERRCODE='22023';
  END IF;
  PERFORM 1 FROM core.graph_version
    WHERE account_id=v_account AND repo=v_repo AND branch=v_branch
      AND extractor_version=v_producer_version;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'patch graph extractor_version does not match stored coordinate'
      USING ERRCODE='22023';
  END IF;

  SELECT COALESCE(jsonb_object_agg(reason,cnt ORDER BY reason),'{}'::jsonb)
    INTO v_node_exclusion_reasons
    FROM (
      SELECT reason,count(*) AS cnt FROM (
        SELECT CASE
          WHEN n->>'id' IS NULL THEN 'missing_id'
          WHEN n->>'path' IS NULL THEN 'missing_path'
          WHEN length(n->>'path')>1024 THEN 'path_too_long'
          WHEN length(n->>'id')>1600 THEN 'id_too_long'
          WHEN length(n->>'name')>512 THEN 'name_too_long'
          WHEN NOT (n->>'path'=ANY(v_touched)) THEN 'outside_touched_paths'
          ELSE NULL END AS reason
        FROM jsonb_array_elements(COALESCE(p_subgraph->'nodes','[]'::jsonb)) n
      ) classified WHERE reason IS NOT NULL GROUP BY reason
    ) counts;
  SELECT COALESCE(jsonb_object_agg(reason,cnt ORDER BY reason),'{}'::jsonb)
    INTO v_edge_exclusion_reasons
    FROM (
      SELECT reason,count(*) AS cnt FROM (
        SELECT CASE
          WHEN e->>'src' IS NULL THEN 'missing_src'
          WHEN e->>'dst' IS NULL THEN 'missing_dst'
          WHEN length(e->>'dst')=0 THEN 'empty_dst'
          WHEN length(e->>'src')>1024 THEN 'src_too_long'
          WHEN length(e->>'dst')>1600 THEN 'dst_too_long'
          -- Every extractor Edge source is a document path. Git paths may
          -- contain ``::``; compare the exact value and never delimiter-guess
          -- it against a generated Node id.
          WHEN NOT (e->>'src'=ANY(v_touched)) THEN 'outside_touched_paths'
          ELSE NULL END AS reason
        FROM jsonb_array_elements(COALESCE(p_subgraph->'edges','[]'::jsonb)) e
      ) classified WHERE reason IS NOT NULL GROUP BY reason
    ) counts;

  -- (1) drop touched paths' OUTGOING edges. Edge.src is the exact document
  -- path, including any legal ``::`` substring; never split it as a generated
  -- Node id. Also remove dangling INCOMING imports to explicitly removed
  -- files.
  PERFORM core.mark_governed_write('code_edge');
  DELETE FROM core.code_edge edge
   WHERE edge.account_id=v_account AND edge.repo=v_repo AND edge.branch=v_branch
     AND (
       edge.src = ANY(v_touched)
       OR (
         COALESCE(array_length(p_removed_paths,1),0) > 0
         AND COALESCE(
               edge.semantic_dst_key,
               core._semantic_ref_key(edge.dst)
             ) = ANY(v_removed_semantic_keys)
       )
     );
  GET DIAGNOSTICS v_edel = ROW_COUNT;

  -- (2) drop the touched paths' nodes (file + its defs/tables/keys — all keyed by path).
  PERFORM core.mark_governed_write('code_node');
  DELETE FROM core.code_node
   WHERE account_id=v_account AND repo=v_repo AND branch=v_branch AND path = ANY(v_touched);
  GET DIAGNOSTICS v_ndel = ROW_COUNT;

  -- (3) insert the re-extracted changed-file subgraph — STRICTLY scoped to the touched paths (defensive: a
  --     stray non-touched path in the subgraph is ignored, so patch can never duplicate a retained node)
  INSERT INTO core.code_node(account_id, repo, branch, node_id, node_kind, path, name, language, start_line, end_line, content_hash,
                             required_arity, optional_arity, has_varargs, has_kwargs, param_names, kwonly_names, shape_fingerprint,
                             canonical_key, resource_scope, extractor, confidence, provenance, semantic_key, analysis_status)
  SELECT v_account, v_repo, v_branch, n->>'id', n->>'kind', n->>'path', core._safe_ref_token(n->>'name'), left(n->>'language',32),
         (core._clean_span(n->'start_line', n->'end_line')).start_line,   -- content-free span (see ingest)
         (core._clean_span(n->'start_line', n->'end_line')).end_line,
         -- FRESHNESS KEY (content-free): MIRROR the full ingest — a re-patched FILE node MUST carry its content
         -- hash, else freshness_ok can never fire for any file touched by a normal (incremental) push and the
         -- symbol-level demotion is unreachable in steady state. `_clean_hash` lowercases + validates (bad → NULL).
         core._clean_hash(n->>'content_hash'),
         -- COMPATIBILITY SIGNATURE SHAPE (content-free): MIRROR the full ingest (the audit-r2 lesson above,
         -- applied on day one — a shape persisted only by the full re-ingest would be silently WIPED for every
         -- file touched by a normal incremental push, in steady state). Same per-field degrade-to-NULL sanitizers.
         core._clean_arity(n->'required_arity'), core._clean_arity(n->'optional_arity'),
         core._clean_flag(n->'has_varargs'), core._clean_flag(n->'has_kwargs'),
         core._clean_name_array(n->'param_names'), core._clean_name_array(n->'kwonly_names'),
         core._clean_hash(n->>'shape_fingerprint'),
         core._resource_canonical_key(n->>'kind',n->>'id',n->>'name',n->>'canonical_key'),
         CASE WHEN n->>'kind'=ANY(v_resource_kinds)
              THEN core._safe_ref_token(COALESCE(n->>'resource_scope',n->>'scope')) ELSE NULL END,
         CASE WHEN n->>'kind'=ANY(v_resource_kinds)
              THEN COALESCE(core._safe_ref_token(n->>'extractor'),core._safe_ref_token(left(n->>'language',128)))
              ELSE NULL END,
         CASE WHEN n->>'kind'=ANY(v_resource_kinds) AND jsonb_typeof(n->'confidence')='number'
              THEN (n->>'confidence')::double precision ELSE NULL END,
         CASE WHEN n->>'kind'=ANY(v_resource_kinds) AND jsonb_typeof(n->'provenance')='object'
              THEN n->'provenance' ELSE NULL END,
         core._node_semantic_key(
           n->>'kind',n->>'id',n->>'path',n->>'name',n->>'canonical_key'
         ),
         n->>'analysis_status'
  FROM jsonb_array_elements(COALESCE(p_subgraph->'nodes','[]'::jsonb)) n
  WHERE n->>'id' IS NOT NULL AND n->>'kind'=ANY(v_allowed_node_kinds)
    AND n->>'path' IS NOT NULL AND n->>'path'=ANY(v_touched)
    AND length(n->>'path')<=1024 AND length(n->>'id')<=1600
    AND (n->>'name' IS NULL OR length(n->>'name')<=512);
  GET DIAGNOSTICS v_nins = ROW_COUNT;
  PERFORM core.mark_governed_write('code_edge');
  INSERT INTO core.code_edge(account_id, repo, branch, src, dst, edge_kind, semantic_dst_key, reference_status)
  SELECT v_account, v_repo, v_branch, e->>'src',
         COALESCE(core._safe_ref_token(e->>'dst'),'ref#'||left(core._semantic_ref_key(e->>'dst'),12)),
         e->>'kind',
         core._semantic_ref_key(e->>'dst'),
         e->>'reference_status'
  FROM jsonb_array_elements(COALESCE(p_subgraph->'edges','[]'::jsonb)) e
  WHERE e->>'src' IS NOT NULL AND e->>'dst' IS NOT NULL AND length(e->>'dst')>0
    AND e->>'kind'=ANY(v_allowed_edge_kinds)
    AND e->>'src'=ANY(v_touched)
    AND length(e->>'src')<=1024 AND length(e->>'dst')<=1600;
  GET DIAGNOSTICS v_eins = ROW_COUNT;

  -- (4) recompute coordinate totals + bump the version (graph_version exists — a patch follows an ingest)
  SELECT count(*) INTO v_nodes FROM core.code_node WHERE account_id=v_account AND repo=v_repo AND branch=v_branch;
  SELECT count(*) INTO v_edges FROM core.code_edge WHERE account_id=v_account AND repo=v_repo AND branch=v_branch;
  SELECT COALESCE(jsonb_object_agg(node_kind,cnt ORDER BY node_kind),'{}'::jsonb)
    INTO v_node_kind_counts
    FROM (SELECT node_kind,count(*) AS cnt FROM core.code_node
           WHERE account_id=v_account AND repo=v_repo AND branch=v_branch GROUP BY node_kind) q;
  SELECT COALESCE(jsonb_object_agg(edge_kind,cnt ORDER BY edge_kind),'{}'::jsonb)
    INTO v_edge_kind_counts
    FROM (SELECT edge_kind,count(*) AS cnt FROM core.code_edge
           WHERE account_id=v_account AND repo=v_repo AND branch=v_branch GROUP BY edge_kind) q;
  v_hash := core._coordinate_graph_hash(v_account,v_repo,v_branch);
  v_persistence_obs := jsonb_build_object(
    'mode','patch',
    'graph_hash_contract','cg4-semantic-v2',
    'base_graph_revision',v_expected_base_revision,
    'graph_revision',v_graph_revision,
    'semantic_ref_version',1,
    'input_files',v_input_files,
    'paths_changed',COALESCE(array_length(p_changed_paths,1),0),
    'paths_removed',COALESCE(array_length(p_removed_paths,1),0),
    'nodes_input',v_n_in,
    'edges_input',v_e_in,
    'nodes_deleted',v_ndel,
    'edges_deleted',v_edel,
    'nodes_inserted',v_nins,
    'edges_inserted',v_eins,
    'producer_extractor_version',v_producer_version,
    'nodes_total',v_nodes,
    'edges_total',v_edges,
    'node_counts_by_kind',v_node_kind_counts,
    'edge_counts_by_kind',v_edge_kind_counts,
    'exclusions',jsonb_build_object(
      'nodes',jsonb_build_object('count',v_n_in-v_nins,'reasons',v_node_exclusion_reasons),
      'edges',jsonb_build_object('count',v_e_in-v_eins,'reasons',v_edge_exclusion_reasons)
    ),
    'evidence_only_edge_kinds',jsonb_build_array('alters_col','queries_col')
  );
  v_observability := v_extractor_obs || jsonb_build_object(
    'persistence',v_persistence_obs,
    'persisted_graph_hash',v_hash
  );
  PERFORM core.mark_governed_write('graph_version');
  -- Same identity boundary as the full writer: a patch proves graph bytes at a coordinate, not which stable
  -- GitHub object owns that mutable full_name.  Only an authenticated App event may re-stamp after this write.
  -- EXTRACTOR VERSION STAMP (G3) — the pre-delete equality guard permits only a patch whose producer exactly
  -- matches the stored whole-coordinate producer. A mixed historical/current patch is rejected so the App takes
  -- the full path. A patch cannot upgrade a stale/NULL coordinate: unchanged files retain their old semantics,
  -- so only a later FULL ingest may stamp the current producer for the whole coordinate.
  -- Same clock-preservation rule as the full writer above: a payload-less patch must never erase the
  -- delivery-order clock that arms the reordered-delivery guard.
  INSERT INTO core.graph_version(account_id, repo, branch, commit_sha, captured_at, ingested_at, node_count, edge_count,
                                 repo_id, extractor_version, graph_hash, observability, graph_revision,
                                 semantic_ref_version)
  VALUES (v_account, v_repo, v_branch, v_sha, p_captured_at, now(), v_nodes, v_edges,
          NULL, NULL, v_hash, v_observability, v_graph_revision, 1)
  ON CONFLICT (account_id, repo, branch) DO UPDATE SET commit_sha=EXCLUDED.commit_sha,
    captured_at=COALESCE(EXCLUDED.captured_at, core.graph_version.captured_at),
    ingested_at=EXCLUDED.ingested_at, node_count=EXCLUDED.node_count, edge_count=EXCLUDED.edge_count, repo_id=NULL,
    extractor_version=CASE
      WHEN core.graph_version.extractor_version=v_producer_version
        THEN core.graph_version.extractor_version
      ELSE NULL
    END,
    graph_hash=EXCLUDED.graph_hash, observability=EXCLUDED.observability,
    graph_revision=EXCLUDED.graph_revision,
    semantic_ref_version=EXCLUDED.semantic_ref_version;

  -- (5) COLD-STATS SIGNAL on HEAVY churn — mirror the full ingest's flag (line ~851), gated. A full ingest
  -- always DELETE+reINSERTs the whole coordinate, so it unconditionally sets core.graph_bulk_loaded so the App
  -- runs a post-commit ANALYZE (the #169 cold-planner-hang class). A patch normally touches a few rows (stats
  -- stay representative → ANALYZE pointless), BUT a repo on the incremental path on EVERY push accumulates row
  -- churn with stale stats and drifts into the same mis-plan. So flag it only when THIS patch's touched churn
  -- (the same counters returned below) crosses a modest threshold — small patches stay quiet, big ones trigger
  -- the identical refresh the full path does. is_local=false: survives COMMIT, reverts on rollback (as in ingest).
  IF (v_ndel + v_nins + v_edel + v_eins) >= 200 THEN
    PERFORM set_config('core.graph_bulk_loaded', '1', false);
  END IF;

  RETURN jsonb_build_object('ok',true,'mode','patch','repo',v_repo,'branch',v_branch,'commit_sha',v_sha,
    'graph_revision',v_graph_revision,
    'semantic_ref_version',1,
    'paths_changed', COALESCE(array_length(p_changed_paths,1),0),
    'paths_removed', COALESCE(array_length(p_removed_paths,1),0),
    'nodes_deleted',v_ndel,'edges_deleted',v_edel,'nodes_inserted',v_nins,'edges_inserted',v_eins,
    'nodes_total',v_nodes,'edges_total',v_edges,'graph_hash',v_hash,'observability',v_observability);
END $$;
ALTER FUNCTION core.patch_graph_with_authority(jsonb,text,text,text[],text[],text,timestamptz) OWNER TO veripsa_migrator;

-- ── STRICT CONVERGENCE REPOSITORY-GENERATION CAS ─────────────────────────────────────────────────────────
--
-- A graph convergence turn deliberately releases every database connection before downloading/extracting a
-- repository.  That is the only scalable shape, but it creates a lifecycle ABA window: while the child is
-- extracting, repository X can be removed (purging its graph) and the same full_name + stable repository.id can
-- be explicitly re-added.  A final "is this id live now?" check would accept the NEW selection and let the OLD
-- extraction repopulate it.
--
-- Capture one exact activation tuple before extraction, then require that exact tuple in the SAME short
-- transaction which persists the graph.  activated_at is part of the token in addition to
-- generation_started_at: repository lifecycle intentionally preserves generation_started_at for some
-- remove→re-add sequences of the same GitHub object, whereas activated_at changes at the new selection boundary.
-- The tuple therefore closes both replacement and same-id ABA.  A first authenticated work observation may have
-- no activation yet, so capture creates the same non-authoritative activation shape reconcile_repo_identity uses
-- after a graph write.  It is content-free and gives that first extraction an exact generation to CAS against.
--
-- Lock order is the lifecycle-global order: stable repository id → mutable repo coordinate → account(shared) →
-- graph coordinate (inside the existing generic writer).  Capture releases all of these at statement commit.
-- The final wrappers hold them only around the lifecycle comparison + existing graph writer; no DB connection or
-- lock spans clone/download/extraction.  Existing generic writer signatures and grants remain unchanged for
-- rolling/old-image compatibility.  Only the App's authenticated convergence path can call these wrappers.
CREATE OR REPLACE FUNCTION core.capture_repository_graph_generation_with_authority(
    p_repo text, p_repository_id text)
    RETURNS jsonb LANGUAGE plpgsql VOLATILE SECURITY DEFINER
    SET search_path TO 'core','pg_catalog' AS $$
DECLARE
  v_account text; v_repo text; v_id text;
  v_activated_at timestamptz; v_generation_started_at timestamptz;
  v_lifecycle_authoritative boolean;
BEGIN
  v_repo := NULLIF(btrim(COALESCE(p_repo,'')),'');
  v_id := NULLIF(btrim(COALESCE(p_repository_id,'')),'');
  IF v_repo IS NULL OR length(v_repo)>512 THEN
    RAISE EXCEPTION 'repository graph generation capture needs a bounded repo'
      USING ERRCODE='23514';
  END IF;
  IF v_id IS NULL OR length(v_id)>32 OR v_id !~ '^[1-9][0-9]*$' THEN
    RAISE EXCEPTION 'repository graph generation capture needs a canonical repository id'
      USING ERRCODE='23514';
  END IF;
  SELECT account INTO v_account
    FROM core.establish_session_write_context() AS c(agent,account);
  PERFORM pg_advisory_xact_lock(hashtext('github-repository-id'),hashtext(v_id));
  PERFORM pg_advisory_xact_lock(
    hashtext(CASE WHEN v_account LIKE 'ACCT-GH-%' THEN substr(v_account,9) ELSE v_account END),
    hashtext(v_repo)
  );
  PERFORM core.assert_account_live_with_authority();
  IF NOT core.repository_account_onboarding_allowed_with_authority(v_repo,v_id) THEN
    RAISE EXCEPTION 'repository graph lifecycle generation is not live at capture'
      USING ERRCODE='55000';
  END IF;
  IF EXISTS (
      SELECT 1 FROM core.repository_lifecycle_tombstone
       WHERE account_id=v_account AND repository_id=v_id AND superseded_at IS NULL) THEN
    RAISE EXCEPTION 'repository graph lifecycle generation is tombstoned at capture'
      USING ERRCODE='55000';
  END IF;

  -- Stable-id + repo locks make this insert race-free with remove/replace/rename.  Do not promote it to lifecycle
  -- authority: only a signed repository.created / installation_repositories.added delivery owns that ordering.
  INSERT INTO core.repository_lifecycle_activation(
      account_id,repository_id,repo,activated_at,lifecycle_authoritative,generation_started_at)
  SELECT v_account,v_id,v_repo,clock_timestamp(),false,NULL
   WHERE NOT EXISTS (
     SELECT 1 FROM core.repository_lifecycle_activation
      WHERE account_id=v_account AND repository_id=v_id)
  ON CONFLICT DO NOTHING;

  SELECT activated_at,generation_started_at,lifecycle_authoritative
    INTO v_activated_at,v_generation_started_at,v_lifecycle_authoritative
    FROM core.repository_lifecycle_activation
   WHERE account_id=v_account AND repository_id=v_id AND repo=v_repo;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'repository graph lifecycle generation changed during capture'
      USING ERRCODE='55000';
  END IF;
  RETURN jsonb_build_object(
    'version','repository-graph-generation-v1',
    'repo',v_repo,
    'repository_id',v_id,
    'activated_at',v_activated_at,
    'generation_started_at',v_generation_started_at,
    'lifecycle_authoritative',v_lifecycle_authoritative
  );
END $$;
ALTER FUNCTION core.capture_repository_graph_generation_with_authority(text,text)
  OWNER TO veripsa_migrator;
REVOKE EXECUTE ON FUNCTION core.capture_repository_graph_generation_with_authority(text,text)
  FROM PUBLIC, veripsa_writer;

-- Internal final-write fence.  The exact activation comparison and the graph mutation happen in the caller's one
-- SQL transaction while this stable-id lock remains held.  Every mismatch raises a distinct operational-state
-- error before the generic writer reaches quota checks, graph DELETEs, or graph INSERTs.
CREATE OR REPLACE FUNCTION core._assert_repository_graph_generation_with_authority(
    p_repo text, p_repository_id text, p_generation jsonb)
    RETURNS text LANGUAGE plpgsql VOLATILE SECURITY DEFINER
    SET search_path TO 'core','pg_catalog' AS $$
DECLARE v_account text; v_repo text; v_id text; v_matches boolean;
BEGIN
  v_repo := NULLIF(btrim(COALESCE(p_repo,'')),'');
  v_id := NULLIF(btrim(COALESCE(p_repository_id,'')),'');
  IF v_repo IS NULL OR length(v_repo)>512
     OR v_id IS NULL OR length(v_id)>32 OR v_id !~ '^[1-9][0-9]*$'
     OR p_generation IS NULL OR jsonb_typeof(p_generation)<>'object'
     OR p_generation->>'version' IS DISTINCT FROM 'repository-graph-generation-v1'
     OR p_generation->>'repo' IS DISTINCT FROM v_repo
     OR p_generation->>'repository_id' IS DISTINCT FROM v_id THEN
    RAISE EXCEPTION 'repository graph lifecycle generation token is malformed or mismatched'
      USING ERRCODE='55000';
  END IF;
  SELECT account INTO v_account
    FROM core.establish_session_write_context() AS c(agent,account);
  PERFORM pg_advisory_xact_lock(hashtext('github-repository-id'),hashtext(v_id));
  PERFORM pg_advisory_xact_lock(
    hashtext(CASE WHEN v_account LIKE 'ACCT-GH-%' THEN substr(v_account,9) ELSE v_account END),
    hashtext(v_repo)
  );
  PERFORM core.assert_account_live_with_authority();
  IF NOT core.repository_account_onboarding_allowed_with_authority(v_repo,v_id)
     OR EXISTS (
       SELECT 1 FROM core.repository_lifecycle_tombstone
        WHERE account_id=v_account AND repository_id=v_id AND superseded_at IS NULL
     ) THEN
    RAISE EXCEPTION 'repository graph lifecycle generation is no longer live'
      USING ERRCODE='55000';
  END IF;
  SELECT EXISTS (
    SELECT 1
      FROM core.repository_lifecycle_activation a
     WHERE a.account_id=v_account
       AND a.repository_id=v_id
       AND a.repo=v_repo
       AND jsonb_typeof(p_generation->'activated_at')='string'
       AND a.activated_at=(p_generation->>'activated_at')::timestamptz
       AND (
         (p_generation->'generation_started_at'='null'::jsonb
          AND a.generation_started_at IS NULL)
         OR
         (jsonb_typeof(p_generation->'generation_started_at')='string'
          AND a.generation_started_at=
              (p_generation->>'generation_started_at')::timestamptz)
       )
       AND to_jsonb(a.lifecycle_authoritative)=p_generation->'lifecycle_authoritative'
  ) INTO v_matches;
  IF NOT COALESCE(v_matches,false) THEN
    RAISE EXCEPTION 'repository graph lifecycle generation changed before persistence'
      USING ERRCODE='55000';
  END IF;
  RETURN v_account;
END $$;
ALTER FUNCTION core._assert_repository_graph_generation_with_authority(text,text,jsonb)
  OWNER TO veripsa_migrator;
REVOKE ALL ON FUNCTION core._assert_repository_graph_generation_with_authority(text,text,jsonb)
  FROM PUBLIC;

CREATE OR REPLACE FUNCTION core.ingest_graph_with_authority_for_repository_generation(
    p_graph jsonb, p_repo text, p_branch text, p_commit_sha text,
    p_captured_at timestamptz, p_repository_id text, p_generation jsonb)
    RETURNS text LANGUAGE plpgsql VOLATILE SECURITY DEFINER
    SET search_path TO 'core','pg_catalog' AS $$
BEGIN
  PERFORM core._assert_repository_graph_generation_with_authority(
    p_repo,p_repository_id,p_generation);
  RETURN core.ingest_graph_with_authority(
    p_graph,p_repo,p_branch,p_commit_sha,p_captured_at);
END $$;
ALTER FUNCTION core.ingest_graph_with_authority_for_repository_generation(
    jsonb,text,text,text,timestamptz,text,jsonb) OWNER TO veripsa_migrator;
REVOKE EXECUTE ON FUNCTION core.ingest_graph_with_authority_for_repository_generation(
    jsonb,text,text,text,timestamptz,text,jsonb) FROM PUBLIC, veripsa_writer;

CREATE OR REPLACE FUNCTION core.patch_graph_with_authority_for_repository_generation(
    p_subgraph jsonb, p_repo text, p_branch text, p_changed_paths text[],
    p_removed_paths text[], p_commit_sha text, p_captured_at timestamptz,
    p_repository_id text, p_generation jsonb)
    RETURNS jsonb LANGUAGE plpgsql VOLATILE SECURITY DEFINER
    SET search_path TO 'core','pg_catalog' AS $$
BEGIN
  PERFORM core._assert_repository_graph_generation_with_authority(
    p_repo,p_repository_id,p_generation);
  RETURN core.patch_graph_with_authority(
    p_subgraph,p_repo,p_branch,p_changed_paths,p_removed_paths,p_commit_sha,p_captured_at);
END $$;
ALTER FUNCTION core.patch_graph_with_authority_for_repository_generation(
    jsonb,text,text,text[],text[],text,timestamptz,text,jsonb) OWNER TO veripsa_migrator;
REVOKE EXECUTE ON FUNCTION core.patch_graph_with_authority_for_repository_generation(
    jsonb,text,text,text[],text[],text,timestamptz,text,jsonb) FROM PUBLIC, veripsa_writer;

-- record_collision_with_authority: a refused claim is a clobber that DID NOT happen — log it as the
-- 'collision_held' event KIND (the no-乱立 ledger). Best-effort: NULL if no holder (a race), never raises.
-- DROP the prior 3-arg signature first: adding a trailing param via CREATE OR REPLACE would otherwise leave the
-- old overload behind on a re-apply (dead code). Mirrors the _claim_adjacency signature-change pattern (70_social).
DROP FUNCTION IF EXISTS core.record_collision_with_authority(text,text,text);
DROP FUNCTION IF EXISTS core.record_collision_with_authority(text,text,text,text);  -- superseded by the +p_change_id 5-arg (PR attribution on the held-collision ledger)
-- p_change_id (optional): the in-flight change that was HELD here (the acting PR, 'PR-<n>'), stored in event.detail
-- so the file-history surface can say WHICH PR was serialized on this path — exactly as warn_issued already carries
-- its change_id in detail. Content-free (a PR number, never a body). Defaults to '' (older callers / a missing ref).
-- p_blocked_agent (optional): WHO was actually held. Under the HOSTED DELEGATION path the connecting identity
-- is the App service seat (AG-APP), but the agent that was turned away is the PR's REAL AUTHOR (GH-<login>) the
-- caller reserved on behalf of. Pass that author so the ledger (collision/effect/notifications surfaces — the
-- buyer's proof-of-value) records the real author as 'blocked', never "Veripsa App". When omitted (the buyer's
-- own-writer path), it falls back to the connecting identity = the buyer's seat (identical to the old behavior).
CREATE OR REPLACE FUNCTION core.record_collision_with_authority(p_target_path text, p_repo text DEFAULT '', p_branch text DEFAULT '', p_blocked_agent text DEFAULT NULL, p_change_id text DEFAULT '')
    RETURNS text LANGUAGE plpgsql SECURITY DEFINER SET search_path TO 'core','pg_catalog' AS $$
DECLARE v_agent text; v_account text; v_repo text; v_branch text; v_holder text; v_id text;
BEGIN
  IF p_target_path IS NULL OR btrim(p_target_path)='' OR length(p_target_path)>1024 THEN RETURN NULL; END IF;
  v_repo := left(COALESCE(p_repo,''),512); v_branch := left(COALESCE(p_branch,''),512);
  SELECT agent, account INTO v_agent, v_account FROM core.establish_session_write_context() AS c(agent,account);
  -- THE LEDGER WALL: over the events cap → record NO collision_held row (this id is md5(random()) = a fresh row
  -- per call, the WORST unbounded growth past the #95 wall). The CLAIM/queue coordination already ran in the
  -- caller (declare/act_for) and is unaffected — only this append-only ledger side-effect is skipped. RETURN NULL
  -- = "recorded nothing" (this fn's existing no-holder sentinel; the caller PERFORMs it and ignores the result).
  IF core._ledger_write_blocked(v_account) THEN RETURN NULL; END IF;
  -- ACTOR-OVERRIDE is DELEGATION-ONLY: the p_blocked_agent override (record the held collision AS the real PR
  -- author, GH-<login>, rather than the connecting seat) is honored ONLY for the App service identity. For any
  -- other caller the override is IGNORED and the actor is the trusted connection identity — so a buyer seat can
  -- never forge "GH-victim was blocked here" on the ledger. (The own-writer path always omits it anyway; this
  -- only walls a hostile non-App caller from supplying a forged actor. session_user is the SAME trusted signal
  -- resolve_session_identity uses to honor the installation pin.)
  IF session_user = 'veripsa_app' THEN
    v_agent := COALESCE(NULLIF(btrim(COALESCE(p_blocked_agent,'')),''), v_agent);  -- the REAL blocked author under delegation
  END IF;
  SELECT agent_id INTO v_holder FROM core.claim
   WHERE account_id=v_account AND repo=v_repo AND branch=v_branch AND target_path=p_target_path AND claim_state='active' LIMIT 1;
  IF v_holder IS NULL THEN RETURN NULL; END IF;
  v_id := 'EV-'||substr(md5(random()::text||clock_timestamp()::text||v_agent),1,16);
  PERFORM core.mark_governed_write('event');
  INSERT INTO core.event(event_id, account_id, kind, agent_id, counterparty_agent, repo, branch, path, detail)
  VALUES (v_id, v_account, 'collision_held', v_agent, v_holder, v_repo, v_branch, p_target_path, left(COALESCE(p_change_id,''),200));
  RETURN v_id;
END $$;
ALTER FUNCTION core.record_collision_with_authority(text,text,text,text,text) OWNER TO veripsa_migrator;

-- record_compat_finding_with_authority: a signature-compatibility finding between TWO analyzed heads — the
-- 'compat_finding' event KIND (the one-ledger law: a new signal is a new KIND, never a new table; the
-- collision_held / push precedents). Appends ONE row mapping:
--   producer head  → commit_sha         (the side whose def shape is the contract)
--   consumer head  → counterparty_sha   (the side whose calls are checked against it)
--   finding id     → fact_fingerprint   (a stable content-free hash over the shape facts — the dedupe key)
--   rule/reason    → detail             (a BOUNDED reason CODE — e.g. 'breaking:required_arity_increase' —
--                                        NEVER raw code, NEVER a parameter default value; the safe-charset
--                                        wall below refuses anything body-shaped LOUDLY rather than truncate,
--                                        so no prefix of a code body can ever persist)
-- INERT AT THIS PR: the function exists and is gated/tested; no production path calls it yet (the compat
-- analysis wiring lands behind VERIPSA_COMPAT_ANALYSIS in a later PR). App-delegation-only, like record_push:
-- a buyer writer must not forge a compatibility fact.
-- MALFORMED INPUT → REFUSE LOUDLY (the record_push house style — ERRCODE 23514): the SHAs and the fingerprint
-- are produced by our own analysis code, so a junk value is a caller bug, never a tenant's bad request.
-- IDEMPOTENT (the record_push deterministic-id precedent): the id is a hash of
-- (account, repo, producer head, consumer head, fact_fingerprint) + ON CONFLICT DO NOTHING — so a webhook
-- redelivery / a re-analysis of the SAME head pair records the SAME finding ONCE; a NEW head pair (either
-- side moved) mints a new id = a new row (branch/path are attribution, not identity — they never dedupe).
--
-- CORRECTIVE LANE S3a (docs/COMPATIBILITY_TRAFFIC_CONTROL_PLAN.md §3 lane 3): the row now carries an
-- EXPLICIT classification + detector stamp in two typed columns (event.fact_class / event.detector —
-- 20_core.sql), so taxonomy is a COLUMN EQUALITY, never a reason-string parse:
--   fact_class → contract_delta_observation | rebase_needed_observation | divergent_definition_observation
--                | evidence_backed_incompatibility. ONLY consumer_call_mismatch:* rows are the evidence
--                class — the other three are observation telemetry, never a proven-breakage claim.
--   detector   → the writer's detector identity+version as ONE bounded token
--                ('python-call-compat/py-call-v1'), so a later current-surface read (lane S3b) can exclude
--                rows an older detector wrote by equality.
-- The WRITER passes the class explicitly (_compat_analysis); the fn validates against the closed enum and
-- refuses junk loudly. NULL class/detector = the legacy/unclassified shape (pre-S3a rows, legacy callers).
-- ADDITIVE OVERLOAD (the release_account_claims / offboard_repository arity precedent): this 9-arg fn is
-- the real body and carries NO parameter defaults (so a 7-arg call can never be ambiguous); the original
-- 7-arg signature below stays as a delegating wrapper (NULL class/detector) so every existing caller and
-- the tamper/perimeter probes stay valid.
CREATE OR REPLACE FUNCTION core.record_compat_finding_with_authority(
    p_repo text, p_branch text, p_path text,
    p_commit_sha text, p_counterparty_sha text, p_fact_fingerprint text, p_detail text,
    p_fact_class text, p_detector text)
    RETURNS text LANGUAGE plpgsql SECURITY DEFINER SET search_path TO 'core','pg_catalog' AS $$
DECLARE v_agent text; v_account text; v_repo text; v_branch text; v_path text;
        v_sha text; v_csha text; v_fp text; v_detail text; v_class text; v_detector text; v_id text;
BEGIN
  v_sha := NULLIF(btrim(COALESCE(p_commit_sha,'')),'');
  IF v_sha IS NULL OR v_sha !~ '^[0-9a-fA-F]{7,64}$' THEN
    RAISE EXCEPTION 'compat finding needs a hex producer commit_sha (7-64 hex)' USING ERRCODE='23514';
  END IF;
  v_csha := NULLIF(btrim(COALESCE(p_counterparty_sha,'')),'');
  IF v_csha IS NULL OR v_csha !~ '^[0-9a-fA-F]{7,64}$' THEN
    RAISE EXCEPTION 'compat finding needs a hex consumer counterparty_sha (7-64 hex)' USING ERRCODE='23514';
  END IF;
  v_fp := NULLIF(btrim(COALESCE(p_fact_fingerprint,'')),'');
  IF v_fp IS NULL OR length(v_fp) > 128 OR v_fp !~ '^[A-Za-z0-9_.:-]+$' THEN
    RAISE EXCEPTION 'compat finding needs a bounded fact_fingerprint (<=128, reference-token charset)' USING ERRCODE='23514';
  END IF;
  -- the CONTENT-FREE wall on detail: a reason CODE only (no whitespace, no quotes/parens/braces — nothing a
  -- code body or a parameter default expression is made of). REFUSE, never truncate: truncation would persist
  -- the first 200 chars of whatever body-shaped text a buggy caller passed.
  v_detail := COALESCE(p_detail,'');
  IF length(v_detail) > 200 OR v_detail !~ '^[A-Za-z0-9_.:,+/-]*$' THEN
    RAISE EXCEPTION 'compat finding detail must be a bounded reason code (<=200, safe charset — never code)' USING ERRCODE='23514';
  END IF;
  -- S3a CLASSIFICATION WALL: the class is a CLOSED enum — anything else is a caller bug, refused loudly
  -- (never coerced, never truncated). NULL = the honest unclassified/legacy shape, always admitted.
  v_class := NULLIF(btrim(COALESCE(p_fact_class,'')),'');
  IF v_class IS NOT NULL AND v_class NOT IN (
      'contract_delta_observation','rebase_needed_observation',
      'divergent_definition_observation','evidence_backed_incompatibility') THEN
    RAISE EXCEPTION 'compat finding fact_class must be one of the four bounded classification codes' USING ERRCODE='23514';
  END IF;
  -- S3a DETECTOR WALL: a bounded reference token only ('<name>/<version>') — never body-shaped text.
  v_detector := NULLIF(btrim(COALESCE(p_detector,'')),'');
  IF v_detector IS NOT NULL AND (length(v_detector) > 64 OR v_detector !~ '^[A-Za-z0-9_./-]+$') THEN
    RAISE EXCEPTION 'compat finding detector must be a bounded reference token (<=64, safe charset)' USING ERRCODE='23514';
  END IF;
  v_repo := left(COALESCE(p_repo,''),512); v_branch := left(COALESCE(p_branch,''),512); v_path := left(COALESCE(p_path,''),1024);
  SELECT agent, account INTO v_agent, v_account FROM core.establish_session_write_context() AS c(agent,account);
  -- THE LEDGER WALL: over the events cap → record NO finding (append-only ledger growth past the #95 wall).
  -- Advisory: RETURN NULL, never RAISE (same as record_collision — over-quota is not a bad request).
  IF core._ledger_write_blocked(v_account) THEN RETURN NULL; END IF;
  -- IDEMPOTENT id: a finding is uniquely (account, repo, producer head, consumer head, fingerprint) —
  -- class/detector are NOT identity (the fingerprint basis already carries rule + detector version), so a
  -- redelivery can never mint a duplicate row just because a stamp differs; the append-only first row wins.
  v_id := 'EV-COMPAT-'||substr(md5(v_account||'|'||v_repo||'|'||v_sha||'|'||v_csha||'|'||v_fp),1,24);
  PERFORM core.mark_governed_write('event');
  INSERT INTO core.event(event_id, account_id, kind, agent_id, repo, branch, path, commit_sha, counterparty_sha, fact_fingerprint, detail, fact_class, detector)
  VALUES (v_id, v_account, 'compat_finding', v_agent, v_repo, v_branch, v_path, v_sha, v_csha, v_fp, v_detail, v_class, v_detector)
  ON CONFLICT (account_id, event_id) DO NOTHING;
  RETURN v_id;
END $$;
ALTER FUNCTION core.record_compat_finding_with_authority(text,text,text,text,text,text,text,text,text) OWNER TO veripsa_migrator;

-- The ORIGINAL 7-arg signature — kept as a delegating wrapper (additive-overload discipline): every
-- pre-S3a caller/test stays valid and records the honest UNCLASSIFIED shape (NULL class/detector — the
-- legacy-evidence marker, never a guessed class). Resolution is unambiguous because the 9-arg overload
-- above carries no defaults.
CREATE OR REPLACE FUNCTION core.record_compat_finding_with_authority(
    p_repo text, p_branch text, p_path text,
    p_commit_sha text, p_counterparty_sha text, p_fact_fingerprint text, p_detail text DEFAULT '')
    RETURNS text LANGUAGE sql SECURITY DEFINER SET search_path TO 'core','pg_catalog' AS $$
  SELECT core.record_compat_finding_with_authority(
    p_repo, p_branch, p_path, p_commit_sha, p_counterparty_sha, p_fact_fingerprint, p_detail,
    NULL::text, NULL::text);
$$;
ALTER FUNCTION core.record_compat_finding_with_authority(text,text,text,text,text,text,text) OWNER TO veripsa_migrator;

-- record_push_with_authority: a push reaching main — the 'push' event KIND. Append-only.
CREATE OR REPLACE FUNCTION core.record_push_with_authority(p_repo text, p_branch text, p_commit_sha text, p_model text DEFAULT NULL)
    RETURNS text LANGUAGE plpgsql SECURITY DEFINER SET search_path TO 'core','pg_catalog' AS $$
DECLARE v_agent text; v_account text; v_sha text; v_id text; v_quota jsonb;
BEGIN
  v_sha := NULLIF(btrim(COALESCE(p_commit_sha,'')),'');
  IF v_sha IS NULL OR length(v_sha)>64 OR v_sha !~ '^[0-9a-fA-F]+$' THEN RAISE EXCEPTION 'push needs a hex commit_sha' USING ERRCODE='23514'; END IF;
  SELECT agent, account INTO v_agent, v_account FROM core.establish_session_write_context() AS c(agent,account);
  -- THE WALL: over the free line → record NO push event + return the structured signal (as text). Advisory. The
  -- landing wrappers (land_on_main / land_change) call this; they still release claims (frees rows, not growth).
  v_quota := core._refuse_if_over_quota(v_account);
  IF v_quota IS NOT NULL THEN RETURN v_quota::text; END IF;
  -- IDEMPOTENT id: a push is uniquely (account,repo,branch,sha). Deterministic id + ON CONFLICT DO NOTHING →
  -- a redelivered push webhook (or a re-push of the same sha) records the 'push' fact ONCE (no inflated count).
  v_id := 'EV-PUSH-'||substr(md5(v_account||'|'||left(COALESCE(p_repo,''),512)||'|'||left(COALESCE(p_branch,''),512)||'|'||v_sha),1,24);
  PERFORM core.mark_governed_write('event');
  INSERT INTO core.event(event_id, account_id, kind, agent_id, repo, branch, commit_sha, model)
  VALUES (v_id, v_account, 'push', v_agent, left(COALESCE(p_repo,''),512), left(COALESCE(p_branch,''),512), v_sha, left(p_model,128))
  ON CONFLICT (account_id, event_id) DO NOTHING;
  RETURN v_id;
END $$;
ALTER FUNCTION core.record_push_with_authority(text,text,text,text) OWNER TO veripsa_migrator;

-- _conclude_change_tombstone: the ORDER-INDEPENDENCE backstop for the "concluded WITH NO surviving claim row"
-- case. change_concluded() answers "has this change already landed/withdrawn?" by reading the claim table
-- (total>0 AND no live claim). That works whenever the open was SEEN before the close (the released claims
-- linger as the proof). But GitHub does NOT guarantee delivery ORDER: a `closed/merged` can arrive BEFORE its
-- `opened` (reorder) — then land_change releases ZERO claims (none were ever declared), the table stays EMPTY
-- for this change, change_concluded reads total=0 → "not concluded", and a LATER stale opened/synchronize
-- RESURRECTS the merged PR as falsely in-flight (the exact bug). The fix: when a land/withdraw frees no claim
-- for the change, leave ONE released TOMBSTONE row (claim_state='released', a sentinel path) so the conclusion
-- is durable in the table change_concluded already reads — total becomes >0, live stays 0 → concluded=true.
-- SELF-CORRECTING: a tombstone only makes change_concluded true while there is NO live claim; a LEGITIMATE
-- reopen (a never-merged PR) re-activates the lanes → live>0 → concluded flips back to false automatically (no
-- explicit clear needed). The sentinel path can never be an active lane (state is 'released'), so it is invisible
-- to every coordination read (all filter active/waiting). Idempotent (ON CONFLICT DO NOTHING). Content-free.
CREATE OR REPLACE FUNCTION core._conclude_change_tombstone(p_account text, p_agent text, p_change text, p_repo text, p_branch text)
    RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path TO 'core','pg_catalog' AS $$
DECLARE v_account text; v_agent text; v_change text; v_repo text; v_branch text; v_id text;
BEGIN
  -- IDENTITY IS RE-DERIVED, NOT TRUSTED FROM THE CALLER. This is a SECURITY DEFINER fn (runs as the migrator
  -- owner, bypasses RLS) that ARMS mark_governed_write('claim') and INSERTs a claim row; trusting p_account
  -- would let any caller forge a `<change>:__concluded__` tombstone in a VICTIM tenant (→ change_concluded()
  -- returns true → the webhook skips analysis → a CROSS-TENANT SILENT MISS). So re-derive the writing account
  -- and agent from the CONNECTION ROLE'S credential (establish_session_write_context, the pattern every sibling
  -- gate fn uses) and write under THAT — never the p_account/p_agent argument. The two in-schema callers
  -- (land_change / release_change) already run this exact derivation and pass the SAME values, so the
  -- re-derivation is byte-identical for the legit owner path and changes nothing for them.
  SELECT agent, account INTO v_agent, v_account FROM core.establish_session_write_context() AS c(agent,account);
  v_change := left(NULLIF(btrim(COALESCE(p_change,'')),''),200);
  IF v_change IS NULL THEN RETURN; END IF;
  v_repo := left(COALESCE(p_repo,''),512); v_branch := left(COALESCE(NULLIF(p_branch,''),'main'),512);
  -- already have a row for this change (a real claim OR a prior tombstone) → nothing to add (change_concluded
  -- already sees it). Only the no-claim-ever case needs the marker.
  IF EXISTS (SELECT 1 FROM core.claim WHERE account_id=v_account AND repo=v_repo AND change_id=v_change) THEN
    RETURN;
  END IF;
  v_id := v_change||':__concluded__';
  PERFORM core.mark_governed_write('claim');
  INSERT INTO core.claim(claim_id, account_id, agent_id, change_id, repo, branch, target_path, claim_state, released_at)
  VALUES (left(v_id,200), v_account, v_agent, v_change, v_repo, v_branch, '__veripsa_concluded__', 'released', now())
  ON CONFLICT (account_id, repo, claim_id) DO NOTHING;
END $$;
ALTER FUNCTION core._conclude_change_tombstone(text,text,text,text,text) OWNER TO veripsa_migrator;
-- INTERNAL-ONLY (cross-tenant silent-miss fix): SECURITY DEFINER + arms a claim write. Postgres grants EXECUTE
-- to PUBLIC by DEFAULT on every CREATE FUNCTION; without this REVOKE any role with the PUBLIC default (a future
-- customer DB seat) could call it directly to forge a `<change>:__concluded__` tombstone in another tenant and
-- suppress that tenant's analysis. Callable ONLY via the in-schema land_change/release_change wrappers (which run
-- as the migrator owner and re-derive identity from the connection role). Mirrors expire_stale_claims() below.
REVOKE EXECUTE ON FUNCTION core._conclude_change_tombstone(text,text,text,text,text) FROM PUBLIC;

-- land_on_main_with_authority: a PR REACHED main (着地). Records the push (the landing fact) AND releases
-- the landing agent's in-flight reservations at {repo,'main'} — its active+waiting claims are freed, and
-- any waiter behind a freed lane is promoted (a serialized PR advances). Returns the "着地しました" payload
-- {ok, landed, commit_sha, event_id, released:[paths], promoted:[agents]}. Re-ingesting main's graph after
-- the landing is a SEPARATE ingest_graph_with_authority call (the App holds the new graph payload, not this).
CREATE OR REPLACE FUNCTION core.land_on_main_with_authority(p_repo text, p_commit_sha text, p_model text DEFAULT NULL, p_branch text DEFAULT 'main')
    RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path TO 'core','pg_catalog' AS $$
DECLARE v_agent text; v_account text; v_repo text; v_branch text; v_sha text; v_event text;
        v_released text[]; v_promoted text[] := ARRAY[]::text[]; v_path text; v_prom text;
BEGIN
  v_sha := NULLIF(btrim(COALESCE(p_commit_sha,'')),'');
  IF v_sha IS NULL OR length(v_sha)>64 OR v_sha !~ '^[0-9a-fA-F]+$' THEN RAISE EXCEPTION 'land needs a hex commit_sha' USING ERRCODE='23514'; END IF;
  v_repo := left(COALESCE(p_repo,''),512);
  v_branch := left(COALESCE(NULLIF(p_branch,''),'main'),512);
  SELECT agent, account INTO v_agent, v_account FROM core.establish_session_write_context() AS c(agent,account);
  -- (1) the landing fact (push event on the protected branch)
  v_event := core.record_push_with_authority(v_repo, v_branch, v_sha, p_model);
  -- (2) free this agent's in-flight reservations on the protected branch (active + waiting); collect the lanes
  PERFORM core.mark_governed_write('claim');
  WITH freed AS (
    UPDATE core.claim SET claim_state='released', released_at=now()
     WHERE account_id=v_account AND agent_id=v_agent AND repo=v_repo AND branch=v_branch AND claim_state IN ('active','waiting')
    RETURNING target_path)
  SELECT array_agg(DISTINCT target_path) INTO v_released FROM freed;
  -- (3) promote the next waiter on each freed lane (a serialized PR advances — highway: a car leaves, the next moves up)
  IF v_released IS NOT NULL THEN
    FOREACH v_path IN ARRAY v_released LOOP
      v_prom := core._promote_next_waiter(v_account, v_repo, v_branch, v_path);
      IF v_prom IS NOT NULL THEN v_promoted := array_append(v_promoted, core.agent_name(v_prom)); END IF;
    END LOOP;
  END IF;
  RETURN jsonb_build_object('ok',true,'landed',true,'commit_sha',v_sha,'event_id',v_event,'branch',v_branch,
    'released', COALESCE(to_jsonb(v_released), '[]'::jsonb),
    'promoted', COALESCE(to_jsonb(v_promoted), '[]'::jsonb));
END $$;
ALTER FUNCTION core.land_on_main_with_authority(text,text,text,text) OWNER TO veripsa_migrator;

-- land_change_on_main_with_authority: the GitHub-App path. A PR reaching main releases only that PR/change's
-- reservations, not every open reservation by the same author/agent. This is the public-router identity:
-- agent = actor, change_id = PR/lane bundle.
CREATE OR REPLACE FUNCTION core.land_change_on_main_with_authority(p_change_id text, p_repo text, p_commit_sha text, p_model text DEFAULT NULL, p_branch text DEFAULT 'main')
    RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path TO 'core','pg_catalog' AS $$
DECLARE v_agent text; v_account text; v_repo text; v_branch text; v_sha text; v_change text; v_event text;
        v_released text[]; v_promoted text[] := ARRAY[]::text[]; v_path text; v_prom text;
BEGIN
  v_change := left(NULLIF(btrim(COALESCE(p_change_id,'')),''),200);
  IF v_change IS NULL THEN RAISE EXCEPTION 'land_change needs a change_id' USING ERRCODE='23514'; END IF;
  v_sha := NULLIF(btrim(COALESCE(p_commit_sha,'')),'');
  IF v_sha IS NULL OR length(v_sha)>64 OR v_sha !~ '^[0-9a-fA-F]+$' THEN RAISE EXCEPTION 'land needs a hex commit_sha' USING ERRCODE='23514'; END IF;
  v_repo := left(COALESCE(p_repo,''),512);
  v_branch := left(COALESCE(NULLIF(p_branch,''),'main'),512);
  SELECT agent, account INTO v_agent, v_account FROM core.establish_session_write_context() AS c(agent,account);
  v_event := core.record_push_with_authority(v_repo, v_branch, v_sha, p_model);
  PERFORM core.mark_governed_write('claim');
  WITH freed AS (
    UPDATE core.claim SET claim_state='released', released_at=now()
     WHERE account_id=v_account AND change_id=v_change AND repo=v_repo AND branch=v_branch
       AND claim_state IN ('active','waiting')
    RETURNING target_path)
  SELECT array_agg(DISTINCT target_path) INTO v_released FROM freed;
  IF v_released IS NOT NULL THEN
    FOREACH v_path IN ARRAY v_released LOOP
      v_prom := core._promote_next_waiter(v_account, v_repo, v_branch, v_path);
      IF v_prom IS NOT NULL THEN v_promoted := array_append(v_promoted, core.agent_name(v_prom)); END IF;
    END LOOP;
  END IF;
  -- ORDER-INDEPENDENCE: a merge for a change whose `opened` was NEVER seen (reordered delivery) freed NO claim
  -- and would otherwise leave NO trace → a later stale opened/synchronize resurrects it. Leave a conclusion
  -- tombstone so change_concluded() sees it (it no-ops when a real claim row already exists). Content-free.
  PERFORM core._conclude_change_tombstone(v_account, v_agent, v_change, v_repo, v_branch);
  RETURN jsonb_build_object('ok',true,'landed',true,'change_id',v_change,'commit_sha',v_sha,'event_id',v_event,'branch',v_branch,
    'released', COALESCE(to_jsonb(v_released), '[]'::jsonb),
    'promoted', COALESCE(to_jsonb(v_promoted), '[]'::jsonb));
END $$;
ALTER FUNCTION core.land_change_on_main_with_authority(text,text,text,text,text) OWNER TO veripsa_migrator;

-- release_change_on_main_with_authority: a change LEFT the in-flight set WITHOUT landing (its PR was closed
-- abandoned / superseded — never merged). Free that change's reserved lanes and promote the next waiter on
-- each — exactly like a landing's release, but it records NO landing (nothing reached main). This is the
-- cancel/withdraw half of the lifecycle: a blocker that gives up must release its queue IMMEDIATELY, never
-- strand the waiters behind it until the lease expires. Releases by account_id+change_id (the App acts for
-- the author, same delegation model as land_change). SECURITY DEFINER; identity from the connection role.
CREATE OR REPLACE FUNCTION core.release_change_on_main_with_authority(p_change_id text, p_repo text, p_branch text DEFAULT 'main')
    RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path TO 'core','pg_catalog' AS $$
DECLARE v_agent text; v_account text; v_repo text; v_branch text; v_change text;
        v_released text[]; v_promoted text[] := ARRAY[]::text[]; v_path text; v_prom text;
BEGIN
  v_change := left(NULLIF(btrim(COALESCE(p_change_id,'')),''),200);
  IF v_change IS NULL THEN RAISE EXCEPTION 'release_change needs a change_id' USING ERRCODE='23514'; END IF;
  v_repo := left(COALESCE(p_repo,''),512);
  v_branch := left(COALESCE(NULLIF(p_branch,''),'main'),512);
  SELECT agent, account INTO v_agent, v_account FROM core.establish_session_write_context() AS c(agent,account);
  PERFORM core.mark_governed_write('claim');
  WITH freed AS (
    UPDATE core.claim SET claim_state='released', released_at=now()
     WHERE account_id=v_account AND change_id=v_change AND repo=v_repo AND branch=v_branch
       AND claim_state IN ('active','waiting')
    RETURNING target_path)
  SELECT array_agg(DISTINCT target_path) INTO v_released FROM freed;
  IF v_released IS NOT NULL THEN
    FOREACH v_path IN ARRAY v_released LOOP
      v_prom := core._promote_next_waiter(v_account, v_repo, v_branch, v_path);
      IF v_prom IS NOT NULL THEN v_promoted := array_append(v_promoted, core.agent_name(v_prom)); END IF;
    END LOOP;
  END IF;
  -- ORDER-INDEPENDENCE: a close/withdraw for a change whose `opened` was never seen (reorder) freed NO claim →
  -- leave a conclusion tombstone (no-op when a real claim row exists) so a later stale opened/synchronize is
  -- skipped. A LEGITIMATE reopen of this never-merged PR re-activates lanes → change_concluded flips back to
  -- false (a live claim outvotes the released tombstone) → it is processed, exactly as a genuine reopen must be.
  PERFORM core._conclude_change_tombstone(v_account, v_agent, v_change, v_repo, v_branch);
  RETURN jsonb_build_object('ok',true,'landed',false,'withdrawn',true,'change_id',v_change,'branch',v_branch,
    'released', COALESCE(to_jsonb(v_released), '[]'::jsonb),
    'promoted', COALESCE(to_jsonb(v_promoted), '[]'::jsonb));
END $$;
ALTER FUNCTION core.release_change_on_main_with_authority(text,text,text) OWNER TO veripsa_migrator;

-- ── RECONCILIATION BACKSTOP — the lock lifecycle is event-driven; a missed/edited webhook would otherwise
--    corrupt the lane state with no self-heal. These two functions make the live claim set CONVERGE to the
--    GitHub truth (the App passes that truth in: a PR's CURRENT file set; the repo's CURRENT open-PR set), so
--    a dropped 'closed' delivery, a synchronize that no longer touches a file, or a base-branch retarget can
--    never strand a lane permanently. Same delegation/identity model as land_change/release_change (the App
--    acts for the author; account from the connection session; content-free — paths/ids only, never bodies).

-- reconcile_change_claims_with_authority: SYNCHRONIZE BACKSTOP. Given a change's (PR's) CURRENT file set at
-- (repo,branch), make THIS change's reservations EXACTLY match it: (a) RELEASE every active/waiting claim of
-- this change whose target_path is NO LONGER in p_paths (a file the PR dropped — closes #1's stale-claim leak;
-- and because the whole set is keyed by the (repo,branch) coordinate, a base-branch RETARGET that re-syncs at
-- the new coordinate leaves no claim on the OLD coordinate's paths — closes #4), promoting the next waiter on
-- each freed lane (a serialized PR advances). (b) ENSURE a claim exists for every path STILL in p_paths (the
-- App's per-path declare_claim is the primary writer; this is the idempotent backstop so a missed insert is
-- repaired) — attributed to the agent already on this change's claims (the real author), else the connecting
-- agent. Releasing is the safe half (it can only FREE lanes); ensuring funnels through _place_claim so the
-- queue truth never drifts. NULL/empty p_paths means the PR touches nothing → release ALL of this change's lanes.
CREATE OR REPLACE FUNCTION core.reconcile_change_claims_with_authority(p_change_id text, p_repo text, p_branch text, p_paths text[])
    RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path TO 'core','pg_catalog' AS $$
DECLARE v_app_agent text; v_account text; v_change text; v_repo text; v_branch text; v_owner_agent text;
        v_paths text[]; v_released text[]; v_promoted text[] := ARRAY[]::text[]; v_ensured text[] := ARRAY[]::text[];
        v_path text; v_prom text; v_has text;
BEGIN
  v_change := left(NULLIF(btrim(COALESCE(p_change_id,'')),''),200);
  IF v_change IS NULL THEN RAISE EXCEPTION 'reconcile_change needs a change_id' USING ERRCODE='23514'; END IF;
  v_repo := left(COALESCE(p_repo,''),512);
  v_branch := left(COALESCE(NULLIF(p_branch,''),'main'),512);
  -- the CURRENT file set, normalized + deduped + bounded (200, mirrors the landing cap); '' = touches nothing.
  v_paths := ARRAY(SELECT DISTINCT left(p,1024) FROM unnest(COALESCE(p_paths,'{}'::text[])) AS p
                    WHERE p IS NOT NULL AND btrim(p)<>'' LIMIT 200);
  SELECT agent, account INTO v_app_agent, v_account FROM core.establish_session_write_context() AS c(agent,account);

  -- (a) RELEASE this change's active/waiting claims whose path is NOT in the current set; collect the freed lanes.
  PERFORM core.mark_governed_write('claim');
  WITH freed AS (
    UPDATE core.claim SET claim_state='released', released_at=now()
     WHERE account_id=v_account AND change_id=v_change AND repo=v_repo AND branch=v_branch
       AND claim_state IN ('active','waiting')
       AND NOT (target_path = ANY(v_paths))         -- v_paths empty → ANY(empty) is false → ALL released
    RETURNING target_path)
  SELECT array_agg(DISTINCT target_path) INTO v_released FROM freed;
  IF v_released IS NOT NULL THEN
    FOREACH v_path IN ARRAY v_released LOOP
      v_prom := core._promote_next_waiter(v_account, v_repo, v_branch, v_path);
      IF v_prom IS NOT NULL THEN v_promoted := array_append(v_promoted, core.agent_name(v_prom)); END IF;
    END LOOP;
  END IF;

  -- (b) ENSURE a claim exists for each path STILL in the set. Attribute to the agent already on THIS change's
  --     claims (the real PR author the App reserved as), falling back to the connecting agent. _place_claim is
  --     idempotent (re-declaring returns the existing grant/queue place) so this only repairs a MISSING claim.
  IF array_length(v_paths,1) IS NOT NULL THEN
    SELECT agent_id INTO v_owner_agent FROM core.claim
     WHERE account_id=v_account AND change_id=v_change AND repo=v_repo AND branch=v_branch
     ORDER BY claimed_at ASC LIMIT 1;
    v_owner_agent := COALESCE(v_owner_agent, v_app_agent);
    FOREACH v_path IN ARRAY v_paths LOOP
      SELECT claim_id INTO v_has FROM core.claim
       WHERE account_id=v_account AND change_id=v_change AND repo=v_repo AND branch=v_branch
         AND target_path=v_path AND claim_state IN ('active','waiting') LIMIT 1;
      IF v_has IS NULL THEN
        PERFORM core._place_claim(v_change||':'||v_path, v_path, v_repo, v_branch, v_account, v_owner_agent);
        v_ensured := array_append(v_ensured, v_path);
      END IF;
    END LOOP;
  END IF;

  RETURN jsonb_build_object('ok',true,'reconciled',true,'change_id',v_change,'repo',v_repo,'branch',v_branch,
    'kept', COALESCE(to_jsonb(v_paths), '[]'::jsonb),
    'released', COALESCE(to_jsonb(v_released), '[]'::jsonb),
    'promoted', COALESCE(to_jsonb(v_promoted), '[]'::jsonb),
    'ensured', COALESCE(to_jsonb(v_ensured), '[]'::jsonb));
END $$;
ALTER FUNCTION core.reconcile_change_claims_with_authority(text,text,text,text[]) OWNER TO veripsa_migrator;

-- reconcile_repo_claims_with_authority: BACKFILL BACKSTOP (the dropped-'closed' self-heal). Given the repo's
-- CURRENT live OPEN-PR set at (repo,branch), RELEASE every active/waiting PR-claim at that coordinate whose
-- change_id is NOT among the open changes — the change has left the in-flight set (its PR merged or closed) but
-- a missed 'closed'/merge delivery left its lanes stranded forever (closes #3: everyone behind it waits). Each
-- freed lane promotes its next waiter (the stranded line finally advances). Active/waiting are the only states
-- touched, so already-released/expired claims (a "landed" change is released) are untouched — only genuinely
-- in-flight lanes of a no-longer-open change are reclaimed. The App calls this from its periodic backfill with
-- the live open-PR list.
--
-- SCOPED TO PR-CLAIMS ('PR-<n>'): the open set is the live OPEN-PR set, so this backstop converges PR-keyed
-- lanes. A pre-PR push reservation ('BR-<branch>' — a feature branch reserves lanes the moment it is pushed,
-- BEFORE any PR exists) is GOVERNED BY A DIFFERENT LIFECYCLE (the server releases it when the branch's PR opens
-- and re-claims as 'PR-<n>'; see server.handle_event's push↔PR reconciliation), and is NOT represented in the
-- open-PR set. Without this scope, the backfill would treat every 'BR-<branch>' claim as "not in the open set"
-- and wrongly RELEASE legitimate pre-PR reservations (and promote waiters past them) on every pass. So we touch
-- ONLY 'PR-%' change_ids here. Empty p_open_change_ids means NO PR is open → release ALL in-flight PR-claims at
-- the coordinate (BR-claims still untouched). Same identity model (account from the session; content-free).
CREATE OR REPLACE FUNCTION core.reconcile_repo_claims_with_authority(p_repo text, p_branch text, p_open_change_ids text[])
    RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path TO 'core','pg_catalog' AS $$
DECLARE v_agent text; v_account text; v_repo text; v_branch text; v_open text[];
        v_freed_lanes text[]; v_promoted text[] := ARRAY[]::text[]; v_changes text[]; v_path text; v_prom text;
BEGIN
  v_repo := left(COALESCE(p_repo,''),512);
  v_branch := left(COALESCE(NULLIF(p_branch,''),'main'),512);
  -- the live open-change set, normalized exactly as _place_claim derives change_id (left-200), deduped.
  v_open := ARRAY(SELECT DISTINCT left(NULLIF(btrim(c),''),200) FROM unnest(COALESCE(p_open_change_ids,'{}'::text[])) AS c
                   WHERE c IS NOT NULL AND btrim(c)<>'');
  SELECT agent, account INTO v_agent, v_account FROM core.establish_session_write_context() AS c(agent,account);

  -- RELEASE in-flight claims whose change_id is NOT in the open set; collect freed lanes + the released changes.
  PERFORM core.mark_governed_write('claim');
  WITH freed AS (
    UPDATE core.claim SET claim_state='released', released_at=now()
     WHERE account_id=v_account AND repo=v_repo AND branch=v_branch
       AND claim_state IN ('active','waiting')
       AND change_id LIKE 'PR-%'                     -- PR-claims only: never reclaim a pre-PR 'BR-<branch>' reservation
       AND NOT (change_id = ANY(v_open))            -- v_open empty → ALL in-flight PR-claims released
    RETURNING target_path, change_id)
  SELECT array_agg(DISTINCT target_path), array_agg(DISTINCT change_id) INTO v_freed_lanes, v_changes FROM freed;
  IF v_freed_lanes IS NOT NULL THEN
    FOREACH v_path IN ARRAY v_freed_lanes LOOP
      v_prom := core._promote_next_waiter(v_account, v_repo, v_branch, v_path);
      IF v_prom IS NOT NULL THEN v_promoted := array_append(v_promoted, core.agent_name(v_prom)); END IF;
    END LOOP;
  END IF;

  RETURN jsonb_build_object('ok',true,'reconciled',true,'repo',v_repo,'branch',v_branch,
    'open', COALESCE(to_jsonb(v_open), '[]'::jsonb),
    'released_changes', COALESCE(to_jsonb(v_changes), '[]'::jsonb),
    'released_lanes', COALESCE(to_jsonb(v_freed_lanes), '[]'::jsonb),
    'promoted', COALESCE(to_jsonb(v_promoted), '[]'::jsonb));
END $$;
ALTER FUNCTION core.reconcile_repo_claims_with_authority(text,text,text[]) OWNER TO veripsa_migrator;

-- reconcile_repo_branch_claims_with_authority: BOOT BACKSTOP for a missed/legacy branch-delete delivery.
-- The App supplies the COMPLETE live GitHub branch set for one repo, normalized to the SAME bounded
-- 'BR-<branch>' change ids reserve_branch_lanes uses. Release every active/waiting BR-claim absent from that
-- authoritative set and promote the next waiter on each freed path. This is deliberately separate from
-- reconcile_repo_claims_with_authority: open PR truth governs PR-* lanes, live branch truth governs BR-* lanes.
--
-- CALLER COMPLETENESS CONTRACT: the App MUST skip this function when GitHub branch pagination is truncated,
-- malformed, or errors. The gate cannot distinguish a complete empty array (an empty repo: release all stale
-- BR lanes) from a partial empty read, so the App proves completeness before calling. Race safety comes from the
-- same per-(account,repo) advisory lock used by boot/live processing: a concurrent push webhook reserves after
-- this transaction, while a branch already visible in the inventory is retained. Long branch ids are safe:
-- the App first maps every live ref through the runtime's exact 182-character branch-change-id cap; this gate
-- then enforces the DB's general 200-character change-id ceiling, so any live preimage keeps the capped lane.
-- Identity comes only from the pinned App session; buyers cannot call this delegation release surface.
CREATE OR REPLACE FUNCTION core.reconcile_repo_branch_claims_with_authority(
    p_repo text, p_branch text, p_live_change_ids text[])
    RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path TO 'core','pg_catalog' AS $$
DECLARE v_agent text; v_account text; v_repo text; v_branch text; v_live text[];
        v_freed_lanes text[]; v_promoted text[] := ARRAY[]::text[]; v_changes text[]; v_path text; v_prom text;
BEGIN
  v_repo := left(COALESCE(p_repo,''),512);
  v_branch := left(COALESCE(NULLIF(p_branch,''),'main'),512);
  v_live := ARRAY(
    SELECT DISTINCT left(NULLIF(btrim(c),''),200)
      FROM unnest(COALESCE(p_live_change_ids,'{}'::text[])) AS c
     WHERE c IS NOT NULL AND btrim(c) LIKE 'BR-%'
     ORDER BY 1
  );
  SELECT agent, account INTO v_agent, v_account
    FROM core.establish_session_write_context() AS c(agent,account);

  PERFORM core.mark_governed_write('claim');
  WITH freed AS (
    UPDATE core.claim SET claim_state='released', released_at=now()
     WHERE account_id=v_account AND repo=v_repo AND branch=v_branch
       AND claim_state IN ('active','waiting')
       AND change_id LIKE 'BR-%'
       AND NOT (change_id = ANY(v_live))
    RETURNING target_path, change_id)
  SELECT array_agg(DISTINCT target_path ORDER BY target_path),
         array_agg(DISTINCT change_id ORDER BY change_id)
    INTO v_freed_lanes, v_changes FROM freed;
  IF v_freed_lanes IS NOT NULL THEN
    FOREACH v_path IN ARRAY v_freed_lanes LOOP
      v_prom := core._promote_next_waiter(v_account, v_repo, v_branch, v_path);
      IF v_prom IS NOT NULL THEN
        v_promoted := array_append(v_promoted, core.agent_name(v_prom));
      END IF;
    END LOOP;
  END IF;

  RETURN jsonb_build_object(
    'ok', true, 'reconciled', true, 'repo', v_repo, 'branch', v_branch,
    'live_count', COALESCE(array_length(v_live,1),0),
    'released_changes', COALESCE(to_jsonb(v_changes), '[]'::jsonb),
    'released_lanes', COALESCE(to_jsonb(v_freed_lanes), '[]'::jsonb),
    'promoted', COALESCE(to_jsonb(v_promoted), '[]'::jsonb));
END $$;
ALTER FUNCTION core.reconcile_repo_branch_claims_with_authority(text,text,text[]) OWNER TO veripsa_migrator;

-- record_landing_with_authority: a change reached main (via PR-merge OR direct push) — record ONE 'landed'
-- event per changed path. This is the UNIFIED LANDING MODEL: every change to main is measured the same way
-- regardless of how it arrived (PR or direct push). Capped at 200 paths per landing (bounded ledger growth).
-- Author is attributed as 'GH-<login>' (same pattern as act_for_claim); if absent, the connecting agent.
-- SECURITY DEFINER; identity from the connection role (never passed in). Append-only (event ledger).
-- The trailing p_author_is_bot (DEFAULT false, back-compatible) stamps the author's agent HUMAN (a seat) vs AI
-- (free) — same seat-metering signal as act_for_claim. DROP the old 5-arg signature first (CREATE OR REPLACE
-- cannot add a param without leaving the prior overload behind); every existing 5-arg caller still resolves.
DROP FUNCTION IF EXISTS core.record_landing_with_authority(text,text,text,text[],text);
CREATE OR REPLACE FUNCTION core.record_landing_with_authority(p_repo text, p_branch text, p_sha text, p_paths text[], p_author text DEFAULT NULL, p_author_is_bot boolean DEFAULT false)
    RETURNS integer LANGUAGE plpgsql SECURITY DEFINER SET search_path TO 'core','pg_catalog' AS $$
DECLARE v_app_agent text; v_account text; v_agent text; v_login text; v_sha text; v_path text;
        v_id text; v_n integer := 0; v_paths_capped text[]; v_kind text;
BEGIN
  v_sha := NULLIF(btrim(COALESCE(p_sha,'')),'');
  IF v_sha IS NULL OR length(v_sha) > 64 OR v_sha !~ '^[0-9a-fA-F]+$' THEN RETURN 0; END IF;  -- skip non-hex (e.g. delete push)
  IF p_paths IS NULL OR array_length(p_paths, 1) IS NULL THEN RETURN 0; END IF;
  SELECT agent, account INTO v_app_agent, v_account FROM core.establish_session_write_context() AS c(agent,account);
  -- THE WALL: over the free line → record NO landing rows. Returns 0 = "recorded nothing" (this fn's existing
  -- sentinel for skipped/no-op work; the int return can't carry the structured jsonb). Advisory + never raises;
  -- landing telemetry is best-effort, so it is silently dropped over quota — the App's user-facing quota note is
  -- posted on the PUSH path (ingest/record_push above), where the structured quota_exceeded result is detectable.
  IF core._account_over_quota(v_account) IS NOT NULL THEN RETURN 0; END IF;
  -- attribute to the real author (GH-login) when given; fall back to the connecting agent.
  IF p_author IS NOT NULL AND btrim(p_author) <> '' THEN
    v_login := left(regexp_replace(p_author, '[^A-Za-z0-9_.\-]', '', 'g'), 64);
    IF v_login <> '' THEN
      v_agent := 'GH-'||v_login;
      -- SEAT METERING: a human author IS a seat; a Bot is free. The login was sanitized ('[bot]' stripped) so the
      -- bot signal MUST arrive from the webhook (the merge: pull_request.user.type; a direct push: sender.type).
      v_kind := CASE WHEN COALESCE(p_author_is_bot, false) THEN 'ai' ELSE 'human' END;
      -- provision the author agent on first sight (same pattern as act_for_claim); SELF-HEAL an author stored under
      -- the OLD default ('ai') to the right kind on its next landing — ONLY for a 'GH-' author row (never AG-*).
      PERFORM core.mark_governed_write('agent');
      INSERT INTO core.agent(agent_id, account_id, display_name, agent_kind) VALUES (v_agent, v_account, v_login, v_kind)
      ON CONFLICT (agent_id) DO NOTHING;
      -- SELF-HEAL (RLS-SAFE, see act_for_claim @audit-r2): account-scoped UPDATE, never `ON CONFLICT DO UPDATE` —
      -- a GH login shared across orgs (global agent PK) would make the 2nd tenant's DO UPDATE raise RLS 42501 = a
      -- poison-pill. account_id=v_account = a cross-tenant same-login row is a clean 0-row no-op.
      UPDATE core.agent SET agent_kind = v_kind
       WHERE agent_id = v_agent AND account_id = v_account AND agent_id LIKE 'GH-%' AND agent_kind <> v_kind;
    ELSE
      v_agent := v_app_agent;
    END IF;
  ELSE
    v_agent := v_app_agent;
  END IF;
  -- cap at 200 paths per landing (stay bounded; a monorepo commit touching thousands is unhelpful noise)
  v_paths_capped := ARRAY(SELECT unnest(p_paths) LIMIT 200);
  FOREACH v_path IN ARRAY v_paths_capped LOOP
    IF v_path IS NULL OR btrim(v_path) = '' OR length(v_path) > 1024 THEN CONTINUE; END IF;
    -- IDEMPOTENT id: a landing is uniquely (account,repo,branch,sha,path). A DETERMINISTIC event_id +
    -- ON CONFLICT DO NOTHING means GitHub's at-least-once webhook REDELIVERY (and a real double-push of the
    -- same sha) records the landing exactly ONCE — never a duplicate 'landed' row to corrupt the records
    -- ledger or inflate counts. Append-only-safe: it REFUSES the duplicate, never UPDATEs history.
    v_id := 'EV-LAND-'||substr(md5(v_account||'|'||left(COALESCE(p_repo,''),512)||'|'||left(COALESCE(p_branch,''),512)||'|'||v_sha||'|'||v_path),1,24);
    PERFORM core.mark_governed_write('event');
    INSERT INTO core.event(event_id, account_id, kind, agent_id, repo, branch, path, commit_sha)
    VALUES (v_id, v_account, 'landed', v_agent,
            left(COALESCE(p_repo,''),512), left(COALESCE(p_branch,''),512), v_path, v_sha)
    ON CONFLICT (account_id, event_id) DO NOTHING;
    IF FOUND THEN v_n := v_n + 1; END IF;        -- count only the rows this delivery actually recorded
  END LOOP;
  RETURN v_n;
END $$;
ALTER FUNCTION core.record_landing_with_authority(text,text,text,text[],text,boolean) OWNER TO veripsa_migrator;

-- collisions_on_main: from 'landed' events within p_window, find REAL COLLISIONS that OCCURRED: pairs of
-- landings on the SAME path by DIFFERENT agents. Same-path, different authors, within the window = a real
-- collision on main (two people changed the same file — one landed over the other's change). Content-free,
-- account-scoped, STABLE. p_branch and p_repo are advisory filters ('' = all repos/branches).
CREATE OR REPLACE FUNCTION core.collisions_on_main(p_repo text DEFAULT '', p_branch text DEFAULT 'main', p_window interval DEFAULT '14 days')
    RETURNS jsonb LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path TO 'core','pg_catalog' AS $$
DECLARE v_account text; v_result jsonb;
BEGIN
  SELECT account INTO v_account FROM core.resolve_session_identity() AS r(agent,account);
  PERFORM set_config('core.current_account', v_account, true);
  SELECT jsonb_build_object(
    'repo', p_repo, 'branch', p_branch, 'window', p_window::text,
    'collisions_count', (
      SELECT count(DISTINCT e.path)::int FROM core.event e
       WHERE e.account_id=v_account AND e.kind='landed'
         AND e.occurred_at > now()-p_window
         AND (p_repo = '' OR e.repo=p_repo)
         AND (p_branch = '' OR e.branch=p_branch)
         AND EXISTS (
           SELECT 1 FROM core.event e2
            WHERE e2.account_id=v_account AND e2.kind='landed' AND e2.path=e.path
              AND e2.agent_id <> e.agent_id
              AND e2.occurred_at > now()-p_window
              AND (p_repo = '' OR e2.repo=p_repo)
              AND (p_branch = '' OR e2.branch=p_branch))),
    'recent', COALESCE((
      SELECT jsonb_agg(x.obj ORDER BY x.latest DESC) FROM (
        SELECT e.path,
               jsonb_build_object(
                 'path', e.path, 'repo', e.repo, 'branch', e.branch,
                 'agents', (SELECT jsonb_agg(DISTINCT e3.agent_id ORDER BY e3.agent_id)
                              FROM core.event e3
                             WHERE e3.account_id=v_account AND e3.kind='landed' AND e3.path=e.path
                               AND e3.occurred_at > now()-p_window
                               AND (p_repo = '' OR e3.repo=p_repo)
                               AND (p_branch = '' OR e3.branch=p_branch)),
                 'at', max(e.occurred_at)) AS obj,
               max(e.occurred_at) AS latest
          FROM core.event e
         WHERE e.account_id=v_account AND e.kind='landed'
           AND e.occurred_at > now()-p_window
           AND (p_repo = '' OR e.repo=p_repo)
           AND (p_branch = '' OR e.branch=p_branch)
           AND EXISTS (
             SELECT 1 FROM core.event e2
              WHERE e2.account_id=v_account AND e2.kind='landed' AND e2.path=e.path
                AND e2.agent_id <> e.agent_id
                AND e2.occurred_at > now()-p_window
                AND (p_repo = '' OR e2.repo=p_repo)
                AND (p_branch = '' OR e2.branch=p_branch))
         GROUP BY e.path, e.repo, e.branch
         ORDER BY latest DESC LIMIT 20) x), '[]'::jsonb)
  ) INTO v_result;
  RETURN v_result;
END $$;
ALTER FUNCTION core.collisions_on_main(text,text,interval) OWNER TO veripsa_migrator;

-- record_pr_failing_with_authority: a PR is RED / STUCK — its CI checks failed, or it cannot merge (conflict).
-- Nobody watches the GitHub inbox, so an ignored failing/conflicting PR rots silently; this records the fact so
-- stuck_prs_surface can TELL a human/AI "this PR is blocked, look at it". It is the complement to
-- stalled_work_surface (which catches the ABANDONED/STARVED in-flight lane); this catches the IN-FLIGHT-BUT-RED
-- PR. 'pr_failing' event KIND (a new VALUE in the no-乱立 ledger, not a new table). Content-free: repo + branch +
-- the PR's change ref (PR-<n>, an opaque id — NOT code) + the head commit_sha + a SHORT reason TOKEN from a
-- bounded allow-list ('ci_failed' | 'conflict' | 'failing'), never a log/diagnostic/code body. NOTIFY-ONLY —
-- recording this NEVER blocks the PR (the product never blocks); it only makes the red state visible.
--   * p_change_id : the PR/lane bundle id ('PR-<n>'), the same content-free change ref the lock uses. Stored in
--                   `path` (the event's per-change ref slot, exactly as 'landed' carries a content-free ref there;
--                    it is NOT a filesystem path here — it is the opaque change id, so no code leaks).
--   * p_reason    : normalized to the allow-list; anything else collapses to the generic 'failing' (so a future
--                   caller can pass a new GitHub conclusion string without ever leaking free text into the ledger).
-- IDEMPOTENT id keyed by (account,repo,branch,pr,sha,reason): GitHub redelivers check webhooks at-least-once, and
-- a check_suite reruns at the SAME head sha — a deterministic id + ON CONFLICT DO NOTHING records the red fact
-- ONCE per (sha,reason), so the surface never shows the same failing PR ten times. Append-only. SECURITY DEFINER;
-- identity from the connection role (the App service identity; account already pinned by enter_installation).
CREATE OR REPLACE FUNCTION core.record_pr_failing_with_authority(p_change_id text, p_repo text, p_branch text, p_commit_sha text DEFAULT NULL, p_reason text DEFAULT 'failing')
    RETURNS text LANGUAGE plpgsql SECURITY DEFINER SET search_path TO 'core','pg_catalog' AS $$
DECLARE v_agent text; v_account text; v_change text; v_repo text; v_branch text; v_sha text; v_reason text; v_id text;
BEGIN
  v_change := left(NULLIF(btrim(COALESCE(p_change_id,'')),''),200);
  IF v_change IS NULL THEN RAISE EXCEPTION 'record_pr_failing needs a change_id (the PR ref)' USING ERRCODE='23514'; END IF;
  v_repo := left(COALESCE(p_repo,''),512); v_branch := left(COALESCE(p_branch,''),512);
  -- commit_sha is OPTIONAL but, if given, must be hex (the event CHECK enforces it too); '' / NULL → no sha.
  v_sha := NULLIF(btrim(COALESCE(p_commit_sha,'')),'');
  IF v_sha IS NOT NULL AND (length(v_sha)>64 OR v_sha !~ '^[0-9a-fA-F]+$') THEN RAISE EXCEPTION 'commit_sha out of bounds' USING ERRCODE='23514'; END IF;
  -- BOUNDED reason: only known content-free tokens; anything else → 'failing' (never free text in the ledger).
  v_reason := lower(btrim(COALESCE(p_reason,'')));
  IF v_reason NOT IN ('ci_failed','conflict','failing') THEN v_reason := 'failing'; END IF;
  SELECT agent, account INTO v_agent, v_account FROM core.establish_session_write_context() AS c(agent,account);
  -- THE LEDGER WALL: over the events cap → record nothing (append-only event ledger growth past the #95 wall).
  IF core._ledger_write_blocked(v_account) THEN RETURN NULL; END IF;
  v_id := 'EV-PRFAIL-'||substr(md5(v_account||'|'||v_repo||'|'||v_branch||'|'||v_change||'|'||COALESCE(v_sha,'')||'|'||v_reason),1,22);
  PERFORM core.mark_governed_write('event');
  INSERT INTO core.event(event_id, account_id, kind, agent_id, repo, branch, path, commit_sha, detail)
  VALUES (v_id, v_account, 'pr_failing', v_agent, v_repo, v_branch, v_change, v_sha, v_reason)
  ON CONFLICT (account_id, event_id) DO NOTHING;
  RETURN v_id;
END $$;
ALTER FUNCTION core.record_pr_failing_with_authority(text,text,text,text,text) OWNER TO veripsa_migrator;

-- ── GRANTs: the gate fns are the buyer's write path (veripsa_writer). provision_seat is admin-only. ───
REVOKE EXECUTE ON FUNCTION core.record_push_with_authority(text,text,text,text) FROM PUBLIC, veripsa_writer;
REVOKE EXECUTE ON FUNCTION core.land_on_main_with_authority(text,text,text,text) FROM PUBLIC, veripsa_writer;
REVOKE EXECUTE ON FUNCTION core.land_change_on_main_with_authority(text,text,text,text,text) FROM PUBLIC, veripsa_writer;
REVOKE EXECUTE ON FUNCTION core.release_change_on_main_with_authority(text,text,text) FROM PUBLIC, veripsa_writer;
REVOKE EXECUTE ON FUNCTION core.reconcile_change_claims_with_authority(text,text,text,text[]) FROM PUBLIC, veripsa_writer;
REVOKE EXECUTE ON FUNCTION core.reconcile_repo_claims_with_authority(text,text,text[]) FROM PUBLIC, veripsa_writer;
REVOKE EXECUTE ON FUNCTION core.reconcile_repo_branch_claims_with_authority(text,text,text[]) FROM PUBLIC, veripsa_writer;
REVOKE EXECUTE ON FUNCTION core.record_pr_failing_with_authority(text,text,text,text,text) FROM PUBLIC, veripsa_writer;  -- App-delegation-only (the App observes CI server-side); a buyer writer must not forge a 'PR is red' fact
REVOKE EXECUTE ON FUNCTION core.record_compat_finding_with_authority(text,text,text,text,text,text,text) FROM PUBLIC, veripsa_writer;  -- App-delegation-only (the App runs the compat analysis server-side); a buyer writer must not forge a compatibility fact
REVOKE EXECUTE ON FUNCTION core.record_compat_finding_with_authority(text,text,text,text,text,text,text,text,text) FROM PUBLIC, veripsa_writer;  -- S3a 9-arg overload (classification + detector stamps): the SAME App-delegation-only wall
-- Internal/admin SECURITY DEFINER fns that take p_account DIRECTLY and pin it (current_account) before
-- writing: callable ONLY via the gate's own internal calls + the *_with_authority wrappers (which run as
-- the migrator owner and derive the account from the connection identity, never from caller input). The
-- PUBLIC default would let any tenant call them directly with a VICTIM account and bypass RLS — so revoke.
REVOKE EXECUTE ON FUNCTION core._promote_next_waiter(text,text,text,text) FROM PUBLIC;       -- lane-promote helper (admin-internal)
REVOKE EXECUTE ON FUNCTION core.provision_seat(text,text,text,text,text,text,text) FROM PUBLIC;  -- seat/account admin
REVOKE EXECUTE ON FUNCTION core.declare_claim_with_authority(text,text,text,text,jsonb,text) FROM PUBLIC;  -- strip the PUBLIC default; only the granted buyer writer below
GRANT EXECUTE ON FUNCTION core.declare_claim_with_authority(text,text,text,text,jsonb,text) TO veripsa_writer;  -- trailing p_ranges (finer collision) + p_base_hash (freshness-gated demotion); old 4-arg/5-arg overloads dropped above
-- DELEGATION GATE — App-only. Postgres grants EXECUTE to PUBLIC by DEFAULT on every CREATE FUNCTION, so the
-- GRANT-to-app below is NOT enough: without this REVOKE the PUBLIC default leaves act_for callable by every role
-- (incl. a buyer veripsa_writer seat), which would let a non-App seat ATTRIBUTE A CLAIM TO AN ARBITRARY AUTHOR
-- (act_for stamps the claim's actor as 'GH-<p_author>' from caller input) — actor forgery on the coordination
-- ledger (it would name a forged GitHub login as the one editing a lane, which then drives collision/notification
-- surfaces + the PR comment). Mirror the App-delegation REVOKE block above (record_push / record_pr_failing / …).
REVOKE EXECUTE ON FUNCTION core.act_for_claim_with_authority(text,text,text,text,text,jsonb,text,boolean) FROM PUBLIC, veripsa_writer;
GRANT EXECUTE ON FUNCTION core.act_for_claim_with_authority(text,text,text,text,text,jsonb,text,boolean) TO veripsa_app;  -- delegation: only the App service identity may act-for-author (_place_claim stays internal/ungranted)
REVOKE EXECUTE ON FUNCTION core.heartbeat_claim_with_authority(text) FROM PUBLIC;  -- strip the PUBLIC default; only the granted buyer writer below
GRANT EXECUTE ON FUNCTION core.heartbeat_claim_with_authority(text) TO veripsa_writer;
REVOKE EXECUTE ON FUNCTION core.release_claim_with_authority(text) FROM PUBLIC;  -- strip the PUBLIC default; only the granted buyer writer below
GRANT EXECUTE ON FUNCTION core.release_claim_with_authority(text) TO veripsa_writer;
REVOKE EXECUTE ON FUNCTION core.break_lane_with_authority(text,text,text) FROM PUBLIC;  -- strip the PUBLIC default; only the granted writer/steward below
GRANT EXECUTE ON FUNCTION core.break_lane_with_authority(text,text,text) TO veripsa_writer, veripsa_demo_steward;
REVOKE EXECUTE ON FUNCTION core.expire_stale_claims() FROM PUBLIC, veripsa_writer;   -- internal-only (cross-tenant DoS fix): it TRUSTS current_account (so the App can sweep per-installation), so a tenant writer could spoof current_account=VICTIM and expire ANOTHER account's claims. Only the SECURITY DEFINER surfaces/gate (running as migrator, account already re-pinned) may sweep.
REVOKE EXECUTE ON FUNCTION core.ingest_graph_with_authority(jsonb,text,text,text,timestamptz) FROM PUBLIC;  -- strip the PUBLIC default; the durable cutover marker below decides whether the legacy writer grant remains
REVOKE EXECUTE ON FUNCTION core.patch_graph_with_authority(jsonb,text,text,text[],text[],text,timestamptz) FROM PUBLIC;
-- Production contract cutover revokes these generic/versionless writers after
-- the lease-fenced convergence worker is proven. CREATE OR REPLACE preserves
-- each function's pg_description marker, so a later schema replay must not
-- transiently re-grant inherited veripsa_app access. Fresh/direct test schemas
-- have no marker and retain the historical writer surface. A partial/foreign
-- marker is never guessed compatible.
WITH markers AS (
  SELECT
    obj_description(to_regprocedure(
      'core.ingest_graph_with_authority(jsonb,text,text,text,timestamptz)'),
      'pg_proc') AS graph_full,
    obj_description(to_regprocedure(
      'core.patch_graph_with_authority(jsonb,text,text,text[],text[],text,timestamptz)'),
      'pg_proc') AS graph_patch
)
SELECT
  COALESCE((
    graph_full='veripsa-graph-direct/v1/fenced'
    AND graph_patch='veripsa-graph-direct/v1/fenced'
  ),false) AS veripsa_graph_direct_fenced,
  NOT (
    (graph_full IS NULL AND graph_patch IS NULL)
    OR
    (
      graph_full='veripsa-graph-direct/v1/fenced'
      AND graph_patch='veripsa-graph-direct/v1/fenced'
    )
  ) AS veripsa_graph_direct_marker_unknown
FROM markers
\gset
\if :veripsa_graph_direct_marker_unknown
SELECT
  'unknown or partial generic graph-writer cutover marker; refusing publication'
  ::integer;
\elif :veripsa_graph_direct_fenced
REVOKE EXECUTE ON FUNCTION
  core.ingest_graph_with_authority(jsonb,text,text,text,timestamptz)
  FROM veripsa_writer,veripsa_app;
REVOKE EXECUTE ON FUNCTION
  core.patch_graph_with_authority(
    jsonb,text,text,text[],text[],text,timestamptz)
  FROM veripsa_writer,veripsa_app;
\else
GRANT EXECUTE ON FUNCTION
  core.ingest_graph_with_authority(jsonb,text,text,text,timestamptz)
  TO veripsa_writer;
GRANT EXECUTE ON FUNCTION
  core.patch_graph_with_authority(
    jsonb,text,text,text[],text[],text,timestamptz)
  TO veripsa_writer;
\endif
GRANT EXECUTE ON FUNCTION core.capture_repository_graph_generation_with_authority(text,text) TO veripsa_app;
GRANT EXECUTE ON FUNCTION core.ingest_graph_with_authority_for_repository_generation(jsonb,text,text,text,timestamptz,text,jsonb) TO veripsa_app;
GRANT EXECUTE ON FUNCTION core.patch_graph_with_authority_for_repository_generation(jsonb,text,text,text[],text[],text,timestamptz,text,jsonb) TO veripsa_app;
REVOKE EXECUTE ON FUNCTION core._resource_canonical_key(text,text,text,text) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION core._semantic_ref_key(text) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION core._safe_ref_display(text) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION core._resource_semantic_key(text,text,text,text) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION core._node_semantic_key(text,text,text,text,text) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION core._coordinate_graph_hash(text,text,text) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION core._validated_graph_observability(jsonb,jsonb,text) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION core._assert_unique_graph_identities(jsonb,jsonb) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION core.current_extractor_version() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION core.current_extractor_version() TO veripsa_writer, veripsa_reader;
REVOKE EXECUTE ON FUNCTION core.current_semantic_ref_version() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION core.current_semantic_ref_version() TO veripsa_writer, veripsa_reader;
REVOKE EXECUTE ON FUNCTION core.graph_schema_inventory() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION core.graph_schema_inventory() TO veripsa_writer, veripsa_reader;
GRANT EXECUTE ON FUNCTION core.coordinate_file_paths(text,text) TO veripsa_writer, veripsa_reader;
REVOKE EXECUTE ON FUNCTION core.coordinate_resource_catalog(text,text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION core.coordinate_resource_catalog(text,text) TO veripsa_writer, veripsa_reader;
GRANT EXECUTE ON FUNCTION core.coordinate_graph_sha(text,text) TO veripsa_writer, veripsa_reader;
GRANT EXECUTE ON FUNCTION core.coordinate_inert_imports(text,text) TO veripsa_writer, veripsa_reader;
REVOKE EXECUTE ON FUNCTION core.record_collision_with_authority(text,text,text,text,text) FROM PUBLIC;  -- strip the PUBLIC default; only the granted buyer writer below
GRANT EXECUTE ON FUNCTION core.record_collision_with_authority(text,text,text,text,text) TO veripsa_writer;
GRANT EXECUTE ON FUNCTION core.record_push_with_authority(text,text,text,text) TO veripsa_app;
GRANT EXECUTE ON FUNCTION core.record_compat_finding_with_authority(text,text,text,text,text,text,text) TO veripsa_app;  -- compat lane PR-2: the App's shadow-finding writer (inert until the analysis wiring lands)
GRANT EXECUTE ON FUNCTION core.record_compat_finding_with_authority(text,text,text,text,text,text,text,text,text) TO veripsa_app;  -- S3a: the classification+detector-stamping writer the live shadow analysis calls
GRANT EXECUTE ON FUNCTION core.land_on_main_with_authority(text,text,text,text) TO veripsa_app;
GRANT EXECUTE ON FUNCTION core.land_change_on_main_with_authority(text,text,text,text,text) TO veripsa_app;
GRANT EXECUTE ON FUNCTION core.release_change_on_main_with_authority(text,text,text) TO veripsa_app;
GRANT EXECUTE ON FUNCTION core.reconcile_change_claims_with_authority(text,text,text,text[]) TO veripsa_app;  -- synchronize backstop: converge a PR's lanes to its CURRENT file set (closes stale-claim leak + base retarget)
GRANT EXECUTE ON FUNCTION core.reconcile_repo_claims_with_authority(text,text,text[]) TO veripsa_app;  -- backfill backstop: release lanes of changes no longer in the live open-PR set (self-heal a dropped 'closed')
GRANT EXECUTE ON FUNCTION core.reconcile_repo_branch_claims_with_authority(text,text,text[]) TO veripsa_app;  -- boot backstop: release BR-* lanes absent from a complete live GitHub branch inventory
REVOKE EXECUTE ON FUNCTION core.record_landing_with_authority(text,text,text,text[],text,boolean) FROM PUBLIC;  -- strip the PUBLIC default; only the granted App identity below
GRANT EXECUTE ON FUNCTION core.record_landing_with_authority(text,text,text,text[],text,boolean) TO veripsa_app;
GRANT EXECUTE ON FUNCTION core.record_pr_failing_with_authority(text,text,text,text,text) TO veripsa_app;  -- a PR is RED/STUCK (CI failed / merge conflict) → recorded so stuck_prs_surface can tell a human nobody is watching the inbox
GRANT EXECUTE ON FUNCTION core.collisions_on_main(text,text,interval) TO veripsa_reader, veripsa_writer, veripsa_demo_steward, veripsa_app;
-- LEAST-PRIVILEGE (audit iter-5 P3): strip the Postgres CREATE-FUNCTION PUBLIC-EXECUTE default on the identity-
-- resolution surfaces so a non-tenant role (veripsa_billing / example_platform_reader) cannot reach them — the
-- explicit GRANTs below name exactly the tenant roles that need them (the App inherits veripsa_writer). Function-
-- only DDL → contention-free (no table lock). resolve_session_identity is a READ (writer/reader/steward);
-- establish_session_write_context arms a write context (writer/App only).
REVOKE EXECUTE ON FUNCTION core.resolve_session_identity(OUT text, OUT text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION core.resolve_session_identity(OUT text, OUT text) TO veripsa_writer, veripsa_reader, veripsa_demo_steward;
REVOKE EXECUTE ON FUNCTION core.establish_session_write_context(OUT text, OUT text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION core.establish_session_write_context(OUT text, OUT text) TO veripsa_writer;

-- ============================================================================================
