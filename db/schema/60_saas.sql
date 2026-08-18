-- PHASE 4a — SaaS plumbing (the door + auth + owner policy). MUTABLE config (no append-only); forgery +
-- RLS. provider is OPEN (bounded text, not a frozen enum) so a new connector needs no schema surgery.
-- ============================================================================================

-- store_connection: an ATTACHED edge (the door/relay target). The user attaches a folder/repo; Veripsa
-- holds only the connection identity + the attach scope (content-free) — never the store's contents.
CREATE TABLE IF NOT EXISTS core.store_connection (
    connection_id text NOT NULL,
    account_id text NOT NULL,
    provider text NOT NULL,                 -- github | gdrive | s3 | sharepoint | local (OPEN — data)
    target text NOT NULL,                   -- the repo/folder/bucket identity (content-free)
    attach_prefix text DEFAULT '' NOT NULL, -- the attached scope within it
    connection_state text DEFAULT 'active' NOT NULL,
    connected_at timestamptz DEFAULT now() NOT NULL,
    CONSTRAINT store_connection_pkey PRIMARY KEY (account_id, connection_id),
    CONSTRAINT store_conn_provider_len CHECK (length(provider) BETWEEN 1 AND 64),
    CONSTRAINT store_conn_target_len CHECK (length(target) <= 512),
    CONSTRAINT store_conn_prefix_len CHECK (length(attach_prefix) <= 1024),
    CONSTRAINT store_conn_state_check CHECK (connection_state = ANY (ARRAY['active','removed']))
);

-- (retired) mcp_token: the MCP-era per-agent token path. Veripsa is a GitHub App — the App authenticates
-- as ONE service identity (veripsa_app) and writes on behalf of PR authors (delegation); agents never hold
-- a per-seat token, and no resolver ever read these hashes. Removed with the MCP transport. (chore/retire-mcp-token)

-- policy: small, SPECIFIC owner keys (external_share, scope_checkpoint, …). Not a governance taxonomy.
CREATE TABLE IF NOT EXISTS core.policy (
    account_id text NOT NULL,
    policy_key text NOT NULL,
    policy_value text NOT NULL,
    set_at timestamptz DEFAULT now() NOT NULL,
    CONSTRAINT policy_pkey PRIMARY KEY (account_id, policy_key),
    CONSTRAINT policy_key_len CHECK (length(policy_key) BETWEEN 1 AND 64),
    CONSTRAINT policy_value_len CHECK (length(policy_value) <= 200)
);

-- moat pattern (FORCE RLS + forgery) on the SaaS config tables. NOT append-only (mutable config).
DO $$
DECLARE t text;
BEGIN
  FOREACH t IN ARRAY ARRAY['store_connection','policy'] LOOP
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
    IF NOT EXISTS (
      SELECT 1 FROM pg_policy p
       WHERE p.polrelid = format('core.%I', t)::regclass
         AND p.polname = 'tenant_isolation'
    ) THEN
      EXECUTE format('CREATE POLICY tenant_isolation ON core.%I USING (account_id = current_setting(''core.current_account'', true)) WITH CHECK (account_id = current_setting(''core.current_account'', true))', t);
    END IF;
    IF NOT EXISTS (
      SELECT 1 FROM pg_trigger tr
       WHERE tr.tgrelid = format('core.%I', t)::regclass
         AND tr.tgname = format('trg_governed_%s', t)
         AND NOT tr.tgisinternal
    ) THEN
      EXECUTE format('CREATE TRIGGER trg_governed_%I BEFORE INSERT OR UPDATE ON core.%I FOR EACH ROW EXECUTE FUNCTION core.assert_governed_write()', t, t);
    END IF;
  END LOOP;
END $$;

-- connect_store: attach an edge. set_policy: set an owner key. get_policies: read the owner keys.
CREATE OR REPLACE FUNCTION core.connect_store_with_authority(p_connection_id text, p_provider text, p_target text, p_attach_prefix text DEFAULT '')
    RETURNS text LANGUAGE plpgsql SECURITY DEFINER SET search_path TO 'core','pg_catalog' AS $$
DECLARE v_agent text; v_account text;
BEGIN
  SELECT agent, account INTO v_agent, v_account FROM core.establish_session_write_context() AS c(agent,account);
  PERFORM core.mark_governed_write('store_connection');
  INSERT INTO core.store_connection(connection_id, account_id, provider, target, attach_prefix)
  VALUES (p_connection_id, v_account, left(p_provider,64), left(p_target,512), left(COALESCE(p_attach_prefix,''),1024))
  ON CONFLICT (account_id, connection_id) DO UPDATE
    SET provider=EXCLUDED.provider, target=EXCLUDED.target,
        attach_prefix=EXCLUDED.attach_prefix, connection_state='active', connected_at=now();
  RETURN p_connection_id;
END $$;
ALTER FUNCTION core.connect_store_with_authority(text,text,text,text) OWNER TO veripsa_migrator;

CREATE OR REPLACE FUNCTION core.set_policy_with_authority(p_key text, p_value text)
    RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path TO 'core','pg_catalog' AS $$
DECLARE v_agent text; v_account text;
BEGIN
  SELECT agent, account INTO v_agent, v_account FROM core.establish_session_write_context() AS c(agent,account);
  PERFORM core.mark_governed_write('policy');
  INSERT INTO core.policy(account_id, policy_key, policy_value) VALUES (v_account, left(p_key,64), left(p_value,200))
  ON CONFLICT (account_id, policy_key) DO UPDATE SET policy_value=EXCLUDED.policy_value, set_at=now();
  -- G4: enqueue this tenant's open in-flight PRs for a background re-derive under the new policy, IN THIS SAME
  -- TXN (atomic + rollback-safe: a rolled-back policy write leaves no outbox row). No GitHub call here — the
  -- drainer does the refresh OUTSIDE the txn. A no-op when the account has no live installation (nothing open).
  PERFORM core._enqueue_policy_refresh(v_account);
END $$;
ALTER FUNCTION core.set_policy_with_authority(text,text) OWNER TO veripsa_migrator;

CREATE OR REPLACE FUNCTION core.get_policies() RETURNS jsonb
    LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path TO 'core','pg_catalog' AS $$
DECLARE v_account text;
BEGIN
  SELECT account INTO v_account FROM core.resolve_session_identity() AS r(agent,account);
  PERFORM set_config('core.current_account', v_account, true);
  RETURN COALESCE((SELECT jsonb_object_agg(policy_key, policy_value) FROM core.policy WHERE account_id=v_account), '{}'::jsonb);
END $$;
ALTER FUNCTION core.get_policies() OWNER TO veripsa_migrator;

-- These write-path gate fns are SECURITY DEFINER (run as the migrator owner) and derive agent+account from the
-- CONNECTION identity, never from an arg. Postgres grants EXECUTE to PUBLIC by default on CREATE FUNCTION, so
-- without an explicit REVOKE the GRANTs below are misleading and the fns stay callable by every role (incl.
-- veripsa_reader, a real connecting identity). Same stray-PUBLIC-grant class closed in 30_gate.sql /
-- 40_surfaces.sql / 50_records.sql. get_policies() is an intentionally-broad READ surface, so it is not revoked.
REVOKE EXECUTE ON FUNCTION core.connect_store_with_authority(text,text,text,text) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION core.set_policy_with_authority(text,text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION core.connect_store_with_authority(text,text,text,text) TO veripsa_writer, veripsa_demo_steward;
GRANT EXECUTE ON FUNCTION core.set_policy_with_authority(text,text) TO veripsa_writer, veripsa_demo_steward;
GRANT EXECUTE ON FUNCTION core.get_policies() TO veripsa_reader, veripsa_writer, veripsa_demo_steward;

-- ============================================================================================
