-- PHASE 4b — SOCIAL (the face). follow (the graph) + standing-from-artifacts (profile) + the public
-- MOVEMENT feed (discover). The face is the movement of people BEFORE the artifact — the layer GitHub
-- (which only sees pushed artifacts) structurally can't take. Standing is FACTS (built/held/stated),
-- not vanity. Cross-account PUBLIC reads via a permissive SELECT policy on the opt-in 'public' rows.
-- ============================================================================================

-- PUBLIC readability: a row marked visibility='public' is readable ACROSS accounts (the social feed).
-- Permissive (OR'd with tenant_isolation); SELECT-only — writes stay tenant-scoped. Default is private,
-- so nothing is public until the owner opts a record in.
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_policy WHERE polrelid = 'core.event'::regclass AND polname = 'public_readable') THEN
    CREATE POLICY public_readable ON core.event FOR SELECT USING (visibility = 'public');
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_policy WHERE polrelid = 'core.statement'::regclass AND polname = 'public_readable') THEN
    CREATE POLICY public_readable ON core.statement FOR SELECT USING (visibility = 'public');
  END IF;
END $$;

-- follow: the social graph. You manage your OWN follows (follower_account = your account).
CREATE TABLE IF NOT EXISTS core.follow (
    follower_account text NOT NULL,
    followed_account text NOT NULL,
    followed_at timestamptz DEFAULT now() NOT NULL,
    CONSTRAINT follow_pkey PRIMARY KEY (follower_account, followed_account),
    CONSTRAINT follow_not_self CHECK (follower_account <> followed_account)
);
DO $$
BEGIN
  IF EXISTS (
    SELECT 1
      FROM pg_class c
      JOIN pg_namespace n ON n.oid = c.relnamespace
     WHERE n.nspname = 'core'
       AND c.relname = 'follow'
       AND c.relowner <> 'veripsa_migrator'::regrole
  ) THEN
    ALTER TABLE core.follow OWNER TO veripsa_migrator;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_class WHERE oid = 'core.follow'::regclass AND relrowsecurity) THEN
    ALTER TABLE core.follow ENABLE ROW LEVEL SECURITY;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_class WHERE oid = 'core.follow'::regclass AND relforcerowsecurity) THEN
    ALTER TABLE ONLY core.follow FORCE ROW LEVEL SECURITY;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_policy WHERE polrelid = 'core.follow'::regclass AND polname = 'tenant_isolation') THEN
    CREATE POLICY tenant_isolation ON core.follow USING (follower_account = current_setting('core.current_account', true)) WITH CHECK (follower_account = current_setting('core.current_account', true));
  END IF;
END $$;
-- RIGHT-TO-DELETION (account erasure) on the social graph: a permissive DELETE policy (OR'd with tenant_isolation)
-- that admits a row when the erasure token (armed ONLY by erase_account_with_authority for the account being
-- erased) names EITHER side of the edge. So a full account hard-delete removes BOTH the rows this account follows
-- AND the rows OTHER tenants have on this account (so no retained tenant is left dangling a 'followed_account =
-- <erased>' reference) — WITHOUT unsetting RLS or being able to touch any edge that does not name the erased
-- account. DELETE-only; the token is un-forgeable (migrator-armed, account-scoped), so this cannot erase a live
-- account's follows. Non-erase sessions never arm the token, so it is inert in normal operation.
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_policy WHERE polrelid = 'core.follow'::regclass AND polname = 'follow_erasable') THEN
    CREATE POLICY follow_erasable ON core.follow FOR DELETE USING (
      NULLIF(current_setting('core.account_erasure_token', true), '') IS NOT NULL
      AND current_setting('core.account_erasure_token', true) IN (follower_account, followed_account)
    );
  END IF;
END $$;
-- READ-VISIBILITY for the erase DELETE (Postgres RLS: a DELETE must first SELECT/lock the target row, so a
-- FOR DELETE policy alone never SEES the inbound cross-tenant row — tenant_isolation [follower_account=current_account]
-- hides the INBOUND edge another tenant holds ON this account, so the unqualified DELETE above matched 0 inbound rows
-- and silently left the dangling 'followed_account=<erased>' reference [a right-to-deletion completeness gap]). This
-- paired FOR SELECT policy makes EXACTLY the same token-named edges READABLE during the erase, so the DELETE can lock
-- and remove them. Same un-forgeable, migrator-armed, account-scoped token + identical predicate as follow_erasable,
-- so it is inert outside an erase and admits nothing the DELETE policy would not.
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_policy WHERE polrelid = 'core.follow'::regclass AND polname = 'follow_erasable_read') THEN
    CREATE POLICY follow_erasable_read ON core.follow FOR SELECT USING (
      NULLIF(current_setting('core.account_erasure_token', true), '') IS NOT NULL
      AND current_setting('core.account_erasure_token', true) IN (follower_account, followed_account)
    );
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM pg_trigger
     WHERE tgrelid = 'core.follow'::regclass
       AND tgname = 'trg_governed_follow'
       AND NOT tgisinternal
  ) THEN
    CREATE TRIGGER trg_governed_follow BEFORE INSERT OR UPDATE ON core.follow FOR EACH ROW EXECUTE FUNCTION core.assert_governed_write();
  END IF;
END $$;

CREATE OR REPLACE FUNCTION core.follow_account_with_authority(p_followed text) RETURNS void
    LANGUAGE plpgsql SECURITY DEFINER SET search_path TO 'core','pg_catalog' AS $$
DECLARE v_agent text; v_account text;
BEGIN
  SELECT agent, account INTO v_agent, v_account FROM core.establish_session_write_context() AS c(agent,account);
  IF p_followed = v_account THEN RETURN; END IF;
  PERFORM core.mark_governed_write('follow');
  INSERT INTO core.follow(follower_account, followed_account) VALUES (v_account, p_followed) ON CONFLICT DO NOTHING;
END $$;
ALTER FUNCTION core.follow_account_with_authority(text) OWNER TO veripsa_migrator;

-- profile_surface: the caller's own STANDING, derived from REAL artifacts (built/held/stated/pushed) —
-- facts, never vanity. Content-free counts.
CREATE OR REPLACE FUNCTION core.profile_surface() RETURNS jsonb
    LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path TO 'core','pg_catalog' AS $$
DECLARE v_account text;
BEGIN
  SELECT account INTO v_account FROM core.resolve_session_identity() AS r(agent,account);
  PERFORM set_config('core.current_account', v_account, true);
  RETURN jsonb_build_object(
    'account', v_account,
    'standing', jsonb_build_object(
      'agents',          (SELECT count(DISTINCT agent_id)::int FROM core.claim WHERE account_id=v_account),
      'active_claims',   (SELECT count(*)::int FROM core.claim WHERE account_id=v_account AND claim_state='active'),
      'pushes',          (SELECT count(*)::int FROM core.event WHERE account_id=v_account AND kind='push'),
      'collisions_held', (SELECT count(*)::int FROM core.event WHERE account_id=v_account AND kind='collision_held'),
      'statements',      (SELECT count(*)::int FROM core.statement WHERE account_id=v_account AND superseded=false)
    ),
    'following', (SELECT count(*)::int FROM core.follow WHERE follower_account=v_account)
  );
END $$;
ALTER FUNCTION core.profile_surface() OWNER TO veripsa_migrator;

-- discover_surface: the public MOVEMENT — recent PUBLIC activity (pushes + statements) ACROSS accounts.
-- Content-free (kind/repo/path/agent/account/at). The "who's building with AI, live" feed.
CREATE OR REPLACE FUNCTION core.discover_surface() RETURNS jsonb
    LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path TO 'core','pg_catalog' AS $$
DECLARE v_account text;
BEGIN
  SELECT account INTO v_account FROM core.resolve_session_identity() AS r(agent,account);
  PERFORM set_config('core.current_account', v_account, true);  -- public_readable still admits public rows cross-account
  RETURN jsonb_build_object(
    'feed', COALESCE((SELECT jsonb_agg(x.obj ORDER BY x.at DESC) FROM (
        SELECT e.occurred_at AS at, jsonb_build_object('kind', e.kind, 'account', e.account_id, 'agent', e.agent_id, 'repo', e.repo, 'path', e.path, 'at', e.occurred_at) AS obj
        FROM core.event e WHERE e.visibility='public'
        UNION ALL
        SELECT s.stated_at AS at, jsonb_build_object('kind','statement', 'account', s.account_id, 'agent', s.agent_id, 'repo', s.about_repo, 'path', s.about_path, 'utterance', s.utterance, 'at', s.stated_at) AS obj
        FROM core.statement s WHERE s.visibility='public'
        ORDER BY at DESC LIMIT 50) x), '[]'::jsonb)
  );
END $$;
ALTER FUNCTION core.discover_surface() OWNER TO veripsa_migrator;

-- follow_account_with_authority is a write-path gate fn: SECURITY DEFINER (runs as the migrator owner),
-- derives agent+account from the CONNECTION identity, never from an arg. Postgres grants EXECUTE to PUBLIC by
-- default on CREATE FUNCTION, so without this REVOKE the GRANT below is misleading and the fn stays callable by
-- every role (incl. veripsa_reader, a real connecting identity). Same stray-PUBLIC-grant class closed in
-- 30_gate.sql / 40_surfaces.sql / 50_records.sql. profile_surface / discover_surface are intentionally-broad
-- READ surfaces (discover_surface is the cross-account PUBLIC feed by design), so they are not revoked.
REVOKE EXECUTE ON FUNCTION core.follow_account_with_authority(text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION core.follow_account_with_authority(text) TO veripsa_writer, veripsa_demo_steward;
GRANT EXECUTE ON FUNCTION core.profile_surface() TO veripsa_reader, veripsa_writer, veripsa_demo_steward;
GRANT EXECUTE ON FUNCTION core.discover_surface() TO veripsa_reader, veripsa_writer, veripsa_demo_steward;

-- _is_test_path: is this a TEST / spec / examples file? Used by the PROD→TEST asymmetry guard in
-- _claim_adjacency's out_adj/in_adj (a non-test file calling a name defined ONLY in a test file is a
-- coincidental ubiquitous-method-name match, not a real dependency — production code does not depend on a
-- test file). Two ways a path is a test: (1) a '/'-bounded test/spec/examples SEGMENT, OR (2) a test/spec
-- BASENAME pattern. RECALL-SAFE / precision-first: every pattern is delimiter-anchored so a PRODUCTION file
-- merely CONTAINING the letters (latest.java, contest.py, manifest.py, attestation.go) is NOT matched — the
-- CamelCase Java/C# 'Test.'/'Spec.' suffix is matched case-SENSITIVELY so 'latest.java' (lowercase) can't
-- false-match. Mirrors tests/audit_repo.py _is_test_path (segments) + the extractor's _test.go lesson
-- (_cg_resolve). Content-free (a path shape, never bytes). IMMUTABLE: pure function of the path string.
CREATE OR REPLACE FUNCTION core._is_test_path(p_path text) RETURNS boolean
    LANGUAGE sql IMMUTABLE AS $ISTEST$
  SELECT CASE WHEN p_path IS NULL OR p_path = '' THEN false ELSE (
    -- (1) a test/spec/examples directory SEGMENT (case-insensitive on the segment word)
    lower(p_path) ~ '(^|/)(tests?|specs?|__tests__|examples?|testing)/'
    OR
    -- (2) a test/spec BASENAME pattern. Lowercase set is delimiter-anchored (_test. / .test. / test_ prefix /
    -- _spec. / .spec.); the CamelCase Java/C#/Scala suffix (FooTest.java, FooTests.cs, FooSpec.scala) is matched
    -- case-SENSITIVELY ('Tests?\.' / 'Specs?\.') so a lowercase 'latest.java' never matches.
    (regexp_replace(p_path, '^.*/', '') ~ '(^test_|_test\.|\.test\.|_spec\.|\.spec\.)')
    OR
    (regexp_replace(p_path, '^.*/', '') ~ '(Tests?\.|Specs?\.)')
  ) END
$ISTEST$;
ALTER FUNCTION core._is_test_path(text) OWNER TO veripsa_migrator;
REVOKE ALL ON FUNCTION core._is_test_path(text) FROM PUBLIC;

-- ============================================================================================
-- _claim_adjacency — THE A→B BLAST-RADIUS ENGINE (ONE source of truth, the moat math). For a coordinate,
-- for each ACTIVE-claimed path f (its agent f_agent), its STRUCTURAL neighbor nbr: another file that calls
-- a symbol f defines (in_adj: nbr is downstream of f), or that f calls (out_adj: f depends on nbr). This
-- is reachability restricted to claimed files BEFORE symbol resolution (FAST — never the exponential path
-- enumeration that made the old blast_exposure 15s). Both consumers share it so the prediction can never
-- drift between the working-branch view (contention_surface) and the main-protection view
-- (main_impact_surface). Content-free (paths only). Account is passed explicitly + filtered.
-- ============================================================================================
-- NOTE: drop first — CREATE OR REPLACE cannot change a function's RETURNS columns (we add a trailing `dir`).
-- The 3 callers select BY NAME (f, f_agent, f_change_id, nbr), so the trailing column is backward-compatible.
DROP FUNCTION IF EXISTS core._claim_adjacency(text,text,text);
CREATE OR REPLACE FUNCTION core._claim_adjacency(p_account text, p_repo text, p_branch text)
    -- `dir` = the COUPLING DIRECTION of nbr relative to the edited file f. The TRUE blast radius is only
    -- 'down'+'shared' (a consumer wanting downstream filters on it); collision/contention detection ignores
    -- `dir` and stays UNDIRECTED (adjacency in EITHER direction = entangled — that is the moat):
    --   'down'   nbr is DOWNSTREAM of f (nbr calls/imports f → editing f can affect nbr)  = blast radius
    --   'up'     f depends on nbr (f calls/imports nbr → editing f cannot affect nbr)      = a dependency
    --   'shared' nbr shares a resource (table/config) with f (undirected coupling)
    RETURNS TABLE(f text, f_agent text, f_change_id text, nbr text, dir text)
    LANGUAGE sql STABLE SECURITY DEFINER SET search_path TO 'core','pg_catalog' AS $$
  WITH
  edited AS (
    SELECT DISTINCT target_path AS path, agent_id, change_id FROM core.claim
     WHERE account_id=p_account AND repo=p_repo AND branch=p_branch AND claim_state='active'),
  -- Calls/imports Edge.src is a code-file path. A generated Resource Node may
  -- legally share the same node_id string as a Git path; only `file` can
  -- resolve this endpoint, or adjacency could jump to the Resource Node's
  -- unrelated path. Resource coupling below continues to use Edge.src itself,
  -- so config_file producers remain represented without widening call/import
  -- review behavior.
  node_file AS (
    SELECT DISTINCT node_id, path,
           COALESCE(
             semantic_key,
             core._node_semantic_key(
               node_kind,node_id,path,name,canonical_key
             )
           ) AS sk
      FROM core.code_node
     WHERE account_id=p_account AND repo=p_repo AND branch=p_branch
       AND node_kind='file'
  ),
  -- CALL resolution by symbol name, tuned for PRECISION + RECALL (real-usage finding, not the toy gate):
  --   * skip DUNDERS (__init__/__repr__…) and a tiny stoplist of ubiquitous names — calling them is not
  --     coupling; resolving them would couple half the repo to one file (cry-wolf).
  --   * a name defined in ≤3 files FANS OUT to all of them (recall: `make_db` in 2 files now links both,
  --     where the old n=1 silently dropped it); a name in >3 files is too ambiguous → dropped (noise).
  --   * skip UNIVERSAL protocol / builtin / test-DSL method names (to_s, inspect, equals, include?, map, it,
  --     describe, before…). Calling one of these is NOT a code dependency — they are language/library protocol
  --     or test scaffolding present in every file, so resolving them couples half the repo to one definition
  --     (cry-wolf). Measured on real repos (sinatra Ruby call co-change was the weakest signal, dragged down by
  --     exactly these names). Domain names (charge, verify_user…) are untouched. Lowercased compare.
  -- MATERIALIZED (audit r2 — the SAME re-CTE-scan the sibling _dampened_adjacency already fixed, MISSED here):
  -- def_n/defs_ok below are GLOBAL same-name ambiguity counts (the n<=3 resolution discipline), re-referenced
  -- many times (edited_def_names, out_adj's defs_ok+def_n joins, in_adj, the import-confirmation NOT EXISTS).
  -- Non-materialized, Postgres re-evaluates this whole def/class scan per reference; at high in-flight counts
  -- (200 active claims over a 20k-node graph) that re-scan dominates (8.3s). Folding ONCE = sub-second. Result
  -- is byte-identical — these are global counts, NOT restricted to `edited`, so materializing changes nothing.
  defs AS MATERIALIZED (
    SELECT name AS nm, path,
           COALESCE(
             semantic_key,
             core._node_semantic_key(
               node_kind,node_id,path,name,canonical_key
             )
           ) AS sk
      FROM core.code_node
     WHERE account_id=p_account AND repo=p_repo AND branch=p_branch AND node_kind IN ('def','class') AND name IS NOT NULL
       AND name !~ '^__'
       AND lower(name) <> ALL (ARRAY[
         'main','run','setup','teardown','handle','dispatch','register','wrap',
         -- stringify / print / log
         'to_s','to_str','tostring','str','repr','inspect','format','print','println','puts','log','warn','debug','trace',
         -- equality / hash / compare
         'equals','eql?','hash','hashcode','compareto','compare','cmp',
         -- predicate / conversion (universal protocol)
         'empty?','blank?','present?','nil?','valid?','include?','contains','respond_to?','key?','has_key?',
         'to_a','to_h','to_sym','to_i','to_json',
         -- enumerable / collection protocol
         'map','each','collect','select','reject','filter','reduce','merge','flatten','zip',
         -- object lifecycle protocol (the clearly-generic ones; NOT new/create/build/find which can be domain)
         'freeze','dup','clone','tap','then','send','call','apply','yield',
         -- test DSL (scaffolding, never a dependency)
         'it','its','describe','context','before','after','around','expect','should','assert','refute','let','subject','mock','stub'
       ])),
  def_n AS MATERIALIZED (SELECT sk, count(DISTINCT path) AS n FROM defs GROUP BY sk),
  defs_ok AS MATERIALIZED (
    SELECT d.nm,d.path,d.sk FROM defs d JOIN def_n c ON c.sk=d.sk WHERE c.n<=3
  ),
  -- HUB DAMPENING (the file-level analogue of the >3-files symbol dampening above). A file DEPENDED ON by many
  -- others — imported by more than the hub cutoff (utils, config, types, a base class…) — is a HUB. Editing it
  -- is graph-adjacent to ALL its importers, so a hub-touching PR would be flagged as colliding with EVERY
  -- in-flight PR in its neighborhood: a wall of warnings that gets the whole product ignored (the noise death;
  -- "無努力・継続使用" demands warnings be worth reading). A hub is exactly where everyone ALREADY knows to
  -- coordinate — Veripsa's differentiated value is the NON-obvious cross-file coupling, not "everyone touches
  -- utils". So we EXCLUDE adjacency through a hub's depended-on endpoint (in BOTH the import and the call path).
  -- The DIRECT same-file collision (two PRs editing the hub) is UNAFFECTED — that still serializes via the
  -- exact-path lock. Cutoff is the `veripsa.hub_degree` GUC (default 8), tunable per deployment / test.
  -- PERF (audit:perf 2026-06-18): MATERIALIZED is load-bearing (same remedy as `hotspot` in 80_contention.sql).
  -- hub_files is a GROUP BY dst / HAVING count(DISTINCT src) scan over EVERY import edge, and it is referenced
  -- FOUR times below (out_adj, in_adj, imp_out, imp_in) as an anti-join. Without the keyword Postgres re-plans
  -- (and on large graphs re-scans) that whole fan-in aggregate at each reference; forcing a SINGLE evaluation
  -- then probing the small materialized result is semantics-IDENTICAL and avoids the repeated full scan.
  hub_files AS MATERIALIZED (
    SELECT n.path,
           COALESCE(
             n.semantic_key,
             core._node_semantic_key(
               n.node_kind,n.node_id,n.path,n.name,n.canonical_key
             )
           ) AS sk
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
     WHERE e.account_id=p_account AND e.repo=p_repo AND e.branch=p_branch
       AND e.edge_kind='imports' AND e.reference_status IS NULL
     GROUP BY n.path,COALESCE(
       n.semantic_key,
       core._node_semantic_key(n.node_kind,n.node_id,n.path,n.name,n.canonical_key)
     )
    HAVING count(DISTINCT e.src) >
      COALESCE(NULLIF(current_setting('veripsa.hub_degree', true), '')::int, 8)),
  -- HUB RESOURCE: the shared-resource analogue of hub_files. A TABLE / config key touched by more than the hub
  -- cutoff distinct files is a HOT resource (a `users` table half the repo reads). Coupling two files merely
  -- because they both touch it is the same noise explosion (res_adj below), so exclude adjacency through it. The
  -- DIRECT same-file collision is unaffected; a normal shared resource (touched by a few files) still couples.
  -- MATERIALIZED for the same reason as hub_files (a GROUP BY dst aggregate scan; folded once, probed cheaply).
  res_hubs AS MATERIALIZED (
    SELECT COALESCE(semantic_dst_key,core._semantic_ref_key(dst)) AS sk
      FROM core.code_edge
     WHERE account_id=p_account AND repo=p_repo AND branch=p_branch
       AND edge_kind IN ('queries','alters','reads_config') AND reference_status IS NULL
     GROUP BY COALESCE(semantic_dst_key,core._semantic_ref_key(dst))
    HAVING count(DISTINCT src) > COALESCE(NULLIF(current_setting('veripsa.hub_degree', true), '')::int, 8)),
  edited_nodes AS (
    SELECT DISTINCT cn.node_id, cn.path
      FROM core.code_node cn JOIN edited e ON e.path=cn.path
     WHERE cn.account_id=p_account AND cn.repo=p_repo AND cn.branch=p_branch
       AND cn.node_kind='file'),
  edited_def_names AS (
    SELECT DISTINCT d.nm,d.path,d.sk FROM defs_ok d JOIN edited e ON e.path=d.path
  ),
  out_adj AS (   -- edited file calls a symbol → the file(s) defining it. IMPORT-AWARE precision: if the caller
                 -- IMPORTS a definer, keep ONLY import-confirmed ones (drop the wrong same-name file). Otherwise
                 -- (the caller imports NONE of the definers) the call is import-unconfirmed: keep it ONLY when the
                 -- name is UNAMBIGUOUS (exactly ONE definer) — fanning a no-import call out to MULTIPLE same-name
                 -- definers manufactures false couplings (FALSE-COUPLING audit 2026-06-18: a `process()` reached
                 -- via a factory/DI couples `worker.py` to BOTH `payments/Gateway` and `images/ImageFilter`, two
                 -- unrelated files → a cry-wolf `warn`). A SINGLE definer with no import is the only place the name
                 -- lives → still kept (recall preserved). `def_n.n` is the same-name definer count.
    SELECT en.path AS ff, d.path AS nb FROM core.code_edge ce
      JOIN edited_nodes en ON en.node_id=ce.src
      JOIN defs_ok d
        ON d.sk=COALESCE(ce.semantic_dst_key,core._semantic_ref_key(ce.dst))
      JOIN def_n dn ON dn.sk=d.sk
     WHERE ce.account_id=p_account AND ce.repo=p_repo AND ce.branch=p_branch
       AND ce.edge_kind='calls' AND ce.reference_status IS NULL
       AND NOT EXISTS (SELECT 1 FROM hub_files hf WHERE hf.path=d.path)   -- don't couple to a HUB you merely call into (dst NOT NULL ⇒ ≡ NOT IN, better plan)
       AND (EXISTS (SELECT 1 FROM core.code_edge ie WHERE ie.account_id=p_account AND ie.repo=p_repo AND ie.branch=p_branch
                      AND ie.edge_kind='imports' AND ie.src=en.path AND ie.reference_status IS NULL
                      AND COALESCE(ie.semantic_dst_key,core._semantic_ref_key(ie.dst))
                          =core._semantic_ref_key(d.path))
            OR (dn.n = 1                                          -- import-unconfirmed → keep ONLY if unambiguous
                -- PERF/correctness identity (#982): dn.n=1 means every same-key defs_ok row has this selected
                -- d.path. If its import exists, the EXISTS arm above is already true; otherwise the former
                -- correlated NOT EXISTS(import to any same-key definer) was necessarily true. Avoid re-scanning
                -- the materialized global defs_ok set per call without changing this OR's truth table.
                -- PROD→TEST asymmetry guard (FALSE-COUPLING audit r3 2026-06-19): a single import-UNCONFIRMED
                -- definer that lives in a TEST file, called from a NON-test file, is almost always a coincidental
                -- ubiquitous-method-name match (.pop()/.end()/.is_a?/.Encode() that a test mock happens to define
                -- once) — production code does not depend on a test file. Drop it (measured 8–40 such impossible
                -- prod→test edges/repo on axios/gin/sinatra). Recall-safe: a test's REAL coupling to production is
                -- recorded from the TEST file's own outgoing call (in_adj), and an IMPORT-confirmed link survives
                -- (the EXISTS branch above). prod→prod single-definer couplings are UNAFFECTED.
                AND NOT (core._is_test_path(d.path) AND NOT core._is_test_path(en.path))
                -- BUILTIN/SHORT-NAME guard (FALSE-COUPLING audit 2026-06-21, measured on flask/nestjs/hugo:
                -- ~8–10% of shipped pairs, ~90–100% false within the class). The extractor records a method
                -- call by its BARE attribute tail (`Promise.all`→`all`, `JSON.parse`→`parse`, `dict.pop`→`pop`,
                -- `Object.assign`→`assign`, `strings.HasPrefix`→`hasprefix`, `sync.Once.Do`→`do`,
                -- `json.NewDecoder`→`newdecoder`). When that bare builtin/stdlib name matches a LONE class method
                -- of the same name (a single definer), the import-unconfirmed rule above false-couples the caller
                -- to that unrelated file. A single-LETTER target (`m`/`a`/`f`/`x`) is worse — it even couples
                -- cross-language (a Go test to a JS bundle). Drop BOTH classes from THIS un-imported branch ONLY:
                --   (1) char_length(ce.dst) <= 2  — a ≤2-char un-imported name is never a meaningful coupling anchor.
                --   (2) lower(ce.dst) ∈ the BUILTIN/STDLIB/container/IO/parse method-name set below.
                -- RECALL-SAFE — this guards the import-UNCONFIRMED branch ONLY: an IMPORT-CONFIRMED call to any of
                -- these names SURVIVES via the EXISTS(imports …) branch above (a file that imports a class + calls
                -- its `pop()` still couples). DOMAIN method names are untouched — and the noisy CRUD-domain verbs
                -- (create/build/find/new/save/update/delete/process/validate) plus the DOMAIN-INTENT verbs the bare
                -- name carries real cross-file intent for (get/set/add/read/write/parse/decode/encode/load — RECALL
                -- audit r4 2026-06-21: #365 over-reached and listed these, silently dropping genuine un-imported
                -- domain couplings; restored) are DELIBERATELY EXCLUDED from the set so a real un-imported domain
                -- coupling on them is preserved. The set keeps ONLY pure language/stdlib method tails (Array.pop,
                -- JSON.stringify, dict.keys, strings.HasPrefix, json.NewDecoder/Marshal, fmt.Sprintf, json.dumps).
                -- content-free (a name, not a body).
                AND char_length(ce.dst) > 2
                AND lower(ce.dst) <> ALL (ARRAY[
                  'pop','push','shift','unshift','slice','splice','all','any','stringify',
                  'keys','values','items','entries','has','join','split','assign',
                  'hasprefix','hassuffix','do','newdecoder','newencoder','marshal','unmarshal',
                  'sprintf','printf','fprintf','sprint','sprintln','close','open','dumps','loads'])))),
  in_adj AS (    -- another file calls a symbol the edited file defines (import-aware, symmetric with out_adj):
                 -- the CALLER (sf) imports the edited definer → keep (import-confirmed); else keep ONLY when the
                 -- name is UNAMBIGUOUS (one definer). A no-import call to a name with MULTIPLE same-name definers
                 -- is unresolvable → linking the caller to THIS definer is a guess (false coupling). Same root fix
                 -- as out_adj (FALSE-COUPLING audit 2026-06-18), kept symmetric so both PRs see the same verdict.
    SELECT edn.path AS ff, sf.path AS nb FROM core.code_edge ce
      JOIN edited_def_names edn
        ON edn.sk=COALESCE(ce.semantic_dst_key,core._semantic_ref_key(ce.dst))
      JOIN node_file sf ON sf.node_id=ce.src
      JOIN def_n dn ON dn.sk=edn.sk
     WHERE ce.account_id=p_account AND ce.repo=p_repo AND ce.branch=p_branch
       AND ce.edge_kind='calls' AND ce.reference_status IS NULL
       AND NOT EXISTS (SELECT 1 FROM hub_files hf WHERE hf.path=edn.path)   -- a HUB edited ↛ contested with all its callers
       AND (EXISTS (SELECT 1 FROM core.code_edge ie WHERE ie.account_id=p_account AND ie.repo=p_repo AND ie.branch=p_branch
                      AND ie.edge_kind='imports' AND ie.src=sf.path AND ie.reference_status IS NULL
                      AND COALESCE(ie.semantic_dst_key,core._semantic_ref_key(ie.dst))
                          =core._semantic_ref_key(edn.path))
            OR (dn.n = 1                                          -- import-unconfirmed → keep ONLY if unambiguous
                -- Same identity proof as out_adj: with one definer path, import-present is handled by the
                -- preceding EXISTS arm and import-absent needs no correlated defs_ok rescan.
                -- PROD→TEST asymmetry guard (symmetric with out_adj): the DEFINER here is the edited file (edn),
                -- the CALLER is sf. A NON-test sf calling a name defined only in a TEST edn is the same impossible
                -- prod→test coupling — drop it. Recall-safe + import-confirmed survives; prod→prod unaffected.
                AND NOT (core._is_test_path(edn.path) AND NOT core._is_test_path(sf.path))
                -- BUILTIN/SHORT-NAME guard (symmetric with out_adj — see its block for the full rationale +
                -- measured FP class). Drop, from THIS un-imported single-definer branch ONLY, a ≤2-char call
                -- target and a builtin/stdlib/container/IO/parse method name. Import-confirmed (EXISTS above)
                -- survives; domain names (incl. the excluded CRUD verbs) are untouched. content-free.
                AND char_length(ce.dst) > 2
                AND lower(ce.dst) <> ALL (ARRAY[
                  'pop','push','shift','unshift','slice','splice','all','any','stringify',
                  'keys','values','items','entries','has','join','split','assign',
                  'hasprefix','hassuffix','do','newdecoder','newencoder','marshal','unmarshal',
                  'sprintf','printf','fprintf','sprint','sprintln','close','open','dumps','loads'])))),
  -- IMPORTS resolve by PATH: the extractor resolves a module to the repo FILE it names, so an `imports`
  -- edge's dst is a file path. out: the edited file imports a target file (depends on it). in: another
  -- file imports the edited file (depends on it). The single highest-signal coupling (unambiguous).
  imp_out AS (
    SELECT e.path AS ff, fn.path AS nb FROM core.code_edge ce
      JOIN edited e ON e.path=ce.src
      JOIN core.code_node fn
        ON fn.account_id=p_account AND fn.repo=p_repo AND fn.branch=p_branch
       AND fn.node_kind='file'
       AND COALESCE(
             fn.semantic_key,
             core._node_semantic_key(
               fn.node_kind,fn.node_id,fn.path,fn.name,fn.canonical_key
             )
           )=COALESCE(ce.semantic_dst_key,core._semantic_ref_key(ce.dst))
     WHERE ce.account_id=p_account AND ce.repo=p_repo AND ce.branch=p_branch
       AND ce.edge_kind='imports' AND ce.reference_status IS NULL
       AND NOT EXISTS (SELECT 1 FROM hub_files hf WHERE hf.sk=COALESCE(ce.semantic_dst_key,core._semantic_ref_key(ce.dst)))),
  imp_in AS (
    SELECT e.path AS ff, ce.src AS nb FROM core.code_edge ce
      JOIN edited e
        ON core._semantic_ref_key(e.path)
           =COALESCE(ce.semantic_dst_key,core._semantic_ref_key(ce.dst))
     WHERE ce.account_id=p_account AND ce.repo=p_repo AND ce.branch=p_branch
       AND ce.edge_kind='imports' AND ce.reference_status IS NULL
       AND NOT EXISTS (SELECT 1 FROM hub_files hf WHERE hf.sk=COALESCE(ce.semantic_dst_key,core._semantic_ref_key(ce.dst)))),
  -- SHARED RESOURCE (schema/config): two files coupled by touching the SAME named resource — a code file
  -- that `queries` table T and a migration that `alters` T, or two files that `reads_config` the same key.
  -- This is the coupling a code call/import graph CANNOT see (there is no code edge between them) — the
  -- differentiated depth (Code + Schema + Config in one engine).
  res_adj AS (
    SELECT e.path AS ff, o.src AS nb FROM core.code_edge ce
      JOIN edited e ON e.path=ce.src
      JOIN core.code_edge o ON o.account_id=p_account AND o.repo=p_repo AND o.branch=p_branch
        AND o.edge_kind IN ('queries','alters','reads_config')
        AND o.reference_status IS NULL
        AND COALESCE(o.semantic_dst_key,core._semantic_ref_key(o.dst))
            =COALESCE(ce.semantic_dst_key,core._semantic_ref_key(ce.dst))
        AND o.src<>ce.src
     WHERE ce.account_id=p_account AND ce.repo=p_repo AND ce.branch=p_branch
       AND ce.edge_kind IN ('queries','alters','reads_config') AND ce.reference_status IS NULL
       AND NOT EXISTS (
         SELECT 1 FROM res_hubs rh
          WHERE rh.sk=COALESCE(ce.semantic_dst_key,core._semantic_ref_key(ce.dst))
       )),
  -- tag each source with its direction (in/imp_in = nbr depends on f = DOWN; out/imp_out = f depends on nbr =
  -- UP; res = SHARED). A pair coupled in both directions appears once per direction (UNION dedups per row); all
  -- consumers read it via DISTINCT / count(DISTINCT), so the per-direction rows never double-count.
  adj AS (SELECT ff, nb, 'up'::text     AS dir FROM out_adj WHERE ff<>nb
          UNION SELECT ff, nb, 'down'::text   AS dir FROM in_adj  WHERE ff<>nb
          UNION SELECT ff, nb, 'up'::text     AS dir FROM imp_out WHERE ff<>nb
          UNION SELECT ff, nb, 'down'::text   AS dir FROM imp_in  WHERE ff<>nb
          UNION SELECT ff, nb, 'shared'::text AS dir FROM res_adj WHERE ff<>nb)
  SELECT DISTINCT adj.ff, e.agent_id, e.change_id, adj.nb, adj.dir FROM adj JOIN edited e ON e.path=adj.ff
$$;
ALTER FUNCTION core._claim_adjacency(text,text,text) OWNER TO veripsa_migrator;
-- INTERNAL ONLY: it takes p_account as a param + runs SECURITY DEFINER (owner), so PUBLIC execute would let
-- any tenant pass another account's id (and pin that GUC for the owner-bypass FORCE-RLS read) and read that
-- account's code-graph adjacency / file paths cross-tenant. Postgres grants EXECUTE to PUBLIC by default —
-- revoke it. Only the owner-context surfaces (contention/main_impact, which pass their OWN re-resolved
-- account) call it. (Same class as _place_claim's revoke in 30_gate.sql.)
REVOKE ALL ON FUNCTION core._claim_adjacency(text,text,text) FROM PUBLIC;

-- ============================================================================================
-- _cross_repo_adjacency — CROSS-REPO contract coupling READ (feasibility-spike STEP 1, SHADOW).
--
-- WHAT: the res_adj coupling above ("two files that touch the SAME contract `dst` are coupled") is
-- HARD-PINNED to ONE repo coordinate — `o.repo=p_repo AND o.branch=p_branch` on BOTH the edited side
-- (ce) and the shared-resource side (o). So it can NEVER couple a DEFINER in repo A with a CONSUMER in
-- repo B, even when both carry an alters/queries edge to the SAME cross-repo-stable contract key
-- (`route::/api/orders/{}`, `api_operation::confirmOrder`, …). This function is res_adj with the repo-
-- equality RELAXED on the shared-resource side, scoped to a FIXED repo-PAIR (A=p_repo_a/p_branch_a,
-- B=p_repo_b/p_branch_b), so a file editing contract C in A couples to a file consuming C in B.
--
-- STRICTLY ADDITIVE + KILL-SWITCHED OFF: this is a SEPARATE function — _claim_adjacency (the within-repo
-- product) is UNTOUCHED. It returns ZERO rows unless the kill switch is ON: the GUC
-- `veripsa.cross_repo` must be a truthy value (1/true/on/yes). Default (unset / '0' / 'false') → empty.
--
-- SAME-ACCOUNT ONLY (this phase): both sides are filtered by the SAME p_account — there is NO consent /
-- cross-tenant machinery here (the dogfood pair is one owner). Cross-tenant is the NEXT phase, gated on
-- this signal proving out. SHADOW: nothing here is wired into a customer-facing verdict; it is read by the
-- shadow runner / the gate only.
--
-- CONTENT-FREE: it joins on contract `dst` KEYS (route/operation/type NAMES + paths) and returns FILE
-- PATHS + the shared key — never bodies. SCOPED to contract keys only (the cross-repo-stable substrates:
-- route::/api_operation::/api_schema::/api_type::/api_message::/api_service::) so a within-repo FILE-path
-- `dst` (imports) or a bare table name can never cross the repo boundary here.
--
-- RETURNS one row per (a_file in A, b_file in B, shared_key, dir): the candidate cross-boundary link. The
-- caller (shadow runner / gate) rolls these up into the repo graph + the co-change lift.
-- ============================================================================================
DROP FUNCTION IF EXISTS core._cross_repo_adjacency(text,text,text,text,text);
CREATE OR REPLACE FUNCTION core._cross_repo_adjacency(
    p_account text, p_repo_a text, p_branch_a text, p_repo_b text, p_branch_b text)
  RETURNS TABLE(a_file text, b_file text, shared_key text, dir text)
  LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = core, pg_catalog AS $$
BEGIN
  -- The owner (veripsa_migrator) is subject to FORCE ROW LEVEL SECURITY on code_edge, so pin the tenant
  -- for the owner-bypass read to THIS account (same discipline as the contention surfaces in 80_contention).
  -- SAME-ACCOUNT only: BOTH repos are read under the one p_account; no cross-tenant read is possible here.
  PERFORM set_config('core.current_account', p_account, true);
  RETURN QUERY
  WITH kill_switch AS (
    -- The whole read is OFF unless veripsa.cross_repo is truthy. A non-truthy/unset GUC yields NO rows
    -- in this CTE, so every join below is empty → the function returns nothing (kill switch works).
    SELECT 1 WHERE lower(COALESCE(NULLIF(current_setting('veripsa.cross_repo', true), ''), '0'))
                   IN ('1','true','on','yes')),
  -- Contract-key edges in repo A (DEFINER `alters` or CONSUMER `queries`) — scoped to the cross-repo-
  -- stable contract substrates ONLY (a file-path import `dst` or a bare table name never crosses here).
  -- NOTE column names af/bf/ckey/ek (NOT a_file/b_file/shared_key) — a CTE column equal to a RETURNS
  -- TABLE OUT-parameter name is an AMBIGUOUS reference inside plpgsql. The final SELECT aliases back to
  -- the OUT names.
  a_edges AS (
    SELECT ce.src AS af, ce.dst AS ckey,
           COALESCE(ce.semantic_dst_key,core._semantic_ref_key(ce.dst)) AS sk,
           ce.edge_kind AS ek
      FROM core.code_edge ce, kill_switch
     WHERE ce.account_id=p_account AND ce.repo=p_repo_a AND ce.branch=p_branch_a
       AND ce.edge_kind IN ('queries','alters') AND ce.reference_status IS NULL
       AND (ce.dst LIKE 'route::%' OR ce.dst LIKE 'api\_operation::%' OR ce.dst LIKE 'api\_schema::%'
            OR ce.dst LIKE 'api\_type::%' OR ce.dst LIKE 'api\_message::%' OR ce.dst LIKE 'api\_service::%')),
  b_edges AS (
    SELECT ce.src AS bf, ce.dst AS ckey,
           COALESCE(ce.semantic_dst_key,core._semantic_ref_key(ce.dst)) AS sk,
           ce.edge_kind AS ek
      FROM core.code_edge ce, kill_switch
     WHERE ce.account_id=p_account AND ce.repo=p_repo_b AND ce.branch=p_branch_b
       AND ce.edge_kind IN ('queries','alters') AND ce.reference_status IS NULL
       AND (ce.dst LIKE 'route::%' OR ce.dst LIKE 'api\_operation::%' OR ce.dst LIKE 'api\_schema::%'
            OR ce.dst LIKE 'api\_type::%' OR ce.dst LIKE 'api\_message::%' OR ce.dst LIKE 'api\_service::%')),
  -- MULTI-DEFINER guard (mirrors res_adj's res_hubs discipline + the extractor-side suppression): a key
  -- DEFINED (`alters`) by >1 file in EITHER repo is ambiguous (the same name from multiple services/copies)
  -- and anchors no cross-repo coupling. Drop it.
  ambig AS (
    SELECT sk FROM (
      SELECT sk, count(DISTINCT af) c FROM a_edges WHERE ek='alters' GROUP BY sk
      UNION ALL
      SELECT sk, count(DISTINCT bf) c FROM b_edges WHERE ek='alters' GROUP BY sk
    ) z GROUP BY sk HAVING max(c) > 1)
  -- A real cross-repo contract: a DEFINER on one side, a CONSUMER on the other (producer↔consumer). We
  -- surface BOTH directions and dedup. (A def↔def or use↔use pair is NOT a producer/consumer contract.)
  SELECT a.af AS a_file, b.bf AS b_file, a.ckey AS shared_key, 'A_def->B_use'::text AS dir
    FROM a_edges a JOIN b_edges b ON b.sk=a.sk
   WHERE a.ek='alters' AND b.ek='queries' AND a.sk NOT IN (SELECT sk FROM ambig)
  UNION
  SELECT a.af AS a_file, b.bf AS b_file, a.ckey AS shared_key, 'B_def->A_use'::text AS dir
    FROM a_edges a JOIN b_edges b ON b.sk=a.sk
   WHERE a.ek='queries' AND b.ek='alters' AND a.sk NOT IN (SELECT sk FROM ambig);
END;
$$;
ALTER FUNCTION core._cross_repo_adjacency(text,text,text,text,text) OWNER TO veripsa_migrator;
-- INTERNAL ONLY (same rationale as _claim_adjacency): it takes p_account + runs SECURITY DEFINER, so PUBLIC
-- execute would let any tenant pass another account's id and read cross-tenant. Revoke PUBLIC execute.
REVOKE ALL ON FUNCTION core._cross_repo_adjacency(text,text,text,text,text) FROM PUBLIC;

-- ============================================================================================
-- _dampened_adjacency — THE UNKNOWN-FIRST GUARD over hub dampening (AUDIT3 2026-06-18). _claim_adjacency
-- EXCLUDES adjacency through a HUB (a file imported by > veripsa.hub_degree others) to stop the wall-of-
-- warnings noise death. That is correct for the IMPORTER-side noise — but it had a SILENT cost the audit
-- proved: when TWO in-flight changes are coupled by a REAL, EXTRACTED import/call edge whose endpoint
-- happens to be a hub (e.g. flask: PR editing globals.py + PR editing one of its 17 importers), the engine
-- DROPPED that edge and the verdict fell through to a CONFIDENT 'clear' — a missed collision rendered SAFE,
-- with no indication anything was silenced. (Measured: hub dampening silenced 135/235 of flask's direct
-- import pairs; 32 real co-change pairs.) That breaks the unknown-first invariant ("never a false clear;
-- if we can't analyze it, say unknown").
--
-- This returns EXACTLY the dampened couplings BETWEEN TWO DIFFERENT in-flight changes (NOT the whole wall of
-- importers — only the ones where the OTHER endpoint is ALSO an active claim, i.e. an actual inflight×inflight
-- contention). main_impact_surface uses it to DOWNGRADE such a change from 'clear' → 'unknown' and surface a
-- visible `dampened_with` (so silence becomes honest, per the prior recommendation). It does NOT re-introduce
-- the noise: it never fans out to non-inflight neighbors, and it never upgrades to 'warn'. `via_hub` = the hub
-- path / hot resource the coupling ran through (rendered as the reason). Content-free.
--
-- SYMMETRY WITH _claim_adjacency's DROPS (the recall fix, 2026-06-18): _claim_adjacency drops hub noise on THREE
-- axes, but this guard originally only recovered ONE (imports). The other two were a residual silent-miss:
--   (a) IMPORTS  — imp_out/imp_in drop `ce.dst NOT IN hub_files` (the imported file is a hub).  [covered: `imp`]
--   (b) CALLS-INTO-HUB — out_adj/in_adj drop a `calls` coupling when the symbol's DEFINER FILE is a hub
--       (`d.path/edn.path NOT IN hub_files`). Two in-flight changes coupled ONLY by a call into a hub-file's
--       symbol fell through to 'clear'.                                                          [now: `calls_h`]
--   (c) SHARED RES-HUB — res_adj drops `ce.dst NOT IN res_hubs` (the shared table/config key is a HOT resource).
--       Two in-flight changes coupled ONLY by both touching a hot shared resource fell through to 'clear'.
--                                                                                                [now: `res_h`]
-- All three are recovered as inflight×inflight pairs → clear→unknown (never warn). The CALLS recovery mirrors
-- _claim_adjacency's resolution discipline (dunder/stoplist skip; a name in ≤3 files; import-confirmed OR a
-- single unambiguous definer) so the recovered set stays ⊆ what was actually dropped (no manufactured unknowns
-- from ubiquitous names like `map`/`run`). The RES recovery keys on the shared resource node only (content-free).
-- ============================================================================================
DROP FUNCTION IF EXISTS core._dampened_adjacency(text,text,text);
CREATE OR REPLACE FUNCTION core._dampened_adjacency(p_account text, p_repo text, p_branch text)
    RETURNS TABLE(f text, f_change_id text, nbr text, nbr_change_id text, via_hub text)
    LANGUAGE sql STABLE SECURITY DEFINER SET search_path TO 'core','pg_catalog' AS $$
  WITH
  active AS (   -- only ACTIVE in-flight claims are real contenders (matches contested/depends_changing)
    SELECT DISTINCT target_path AS path, change_id FROM core.claim
     WHERE account_id=p_account AND repo=p_repo AND branch=p_branch AND claim_state='active'),
  -- PERF (audit:perf 2026-06-19, the BRAIN-PATH launch-blocker): the DISTINCT PATH SET of the active in-flight
  -- claims, materialized ONCE. Every axis below (imp / calls_h / res_h) emits a coupling ONLY between two paths
  -- the final `paired` join re-confirms are BOTH active — so the whole helper's output depends on the change-set
  -- ONLY through this set. Pre-restricting each axis's DRIVING edges to active_path BEFORE the heavy graph joins
  -- (instead of computing imp/calls_h/res_h over the ENTIRE graph and intersecting with `active` only at the very
  -- end) is SEMANTICS-IDENTICAL — `paired`'s `active ai/ah/ac/ad/a1/a2` joins already discard every row whose
  -- endpoints are not both active — but turns a whole-graph scan into a scan of only the (tiny) in-flight subgraph.
  -- The audit measured the un-restricted version at 13,256ms returning 0 rows on a 6k-file graph; the sibling
  -- _claim_adjacency stays at 363ms BECAUSE it restricts to `edited` (its active set) before the heavy joins. This
  -- mirrors that discipline. NOTE: only the DRIVING file membership is restricted — the hub/res classification
  -- (hub_files / res_hubs), the global same-name ambiguity counts (def_n / defs_ok), and the import-confirmation
  -- EXISTS subqueries in calls_h stay GRAPH-WIDE (they are graph properties, not change-set properties); narrowing
  -- those would change WHICH couplings the hub guard considers dropped = a behavior change. We narrow only the rows.
  -- CAVEAT (audit:perf 2026-06-21): #218's `e1.src IN (active_path)` form narrowed imp/calls_h effectively, but for
  -- the res axis it did NOT — Postgres would not push those semi-joins below the code_edge×code_edge self-join and
  -- still scanned the whole hot-resource fan-in (O(#res_hubs × fan-in²); 23.5s@N=100). The (c) block below now
  -- DRIVES the res self-join from the active set (active_res_edge), completing the narrowing for all three axes.
  active_path AS MATERIALIZED (SELECT DISTINCT path FROM active),
  -- MATERIALIZED hub/res classification: each is a GROUP BY dst / HAVING count(DISTINCT src) fan-in scan referenced
  -- as an `IN (...)` probe below; folding it once then probing the small result is semantics-identical and avoids
  -- a re-scan per reference (same remedy as _claim_adjacency's hub_files/res_hubs and 80_contention.sql's hotspot).
  hub_files AS MATERIALIZED (
    SELECT n.path,
           COALESCE(
             n.semantic_key,
             core._node_semantic_key(
               n.node_kind,n.node_id,n.path,n.name,n.canonical_key
             )
           ) AS sk
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
     WHERE e.account_id=p_account AND e.repo=p_repo AND e.branch=p_branch
       AND e.edge_kind='imports' AND e.reference_status IS NULL
     GROUP BY n.path,COALESCE(
       n.semantic_key,
       core._node_semantic_key(n.node_kind,n.node_id,n.path,n.name,n.canonical_key)
     )
    HAVING count(DISTINCT e.src) >
      COALESCE(NULLIF(current_setting('veripsa.hub_degree', true), '')::int, 8)),
  -- res_hubs: the resource analogue of hub_files — a table/config key touched by > the hub cutoff distinct files
  -- (the HOT resource res_adj drops `ce.dst NOT IN res_hubs`). Identical cutoff to _claim_adjacency's res_hubs.
  res_hubs AS MATERIALIZED (
    SELECT COALESCE(semantic_dst_key,core._semantic_ref_key(dst)) AS sk
      FROM core.code_edge
     WHERE account_id=p_account AND repo=p_repo AND branch=p_branch
       AND edge_kind IN ('queries','alters','reads_config') AND reference_status IS NULL
     GROUP BY COALESCE(semantic_dst_key,core._semantic_ref_key(dst))
    HAVING count(DISTINCT src) > COALESCE(NULLIF(current_setting('veripsa.hub_degree', true), '')::int, 8)),
  node_file AS (
    SELECT DISTINCT node_id,path,
           COALESCE(
             semantic_key,
             core._node_semantic_key(
               node_kind,node_id,path,name,canonical_key
             )
           ) AS sk
      FROM core.code_node
     WHERE account_id=p_account AND repo=p_repo AND branch=p_branch
       AND node_kind='file'
  ),
  -- definer files per symbol NAME, with _claim_adjacency's exact precision discipline: skip dunders + the
  -- ubiquitous-name stoplist (calling them is protocol, not coupling), and only a name defined in ≤3 files is a
  -- resolvable definer (>3 = too ambiguous, dropped as noise there too). Mirrors defs / def_n / defs_ok.
  -- MATERIALIZED: def_n/defs_ok are re-referenced (the calls_h join + the import-confirmation NOT EXISTS over
  -- defs_ok); they are GLOBAL ambiguity counts (must NOT be restricted to active, or the n<=3 / n=1 tests change),
  -- so fold them ONCE. This is the re-CTE-scan the audit flagged (defs/def_n/defs_ok scanned repeatedly).
  defs AS MATERIALIZED (
    SELECT name AS nm,path,
           COALESCE(
             semantic_key,
             core._node_semantic_key(
               node_kind,node_id,path,name,canonical_key
             )
           ) AS sk
      FROM core.code_node
     WHERE account_id=p_account AND repo=p_repo AND branch=p_branch AND node_kind IN ('def','class') AND name IS NOT NULL
       AND name !~ '^__'
       AND lower(name) <> ALL (ARRAY[
         'main','run','setup','teardown','handle','dispatch','register','wrap',
         'to_s','to_str','tostring','str','repr','inspect','format','print','println','puts','log','warn','debug','trace',
         'equals','eql?','hash','hashcode','compareto','compare','cmp',
         'empty?','blank?','present?','nil?','valid?','include?','contains','respond_to?','key?','has_key?',
         'to_a','to_h','to_sym','to_i','to_json',
         'map','each','collect','select','reject','filter','reduce','merge','flatten','zip',
         'freeze','dup','clone','tap','then','send','call','apply','yield',
         'it','its','describe','context','before','after','around','expect','should','assert','refute','let','subject','mock','stub'
       ])),
  def_n AS MATERIALIZED (SELECT sk,count(DISTINCT path) AS n FROM defs GROUP BY sk),
  defs_ok AS MATERIALIZED (
    SELECT d.nm,d.path,d.sk FROM defs d JOIN def_n c ON c.sk=d.sk WHERE c.n<=3
  ),
  -- ── (a) IMPORTS: a DIRECT import edge between two active in-flight files where the IMPORTED endpoint is a hub.
  -- Exactly the edge _claim_adjacency's `ce.dst NOT IN hub_files` dropped. Symmetric (importer→hub, hub→importer).
  -- PRE-RESTRICTED: both endpoints must be active in-flight files (what `paired`'s active ai/ah joins re-confirm).
  imp AS (
    SELECT e.src AS importer,n.path AS hub
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
     WHERE e.account_id=p_account AND e.repo=p_repo AND e.branch=p_branch
       AND e.edge_kind='imports' AND e.reference_status IS NULL
       AND e.src IN (SELECT path FROM active_path)            -- importer must be an active in-flight file
       AND n.path IN (SELECT path FROM active_path)            -- hub endpoint must ALSO be active in-flight
       AND COALESCE(e.semantic_dst_key,core._semantic_ref_key(e.dst))
           IN (SELECT sk FROM hub_files)),
  -- ── (b) CALLS-INTO-HUB: a `calls` edge from a caller file to a symbol whose DEFINER FILE is a hub. The
  -- definer file ∈ hub_files is exactly what out_adj/in_adj's `path NOT IN hub_files` guards dropped. Keep only
  -- resolvable couplings (defs_ok) that are import-confirmed OR have a single unambiguous definer — the same
  -- precision out_adj/in_adj apply — so we recover ⊆ what the hub guard removed, not a new fan-out.
  -- PRE-RESTRICTED: caller AND definer must both be active in-flight files (what `paired`'s active ac/ad re-confirm).
  -- Import confirmation stays GRAPH-WIDE: it tests the global import structure, not only active files.
  calls_h AS (
    SELECT cf.path AS caller, d.path AS definer
      FROM core.code_edge ce
      JOIN node_file cf ON cf.node_id=ce.src
      JOIN defs_ok d
        ON d.sk=COALESCE(ce.semantic_dst_key,core._semantic_ref_key(ce.dst))
      JOIN def_n dn ON dn.sk=d.sk
     WHERE ce.account_id=p_account AND ce.repo=p_repo AND ce.branch=p_branch
       AND ce.edge_kind='calls' AND ce.reference_status IS NULL
       AND cf.path IN (SELECT path FROM active_path)          -- caller must be an active in-flight file
       AND d.path IN (SELECT path FROM active_path)           -- definer file must ALSO be active in-flight
       AND core._semantic_ref_key(d.path) IN (SELECT sk FROM hub_files)
       AND cf.path <> d.path
       AND (EXISTS (SELECT 1 FROM core.code_edge ie WHERE ie.account_id=p_account AND ie.repo=p_repo AND ie.branch=p_branch
                      AND ie.edge_kind='imports' AND ie.src=cf.path AND ie.reference_status IS NULL
                      AND COALESCE(ie.semantic_dst_key,core._semantic_ref_key(ie.dst))
                          =core._semantic_ref_key(d.path))
            OR (dn.n = 1
                -- Same identity proof as _claim_adjacency: the only same-key definer is d.path. Import-present
                -- is already accepted by EXISTS; in the no-import arm the former correlated NOT EXISTS was
                -- tautological and only re-scanned the global defs_ok set for every call.
                -- BUILTIN/SHORT-NAME guard (mirrors out_adj/in_adj in _claim_adjacency — same un-imported
                -- single-definer FP class, same `calls_h` n=1 rule). Drop a ≤2-char target + builtin/stdlib
                -- method names from THIS un-imported branch ONLY; import-confirmed (EXISTS above) survives,
                -- domain names untouched. Keeps the two functions' precision discipline in lockstep. content-free.
                AND char_length(ce.dst) > 2
                AND lower(ce.dst) <> ALL (ARRAY[
                  'pop','push','shift','unshift','slice','splice','all','any','stringify',
                  'keys','values','items','entries','has','join','split','assign',
                  'hasprefix','hassuffix','do','newdecoder','newencoder','marshal','unmarshal',
                  'sprintf','printf','fprintf','sprint','sprintln','close','open','dumps','loads'])))),
  -- ── (c) SHARED RES-HUB: two DISTINCT files each touch the SAME hot resource (∈ res_hubs) via queries/alters/
  -- reads_config — exactly the coupling res_adj's `ce.dst NOT IN res_hubs` dropped. The resource is the via_hub.
  -- PRE-RESTRICTED: both co-touching files must be active in-flight files (what `paired`'s active a1/a2 re-confirm).
  --
  -- PERF (audit:perf 2026-06-21 — the SECOND brain-path blow-up #218 left in this sibling): the prior form was a
  -- `code_edge e1 JOIN code_edge e2 ON e2.dst=e1.dst` SELF-JOIN with the active restriction written as
  -- `e1.src IN (active_path)` / `e2.src IN (active_path)`. Unlike the imp/calls_h axes (whose `active_path` filter
  -- the planner DOES push to drive the scan), Postgres does NOT push those two semi-joins BELOW the self-join: it
  -- drives `res_hubs → e1 (by dst) → e2 (by dst)` — the FULL hot-resource fan-in (every file touching the hot
  -- table/key, |touchers|² rows PER res_hub, almost none active) — and applies the active filter at the very END.
  -- That is O(#res_hubs × fan-in²), independent of (and dwarfing) the in-flight count: MEASURED 4.3s@N=50 / 23.5s@
  -- N=100 returning ~86/490 rows on a 12k-file graph with hot cross-substrate tables/config keys (schema/config/
  -- API/IaC/OpenAPI all funnel through this res axis via queries/alters/reads_config). The fix MIRRORS #218's
  -- imp/calls_h discipline: materialize the ACTIVE files' resource edges FIRST (driven by code_edge_coord_kind_src
  -- off active_path — O(in-flight touches), NOT the whole fan-in), keep only those on a res_hub, then self-join
  -- THAT small set on the shared resource. SEMANTICS-IDENTICAL: the result is still {(f1,f2,resource) : f1≠f2 both
  -- active, both touch the same res_hub via the resource kinds}; downstream `SELECT DISTINCT` already collapses the
  -- multi-edge duplicates the old e1×e2 form produced (via_hub=resource on every row, unchanged). Proven byte-
  -- identical N=10/50/100/200 (tests/test_dampened_res_scale.py + the existing test_dampened_identity oracle).
  -- Measured 22.6x@N=100 (23.5s→1.0s); residual ~1s is the graph-wide hub/res/def classification #218 KEEPS by
  -- design (graph properties, not change-set properties). active_res_edge/res_touch MATERIALIZED — folded once.
  active_res_edge AS MATERIALIZED (
    SELECT DISTINCT e.src AS f,e.dst AS resource,
           COALESCE(e.semantic_dst_key,core._semantic_ref_key(e.dst)) AS sk
      FROM active_path ap
      JOIN core.code_edge e ON e.account_id=p_account AND e.repo=p_repo AND e.branch=p_branch
        AND e.edge_kind IN ('queries','alters','reads_config') AND e.src=ap.path
        AND e.reference_status IS NULL),  -- src-index driven (active only)
  res_touch AS MATERIALIZED (
    SELECT are.f,are.resource,are.sk FROM active_res_edge are
     WHERE are.sk IN (SELECT sk FROM res_hubs)),
  res_h AS (
    SELECT t1.f AS f1, t2.f AS f2, t1.resource AS resource
      FROM res_touch t1
      JOIN res_touch t2 ON t2.sk=t1.sk AND t2.f<>t1.f),
  -- both endpoints must be DISTINCT active in-flight changes (an inflight×inflight contention, not noise).
  -- SYMMETRIC for each axis: emit the pair from BOTH sides so each PR sees the suppressed coupling (the hub-side
  -- editor's change DOES affect its in-flight counterpart — that must not be a confident 'clear').
  paired AS (
    -- (a) imports
    SELECT ai.path AS f, ai.change_id AS f_change_id, ah.path AS nbr, ah.change_id AS nbr_change_id, i.hub AS via_hub
      FROM imp i JOIN active ai ON ai.path=i.importer JOIN active ah ON ah.path=i.hub WHERE ai.change_id<>ah.change_id
    UNION ALL
    SELECT ah.path, ah.change_id, ai.path, ai.change_id, i.hub
      FROM imp i JOIN active ai ON ai.path=i.importer JOIN active ah ON ah.path=i.hub WHERE ai.change_id<>ah.change_id
    -- (b) calls-into-hub
    UNION ALL
    SELECT ac.path, ac.change_id, ad.path, ad.change_id, c.definer
      FROM calls_h c JOIN active ac ON ac.path=c.caller JOIN active ad ON ad.path=c.definer WHERE ac.change_id<>ad.change_id
    UNION ALL
    SELECT ad.path, ad.change_id, ac.path, ac.change_id, c.definer
      FROM calls_h c JOIN active ac ON ac.path=c.caller JOIN active ad ON ad.path=c.definer WHERE ac.change_id<>ad.change_id
    -- (c) shared res-hub (already symmetric in (f1,f2) since res_h emits both src orders, but keep the explicit
    -- both-orientation join for parity with the others and so via_hub is the resource on each row)
    UNION ALL
    SELECT a1.path, a1.change_id, a2.path, a2.change_id, r.resource
      FROM res_h r JOIN active a1 ON a1.path=r.f1 JOIN active a2 ON a2.path=r.f2 WHERE a1.change_id<>a2.change_id)
  SELECT DISTINCT f, f_change_id, nbr, nbr_change_id, via_hub FROM paired
$$;
ALTER FUNCTION core._dampened_adjacency(text,text,text) OWNER TO veripsa_migrator;
-- INTERNAL ONLY (same tenant-leak class as _claim_adjacency / _inflight_components above).
REVOKE ALL ON FUNCTION core._dampened_adjacency(text,text,text) FROM PUBLIC;

-- _inflight_components — N-SCALE GROUPING. At 2 agents a pair of warnings is readable; at N it is N²
-- noise. The in-flight changes + their links form a GRAPH (a node per PR/change; an undirected link for a
-- DIRECT same-lane collision OR a SEMANTIC A→B adjacency). Its CONNECTED COMPONENTS are the contention
-- NEIGHBORHOODS — the human-scalable unit (coordinate a cluster, not a hotspot's N² pairs; the market's
-- "shared hotspot files" pile-up). Returns (change_id, comp) where comp = the component's representative
-- (min change id reachable). Content-free; small N (in-flight PRs at once).
--
-- DEGENERATE-GRAPH BOUND (audit 2026-06-18): the link graph can be DEGENERATE — a deep linear contention chain
-- or a big ring (a layered monorepo: 1500 open PRs n0–n1–…–n_k, each a same-lane / A→B link to the next). The
-- ORIGINAL recursive-CTE min-label propagation re-pushed every label one hop at a time AND every node seeded its
-- own descending wave, so the recursion materialized O(N²) (node,comp) pairs (measured: a 1500-chain = 1,125,750
-- rows, ~19s — on the WEBHOOK path = a self-inflicted DoS on a real input).
--
-- FIX: POINTER-JUMPING (Shiloach–Vishkin-style) connected components instead of a recursive label closure. Each
-- node carries a `comp` pointer (seeded to itself). A round does two bulk set-based UPDATEs over TEMP tables:
--   HOOK — lower each node's comp to the MIN comp among itself + its neighbours (one-hop label pull), and
--   JUMP — pointer-double: comp := comp-of-(comp-of-node), so a node's pointer leaps toward the component root
--           in HALVING distance each round (the log-depth trick).
-- Iterated to fixpoint (no pointer changed), every node ends at its component's MIN id = exact connected
-- components. CORRECT for ANY shape — a fully entangled N-ring collapses to ONE component, a deep chain to ONE,
-- a forest to its trees — with NO cap that could wrongly split a real neighbourhood. BOUNDED: pointer-doubling
-- converges in O(log N) rounds (measured: 40-ring 6 rounds/22ms, 400-chain 10/99ms, 1500-chain 14 rounds/0.35s —
-- vs the old recursive closure's ~19s on the same 1500-chain). A clamped round cap (`cluster_max_rounds`, default
-- 64, clamp 1..1000) is a pure NON-TERMINATION BACKSTOP that should never bind (log₂ of any realistic in-flight
-- count is < 30); if it ever did, the result is a valid COARSENING (sub-components not yet merged), never a
-- contradiction. Tenant-pinned; content-free (change ids + links only).
-- VOLATILE (not STABLE): it builds + mutates TEMP tables (ON COMMIT DROP), which a STABLE function may not do.
-- Still read-only w.r.t. PERSISTENT state (it never writes a core table) — the temp scratch is private per call.
CREATE OR REPLACE FUNCTION core._inflight_components(p_account text, p_repo text, p_branch text)
    RETURNS TABLE(change_id text, comp text)
    LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path TO 'core','pg_catalog' AS $$
DECLARE v_rounds int := core._policy_int('cluster_max_rounds', 64, 1, 1000);  -- log-depth backstop (should never bind)
        v_changed bigint;
        v_changed2 bigint;
BEGIN
  -- seed each node's pointer = its own id (a TEMP table dropped at commit), + the undirected link set. DROP-first:
  -- main_impact_surface can call this MULTIPLE times in ONE statement (e.g. inflight_count||cluster_count); the
  -- ON COMMIT DROP only fires at COMMIT, so a 2nd in-statement call would hit "relation already exists" — drop it.
  DROP TABLE IF EXISTS _cc_node; DROP TABLE IF EXISTS _cc_link;
  CREATE TEMP TABLE _cc_node(node text PRIMARY KEY, comp text) ON COMMIT DROP;
  CREATE TEMP TABLE _cc_link(a text, b text) ON COMMIT DROP;
  INSERT INTO _cc_node(node, comp)
    SELECT DISTINCT c.change_id, c.change_id FROM core.claim c
     WHERE c.account_id=p_account AND c.repo=p_repo AND c.branch=p_branch AND c.claim_state IN ('active','waiting');
  -- undirected links: a SEMANTIC A→B adjacency (a held neighbour) OR a DIRECT same-lane wait, both directions.
  INSERT INTO _cc_link(a, b)
    WITH active AS (SELECT DISTINCT c.target_path AS path, c.change_id FROM core.claim c
                     WHERE c.account_id=p_account AND c.repo=p_repo AND c.branch=p_branch AND c.claim_state='active'),
         adj AS (SELECT f_change_id, nbr FROM core._claim_adjacency(p_account, p_repo, p_branch)),
         sem AS (SELECT DISTINCT adj.f_change_id AS a, av.change_id AS b FROM adj JOIN active av ON av.path=adj.nbr AND av.change_id<>adj.f_change_id),
         direct AS (SELECT DISTINCT w.change_id AS a, h.change_id AS b FROM core.claim w JOIN core.claim h
                      ON h.account_id=w.account_id AND h.repo=w.repo AND h.branch=w.branch AND h.target_path=w.target_path AND h.claim_state='active'
                     WHERE w.account_id=p_account AND w.repo=p_repo AND w.branch=p_branch AND w.claim_state='waiting' AND w.change_id<>h.change_id)
    SELECT a,b FROM sem UNION SELECT b,a FROM sem UNION SELECT a,b FROM direct UNION SELECT b,a FROM direct;
  CREATE INDEX IF NOT EXISTS _cc_link_a ON _cc_link(a);   -- IF NOT EXISTS: migration-safe form (a fresh per-call temp table)
  -- HOOK + JUMP to fixpoint. Each round halves every pointer's distance to its component root → O(log N) rounds.
  FOR i IN 1..v_rounds LOOP
    -- HOOK: pull the min comp over self + neighbours (one-hop relaxation).
    WITH cand AS (
      SELECT me.node, LEAST(me.comp, COALESCE(min(nb.comp), me.comp)) AS p
        FROM _cc_node me
        LEFT JOIN _cc_link l ON l.a = me.node
        LEFT JOIN _cc_node nb ON nb.node = l.b
       GROUP BY me.node, me.comp)
    UPDATE _cc_node n SET comp = cand.p FROM cand WHERE cand.node = n.node AND cand.p < n.comp;
    GET DIAGNOSTICS v_changed = ROW_COUNT;
    -- JUMP: pointer-double — comp := comp-of-(comp-of-node) — leaping toward the root (the log-depth step).
    WITH jmp AS (
      SELECT n.node, gp.comp AS gpp
        FROM _cc_node n JOIN _cc_node p ON p.node = n.comp JOIN _cc_node gp ON gp.node = p.comp)
    UPDATE _cc_node n SET comp = jmp.gpp FROM jmp WHERE jmp.node = n.node AND jmp.gpp < n.comp;
    GET DIAGNOSTICS v_changed2 = ROW_COUNT;
    EXIT WHEN v_changed + v_changed2 = 0;   -- fixpoint: no pointer improved this round → components are final
  END LOOP;
  RETURN QUERY SELECT n.node, n.comp FROM _cc_node n;
END $$;
ALTER FUNCTION core._inflight_components(text,text,text) OWNER TO veripsa_migrator;
-- INTERNAL ONLY (same class as _claim_adjacency above): takes p_account + SECURITY DEFINER, so PUBLIC execute
-- would leak another account's in-flight change set cross-tenant. Only main_impact_surface (owner-context,
-- passing its OWN re-resolved account) calls it.
REVOKE ALL ON FUNCTION core._inflight_components(text,text,text) FROM PUBLIC;

-- ============================================================================================
