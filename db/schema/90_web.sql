-- PHASE 5 — WEB SURFACES (the console's read/owner-write path). Everything the 13 routes need beyond
-- the core surfaces, derived FROM the existing tables — NO new tables (the no-乱立 law: the manifest
-- stays at 15). The home's directory tree + Main panel; the account page's tokens + connections +
-- account meta; the in-app notifications feed. Content-free; tenant-pinned; granted to the console roles.
-- ============================================================================================

-- _latest_coordinate: the most-recently-ingested (repo,branch) for the pinned account — so the home can
-- render without the caller knowing the coordinate. (helper; account already pinned by the caller.)
CREATE OR REPLACE FUNCTION core._latest_coordinate(p_account text, OUT o_repo text, OUT o_branch text) RETURNS record
    LANGUAGE sql STABLE SECURITY DEFINER SET search_path TO 'core','pg_catalog' AS $$
  SELECT repo, branch FROM core.graph_version WHERE account_id=p_account ORDER BY ingested_at DESC LIMIT 1
$$;
ALTER FUNCTION core._latest_coordinate(text) OWNER TO veripsa_migrator;
-- INTERNAL ONLY: takes p_account + SECURITY DEFINER, so PUBLIC execute would let any tenant read ANOTHER
-- account's latest (repo,branch) coordinate cross-tenant (its private repo name). Postgres grants EXECUTE to
-- PUBLIC by default — revoke it. Only directory_surface (owner-context, passing its OWN account) calls it.
REVOKE ALL ON FUNCTION core._latest_coordinate(text) FROM PUBLIC;

-- directory_surface: the DIRECTORY tree for a coordinate (the home's main tree). For each file: its
-- language, its symbol count (def/class children), and whether an agent is editing it live + who.
-- No coordinate given → the latest-ingested one. Content-free (paths/names/counts, never bodies).
CREATE OR REPLACE FUNCTION core.directory_surface(p_repo text DEFAULT NULL, p_branch text DEFAULT NULL) RETURNS jsonb
    LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path TO 'core','pg_catalog' AS $$
DECLARE v_account text; v_repo text; v_branch text; v_result jsonb;
BEGIN
  SELECT account INTO v_account FROM core.resolve_session_identity() AS r(agent,account);
  PERFORM set_config('core.current_account', v_account, true);
  IF p_repo IS NULL AND p_branch IS NULL THEN
    SELECT o_repo, o_branch INTO v_repo, v_branch FROM core._latest_coordinate(v_account);
  ELSE
    v_repo := left(COALESCE(p_repo,''),512); v_branch := left(COALESCE(p_branch,''),512);
  END IF;
  v_repo := COALESCE(v_repo,''); v_branch := COALESCE(v_branch,'');
  SELECT jsonb_build_object(
    'repo', v_repo, 'branch', v_branch,
    'total_files', (SELECT count(*)::int FROM core.code_node WHERE account_id=v_account AND repo=v_repo AND branch=v_branch AND node_kind='file'),
    'files', COALESCE((SELECT jsonb_agg(jsonb_build_object(
        'path', f.path, 'language', f.lang,
        'symbols', COALESCE(s.n,0),
        'claimed', (cl.agent_id IS NOT NULL),
        'claimed_by', core.agent_name(cl.agent_id)
      ) ORDER BY f.path)
      FROM (SELECT path, max(language) AS lang FROM core.code_node
             WHERE account_id=v_account AND repo=v_repo AND branch=v_branch AND node_kind='file' GROUP BY path) f
      LEFT JOIN (SELECT path, count(*)::int AS n FROM core.code_node
                  WHERE account_id=v_account AND repo=v_repo AND branch=v_branch AND node_kind IN ('def','class') GROUP BY path) s ON s.path=f.path
      LEFT JOIN (SELECT DISTINCT ON (target_path) target_path, agent_id FROM core.claim
                  WHERE account_id=v_account AND repo=v_repo AND branch=v_branch AND claim_state='active') cl ON cl.target_path=f.path
    ), '[]'::jsonb)
  ) INTO v_result;
  RETURN v_result;
END $$;
ALTER FUNCTION core.directory_surface(text,text) OWNER TO veripsa_migrator;

-- account_coverage_surface: the BILLING-COVERAGE lens for the pinned account — the ONE read the PR check + the
-- console use to NUDGE an upgrade (PO: "課金促すように"). Content-free: the plan LABEL, the plan's analyzed-file
-- ceiling (_plan_file_limit), the account's CURRENT analyzed-file count, and how far OVER the line it is. The
-- analyzed-file count = DISTINCT (repo,path) file nodes for the account: a file counts ONCE per repo, so feature
-- branches NEVER inflate the meter (it tracks the codebase, the "road network", not the branch fan-out), and it
-- AUTO-TRACKS deletes (patch_graph removes a deleted file's node → the count drops on the next push). ADVISORY
-- ONLY — never blocks; the App renders an honest "N files beyond your plan aren't covered yet — upgrade" hint.
--   file_limit NULL = unlimited (Enterprise / unmapped paid) ⇒ over_by 0, near false (never nudge a payer).
--   over_by = files beyond the line (0 when under or unlimited). near = within 80% of the line (gentle heads-up).
-- Tenant-pinned (resolve_session_identity → current_account): one account can never read another's count/plan.
CREATE OR REPLACE FUNCTION core.account_coverage_surface() RETURNS jsonb
    LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path TO 'core','pg_catalog' AS $$
DECLARE v_account text; v_plan text; v_limit bigint; v_files bigint; v_over bigint; v_near boolean;
BEGIN
  SELECT account INTO v_account FROM core.resolve_session_identity() AS r(agent,account);
  PERFORM set_config('core.current_account', v_account, true);
  SELECT COALESCE(NULLIF(btrim(COALESCE(plan,'')),''),'free') INTO v_plan FROM core.account WHERE account_id=v_account;
  v_plan  := COALESCE(v_plan,'free');
  v_limit := core._plan_file_limit(v_plan);
  SELECT COALESCE(count(*),0) INTO v_files
    FROM (SELECT DISTINCT repo, path FROM core.code_node
           WHERE account_id=v_account AND node_kind='file') d;
  IF v_limit IS NULL THEN
    v_over := 0; v_near := false;
  ELSE
    v_over := GREATEST(v_files - v_limit, 0);
    v_near := (v_over = 0 AND v_files::numeric >= v_limit::numeric * 0.8);
  END IF;
  RETURN jsonb_build_object(
    'plan',       v_plan,
    'file_limit', v_limit,          -- NULL = unlimited
    'file_count', v_files,
    'over_by',    v_over,           -- files beyond the plan (the nudge driver)
    'near',       v_near,           -- within 80% of the line (gentle heads-up)
    'advisory',   true);            -- never blocks; a hint only
EXCEPTION WHEN OTHERS THEN
  -- FAIL-SAFE: a broken coverage read must never break the check render — return a benign "unlimited / no nudge".
  RETURN jsonb_build_object('plan','free','file_limit',NULL,'file_count',0,'over_by',0,'near',false,'advisory',true);
END $$;
ALTER FUNCTION core.account_coverage_surface() OWNER TO veripsa_migrator;

-- account_coverage_for_installation: the SAME billing-coverage lens as account_coverage_surface, but keyed by
-- INSTALLATION id (the platform's content-free routing key) instead of the session identity — so the hosted web
-- dashboard can show a viewer BOTH their correct plan AND their current usage. Resolves installation → account
-- (core.installation_account), pins it, and returns {plan, file_limit, file_count, over_by, near}. CONTENT-FREE:
-- the plan LABEL + the analyzed-file COUNT (the PUBLIC billing meter — DISTINCT (repo,path) file nodes) + the
-- plan ceiling. It NEVER exposes the code graph itself (no node/edge structure, no edges, no coverage % of the
-- graph) — only the file count the customer is billed on, which the PO chose as the public meter. SECURITY
-- DEFINER + routed pin (same shape as the other *_for_installation reads); fail-safe to a benign 'free' row so a
-- broken read can't break the dashboard. Granted to the platform reader + the App service identity.
CREATE OR REPLACE FUNCTION core.account_coverage_for_installation(p_installation_id text) RETURNS jsonb
    LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path TO 'core','pg_catalog' AS $$
DECLARE v_inst text; v_account text; v_plan text; v_limit bigint; v_files bigint; v_over bigint; v_near boolean;
BEGIN
  v_inst := left(NULLIF(btrim(COALESCE(p_installation_id,'')),''),64);
  IF v_inst IS NULL THEN RETURN NULL; END IF;
  SELECT account_id INTO v_account FROM core.installation_account WHERE installation_id = v_inst AND revoked_at IS NULL;
  IF v_account IS NULL THEN RETURN NULL; END IF;            -- unknown/revoked installation → no row (the platform shows nothing)
  PERFORM set_config('core.current_account', v_account, true);
  SELECT COALESCE(NULLIF(btrim(COALESCE(plan,'')),''),'free') INTO v_plan FROM core.account WHERE account_id=v_account;
  v_plan  := COALESCE(v_plan,'free');
  v_limit := core._plan_file_limit(v_plan);
  SELECT COALESCE(count(*),0) INTO v_files
    FROM (SELECT DISTINCT repo, path FROM core.code_node
           WHERE account_id=v_account AND node_kind='file') d;
  IF v_limit IS NULL THEN
    v_over := 0; v_near := false;
  ELSE
    v_over := GREATEST(v_files - v_limit, 0);
    v_near := (v_over = 0 AND v_files::numeric >= v_limit::numeric * 0.8);
  END IF;
  RETURN jsonb_build_object(
    'plan',       v_plan,
    'file_limit', v_limit,          -- NULL = unlimited (Enterprise / unmapped paid)
    'file_count', v_files,          -- the public billing meter (DISTINCT (repo,path) file nodes)
    'over_by',    v_over,
    'near',       v_near,
    'advisory',   true);
EXCEPTION WHEN OTHERS THEN
  -- FAIL-SAFE: a broken coverage read must never break the dashboard — benign "free / no nudge".
  RETURN jsonb_build_object('plan','free','file_limit',NULL,'file_count',0,'over_by',0,'near',false,'advisory',true);
END $$;
ALTER FUNCTION core.account_coverage_for_installation(text) OWNER TO veripsa_migrator;
REVOKE ALL ON FUNCTION core.account_coverage_for_installation(text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION core.account_coverage_for_installation(text) TO example_platform_reader, veripsa_app;

-- graph_insights_for_installation: the ALWAYS-PRESENT structural lens for the hosted dashboard. Every other
-- reader-facing *_for_installation read (effect/now/repo_insights) is COLLISION-EVENT based — so the customer's
-- dashboard goes DARK whenever there are no recent collisions, even though the rich code graph (code_node/
-- code_edge) + the co-change cache are sitting right there. This read surfaces that standing intelligence: it is
-- non-empty as soon as a graph has been ingested, with or without any contention. ONE installation → its busiest
-- repo (the repo with the MOST code_node rows, on the branch carrying the most nodes), and for that repo:
--   • coupled  — the top 8 co-change pairs by LIFT, at a floor of lift>=2 AND co>=3 (sub-floor noise never
--                surfaces). {a,b,pct,support_n,observed_m}: a<b (canonical), pct = round(strength*100) — the
--                SAME % the PR-check coverage-nudge / cochange line already render (a directional
--                conditional-probability, not a raw count). Empty until real coupling has formed.
--                support_n + observed_m are the HONEST DENOMINATOR pair so the dashboard can show
--                "N of M observed commits · X%" instead of a bare % that LIES on tiny samples (a single
--                1-of-1 co-occurrence has strength=1.0 → pct=100, masquerading as a perfect coupling):
--                  • support_n = co            — N: #commits where BOTH files changed (the co-occurrence support).
--                  • observed_m = n_a+n_b-co   — M: #commits where EITHER file changed (the UNION = the set of
--                    commits in which the pair was OBSERVABLE). By inclusion-exclusion |A∪B| = |A|+|B|-|A∩B|,
--                    and |A∩B| = co. This is the statistically-correct base for "of the M commits that touched
--                    either file, N touched both" — and it is what makes a 1-of-1 self-evidently weak: support_n=1,
--                    observed_m=1 reads "1 of 1", exposing the sample size the bare pct hides. (We deliberately do
--                    NOT use n_total — the whole-repo commit count — as M: it would make "N of M" read "N of
--                    <every commit in the repo>", which understates the rate for two files that simply change less
--                    often than the repo as a whole. The union is the honest, pair-local observation set.)
--                MOAT NOTE: support_n/observed_m are BEHAVIORAL co-occurrence COUNTS (how often the customer's own
--                two files changed together / at all) — the product's VISIBLE EFFECT for the customer's OWN repo
--                in the AUTHENTICATED dashboard, NOT the graph MECHANISM (no node/edge totals, no history-size /
--                coverage / billing metric). This is a distinct channel from the GitHub PR-check string, where the
--                raw "(N of M changes)" parenthetical IS banned (gate 184) because it leaked the graph's depth to
--                a public surface; here the platform renders the honest denominator for the tenant's own data.
--   • central  — the top 8 FILE nodes by in-repo FAN-IN = COUNT(DISTINCT code_edge.src) over edge_kind='imports'
--                edges whose dst resolves to an INTERNAL file node (stdlib/external symbols are dropped by the
--                file-node join — same resolution split_candidates' structural arm uses). {path,dependents}:
--                how many of the customer's OWN files import this one. dependents desc.
-- MOAT / CONTENT-FREE: this exposes ONLY the customer's OWN repo data — file PATH strings (already stored,
-- content-free), a co-change % (strength), and an in-repo dependents COUNT of the customer's own files. It NEVER
-- exposes global node/edge TOTALS, the coverage % / billing metric, or any cross-account data (the account is
-- pinned, so RLS scopes every read to this one tenant). Bounded (LIMIT 8 each). SECURITY DEFINER + routed pin,
-- same shape as account_coverage_for_installation. NEVER raises — any error (incl. an unmapped/blank id) returns
-- the benign empty shape {"repo":null,"coupled":[],"central":[]} so a broken read can't break the dashboard.
-- OPTIONAL p_repo: the dashboard's per-repo DETAIL page renders THIS SAME graph scoped to the repo being viewed.
--   • p_repo NULL (default) → EXACT prior behavior: the account's BUSIEST repo (1-arg call sites unchanged — the
--     DEFAULT makes graph_insights_for_installation(id) resolve to this one function, no separate overload).
--   • p_repo given AND it is one of the account's OWN repos (has code_node rows under the pinned account — the
--     same membership the busiest-repo scan walks) → scope coupled/central to THAT repo (its busiest branch).
--   • p_repo given but NOT the account's repo → the benign empty shape (NEVER another repo's data, NEVER an
--     error). The account pin + the membership EXISTS check make a cross-account / foreign repo a no-op.
-- Adding the param via CREATE OR REPLACE would leave the OLD 1-arg overload behind on a re-apply (dead code + an
-- ambiguous call); DROP it first (mirrors the patch_graph_with_authority / record_collision signature pattern).
DROP FUNCTION IF EXISTS core.graph_insights_for_installation(text);
CREATE OR REPLACE FUNCTION core.graph_insights_for_installation(p_installation_id text, p_repo text DEFAULT NULL) RETURNS jsonb
    LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path TO 'core','pg_catalog' AS $$
DECLARE v_inst text; v_account text; v_repo text; v_branch text; v_want text; v_result jsonb;
  v_empty constant jsonb := jsonb_build_object('repo', NULL, 'coupled', '[]'::jsonb, 'central', '[]'::jsonb);
BEGIN
  v_inst := left(NULLIF(btrim(COALESCE(p_installation_id,'')),''),64);
  IF v_inst IS NULL THEN RETURN v_empty; END IF;
  SELECT account_id INTO v_account FROM core.installation_account WHERE installation_id = v_inst AND revoked_at IS NULL;
  IF v_account IS NULL THEN RETURN v_empty; END IF;            -- unknown/revoked installation → benign empty
  PERFORM set_config('core.current_account', v_account, true);
  v_want := NULLIF(btrim(COALESCE(p_repo,'')),'');             -- NULL/blank p_repo → busiest-repo (prior) path
  IF v_want IS NULL THEN
    -- BUSIEST repo = most code_node rows; within it, the branch carrying the most nodes (a feature branch with a
    -- partial graph never wins over the primary line). One scan, deterministic tie-break on (repo,branch).
    SELECT repo, branch INTO v_repo, v_branch
      FROM core.code_node WHERE account_id = v_account
     GROUP BY repo, branch ORDER BY count(*) DESC, repo, branch LIMIT 1;
  ELSE
    -- SCOPED: only if v_want is one of the account's OWN repos (has code_node rows under the pinned account).
    -- Pick THAT repo's busiest branch (same node-count tie-break). A foreign/cross-account repo selects no row
    -- → v_repo stays NULL → benign empty below (never another tenant's data, never an error).
    SELECT repo, branch INTO v_repo, v_branch
      FROM core.code_node WHERE account_id = v_account AND repo = v_want
     GROUP BY repo, branch ORDER BY count(*) DESC, repo, branch LIMIT 1;
  END IF;
  IF v_repo IS NULL THEN RETURN v_empty; END IF;               -- no graph ingested / foreign repo → benign empty
  SELECT jsonb_build_object(
    'repo', v_repo,
    -- coupled: top-8 co-change pairs by lift at the floor (lift>=2 AND co>=3). a<b is already the stored
    -- canonical order (co_change_canonical CHECK). pct = round(strength*100) — the rendered conditional-probability.
    'coupled', COALESCE((SELECT jsonb_agg(jsonb_build_object(
        'a', path_a, 'b', path_b, 'pct', round(strength * 100)::int,
        -- HONEST DENOMINATOR: support_n = co (BOTH changed); observed_m = the UNION n_a+n_b-co (EITHER changed) =
        -- the commits in which the pair was observable. GREATEST(.,co) is a belt: the union can never be below the
        -- intersection, so a malformed stored row (n_a/n_b somehow < co) still yields a self-consistent "N of M".
        'support_n', co, 'observed_m', GREATEST(co, n_a + n_b - co)) ORDER BY lift DESC, co DESC, path_a, path_b)
      FROM (SELECT path_a, path_b, strength, lift, co, n_a, n_b FROM core.co_change
              WHERE account_id = v_account AND repo = v_repo
                AND lift >= 2::real AND co >= 3
             ORDER BY lift DESC, co DESC, path_a, path_b LIMIT 8) cc), '[]'::jsonb),
    -- central: top-8 internal FILE nodes by in-repo import fan-in. dst resolved to a real file node on THIS
    -- coordinate (pin branch like split_candidates' fan-in, so a same-named file on another branch can't bleed),
    -- which drops stdlib/external import targets. dependents = COUNT(DISTINCT src) = how many own files import it.
    'central', COALESCE((SELECT jsonb_agg(jsonb_build_object(
        'path', path, 'dependents', dependents) ORDER BY dependents DESC, path)
      FROM (SELECT n.path AS path, count(DISTINCT e.src)::int AS dependents
              FROM core.code_edge e
              JOIN core.code_node n
                ON n.account_id=e.account_id AND n.repo=e.repo AND n.branch=e.branch
               AND n.node_kind='file'
               AND COALESCE(
                     n.semantic_key,
                     core._node_semantic_key(
                       n.node_kind,n.node_id,n.path,n.name,n.canonical_key
                     )
                   )=COALESCE(e.semantic_dst_key,core._semantic_ref_key(e.dst))
             WHERE e.account_id = v_account AND e.repo = v_repo AND e.branch = v_branch
               AND e.edge_kind = 'imports' AND e.reference_status IS NULL
             GROUP BY n.path
             ORDER BY count(DISTINCT e.src) DESC,n.path LIMIT 8) f), '[]'::jsonb)
  ) INTO v_result;
  RETURN v_result;
EXCEPTION WHEN OTHERS THEN
  -- FAIL-SAFE: a broken structural read must never break the dashboard — return the benign empty shape.
  RETURN jsonb_build_object('repo', NULL, 'coupled', '[]'::jsonb, 'central', '[]'::jsonb);
END $$;
ALTER FUNCTION core.graph_insights_for_installation(text, text) OWNER TO veripsa_migrator;
REVOKE ALL ON FUNCTION core.graph_insights_for_installation(text, text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION core.graph_insights_for_installation(text, text) TO example_platform_reader, veripsa_app;

-- branch_surface: the MAIN panel — the recorded coordinates (graph_version) + recent pushes (event KIND
-- 'push'). HONEST-EMPTY: empty arrays until git-sync has recorded something (the view shows no dead box).
CREATE OR REPLACE FUNCTION core.branch_surface() RETURNS jsonb
    LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path TO 'core','pg_catalog' AS $$
DECLARE v_account text; v_result jsonb;
BEGIN
  SELECT account INTO v_account FROM core.resolve_session_identity() AS r(agent,account);
  PERFORM set_config('core.current_account', v_account, true);
  SELECT jsonb_build_object(
    'coordinates', COALESCE((SELECT jsonb_agg(jsonb_build_object(
        'repo', repo, 'branch', branch, 'node_count', node_count, 'edge_count', edge_count,
        'commit_sha', commit_sha, 'ingested_at', ingested_at) ORDER BY ingested_at DESC)
      FROM core.graph_version WHERE account_id=v_account), '[]'::jsonb),
    'pushes', COALESCE((SELECT jsonb_agg(jsonb_build_object(
        'repo', repo, 'branch', branch, 'commit_sha', commit_sha, 'by', core.agent_name(agent_id),
        'model', model, 'at', occurred_at) ORDER BY occurred_at DESC)
      FROM (SELECT * FROM core.event WHERE account_id=v_account AND kind='push' ORDER BY occurred_at DESC LIMIT 20) p), '[]'::jsonb)
  ) INTO v_result;
  RETURN v_result;
END $$;
ALTER FUNCTION core.branch_surface() OWNER TO veripsa_migrator;

-- graph_freshness_surface: the FRESHNESS lens — for each recorded coordinate, the STORED main-graph commit_sha
-- and how OLD it is (age in seconds since the last ingest). This is the DB-only half of the freshness signal
-- the /healthz + status surfaces expose and the watchdog samples: cheap (one bounded read over graph_version,
-- no GitHub call), content-free (a commit sha is public git metadata + a timestamp). The "is it BEHIND main
-- HEAD" half needs main's current HEAD (a GitHub read), so the App joins this with repo_default_branch_head
-- (server.graph_freshness). max_age_seconds is the staleness of the OLDEST coordinate (the worst case the
-- freshness alert watches). HONEST-EMPTY: empty list + null max until something has been ingested.
CREATE OR REPLACE FUNCTION core.graph_freshness_surface() RETURNS jsonb
    LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path TO 'core','pg_catalog' AS $$
DECLARE v_account text; v_result jsonb;
BEGIN
  SELECT account INTO v_account FROM core.resolve_session_identity() AS r(agent,account);
  PERFORM set_config('core.current_account', v_account, true);
  SELECT jsonb_build_object(
    'coordinates', COALESCE((SELECT jsonb_agg(jsonb_build_object(
        'repo', repo, 'branch', branch, 'commit_sha', commit_sha,
        'node_count', node_count, 'edge_count', edge_count,
        'ingested_at', ingested_at,
        'age_seconds', GREATEST(0, round(EXTRACT(EPOCH FROM (now() - ingested_at)))::bigint)
      ) ORDER BY ingested_at ASC)                       -- oldest (stalest) first
      FROM core.graph_version WHERE account_id=v_account), '[]'::jsonb),
    'coordinate_count', (SELECT count(*)::int FROM core.graph_version WHERE account_id=v_account),
    'max_age_seconds', (SELECT GREATEST(0, round(EXTRACT(EPOCH FROM (now() - min(ingested_at))))::bigint)
                          FROM core.graph_version WHERE account_id=v_account)
  ) INTO v_result;
  RETURN v_result;
END $$;
ALTER FUNCTION core.graph_freshness_surface() OWNER TO veripsa_migrator;

-- (retired) mint/revoke/list_mcp_tokens lived here — the MCP-era per-agent token path. Veripsa is a
-- GitHub App: the App authenticates as veripsa_app and acts on behalf of PR authors (delegation); buyers
-- never mint per-agent tokens and nothing ever resolved these hashes. Removed with MCP. (chore/retire-mcp-token)

-- list_store_connections: the attached edges (the door targets). Content-free identities only.
CREATE OR REPLACE FUNCTION core.list_store_connections() RETURNS jsonb
    LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path TO 'core','pg_catalog' AS $$
DECLARE v_account text;
BEGIN
  SELECT account INTO v_account FROM core.resolve_session_identity() AS r(agent,account);
  PERFORM set_config('core.current_account', v_account, true);
  RETURN COALESCE((SELECT jsonb_agg(jsonb_build_object(
      'connection_id', connection_id, 'provider', provider, 'target', target,
      'attach_prefix', attach_prefix, 'state', connection_state, 'connected_at', connected_at) ORDER BY connected_at DESC)
    FROM core.store_connection WHERE account_id=v_account AND connection_state='active'), '[]'::jsonb);
END $$;
ALTER FUNCTION core.list_store_connections() OWNER TO veripsa_migrator;

-- account_surface: the account page's header — account meta + its seats (agents: plate · maker · rider)
-- + the standing counts. credential is read owner-bypass (ENABLE-not-FORCE); account/agent tenant-pinned.
CREATE OR REPLACE FUNCTION core.account_surface() RETURNS jsonb
    LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path TO 'core','pg_catalog' AS $$
DECLARE v_account text; v_result jsonb;
BEGIN
  SELECT account INTO v_account FROM core.resolve_session_identity() AS r(agent,account);
  PERFORM set_config('core.current_account', v_account, true);
  SELECT jsonb_build_object(
    'account', v_account,
    'display_name', (SELECT display_name FROM core.account WHERE account_id=v_account),
    'plan', (SELECT plan FROM core.account WHERE account_id=v_account),
    'state', (SELECT account_state FROM core.account WHERE account_id=v_account),
    'created_at', (SELECT created_at FROM core.account WHERE account_id=v_account),
    'seats', COALESCE((SELECT jsonb_agg(jsonb_build_object(
        'agent_id', a.agent_id, 'name', COALESCE(a.display_name,a.agent_id), 'maker', a.default_model,
        'rider', a.operator, 'kind', a.agent_kind, 'state', a.agent_state,
        'role', (SELECT role_name FROM core.credential c WHERE c.agent_id=a.agent_id AND c.account_id=v_account AND c.credential_state='active' LIMIT 1)
      ) ORDER BY a.agent_id) FROM core.agent a WHERE a.account_id=v_account), '[]'::jsonb),
    'counts', jsonb_build_object(
      'agents',      (SELECT count(*)::int FROM core.agent WHERE account_id=v_account),
      'connections', (SELECT count(*)::int FROM core.store_connection WHERE account_id=v_account AND connection_state='active'))
  ) INTO v_result;
  RETURN v_result;
END $$;
ALTER FUNCTION core.account_surface() OWNER TO veripsa_migrator;

-- notifications_surface: the in-app feed — the account's own recent recorded facts (collisions held,
-- pushes), each content-free. HONEST-EMPTY (no fabricated notices). Tenant-pinned.
CREATE OR REPLACE FUNCTION core.notifications_surface() RETURNS jsonb
    LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path TO 'core','pg_catalog' AS $$
DECLARE v_account text;
BEGIN
  SELECT account INTO v_account FROM core.resolve_session_identity() AS r(agent,account);
  PERFORM set_config('core.current_account', v_account, true);
  RETURN jsonb_build_object(
    'items', COALESCE((SELECT jsonb_agg(jsonb_build_object(
        'kind', kind, 'agent', core.agent_name(agent_id), 'counterparty', core.agent_name(counterparty_agent),
        'repo', repo, 'branch', branch, 'path', path, 'at', occurred_at) ORDER BY occurred_at DESC)
      FROM (SELECT * FROM core.event WHERE account_id=v_account ORDER BY occurred_at DESC LIMIT 50) e), '[]'::jsonb)
  );
END $$;
ALTER FUNCTION core.notifications_surface() OWNER TO veripsa_migrator;

GRANT EXECUTE ON FUNCTION core.directory_surface(text,text) TO veripsa_reader, veripsa_writer, veripsa_demo_steward;
GRANT EXECUTE ON FUNCTION core.account_coverage_surface() TO veripsa_reader, veripsa_writer, veripsa_demo_steward;
GRANT EXECUTE ON FUNCTION core.branch_surface() TO veripsa_reader, veripsa_writer, veripsa_demo_steward;
-- the watchdog samples freshness as the App (veripsa_app inherits veripsa_writer); the console reads it.
GRANT EXECUTE ON FUNCTION core.graph_freshness_surface() TO veripsa_reader, veripsa_writer, veripsa_demo_steward;
GRANT EXECUTE ON FUNCTION core.list_store_connections() TO veripsa_reader, veripsa_writer, veripsa_demo_steward;
-- LEAST-PRIVILEGE (audit iter-5 P3): strip the PUBLIC-EXECUTE default so a non-tenant role (billing/platform-
-- reader) can't reach the account console surface; the GRANT names exactly the tenant roles (App inherits writer).
REVOKE EXECUTE ON FUNCTION core.account_surface() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION core.account_surface() TO veripsa_reader, veripsa_writer, veripsa_demo_steward;
GRANT EXECUTE ON FUNCTION core.notifications_surface() TO veripsa_reader, veripsa_writer, veripsa_demo_steward;
-- export_my_account: the buyer's own content-free record (account meta + standing + recent facts), as
-- one jsonb document — the GDPR-style "give me my data" export. Everything Veripsa holds is content-free.
CREATE OR REPLACE FUNCTION core.export_my_account() RETURNS jsonb
    LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path TO 'core','pg_catalog' AS $$
DECLARE v_account text;
BEGIN
  SELECT account INTO v_account FROM core.resolve_session_identity() AS r(agent,account);
  PERFORM set_config('core.current_account', v_account, true);
  RETURN jsonb_build_object(
    'account', core.account_surface(),
    'standing', core.profile_surface(),
    'statements', core.meaning_surface(),
    'recent_facts', core.notifications_surface(),
    'connections', core.list_store_connections(),
    'exported_at', now());
END $$;
ALTER FUNCTION core.export_my_account() OWNER TO veripsa_migrator;

-- close_my_account_with_authority: 退会. Mark the account closed + revoke every credential (access
-- stops). The content-free immutable ledger (event/statement history) is RETAINED by design — a closed
-- account's recorded facts remain (the records-not-correctness substrate), only ACCESS is revoked.
CREATE OR REPLACE FUNCTION core.close_my_account_with_authority() RETURNS jsonb
    LANGUAGE plpgsql SECURITY DEFINER SET search_path TO 'core','pg_catalog' AS $$
DECLARE v_agent text; v_account text;
BEGIN
  SELECT agent, account INTO v_agent, v_account FROM core.establish_session_write_context() AS c(agent,account);
  PERFORM core.mark_governed_write('account');
  UPDATE core.account SET account_state='closed' WHERE account_id=v_account;
  PERFORM core.mark_governed_write('agent');
  UPDATE core.agent SET agent_state='stopped' WHERE account_id=v_account;
  -- credential is the bootstrap table (no forgery trigger; owner writes it). Revoke every seat role.
  UPDATE core.credential SET credential_state='revoked' WHERE account_id=v_account AND credential_state='active';
  RETURN jsonb_build_object('ok', true, 'account', v_account, 'state', 'closed');
END $$;
ALTER FUNCTION core.close_my_account_with_authority() OWNER TO veripsa_migrator;

-- OWNER-write gates (the console's owner actions): granted to the STEWARD too — the owner console
-- connects as the steward and performs account config (connect store · set policy · close). The TRAFFIC
-- gates (claim/push/statement/collision/ingest) stay writer-only (the agents).
-- LEAST-PRIVILEGE (audit iter-5 P3): strip the PUBLIC-EXECUTE default on the data-export surface (GDPR portability)
-- so a non-tenant role (billing/platform-reader) can't reach it; the GRANT names exactly the tenant roles.
REVOKE EXECUTE ON FUNCTION core.export_my_account() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION core.export_my_account() TO veripsa_reader, veripsa_writer, veripsa_demo_steward;
REVOKE EXECUTE ON FUNCTION core.close_my_account_with_authority() FROM PUBLIC;  -- strip the PUBLIC default; only the granted writer/steward below
GRANT EXECUTE ON FUNCTION core.close_my_account_with_authority() TO veripsa_writer, veripsa_demo_steward;

-- db_usage_surface: the OPERATOR's "is the whole Postgres getting big/expensive?" lens — the input to the
-- db_usage_high alert (alerts.evaluate_db_usage), the AWS-billing-alarm equivalent for our Render Postgres.
-- DISTINCT LAYER from the per-tenant free-line quota (owner_cost_surface / the core quota gate): those watch
-- ONE buyer's footprint inside its RLS wall; THIS watches the ENTIRE shared instance — the silent disk-fill /
-- surprise-bill that no per-tenant signal can see (every tenant can be under its own line while the SUM fills
-- the disk). Whole-DB, so NO per-account pin is needed (it reads pg_catalog size aggregates, not tenant rows).
--   • db_total_bytes = pg_database_size(current_database()) — the one cheap whole-DB measure.
--   • cap_bytes / pct_used = the configured storage cap (p_cap_mb, the operator's ceiling — Render plan size or
--     a chosen budget) and where the DB sits against it (0 cap = "uncapped", pct_used null — never a /0).
--   • top_tables = the largest N relations by pg_total_relation_size (incl. indexes + TOAST), schema-qualified
--     relname + byte size + share-of-DB %. CONTENT-FREE: a RELATION NAME is the App's own schema identifier
--     (public DDL — `core.event`, `core.code_edge`, …), never a row, a path, an id, or any customer datum. We
--     never read a row to size a table; pg_total_relation_size is a catalog stat. Capped at p_top (default 10).
-- HONEST: a tier or DB it cannot stat simply yields what it could read (size is always available on the current
-- DB). Read-only (size aggregates only — no writes). SECURITY DEFINER (migrator owner) so the App role, which
-- cannot read pg_catalog ownership freely, gets a stable bounded read; REVOKEd from PUBLIC + every tenant role,
-- GRANTed ONLY to veripsa_app (the host service identity that runs the watchdog) — exactly like owner_cost_surface.
-- This is operator-only by design: a buyer seat has no business seeing the whole instance's size.
CREATE OR REPLACE FUNCTION core.db_usage_surface(p_cap_mb int DEFAULT 0, p_top int DEFAULT 10) RETURNS jsonb
    LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path TO 'core','pg_catalog' AS $$
DECLARE
  v_total bigint; v_cap_bytes bigint; v_top int; v_tables jsonb;
BEGIN
  -- clamp the inputs defensively (a caller could pass a negative/huge top or cap): a cap below 0 means
  -- "uncapped" (treated as 0 → pct null); top is bounded to [1, 200] so the row list can never blow up.
  v_top := LEAST(GREATEST(COALESCE(p_top, 10), 1), 200);
  v_cap_bytes := GREATEST(COALESCE(p_cap_mb, 0), 0)::bigint * 1024 * 1024;
  v_total := pg_database_size(current_database());

  -- the largest relations by TOTAL size (heap + indexes + TOAST). pg_class over the live DB; ordinary tables
  -- only (relkind 'r'/'p' = table / partitioned table). relname is the App's own schema identifier — never a row.
  SELECT COALESCE(jsonb_agg(t ORDER BY (t->>'bytes')::bigint DESC), '[]'::jsonb) INTO v_tables
  FROM (
    SELECT jsonb_build_object(
             'table', n.nspname || '.' || c.relname,
             'bytes', pg_total_relation_size(c.oid),
             'pct_of_db', CASE WHEN v_total > 0
                               THEN round(pg_total_relation_size(c.oid)::numeric * 100 / v_total, 1)
                               ELSE 0 END
           ) AS t
    FROM pg_class c
    JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE c.relkind IN ('r', 'p')
      AND n.nspname NOT IN ('pg_catalog', 'information_schema')
    ORDER BY pg_total_relation_size(c.oid) DESC
    LIMIT v_top
  ) top;

  RETURN jsonb_build_object(
    'db_total_bytes', v_total,
    'cap_mb',         GREATEST(COALESCE(p_cap_mb, 0), 0),
    'cap_bytes',      v_cap_bytes,
    -- pct_used: where the DB sits against the configured ceiling. NULL when uncapped (cap 0) — the caller must
    -- not divide by zero; an uncapped instance simply cannot cross a percentage and the alert stays quiet.
    'pct_used',       CASE WHEN v_cap_bytes > 0
                          THEN round(v_total::numeric * 100 / v_cap_bytes, 1)
                          ELSE NULL END,
    'top_tables',     v_tables,
    'measured_at',    now());
END $$;
ALTER FUNCTION core.db_usage_surface(int,int) OWNER TO veripsa_migrator;

-- OPERATOR-ONLY lock (the SAME wall as owner_cost_surface): REVOKE from PUBLIC so no tenant role inherits it,
-- GRANT only to veripsa_app (the host identity the watchdog runs as). A buyer seat gets permission-denied — the
-- whole-instance size is the operator's concern, not a tenant's, and exposing it would leak cross-tenant volume.
REVOKE EXECUTE ON FUNCTION core.db_usage_surface(int,int) FROM PUBLIC;
GRANT  EXECUTE ON FUNCTION core.db_usage_surface(int,int) TO veripsa_app;  -- OPERATOR DB-USAGE LENS: whole-instance size vs the configured cap (the AWS-billing-alarm equivalent)
