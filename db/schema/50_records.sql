-- PHASE 3 — RECORDS (records-not-correctness). `statement`: a stated meaning, content-free, anchored
-- DOWN to code (about_repo/branch/path), with a DIRECT supersede lineage (re-stating about the same
-- anchor supersedes the prior — the maintained-current property git lacks). NOT a generic atom/relation.
-- ============================================================================================
CREATE TABLE IF NOT EXISTS core.statement (
    statement_id text NOT NULL,
    account_id text NOT NULL,
    agent_id text NOT NULL,
    utterance text NOT NULL,                -- a stated meaning, a content-free one-liner (a label, never a body)
    about_repo text DEFAULT '' NOT NULL,
    about_branch text DEFAULT '' NOT NULL,
    about_path text DEFAULT '' NOT NULL,    -- the vertical anchor down to code ('' = unanchored)
    supersedes text,                        -- the prior statement_id this replaces (direct lineage)
    superseded boolean DEFAULT false NOT NULL,
    stated_at timestamptz DEFAULT now() NOT NULL,
    visibility text DEFAULT 'private' NOT NULL,
    CONSTRAINT statement_pkey PRIMARY KEY (account_id, statement_id),
    CONSTRAINT statement_utterance_len CHECK (length(utterance) BETWEEN 1 AND 500),
    CONSTRAINT statement_repo_len CHECK (length(about_repo) <= 512),
    CONSTRAINT statement_branch_len CHECK (length(about_branch) <= 512),
    CONSTRAINT statement_path_len CHECK (length(about_path) <= 1024),
    CONSTRAINT statement_vis_check CHECK (visibility = ANY (ARRAY['private','team','public']))
);
DO $$
BEGIN
  IF EXISTS (
    SELECT 1
      FROM pg_class c
      JOIN pg_namespace n ON n.oid = c.relnamespace
     WHERE n.nspname = 'core'
       AND c.relname = 'statement'
       AND c.relowner <> 'veripsa_migrator'::regrole
  ) THEN
    ALTER TABLE core.statement OWNER TO veripsa_migrator;
  END IF;
END $$;
CREATE INDEX CONCURRENTLY IF NOT EXISTS statement_current ON core.statement (account_id, about_path) WHERE superseded = false;
CREATE INDEX CONCURRENTLY IF NOT EXISTS statement_by_account ON core.statement (account_id, stated_at DESC);

-- statement immutability: the CONTENT is permanent; only superseded (false→true, never back) and
-- visibility may change. DELETE blocked. (Same named ACCT-DEMO demo bypass.)
CREATE OR REPLACE FUNCTION core.assert_statement_immutable() RETURNS trigger
    LANGUAGE plpgsql SET search_path TO 'core','pg_catalog' AS $$
DECLARE v_old jsonb; v_new jsonb;
BEGIN
  IF current_setting('core.demo_maintenance_token', true) = OLD.account_id AND OLD.account_id = 'ACCT-DEMO' THEN
    RETURN CASE WHEN TG_OP='DELETE' THEN OLD ELSE NEW END;
  END IF;
  -- RIGHT-TO-DELETION (account erasure): a DELETE armed by the gate (mark_account_erasure) for THIS row's OWN
  -- account is the controlled hard-erase of an offboarding tenant — account-pinned + un-forgeable (the same
  -- shape as the event ledger's erasure exception). It can never reach another tenant's statements, so the
  -- records stream stays immutable for every retained tenant. A plain DELETE is still refused below.
  IF TG_OP='DELETE'
     AND current_setting('core.account_erasure_token', true) IS NOT NULL
     AND current_setting('core.account_erasure_token', true) = OLD.account_id THEN
    RETURN OLD;
  END IF;
  IF TG_OP='DELETE' THEN RAISE EXCEPTION 'append-only: a statement is permanent; it cannot be deleted.' USING ERRCODE='42501'; END IF;
  v_old := to_jsonb(OLD) - 'superseded' - 'visibility';
  v_new := to_jsonb(NEW) - 'superseded' - 'visibility';
  IF v_old IS DISTINCT FROM v_new THEN
    RAISE EXCEPTION 'append-only: a statement is permanent; only superseded (once) + visibility may change.' USING ERRCODE='42501';
  END IF;
  IF OLD.superseded = true AND NEW.superseded = false THEN
    RAISE EXCEPTION 'append-only: a statement cannot be un-superseded.' USING ERRCODE='42501';
  END IF;
  RETURN NEW;
END $$;
ALTER FUNCTION core.assert_statement_immutable() OWNER TO veripsa_migrator;

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_class WHERE oid = 'core.statement'::regclass AND relrowsecurity) THEN
    ALTER TABLE core.statement ENABLE ROW LEVEL SECURITY;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_class WHERE oid = 'core.statement'::regclass AND relforcerowsecurity) THEN
    ALTER TABLE ONLY core.statement FORCE ROW LEVEL SECURITY;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_policy WHERE polrelid = 'core.statement'::regclass AND polname = 'tenant_isolation') THEN
    CREATE POLICY tenant_isolation ON core.statement USING (account_id = current_setting('core.current_account', true)) WITH CHECK (account_id = current_setting('core.current_account', true));
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM pg_trigger
     WHERE tgrelid = 'core.statement'::regclass
       AND tgname = 'trg_governed_statement'
       AND NOT tgisinternal
  ) THEN
    CREATE TRIGGER trg_governed_statement BEFORE INSERT OR UPDATE ON core.statement FOR EACH ROW EXECUTE FUNCTION core.assert_governed_write();
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM pg_trigger
     WHERE tgrelid = 'core.statement'::regclass
       AND tgname = 'trg_immutable_statement'
       AND NOT tgisinternal
  ) THEN
    CREATE TRIGGER trg_immutable_statement BEFORE DELETE OR UPDATE ON core.statement FOR EACH ROW EXECUTE FUNCTION core.assert_statement_immutable();
  END IF;
END $$;
-- TRUNCATE-PROOF: the immutable row trigger above is FOR EACH ROW and never fires on TRUNCATE (a statement-level
-- wipe), so a STATEMENT-level BEFORE TRUNCATE guard closes the path that could erase the whole records stream with
-- zero the append-only guarantee. Account-erasure (the only sanctioned hard-delete here) is a row-DELETE via the gate, not a
-- TRUNCATE, so this never blocks a legitimate erase. Same assert_no_truncate guard as the event ledger.
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_trigger
     WHERE tgrelid = 'core.statement'::regclass
       AND tgname = 'trg_no_truncate_statement'
       AND NOT tgisinternal
  ) THEN
    CREATE TRIGGER trg_no_truncate_statement BEFORE TRUNCATE ON core.statement FOR EACH STATEMENT EXECUTE FUNCTION core.assert_no_truncate();
  END IF;
END $$;

-- record_statement_with_authority: state a meaning. Re-stating about the SAME anchor (about_path)
-- supersedes the agent's prior current statement there (maintained-current). Content-free; the gate.
CREATE OR REPLACE FUNCTION core.record_statement_with_authority(p_utterance text, p_about_path text DEFAULT '', p_about_repo text DEFAULT '', p_about_branch text DEFAULT '')
    RETURNS text LANGUAGE plpgsql SECURITY DEFINER SET search_path TO 'core','pg_catalog' AS $$
DECLARE v_agent text; v_account text; v_path text; v_repo text; v_branch text; v_prior text; v_id text;
BEGIN
  IF p_utterance IS NULL OR length(btrim(p_utterance)) < 1 THEN RAISE EXCEPTION 'a statement needs an utterance' USING ERRCODE='23514'; END IF;
  IF length(p_utterance) > 500 THEN RAISE EXCEPTION 'utterance too long (max 500; a label, not a body)' USING ERRCODE='23514'; END IF;
  v_path := left(COALESCE(p_about_path,''),1024); v_repo := left(COALESCE(p_about_repo,''),512); v_branch := left(COALESCE(p_about_branch,''),512);
  SELECT agent, account INTO v_agent, v_account FROM core.establish_session_write_context() AS c(agent,account);
  -- THE LEDGER WALL (#95 follow-up): over the events cap → record nothing (statement_id is md5(random()) = a
  -- fresh append-only row per call, the same unbounded-ledger growth the event cap exists to bound). Advisory:
  -- RETURN NULL (do not even run the supersede UPDATE below) rather than RAISE — over-quota is not a bad request.
  IF core._ledger_write_blocked(v_account) THEN RETURN NULL; END IF;
  IF v_path <> '' THEN
    SELECT statement_id INTO v_prior FROM core.statement
     WHERE account_id=v_account AND agent_id=v_agent AND about_path=v_path AND superseded=false ORDER BY stated_at DESC LIMIT 1;
    IF v_prior IS NOT NULL THEN
      PERFORM core.mark_governed_write('statement');
      UPDATE core.statement SET superseded=true WHERE account_id=v_account AND statement_id=v_prior;
    END IF;
  END IF;
  v_id := 'ST-'||substr(md5(random()::text||clock_timestamp()::text||v_agent),1,16);
  PERFORM core.mark_governed_write('statement');
  INSERT INTO core.statement(statement_id, account_id, agent_id, utterance, about_repo, about_branch, about_path, supersedes)
  VALUES (v_id, v_account, v_agent, p_utterance, v_repo, v_branch, v_path, v_prior);
  RETURN v_id;
END $$;
ALTER FUNCTION core.record_statement_with_authority(text,text,text,text) OWNER TO veripsa_migrator;

-- meaning_surface: the Record — current (non-superseded) statements + the superseded history, each
-- carrying its agent + the anchor down to code. Content-free; tenant-pinned.
CREATE OR REPLACE FUNCTION core.meaning_surface() RETURNS jsonb
    LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path TO 'core','pg_catalog' AS $$
DECLARE v_account text; v_result jsonb;
BEGIN
  SELECT account INTO v_account FROM core.resolve_session_identity() AS r(agent,account);
  PERFORM set_config('core.current_account', v_account, true);
  SELECT jsonb_build_object(
    'total', (SELECT count(*)::int FROM core.statement WHERE account_id=v_account AND superseded=false),
    'current', COALESCE((SELECT jsonb_agg(jsonb_build_object(
        'id', s.statement_id, 'by', core.agent_name(s.agent_id), 'utterance', s.utterance,
        'about_path', s.about_path, 'about_repo', s.about_repo, 'about_branch', s.about_branch,
        'supersedes', s.supersedes, 'at', s.stated_at) ORDER BY s.stated_at DESC)
      FROM core.statement s WHERE s.account_id=v_account AND s.superseded=false), '[]'::jsonb),
    'superseded', COALESCE((SELECT jsonb_agg(jsonb_build_object(
        'id', s.statement_id, 'by', core.agent_name(s.agent_id), 'utterance', s.utterance,
        'about_path', s.about_path, 'at', s.stated_at) ORDER BY s.stated_at DESC)
      FROM core.statement s WHERE s.account_id=v_account AND s.superseded=true), '[]'::jsonb)
  ) INTO v_result;
  RETURN v_result;
END $$;
ALTER FUNCTION core.meaning_surface() OWNER TO veripsa_migrator;

-- record_statement_with_authority is the buyer's write path (veripsa_writer). It is SECURITY DEFINER (runs
-- as the migrator owner) and derives the agent+account from the CONNECTION identity, never from an arg — so a
-- credential-less role 42501s inside establish_session_write_context(). But Postgres grants EXECUTE to PUBLIC
-- by default on CREATE FUNCTION, so without this REVOKE the explicit GRANT below is misleading and the fn stays
-- callable by every role (incl. veripsa_reader, a real connecting identity granted meaning_surface). Same
-- stray-PUBLIC-grant class the moat's threat model closes everywhere else (see 30_gate.sql / 40_surfaces.sql).
REVOKE EXECUTE ON FUNCTION core.record_statement_with_authority(text,text,text,text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION core.record_statement_with_authority(text,text,text,text) TO veripsa_writer;
GRANT EXECUTE ON FUNCTION core.meaning_surface() TO veripsa_reader, veripsa_writer, veripsa_demo_steward;

-- ============================================================================================
