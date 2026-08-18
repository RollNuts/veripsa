-- ============================================================================================
-- CO-CHANGE (logical coupling) — the content-free EMPIRICAL coupling cache. See _cg_cochange.py.
-- Two files that keep changing together in history are coupled even when NO call/import/schema/config edge
-- links them (an implicit contract / convention / a rule split across files) — the "you touched A, B usually
-- comes with it" gut-feeling the static code graph is structurally BLIND to. A SECOND, INDEPENDENT detector
-- next to the graph. Stored per (account, repo): file PATHS + COUNTS + a conditional-probability strength,
-- NEVER code. Same moat as the code graph: FORCE RLS + tenant isolation + the governed-write forgery gate;
-- written only by the gated ingest fn; read only via a SECURITY DEFINER surface. A DERIVED cache
-- (DELETE+reINSERT on each ingest, exactly like code_node) — recomputed from the clone the graph extractor
-- already holds (clone → extract → delete); nothing extra fetched or retained.
-- ============================================================================================
CREATE TABLE IF NOT EXISTS core.co_change (
    account_id text NOT NULL,
    repo       text NOT NULL,
    path_a     text NOT NULL,           -- canonical order: path_a < path_b (one row per UNORDERED pair)
    path_b     text NOT NULL,
    co         int  NOT NULL,           -- #commits touching BOTH (content-free count)
    n_a        int  NOT NULL,           -- #commits touching path_a
    n_b        int  NOT NULL,           -- #commits touching path_b
    strength   real NOT NULL,           -- max(co/n_a, co/n_b) — the directional CONFIDENCE (human "X% of the time")
    lift       real NOT NULL DEFAULT 1, -- co·N/(n_a·n_b) — how many times MORE than chance (the base-rate-corrected
                                        -- PRECISION signal: a HOT file co-changes with everything at high confidence
                                        -- but lift≈1; a real coupling has lift>>1). The signal we rank/gate on.
    n_total    int  NOT NULL DEFAULT 0, -- N = #non-giant commits the pair was computed against (the base-rate
                                        -- denominator emit_pairs carries). Persisted so the PER-PUSH increment can
                                        -- read it BACK into seed_counters (which needs co/n_a/n_b/n_total to
                                        -- reconstruct the counters) — without it the increment can't recover N and
                                        -- the byte-identical-to-batch property breaks. Content-free (a commit COUNT).
    generation_observed_at timestamptz, -- processing-time generation stamp; NULL is legacy/unknown provenance
    PRIMARY KEY (account_id, repo, path_a, path_b),
    CONSTRAINT co_change_canonical CHECK (path_a < path_b),
    CONSTRAINT co_change_bounded   CHECK (length(path_a) <= 1024 AND length(path_b) <= 1024
                                          AND co >= 0 AND n_a >= 0 AND n_b >= 0 AND n_total >= 0
                                          AND strength >= 0 AND strength <= 1 AND lift >= 0)
);
-- OWN THE TABLE (audit #328-owner — privacy/GDPR/co-change launch-blocker). Every other core table reassigns its
-- owner to veripsa_migrator right after CREATE (20_core.sql claim/code_node/code_edge/graph_version/event/intent)
-- — co_change/co_change_seen_commit were the ONLY core tables that didn't, so on the MANAGED-Postgres path (the
-- schema applied by a role that is a MEMBER of veripsa_migrator: `GRANT veripsa_migrator TO <owner>` then apply
-- AS <owner> — OUR Render prod, per render.yaml) Postgres makes a new table owned by the CURRENT role (<owner>),
-- NOT the role it is a member of. The 5 SECURITY DEFINER fns that touch these tables run AS veripsa_migrator
-- (ALTER FUNCTION … OWNER TO veripsa_migrator below + in 35_lifecycle.sql), so a co_change owned by <owner> gives
-- them `permission denied for table co_change`: the uninstall purge silently no-ops (privacy claim broken),
-- the GDPR erase RAISES, and the whole co-change (2nd detector) read/write path dies SILENTLY. Pin the owner to
-- veripsa_migrator so the DEFINER fns own the tables regardless of WHICH veripsa_migrator-member applied the
-- schema. Idempotent + migration-safe: a member CAN reassign to a role it is a member of, and re-applying the
-- schema REASSIGNS an already-mis-owned table — so this also FIXES an existing managed deploy on redeploy (the
-- ADD COLUMN / ALTER POLICY / GRANT below already run owner-equivalent on re-apply; this just adds the missing one).
DO $$
BEGIN
  IF EXISTS (
    SELECT 1
      FROM pg_class c
      JOIN pg_namespace n ON n.oid = c.relnamespace
     WHERE n.nspname = 'core'
       AND c.relname = 'co_change'
       AND c.relowner <> 'veripsa_migrator'::regrole
  ) THEN
    ALTER TABLE core.co_change OWNER TO veripsa_migrator;
  END IF;
END $$;
SELECT core._ensure_column_online(
  'co_change','lift','real NOT NULL DEFAULT 1');
SELECT core._ensure_column_online(
  'co_change','n_total','int NOT NULL DEFAULT 0');
SELECT core._ensure_column_online(
  'co_change','generation_observed_at','timestamptz');
-- the moat PATTERN, applied to co_change exactly as 20_core.sql applies it to the code graph.
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_class WHERE oid = 'core.co_change'::regclass AND relrowsecurity) THEN
    ALTER TABLE core.co_change ENABLE ROW LEVEL SECURITY;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_class WHERE oid = 'core.co_change'::regclass AND relforcerowsecurity) THEN
    ALTER TABLE ONLY core.co_change FORCE ROW LEVEL SECURITY;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_policy WHERE polrelid = 'core.co_change'::regclass AND polname = 'tenant_isolation') THEN
    CREATE POLICY tenant_isolation ON core.co_change
        USING (account_id = current_setting('core.current_account', true))
        WITH CHECK (account_id = current_setting('core.current_account', true));
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM pg_trigger
     WHERE tgrelid = 'core.co_change'::regclass
       AND tgname = 'trg_governed_co_change'
       AND NOT tgisinternal
  ) THEN
    CREATE TRIGGER trg_governed_co_change BEFORE INSERT OR UPDATE ON core.co_change
        FOR EACH ROW EXECUTE FUNCTION core.assert_governed_write();
  END IF;
END $$;
CREATE INDEX CONCURRENTLY IF NOT EXISTS co_change_by_account_repo ON core.co_change (account_id, repo);
CREATE INDEX CONCURRENTLY IF NOT EXISTS co_change_a ON core.co_change (account_id, repo, path_a);
CREATE INDEX CONCURRENTLY IF NOT EXISTS co_change_b ON core.co_change (account_id, repo, path_b);

-- co_change_seen_commit — the content-free FOLDED-COMMIT ledger that makes the PER-PUSH increment IDEMPOTENT
-- ACROSS DELIVERIES. The increment is ALL-TIME-ADDITIVE (it folds a push's commits onto the stored seed), so a
-- GitHub webhook REDELIVERY (the SAME push event re-sent — GitHub does this routinely) carries the SAME commits[]
-- and would DOUBLE-COUNT them. In-payload sha-dedupe (the dispatcher) cannot help across two separate deliveries.
-- So we record every folded COMMIT SHA per (account, repo) and fold a sha at most once, EVER. Content-free: only a
-- git commit hash (no message / body / author / diff). Same moat as the cache: FORCE RLS + the governed-write
-- forgery gate; written only by the gated filter fn below. Bounded: shas are 64 hex chars; the periodic re-backfill
-- (the source of truth) is unaffected — this only de-dupes the between-backfill increment.
CREATE TABLE IF NOT EXISTS core.co_change_seen_commit (
    account_id text NOT NULL,
    repo       text NOT NULL,
    commit_sha text NOT NULL,
    generation_observed_at timestamptz,
    PRIMARY KEY (account_id, repo, commit_sha),
    CONSTRAINT co_change_seen_bounded CHECK (length(repo) <= 512 AND length(commit_sha) BETWEEN 1 AND 64
                                             AND commit_sha ~ '^[0-9a-fA-F]+$')
);
-- OWN THE TABLE (audit #328-owner) — same fix + same reason as core.co_change above: the SECURITY DEFINER fns
-- (purge_account_working_set / erase_account in 35_lifecycle.sql, co_change_filter_unseen_commits below) run AS
-- veripsa_migrator and must OWN this idempotency ledger or they hit `permission denied for table
-- co_change_seen_commit` whenever a veripsa_migrator-MEMBER applied the schema. Idempotent; reassigns on re-apply.
DO $$
BEGIN
  IF EXISTS (
    SELECT 1
      FROM pg_class c
      JOIN pg_namespace n ON n.oid = c.relnamespace
     WHERE n.nspname = 'core'
       AND c.relname = 'co_change_seen_commit'
       AND c.relowner <> 'veripsa_migrator'::regrole
  ) THEN
    ALTER TABLE core.co_change_seen_commit OWNER TO veripsa_migrator;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_class WHERE oid = 'core.co_change_seen_commit'::regclass AND relrowsecurity) THEN
    ALTER TABLE core.co_change_seen_commit ENABLE ROW LEVEL SECURITY;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_class WHERE oid = 'core.co_change_seen_commit'::regclass AND relforcerowsecurity) THEN
    ALTER TABLE ONLY core.co_change_seen_commit FORCE ROW LEVEL SECURITY;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_policy WHERE polrelid = 'core.co_change_seen_commit'::regclass AND polname = 'tenant_isolation') THEN
    CREATE POLICY tenant_isolation ON core.co_change_seen_commit
        USING (account_id = current_setting('core.current_account', true))
        WITH CHECK (account_id = current_setting('core.current_account', true));
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM pg_trigger
     WHERE tgrelid = 'core.co_change_seen_commit'::regclass
       AND tgname = 'trg_governed_co_change_seen_commit'
       AND NOT tgisinternal
  ) THEN
    CREATE TRIGGER trg_governed_co_change_seen_commit BEFORE INSERT OR UPDATE ON core.co_change_seen_commit
        FOR EACH ROW EXECUTE FUNCTION core.assert_governed_write();
  END IF;
END $$;
SELECT core._ensure_column_online(
  'co_change_seen_commit','generation_observed_at','timestamptz');

-- co_change_filter_unseen_commits_with_authority: the gated, atomic SHA-DEDUPE. Given a push's commit shas, RECORD
-- the ones not yet folded for this (account, repo) and RETURN exactly that newly-recorded subset — so the caller
-- folds each commit AT MOST ONCE across all deliveries (a redelivery's shas are already present → returned empty →
-- a true no-op). Atomic by the INSERT … ON CONFLICT DO NOTHING RETURNING: two concurrent deliveries of the same
-- push each try to insert the shas; only the first wins each row, so each sha is returned to exactly one caller.
-- Content-free (only hashes). The forgery gate + RLS apply exactly like every other gated write.
CREATE OR REPLACE FUNCTION core.co_change_filter_unseen_commits_with_authority(p_repo text, p_shas text[])
    RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path TO 'core','pg_catalog' AS $$
DECLARE v_agent text; v_account text; v_repo text; v_out jsonb; v_generation_after timestamptz;
BEGIN
  SELECT agent, account INTO v_agent, v_account FROM core.establish_session_write_context() AS c(agent,account);
  v_repo := left(NULLIF(btrim(COALESCE(p_repo,'')),''),512);
  IF v_repo IS NULL THEN RAISE EXCEPTION 'co_change_seen needs a repo' USING ERRCODE='23514'; END IF;
  -- IN-TXN RESURRECTION GUARD (root fix, small-findings sweep — the co-change/uninstall LIVE race), the seen-commit
  -- LEDGER half of the same fix in ingest_cochange_with_authority below. The per-push increment writes the folded-
  -- commit ledger HERE first, then re-ingests pairs there — so BOTH writers must be walled against a tombstone that
  -- the account-wide purge slipped into the autocommit gap after the pre-flight assert, else a redelivery-dedupe row
  -- (commit shas = content-free, but still this tenant's state) resurrects into a purged tenant. Row-lock the account
  -- FOR SHARE (serializes against the purge's UPDATE + the erase's DELETE) then refuse if tombstoned — ATOMIC with
  -- the ledger INSERT in this one statement-transaction. RLS pins v_account, so the lock touches only this tenant.
  PERFORM 1 FROM core.account WHERE account_id = v_account FOR SHARE;
  IF EXISTS (
    SELECT 1 FROM core.account_lifecycle_tombstone
     WHERE account_id = v_account AND active
  ) THEN
    RAISE EXCEPTION 'co-change seen-commit write refused: account % is tombstoned (uninstall-purged or erased)', v_account
      USING ERRCODE='42501';
  END IF;
  IF EXISTS (
      SELECT 1 FROM core.repository_lifecycle_tombstone
       WHERE account_id=v_account AND repo=v_repo AND superseded_at IS NULL) THEN
    RAISE EXCEPTION 'co-change seen-commit write refused: repository % is tombstoned', v_repo
      USING ERRCODE='42501';
  END IF;
  SELECT core._repository_generation_boundary(v_repo) INTO v_generation_after;
  IF v_generation_after > '-infinity'::timestamptz THEN
    DELETE FROM core.co_change_seen_commit
     WHERE account_id=v_account AND repo=v_repo
       AND (generation_observed_at IS NULL OR generation_observed_at < v_generation_after);
  END IF;
  PERFORM core.mark_governed_write('co_change_seen_commit');
  -- A data-modifying CTE (INSERT … RETURNING) must be at the TOP LEVEL of its statement — it cannot be nested in a
  -- scalar RETURN (…) subquery. So we run it as the top-level statement of a SELECT … INTO and aggregate its
  -- RETURNING in an outer CTE. ON CONFLICT DO NOTHING makes it atomically return ONLY the newly-recorded shas.
  WITH incoming AS (
    SELECT DISTINCT s AS commit_sha
      FROM unnest(COALESCE(p_shas, '{}'::text[])) s
     WHERE s IS NOT NULL AND length(s) BETWEEN 1 AND 64 AND s ~ '^[0-9a-fA-F]+$'
  ),
  inserted AS (
    INSERT INTO core.co_change_seen_commit(account_id, repo, commit_sha, generation_observed_at)
    SELECT v_account, v_repo, commit_sha, clock_timestamp() FROM incoming
    ON CONFLICT (account_id, repo, commit_sha) DO NOTHING
    RETURNING commit_sha
  )
  SELECT COALESCE(jsonb_agg(commit_sha ORDER BY commit_sha), '[]'::jsonb) INTO v_out FROM inserted;
  RETURN v_out;
END $$;
ALTER FUNCTION core.co_change_filter_unseen_commits_with_authority(text, text[]) OWNER TO veripsa_migrator;
REVOKE EXECUTE ON FUNCTION core.co_change_filter_unseen_commits_with_authority(text, text[]) FROM PUBLIC, veripsa_writer;
GRANT  EXECUTE ON FUNCTION core.co_change_filter_unseen_commits_with_authority(text, text[]) TO veripsa_app;   -- only the App folds pushes

-- ingest_cochange_with_authority: the gated write. DELETE+reINSERT the repo's co-change pairs (a derived cache,
-- recomputed each ingest). Content-free + bounded: only paths + counts + a clamped strength cross the boundary.
-- Mirrors ingest_graph_with_authority's mark_governed_write usage (one arm before the multi-row INSERT).
CREATE OR REPLACE FUNCTION core.ingest_cochange_with_authority(p_pairs jsonb, p_repo text)
    RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path TO 'core','pg_catalog' AS $$
DECLARE v_agent text; v_account text; v_repo text; v_n int; v_generated_at timestamptz;
BEGIN
  SELECT agent, account INTO v_agent, v_account FROM core.establish_session_write_context() AS c(agent,account);
  v_repo := left(NULLIF(btrim(COALESCE(p_repo,'')),''),512);
  IF v_repo IS NULL THEN RAISE EXCEPTION 'ingest_cochange needs a repo' USING ERRCODE='23514'; END IF;
  -- IN-TXN RESURRECTION GUARD (root fix, small-findings sweep — the co-change/uninstall LIVE race). The background
  -- co-change populate/increment calls core.assert_account_live_with_authority() as a SEPARATE autocommit statement
  -- and THEN calls this writer as ANOTHER separate statement — so on the autocommit connection there is a real
  -- TOCTOU window BETWEEN the two: a pre-flight assert commits before this write begins. The liveness check must
  -- therefore be ATOMIC WITH THE WRITE inside this function's single statement-transaction. The account row lock
  -- is defense in depth beside the shared account-lifecycle fence taken by assert_account_live_with_authority():
  -- whoever wins serialization first either writes before the purge (which then reaps it), or sees the tombstone
  -- and refuses with 42501. Either ordering leaves no post-tombstone residue. RLS pins v_account, so FOR SHARE locks
  -- only this tenant's row.
  PERFORM 1 FROM core.account WHERE account_id = v_account FOR SHARE;
  IF EXISTS (
    SELECT 1 FROM core.account_lifecycle_tombstone
     WHERE account_id = v_account AND active
  ) THEN
    RAISE EXCEPTION 'co-change write refused: account % is tombstoned (uninstall-purged or erased)', v_account
      USING ERRCODE='42501';   -- same fail-closed class as assert_account_live_with_authority
  END IF;
  IF EXISTS (
      SELECT 1 FROM core.repository_lifecycle_tombstone
       WHERE account_id=v_account AND repo=v_repo AND superseded_at IS NULL) THEN
    RAISE EXCEPTION 'co-change write refused: repository % is tombstoned', v_repo
      USING ERRCODE='42501';
  END IF;
  v_generated_at := clock_timestamp();
  DELETE FROM core.co_change WHERE account_id=v_account AND repo=v_repo;   -- DELETE is not governed-triggered (INSERT/UPDATE only); RLS still walls it
  PERFORM core.mark_governed_write('co_change');
  INSERT INTO core.co_change(
      account_id, repo, path_a, path_b, co, n_a, n_b, strength, lift, n_total, generation_observed_at)
  SELECT v_account, v_repo,
         LEAST(p->>'a', p->>'b'), GREATEST(p->>'a', p->>'b'),   -- canonical order (path_a < path_b)
         LEAST(GREATEST((p->>'co')::int, 0), 1000000000),
         LEAST(GREATEST((p->>'n_a')::int, 0), 1000000000),
         LEAST(GREATEST((p->>'n_b')::int, 0), 1000000000),
         GREATEST(0::real, LEAST(1::real, (p->>'strength')::real)),   -- confidence, clamp to [0,1]
         GREATEST(0::real, COALESCE((p->>'lift')::real, 1)),           -- lift (base-rate-corrected), >= 0
         LEAST(GREATEST(COALESCE((p->>'n_total')::int, 0), 0), 1000000000),  -- N (base-rate denominator; missing → 0, bounded)
         v_generated_at
  FROM jsonb_array_elements(COALESCE(p_pairs, '[]'::jsonb)) p
  WHERE p->>'a' IS NOT NULL AND p->>'b' IS NOT NULL AND p->>'a' <> p->>'b'
        AND length(p->>'a') <= 1024 AND length(p->>'b') <= 1024
  ON CONFLICT (account_id, repo, path_a, path_b) DO NOTHING;   -- extractor emits one row per unordered pair; belt
  GET DIAGNOSTICS v_n = ROW_COUNT;
  RETURN jsonb_build_object('ok', true, 'repo', v_repo, 'pairs', v_n);
END $$;
ALTER FUNCTION core.ingest_cochange_with_authority(jsonb, text) OWNER TO veripsa_migrator;
REVOKE EXECUTE ON FUNCTION core.ingest_cochange_with_authority(jsonb, text) FROM PUBLIC, veripsa_writer;
GRANT  EXECUTE ON FUNCTION core.ingest_cochange_with_authority(jsonb, text) TO veripsa_app;   -- the App ingests co-change on backfill, like the graph

-- co_change_partners_with_authority: the read surface. For a PR's EDITED `p_paths`, the strongest co-change
-- PARTNER files NOT already in the edit — the "you touched A; B historically comes with it (X% of the time)"
-- completeness hint. `prob` is the DIRECTIONAL conditional probability P(partner changes | edited changed) =
-- co / n(edited) — the number that actually answers "given I touched this, how often does the partner follow".
-- Account is pinned by the caller (the App entered the installation); co_change RLS walls it to that tenant.
-- Content-free (paths + counts). Capped at the top `p_limit` partners per edited file (advisory, not a firehose).
CREATE OR REPLACE FUNCTION core.co_change_partners_with_authority(p_repo text, p_paths text[], p_limit int DEFAULT 3, p_min_prob real DEFAULT 0.4)
    RETURNS jsonb LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path TO 'core','pg_catalog' AS $$
DECLARE v_account text; v_generation_after timestamptz;
BEGIN
  -- RESOLVE + RE-PIN the tenant: enter_installation sets core.current_account TXN-LOCAL, so a SEPARATE read
  -- statement (autocommit) has lost it → RLS would hide every row. Re-pin from the session identity exactly as
  -- main_impact_surface does (resolve_session_identity reads the persistent installation_account / role).
  SELECT account INTO v_account FROM core.resolve_session_identity() AS r(agent, account);
  PERFORM set_config('core.current_account', v_account, true);
  SELECT core._repository_generation_boundary(left(COALESCE(p_repo, ''), 512)) INTO v_generation_after;
  RETURN (
  WITH edited AS (SELECT DISTINCT x AS path FROM unnest(COALESCE(p_paths, '{}'::text[])) x WHERE x IS NOT NULL),
  rp AS (SELECT left(COALESCE(p_repo, ''), 512) AS repo),
  -- RENDER FLOOR (PR #314 proper fix): the STORE now keeps every REPEATED co-change RAW (co>=2, no prob/lift
  -- pruning) so the per-push increment can ACCUMULATE a coupling that forms across pushes (the store is the
  -- accumulator, _cg_cochange.PERSIST_MIN_SUPPORT). The customer-facing PRECISION floor — co>=5 (RENDER_MIN_SUPPORT)
  -- and lift>=2 (RENDER_MIN_LIFT) — is applied HERE, at the read, so the annotation/format-sweep FP class (rare
  -- pairs co-changed only 3-4×) never surfaces to a customer, while sub-floor pairs stay in the store and grow.
  partners AS (
    -- edited is path_a → partner is path_b; directional prob = co / n_a
    SELECT cc.path_a AS edited, cc.path_b AS partner, cc.co, cc.lift AS lift,
           (cc.co::real / NULLIF(cc.n_a, 0)) AS prob, cc.n_a AS n
      FROM core.co_change cc JOIN edited e ON e.path = cc.path_a, rp
     WHERE cc.repo = rp.repo AND cc.path_b NOT IN (SELECT path FROM edited)
       AND (v_generation_after='-infinity'::timestamptz
            OR cc.generation_observed_at >= v_generation_after)
       AND cc.co >= 5 AND cc.lift >= 2::real      -- render floor (see comment above)
    UNION ALL
    -- edited is path_b → partner is path_a; directional prob = co / n_b
    SELECT cc.path_b AS edited, cc.path_a AS partner, cc.co, cc.lift AS lift,
           (cc.co::real / NULLIF(cc.n_b, 0)) AS prob, cc.n_b AS n
      FROM core.co_change cc JOIN edited e ON e.path = cc.path_b, rp
     WHERE cc.repo = rp.repo AND cc.path_a NOT IN (SELECT path FROM edited)
       AND (v_generation_after='-infinity'::timestamptz
            OR cc.generation_observed_at >= v_generation_after)
       AND cc.co >= 5 AND cc.lift >= 2::real      -- render floor (see comment above)
  ),
  ranked AS (
    -- rank by LIFT first (real coupling beyond chance — the base-rate-corrected precision signal), then the
    -- directional confidence; NEVER an average. prob is kept only for the human "X% of the time" display.
    SELECT edited, partner, co, prob, lift, n,
           row_number() OVER (PARTITION BY edited ORDER BY lift DESC NULLS LAST, prob DESC NULLS LAST, co DESC, partner) AS rn
      FROM partners WHERE prob IS NOT NULL AND prob >= GREATEST(0::real, LEAST(1::real, COALESCE(p_min_prob, 0.4)))
  )
  SELECT COALESCE(
    jsonb_agg(jsonb_build_object('edited', edited, 'partner', partner, 'co', co,
                                 'prob', round(prob::numeric, 3), 'lift', round(lift::numeric, 2), 'n', n)
              ORDER BY lift DESC, prob DESC, co DESC)
    FILTER (WHERE rn <= GREATEST(1, LEAST(COALESCE(p_limit, 3), 10))), '[]'::jsonb)
    FROM ranked);
END $$;
ALTER FUNCTION core.co_change_partners_with_authority(text, text[], int, real) OWNER TO veripsa_migrator;
REVOKE EXECUTE ON FUNCTION core.co_change_partners_with_authority(text, text[], int, real) FROM PUBLIC;
GRANT  EXECUTE ON FUNCTION core.co_change_partners_with_authority(text, text[], int, real) TO veripsa_app, veripsa_writer;

-- co_change_all_with_authority: the READ-BACK-ALL surface — the repo's WHOLE stored co-change pair set in the
-- SAME shape emit_pairs emits ({a,b,co,n_a,n_b,n_total,strength,lift,...}), so _cg_cochange.seed_counters can
-- reconstruct the (change, co, n_total) counters the PER-PUSH increment folds onto. The partner reader
-- (co_change_partners_with_authority) only returns the partners of a GIVEN edited-file set; the increment needs
-- the ENTIRE stored pair list to recover the counters, which no surface exposed before #259 handed this off.
-- `p_branch` is accepted for caller symmetry (the populate/push path carries a branch) but the cache is keyed
-- per (account, repo) like the partner reader — branch is not a stored dimension. MOAT, IDENTICAL to the partner
-- reader: same SECURITY DEFINER + resolve_session_identity re-pin (a separate autocommit read has lost the
-- txn-local pin), so FORCE-RLS walls the cache to the caller's own tenant — no cross-tenant read. Content-free
-- (paths + counts only). Bounded: capped at p_limit rows, ordered by lift then support (a busy monorepo's pair
-- space is O(files²); the cap bounds the read just as ingest's max_pairs bounds the write).
CREATE OR REPLACE FUNCTION core.co_change_all_with_authority(p_repo text, p_branch text DEFAULT NULL, p_limit int DEFAULT 5000)
    RETURNS jsonb LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path TO 'core','pg_catalog' AS $$
DECLARE v_account text; v_generation_after timestamptz;
BEGIN
  -- RE-PIN the tenant from the session identity (same reason co_change_partners_with_authority does it): a
  -- separate read statement has lost the txn-local core.current_account, so without this RLS hides every row.
  SELECT account INTO v_account FROM core.resolve_session_identity() AS r(agent, account);
  PERFORM set_config('core.current_account', v_account, true);
  SELECT core._repository_generation_boundary(left(COALESCE(p_repo, ''), 512)) INTO v_generation_after;
  RETURN (
  WITH rp AS (SELECT left(COALESCE(p_repo, ''), 512) AS repo),
  rows AS (
    SELECT cc.path_a AS a, cc.path_b AS b, cc.co, cc.n_a, cc.n_b, cc.n_total, cc.strength, cc.lift
      FROM core.co_change cc, rp
     WHERE cc.repo = rp.repo
       AND (v_generation_after='-infinity'::timestamptz
            OR cc.generation_observed_at >= v_generation_after)
     ORDER BY cc.lift DESC NULLS LAST, cc.strength DESC NULLS LAST, cc.co DESC, cc.path_a, cc.path_b
     LIMIT GREATEST(1, LEAST(COALESCE(p_limit, 5000), 100000))
  )
  SELECT COALESCE(
    jsonb_agg(jsonb_build_object('a', a, 'b', b, 'co', co, 'n_a', n_a, 'n_b', n_b, 'n_total', n_total,
                                 'strength', round(strength::numeric, 3), 'lift', round(lift::numeric, 2))),
    '[]'::jsonb)
    FROM rows);
END $$;
ALTER FUNCTION core.co_change_all_with_authority(text, text, int) OWNER TO veripsa_migrator;
REVOKE EXECUTE ON FUNCTION core.co_change_all_with_authority(text, text, int) FROM PUBLIC;
GRANT  EXECUTE ON FUNCTION core.co_change_all_with_authority(text, text, int) TO veripsa_app, veripsa_writer;
