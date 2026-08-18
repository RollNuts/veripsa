-- PHASE 2 — TRAFFIC-CONTROL CORE. The nouns (claim · code graph) + the ONE kinded fact ledger (event).
-- Multi-coordinate COEXISTENCE: the graph + version are keyed per (account, repo, branch); ingesting one
-- coordinate never wipes another. Facts do NOT each get a table — they are KINDS in `event` (no 乱立).
-- ============================================================================================

-- claim: THE LOCK. Declare-before-edit on a coordinate (repo,branch,path). LIVE, mutable state (active →
-- released/expired, heartbeat/lease) — NOT a ledger; the one write path is the gate (forgery-blocked).
-- At most ONE active claim per (account, repo, branch, target_path) — the structural lock backstop.
CREATE TABLE IF NOT EXISTS core.claim (
    claim_id text NOT NULL,
    account_id text NOT NULL,
    agent_id text NOT NULL,
    change_id text DEFAULT '' NOT NULL,
    repo text DEFAULT '' NOT NULL,
    branch text DEFAULT '' NOT NULL,
    target_path text NOT NULL,
    claim_state text DEFAULT 'active' NOT NULL,
    claimed_at timestamptz DEFAULT now() NOT NULL,
    released_at timestamptz,
    heartbeat_at timestamptz DEFAULT now() NOT NULL,
    lease_expires_at timestamptz DEFAULT (now() + interval '30 minutes') NOT NULL,
    visibility text DEFAULT 'private' NOT NULL,
    -- FINER COLLISION (content-free): the CHANGED LINE RANGES this claim's change touches on this path —
    -- parsed from the PR diff's HUNK HEADERS only (`@@ -a,b +c,d @@` = changed line spans), the +/- body
    -- DISCARDED. int4range[] so overlap is a native `&&`. NULL = no range data (a file-level claim, an
    -- unsupported diff, a brand-new file) → the engine falls back to FILE-level collision (the safety net,
    -- never a missed collision). When BOTH sides of a same-file pair carry ranges AND map to DISJOINT symbols
    -- do we drop the file-level serialize (the finer win). Additive + back-compatible (NULL on old claims).
    touched_ranges int4range[],
    -- FRESHNESS KEY (content-free): the content hash (git-blob-sha — a fingerprint, NEVER the bytes) of THIS
    -- file AT THE PR'S BASE. The App already sees each changed file's blob sha, so this costs nothing to carry.
    -- It is the proof the engine needs before it dares demote a file-level collision to the finer symbol verdict:
    -- the claim's touched_ranges are line numbers relative to the PR's BASE, but main's graph symbol spans are
    -- relative to the INGESTED commit. They only line up when the file is UNCHANGED between the two — provable
    -- exactly when this base hash == the graph file node's content_hash. If they differ, or either is NULL/unknown,
    -- the line→symbol mapping could land on the WRONG symbol → the engine KEEPS the file-level collision
    -- (over-flag, recall-safe), never a silent demotion on unverifiable freshness. NULL on old claims = unknown.
    base_content_hash text,
    -- The PR head whose file/range evidence produced this live claim. A commit fingerprint only (no code);
    -- NULL on legacy/non-PR rows means unproven and prevents neighbor refresh from re-rendering over stale DB
    -- evidence. main_impact_surface exposes it only when every live path of a change agrees.
    analyzed_head_sha text,
    CONSTRAINT claim_pkey PRIMARY KEY (account_id, claim_id),
    CONSTRAINT claim_basehash_ok CHECK (base_content_hash IS NULL OR (length(base_content_hash) <= 64 AND base_content_hash ~ '^[0-9a-fA-F]+$')),
    CONSTRAINT claim_analyzed_head_ok CHECK (analyzed_head_sha IS NULL OR (length(analyzed_head_sha) BETWEEN 1 AND 64 AND analyzed_head_sha ~ '^[0-9a-f]+$')),
    CONSTRAINT claim_path_len CHECK (length(target_path) <= 1024),
    CONSTRAINT claim_repo_len CHECK (length(repo) <= 512),
    CONSTRAINT claim_branch_len CHECK (length(branch) <= 512),
    CONSTRAINT claim_state_check CHECK (claim_state = ANY (ARRAY['active','released','expired','waiting'])),
    CONSTRAINT claim_vis_check CHECK (visibility = ANY (ARRAY['private','team']))
);
SELECT core._ensure_column_online('claim','change_id',
  'text DEFAULT '''' NOT NULL');
-- ADDITIVE + IDEMPOTENT: the content-free changed-line-ranges (finer collision). NULL on every existing claim
-- → behaves exactly as today (file-level) until a claim is re-declared with ranges.
SELECT core._ensure_column_online('claim','touched_ranges','int4range[]');
-- ADDITIVE + IDEMPOTENT freshness key (the staleness-gated demotion): NULL on every existing claim → unknown →
-- file-level fallback, exactly as today, until a claim is re-declared carrying its base content hash.
SELECT core._ensure_column_online('claim','base_content_hash','text');
SELECT core._ensure_column_online('claim','analyzed_head_sha','text');
-- GUARDED + IDEMPOTENT CHECK (migration-safe): add the hash shape constraint only if absent — an unconditional
-- ADD CONSTRAINT re-errors "already exists" on redeploy. Safe on a populated table: base_content_hash is NULL on
-- every pre-existing claim and NULL satisfies the constraint, so no existing row can violate it.
DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid='core.claim'::regclass AND conname='claim_basehash_ok') THEN
    ALTER TABLE core.claim ADD CONSTRAINT claim_basehash_ok
      CHECK (base_content_hash IS NULL OR (length(base_content_hash) <= 64 AND base_content_hash ~ '^[0-9a-fA-F]+$'));
  END IF;
END $$;
DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid='core.claim'::regclass AND conname='claim_analyzed_head_ok') THEN
    ALTER TABLE core.claim ADD CONSTRAINT claim_analyzed_head_ok
      CHECK (analyzed_head_sha IS NULL OR (length(analyzed_head_sha) BETWEEN 1 AND 64 AND analyzed_head_sha ~ '^[0-9a-f]+$'))
      NOT VALID;
  END IF;
END $$;
-- Validate separately so a populated-table rollout avoids ADD CONSTRAINT's stronger full-scan lock. The newly
-- added nullable column is NULL on legacy rows, so validation is safe and future writes are checked immediately.
SELECT core._ensure_constraint_valid_online(
  'claim','claim_analyzed_head_ok');
-- CLAIM IDENTITY IS PER-REPO (multi-repo org fix). The App builds claim_id = 'PR-<n>:<path>' (the gate derives
-- change_id='PR-<n>' from the first ':'-segment). PR numbers and paths are PER-REPO, so two DIFFERENT repos in
-- the SAME account routinely share a claim_id (e.g. both have a PR #9 touching README.md) — and a multi-repo org
-- install puts every repo in ONE account. With the PK on (account_id, claim_id) the SECOND repo's claim INSERT
-- hit a unique_violation on claim_pkey, and _place_claim's unique_violation handler INSERTs the same PK AGAIN,
-- uncaught → the webhook event CRASHED (no check/comment on that PR — silent first-value failure). The lane
-- uniqueness (claim_one_active, below) was ALWAYS repo-scoped; only the PK lagged. Widen the PK to include repo
-- so a claim is unique per (account, repo) — the every-WHERE-clause already carries repo, so nothing else moves.
-- Idempotent + migration-safe: re-key only if the current PK is the old (account_id, claim_id) shape.
DO $$
DECLARE v_pk_cols text;
BEGIN
  SELECT string_agg(a.attname, ',' ORDER BY array_position(c.conkey, a.attnum))
    INTO v_pk_cols
    FROM pg_constraint c
    JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = ANY(c.conkey)
   WHERE c.conrelid = 'core.claim'::regclass AND c.contype = 'p';
  IF v_pk_cols = 'account_id,claim_id' THEN
    ALTER TABLE core.claim DROP CONSTRAINT claim_pkey;
    ALTER TABLE core.claim ADD CONSTRAINT claim_pkey PRIMARY KEY (account_id, repo, claim_id);
  END IF;
END $$;
CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS claim_one_active ON core.claim (account_id, repo, branch, target_path) WHERE claim_state = 'active';
CREATE INDEX CONCURRENTLY IF NOT EXISTS claim_active_by_account ON core.claim (account_id) WHERE claim_state = 'active';
CREATE INDEX CONCURRENTLY IF NOT EXISTS claim_open_change ON core.claim (account_id, repo, branch, change_id) WHERE claim_state IN ('active','waiting');

-- code graph (content-free; coordinate-tagged for COEXISTENCE).
CREATE TABLE IF NOT EXISTS core.code_node (
    account_id text NOT NULL,
    repo text DEFAULT '' NOT NULL,
    branch text DEFAULT '' NOT NULL,
    node_id text NOT NULL,
    node_kind text NOT NULL,
    path text NOT NULL,
    name text,
    language text,
    -- FINER COLLISION (content-free symbol span): a def/class (+ cheap schema/config) symbol's [start_line,
    -- end_line] over the file — LINE NUMBERS ONLY, never code. Nullable + back-compatible: a file node, an
    -- older graph, or an un-spanned symbol leaves these NULL and the engine falls back to FILE-level collision
    -- (the safety net — never a missed collision). 1-based, inclusive (start_line <= end_line when present).
    start_line int,
    end_line int,
    -- FRESHNESS KEY (content-free): a FILE node's content hash (a git-blob-sha — a fingerprint, NEVER the
    -- bytes) AT THE COMMIT THIS GRAPH WAS INGESTED FROM. It is what lets the finer (symbol-level) demotion be
    -- PROVABLY safe: a claim's diff line numbers are relative to the PR's BASE, but the symbol spans here are
    -- relative to the INGESTED commit. If the file changed between them, line→symbol mapping silently lands on
    -- the WRONG symbol and a real collision is wrongly demoted to 'warn' = a SILENT MISS. The engine demotes to
    -- the finer verdict ONLY when this hash == the claim's base content hash (spans provably valid); otherwise
    -- it keeps the file-level collision (over-flag, recall-safe). NULL on file nodes from an OLD ingest (no hash)
    -- and on def/class/table/column/config nodes (the hash lives on the FILE) → unknown → file-level fallback.
    content_hash text,
    -- COMPATIBILITY SIGNATURE SHAPE (content-free, #831's extractor fields persisted): a `def` node's
    -- normalized public signature — parameter NAMES, required/optional arity COUNTS, varargs/kwargs presence
    -- FLAGS, keyword-only NAMES, and a stable hash FINGERPRINT (a hash over the normalized shape — NEVER a
    -- default-value expression, NEVER annotation source, NEVER a body; the same content-free class as
    -- name/span/content_hash). Nullable + back-compatible: file/class/table/column/config nodes, older
    -- graphs, and an un-shaped def leave all seven NULL (= unknown shape). Nothing reads these columns yet;
    -- the compatibility rules that will read them treat NULL as Unknown, never Clear.
    required_arity int,
    optional_arity int,
    has_varargs boolean,
    has_kwargs boolean,
    param_names text[],
    kwonly_names text[],
    shape_fingerprint text,
    -- FIRST-CLASS RESOURCE IDENTITY (content-free): canonical_key is the bounded, display-safe representation of
    -- the resolver key carried by resource edges (for example table `orders`, column `orders.id`, or a scoped
    -- Terraform/K8s key). Exact equality is preserved independently in semantic_key below. The remaining nullable
    -- fields preserve bounded extractor provenance without storing source bodies. They are NULL on code nodes and
    -- legacy resource rows until the next cg3 re-ingest.
    canonical_key text,
    resource_scope text,
    extractor text,
    confidence double precision,
    provenance jsonb,
    -- LOSSLESS RESOLVER IDENTITY (content-free): SHA-256 of the extractor's exact lookup value. Display fields
    -- above remain shape-sanitized; this fixed-length key preserves equality for legal Git/symbol/resource
    -- characters which the display sanitizer rewrites. file/config_file hash path, def/class hash name, and
    -- Resource kinds hash canonical_key. NULL is reserved for legacy rows and unresolved symbol names.
    semantic_key text,
    -- PER-PATH ANALYSIS UNCERTAINTY (content-free closed enum). NULL means the document extractor completed
    -- normally. `failed` preserves a document whose parser/extractor failed; `ambiguous` preserves a document
    -- whose local resolution could not select one target; `incomplete` preserves a parser which returned a
    -- partial tree that cannot prove complete references. Only canonical document nodes may carry this marker.
    -- The effective verdict consumes it as Unknown only after stronger serialize/warn evidence.
    analysis_status text,
    -- SCHEMA CONTRACT cg3: every kind build_graph can emit is accepted. alters_col/queries_col are persisted
    -- below as evidence-only edges; effective adjacency intentionally continues to use table-level edges.
    CONSTRAINT code_node_kind_check CHECK (node_kind = ANY (ARRAY[
      'file','def','class','table','column','config_file','config_key',
      'iac_resource','k8s_resource','api_type','api_message','api_service','api_operation','api_schema',
      'ci_script','app_command','job_task','job_queue','sibling_stem','role_feature'
    ])),
    CONSTRAINT code_node_path_len CHECK (length(path) <= 1024),
    CONSTRAINT code_node_id_len CHECK (length(node_id) <= 1600),
    CONSTRAINT code_node_name_len CHECK (name IS NULL OR length(name) <= 512),
    CONSTRAINT code_node_lang_len CHECK (language IS NULL OR length(language) <= 32),
    CONSTRAINT code_node_canonical_key_len CHECK (canonical_key IS NULL OR length(canonical_key) <= 1600),
    CONSTRAINT code_node_resource_scope_len CHECK (resource_scope IS NULL OR length(resource_scope) <= 1024),
    CONSTRAINT code_node_extractor_len CHECK (extractor IS NULL OR length(extractor) <= 128),
    CONSTRAINT code_node_confidence_range CHECK (confidence IS NULL OR confidence BETWEEN 0.0 AND 1.0),
    CONSTRAINT code_node_provenance_shape CHECK (
      provenance IS NULL OR (jsonb_typeof(provenance) = 'object' AND octet_length(provenance::text) <= 8192)),
    CONSTRAINT code_node_semantic_key_shape CHECK (
      semantic_key IS NULL OR (length(semantic_key)=64 AND semantic_key ~ '^[0-9a-f]{64}$')),
    CONSTRAINT code_node_analysis_status_check CHECK (
      analysis_status IS NULL
      OR (
        node_kind = ANY (ARRAY['file','config_file'])
        AND analysis_status = ANY (ARRAY['failed','ambiguous','incomplete'])
      )),
    -- a content hash is a fixed-width hex fingerprint (git-blob-sha = 40 hex; a sha256 = 64). Bound it so a
    -- junk/oversized value can never be stored (defense in depth alongside the ingest-side sanitizer).
    CONSTRAINT code_node_hash_len CHECK (content_hash IS NULL OR (length(content_hash) <= 64 AND content_hash ~ '^[0-9a-fA-F]+$')),
    CONSTRAINT code_node_repo_len CHECK (length(repo) <= 512),
    CONSTRAINT code_node_branch_len CHECK (length(branch) <= 512),
    -- spans are content-free line numbers: bounded + ordered when present (a junk/inverted span is rejected,
    -- never stored — so the engine can trust a non-null span without re-validating it).
    CONSTRAINT code_node_span_ok CHECK (
      (start_line IS NULL AND end_line IS NULL)
      OR (start_line >= 1 AND end_line >= start_line AND end_line <= 100000000)),
    -- signature-shape bounds are content-free COUNTS/lengths: arity counts bounded, the name arrays bounded
    -- in CARDINALITY, the fingerprint a bounded hex string (same family as content_hash). NULL always passes
    -- (unknown shape = back-compat); a junk/oversized value is rejected at the wall, never stored.
    CONSTRAINT code_node_shape_ok CHECK (
      (required_arity IS NULL OR (required_arity >= 0 AND required_arity <= 512))
      AND (optional_arity IS NULL OR (optional_arity >= 0 AND optional_arity <= 512))
      AND (param_names IS NULL OR COALESCE(array_length(param_names, 1), 0) <= 128)
      AND (kwonly_names IS NULL OR COALESCE(array_length(kwonly_names, 1), 0) <= 128)
      AND (shape_fingerprint IS NULL OR (length(shape_fingerprint) <= 64 AND shape_fingerprint ~ '^[0-9a-fA-F]+$')))
);
-- ADDITIVE + IDEMPOTENT (never drops data): an existing instance gets the span columns via ADD COLUMN IF NOT
-- EXISTS, so a redeploy over a populated graph keeps every node (the spans are simply NULL until the next
-- re-ingest, which behaves exactly as today = back-compat).
SELECT core._ensure_column_online('code_node','start_line','int');
SELECT core._ensure_column_online('code_node','end_line','int');
-- ADDITIVE + IDEMPOTENT freshness key: a redeploy over a populated graph keeps every node; content_hash is
-- simply NULL on every pre-existing file node (unknown = file-level fallback = back-compat) until the next
-- re-ingest stamps it. NULLABLE, so the column add never rewrites/locks-out old rows.
SELECT core._ensure_column_online('code_node','content_hash','text');
-- GUARDED + IDEMPOTENT CHECK (migration-safe): like claim_basehash_ok — add only if absent; NULL on every
-- pre-existing node satisfies it, so a redeploy over populated data cannot fail.
DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid='core.code_node'::regclass AND conname='code_node_hash_len') THEN
    ALTER TABLE core.code_node ADD CONSTRAINT code_node_hash_len
      CHECK (content_hash IS NULL OR (length(content_hash) <= 64 AND content_hash ~ '^[0-9a-fA-F]+$'));
  END IF;
END $$;
-- ADDITIVE + IDEMPOTENT signature shape (#831's fields persisted): an existing instance gets the seven shape
-- columns via ADD COLUMN IF NOT EXISTS — NULLABLE, NO DEFAULT, NO backfill, so each statement is a
-- catalog-only change that finishes well inside the preDeploy statement_timeout on a populated graph. Every
-- pre-existing node reads NULL (= unknown shape = back-compat) until its file is next re-ingested.
SELECT core._ensure_column_online('code_node','required_arity','int');
SELECT core._ensure_column_online('code_node','optional_arity','int');
SELECT core._ensure_column_online('code_node','has_varargs','boolean');
SELECT core._ensure_column_online('code_node','has_kwargs','boolean');
SELECT core._ensure_column_online('code_node','param_names','text[]');
SELECT core._ensure_column_online('code_node','kwonly_names','text[]');
SELECT core._ensure_column_online('code_node','shape_fingerprint','text');
-- EXPAND-FIRST cg3 resource metadata. All columns are nullable with no default, so this is a catalog-only,
-- backwards-compatible expansion on a populated graph. The new writer lands only after these columns exist.
SELECT core._ensure_column_online('code_node','canonical_key','text');
SELECT core._ensure_column_online('code_node','resource_scope','text');
SELECT core._ensure_column_online('code_node','extractor','text');
SELECT core._ensure_column_online(
  'code_node','confidence','double precision');
SELECT core._ensure_column_online('code_node','provenance','jsonb');
SELECT core._ensure_column_online('code_node','semantic_key','text');
SELECT core._ensure_column_online('code_node','analysis_status','text');
-- GUARDED + IDEMPOTENT CHECK (migration-safe, code_node_hash_len precedent): add only if absent. NOT VALID
-- then VALIDATE so a populated table never scans under the ACCESS EXCLUSIVE lock (the ADD is catalog-only;
-- the VALIDATE scans under the weaker SHARE UPDATE EXCLUSIVE) — and every pre-existing row is all-NULL on
-- these columns, so the VALIDATE trivially passes. On a fresh install the CREATE TABLE above already carries
-- the constraint and this block is a no-op.
DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid='core.code_node'::regclass AND conname='code_node_shape_ok') THEN
    ALTER TABLE core.code_node ADD CONSTRAINT code_node_shape_ok
      CHECK ((required_arity IS NULL OR (required_arity >= 0 AND required_arity <= 512))
        AND (optional_arity IS NULL OR (optional_arity >= 0 AND optional_arity <= 512))
        AND (param_names IS NULL OR COALESCE(array_length(param_names, 1), 0) <= 128)
        AND (kwonly_names IS NULL OR COALESCE(array_length(kwonly_names, 1), 0) <= 128)
        AND (shape_fingerprint IS NULL OR (length(shape_fingerprint) <= 64 AND shape_fingerprint ~ '^[0-9a-fA-F]+$')))
      NOT VALID;
    ALTER TABLE core.code_node VALIDATE CONSTRAINT code_node_shape_ok;
  END IF;
END $$;
DO $$
DECLARE v_definition text;
BEGIN
  SELECT pg_get_constraintdef(oid)
    INTO v_definition
    FROM pg_constraint
   WHERE conrelid='core.code_node'::regclass
     AND conname='code_node_analysis_status_check';
  -- Generation 15 originally introduced failed/ambiguous. The cg4 noded
  -- rollout adds incomplete; repair that already-present expand-first check
  -- instead of letting ADD-IF-MISSING silently preserve the narrower wall.
  IF v_definition IS NULL OR position('''incomplete''' IN v_definition)=0 THEN
    ALTER TABLE core.code_node
      DROP CONSTRAINT IF EXISTS code_node_analysis_status_check;
    ALTER TABLE core.code_node ADD CONSTRAINT code_node_analysis_status_check
      CHECK (
        analysis_status IS NULL
        OR (
          node_kind = ANY (ARRAY['file','config_file'])
          AND analysis_status = ANY (ARRAY['failed','ambiguous','incomplete'])
        )
      ) NOT VALID;
  END IF;
END $$;
DO $$ BEGIN
  IF EXISTS (
    SELECT 1 FROM pg_constraint
     WHERE conrelid='core.code_node'::regclass
       AND conname='code_node_analysis_status_check'
       AND NOT convalidated
  ) THEN
    ALTER TABLE core.code_node VALIDATE CONSTRAINT code_node_analysis_status_check;
  END IF;
END $$;
DO $$
DECLARE v_actual text;
BEGIN
  SELECT regexp_replace(
           replace(pg_get_constraintdef(oid),'::text',''),
           '[[:space:]()]','','g'
         )
    INTO v_actual
    FROM pg_constraint
   WHERE conrelid='core.code_node'::regclass
     AND conname='code_node_semantic_key_shape';
  IF v_actual IS DISTINCT FROM
     'CHECKsemantic_keyISNULLORlengthsemantic_key=64ANDsemantic_key~''^[0-9a-f]{64}$''' THEN
    ALTER TABLE core.code_node DROP CONSTRAINT IF EXISTS code_node_semantic_key_shape;
    ALTER TABLE core.code_node ADD CONSTRAINT code_node_semantic_key_shape
      CHECK (semantic_key IS NULL OR (length(semantic_key)=64 AND semantic_key ~ '^[0-9a-f]{64}$')) NOT VALID;
  END IF;
END $$;
DO $$ BEGIN
  IF EXISTS (
    SELECT 1 FROM pg_constraint
     WHERE conrelid='core.code_node'::regclass AND conname='code_node_semantic_key_shape'
       AND NOT convalidated
  ) THEN
    ALTER TABLE core.code_node VALIDATE CONSTRAINT code_node_semantic_key_shape;
  END IF;
END $$;
-- WIDEN, never narrow, the persisted-kind wall before complete-contract writers start. CREATE TABLE IF NOT EXISTS does not
-- update an existing constraint, so a guarded replacement is required for live databases. Compare the COMPLETE
-- literal set, not two sentinels: a partially-applied/manual constraint which happened to contain the sentinels
-- but omitted an intermediate kind must converge on re-apply instead of leaving the new App boot-looping.
--
-- LOCK DISCIPLINE IS DELIBERATELY TWO TRANSACTIONS. This first DO only performs the catalog-only DROP + ADD NOT
-- VALID while holding ACCESS EXCLUSIVE. psql commits the statement before the second DO performs the table scan,
-- so VALIDATE runs with SHARE UPDATE EXCLUSIVE and does not inherit ACCESS EXCLUSIVE for the duration of the scan.
DO $$
DECLARE
  v_expected text[] := ARRAY[
    'file','def','class','table','column','config_file','config_key',
    'iac_resource','k8s_resource','api_type','api_message','api_service','api_operation','api_schema',
    'ci_script','app_command','job_task','job_queue','sibling_stem','role_feature'
  ];
  v_actual text[];
BEGIN
  SELECT ARRAY(
    SELECT DISTINCT pieces[1]
      FROM pg_constraint c
      CROSS JOIN LATERAL regexp_matches(
        pg_get_constraintdef(c.oid), '''([^'']+)''', 'g') AS r(pieces)
     WHERE c.conrelid='core.code_node'::regclass AND c.conname='code_node_kind_check'
     ORDER BY pieces[1]
  ) INTO v_actual;
  SELECT ARRAY(SELECT k FROM unnest(v_expected) AS k ORDER BY k) INTO v_expected;
  IF v_actual IS DISTINCT FROM v_expected THEN
    ALTER TABLE core.code_node DROP CONSTRAINT IF EXISTS code_node_kind_check;
    ALTER TABLE core.code_node ADD CONSTRAINT code_node_kind_check CHECK (node_kind = ANY (ARRAY[
      'file','def','class','table','column','config_file','config_key',
      'iac_resource','k8s_resource','api_type','api_message','api_service','api_operation','api_schema',
      'ci_script','app_command','job_task','job_queue','sibling_stem','role_feature'
    ])) NOT VALID;
  END IF;
END $$;
DO $$ BEGIN
  IF EXISTS (
    SELECT 1 FROM pg_constraint
     WHERE conrelid='core.code_node'::regclass AND conname='code_node_kind_check' AND NOT convalidated
  ) THEN
    ALTER TABLE core.code_node VALIDATE CONSTRAINT code_node_kind_check;
  END IF;
END $$;
-- Bounded nullable metadata constraints, guarded for idempotent hot re-apply. Legacy rows are NULL and satisfy
-- every check; future malformed metadata is rejected at the table wall rather than silently truncated. ADDs and
-- VALIDATE are split for the same lock-lifetime reason as the kind wall above.
DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid='core.code_node'::regclass AND conname='code_node_canonical_key_len') THEN
    ALTER TABLE core.code_node ADD CONSTRAINT code_node_canonical_key_len
      CHECK (canonical_key IS NULL OR length(canonical_key) <= 1600) NOT VALID;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid='core.code_node'::regclass AND conname='code_node_resource_scope_len') THEN
    ALTER TABLE core.code_node ADD CONSTRAINT code_node_resource_scope_len
      CHECK (resource_scope IS NULL OR length(resource_scope) <= 1024) NOT VALID;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid='core.code_node'::regclass AND conname='code_node_extractor_len') THEN
    ALTER TABLE core.code_node ADD CONSTRAINT code_node_extractor_len
      CHECK (extractor IS NULL OR length(extractor) <= 128) NOT VALID;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid='core.code_node'::regclass AND conname='code_node_confidence_range') THEN
    ALTER TABLE core.code_node ADD CONSTRAINT code_node_confidence_range
      CHECK (confidence IS NULL OR confidence BETWEEN 0.0 AND 1.0) NOT VALID;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid='core.code_node'::regclass AND conname='code_node_provenance_shape') THEN
    ALTER TABLE core.code_node ADD CONSTRAINT code_node_provenance_shape
      CHECK (provenance IS NULL OR (jsonb_typeof(provenance)='object' AND octet_length(provenance::text) <= 8192))
      NOT VALID;
  END IF;
END $$;
DO $$
DECLARE v_name text;
BEGIN
  FOREACH v_name IN ARRAY ARRAY[
    'code_node_canonical_key_len','code_node_resource_scope_len','code_node_extractor_len',
    'code_node_confidence_range','code_node_provenance_shape'
  ] LOOP
    IF EXISTS (
      SELECT 1 FROM pg_constraint
       WHERE conrelid='core.code_node'::regclass AND conname=v_name AND NOT convalidated
    ) THEN
      EXECUTE format('ALTER TABLE core.code_node VALIDATE CONSTRAINT %I', v_name);
    END IF;
  END LOOP;
END $$;
CREATE INDEX CONCURRENTLY IF NOT EXISTS code_node_coord_path ON core.code_node (account_id, repo, branch, path);
-- NODE-ID-keyed companion to code_node_coord_path (PERF audit:perf 2026-06-23, EXPLAIN-ANALYZE-measured). The
-- coord_path index above serves the PATH-keyed lookups (the file/symbol scans that filter/group by path). But
-- core._claim_adjacency joins code_node on its OTHER key — `node_id` (node_file/edited_nodes ↔ code_edge.src,
-- the in_adj/out_adj import-confirmation probes that resolve an edge endpoint to the file that owns it). code_node
-- had NO index on node_id, so every such lookup was a SEQ SCAN of the whole coordinate's node set: at scale / on a
-- freshly-ingested (cold-stats) tenant the planner picks a NESTED LOOP and re-seq-scans code_node PER probed edge
-- (MEASURED: `code_node WHERE node_id=…` = Seq Scan cost 71.65, 1826 rows removed for a SINGLE match — the 8.3s
-- @20k-node/200-claim blow-up _claim_adjacency's own comment records, same path as the gate-160 cold-stats spike).
-- This (account_id, repo, branch, node_id) index — RLS account pin + coordinate + the join key, exact column order
-- so the planner can use the full predicate — flips that to an INDEX SCAN (MEASURED: cost 0.38..8.41, 0.02ms,
-- Index Cond on node_id). Pure ADDITIVE + IDEMPOTENT (IF NOT EXISTS) → migration-safe, no behavior change.
-- The concurrent build does not lock out live writes. It may still wait for a
-- pre-existing writer/old snapshot and therefore remains bounded by predeploy.
CREATE INDEX CONCURRENTLY IF NOT EXISTS code_node_coord_nodeid ON core.code_node (account_id, repo, branch, node_id);

CREATE TABLE IF NOT EXISTS core.code_edge (
    account_id text NOT NULL,
    repo text DEFAULT '' NOT NULL,
    branch text DEFAULT '' NOT NULL,
    src text NOT NULL,
    dst text NOT NULL,
    edge_kind text NOT NULL,
    -- SHA-256 of the exact pre-sanitized extractor dst. New graphs always populate it; NULL marks legacy rows
    -- whose effective key is derived from the already-sanitized dst for behavior-compatible migration.
    semantic_dst_key text,
    -- Explicit local-reference uncertainty. NULL means the edge is resolved evidence; `ambiguous` retains
    -- multiple local candidates and `unresolved` retains a syntactically local reference with zero repository
    -- candidates. Both are excluded from every effective adjacency consumer. Their source path falls back to
    -- Unknown unless a stronger serialize/warn signal already exists.
    reference_status text,
    -- cg3 stores the complete extractor edge contract. alters_col/queries_col are evidence-only at this stage:
    -- persistence/hash/catalog retain them, while effective adjacency deliberately remains table-level so this
    -- schema-alignment rollout cannot change existing review verdicts or hub damping.
    CONSTRAINT code_edge_kind_check CHECK (edge_kind = ANY (ARRAY[
      'contains','calls','imports','queries','alters','reads_config','alters_col','queries_col'
    ])),
    CONSTRAINT code_edge_src_len CHECK (length(src) <= 1024),
    CONSTRAINT code_edge_dst_len CHECK (length(dst) <= 1600),
    CONSTRAINT code_edge_semantic_dst_key_shape CHECK (
      semantic_dst_key IS NULL OR
      (length(semantic_dst_key)=64 AND semantic_dst_key ~ '^[0-9a-f]{64}$')),
    CONSTRAINT code_edge_reference_status_check CHECK (
      reference_status IS NULL
      OR reference_status = ANY (ARRAY['ambiguous','unresolved'])),
    CONSTRAINT code_edge_repo_len CHECK (length(repo) <= 512),
    CONSTRAINT code_edge_branch_len CHECK (length(branch) <= 512)
);
SELECT core._ensure_column_online('code_edge','semantic_dst_key','text');
SELECT core._ensure_column_online('code_edge','reference_status','text');
DO $$
DECLARE v_actual text;
BEGIN
  SELECT regexp_replace(
           replace(pg_get_constraintdef(oid),'::text',''),
           '[[:space:]()]','','g'
         )
    INTO v_actual
    FROM pg_constraint
   WHERE conrelid='core.code_edge'::regclass
     AND conname='code_edge_semantic_dst_key_shape';
  IF v_actual IS DISTINCT FROM
     'CHECKsemantic_dst_keyISNULLORlengthsemantic_dst_key=64ANDsemantic_dst_key~''^[0-9a-f]{64}$''' THEN
    ALTER TABLE core.code_edge DROP CONSTRAINT IF EXISTS code_edge_semantic_dst_key_shape;
    ALTER TABLE core.code_edge ADD CONSTRAINT code_edge_semantic_dst_key_shape
      CHECK (
        semantic_dst_key IS NULL OR
        (length(semantic_dst_key)=64 AND semantic_dst_key ~ '^[0-9a-f]{64}$')
      ) NOT VALID;
  END IF;
END $$;
DO $$
DECLARE
  v_expected text[] := ARRAY['ambiguous','unresolved'];
  v_actual text[];
BEGIN
  SELECT ARRAY(
    SELECT DISTINCT pieces[1]
      FROM pg_constraint c
      CROSS JOIN LATERAL regexp_matches(
        pg_get_constraintdef(c.oid), '''([^'']+)''', 'g'
      ) AS r(pieces)
     WHERE c.conrelid='core.code_edge'::regclass
       AND c.conname='code_edge_reference_status_check'
     ORDER BY pieces[1]
  ) INTO v_actual;
  SELECT ARRAY(
    SELECT status FROM unnest(v_expected) AS status ORDER BY status
  ) INTO v_expected;
  IF v_actual IS DISTINCT FROM v_expected THEN
    ALTER TABLE core.code_edge
      DROP CONSTRAINT IF EXISTS code_edge_reference_status_check;
    ALTER TABLE core.code_edge ADD CONSTRAINT code_edge_reference_status_check
      CHECK (
        reference_status IS NULL
        OR reference_status = ANY (ARRAY['ambiguous','unresolved'])
      ) NOT VALID;
  END IF;
END $$;
DO $$ BEGIN
  IF EXISTS (
    SELECT 1 FROM pg_constraint
     WHERE conrelid='core.code_edge'::regclass
       AND conname='code_edge_reference_status_check'
       AND NOT convalidated
  ) THEN
    ALTER TABLE core.code_edge VALIDATE CONSTRAINT code_edge_reference_status_check;
  END IF;
END $$;
DO $$ BEGIN
  IF EXISTS (
    SELECT 1 FROM pg_constraint
     WHERE conrelid='core.code_edge'::regclass AND conname='code_edge_semantic_dst_key_shape'
       AND NOT convalidated
  ) THEN
    ALTER TABLE core.code_edge VALIDATE CONSTRAINT code_edge_semantic_dst_key_shape;
  END IF;
END $$;
-- Expand the live edge constraint before deploying cg3 writers. Exact-set guard + split validation mirror the
-- node wall: malformed partial state converges, and no ACCESS EXCLUSIVE lock survives into the validation scan.
DO $$
DECLARE
  v_expected text[] := ARRAY[
    'contains','calls','imports','queries','alters','reads_config','alters_col','queries_col'
  ];
  v_actual text[];
BEGIN
  SELECT ARRAY(
    SELECT DISTINCT pieces[1]
      FROM pg_constraint c
      CROSS JOIN LATERAL regexp_matches(
        pg_get_constraintdef(c.oid), '''([^'']+)''', 'g') AS r(pieces)
     WHERE c.conrelid='core.code_edge'::regclass AND c.conname='code_edge_kind_check'
     ORDER BY pieces[1]
  ) INTO v_actual;
  SELECT ARRAY(SELECT k FROM unnest(v_expected) AS k ORDER BY k) INTO v_expected;
  IF v_actual IS DISTINCT FROM v_expected THEN
    ALTER TABLE core.code_edge DROP CONSTRAINT IF EXISTS code_edge_kind_check;
    ALTER TABLE core.code_edge ADD CONSTRAINT code_edge_kind_check CHECK (edge_kind = ANY (ARRAY[
      'contains','calls','imports','queries','alters','reads_config','alters_col','queries_col'
    ])) NOT VALID;
  END IF;
END $$;
DO $$ BEGIN
  IF EXISTS (
    SELECT 1 FROM pg_constraint
     WHERE conrelid='core.code_edge'::regclass AND conname='code_edge_kind_check' AND NOT convalidated
  ) THEN
    ALTER TABLE core.code_edge VALIDATE CONSTRAINT code_edge_kind_check;
  END IF;
END $$;
CREATE INDEX CONCURRENTLY IF NOT EXISTS code_edge_coord ON core.code_edge (account_id, repo, branch);
CREATE INDEX CONCURRENTLY IF NOT EXISTS code_edge_coord_kind_dst ON core.code_edge (account_id, repo, branch, edge_kind, dst);
-- SRC-keyed mirror of the dst index (PERF audit:perf 2026-06-18). The dst index above serves the GROUP BY dst
-- hub/fan-in scans + dst-keyed joins (imp_in, res_adj). But _claim_adjacency's out_adj/in_adj probe the OTHER
-- end: a correlated EXISTS / anti-join on `ie.src=<edited file>` (the import-confirmation check) and joins on
-- ce.src — none of which the (…,dst) index can serve, so each fell back to a full code_edge seq scan PER probed
-- file. This (…,edge_kind,src) leading-prefix index makes those src-keyed lookups an index scan, the heaviest
-- read on large graphs (~100k edges, many in-flight PRs). ADDITIVE + IDEMPOTENT (IF NOT EXISTS) → migration-safe.
CREATE INDEX CONCURRENTLY IF NOT EXISTS code_edge_coord_kind_src ON core.code_edge (account_id, repo, branch, edge_kind, src);

-- NEVER-REUSED GRAPH WRITE TOKEN.  A row-local counter is not sufficient: lifecycle/offboarding may delete a
-- graph_version row and later recreate the same mutable coordinate at the same SHA, allowing an old token value
-- to recur.  PostgreSQL sequences are non-transactional and NO CYCLE, so even rolled-back writes leave gaps
-- rather than reusing a token.  Full and patch writers stamp nextval() while holding the coordinate lock.
CREATE SEQUENCE IF NOT EXISTS core.graph_revision_seq
  AS bigint MINVALUE 1 MAXVALUE 999999999999999999 NO CYCLE;
-- ALTER SEQUENCE takes a lock that conflicts with live nextval() users even
-- when ownership is already correct. Check the catalog first so a declarative
-- replay cannot queue behind one graph-ingest transaction and convoy every
-- later graph writer behind the deploy.
DO $$
BEGIN
  IF EXISTS (
    SELECT 1
      FROM pg_class c
      JOIN pg_namespace n ON n.oid=c.relnamespace
     WHERE n.nspname='core'
       AND c.relname='graph_revision_seq'
       AND c.relkind='S'
       AND c.relowner <> 'veripsa_migrator'::regrole
  ) THEN
    ALTER SEQUENCE core.graph_revision_seq OWNER TO veripsa_migrator;
  END IF;
END $$;

-- graph_version: the as-of per COORDINATE — PK (account, repo, branch) so many coexist (no tenant-wipe).
CREATE TABLE IF NOT EXISTS core.graph_version (
    account_id text NOT NULL,
    repo text DEFAULT '' NOT NULL,
    branch text DEFAULT '' NOT NULL,
    commit_sha text,
    captured_at timestamptz,
    ingested_at timestamptz DEFAULT now() NOT NULL,
    node_count integer,
    edge_count integer,
    -- RENAME-STABLE IDENTITY (content-free): GitHub's repository.id is a fixed numeric id that survives a repo
    -- rename AND an owner-login rename — UNLIKE `repo` (= full_name = owner/name), which CHANGES on either. The
    -- coordinate keys the whole working set by `repo`, so a rename mints a NEW coordinate and ORPHANS the old one
    -- (it sits at a stale sha, behind main HEAD FOREVER → graph_stale spams, never self-heals — the prod orphan
    -- example-user/veripsa-core-old after the account login renamed to RollNuts). NULLABLE + back-compat: a pre-existing
    -- coordinate (or a non-GH dogfood one) leaves it NULL until the next push stamps it (reconcile_repo_identity).
    -- Once stamped, an incoming event whose stable id matches a DIFFERENTLY-NAMED coordinate is detected as a
    -- rename and MIGRATED old→new (reconcile_repo_identity_with_authority) instead of orphaning. Bounded so a junk
    -- value can never be stored (GitHub ids are positive integers; we store the string form, ≤ 32 digits).
    repo_id text,
    -- EXTRACTOR/SCHEMA VERSION STAMP (content-free — a bounded version TOKEN only, never bodies/counts). The
    -- stored graph's edges/nodes were produced by ONE version of the extraction logic (code_graph_extract →
    -- core.ingest_graph_with_authority). Freshness previously keyed ONLY on commit_sha == HEAD, so after an
    -- extractor UPGRADE an UNMOVED repo (same HEAD) read "fresh" and kept computing coupling on OLD-extractor
    -- edges — a coupling only the NEW extractor finds was SILENTLY missed (an indefinite false `clear`, G3). We
    -- stamp the explicitly validated PRODUCER version at every FULL ingest; graph_freshness compares that value
    -- with core.current_extractor_version(). A stored version that DIFFERS from current — OR is NULL (a legacy
    -- row never re-ingested since the stamp landed) — behaves exactly like a lagged SHA (behind=True → self-heal
    -- re-ingests with the current producer; the G1 withhold covers a failed re-ingest as `unknown`, never a false
    -- clear). NULLABLE + back-compat: every pre-existing coordinate is NULL until its next FULL re-ingest.
    extractor_version text,
    -- CANONICAL PERSISTED GRAPH IDENTITY (cg4): SHA-256 over the deterministic, sorted persisted node/edge
    -- projection. It intentionally excludes timestamps, DB ids and observability so full and incremental writes
    -- of the same coordinate state produce the same value. NULL marks a legacy/pre-cg4 row awaiting full re-ingest.
    graph_hash text,
    -- Bounded content-free ingest telemetry. The writer merges extractor-supplied metrics with authoritative
    -- persistence counts, exclusion reasons, evidence-only kinds and graph_hash.
    observability jsonb,
    -- NEVER-REUSED GRAPH REVISION (content-free): sourced from graph_revision_seq on every successful full/patch
    -- graph replacement. commit_sha alone is not a safe compare-and-swap token because a concurrent P→B→P
    -- sequence returns to the same SHA (ABA) while changing the stored graph twice; a coordinate-local counter
    -- also repeats after row deletion/recreation. Patch writers match BOTH SHA and revision before first DELETE.
    graph_revision bigint DEFAULT 0 NOT NULL,
    -- Semantic reference storage generation. 0 = legacy display-only rows; 1 = every persisted Edge and every
    -- resolvable Node carries the exact-reference SHA-256 key. Incremental patching is allowed only at 1.
    semantic_ref_version smallint DEFAULT 0 NOT NULL,
    CONSTRAINT graph_version_pkey PRIMARY KEY (account_id, repo, branch),
    CONSTRAINT graph_version_sha CHECK (commit_sha IS NULL OR (length(commit_sha) <= 64 AND commit_sha ~ '^[0-9a-fA-F]+$')),
    CONSTRAINT graph_version_repo_id_shape CHECK (
      repo_id IS NULL OR (length(repo_id) BETWEEN 1 AND 32 AND repo_id ~ '^[1-9][0-9]*$')),
    -- the version stamp is a bounded reference TOKEN (safe charset, ≤ 32 chars) — a junk value can never be
    -- stored (the wall, not just the writer). NULL = legacy/unknown (treated as behind), never "assumed fresh".
    CONSTRAINT graph_version_extractor_version_shape CHECK (
      extractor_version IS NULL OR (length(extractor_version) BETWEEN 1 AND 32 AND extractor_version ~ '^[A-Za-z0-9_.:-]+$')),
    CONSTRAINT graph_version_graph_hash_shape CHECK (
      graph_hash IS NULL OR (length(graph_hash)=64 AND graph_hash ~ '^[0-9a-f]{64}$')),
    CONSTRAINT graph_version_observability_shape CHECK (
      observability IS NULL OR (jsonb_typeof(observability)='object' AND octet_length(observability::text) <= 131072)),
    CONSTRAINT graph_version_revision_shape CHECK (
      graph_revision BETWEEN 0 AND 999999999999999999),
    CONSTRAINT graph_version_semantic_ref_version_shape CHECK (
      semantic_ref_version IN (0,1))
);
-- ADDITIVE + IDEMPOTENT (migration-safe): an existing instance gets the rename-stable id column via ADD COLUMN IF
-- NOT EXISTS — NULL on every pre-existing coordinate (unknown id → reconcile stamps it on the next push), so a
-- redeploy over a populated graph_version never rewrites/locks-out old rows. The GUARDED CHECK is added only if
-- absent; NULL on every old row satisfies it, so the constraint add cannot fail on populated data.
SELECT core._ensure_column_online('graph_version','repo_id','text');
DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid='core.graph_version'::regclass AND conname='graph_version_repo_id_shape') THEN
    ALTER TABLE core.graph_version ADD CONSTRAINT graph_version_repo_id_shape
      CHECK (repo_id IS NULL OR (length(repo_id) BETWEEN 1 AND 32 AND repo_id ~ '^[1-9][0-9]*$'));
  END IF;
END $$;
-- ADDITIVE + IDEMPOTENT (migration-safe, the repo_id precedent above): an existing instance gets the extractor
-- version stamp via ADD COLUMN IF NOT EXISTS — NULL on every pre-existing coordinate (unknown extractor version →
-- graph_freshness treats it as behind → the next FULL re-ingest stamps it), so a redeploy over a populated
-- graph_version never rewrites/locks-out old rows. NULLABLE, NO DEFAULT, NO backfill → catalog-only on a populated
-- table (finishes well inside the preDeploy statement_timeout). The GUARDED CHECK is added only if absent; NULL on
-- every old row satisfies it, so the constraint add cannot fail on populated data.
SELECT core._ensure_column_online(
  'graph_version','extractor_version','text');
SELECT core._ensure_column_online('graph_version','graph_hash','text');
SELECT core._ensure_column_online('graph_version','observability','jsonb');
-- Existing rows start at reserved legacy revision zero. The constant DEFAULT is metadata-only on supported
-- PostgreSQL versions; the next official full write replaces it with a never-reused sequence value.
SELECT core._ensure_column_online(
  'graph_version','graph_revision','bigint NOT NULL DEFAULT 0');
SELECT core._ensure_column_online(
  'graph_version','semantic_ref_version',
  'smallint NOT NULL DEFAULT 0');
DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid='core.graph_version'::regclass AND conname='graph_version_extractor_version_shape') THEN
    ALTER TABLE core.graph_version ADD CONSTRAINT graph_version_extractor_version_shape
      CHECK (extractor_version IS NULL OR (length(extractor_version) BETWEEN 1 AND 32 AND extractor_version ~ '^[A-Za-z0-9_.:-]+$'));
  END IF;
END $$;
DO $$
DECLARE v_actual text;
BEGIN
  SELECT regexp_replace(
           replace(pg_get_constraintdef(oid),'::text',''),
           '[[:space:]()]','','g'
         )
    INTO v_actual
    FROM pg_constraint
   WHERE conrelid='core.graph_version'::regclass
     AND conname='graph_version_semantic_ref_version_shape';
  IF v_actual IS DISTINCT FROM
     'CHECKsemantic_ref_version=ANYARRAY[0,1]' THEN
    ALTER TABLE core.graph_version
      DROP CONSTRAINT IF EXISTS graph_version_semantic_ref_version_shape;
    ALTER TABLE core.graph_version ADD CONSTRAINT graph_version_semantic_ref_version_shape
      CHECK (semantic_ref_version IN (0,1)) NOT VALID;
  END IF;
END $$;
DO $$ BEGIN
  IF EXISTS (
    SELECT 1 FROM pg_constraint
     WHERE conrelid='core.graph_version'::regclass
       AND conname='graph_version_semantic_ref_version_shape'
       AND NOT convalidated
  ) THEN
    ALTER TABLE core.graph_version VALIDATE CONSTRAINT graph_version_semantic_ref_version_shape;
  END IF;
END $$;
DO $$ BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
     WHERE conrelid='core.graph_version'::regclass AND conname='graph_version_graph_hash_shape'
       AND pg_get_constraintdef(oid) LIKE '%length(graph_hash) = 64%'
       AND pg_get_constraintdef(oid) LIKE '%^[0-9a-f]{64}$%'
  ) THEN
    ALTER TABLE core.graph_version DROP CONSTRAINT IF EXISTS graph_version_graph_hash_shape;
    ALTER TABLE core.graph_version ADD CONSTRAINT graph_version_graph_hash_shape
      CHECK (graph_hash IS NULL OR (length(graph_hash)=64 AND graph_hash ~ '^[0-9a-f]{64}$')) NOT VALID;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid='core.graph_version'::regclass AND conname='graph_version_observability_shape') THEN
    ALTER TABLE core.graph_version ADD CONSTRAINT graph_version_observability_shape
      CHECK (observability IS NULL OR (jsonb_typeof(observability)='object' AND octet_length(observability::text) <= 131072))
      NOT VALID;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid='core.graph_version'::regclass AND conname='graph_version_revision_shape') THEN
    ALTER TABLE core.graph_version ADD CONSTRAINT graph_version_revision_shape
      CHECK (graph_revision BETWEEN 0 AND 999999999999999999) NOT VALID;
  END IF;
END $$;
DO $$
DECLARE v_name text;
BEGIN
  FOREACH v_name IN ARRAY ARRAY[
    'graph_version_graph_hash_shape','graph_version_observability_shape','graph_version_revision_shape'
  ] LOOP
    IF EXISTS (
      SELECT 1 FROM pg_constraint
       WHERE conrelid='core.graph_version'::regclass AND conname=v_name AND NOT convalidated
    ) THEN
      EXECUTE format('ALTER TABLE core.graph_version VALIDATE CONSTRAINT %I', v_name);
    END IF;
  END LOOP;
END $$;
-- (account_id, repo_id) index: reconcile_repo_identity_with_authority looks up "is there ANY coordinate for THIS
-- stable id under this tenant?" (the rename probe) — an index scan, not a per-push seq scan of graph_version.
-- ADDITIVE + IDEMPOTENT. The concurrent build does not lock out live writes; it
-- may still wait for a pre-existing writer/old snapshot and is bounded by predeploy.
CREATE INDEX CONCURRENTLY IF NOT EXISTS graph_version_repo_id ON core.graph_version (account_id, repo_id) WHERE repo_id IS NOT NULL;

-- event: THE ONE append-only fact ledger — the no-乱立 law. Facts are KINDS here (collision_held · push ·
-- drift · future signals), each written by its own gate fn (the kind is open text, but only a gate fn can
-- write it — the forgery token blocks direct writes, so no garbage kind can enter). Typed common columns
-- + a few TYPED nullable extensions; content-free. A new signal is a new KIND (a row), never a new table.
CREATE TABLE IF NOT EXISTS core.event (
    event_id text NOT NULL,
    account_id text NOT NULL,
    kind text NOT NULL,                 -- collision_held | push | drift | compat_finding | <future via a gate fn>
    agent_id text NOT NULL,             -- the primary actor
    counterparty_agent text,            -- the other agent (e.g. a collision's holder); nullable
    repo text DEFAULT '' NOT NULL,
    branch text DEFAULT '' NOT NULL,
    path text DEFAULT '' NOT NULL,      -- '' for repo-level facts (e.g. push)
    commit_sha text,                    -- push; nullable
    model text,                         -- push model attribution; nullable
    detail text,                        -- a short content-free label; nullable (never a body)
    -- COMPATIBILITY FINDING extensions (typed nullable, the `model` precedent — kind 'compat_finding',
    -- written only by record_compat_finding_with_authority). counterparty_sha is the OTHER head of the
    -- analyzed pair (producer head lives in commit_sha; consumer head here) — a hex fingerprint, NEVER
    -- content. fact_fingerprint is the finding's stable identity hash (a fingerprint over content-free
    -- shape facts — NEVER a default-value expression, NEVER a body) — the dedupe key. NULL on every
    -- other kind (back-compat: pre-existing rows read NULL and every other gate fn leaves them NULL).
    counterparty_sha text,
    fact_fingerprint text,
    -- COMPAT TAXONOMY + DETECTOR STAMP (corrective lane S3a — docs/COMPATIBILITY_TRAFFIC_CONTROL_PLAN.md
    -- §2.4/§3 lane 3; typed nullable, the counterparty_sha precedent). fact_class is the finding's EXPLICIT
    -- classification — a bounded enum of four codes, so "is this row an observation or a proven
    -- incompatibility" is a COLUMN EQUALITY, never a reason-string parse. ONLY consumer_call_mismatch:* rows
    -- carry 'evidence_backed_incompatibility'; the other three classes are observation telemetry, never a
    -- breakage claim. detector is the writing detector's identity+version as ONE bounded reference token
    -- ('python-call-compat/py-call-v1') — equality-queryable, so a future current-surface read (lane S3b)
    -- can exclude rows an older detector wrote without parsing anything. NULL on every other kind AND on
    -- pre-S3a compat rows (append-only legacy evidence — unclassified, never treated as current).
    fact_class text,
    detector text,
    occurred_at timestamptz DEFAULT now() NOT NULL,
    visibility text DEFAULT 'private' NOT NULL,
    CONSTRAINT event_pkey PRIMARY KEY (account_id, event_id),
    CONSTRAINT event_kind_len CHECK (length(kind) <= 64),
    CONSTRAINT event_repo_len CHECK (length(repo) <= 512),
    CONSTRAINT event_branch_len CHECK (length(branch) <= 512),
    CONSTRAINT event_path_len CHECK (length(path) <= 1024),
    CONSTRAINT event_sha_shape CHECK (commit_sha IS NULL OR (length(commit_sha) <= 64 AND commit_sha ~ '^[0-9a-fA-F]+$')),
    CONSTRAINT event_model_len CHECK (model IS NULL OR length(model) <= 128),
    CONSTRAINT event_detail_len CHECK (detail IS NULL OR length(detail) <= 200),
    -- same family as event_sha_shape, with an explicit 7-hex floor (a short-SHA is the minimum honest
    -- head reference; a junk 1-char "sha" can never be stored).
    CONSTRAINT event_counterparty_sha_shape CHECK (counterparty_sha IS NULL OR counterparty_sha ~ '^[0-9a-fA-F]{7,64}$'),
    -- a fingerprint is a bounded reference token (hex or a prefixed hash form) — safe charset, never a body.
    CONSTRAINT event_fact_fingerprint_shape CHECK (fact_fingerprint IS NULL OR (length(fact_fingerprint) <= 128 AND fact_fingerprint ~ '^[A-Za-z0-9_.:-]+$')),
    -- the classification is a CLOSED enum (S3a): a junk/invented class can never be stored, even by a
    -- token-armed owner insert — the wall, not just the gate fn.
    CONSTRAINT event_fact_class_ok CHECK (fact_class IS NULL OR fact_class = ANY (ARRAY[
      'contract_delta_observation','rebase_needed_observation',
      'divergent_definition_observation','evidence_backed_incompatibility'])),
    -- the detector stamp is a bounded reference token ('<name>/<version>') — safe charset, never a body.
    CONSTRAINT event_detector_shape CHECK (detector IS NULL OR (length(detector) <= 64 AND detector ~ '^[A-Za-z0-9_./-]+$')),
    CONSTRAINT event_vis_check CHECK (visibility = ANY (ARRAY['private','team','public']))
);
-- ADDITIVE + IDEMPOTENT (migration-safe, the code_node shape-column precedent): an existing instance gets the
-- two compat-finding columns via ADD COLUMN IF NOT EXISTS — NULLABLE, NO DEFAULT, NO backfill, so each
-- statement is a catalog-only change that finishes well inside the preDeploy statement_timeout on a populated
-- ledger. Every pre-existing event row reads NULL (no other kind ever sets them).
SELECT core._ensure_column_online('event','counterparty_sha','text');
SELECT core._ensure_column_online('event','fact_fingerprint','text');
-- ADDITIVE + IDEMPOTENT (corrective lane S3a, same precedent): the classification + detector-stamp columns.
-- NULLABLE, NO DEFAULT, NO backfill — catalog-only on a populated ledger; every pre-existing row (including
-- pre-S3a compat rows) reads NULL = unclassified legacy evidence.
SELECT core._ensure_column_online('event','fact_class','text');
SELECT core._ensure_column_online('event','detector','text');
-- GUARDED + IDEMPOTENT CHECKs (code_node_shape_ok precedent): add only if absent. NOT VALID then VALIDATE so
-- a populated table never scans under the ACCESS EXCLUSIVE lock (the ADD is catalog-only; the VALIDATE scans
-- under the weaker SHARE UPDATE EXCLUSIVE) — and every pre-existing row is NULL on these columns, so the
-- VALIDATE trivially passes. On a fresh install the CREATE TABLE above already carries both constraints and
-- this block is a no-op.
DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid='core.event'::regclass AND conname='event_counterparty_sha_shape') THEN
    ALTER TABLE core.event ADD CONSTRAINT event_counterparty_sha_shape
      CHECK (counterparty_sha IS NULL OR counterparty_sha ~ '^[0-9a-fA-F]{7,64}$') NOT VALID;
    ALTER TABLE core.event VALIDATE CONSTRAINT event_counterparty_sha_shape;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid='core.event'::regclass AND conname='event_fact_fingerprint_shape') THEN
    ALTER TABLE core.event ADD CONSTRAINT event_fact_fingerprint_shape
      CHECK (fact_fingerprint IS NULL OR (length(fact_fingerprint) <= 128 AND fact_fingerprint ~ '^[A-Za-z0-9_.:-]+$')) NOT VALID;
    ALTER TABLE core.event VALIDATE CONSTRAINT event_fact_fingerprint_shape;
  END IF;
  -- S3a: the closed classification enum + the bounded detector token (same NOT VALID → VALIDATE discipline;
  -- every pre-existing row is NULL on both columns, so the VALIDATE trivially passes).
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid='core.event'::regclass AND conname='event_fact_class_ok') THEN
    ALTER TABLE core.event ADD CONSTRAINT event_fact_class_ok
      CHECK (fact_class IS NULL OR fact_class = ANY (ARRAY[
        'contract_delta_observation','rebase_needed_observation',
        'divergent_definition_observation','evidence_backed_incompatibility'])) NOT VALID;
    ALTER TABLE core.event VALIDATE CONSTRAINT event_fact_class_ok;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid='core.event'::regclass AND conname='event_detector_shape') THEN
    ALTER TABLE core.event ADD CONSTRAINT event_detector_shape
      CHECK (detector IS NULL OR (length(detector) <= 64 AND detector ~ '^[A-Za-z0-9_./-]+$')) NOT VALID;
    ALTER TABLE core.event VALIDATE CONSTRAINT event_detector_shape;
  END IF;
END $$;
CREATE INDEX CONCURRENTLY IF NOT EXISTS event_by_account_kind ON core.event (account_id, kind, occurred_at DESC);
CREATE INDEX CONCURRENTLY IF NOT EXISTS event_by_account_repo_occurred ON core.event (account_id, repo, occurred_at DESC);

-- intent: the agent's DECLARED scope, so drift (touched-but-out-of-scope) is measured by FACT. One
-- current (non-superseded) intent per (account, agent, work_ref); a re-declare supersedes. Content-free.
CREATE TABLE IF NOT EXISTS core.intent (
    intent_id text NOT NULL,
    account_id text NOT NULL,
    agent_id text NOT NULL,
    work_ref text NOT NULL,
    summary text NOT NULL,
    scope_in text[] NOT NULL,
    scope_out text[],
    superseded boolean DEFAULT false NOT NULL,
    declared_at timestamptz DEFAULT now() NOT NULL,
    CONSTRAINT intent_pkey PRIMARY KEY (account_id, intent_id),
    CONSTRAINT intent_summary_len CHECK (length(summary) BETWEEN 1 AND 400),
    CONSTRAINT intent_workref_len CHECK (length(work_ref) <= 200),
    CONSTRAINT intent_scope_in_ok CHECK (array_length(scope_in, 1) >= 1)
);
CREATE INDEX CONCURRENTLY IF NOT EXISTS intent_current ON core.intent (account_id, agent_id, work_ref) WHERE superseded = false;

-- Hot-deploy owner repair. A managed Postgres deploy can apply the schema as a role that is a MEMBER of
-- veripsa_migrator, which makes newly-created tables owned by that deploy role. Repair those cases, but do not
-- re-run ALTER OWNER on already-correct busy tables during every Render preDeploy; ALTER OWNER takes a table DDL
-- lock and can block behind the old live instance's event writes.
DO $$
DECLARE t text;
BEGIN
  FOREACH t IN ARRAY ARRAY['claim','code_node','code_edge','graph_version','event','intent'] LOOP
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

-- ── the moat PATTERN applied uniformly: FORCE RLS + account isolation on every core table; forgery block
--    on every gated table; append-only on the `event` ledger (a fact is permanent). ───────────────────
DO $$
DECLARE t text;
BEGIN
  FOREACH t IN ARRAY ARRAY['claim','code_node','code_edge','graph_version','event','intent'] LOOP
    -- Avoid re-taking table DDL locks on every deploy. On a hot service this block is re-applied by
    -- preDeployCommand while the old instance is still serving traffic; unconditional ALTER/DROP/CREATE here can
    -- deadlock with live reads of core.event. Only create missing moat pieces; if a future policy/trigger changes,
    -- ship that as an explicit migration rather than silently rebuilding it on every deploy.
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
  -- the event ledger is APPEND-ONLY (a recorded fact is permanent); claim/graph are live/mutable.
  IF NOT EXISTS (
    SELECT 1 FROM pg_trigger tr
     WHERE tr.tgrelid = 'core.event'::regclass
       AND tr.tgname = 'trg_append_only_event'
       AND NOT tr.tgisinternal
  ) THEN
    EXECUTE 'CREATE TRIGGER trg_append_only_event BEFORE DELETE OR UPDATE ON core.event FOR EACH ROW EXECUTE FUNCTION core.assert_append_only()';
  END IF;
  -- … and TRUNCATE-PROOF: the row trigger above skips TRUNCATE (a statement-level wipe), so a STATEMENT-level
  -- BEFORE TRUNCATE guard closes the one path that could erase the whole ledger with zero the append-only guarantee.
  -- The sanctioned erases (retention prune / account erasure) are row-DELETEs via the gate, not TRUNCATE.
  IF NOT EXISTS (
    SELECT 1 FROM pg_trigger tr
     WHERE tr.tgrelid = 'core.event'::regclass
       AND tr.tgname = 'trg_no_truncate_event'
       AND NOT tr.tgisinternal
  ) THEN
    EXECUTE 'CREATE TRIGGER trg_no_truncate_event BEFORE TRUNCATE ON core.event FOR EACH STATEMENT EXECUTE FUNCTION core.assert_no_truncate()';
  END IF;
END $$;

-- Phase 1b — forgery-gate the IDENTITY tables core.account + core.agent too. They already carry FORCE RLS;
-- this adds the un-forgeable-write trigger (parity with claim/code_node/event/intent/…) so every INSERT/UPDATE
-- must arm the governed-write token via a gate fn. The six writers — provision_seat / enter_installation /
-- set_account_plan / act_for_claim / record_landing / close_my_account — arm it (this PR). (credential stays
-- ungated: the migrator bootstraps it out-of-band, like the roles. core.grant has only gated DELETEs today —
-- no INSERT/UPDATE path — so its trigger is a forward-looking follow-up, not added here.)
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_trigger
     WHERE tgrelid = 'core.account'::regclass
       AND tgname = 'trg_governed_account'
       AND NOT tgisinternal
  ) THEN
    CREATE TRIGGER trg_governed_account BEFORE INSERT OR UPDATE ON core.account FOR EACH ROW EXECUTE FUNCTION core.assert_governed_write();
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM pg_trigger
     WHERE tgrelid = 'core.agent'::regclass
       AND tgname = 'trg_governed_agent'
       AND NOT tgisinternal
  ) THEN
    CREATE TRIGGER trg_governed_agent BEFORE INSERT OR UPDATE ON core.agent FOR EACH ROW EXECUTE FUNCTION core.assert_governed_write();
  END IF;
END $$;

-- ============================================================================================
-- arch-exp/Single-App Consolidated — deploy-coord substrate stub (NOT enabled).
-- Records the SHAPE of a deployment signal as a memo for the placement decision:
-- deploy-coord lives in-core-app, extending handle_event with deployment/deployment_status
-- branches. Content-free: repo, sha, environment, conclusion, ts only — no payload bodies,
-- no logs, no diff. Commented out until the feature is built; this is a substrate signal,
-- not a live migration. Generated by veripsa-arch-loop.
-- ----------------------------------------------------------------------------------------------
-- CREATE TABLE IF NOT EXISTS core.deploy_event (
--     account_id  text         NOT NULL,
--     repo        text         NOT NULL,
--     sha         text         NOT NULL,
--     environment text         NOT NULL,
--     conclusion  text         NOT NULL,
--     ts          timestamptz  NOT NULL DEFAULT now(),
--     PRIMARY KEY (account_id, repo, sha, environment, ts)
-- );
-- ============================================================================================
