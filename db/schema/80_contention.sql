-- ============================================================================================
-- DRAFT (SCOUT-WINDOW) state on a claim — additive + back-compatible. A PR opened (or marked back to) DRAFT is
-- a SCOUT: it wants Core's verdict on the in-flight set BEFORE it ever heads to merge (PO 2026-06-25 round-2
-- framing — humans AND AI agents draft precisely to SEE Core's call). The earlier "draft = skip" policy is
-- dropped; drafts now declare claims like any other PR, get a check and a comment, and participate in the
-- contention picture — but the verdict softens a draft↔non-draft same-file overlap to 'warn' (a heads-up,
-- never the hard 'serialize' that pauses a non-draft behind a still-iterating scout). Draft↔draft can still
-- 'serialize' (scout-vs-scout coordination is still actionable). The is_draft flag is PER-CLAIM (not per-change)
-- because a PR can transition draft↔ready mid-flight; the App overwrites it on each declare and the surface
-- treats a change as draft when ANY of its active/waiting claims are flagged draft (the freshest signal wins
-- in practice — every declare flushes every path of the PR via the reconcile path).
-- CONTENT-FREE: a boolean only. RECALL-SAFE: NULL/false on every pre-existing claim → today's hard 'serialize'
-- behaviour exactly. Idempotent ALTER + indexed for the per-change OR over draft membership the surface needs.
SELECT core._ensure_column_online(
  'claim','is_draft','boolean DEFAULT false NOT NULL');
CREATE INDEX CONCURRENTLY IF NOT EXISTS claim_draft_by_change ON core.claim (account_id, repo, branch, change_id) WHERE is_draft;

-- ANALYZED HEAD (content-free): the commit identity whose CURRENT file/range evidence produced this claim.
-- Neighbor refreshes read GitHub independently of the PR event that last reconciled the DB; paths alone cannot
-- distinguish two heads that edit the same file at different ranges. NULL on old/non-PR claims is deliberately
-- unproven: the refresh layer preserves the last authoritative GitHub surface until the PR's own next event stamps
-- every live path. A per-claim value lets the surface reject mixed, partially-restamped changes fail-closed.
SELECT core._ensure_column_online('claim','analyzed_head_sha','text');

-- SUPERSEDED (#851 ROOT MODEL, gen 8): main_impact_surface no longer reads last_real_push_at — a non-release
-- 'BR-%' holder is now ADVISORY REGARDLESS OF AGE (a pushed branch with no open PR is not in the merge queue, so
-- it must never hard-block a real PR). This column + note_branch_push_head_with_authority are KEPT IN PLACE (no
-- DROP/ALTER) so the change stays FUNCTION-ONLY — a table DDL would make the deploy contention-prone. The prose
-- below documents the retired #917/#923 age-decay signal these were built for.
-- REAL-PUSH SIGNAL (content-free; the #851/#917 stale-branch-reservation-decay fix). The wall-clock of the last
-- GENUINE push to a 'BR-<branch>' lane reservation — a push that actually MOVED the branch head (a new commit),
-- NOT an idempotent re-declaration / boot reconcile that re-stamps heartbeat_at without a new commit. The decay
-- in main_impact_surface keys off THIS instead of heartbeat_at, so a phantom hold cannot be kept "fresh" forever
-- by unrelated re-declarations that bump heartbeat_at (the exact prod defect: long-lived fixture branches with a
-- FROZEN git head showed heartbeat_at ~ now because a re-push re-declared their still-present BR claim → the decay
-- never fired → they hard-blocked every upstream PR). heartbeat_at stays the crash-recovery LEASE pulse
-- (expire_stale_claims keys on the lease) — deliberately UNTOUCHED here so recovery is not regressed.
-- ADDITIVE + IDEMPOTENT: nullable ADD COLUMN (metadata-only, no table rewrite / long lock), backfilled from
-- claimed_at on existing rows, DEFAULT now() for future rows; the decay predicate COALESCEs to claimed_at so a
-- theoretical NULL never means "never decays". RECALL-SAFE: a genuinely fresh branch (new head within the knob)
-- keeps a recent last_real_push_at and stays a hard 'serialize' exactly as today.
SELECT core._ensure_column_online(
  'claim','last_real_push_at','timestamptz');
UPDATE core.claim SET last_real_push_at = claimed_at WHERE last_real_push_at IS NULL;
SELECT core._ensure_column_default_online(
  'claim','last_real_push_at','now()');

CREATE OR REPLACE FUNCTION core.set_change_head_sha_with_authority(
    p_change text, p_repo text, p_branch text, p_head_sha text)
    RETURNS integer LANGUAGE plpgsql SECURITY DEFINER SET search_path TO 'core','pg_catalog' AS $$
DECLARE v_agent text; v_account text; v_change text; v_repo text; v_branch text; v_head text; v_updated integer;
BEGIN
  SELECT agent, account INTO v_agent, v_account
    FROM core.establish_session_write_context() AS c(agent,account);
  v_change := left(NULLIF(btrim(COALESCE(p_change,'')),''),200);
  v_repo := left(COALESCE(p_repo,''),512);
  v_branch := left(COALESCE(NULLIF(p_branch,''),'main'),512);
  v_head := core._clean_hash(p_head_sha);
  IF v_change IS NULL OR v_change NOT LIKE 'PR-%' THEN
    RAISE EXCEPTION 'set_change_head_sha needs a PR change_id' USING ERRCODE='23514';
  END IF;
  PERFORM core.mark_governed_write('claim');
  UPDATE core.claim SET analyzed_head_sha = v_head
   WHERE account_id=v_account AND change_id=v_change AND repo=v_repo AND branch=v_branch
     AND claim_state IN ('active','waiting') AND analyzed_head_sha IS DISTINCT FROM v_head;
  GET DIAGNOSTICS v_updated = ROW_COUNT;
  RETURN v_updated;
END $$;
ALTER FUNCTION core.set_change_head_sha_with_authority(text,text,text,text) OWNER TO veripsa_migrator;
REVOKE EXECUTE ON FUNCTION core.set_change_head_sha_with_authority(text,text,text,text) FROM PUBLIC, veripsa_writer;
GRANT EXECUTE ON FUNCTION core.set_change_head_sha_with_authority(text,text,text,text) TO veripsa_app;

-- note_branch_push_head_with_authority: record a 'BR-<branch>' reservation's real branch head and advance its
-- last_real_push_at ONLY when the head actually CHANGED (a genuine new commit), never on an idempotent
-- re-declaration of the SAME head. The App calls this once per feature-branch push (reserve_branch_lanes),
-- passing the push's new head sha; p_branch is the PROTECTED (lane-namespace) branch the BR claims live on, the
-- SAME coordinate reserve_branch_lanes reserves under. analyzed_head_sha stores the prior head (it was NULL/unused
-- for BR claims until now; the neighbor-refresh consumer already skips BR-* — _compat_analysis: "BR-* branch
-- reservations have no PR head" — so this reuse is inert there). The FIRST observation of a head (NULL -> X)
-- records the head but does NOT advance last_real_push_at (the row's own claimed_at/default already dates the
-- reservation — a re-observed OLD branch must never be spuriously freshened at deploy time); a proven head
-- TRANSITION (X -> Y) advances it to now(). The idempotent same-head re-push is a no-op (the WHERE excludes it).
-- Content-free: a commit fingerprint + a timestamp only, never file bytes. RLS-safe (account pinned by the App
-- session before this runs); buyers cannot call this delegation surface.
CREATE OR REPLACE FUNCTION core.note_branch_push_head_with_authority(
    p_change text, p_repo text, p_branch text, p_head_sha text)
    RETURNS integer LANGUAGE plpgsql SECURITY DEFINER SET search_path TO 'core','pg_catalog' AS $$
DECLARE v_agent text; v_account text; v_change text; v_repo text; v_branch text; v_head text; v_updated integer;
BEGIN
  SELECT agent, account INTO v_agent, v_account
    FROM core.establish_session_write_context() AS c(agent,account);
  v_change := left(NULLIF(btrim(COALESCE(p_change,'')),''),200);
  v_repo := left(COALESCE(p_repo,''),512);
  v_branch := left(COALESCE(NULLIF(p_branch,''),'main'),512);
  v_head := core._clean_hash(p_head_sha);
  IF v_change IS NULL OR v_change NOT LIKE 'BR-%' THEN
    RAISE EXCEPTION 'note_branch_push_head needs a BR branch-reservation change_id' USING ERRCODE='23514';
  END IF;
  IF v_head IS NULL THEN RETURN 0; END IF;   -- a delete/malformed head (no valid sha) → nothing to record
  PERFORM core.mark_governed_write('claim');
  -- One idempotent UPDATE: always store the current head; advance last_real_push_at ONLY on a proven head
  -- TRANSITION (prior head non-NULL AND different). The WHERE makes an identical-head re-push a pure no-op.
  UPDATE core.claim
     SET last_real_push_at = CASE
           WHEN analyzed_head_sha IS NOT NULL AND analyzed_head_sha IS DISTINCT FROM v_head
           THEN now() ELSE last_real_push_at END,
         analyzed_head_sha = v_head
   WHERE account_id=v_account AND change_id=v_change AND repo=v_repo AND branch=v_branch
     AND claim_state IN ('active','waiting') AND analyzed_head_sha IS DISTINCT FROM v_head;
  GET DIAGNOSTICS v_updated = ROW_COUNT;
  RETURN v_updated;
END $$;
ALTER FUNCTION core.note_branch_push_head_with_authority(text,text,text,text) OWNER TO veripsa_migrator;
REVOKE EXECUTE ON FUNCTION core.note_branch_push_head_with_authority(text,text,text,text) FROM PUBLIC, veripsa_writer;
GRANT EXECUTE ON FUNCTION core.note_branch_push_head_with_authority(text,text,text,text) TO veripsa_app;

-- _set_claim_is_draft: stamp the per-claim draft state from the App's webhook payload (prj.draft). Updates
-- only the caller's active/waiting claims for THIS (agent, change, repo, branch, path). RLS-safe (account
-- pinned by the calling wrapper before this runs). Idempotent: re-setting the same value is a no-op write.
CREATE OR REPLACE FUNCTION core._set_claim_is_draft(p_account text, p_agent text, p_change text, p_repo text,
                                                    p_branch text, p_path text, p_is_draft boolean)
    RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path TO 'core','pg_catalog' AS $$
BEGIN
  IF p_is_draft IS NULL THEN RETURN; END IF;   -- NULL = "the caller didn't say" → leave the column alone
  PERFORM core.mark_governed_write('claim');
  UPDATE core.claim SET is_draft = p_is_draft
   WHERE account_id = p_account AND agent_id = p_agent AND change_id = p_change
     AND repo = p_repo AND branch = p_branch AND target_path = p_path
     AND claim_state IN ('active','waiting')
     AND is_draft IS DISTINCT FROM p_is_draft;
END $$;
ALTER FUNCTION core._set_claim_is_draft(text,text,text,text,text,text,boolean) OWNER TO veripsa_migrator;
REVOKE ALL ON FUNCTION core._set_claim_is_draft(text,text,text,text,text,text,boolean) FROM PUBLIC;

-- act_for_claim_with_authority: trailing p_is_draft (DEFAULT NULL, back-compatible). NULL = "caller doesn't carry
-- the signal" → leave the column unchanged (today's behaviour). The hosted App passes prj.draft on every declare;
-- when the PR transitions from draft to ready (or vice versa) GitHub fires `ready_for_review` / `converted_to_draft`
-- and the App re-declares, which flips the flag on every reserved path of the change.
-- Idempotent overload-replace: the 8-arg signature already exists (act_for added p_author_is_bot); drop it first
-- so adding the 9th param does not leave the 8-arg overload behind (same pattern the file uses for every prior add).
DROP FUNCTION IF EXISTS core.act_for_claim_with_authority(text,text,text,text,text,jsonb,text,boolean);
CREATE OR REPLACE FUNCTION core.act_for_claim_with_authority(p_claim_id text, p_target_path text, p_repo text, p_branch text, p_author text, p_ranges jsonb DEFAULT NULL, p_base_hash text DEFAULT NULL, p_author_is_bot boolean DEFAULT false, p_is_draft boolean DEFAULT NULL)
    RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path TO 'core','pg_catalog' AS $$
DECLARE v_app_agent text; v_account text; v_agent text; v_login text; v_res jsonb; v_change text; v_kind text;
BEGIN
  SELECT agent, account INTO v_app_agent, v_account FROM core.establish_session_write_context() AS c(agent,account);
  v_login := left(regexp_replace(COALESCE(p_author,''), '[^A-Za-z0-9_.\-]', '', 'g'), 64);
  IF v_login = '' THEN RAISE EXCEPTION 'act_for needs a non-empty author login' USING ERRCODE='23514'; END IF;
  v_agent := 'GH-'||v_login;
  v_kind := CASE WHEN COALESCE(p_author_is_bot, false) THEN 'ai' ELSE 'human' END;
  PERFORM core.mark_governed_write('agent');
  INSERT INTO core.agent(agent_id, account_id, display_name, agent_kind) VALUES (v_agent, v_account, v_login, v_kind)
  ON CONFLICT (agent_id) DO NOTHING;
  UPDATE core.agent SET agent_kind = v_kind
   WHERE agent_id = v_agent AND account_id = v_account AND agent_id LIKE 'GH-%' AND agent_kind <> v_kind;
  v_res := core._place_claim(p_claim_id, p_target_path, p_repo, p_branch, v_account, v_agent);
  v_change := v_res->>'change_id';
  PERFORM core._set_claim_ranges(v_account, v_agent, v_change, left(COALESCE(p_repo,''),512), left(COALESCE(p_branch,''),512), p_target_path, core._ranges_from_jsonb(p_ranges));
  PERFORM core._set_claim_base_hash(v_account, v_agent, v_change, left(COALESCE(p_repo,''),512), left(COALESCE(p_branch,''),512), p_target_path, p_base_hash);
  -- DRAFT (scout) state — only when the caller passed it (NULL = leave the existing flag alone).
  PERFORM core._set_claim_is_draft(v_account, v_agent, v_change, left(COALESCE(p_repo,''),512), left(COALESCE(p_branch,''),512), p_target_path, p_is_draft);
  RETURN v_res;
END $$;
ALTER FUNCTION core.act_for_claim_with_authority(text,text,text,text,text,jsonb,text,boolean,boolean) OWNER TO veripsa_migrator;
REVOKE EXECUTE ON FUNCTION core.act_for_claim_with_authority(text,text,text,text,text,jsonb,text,boolean,boolean) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION core.act_for_claim_with_authority(text,text,text,text,text,jsonb,text,boolean,boolean) TO veripsa_app;

-- declare_claim_with_authority: trailing p_is_draft (DEFAULT NULL) for symmetry with act_for. The buyer's OWN
-- writer path (act_for=False trial; tests) uses this; NULL means "leave the existing flag alone" (today's behaviour).
DROP FUNCTION IF EXISTS core.declare_claim_with_authority(text,text,text,text,jsonb,text);
CREATE OR REPLACE FUNCTION core.declare_claim_with_authority(p_claim_id text, p_target_path text, p_repo text DEFAULT '', p_branch text DEFAULT '', p_ranges jsonb DEFAULT NULL, p_base_hash text DEFAULT NULL, p_is_draft boolean DEFAULT NULL)
    RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path TO 'core','pg_catalog' AS $$
DECLARE v_agent text; v_account text; v_res jsonb; v_change text;
BEGIN
  SELECT agent, account INTO v_agent, v_account FROM core.establish_session_write_context() AS c(agent,account);
  v_res := core._place_claim(p_claim_id, p_target_path, p_repo, p_branch, v_account, v_agent);
  v_change := v_res->>'change_id';
  PERFORM core._set_claim_ranges(v_account, v_agent, v_change, left(COALESCE(p_repo,''),512), left(COALESCE(p_branch,''),512), p_target_path, core._ranges_from_jsonb(p_ranges));
  PERFORM core._set_claim_base_hash(v_account, v_agent, v_change, left(COALESCE(p_repo,''),512), left(COALESCE(p_branch,''),512), p_target_path, p_base_hash);
  PERFORM core._set_claim_is_draft(v_account, v_agent, v_change, left(COALESCE(p_repo,''),512), left(COALESCE(p_branch,''),512), p_target_path, p_is_draft);
  RETURN v_res;
END $$;
ALTER FUNCTION core.declare_claim_with_authority(text,text,text,text,jsonb,text,boolean) OWNER TO veripsa_migrator;
REVOKE EXECUTE ON FUNCTION core.declare_claim_with_authority(text,text,text,text,jsonb,text,boolean) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION core.declare_claim_with_authority(text,text,text,text,jsonb,text,boolean) TO veripsa_writer;

-- ============================================================================================
-- _file_pair_symbol_overlap — THE FINER (SYMBOL-LEVEL) COLLISION POINT. "中身を持たずに、衝突点を細かくする":
-- without holding file contents, make the collision unit FILE → SYMBOL, content-free. Today a DIRECT
-- collision fires when two in-flight changes touch the SAME FILE (the claim lock on target_path). Too blunt:
-- two PRs editing DIFFERENT functions of render.py are forced to wait in line even though git auto-merges
-- them. This refines that, using ONLY line metadata: each claim carries its CHANGED LINE RANGES (touched_
-- ranges — parsed from the PR diff's HUNK HEADERS, body discarded), and each symbol carries its [start_line,
-- end_line] span. A change TOUCHES symbol S when one of its ranges overlaps S's span.
--
-- For every same-(repo,branch,path) pair of DISTINCT in-flight changes (one ACTIVE, one WAITING — the gate's
-- file-level direct collision), this returns ONE row classifying the pair:
--   'same'      they touch the SAME symbol → a REAL collision; serialize, and NAME the symbol.
--   'disjoint'  BOTH sides are CONFIDENTLY mapped to DISJOINT symbols → the file-level collision is a FALSE
--               wait; DROP it (the win). `sym_a`/`sym_b` are sample touched symbols, for the "...both edit X /
--               you edit Y" message. 'disjoint' requires the file to HAVE spans AND BOTH changes to be
--               fully-mapped (every changed range lands inside some symbol) AND their symbol sets to not
--               intersect. Anything weaker is NOT 'disjoint'.
--   'append'    a PROVABLE PURE-ADDITIVE overlap → KEEP the wait but DEMOTE it to a non-blocking heads-up (the
--               HIGH false-PAUSE fix). At least one side appends a brand-new top-level symbol STRICTLY PAST the
--               file's last known symbol end (past EOF-at-base = no graph span yet), and the OTHER side is ALSO a
--               past-max append OR a fully-mapped DISJOINT interior edit, AND the two sides' changed line ranges
--               do NOT overlap EACH OTHER. Then the two changed-line sets are disjoint and share no symbol, so two
--               independent appends can never be a LOGIC collision — only a trivial git-append-order conflict.
--               UNLIKE 'disjoint' it does NOT drop (a real textual conflict could still need a rebase); the caller
--               softens it to 'serialize_soft' (neutral), never a pause.
--               RECALL-SAFE: an append that OVERLAPS/spills into an interior edit is not past-max on that side; a
--               partly-mapped interior edit is not fully-mapped; and two appends to the SAME tail insertion point
--               (their ranges overlap each other) are a real conflict → NONE qualifies → it falls to 'unknown' (HARD).
--   'unknown'   we could not confidently map one/both sides (no ranges, a range outside every symbol =
--               top-level/module code, a brand-new symbol with no graph span, no spans on the file at all) →
--               FALL BACK to the FILE-level collision (over-flag, NEVER miss). This is the recall-preserving
--               safety net: going finer must never turn a real collision into a silent miss.
-- Only 'disjoint' DROPS the file-level collision; 'same'/'unknown'/'append' all KEEP it (but 'append' is then
-- SOFTENED by the caller to a non-blocking heads-up, while 'same'/'unknown' stay a hard wait). Content-free (paths,
-- symbol names, line numbers only — never code). Account passed explicitly + filtered (internal-only).
-- `line_lo`/`line_hi` give a tiny representative changed line range for the "...both edit lines L1–L2" message.
-- ============================================================================================
-- DROP first: this CREATE OR REPLACE adds a column (`freshness_ok`) to the RETURNS TABLE, which Postgres treats
-- as changing the result type — CREATE OR REPLACE alone errors ("cannot change return type"). Same pattern as
-- _dampened_adjacency below.
DROP FUNCTION IF EXISTS core._file_pair_symbol_overlap(text,text,text);
CREATE OR REPLACE FUNCTION core._file_pair_symbol_overlap(p_account text, p_repo text, p_branch text)
    RETURNS TABLE(path text, a_change text, b_change text, relation text, sym_a text, sym_b text, line_lo int, line_hi int,
                  freshness_ok boolean)
    LANGUAGE sql STABLE SECURITY DEFINER SET search_path TO 'core','pg_catalog' AS $$
  WITH
  -- the gate's FILE-level direct collision: a WAITING change behind an ACTIVE holder on the SAME path. This is
  -- the exact set _place_claim serialized (recall safety net) — we refine ONLY these pairs, never invent new ones.
  filepairs AS (
    SELECT w.target_path AS path, w.change_id AS a_change, h.change_id AS b_change,
           w.touched_ranges AS a_ranges, h.touched_ranges AS b_ranges,
           w.base_content_hash AS a_base_hash, h.base_content_hash AS b_base_hash
      FROM core.claim w JOIN core.claim h
        ON h.account_id=w.account_id AND h.repo=w.repo AND h.branch=w.branch
       AND h.target_path=w.target_path AND h.claim_state='active'
     WHERE w.account_id=p_account AND w.repo=p_repo AND w.branch=p_branch
       AND w.claim_state='waiting' AND w.change_id<>h.change_id),
  -- FRESHNESS KEY (the staleness silent-miss fix): the content hash main's graph stored for each FILE node, so
  -- we can PROVE a claim's diff line numbers were mapped against the SAME version of the file the symbol spans
  -- came from. The claim's touched_ranges are relative to the PR's BASE; these spans are relative to the
  -- INGESTED commit. If the file changed between them, a line silently lands on the WRONG symbol → a real
  -- collision wrongly drops to 'warn' = a SILENT MISS. The demotion below trusts a 'disjoint' mapping ONLY when
  -- BOTH sides' base hash == this file hash (all three non-NULL, equal). Content-free: a hash, never the bytes.
  file_hash AS (
    SELECT path, max(content_hash) AS content_hash   -- one FILE node per path; max() collapses to its hash
      FROM core.code_node
     WHERE account_id=p_account AND repo=p_repo AND branch=p_branch
       AND node_kind='file' AND content_hash IS NOT NULL
     GROUP BY path),
  -- the file's SYMBOL spans (def/class with a non-null [start_line,end_line]). A file with NO spanned symbol
  -- (an old graph, an unsupported language, a node-only file) yields no rows here → every pair on it is 'unknown'.
  syms AS (
    SELECT path, name AS sym, start_line AS s, end_line AS e
      FROM core.code_node
     WHERE account_id=p_account AND repo=p_repo AND branch=p_branch
       AND node_kind IN ('def','class') AND name IS NOT NULL AND start_line IS NOT NULL AND end_line IS NOT NULL),
  has_spans AS (SELECT DISTINCT path FROM syms),
  -- one row per (pair, side) carrying that side's ranges, so we can map each side to symbols independently.
  sides AS (
    SELECT path, a_change, b_change, 'a'::text AS side, a_ranges AS ranges FROM filepairs
    UNION ALL
    SELECT path, a_change, b_change, 'b'::text AS side, b_ranges AS ranges FROM filepairs),
  -- explode each side's ranges to individual changed spans (NULL/empty ranges → no rows = NOT mapped).
  side_ranges AS (
    SELECT path, a_change, b_change, side, rng
      FROM sides, unnest(COALESCE(ranges, ARRAY[]::int4range[])) AS rng
     WHERE ranges IS NOT NULL),
  -- INNERMOST-CONTAINING symbol per changed range (the finer-of-two fixes). For each changed range we pick the
  -- SMALLEST symbol span that FULLY CONTAINS it (<@, not &&). Two reasons this must be CONTAINMENT not overlap:
  --   (1) RECALL (off-by-one safety): a range that merely OVERLAPS a symbol but SPILLS past its end (e.g. an
  --       edit on a def's last line AND the top-level line just after) is NOT confidently inside that symbol —
  --       it also touched module/top-level code. Counting it "mapped" would let the pair be DROPPED as disjoint
  --       even though one side edited top-level code = a SILENT MISS. Containment refuses that: the spilling
  --       range maps to NO symbol → the side is un-mapped → the pair stays file-level (over-flag, never miss).
  --   (2) NESTING (two methods of one class are NOT the same symbol): with overlap, an edit to method m and an
  --       edit to method n BOTH also overlap the enclosing CLASS span, so they spuriously "share" the class and
  --       serialize — defeating the whole feature for the common class-with-methods file. Innermost containment
  --       attributes m's edit to m and n's edit to n (the class is a STRICT superset of each, never the tightest),
  --       so m vs n are disjoint (the real win) while m vs m still collide.
  -- A range contained in NO symbol (top-level/module code, or a spilling range) yields no row here = NOT mapped.
  side_range_sym AS (
    SELECT DISTINCT ON (sr.path, sr.a_change, sr.b_change, sr.side, sr.rng)
           sr.path, sr.a_change, sr.b_change, sr.side, sr.rng,
           y.sym, y.s AS sym_s, y.e AS sym_e
      FROM side_ranges sr
      JOIN syms y ON y.path=sr.path AND sr.rng <@ int4range(y.s, y.e, '[]')   -- range FULLY inside the symbol span
     ORDER BY sr.path, sr.a_change, sr.b_change, sr.side, sr.rng, (y.e - y.s) ASC, y.sym   -- tightest (smallest) wins
  ),
  -- a side is FULLY-MAPPED iff it HAS ranges AND EVERY changed range was contained in some symbol (got an
  -- innermost symbol above). A single un-contained range (top-level / spilling) makes the side un-mappable →
  -- the pair is 'unknown' (file-level fallback, recall preserved). count(distinct rng matched) = count(ranges).
  side_state AS (
    SELECT s.path, s.a_change, s.b_change, s.side,
           (count(srs.rng) = (SELECT count(*) FROM side_ranges sr2
                                WHERE sr2.path=s.path AND sr2.a_change=s.a_change
                                  AND sr2.b_change=s.b_change AND sr2.side=s.side)) AS all_ranges_in_symbols,
           (SELECT count(*) FROM side_ranges sr2
              WHERE sr2.path=s.path AND sr2.a_change=s.a_change AND sr2.b_change=s.b_change AND sr2.side=s.side) AS n_ranges
      FROM (SELECT DISTINCT path, a_change, b_change, side FROM side_ranges) s
      LEFT JOIN side_range_sym srs USING (path, a_change, b_change, side)
     GROUP BY s.path, s.a_change, s.b_change, s.side),
  -- the INNERMOST symbols each side TOUCHES (one per contained range), carrying each symbol's span so we can
  -- test NESTING between the two sides' chosen symbols. Used for the 'same' test and the message symbol names.
  side_syms AS (
    SELECT DISTINCT srs.path, srs.a_change, srs.b_change, srs.side, srs.sym, srs.sym_s, srs.sym_e
      FROM side_range_sym srs),
  -- PAST-MAX (the APPEND distinguisher, the false-PAUSE fix): the file's HIGHEST known symbol end-line. A change
  -- that only APPENDS a brand-new top-level function lands on lines STRICTLY BEYOND this (past EOF-at-base) — it
  -- has no graph span (it did not exist when main was ingested), so it maps to NO symbol = un-mapped = 'unknown'
  -- today, KEEPING the file-level collision and escalating two independent appends to a HARD serialize/pause. But
  -- "all my changed lines are past the last symbol" is content-free, PROVABLE evidence the edit is PURELY ADDITIVE
  -- (a new tail def), DISTINGUISHABLE from an interior top-level edit (which would land at/inside the known span
  -- range). Two such appends cannot textually collide (disjoint line tails) → demote the wait to a heads-up, never
  -- a pause. Computed only for files that HAVE spans (no spans → no max → can't prove append → stays 'unknown').
  file_max AS (SELECT path, max(e) AS max_e FROM syms GROUP BY path),
  -- per (pair, side): is this side a PROVABLE PURE APPEND — it HAS ranges AND its LOWEST touched line is strictly
  -- past the file's max symbol end-line (so EVERY range is past max, since the minimum start already is)? lower(rng)
  -- is the inclusive start (ranges are stored half-open [s,e+1)); max_e is the inclusive end of the last symbol.
  -- min(lower(rng)) > max_e ⟺ all of this side's edits sit beyond every known symbol = append past EOF-at-base.
  -- A side with even ONE range at/inside/spilling-into the symbol body has min(lower) ≤ max_e → NOT append → it
  -- stays un-mapped/'unknown' and HARD (the recall guard: an append that overlaps an interior edit is never demoted).
  side_append AS (
    SELECT sr.path, sr.a_change, sr.b_change, sr.side,
           (min(lower(sr.rng)) > fm.max_e) AS past_max
      FROM side_ranges sr JOIN file_max fm ON fm.path=sr.path
     GROUP BY sr.path, sr.a_change, sr.b_change, sr.side, fm.max_e),
  -- RANGES OVERLAP (the OVERLAPPING-APPEND recall fix): does ANY of side A's changed ranges overlap ANY of side
  -- B's? past_max is computed PER SIDE (each side's lowest line is past the file's last symbol) — it proves each
  -- side is a tail append, but it does NOT prove the two appends are disjoint FROM EACH OTHER. Two PRs that BOTH
  -- insert at the SAME tail line range (e.g. A on [11,14), B on [12,15) — both past max, but overlapping) are a
  -- REAL textual collision at one insertion point, NOT the disjoint-tails multi-agent pattern the append demotion
  -- is for. So we test the two sides' raw changed ranges for overlap (`&&`) directly off the claim touched_ranges
  -- (content-free line numbers, already in `filepairs`). When they overlap, the 'append' arm below is BLOCKED →
  -- the pair stays 'unknown' = HARD. SAME range data the finer mapping uses; no new content. (NULL/empty ranges →
  -- no exploded rows → no overlap row → ranges_overlap is COALESCE'd to false in `pair`, and such a side is also
  -- not past_max anyway, so the arm is unreachable — overlap only ever GATES, it never enables a demotion.)
  pair_ranges_overlap AS (
    SELECT sa.path, sa.a_change, sa.b_change, bool_or(sa.rng && sb.rng) AS ranges_overlap
      FROM side_ranges sa JOIN side_ranges sb
        ON sb.path=sa.path AND sb.a_change=sa.a_change AND sb.b_change=sa.b_change
       AND sa.side='a' AND sb.side='b'
     GROUP BY sa.path, sa.a_change, sa.b_change),
  -- per-pair rollup: is each side fully-mapped? do they share a symbol? a sample symbol per side + a sample line.
  pair AS (
    SELECT fp.path, fp.a_change, fp.b_change,
           (fp.path IN (SELECT path FROM has_spans)) AS file_has_spans,
           -- FRESHNESS-PROVABLE iff BOTH sides' claim base hash == this file's graph hash (all three present +
           -- equal). Only then are the diff line numbers provably aligned to these symbol spans, so a 'disjoint'
           -- mapping is trustworthy. If the graph has no hash for this file (old ingest), or either claim carries
           -- no base hash (old/un-plumbed claim), or any hash differs (the file changed between the graph's commit
           -- and the PR's base = the stale-spans bug) → NOT provable → we KEEP the file-level collision below.
           (EXISTS (SELECT 1 FROM file_hash fh
                      WHERE fh.path=fp.path
                        AND fp.a_base_hash IS NOT NULL AND fp.b_base_hash IS NOT NULL
                        AND fh.content_hash = fp.a_base_hash AND fh.content_hash = fp.b_base_hash)) AS freshness_ok,
           COALESCE((SELECT ss.all_ranges_in_symbols AND ss.n_ranges>0 FROM side_state ss
                       WHERE ss.path=fp.path AND ss.a_change=fp.a_change AND ss.b_change=fp.b_change AND ss.side='a'), false) AS a_mapped,
           COALESCE((SELECT ss.all_ranges_in_symbols AND ss.n_ranges>0 FROM side_state ss
                       WHERE ss.path=fp.path AND ss.a_change=fp.a_change AND ss.b_change=fp.b_change AND ss.side='b'), false) AS b_mapped,
           -- PROVABLE PURE APPEND per side (the false-PAUSE fix): all of this side's edits land strictly past the
           -- file's last known symbol = a new tail def, not an interior edit. Gates the 'append' relation below.
           COALESCE((SELECT sap.past_max FROM side_append sap
                       WHERE sap.path=fp.path AND sap.a_change=fp.a_change AND sap.b_change=fp.b_change AND sap.side='a'), false) AS a_append,
           COALESCE((SELECT sap.past_max FROM side_append sap
                       WHERE sap.path=fp.path AND sap.a_change=fp.a_change AND sap.b_change=fp.b_change AND sap.side='b'), false) AS b_append,
           -- DO THE TWO SIDES' CHANGED RANGES OVERLAP EACH OTHER? (the overlapping-append recall guard): blocks the
           -- 'append' demotion when both appends sit at the SAME tail insertion point (a real conflict, not disjoint
           -- tails). false when no pair of ranges overlaps OR a side has no ranges. Gates the 'append' arm below.
           COALESCE((SELECT pro.ranges_overlap FROM pair_ranges_overlap pro
                       WHERE pro.path=fp.path AND pro.a_change=fp.a_change AND pro.b_change=fp.b_change), false) AS ranges_overlap,
           -- SHARES A SYMBOL = the two sides touch the SAME symbol OR one side's symbol NESTS the other's (one
           -- span CONTAINS the other — e.g. a CLASS-BODY edit on one side and a METHOD of that class on the
           -- other). Nesting → keep the collision (the recall guard); only two symbols where NEITHER contains
           -- the other (true siblings — m vs n, alpha vs beta) are disjoint. IDENTITY IS THE SPAN, NOT THE NAME:
           -- "the same symbol" iff [start,end] spans match — comparing by NAME would falsely collide two DIFFERENT
           -- functions that happen to share a name (very common in Python: every decorator's inner `wrapper`).
           EXISTS (SELECT 1 FROM side_syms sa JOIN side_syms sb
                     ON sa.path=sb.path AND sa.a_change=sb.a_change AND sa.b_change=sb.b_change
                    AND sa.side='a' AND sb.side='b'
                    AND ((sa.sym_s=sb.sym_s AND sa.sym_e=sb.sym_e)
                         OR int4range(sa.sym_s,sa.sym_e,'[]') <@ int4range(sb.sym_s,sb.sym_e,'[]')
                         OR int4range(sb.sym_s,sb.sym_e,'[]') <@ int4range(sa.sym_s,sa.sym_e,'[]'))
                   WHERE sa.path=fp.path AND sa.a_change=fp.a_change AND sa.b_change=fp.b_change) AS shares_symbol,
           (SELECT min(sym) FROM side_syms ss WHERE ss.path=fp.path AND ss.a_change=fp.a_change AND ss.b_change=fp.b_change AND ss.side='a') AS sym_a,
           (SELECT min(sym) FROM side_syms ss WHERE ss.path=fp.path AND ss.a_change=fp.a_change AND ss.b_change=fp.b_change AND ss.side='b') AS sym_b,
           (SELECT min(lower(rng)) FROM side_ranges sr WHERE sr.path=fp.path AND sr.a_change=fp.a_change AND sr.b_change=fp.b_change AND sr.side='a') AS line_lo,
           (SELECT max(upper(rng)-1) FROM side_ranges sr WHERE sr.path=fp.path AND sr.a_change=fp.a_change AND sr.b_change=fp.b_change AND sr.side='a') AS line_hi
      FROM filepairs fp)
  SELECT path, a_change, b_change,
         CASE
           WHEN shares_symbol THEN 'same'
           -- DISJOINT (drop the file-level collision) requires: the file HAS spans AND BOTH sides fully-mapped
           -- AND they do NOT share a symbol AND the spans are PROVABLY VALID for both sides' base (freshness_ok).
           -- That last clause is the staleness silent-miss fix: without it, a graph ingested from a DIFFERENT
           -- commit than the PR's base (for this file) maps the diff lines onto the WRONG symbols and a real
           -- same-symbol collision is wrongly dropped as 'disjoint'. NO freshness proof → NOT 'disjoint' → the
           -- file-level collision is KEPT (over-flag, recall-safe). Everything else stays file-level too.
           WHEN file_has_spans AND a_mapped AND b_mapped AND NOT shares_symbol AND freshness_ok THEN 'disjoint'
           -- APPEND (the HIGH false-PAUSE fix — DEMOTE, do NOT drop). The collision SURVIVES (still surfaced as a
           -- heads-up) but is provably a PURE-ADDITIVE tail-vs-tail (or tail-vs-disjoint-interior) overlap that can
           -- never be a logic collision: at least one side appends a brand-new function STRICTLY PAST the file's
           -- last known symbol, and the OTHER side is ALSO a past-max append OR a fully-mapped disjoint interior
           -- edit. Either way the two changed-line sets are disjoint and neither touches the other's symbol, so a
           -- HARD serialize/pause here is over-firing (the wallpaper the pause tier must avoid). RECALL-SAFE — this
           -- requires file_has_spans AND freshness_ok (provable line↔span alignment) AND NOT shares_symbol; an
           -- append whose range overlaps/spills into an interior edit is NOT past_max on that side and an interior
           -- edit that is not fully mapped is NOT a_mapped/b_mapped → neither qualifies → it stays 'unknown' = HARD.
           -- Unlike 'disjoint' this does NOT drop the wait; the caller (waits/verdict ladder) keeps it VISIBLE and
           -- softens it to a non-material 'serialize_soft' (neutral, never action_required) — same treatment as a
           -- low-value runner-list append-order collision.
           -- NOT ranges_overlap (the OVERLAPPING-APPEND recall guard): past_max proves each side is a tail append
           -- INDEPENDENTLY, but the demotion's premise is "two appends to DISJOINT tails cannot textually collide".
           -- Two PRs that BOTH insert at the SAME tail line range (each past max, but overlapping each other) DO
           -- collide there — a real same-insertion-point conflict, not the disjoint multi-agent pattern. So when the
           -- two sides' changed ranges overlap, this is NOT 'append' → it falls to 'unknown' = HARD (recall-safe).
           WHEN file_has_spans AND freshness_ok AND NOT shares_symbol AND NOT ranges_overlap
                AND ((a_append AND b_append) OR (a_append AND b_mapped) OR (b_append AND a_mapped)) THEN 'append'
           ELSE 'unknown'
         END AS relation,
         -- freshness_ok rides ALONGSIDE the relation so the caller can gate the symbol NAMING on the SAME proof
         -- that gates 'disjoint' (FIX3): a 'same' verdict NAMES sym_a, but the spans the name comes from are only
         -- provably aligned to the diff lines when freshness_ok. Under a STALE graph the collision is correctly
         -- KEPT (recall held — 'same'/'unknown' both serialize), but sym_a may be the WRONG symbol name → the
         -- caller falls back to file/line phrasing when this is false. The relation itself is unchanged.
         sym_a, sym_b, line_lo, line_hi, freshness_ok
    FROM pair
$$;
ALTER FUNCTION core._file_pair_symbol_overlap(text,text,text) OWNER TO veripsa_migrator;
-- INTERNAL ONLY (same tenant-leak class as _claim_adjacency): takes p_account + SECURITY DEFINER, so PUBLIC
-- execute would let a tenant pass a VICTIM account and read its claim ranges / symbol spans cross-tenant.
REVOKE ALL ON FUNCTION core._file_pair_symbol_overlap(text,text,text) FROM PUBLIC;

-- ============================================================================================
-- _is_low_value_collision_path — PRECISION (anti-wallpaper): is a same-line collision on THIS file a
-- TRIVIAL git-ordering conflict rather than meaningful LOGIC coupling? A synthetic adjacent-edit fixture on
-- `run_gates.sh` gets a hard collision. But run_gates.sh is a build/test-RUNNER
-- registration list — every audit PR APPENDS its own test-invocation block, so concurrent PRs textually
-- "collide" on adjacent lines there. That is a 5-second git-conflict anyone resolves, NOT a logic collision
-- like two PRs editing the same function of server.py. Treating both with the SAME hard-serialize severity
-- is OVER-SERIALIZATION → the wallpaper Veripsa explicitly avoids (品質=正確な沈黙, not 鳴りっぱなし).
--
-- THE RULE (why a small documented allowlist, not "no graph spans"): the honest content-free signal is
-- "this file is an append-mostly RUNNER / registration list whose lines have no code-graph identity"
-- — it has NO importable symbols and NO callers/callees in the graph, so any overlap is line-ORDER, not
-- logic. But "no symbol spans" ALONE is far too broad: every unsupported-language SOURCE file (.scala, a
-- bare .kt, a .lua) also has no spans, and softening a real same-function collision there would be a
-- precision REGRESSION in the wrong direction (a silent under-warn). So we DO NOT key off "no spans".
-- Instead we name the narrow, well-understood class explicitly: gate/test RUNNER shell scripts and the
-- few registration-list scripts of this repo's own shape. Deliberately CONSERVATIVE — when unsure, return
-- FALSE (keep the hard serialize); a missed real collision is the failure we sell against, a softened
-- trivial one is merely one fewer false alarm. Owner-extensible via the `low_value_collision_globs` policy
-- (comma/whitespace-separated basename globs) so a customer whose repo has its own runner-list files can
-- name them without a code change. Content-free: operates on the PATH only, never file bytes.
--
-- IMMUTABLE except for the policy read (so it stays a cheap per-path predicate); the policy lookup is folded
-- in as a STABLE arm. Pure-string, no account leak (it classifies a path shape, reads only the CALLER's own
-- account policy via _policy_text). NOT a substitute for `disjoint` (which DROPS a false wait): this only
-- SOFTENS a SURVIVING wait — the collision is still real and still surfaced, just marked low-stakes.
-- ============================================================================================
CREATE OR REPLACE FUNCTION core._is_low_value_collision_path(p_path text) RETURNS boolean
    LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path TO 'core','pg_catalog' AS $$
DECLARE
  v_base text := lower(regexp_replace(COALESCE(p_path,''), '^.*/', ''));   -- basename, case-folded; content-free
  v_glob text;
BEGIN
  IF v_base = '' THEN RETURN false; END IF;
  -- BUILT-IN class: gate/test/CI RUNNER and registration-list scripts. These are sequences of test/command
  -- INVOCATIONS (or a flat append-only list); two PRs each appending their own line "collide" only on order.
  --   run_gates.sh / *_gates.sh / *-gates.sh — the release-gate runner: every audit PR appends a gate block
  --   smoke.sh / smoke_*.sh / *_smoke.sh     — smoke-test runners (db/smoke.sh, …): each feature appends its smoke line
  --   dogfood.sh / dogfood*.sh               — the dogfood driver: appends seed/demo steps
  --   run_tests.sh / runtests.sh / test.sh / tests.sh / ci.sh — generic test/CI runners
  --
  -- WORD-BOUNDARY ANCHORED (PRECISION, the under-warn fix): the old bare `%gates%.sh` / `%smoke%.sh` substring
  -- globs OVER-MATCHED real SOURCE filenames that merely CONTAIN those letters — `aggregates.sh`, `propagates.sh`,
  -- `delegates.sh` (all end in `gates.sh`), `smokestack.sh` (contains `smoke`+`.sh`) — silently SOFTENING a real
  -- same-function collision on a genuine script = an under-warn. We now anchor: a `gates`/`smoke` token must sit
  -- at a word boundary (start, or after `_`/`-`), not be a tail of a longer word. So `*_gates.sh`/`*-gates.sh`
  -- and `run_gates.sh` match but `aggregates.sh` does NOT; `smoke.sh`/`smoke_*.sh`/`*_smoke.sh` match but
  -- `smokestack.sh` does NOT. Conservative when unsure (a missed real collision is the failure we sell against).
  IF v_base IN ('run_gates.sh','dogfood.sh','run_tests.sh','runtests.sh','test.sh','tests.sh','ci.sh')
     OR v_base ~ '^(.*[_-])?gates\.sh$'      -- run_gates.sh / a_gates.sh / a-gates.sh — NOT aggregates.sh / delegates.sh
     OR v_base ~ '^smoke(_.*)?\.sh$'         -- smoke.sh / smoke_db.sh                  — NOT smokestack.sh
     OR v_base ~ '^.*_smoke\.sh$'            -- db_smoke.sh / e2e_smoke.sh              — NOT smokescreen.sh
     OR v_base LIKE 'dogfood%.sh'
  THEN
    RETURN true;
  END IF;
  -- OWNER EXTENSION: comma/whitespace-separated basename GLOBS (filesystem-style: `*` = any run, `?` = one
  -- char — e.g. `pipeline.list`, `suite_runner.sh`, `*.list`). A customer whose repo has its own runner/
  -- registration list names them here so the softening applies without shipping code. Empty/unset → no extra
  -- match (built-ins still apply). We translate the GLOB to a SQL LIKE pattern rather than feeding it raw:
  -- the docstring + examples promise filesystem-glob syntax, but `v_base LIKE raw_glob` would read `*`/`?`
  -- as LITERAL characters (SQL LIKE wildcards are `%`/`_`), so the documented `*.py` silently matched NOTHING
  -- while an undocumented `%.py` matched — a contract mismatch. Translation: first ESCAPE the owner's literal
  -- LIKE metacharacters (`\`,`%`,`_`) so they can't smuggle in a wildcard, THEN map glob `*`→`%` and `?`→`_`.
  -- (Content-free: still classifies a path SHAPE, never file bytes; bounded by _policy_text's 4096-char cap.)
  FOR v_glob IN
    SELECT lower(trim(g)) FROM unnest(regexp_split_to_array(
             COALESCE(core._policy_text('low_value_collision_globs',''),''), '[,\s]+')) AS g
     WHERE trim(g) <> ''
  LOOP
    IF v_base LIKE translate(
         replace(replace(replace(v_glob, '\', '\\'), '%', '\%'), '_', '\_'),  -- escape literal LIKE metachars first
         '*?', '%_')                                                            -- then glob → LIKE wildcards
    THEN RETURN true; END IF;
  END LOOP;
  RETURN false;
END $$;
ALTER FUNCTION core._is_low_value_collision_path(text) OWNER TO veripsa_migrator;
-- INTERNAL ONLY (matches the contention family): SECURITY DEFINER + reads the caller-account policy, so it
-- is not a PUBLIC surface. Reachable only through main_impact_surface (which sets the account first).
REVOKE ALL ON FUNCTION core._is_low_value_collision_path(text) FROM PUBLIC;

-- _is_generated_or_vendored_path — PRECISION for the proactive SPLIT ADVICE only (audit 2026-06-18). A VENDORED
-- (third-party code copied in) or GENERATED (regenerated from a source-of-truth) file is a poor split target: you
-- do not hand-restructure node_modules or a *_pb2.py — you re-vendor or regenerate it. So even when such a file is
-- widely imported (vendored libs often are) or huge (generated protobufs often are) AND churns (re-genned),
-- advising "split it into cohesive modules" is cry-wolf. This predicate EXCLUDES that path SHAPE from the split-
-- advice arms (FOUNDATION fan-in AND GOD-FILE size), in BOTH the per-PR shared_foundation and the split_candidates
-- chart, so they agree. It NEVER touches a VERDICT (serialize/warn/clear) — a real collision ON a vendored file
-- still serializes; only the proactive structural ADVICE is suppressed. TEST files are intentionally NOT excluded
-- (a god test file IS a legitimate split target — we split test_server.py itself). Content-free (path SHAPE only);
-- owner-extensible via the `split_exclude_globs` policy (same glob→LIKE translation as low_value_collision_globs).
CREATE OR REPLACE FUNCTION core._is_generated_or_vendored_path(p_path text) RETURNS boolean
    LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path TO 'core','pg_catalog' AS $$
DECLARE
  v_path text := lower(COALESCE(p_path,''));                              -- full path, case-folded; content-free
  v_base text := regexp_replace(v_path, '^.*/', '');                      -- basename for suffix/glob tests
  v_glob text;
BEGIN
  IF v_path = '' THEN RETURN false; END IF;
  -- VENDORED / build-output dirs — a '/'-bounded path SEGMENT that is a known third-party drop or generated output.
  -- Segment-anchored so a SOURCE file merely CONTAINING the word (e.g. 'vendoring_test.py', 'rebuild.py') is NOT
  -- excluded. ALIGNED to the extractor's walk-skip set (audit r2): dropped 'vendored'/'pods'/'site-packages'/'venv'/
  -- 'virtualenv' — the extractor ADMITS files in those (it only walk-skips '.venv'/'Pods'/dist/build/generated), so
  -- excluding them HERE silently suppressed split advice on real first-party source (e.g. a 'pods/' microservice or
  -- 'src/venv/' dir). Also NOT 'packages'/'bin'/'gen'/'migrations' (commonly first-party). Verdicts are unaffected.
  IF v_path ~ '(^|/)(vendor|third[_-]?party|node_modules|bower_components|\.venv|dist|build|generated|__generated__|\.next|\.nuxt)/'
  THEN RETURN true; END IF;
  -- GENERATED file suffixes — regenerated from a source of truth (protobuf/grpc/dart/minified/designer/typedecls).
  IF v_base ~ '(\.pb\.(go|cc|h|py|rb|java)|_pb2(_grpc)?\.py|\.pb\.dart|\.g\.dart|\.generated\.[a-z]+|_generated\.[a-z]+|\.min\.(js|css)|\.bundle\.js|\.d\.ts|\.designer\.cs|_pb\.js)$'
  THEN RETURN true; END IF;
  -- OWNER EXTENSION: comma/whitespace-separated basename GLOBS (same escape-then-glob→LIKE translation as the
  -- low_value path predicate). A customer names extra generated/vendored basenames here; empty/unset → no extra match.
  FOR v_glob IN
    SELECT lower(trim(g)) FROM unnest(regexp_split_to_array(
             COALESCE(core._policy_text('split_exclude_globs',''),''), '[,\s]+')) AS g
     WHERE trim(g) <> ''
  LOOP
    IF v_base LIKE translate(
         replace(replace(replace(v_glob, '\', '\\'), '%', '\%'), '_', '\_'),
         '*?', '%_')
    THEN RETURN true; END IF;
  END LOOP;
  RETURN false;
END $$;
ALTER FUNCTION core._is_generated_or_vendored_path(text) OWNER TO veripsa_migrator;
REVOKE ALL ON FUNCTION core._is_generated_or_vendored_path(text) FROM PUBLIC;

-- ============================================================================================
-- _branch_matches_release — is a BRANCH NAME a POLICY-PINNED release lane (EXEMPT from reservation decay)?
-- Used ONLY by the STALE BRANCH-RESERVATION DECAY (#851) in main_impact_surface. A push to a non-main branch
-- reserves a lane under change_id 'BR-<branch>' for each changed path (webhook_handlers.reserve_branch_lanes);
-- by invariant a 'BR-%' claim means NO open PR yet (a PR re-declares those paths as 'PR-<n>'). A long-lived
-- scenario/demo branch (e.g. a permanent collision-lab branch kept for /try, never deletable) that never opens
-- a PR and is not pushed for a long time otherwise keeps its 'BR-' lane 'active' forever, so every upstream PR
-- touching those paths is queued behind it — a phantom hold. The decay downgrades that BLOCKING 'serialize' to a
-- non-blocking heads-up once the reservation is idle past a knob. BUT a release/hotfix/develop line is a
-- DELIBERATELY long-lived integration branch whose reservation SHOULD keep holding the lane even when idle — so
-- a branch matching this policy is EXEMPT: the collision stays a hard 'serialize'. Owner-tunable via the
-- `release_branch_patterns` policy (comma/whitespace-separated filesystem-style globs; default
-- 'release/*,hotfix/*,develop,main'). SAME escape-then-glob→LIKE translation as _is_low_value_collision_path, so
-- the documented '*'/'?' behave as filesystem wildcards, not raw SQL-LIKE metacharacters. Case-folded + trimmed.
-- CONSERVATIVE: an empty/blank policy falls back to the default set (via _policy_text); no glob matches → false
-- (the reservation decays as normal). Content-free: classifies a BRANCH NAME only, never file bytes.
CREATE OR REPLACE FUNCTION core._branch_matches_release(p_branch text) RETURNS boolean
    LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path TO 'core','pg_catalog' AS $$
DECLARE
  v_branch text := lower(btrim(COALESCE(p_branch,'')));   -- the recovered branch name, case-folded; content-free
  v_glob text;
BEGIN
  IF v_branch = '' THEN RETURN false; END IF;
  FOR v_glob IN
    SELECT lower(btrim(g)) FROM unnest(regexp_split_to_array(
             core._policy_text('release_branch_patterns', 'release/*,hotfix/*,develop,main'), '[,\s]+')) AS g
     WHERE btrim(g) <> ''
  LOOP
    IF v_branch LIKE translate(
         replace(replace(replace(v_glob, '\', '\\'), '%', '\%'), '_', '\_'),  -- escape literal LIKE metachars first
         '*?', '%_')                                                            -- then glob → LIKE wildcards
    THEN RETURN true; END IF;
  END LOOP;
  RETURN false;
END $$;
ALTER FUNCTION core._branch_matches_release(text) OWNER TO veripsa_migrator;
-- INTERNAL ONLY (matches the contention family): SECURITY DEFINER + reads the caller-account policy via
-- _policy_text, so strip the PUBLIC default; reachable only through main_impact_surface (which pins the account).
REVOKE ALL ON FUNCTION core._branch_matches_release(text) FROM PUBLIC;

-- ============================================================================================
-- _file_pair_conflict_likely — MECHANICAL merge-conflict anticipation. A DIFFERENT AXIS from everything
-- above. The functions above answer "how IMPORTANT is this coupling (logic risk)?" — they decide whether to
-- hard-serialize (real source collision) or soften (append-mostly runner file = trivial). This answers a
-- SEPARATE question: "will these two in-flight PRs produce a GIT MERGE CONFLICT on rebase (mechanical
-- certainty)?" The two are independent — a LOW-value file can be LOW-severity (don't serialize) yet
-- HIGH-conflict-certainty (you WILL hit a trivial merge conflict).
--
-- WHY THIS EXISTS (the case that bit us, LIVE): three PRs each APPENDED a gate-registration block near the
-- SAME lines of run_gates.sh → a guaranteed git merge conflict on rebase. But run_gates.sh is "low value",
-- so _is_low_value_collision_path SOFTENED it to a "heads up" and the conflict was a SURPRISE — softening
-- demoted the severity AND silently dropped the forewarning. Lesson: softening the SEVERITY must not also
-- drop the mechanical conflict signal. So this is a parallel, additive signal: it NEVER changes a verdict;
-- it rides ALONGSIDE so a softened collision still carries an honest "expect a small conflict here" note.
--
-- THE HEURISTIC (content-free — line numbers ONLY, never file bytes): for each same-(repo,branch,path) pair
-- of in-flight changes (the EXACT waiter↔holder pairs the gate already serialized — we refine these, never
-- invent new ones), test their changed-line ranges (touched_ranges, half-open [s,e+1) from diff HUNK HEADERS):
--   conflict-likely  ⟺  some range of side A and some range of side B OVERLAP (a && b — both edit a shared
--                       line) OR sit within a small GAP (≤ conflict_adjacency_lines, default 2 — "the same
--                       insertion point"). The gap captures the APPEND case: two PRs appending at the same
--                       place produce ranges that overlap OR are separated only by a context line or two; git
--                       conflicts on both. Ranges with a REAL gap (distinct regions of one file) are NOT
--                       flagged → no false conflicts (two PRs on far-apart functions auto-merge).
-- The gap is GREATEST(lower)−LEAST(upper): ≤0 when the ranges overlap, = the line distance when disjoint;
-- ≤ tolerance ⇒ likely. PURE INSERTIONS at the same point (no existing line is "shared") are caught because
-- both insertion ranges start at the same hunk line ⇒ they overlap or are within the gap (the run_gates.sh
-- regression). Returns ONE row per pair: merge_conflict_likely + a representative conflict_line (the lowest
-- shared/adjacent line — the "near line N" the message names).
--
-- HONEST LIMIT (the point of staying content-free): this is a HEURISTIC, not a 3-way merge. A true conflict
-- check needs the file BODIES (the base + both sides' bytes), which Veripsa must NOT read. So we say "likely"
-- / "expect", NEVER "will definitely". A change with NO ranges (or NULL touched_ranges) yields NO conflict
-- claim here (we cannot anticipate without line data) — the file-level COLLISION still stands on its own; we
-- simply add no conflict note. Under-anticipating a conflict is acceptable (git will still surface it on
-- rebase); FALSELY shouting "conflict" on disjoint ranges is the failure this avoids. Content-free, tenant-
-- pinned (p_account passed + filtered, same internal-only contract as _file_pair_symbol_overlap).
-- ============================================================================================
CREATE OR REPLACE FUNCTION core._file_pair_conflict_likely(p_account text, p_repo text, p_branch text)
    RETURNS TABLE(path text, a_change text, b_change text, merge_conflict_likely boolean, conflict_line int)
    LANGUAGE sql STABLE SECURITY DEFINER SET search_path TO 'core','pg_catalog' AS $$
  WITH
  -- the gate's FILE-level direct collision: a WAITING change behind an ACTIVE holder on the SAME path. The
  -- EXACT set _place_claim serialized — we annotate these pairs with a conflict signal, never invent new ones.
  filepairs AS (
    SELECT w.target_path AS path, w.change_id AS a_change, h.change_id AS b_change,
           w.touched_ranges AS a_ranges, h.touched_ranges AS b_ranges
      FROM core.claim w JOIN core.claim h
        ON h.account_id=w.account_id AND h.repo=w.repo AND h.branch=w.branch
       AND h.target_path=w.target_path AND h.claim_state='active'
     WHERE w.account_id=p_account AND w.repo=p_repo AND w.branch=p_branch
       AND w.claim_state='waiting' AND w.change_id<>h.change_id),
  -- owner-tunable, clamped gap tolerance (lines). 0 = overlap-or-strictly-adjacent only; default 2 absorbs the
  -- context-line slack between two appends at the same insertion point. Read once via the standard _policy_int.
  tol AS (SELECT core._policy_int('conflict_adjacency_lines', 2, 0, 50) AS n),
  -- every (a_range × b_range) cross-pair that OVERLAPS or sits within the gap tolerance = a likely conflict
  -- locus. CROSS JOIN the small tol scalar. conflict_line = the lowest shared/adjacent line for the message.
  hits AS (
    SELECT fp.path, fp.a_change, fp.b_change,
           GREATEST(lower(ar), lower(br)) AS line   -- the overlap/adjacency start (content-free line number)
      FROM filepairs fp
      CROSS JOIN tol t
      CROSS JOIN unnest(COALESCE(fp.a_ranges, ARRAY[]::int4range[])) AS ar
      CROSS JOIN unnest(COALESCE(fp.b_ranges, ARRAY[]::int4range[])) AS br
     WHERE (ar && br)                                              -- a shared line (overlap)
        OR (GREATEST(lower(ar), lower(br)) - LEAST(upper(ar), upper(br)) <= t.n))   -- OR within the gap (same insertion point)
  SELECT fp.path, fp.a_change, fp.b_change,
         EXISTS (SELECT 1 FROM hits h WHERE h.path=fp.path AND h.a_change=fp.a_change AND h.b_change=fp.b_change) AS merge_conflict_likely,
         (SELECT min(h.line) FROM hits h WHERE h.path=fp.path AND h.a_change=fp.a_change AND h.b_change=fp.b_change) AS conflict_line
    FROM filepairs fp
$$;
ALTER FUNCTION core._file_pair_conflict_likely(text,text,text) OWNER TO veripsa_migrator;
-- INTERNAL ONLY (same tenant-leak class as _file_pair_symbol_overlap / _claim_adjacency): takes p_account +
-- SECURITY DEFINER, so a PUBLIC execute would let a tenant pass a VICTIM account and read its claim ranges
-- cross-tenant. Reachable only through main_impact_surface (which pins the caller's account first).
REVOKE ALL ON FUNCTION core._file_pair_conflict_likely(text,text,text) FROM PUBLIC;

-- PHASE 2 (surfaces, cont.) — STRUCTURAL CONTENTION: the collision PREDICTION the exact-path lock can't
-- see. A file F an agent edits is "contested" when a DIFFERENT agent holds a file STRUCTURALLY ADJACENT
-- to F (one calls a symbol the other defines) in the SAME coordinate. Reuses _claim_adjacency (the shared
-- A→B engine). Also the "↘ N likely-touched-next" degree. Content-free; coordinate-scoped.
-- ============================================================================================
CREATE OR REPLACE FUNCTION core.contention_surface(p_repo text DEFAULT '', p_branch text DEFAULT '') RETURNS jsonb
    LANGUAGE plpgsql SECURITY DEFINER SET search_path TO 'core','pg_catalog' AS $$
DECLARE v_account text; v_repo text; v_branch text; v_result jsonb;
BEGIN
  SELECT account INTO v_account FROM core.resolve_session_identity() AS r(agent,account);
  PERFORM set_config('core.current_account', v_account, true);
  PERFORM core.expire_stale_claims();   -- sweep crashed/silent holders BEFORE deciding (matches discover_surface + gate): a dead session must never block a live PR or show as a phantom collision
  v_repo := left(COALESCE(p_repo,''),512); v_branch := left(COALESCE(p_branch,''),512);
  v_result := (
  WITH
  edited AS (
    SELECT DISTINCT target_path AS path, agent_id FROM core.claim
     WHERE account_id=v_account AND repo=v_repo AND branch=v_branch AND claim_state='active'),
  adj AS (SELECT f, nbr FROM core._claim_adjacency(v_account, v_repo, v_branch)),
  contested AS (
    SELECT DISTINCT a.path AS f, core.agent_name(b.agent_id) AS by
    FROM edited a JOIN adj ON adj.f=a.path JOIN edited b ON b.path=adj.nbr AND b.agent_id<>a.agent_id)
  SELECT jsonb_build_object(
    'repo', v_repo, 'branch', v_branch,
    'contested_count', (SELECT count(DISTINCT f)::int FROM contested),
    'files', COALESCE((SELECT jsonb_agg(jsonb_build_object(
        'path', e.path, 'editing', core.agent_name(e.agent_id),
        'contested', EXISTS (SELECT 1 FROM contested c WHERE c.f=e.path),
        'contested_with', COALESCE((SELECT jsonb_agg(DISTINCT c.by) FROM contested c WHERE c.f=e.path), '[]'::jsonb),
        'degree', (SELECT count(DISTINCT nbr)::int FROM adj WHERE adj.f=e.path)
      ) ORDER BY e.path) FROM edited e), '[]'::jsonb)
  ));
  RETURN v_result;
END $$;
ALTER FUNCTION core.contention_surface(text,text) OWNER TO veripsa_migrator;
GRANT EXECUTE ON FUNCTION core.contention_surface(text,text) TO veripsa_reader, veripsa_writer, veripsa_demo_steward;

-- ============================================================================================
-- main_impact_surface — THE GITHUB-APP BRAIN (main protection). The breakthrough (PO 2026-06-17): with
-- main's code graph we need only return (a) the IMPACT RANGE of what others are touching and (b) "landed".
-- In-flight changes headed to main are modeled as claims at the {repo, branch='main'} coordinate (the App
-- maps each open PR's changed paths → claims by the PR's author against main). Over MAIN's graph, for each
-- in-flight change (per agent = the PR proxy) this returns: the paths it reserves, its A→B blast radius
-- (impact = structurally-downstream files, via the shared _claim_adjacency), which other in-flight change
-- it collides with (contested_with / serialize_behind), and a VERDICT:
--   serialize = a DIRECT same-path collision (queued behind a holder; land in order — 順番待ち)
--   warn      = a SEMANTIC A→B exposure (a neighbor is held by another in-flight change; steer, don't block)
--   clear     = neither.
-- Default is warn+show-impact, NEVER a blunt block (止めるだけ=コンフリクトと同じ). Content-free; main-pinned.
-- ============================================================================================
CREATE OR REPLACE FUNCTION core.main_impact_surface(p_repo text DEFAULT '', p_branch text DEFAULT 'main') RETURNS jsonb
    LANGUAGE plpgsql SECURITY DEFINER SET search_path TO 'core','pg_catalog' AS $$
DECLARE v_account text; v_repo text; v_branch text; v_fanin int; v_churn int; v_win int; v_symbols int; v_landorder_hops int;
        v_clusrounds int; v_changed bigint; v_changed2 bigint; v_result jsonb;
BEGIN
  SELECT account INTO v_account FROM core.resolve_session_identity() AS r(agent,account);
  PERFORM set_config('core.current_account', v_account, true);
  PERFORM core.expire_stale_claims();   -- sweep crashed/silent holders BEFORE deciding (matches discover_surface + gate): a dead session must never block a live PR or show as a phantom collision
  -- shared-foundation knobs — the SAME gate as split_candidates, so "a file too many things depend on" means
  -- ONE thing everywhere (the chart and the inline PR note never disagree). Owner-tunable + clamped via _policy_int.
  v_fanin := core._policy_int('split_min_fanin', 5, 2, 1000);
  v_churn := core._policy_int('split_min_churn', 3, 1, 1000);
  v_win   := core._policy_int('split_window_days', 30, 1, 365);
  v_symbols := core._policy_int('split_min_symbols', 25, 5, 100000);   -- GOD-FILE arm: a file DEFINING this many symbols (def/class/table) AND churning is a split candidate even at LOW fan-in (server.py-class: huge body, not widely imported)
  -- LAND-ORDER recursion BOUND (degenerate-graph safety, audit 2026-06-18). The suggested-land-order rank below
  -- walks the in-flight dependency DAG; on a DEGENERATE shape — a deep linear import chain (a layered monorepo:
  -- n0←n1←…←n_k, every level an open PR) — the old FULL transitive closure materialized O(N²) (root,node) pairs
  -- (measured: a 2000-deep chain = 2,001,000 rows, ~36s, statement-timeout on the WEBHOOK path = a self-inflicted
  -- DoS on a real input). The rank is only a DESC tiebreak for an ADVISORY order, so it does not need the exact
  -- transitive count past a bound: we cap the dependent-DAG walk at this many hops (clamped, owner-tunable). A
  -- bounded walk stays a SOUND partial order (deeper-rooted changes still rank ahead; equal-or-beyond-cap nodes
  -- fall through to the existing blast-count→id tiebreak — never a contradiction). Default 64, clamp 1..256.
  v_landorder_hops := core._policy_int('land_order_max_hops', 64, 1, 256);
  -- CLUSTER pointer-jumping round cap (degenerate-graph safety, audit 2026-06-18): a pure NON-TERMINATION backstop
  -- for the HOOK+JUMP connected-components loop below (log₂ of any realistic in-flight count is < 30, so it should
  -- never bind); if it ever did, the result is a valid COARSENING (sub-components not yet merged), never a wrong
  -- split. Same _policy_int idiom (owner-tunable, clamped). Default 64, clamp 1..1000.
  v_clusrounds := core._policy_int('cluster_max_rounds', 64, 1, 1000);
  -- (#851 ROOT MODEL, gen 8: the branch_reservation_stale_days knob + its v_stale_days read were RETIRED here —
  -- a non-release 'BR-%' holder is now advisory REGARDLESS OF AGE, so there is no idle threshold to read. The
  -- advisory rule is purely structural now; see the waits_all CTE below. Supersedes the #917/#923 age-decay.)
  -- the PROTECTED branch (usually main; master/trunk/etc. for repos that name it differently).
  v_branch := left(COALESCE(NULLIF(p_branch,''),'main'),512);
  v_repo := left(COALESCE(p_repo,''),512);
  IF v_repo = '' THEN
    SELECT repo INTO v_repo FROM core.graph_version WHERE account_id=v_account AND branch=v_branch ORDER BY ingested_at DESC LIMIT 1;
    IF v_repo IS NULL THEN
      SELECT repo INTO v_repo FROM core.graph_version WHERE account_id=v_account ORDER BY ingested_at DESC LIMIT 1;
    END IF;
    v_repo := COALESCE(v_repo,'');
  END IF;
  -- COLD/STALE-STATS GUARD (degenerate-graph safety, audit 2026-06-18). A freshly-ONBOARDED tenant (or any repo
  -- that just bulk-loaded its graph) has code_node/code_edge whose planner statistics are MISSING or STALE until
  -- autovacuum catches up — so the heavy joins below AND core._claim_adjacency (the O(graph) blast-radius scan)
  -- mis-plan to O(N²) NESTED LOOPS. MEASURED: core._claim_adjacency on a 1000-node in-flight graph with no/stale
  -- stats ran ~58s (the whole surface ~60s) vs ~1s once ANALYZE'd — a statement-timeout on the webhook path = a
  -- self-inflicted cold-start DoS, exactly the deep-chain shape the degenerate-graph gate feeds. The fix ANALYZEs
  -- the graph tables when their stats are stale, gated on the PG never-analyzed sentinel (reltuples = -1) OR a
  -- significant write churn since the last analyze (n_mod_since_analyze past autovacuum's own 10%+50 rule). So a
  -- WARM, already-analyzed repo pays NOTHING (the common steady-state webhook), while a cold/just-ingested
  -- coordinate gets correct cardinalities BEFORE the heavy joins. NOT gated on reltuples=-1 ALONE: a server that
  -- analyzes once then loads a NEW repo's rows into the same shared tables would otherwise keep the first repo's
  -- stale stats (the degenerate gate's 6 sequential shapes proved this). The surface runs as the table owner
  -- (SECURITY DEFINER) so it may ANALYZE; ANALYZE takes only a ShareUpdateExclusiveLock (never blocks concurrent
  -- reads/writes) and is content-free (statistics, not data).
  --
  -- SAME-EVENT SELF-HEAL GAP (slow-ingest incident 2026-06-21, the RECURRING 600s wedge). The two pg_class/
  -- pg_stat arms above MISS the live App's hottest cold-stats path: a PR event whose self-heal re-ingests main's
  -- graph (ingest.self_heal_main_graph → core.ingest_graph_with_authority = bulk DELETE+INSERT of the WHOLE
  -- coordinate) and then computes THIS surface in the SAME body txn (webhook_handlers._handle_pull_request_event).
  -- After a re-ingest of an ALREADY-analyzed coordinate, neither arm trips:
  --   • reltuples is NOT the -1 sentinel — a DELETE+INSERT reuses the heap, so pg_class keeps the PRIOR analyze's
  --     reltuples (the STALE, possibly tiny/old picture), never resetting to -1. (Measured: re-ingest left
  --     reltuples at the old value; forcing it stale-small (10) made _claim_adjacency mis-plan to nested loops →
  --     ~7s at 60 in-flight PRs vs ~0.03s warm — the seed of the 600s queue wedge under a push burst.)
  --   • n_mod_since_analyze is still 0 — Postgres flushes the cumulative stats collector ASYNCHRONOUSLY (~1s), so
  --     read inside the SAME txn that just did the bulk write it has NOT yet reflected the churn. (This is the very
  --     reason the post-commit App helper uses the synchronous GUC below, not n_mod_since_analyze — server_dbops.)
  -- So the prediction planned on stale cardinalities and wedged the single-threaded worker for the full
  -- statement_timeout. The post-commit ANALYZE (_refresh_graph_stats_if_bulk_loaded) only helps the NEXT event —
  -- never THIS one's in-txn surface. FIX (root, behaviour-preserving): also force the ANALYZE when the SYNCHRONOUS
  -- bulk-loaded session flag is set. core.ingest_graph_with_authority sets core.graph_bulk_loaded='1' (is_local=
  -- false → session-scoped, reverts on rollback) inside the body txn; it is readable IMMEDIATELY on the same
  -- session/txn (a GUC, not the async stats collector), so it is the precise, lag-free "the graph tables were just
  -- rewritten — their stats are stale regardless of pg_class" signal. We CLEAR it right after the ANALYZE so a
  -- second in-statement/in-session call (e.g. the perf gate's jsonb_array_elements over the result) does not
  -- re-ANALYZE; the post-commit helper's separate read is unaffected (it ran on the EVENT connection, not here).
  -- THIRD ARM — the DISK-vs-STATS mismatch (slow-ingest incident 2026-06-21 follow-through). The flag arm below
  -- is the precise signal ONLY when the bulk-load actually WROTE: ingest_graph_with_authority sets
  -- core.graph_bulk_loaded at its TAIL, AFTER the _refuse_if_over_quota wall. So an OVER-QUOTA account whose graph
  -- is already stored (usage > the plan line) takes the EARLY refusal RETURN — the self-heal re-ingest is a no-op,
  -- the flag is NEVER set — yet the App STILL computes this surface against the stored (possibly stale-stats) graph
  -- (ingest.self_heal_main_graph returns healed=False on the quota refusal and PR analysis proceeds against the
  -- stored graph by design). reltuples is not -1 and n_mod_since_analyze is 0/async-lagged, so NONE of the other
  -- arms trip and the surface mis-plans core._claim_adjacency into the O(N^2) nested-loop wedge (MEASURED on this
  -- repo's ~24k-edge graph with forced-stale stats: ~11s vs ~0.15s analyzed). So we ALSO trip on a SYNCHRONOUS,
  -- catalog-only proxy for "the heap is far bigger than the planner's row-count believes": the relation's ACTUAL
  -- on-disk page count (pg_relation_size, an O(1) stat() — no scan) exceeds what pg_class.relpages claims by a wide
  -- margin. A bulk DELETE+INSERT that grew the heap leaves relpages at the prior (small/old) value until ANALYZE/
  -- autovacuum catches up; a stale-SMALL reltuples rides alongside it → the exact mis-plan trigger. A WARM, freshly
  -- analyzed table has relpages == actual pages so this is FALSE (it pays nothing — verified against a genuinely
  -- small AND a genuinely warm table); content-free (catalog sizes only, never row bytes). The 4x+32 margin keeps
  -- a normally-bloated-but-analyzed table (relpages tracks the real size) from tripping — only a real stale gap does.
  IF EXISTS (
    SELECT 1 FROM pg_class c
      JOIN pg_namespace ns ON ns.oid = c.relnamespace AND ns.nspname = 'core'
      LEFT JOIN pg_stat_all_tables s ON s.relid = c.oid
     WHERE c.relname IN ('code_node','code_edge','claim')
       AND (c.reltuples = -1 OR COALESCE(s.n_mod_since_analyze,0) > 50 + 0.1 * GREATEST(c.reltuples,0)
            OR pg_relation_size(c.oid) / current_setting('block_size')::int > c.relpages * 4 + 32)
  ) OR COALESCE(NULLIF(current_setting('core.graph_bulk_loaded', true), ''), '0') = '1' THEN
    ANALYZE core.code_node; ANALYZE core.code_edge; ANALYZE core.claim;
    -- one ANALYZE per bulk-load is enough: clear the synchronous flag so a 2nd in-session surface call (or the
    -- perf gate's double call) does not redundantly re-ANALYZE. is_local=false → matches how the ingest set it.
    PERFORM set_config('core.graph_bulk_loaded', '0', false);
  END IF;
  -- ── SINGLE-SCAN ADJACENCY + BOUNDED CONTENTION COMPONENTS (degenerate-graph safety, audit 2026-06-18) ──────
  -- TWO concerns reconciled here, BEFORE the big WITH below, both via per-call TEMP tables (ON COMMIT DROP):
  --   (1) SINGLE adjacency scan (audit:scale 2026-06-18 — the perf gate's LOCK3 anchors it): core._claim_adjacency
  --       is the O(graph) whole-graph blast-radius scan (~0.9s on a real Django-class graph). The surface used to
  --       pay it TWICE — once for its `adj` CTE, once inside core._inflight_components. We evaluate it EXACTLY ONCE
  --       into `_mis_adj`; both the component build (just below) AND the big WITH's `adj` CTE read that temp table.
  --   (2) EXACT + BOUNDED contention components (this audit): the in-flight changes + their links form a graph whose
  --       CONNECTED COMPONENTS are the cluster neighbourhoods. The prior INLINE recursive min-label CTE (comp_cc)
  --       materialized O(N²) (node,comp) pairs — a 1500-deep contention chain ≈ 1.1M rows / ~19s, statement-timeout
  --       on the WEBHOOK path. Replaced with POINTER-JUMPING (Shiloach–Vishkin) over temp tables: HOOK (pull min comp
  --       over neighbours) + JUMP (pointer-double toward the component root) iterated to fixpoint → EXACT for any
  --       shape (a fully entangled N-ring collapses to ONE component; a deep chain to ONE), bounded in O(log N)
  --       rounds. `comps` (below) reads the result `_mis_node(change_id, comp)`; comp = MIN change id in the
  --       component, so every downstream cluster/verdict/agents/changes/hotspot computation is UNCHANGED. It is the
  --       SAME algorithm as the standalone core._inflight_components — but inlined here off the shared `_mis_adj` so
  --       the single-scan win is preserved (calling the helper would re-scan the adjacency = the LOCK3 regression).
  -- DROP-first: main_impact_surface can be called TWICE in one statement (the perf gate's shared_foundation probe
  -- does jsonb_array_elements(...) over the result), and ON COMMIT DROP only fires at COMMIT — so a 2nd in-statement
  -- call would hit "relation already exists"; drop the scratch up front. CREATE INDEX IF NOT EXISTS = migration-safe.
  DROP TABLE IF EXISTS _mis_adj; DROP TABLE IF EXISTS _mis_node; DROP TABLE IF EXISTS _mis_link; DROP TABLE IF EXISTS _mis_inflight; DROP TABLE IF EXISTS _mis_graphfile; DROP TABLE IF EXISTS _mis_uncertain;
  CREATE TEMP TABLE _mis_adj(f text, f_agent text, f_change_id text, nbr text, dir text) ON COMMIT DROP;
  CREATE TEMP TABLE _mis_node(change_id text PRIMARY KEY, comp text) ON COMMIT DROP;
  CREATE TEMP TABLE _mis_link(a text, b text) ON COMMIT DROP;
  -- the in-flight working set (every active/waiting change on this branch), materialized + ANALYZE'd ONCE. WHY a temp
  -- table and not just the CTE: a freshly-seeded DB (the webhook path right after onboarding ingest, and the
  -- degenerate-graph gate's per-case fresh DB) has NO statistics on core.claim, so the big WITH's PER-CHANGE
  -- correlated subqueries (paths/unknown_paths/the hotspot join) and joins that read `inflight` would mis-plan to
  -- nested loops over an assumed-empty core.claim = O(changes²) catastrophe (measured: a 1000-deep chain on a
  -- no-stats cluster = ~60s vs ~0.9s once ANALYZE'd — a self-inflicted webhook-path DoS the warm dev DB hides).
  -- Materialising the in-flight set into an ANALYZE'd, change_id-indexed temp table gives the planner EXACT
  -- cardinalities for the most-joined relation regardless of core.claim's stats, so the surface stays bounded on a
  -- cold tenant. (core.claim itself is small per branch; this is one cheap scan, then every consumer reads the temp.)
  -- is_draft per claim: needed by the draft↔non-draft softening in the verdict CTE below. A claim's is_draft is
  -- per (claim, path) but the verdict logic uses it per CHANGE — a change is "draft" when ANY of its active/waiting
  -- claims carry is_draft=true (the freshest signal wins; every re-declare from the App flushes every path of the
  -- PR through the reconcile, so a state transition draft↔ready propagates uniformly).
  CREATE TEMP TABLE _mis_inflight(agent_id text, change_id text, path text, claim_state text,
                                  is_draft boolean, analyzed_head_sha text) ON COMMIT DROP;
  INSERT INTO _mis_adj(f, f_agent, f_change_id, nbr, dir)
    SELECT f, f_agent, f_change_id, nbr, dir FROM core._claim_adjacency(v_account, v_repo, v_branch);  -- the ONE whole-graph scan
  -- INDEX on f_change_id: the big WITH below correlates on adj.f_change_id PER in-flight change (impacts, and the
  -- per-change `impact`/`impact_count` subqueries in the final jsonb build). Without this index those correlated
  -- lookups seq-scan the whole _mis_adj once per change = O(changes × edges) — a 1000-deep chain blew that to ~1M
  -- scans (the cold-cache webhook-path cost the prior CTE form's planner-materialization hid). CREATE INDEX IF NOT
  -- EXISTS = migration-safe (a fresh per-call temp table). Restores O(changes × log edges).
  CREATE INDEX IF NOT EXISTS _mis_adj_fcid ON _mis_adj(f_change_id);
  INSERT INTO _mis_inflight(agent_id, change_id, path, claim_state, is_draft, analyzed_head_sha)
    SELECT agent_id, change_id, target_path, claim_state, COALESCE(is_draft, false), analyzed_head_sha FROM core.claim
     WHERE account_id=v_account AND repo=v_repo AND branch=v_branch AND claim_state IN ('active','waiting');
  CREATE INDEX IF NOT EXISTS _mis_inflight_cid ON _mis_inflight(change_id);   -- per-change correlated subqueries (paths/unknown_paths/hotspot) probe by change_id
  -- the FILE paths present in main's graph for this coordinate, materialized + indexed ONCE. The UNKNOWN-FIRST
  -- verdict + unknown_paths field test, PER in-flight change, whether a touched path is a file in main's graph via
  -- a NOT EXISTS over core.code_node. On a freshly-seeded (no-stats) tenant the planner seq-scans the whole
  -- code_node table once PER change = O(changes × nodes): MEASURED a 1000-deep chain at ~60s on a cold cluster
  -- (vs ~1s once code_node is ANALYZE'd) — the SINGLE dominant cold-path cost (binary-searched). Folding the
  -- file-path set into an ANALYZE'd, path-indexed temp table makes each per-change membership test an index probe,
  -- bounded regardless of code_node's stats. One scan here; every per-change check reads the temp set.
  CREATE TEMP TABLE _mis_graphfile(path text) ON COMMIT DROP;
  INSERT INTO _mis_graphfile(path)
    SELECT DISTINCT path FROM core.code_node
     WHERE account_id=v_account AND repo=v_repo AND branch=v_branch AND node_kind='file';
  CREATE INDEX IF NOT EXISTS _mis_graphfile_path ON _mis_graphfile(path);
  -- FIRST-CLASS PER-PATH UNCERTAINTY. Resolved edges continue to drive adjacency; an ambiguous edge is retained
  -- only as provenance and contributes bounded markers for both its source and
  -- any destination that resolves to a persisted document. An unresolved
  -- local reference has no destination document by definition, so it marks
  -- its source only. This wall prevents status-bearing evidence from leaving
  -- either known endpoint falsely Clear. Analysis ambiguity is bounded the same way.
  -- Failed/incomplete analysis remains path-local here; the verdict CTE later
  -- uses only an actual in-flight failed/incomplete path to activate its
  -- query-scoped unbounded-loss wall.
  CREATE TEMP TABLE _mis_uncertain(path text, reason text) ON COMMIT DROP;
  INSERT INTO _mis_uncertain(path, reason)
    SELECT DISTINCT path,
           CASE analysis_status
             WHEN 'failed' THEN 'analysis_failed'
             WHEN 'ambiguous' THEN 'analysis_ambiguous'
             ELSE 'analysis_incomplete'
           END
      FROM core.code_node
     WHERE account_id=v_account AND repo=v_repo AND branch=v_branch
       AND node_kind IN ('file','config_file')
       AND analysis_status IN ('failed','ambiguous','incomplete')
    UNION
    SELECT DISTINCT src, 'reference_ambiguous'
      FROM core.code_edge
     WHERE account_id=v_account AND repo=v_repo AND branch=v_branch
       AND reference_status='ambiguous'
    UNION
    SELECT DISTINCT src, 'reference_unresolved'
      FROM core.code_edge
     WHERE account_id=v_account AND repo=v_repo AND branch=v_branch
       AND reference_status='unresolved'
    UNION
    SELECT DISTINCT n.path, 'reference_ambiguous'
      FROM core.code_edge e
      JOIN core.code_node n
        ON n.account_id=e.account_id AND n.repo=e.repo AND n.branch=e.branch
       AND n.node_kind IN ('file','config_file')
       AND COALESCE(
             n.semantic_key,
             core._node_semantic_key(
               n.node_kind,n.node_id,n.path,n.name,n.canonical_key
             )
           )=COALESCE(e.semantic_dst_key,core._semantic_ref_key(e.dst))
     WHERE e.account_id=v_account AND e.repo=v_repo AND e.branch=v_branch
       AND e.reference_status='ambiguous';
  CREATE INDEX IF NOT EXISTS _mis_uncertain_path ON _mis_uncertain(path);
  -- node set = every in-flight change (seed each pointer to its own id).
  INSERT INTO _mis_node(change_id, comp)
    SELECT DISTINCT change_id, change_id FROM _mis_inflight;
  -- undirected links (read off the already-materialized _mis_adj — NO second adjacency scan): a SEMANTIC A→B
  -- adjacency into another change's active path OR a DIRECT same-lane wait, both directions. Identical link set to
  -- the prior inline comp_sem/comp_direct/comp_links (and to the standalone helper).
  INSERT INTO _mis_link(a, b)
    WITH active AS (SELECT DISTINCT path, change_id FROM _mis_inflight WHERE claim_state='active'),   -- off the analyzed temp set (not a cold core.claim scan)
         sem AS (SELECT DISTINCT a.f_change_id AS a, av.change_id AS b FROM _mis_adj a JOIN active av ON av.path=a.nbr AND av.change_id<>a.f_change_id),
         direct AS (SELECT DISTINCT w.change_id AS a, h.change_id AS b FROM core.claim w JOIN core.claim h
                      ON h.account_id=w.account_id AND h.repo=w.repo AND h.branch=w.branch AND h.target_path=w.target_path AND h.claim_state='active'
                     WHERE w.account_id=v_account AND w.repo=v_repo AND w.branch=v_branch AND w.claim_state='waiting' AND w.change_id<>h.change_id)
    SELECT a,b FROM sem UNION SELECT b,a FROM sem UNION SELECT a,b FROM direct UNION SELECT b,a FROM direct;
  CREATE INDEX IF NOT EXISTS _mis_link_a ON _mis_link(a);
  ANALYZE _mis_adj; ANALYZE _mis_node; ANALYZE _mis_link; ANALYZE _mis_inflight; ANALYZE _mis_graphfile; ANALYZE _mis_uncertain;   -- give the planner EXACT row stats (the big WITH joins _mis_adj + correlates on _mis_inflight/_mis_graphfile per change; default no-stats mis-plans to O(N²) nested loops on a cold tenant)
  -- HOOK + JUMP to fixpoint (pointer-doubling → O(log N) rounds; cluster_max_rounds is a non-termination backstop).
  FOR i IN 1..v_clusrounds LOOP
    WITH cand AS (   -- HOOK: pull the min comp over self + neighbours (one-hop relaxation)
      SELECT me.change_id AS node, LEAST(me.comp, COALESCE(min(nb.comp), me.comp)) AS p
        FROM _mis_node me
        LEFT JOIN _mis_link l ON l.a = me.change_id
        LEFT JOIN _mis_node nb ON nb.change_id = l.b
       GROUP BY me.change_id, me.comp)
    UPDATE _mis_node n SET comp = cand.p FROM cand WHERE cand.node = n.change_id AND cand.p < n.comp;
    GET DIAGNOSTICS v_changed = ROW_COUNT;
    WITH jmp AS (   -- JUMP: pointer-double — comp := comp-of-(comp-of-node) — leap toward the root
      SELECT n.change_id AS node, gp.comp AS gpp
        FROM _mis_node n JOIN _mis_node p ON p.change_id = n.comp JOIN _mis_node gp ON gp.change_id = p.comp)
    UPDATE _mis_node n SET comp = jmp.gpp FROM jmp WHERE jmp.node = n.change_id AND jmp.gpp < n.comp;
    GET DIAGNOSTICS v_changed2 = ROW_COUNT;
    EXIT WHEN v_changed + v_changed2 = 0;   -- fixpoint: no pointer improved → components are final
  END LOOP;
  v_result := (
  WITH RECURSIVE
  inflight AS (   -- read the ANALYZE'd, change_id-indexed temp table materialized above (insulates the per-change correlated subqueries below from cold core.claim stats)
    SELECT agent_id, change_id, path, claim_state, is_draft, analyzed_head_sha FROM _mis_inflight),
  -- UNBOUNDED EXTRACTION-LOSS WALL, scoped to THIS query's in-flight set. A failed/incomplete document can have
  -- omitted arbitrary outgoing facts, so an otherwise-Clear peer cannot be proven safe while that document is
  -- actively changing. Do NOT key this off every uncertain row in the coordinate: analysis/reference ambiguity is
  -- bounded to its recorded endpoint, and a historical failed/incomplete path that is not in-flight must not
  -- permanently poison unrelated work. The verdict ladder applies this only after every Serialize/Warn arm.
  inflight_unbounded_loss AS MATERIALIZED (
    SELECT EXISTS(
      SELECT 1
        FROM inflight i
        JOIN _mis_uncertain u ON u.path=i.path
       WHERE u.reason IN ('analysis_failed','analysis_incomplete')
    ) AS present),
  active AS (SELECT DISTINCT path, agent_id, change_id FROM inflight WHERE claim_state='active'),
  -- DRAFT (scout-window) per CHANGE: a change is "draft" when ANY of its active/waiting claims carry is_draft=true.
  -- Used by the verdict CTE below to soften a draft↔non-draft same-file overlap to 'warn' (never the hard hold-up
  -- 'serialize' that would pause a non-draft behind a still-iterating scout). Draft↔draft still 'serialize' as
  -- needed (scout-vs-scout coordination is still actionable). PO 2026-06-25 round-2 framing.
  change_draft AS (
    SELECT change_id, bool_or(COALESCE(is_draft,false)) AS is_draft FROM inflight GROUP BY change_id),
  -- A head is proven only when EVERY live path was stamped and all stamps agree. Mixed/null means an event is
  -- between reconcile phases (or predates this migration), so neighbor refresh must not combine it with current
  -- GitHub evidence. The PR's own next event converges every path and restores refresh eligibility.
  change_head AS (
    SELECT change_id,
           CASE WHEN count(*) = count(NULLIF(analyzed_head_sha,''))
                     AND count(DISTINCT analyzed_head_sha) = 1
                THEN min(analyzed_head_sha) ELSE NULL END AS head_sha
      FROM inflight GROUP BY change_id),
  labels AS (
    SELECT change_id, min(agent_id) AS agent_id,
           core.agent_name(min(agent_id)) AS agent,
           CASE WHEN change_id LIKE 'PR-%' THEN core.agent_name(min(agent_id))||' '||change_id ELSE core.agent_name(min(agent_id)) END AS label
      FROM inflight GROUP BY change_id),
  adj AS (SELECT f, f_agent, f_change_id, nbr, dir FROM _mis_adj),   -- the ONE whole-graph scan, materialized above (single-scan win preserved; perf gate LOCK3)
  contested AS (
    SELECT DISTINCT adj.f_change_id AS by_change, av.change_id AS with_change
    FROM adj JOIN active av ON av.path=adj.nbr AND av.change_id<>adj.f_change_id),
  -- UPSTREAM collision (the single most important warning): a file THIS change DEPENDS ON (dir='up') is being
  -- changed by ANOTHER in-flight change. Editing your code on top of a foundation that is shifting = the rework
  -- trap. (The reverse — your downstream being changed — is just 'contested_with'.) Carries the dep path + who.
  depends_changing AS (
    SELECT DISTINCT adj.f_change_id AS change_id, adj.nbr AS dep_path, av.change_id AS by_change
    FROM adj JOIN active av ON av.path=adj.nbr AND av.change_id<>adj.f_change_id AND adj.dir='up'),
  -- UNKNOWN-FIRST GUARD over hub dampening (AUDIT3): the REAL import couplings _claim_adjacency dropped
  -- because an endpoint is a hub, but ONLY between two distinct in-flight changes (an actual contention, not
  -- the importer-wall noise). A change with one of these is NOT 'clear' — we silenced a real edge — so it
  -- becomes 'unknown' (honest) carrying who + which hub. Uncorroborated, it stays 'unknown' (never the noise of a
  -- bare-indegree 'warn'). CO-CHANGE CORROBORATION (recall, precision-SAFE): if git co-change history INDEPENDENTLY
  -- confirms the two dampened files really move together — lift >= 2 (≥2× chance, base-rate-corrected) AND co >= 3
  -- (repeated, not a one-off) — then TWO agreeing signals (a real structural import edge we hid for hub-noise + a
  -- corroborated co-change) make it a CONFIDENT real coupling, so it EARNS a 'warn' (the never-warn guard is relaxed
  -- for EXACTLY the corroborated ones; everything else is unchanged). co_change is per-tenant (FORCE RLS),
  -- content-free (paths + counts only), indexed (co_change_a/_b), and this LEFT JOIN is BOUNDED to the tiny dampened
  -- in-flight set — NO new whole-graph scan. When co_change is empty (a repo not yet backfilled) cc.* is NULL →
  -- corroborated=false → today's 'unknown' behavior (graceful). Thresholds are owner-tunable knobs (clamped reads).
  dampened AS (
    SELECT d.f_change_id AS change_id, d.nbr_change_id AS by_change, d.via_hub,
           cc.lift AS cc_lift, cc.strength AS cc_strength,
           (cc.lift IS NOT NULL
             AND cc.lift >= COALESCE(NULLIF(current_setting('veripsa.cochange_corroborate_lift', true), '')::real, 2.0)
             AND cc.co   >= COALESCE(NULLIF(current_setting('veripsa.cochange_corroborate_co',   true), '')::int,  3)
           ) AS corroborated
    FROM core._dampened_adjacency(v_account, v_repo, v_branch) d
    LEFT JOIN core.co_change cc
           ON cc.account_id = v_account AND cc.repo = v_repo
          AND cc.path_a = LEAST(d.f, d.nbr) AND cc.path_b = GREATEST(d.f, d.nbr)),
  -- FINER (SYMBOL-LEVEL) COLLISION POINT: classify each FILE-level direct collision (waiter↔holder on one
  -- path) at SYMBOL granularity from line metadata only (content-free). 'disjoint' = both sides confidently
  -- mapped to NON-overlapping symbols → a FALSE wait we DROP. 'same'/'unknown' = a real symbol collision or
  -- an un-mappable change → KEEP the file-level collision (over-flag, never miss = recall preserved).
  symovl AS (SELECT path, a_change, b_change, relation, sym_a, sym_b, line_lo, line_hi, freshness_ok
               FROM core._file_pair_symbol_overlap(v_account, v_repo, v_branch)),
  -- MECHANICAL merge-conflict anticipation (a DIFFERENT axis from symbol severity): for each file-level direct
  -- collision, does the two sides' changed-line geometry make a GIT merge conflict on rebase likely? Overlapping
  -- OR same-insertion-point ranges (content-free, line numbers only) → likely. Independent of the verdict: it
  -- NEVER changes serialize/soft/warn — it rides ALONGSIDE so a SOFTENED low-value collision still forewarns the
  -- conflict (the run_gates.sh regression: softening the severity used to silently drop the conflict heads-up).
  conflict AS (SELECT path, a_change, b_change, merge_conflict_likely, conflict_line
                 FROM core._file_pair_conflict_likely(v_account, v_repo, v_branch)),
  -- the file-level direct collisions the gate serialized (waiter behind active holder on the same path).
  waits_all AS (
    -- BASE: every file-level direct wait (a 'waiting' claim behind the ONE 'active' holder on its path).
    SELECT w.change_id AS waiter, h.change_id AS holder, w.target_path AS path,
           -- ADVISORY BRANCH RESERVATION (#851 ROOT MODEL — supersedes the #917/#923 age-decay): is the HOLDER a
           -- 'BR-<branch>' lane reservation with NO open PR that is NOT a policy-pinned release lane? "Wait in
           -- line"/Paused means a REAL in-flight PR is ahead of you IN THE MERGE QUEUE. A 'BR-%' claim is, by
           -- invariant, a pushed branch with NO open PR (a PR re-declares those paths as 'PR-<n>') — so it is NOT
           -- in the merge queue and must NEVER hard-block a real PR, REGARDLESS OF AGE. The predicate is therefore
           -- PURELY STRUCTURAL: 'BR-%' AND NOT a release lane. (a) 'BR-%' ⇒ no open PR; (b) release/hotfix/develop/
           -- main are the opt-in exemption (core._branch_matches_release) that MAY still hard-block, so they are
           -- excluded here. The branch name is recovered from the change_id (webhook_coercion._branch_change_id
           -- builds it as 'BR-'||branch, capped 182), so substring past the 3-char 'BR-' prefix is the branch. When
           -- true, the verdict CTE below downgrades the blocking 'serialize' to a non-blocking 'serialize_soft'
           -- (heads-up) and the holder is dropped from serialize_behind (never a hard blocker). Content-free (a
           -- branch name only). RECALL-SAFE: a real 'PR-<n>' holder or a release-lane branch stays a hard
           -- 'serialize' exactly as today. NOTE: last_real_push_at / note_branch_push_head_with_authority (#923)
           -- are now UNUSED by this predicate — kept in place (no DDL) to keep this a FUNCTION-ONLY, contention-free deploy.
           (h.change_id LIKE 'BR-%'
            AND NOT core._branch_matches_release(substring(h.change_id from 4))) AS holder_advisory
    FROM core.claim w JOIN core.claim h
      ON h.account_id=w.account_id AND h.repo=w.repo AND h.branch=w.branch AND h.target_path=w.target_path AND h.claim_state='active'
    WHERE w.account_id=v_account AND w.repo=v_repo AND w.branch=v_branch AND w.claim_state='waiting' AND w.change_id<>h.change_id
    UNION ALL
    -- PR-ORDERING BEHIND AN ADVISORY BR (#851 ROOT MODEL): an advisory 'BR-' holder must NOT stop the real PRs from
    -- ordering AMONG THEMSELVES. When the 'active' lane holder on a path is an advisory (non-release) BR, EVERY real
    -- PR on that path is 'waiting' behind it (so the BASE arm above only ever pairs those PRs with the BR, which is
    -- advisory). The correct merge order is: the EARLIEST such PR (by claimed_at, change_id) is the effective LEADER
    -- and every LATER PR hard-'serializes' behind that EARLIEST PR — NOT behind the BR. We synthesize exactly those
    -- (later-PR → earliest-PR) waits here as HARD (holder_advisory=false); the earliest PR itself gets no row (the
    -- LATERAL's `<>` filter), so it stays leader (its only wait is the advisory BR ⇒ 'serialize_soft'). Content-free
    -- (ids + timestamps). RECALL-SAFE: fires ONLY when the active holder is an advisory BR — a real 'PR-<n>' active
    -- holder already produces the ordinary hard wait via the base arm, so this never double-counts or over-softens.
    SELECT w.change_id AS waiter, ldr.change_id AS holder, w.target_path AS path, false AS holder_advisory
    FROM core.claim w
    JOIN core.claim br
      ON br.account_id=w.account_id AND br.repo=w.repo AND br.branch=w.branch AND br.target_path=w.target_path
     AND br.claim_state='active' AND br.change_id LIKE 'BR-%'
     AND NOT core._branch_matches_release(substring(br.change_id from 4))
    JOIN LATERAL (
      SELECT c.change_id
        FROM core.claim c
       WHERE c.account_id=w.account_id AND c.repo=w.repo AND c.branch=w.branch AND c.target_path=w.target_path
         AND c.claim_state IN ('active','waiting') AND c.change_id LIKE 'PR-%'
       ORDER BY c.claimed_at, c.change_id LIMIT 1) ldr ON ldr.change_id <> w.change_id
    WHERE w.account_id=v_account AND w.repo=v_repo AND w.branch=v_branch
      AND w.claim_state='waiting' AND w.change_id LIKE 'PR-%'),
  -- DROP only the pairs the finer engine proved 'disjoint' (both confidently mapped, non-overlapping symbols).
  -- 'same' and 'unknown' are NOT dropped (real collision / un-mappable → keep waiting). symovl is symmetric in
  -- (a,b) for a given path: match the dropped pair on EITHER orientation so a waiter↔holder pair drops regardless.
  -- `low_value` (PRECISION / anti-wallpaper): this SURVIVING collision is on an append-mostly build/test-RUNNER
  -- or registration-list file (run_gates.sh and kin) where two PRs only collide on APPEND ORDER — a trivial git
  -- conflict, NOT logic coupling. The collision is still KEPT (we never go silent on a real textual conflict),
  -- but it lets the verdict be SOFTENED below to 'serialize_soft' when EVERY surviving wait is low-value.
  --
  -- INTERACTION GUARD (#127 soften × #122 freshness-gated symbol demotion — the silent-miss this closes): the
  -- HONEST premise of _is_low_value_collision_path is "this file is an append-mostly runner/registration list
  -- whose lines have NO code-graph identity → any overlap is line-ORDER, not logic". For the BUILT-IN allowlist
  -- (all .sh runner scripts) that holds — those files have no def/class spans, so the finer engine can only ever
  -- return 'unknown' there, never 'same'. But the OWNER-EXTENSION glob can be pointed (via SQL-LIKE semantics,
  -- e.g. `%.py`) at a file that DOES have symbols. Then a PROVEN same-symbol collision — including one #122
  -- deliberately KEPT hard 'serialize' because the graph was STALE (freshness unverifiable → file-level, never
  -- silently dropped) — would be SOFTENED to a non-blocking heads-up here = re-introducing exactly the silent
  -- under-warn #122 exists to prevent. So a wait is low-value ONLY when its path is low-value AND the finer
  -- engine did NOT prove it a 'same'-symbol collision: a proven same-symbol overlap (stale or fresh) is a real
  -- logic collision and stays HARD regardless of the file's name/glob. (No symovl 'same' row → un-mappable /
  -- spanless → the soften still applies, as intended for the genuine runner-list case.)
  -- `append_soft` (PRECISION / anti-wallpaper, the HIGH false-PAUSE fix): this SURVIVING collision is a PROVABLE
  -- PURE-ADDITIVE overlap (the finer engine returned relation='append' — at least one side appends a brand-new
  -- function strictly past the file's last symbol, the other is also a past-max append OR a disjoint interior edit,
  -- and they share NO symbol). Two independent appends to disjoint line tails of the SAME real source file (the
  -- canonical multi-agent pattern: each PR adds its own new function) are NOT a logic collision — only a trivial
  -- git-append-order conflict. So, exactly like a low-value runner-list overlap, the collision is KEPT (still
  -- surfaced — a rebase may be needed) but SOFTENED below to 'serialize_soft' (neutral, never action_required/pause).
  -- This rides ALONGSIDE low_value (not folded into it) because the two are different premises: low_value keys off
  -- the FILE (an append-mostly runner), append_soft keys off the proven SYMBOL-level GEOMETRY (additive tail edits)
  -- of ANY file. SAME recall guard as low_value, structurally: 'append' is ONLY ever returned when NOT shares_symbol
  -- (a proven 'same'-symbol collision can never be 'append'), so a real logic collision is never softened here.
  waits AS (
    SELECT wa.waiter, wa.holder, wa.path,
           (core._is_low_value_collision_path(wa.path)
            AND NOT EXISTS (SELECT 1 FROM symovl so WHERE so.relation='same' AND so.path=wa.path
                              AND ((so.a_change=wa.waiter AND so.b_change=wa.holder)
                                OR (so.a_change=wa.holder AND so.b_change=wa.waiter)))) AS low_value,
           EXISTS (SELECT 1 FROM symovl so WHERE so.relation='append' AND so.path=wa.path
                     AND ((so.a_change=wa.waiter AND so.b_change=wa.holder)
                       OR (so.a_change=wa.holder AND so.b_change=wa.waiter))) AS append_soft,
           -- ADVISORY BRANCH RESERVATION (#851 ROOT MODEL): the HOLDER is a non-release 'BR-<branch>' reservation
           -- with no open PR (computed in waits_all). Treated EXACTLY like low_value/append_soft — a SURVIVING wait
           -- that is SOFTENED (never dropped): the collision stays visible as a heads-up but never contributes to a
           -- hard 'serialize'. Recall guard is identical: hard still WINS whenever this waiter also has a real
           -- (non-soft, non-cross_draft) wait — INCLUDING the synthetic (later-PR → earliest-PR) hard wait added in
           -- waits_all — the verdict CTE below ORs advisory_res into the soft set, so ONE genuine collision re-hardens the pair.
           wa.holder_advisory AS advisory_res,
           -- DRAFT-CROSS-STATE softening (PO 2026-06-25 SCOUT-WINDOW): a wait pair across draft↔non-draft is a
           -- scout overlap, NOT a queue-in-line situation — a non-draft must never be paused behind a still-
           -- iterating scout, AND a scout must never feel paused behind a non-draft it can simply rebase later.
           -- Demote such pairs out of 'serialize'/'serialize_soft' (they become 'warn' via the verdict CTE
           -- below: the contested edge stays, the queue-in-line copy goes). draft↔draft is still a real
           -- coordination need (two scouts on the same lane), so a wait between two drafts stays HARD. NULL-safe:
           -- a change with no recorded draft state defaults false → today's behaviour for any pre-existing
           -- claim that never received an is_draft from the App.
           (COALESCE((SELECT cd.is_draft FROM change_draft cd WHERE cd.change_id=wa.waiter), false)
              <> COALESCE((SELECT cd.is_draft FROM change_draft cd WHERE cd.change_id=wa.holder), false)) AS cross_draft
      FROM waits_all wa
     WHERE NOT EXISTS (SELECT 1 FROM symovl so WHERE so.relation='disjoint' AND so.path=wa.path
                         AND ((so.a_change=wa.waiter AND so.b_change=wa.holder)
                           OR (so.a_change=wa.holder AND so.b_change=wa.waiter)))),
  -- the finer POINT for a SURVIVING same-file collision: the symbol the two sides share ('same') or, when we
  -- could not map it finer, the file. One row per (waiter,holder,path) with a content-free locus label.
  -- NAME-THE-SYMBOL is freshness-gated (FIX3, ACCURACY): a 'same' verdict reads sym_a off main's ingested symbol
  -- SPANS, which are only provably aligned to the diff lines when so.freshness_ok (the file's graph content-hash
  -- == BOTH sides' base hash). Under a STALE graph the collision is still correctly KEPT ('same'/'unknown' both
  -- serialize — recall held), but sym_a may name the WRONG symbol ("you both edit `render_pr_check`" when in
  -- truth it's a different function shifted to those lines). So we only emit the symbol NAME when freshness is
  -- proven; otherwise it stays NULL and render falls back to the line-range / same-file phrasing (line_lo/line_hi
  -- come from the claim's OWN changed ranges, not the stale spans, so they remain honest). Same proof that gates
  -- 'disjoint' in _file_pair_symbol_overlap now also gates the confident symbol name.
  wait_points AS (
    SELECT wa.waiter, wa.holder, wa.path,
           (SELECT min(so.sym_a) FROM symovl so WHERE so.relation='same' AND so.freshness_ok AND so.path=wa.path
              AND ((so.a_change=wa.waiter AND so.b_change=wa.holder) OR (so.a_change=wa.holder AND so.b_change=wa.waiter))) AS symbol,
           (SELECT min(so.line_lo) FROM symovl so WHERE so.path=wa.path
              AND ((so.a_change=wa.waiter AND so.b_change=wa.holder) OR (so.a_change=wa.holder AND so.b_change=wa.waiter))) AS line_lo,
           (SELECT max(so.line_hi) FROM symovl so WHERE so.path=wa.path
              AND ((so.a_change=wa.waiter AND so.b_change=wa.holder) OR (so.a_change=wa.holder AND so.b_change=wa.waiter))) AS line_hi
      FROM waits wa),
  -- MECHANICAL conflict anticipation per SURVIVING collision (waiter↔holder on one path): is a git merge
  -- conflict on rebase likely, and near which content-free line? conflict is symmetric in (a,b) for a path, so
  -- match on EITHER orientation. Likely ⟺ overlapping or same-insertion-point ranges; NULL/disjoint → not
  -- flagged (no false conflict). This is ADDITIVE to the verdict — surfaced for soft AND hard alike.
  wait_conflicts AS (
    SELECT wa.waiter, wa.holder, wa.path,
           COALESCE((SELECT bool_or(co.merge_conflict_likely) FROM conflict co WHERE co.path=wa.path
                       AND ((co.a_change=wa.waiter AND co.b_change=wa.holder) OR (co.a_change=wa.holder AND co.b_change=wa.waiter))), false) AS conflict_likely,
           (SELECT min(co.conflict_line) FROM conflict co WHERE co.path=wa.path AND co.merge_conflict_likely
              AND ((co.a_change=wa.waiter AND co.b_change=wa.holder) OR (co.a_change=wa.holder AND co.b_change=wa.waiter))) AS conflict_line
      FROM waits wa),
  changes AS (SELECT DISTINCT change_id FROM inflight),
  verdicts AS (
    SELECT ch.change_id,
      -- SERIALIZE only on a SURVIVING file-level collision (the finer engine did NOT prove it disjoint). A
      -- change that is physically 'waiting' but whose every collision is symbol-DISJOINT is NOT serialized here
      -- (the finer win: no false "wait in line") — it falls through to the normal warn/unknown/clear logic.
      --
      -- HARD vs SOFT serialize (PRECISION / anti-wallpaper, the #116 fix + the HIGH false-PAUSE append fix): a
      -- surviving collision on a REAL source file (server.py, a .sql, anything NOT a runner/registration list) at a
      -- real symbol is a hard 'serialize' — land in order. But TWO classes of collision SOFTEN to 'serialize_soft'
      -- (a "heads up", still surfaced, never silent, never a pause):
      --   (1) low_value     — the collision is ONLY on append-mostly build/test-RUNNER files (run_gates.sh and kin):
      --                       a trivial append-ORDER git conflict, not logic coupling (the #116 fix).
      --   (2) append_soft   — the finer engine PROVED the overlap is pure-additive (two PRs each appending a brand-
      --                       new function to disjoint line tails of the SAME file, sharing no symbol): the canonical
      --                       multi-agent pattern, a git-append-order conflict, not logic coupling (the false-PAUSE fix).
      --   (3) advisory_res  — the HOLDER is a non-release 'BR-<branch>' reservation with NO open PR (#851 ROOT
      --                       MODEL): a pushed branch with no open PR is NOT in the merge queue, so it never
      --                       hard-blocks a real PR — REGARDLESS OF AGE (supersedes the #917/#923 age-decay).
      --                       Release/hotfix/develop lanes are the opt-in EXEMPT set (core._branch_matches_release)
      --                       and stay hard; a real 'PR-<n>' holder is NEVER advisory. The real PRs still order
      --                       AMONG THEMSELVES behind an advisory BR (the synthetic later-PR→earliest-PR hard wait
      --                       in waits_all), so a genuine cross-PR collision is untouched.
      -- THE GUARD (a soft wait must never MASK a real collision): hard wins whenever ANY surviving wait is none of
      -- low_value / append_soft / advisory_res. So 'serialize_soft' requires ≥1 surviving wait AND that EVERY surviving
      -- wait be soft (low_value, proven-append, or an advisory branch reservation); the moment one real-source same-
      -- symbol collision — or a later-PR-behind-earlier-PR hard wait — is also present, the pair is hard 'serialize'
      -- again. A change with no surviving wait at all falls through to warn/unknown/clear.
      -- DRAFT-CROSS-STATE softening (PO 2026-06-25 round-2 SCOUT-WINDOW): a wait whose two ends straddle the
      -- draft/non-draft boundary is a SCOUT overlap, NOT a queue-in-line — a non-draft must never be paused
      -- behind a still-iterating scout, and a scout must never feel paused behind a non-draft it can rebase on.
      -- So a cross_draft wait NEVER contributes to 'serialize'/'serialize_soft'; only same-state waits do.
      -- A change whose only surviving waits are cross_draft falls through to 'warn' (the heads-up tier — the
      -- contention is real, the queue-in-line copy isn't). Draft↔draft is same-state → still HARD/SOFT (scout-
      -- vs-scout coordination is still actionable; the existing low_value/append_soft logic governs hard vs soft).
      CASE WHEN EXISTS(SELECT 1 FROM waits wkeep WHERE wkeep.waiter=ch.change_id
                         AND NOT wkeep.cross_draft AND NOT (wkeep.low_value OR wkeep.append_soft OR wkeep.advisory_res)) THEN 'serialize'
           WHEN EXISTS(SELECT 1 FROM waits wkeep WHERE wkeep.waiter=ch.change_id AND NOT wkeep.cross_draft) THEN 'serialize_soft'
           -- a cross-draft wait is still a real overlap, just not a hold-up → 'warn' (heads-up).
           WHEN EXISTS(SELECT 1 FROM waits wkeep WHERE wkeep.waiter=ch.change_id AND wkeep.cross_draft) THEN 'warn'
           WHEN EXISTS(SELECT 1 FROM contested c WHERE c.by_change=ch.change_id) THEN 'warn'
           -- UNKNOWN-FIRST (honest recall): a change touching a path that is NOT a file in main's graph
           -- (a new file, an unsupported language, an un-indexed dir) cannot be predicted — say so, never
           -- a false 'clear'. A missed collision is the failure we sell against; silence ≠ safe.
           WHEN EXISTS(SELECT 1 FROM inflight i WHERE i.change_id=ch.change_id AND NOT EXISTS(
                 SELECT 1 FROM _mis_graphfile g WHERE g.path=i.path)) THEN 'unknown'   -- in main's graph? (the file-path set, materialized+indexed above — NOT a per-change cold core.code_node seq scan)
           -- CO-CHANGE-CORROBORATED hub coupling (recall, precision-SAFE): a dampened coupling that git co-change
           -- INDEPENDENTLY confirms (lift>=2 & co>=3) is a real coupling TWO signals agree on, not hub-noise — so it
           -- EARNS a 'warn'. This is the ONLY relaxation of AUDIT3's never-warn guard, gated on the corroboration so
           -- it can never reopen the bare-indegree noise (an uncorroborated dampened coupling still goes 'unknown').
           WHEN EXISTS(SELECT 1 FROM dampened dm WHERE dm.change_id=ch.change_id AND dm.corroborated) THEN 'warn'
           -- IN-FLIGHT UNBOUNDED-LOSS WALL: failed/incomplete extraction on ANY currently changing document can
           -- hide an arbitrary coupling to another currently changing path. Once the stronger Serialize/Warn
           -- evidence above has had precedence, every remaining in-flight change is Unknown rather than a false
           -- Clear. Bounded analysis/reference ambiguity never activates this wall.
           WHEN (SELECT present FROM inflight_unbounded_loss) THEN 'unknown'
           -- CLOSED UNCERTAINTY FALLBACK: parser/extractor failure or an ambiguous local reference means the
           -- stored path is present but its structural neighborhood is incomplete. Stronger serialize/warn
           -- evidence above still wins; otherwise this is Unknown, never a false Clear.
           WHEN EXISTS(
             SELECT 1 FROM inflight i JOIN _mis_uncertain u ON u.path=i.path
              WHERE i.change_id=ch.change_id
           ) THEN 'unknown'
           -- UNKNOWN-FIRST over HUB DAMPENING (AUDIT3): a real import coupling with ANOTHER in-flight change
           -- was suppressed because an endpoint is a hub. We HAD the edge and hid it for noise — so this is
           -- NOT a confident 'clear'; surface it as 'unknown' (honest) with dampened_with, never silently safe.
           WHEN EXISTS(SELECT 1 FROM dampened dm WHERE dm.change_id=ch.change_id) THEN 'unknown'
           ELSE 'clear' END AS verdict
    FROM changes ch),
  -- Changes whose verdict was promoted specifically by the in-flight unbounded-loss wall. Existing local Unknown
  -- causes retain their precise path/reason; only changes that would otherwise have reached Clear receive the fixed
  -- peer-loss reason on their own paths. Stronger Serialize/Warn verdicts never enter this set.
  inflight_loss_promoted AS MATERIALIZED (
    SELECT v.change_id
      FROM verdicts v
     WHERE v.verdict='unknown'
       AND (SELECT present FROM inflight_unbounded_loss)
       AND NOT EXISTS(
             SELECT 1 FROM inflight i
              WHERE i.change_id=v.change_id
                AND NOT EXISTS(SELECT 1 FROM _mis_graphfile g WHERE g.path=i.path))
       AND NOT EXISTS(
             SELECT 1 FROM inflight i JOIN _mis_uncertain u ON u.path=i.path
              WHERE i.change_id=v.change_id)
       AND NOT EXISTS(
             SELECT 1 FROM dampened dm WHERE dm.change_id=v.change_id)),
  -- impacts feeds suggested_order: count only TRUE blast radius (down+shared, not upstream deps), so
  -- "biggest blast first" = most DEPENDENTS first = the foundational change lands first (the rest revise once).
  impacts AS (SELECT f_change_id AS change_id, count(DISTINCT nbr) AS ic FROM adj WHERE dir IN ('down','shared') GROUP BY f_change_id),
  -- TOPOLOGICAL land order: change A is UPSTREAM of change B (A should land first) when B depends on A —
  -- i.e. a path B edits is DOWNSTREAM (dir='down') of a path A edits. We rank by how DEEP the in-flight
  -- dependent DAG runs BELOW a change — its longest dependent chain (= height) — so a chain X→Y→Z orders
  -- X,Y,Z even though a middle change can have more DIRECT dependents than its own upstream (the direct-count
  -- heuristic would wrongly put the middle first). The DEPTH (height) is monotone along every dependency edge
  -- (an upstream is STRICTLY deeper than any of its dependents), so ranking by it is a SOUND topological order.
  --
  -- DEGENERATE-GRAPH BOUND (audit 2026-06-18): the prior version computed the FULL transitive closure
  -- (root,node) — O(N²) rows on a deep linear chain (a 2000-deep import chain = 2.0M rows, ~36s, webhook-path
  -- timeout). This walk carries only (change_id, depth) and is capped at v_landorder_hops, so it is bounded by
  -- O(edges × hops) regardless of graph shape — chain, diamond/lattice, fan-in hub, or a dependency CYCLE
  -- (UNION dedups full rows so a cycle just re-enters its own ids and terminates at the hop cap). Measured:
  -- the same 2000-chain drops 36s→0.6s. Capping the depth only flattens the rank PAST the cap (those changes
  -- tie and fall through to the blast-count→id tiebreak) — still a valid order, never a contradiction.
  change_dep AS (
    SELECT DISTINCT adj.f_change_id AS up, i.change_id AS down
      FROM adj JOIN inflight i ON i.path = adj.nbr AND i.change_id <> adj.f_change_id
     WHERE adj.dir = 'down'),
  -- height = longest dependent chain BELOW a change. Seed the SINKS (a most-downstream change that nothing
  -- in-flight depends on — it is some edge's 'down' but never an 'up') at depth 0, then climb UP one dependency
  -- edge per step (cd.down = current → cd.up gets depth+1). max(d) per change = its height. Hop-capped
  -- (dw.d < v_landorder_hops) so a deep chain / lattice / CYCLE is bounded; UNION dedups (cycle re-entry terminates).
  depth_walk AS (
    SELECT cd.down AS change_id, 0 AS d
      FROM change_dep cd
     WHERE NOT EXISTS (SELECT 1 FROM change_dep c2 WHERE c2.up = cd.down)   -- a sink: nothing in-flight depends on it
    UNION
    SELECT cd.up AS change_id, dw.d + 1 AS d
      FROM depth_walk dw JOIN change_dep cd ON cd.down = dw.change_id
     WHERE dw.d < v_landorder_hops),
  trans_dep AS (SELECT change_id, max(d) AS td FROM depth_walk GROUP BY change_id),
  -- CONTENTION NEIGHBORHOODS: read the connected-components result already computed by the POINTER-JUMPING loop in
  -- the plpgsql body ABOVE (into the temp table _mis_node), off the SAME single `_mis_adj` adjacency scan this WITH's
  -- `adj` CTE reads. comp = the MIN change id in each node's component, so this is SEMANTICALLY IDENTICAL to the old
  -- inline recursive min-label CTE (comp_cc) and to the standalone core._inflight_components — every downstream
  -- cluster/verdict/agents/changes/hotspot computation is unchanged. WHY it moved OUT of this WITH into a pre-loop:
  --   • EXACT + BOUNDED (this audit): the old inline recursive min-label closure materialized O(N²) (node,comp) pairs
  --     on a degenerate shape (a 1500-deep contention chain ≈ 1.1M rows / ~19s, statement-timeout on the WEBHOOK
  --     path). Pointer-jumping (HOOK+JUMP to fixpoint, cluster_max_rounds backstop) is O(log N) rounds, exact for any
  --     shape (a fully entangled N-ring collapses to ONE component; a deep chain to ONE) — but it needs an imperative
  --     loop over mutable scratch, which a single declarative CTE cannot express, hence the pre-WITH plpgsql block.
  --   • SINGLE adjacency scan (audit:scale 2026-06-18 — the perf gate's LOCK3): the components are built off the
  --     already-materialized _mis_adj, NOT by re-invoking the standalone connected-components helper, which would
  --     re-run _claim_adjacency — the O(graph) whole-graph scan — a SECOND time (the audited 2.48s→1.33s regression).
  --     The surface body therefore contains NO call to that helper; _claim_adjacency runs exactly ONCE (into
  --     _mis_adj). split_candidates is unaffected — it never called the helper.
  comps AS (SELECT change_id, comp FROM _mis_node),
  member_touch AS (   -- what each in-flight change touches: its own claimed paths + its blast-radius neighbors
    SELECT change_id, path AS file FROM inflight
    UNION SELECT f_change_id AS change_id, nbr AS file FROM adj),
  clusters AS (SELECT comp, count(*)::int AS sz FROM comps GROUP BY comp HAVING count(*) >= 2),
  -- SHARED FOUNDATION / SPLIT ADVICE (PO 2026-06-18 "独り占め禁止" + "Veripsaに分割を勧めさせる" / 触る奴にだけ出す):
  -- a CHRONIC contention file the PR touches — it changes often AND is either a FOUNDATION (many files import it →
  -- wide blast radius, downstream waits) OR a GOD-FILE (defines many symbols → one file doing too many things, so
  -- independent edits keep colliding). Both → split into cohesive modules. Same gate as split_candidates so the
  -- inline PR note and the chart agree. Computed over MAIN's graph + ledger. Content-free (path + counts only).
  hot_fanin AS (   -- FOUNDATION arm — in-degree: distinct files importing each file (resolved to internal FILE nodes — not stdlib)
    SELECT g.path AS path, count(DISTINCT e.src)::int AS fan_in
      FROM core.code_edge e
      JOIN _mis_graphfile g
        ON core._semantic_ref_key(g.path)
           =COALESCE(e.semantic_dst_key,core._semantic_ref_key(e.dst))
     WHERE e.account_id=v_account AND e.repo=v_repo AND e.branch=v_branch
       AND e.edge_kind='imports' AND e.reference_status IS NULL
     GROUP BY g.path),
  hot_size AS (   -- GOD-FILE arm — how many symbols the file DEFINES (def/class/table nodes). CONTENT-FREE size proxy:
                  -- a file doing too many things, INDEPENDENT of who imports it (server.py-class: low fan-in, huge body).
    SELECT path, count(*)::int AS symbols FROM core.code_node
     WHERE account_id=v_account AND repo=v_repo AND branch=v_branch AND node_kind IN ('def','class','table')
     GROUP BY path),
  hot_churn AS (   -- how often the file actually landed on the protected branch in the window (gates BOTH arms — a file that never changes is fine even if huge / widely-imported)
    SELECT path, count(*)::int AS churn FROM core.event
     WHERE account_id=v_account AND kind='landed' AND repo=v_repo AND branch=v_branch AND path<>''
       AND occurred_at > now() - make_interval(days => v_win) GROUP BY path),
  -- PERF (audit:perf 2026-06-18): MATERIALIZED is load-bearing, NOT cosmetic. `hotspot` is referenced ONLY by
  -- the CORRELATED per-change subquery `shared_foundation` (… JOIN hotspot hs … WHERE i.change_id=v.change_id).
  -- Without the keyword Postgres INLINES it into that subquery and re-runs the WHOLE fan-in scan (full code_edge
  -- seq scan + count(DISTINCT src) GroupAggregate over EVERY import edge) ONCE PER in-flight change = O(edges ×
  -- changes). Measured at 3000 files / 8k imports / 80 PRs: that one correlated re-scan was 3.8s of a 4.2s call
  -- (auto-explain SubPlan: 47.7ms × 80 loops, 33k buffer hits). The fan-in is change-INDEPENDENT, so forcing a
  -- SINGLE evaluation (then 80 cheap joins against the small materialized result) is semantics-IDENTICAL and cuts
  -- main_impact_surface ~4.2s → ~0.7s. (split_candidates computes the same arm once already, so it never hit this.)
  hotspot AS MATERIALIZED (     -- a CHRONIC contention point: it CHURNS and (many files import it OR it defines many symbols).
                               -- TWO arms, BOTH gated on churn (a stable file is good design, not a hotspot):
                               --   FOUNDATION = high fan-in  → splitting frees the downstream waiters; basis 'foundation'.
                               --   GOD-FILE   = high symbols → splitting separates concerns so independent edits stop colliding; basis 'god_file'.
                               -- 'both' when it qualifies on both arms. (hot_size is also change-independent → materialized once, same as fan_in.)
    SELECT u.path, COALESCE(f.fan_in,0) AS fan_in, COALESCE(s.symbols,0) AS symbols, COALESCE(c.churn,0) AS churn,
           CASE WHEN COALESCE(f.fan_in,0) >= v_fanin AND COALESCE(s.symbols,0) >= v_symbols THEN 'both'
                WHEN COALESCE(f.fan_in,0) >= v_fanin THEN 'foundation'
                ELSE 'god_file' END AS basis
      FROM (SELECT path FROM hot_fanin UNION SELECT path FROM hot_size) u
      LEFT JOIN hot_fanin f USING(path)
      LEFT JOIN hot_size  s USING(path)
      LEFT JOIN hot_churn c USING(path)
     WHERE COALESCE(c.churn,0) >= v_churn
       AND (COALESCE(f.fan_in,0) >= v_fanin OR COALESCE(s.symbols,0) >= v_symbols)
       AND NOT core._is_generated_or_vendored_path(u.path))   -- don't advise splitting a vendored/generated file (cry-wolf); verdicts unaffected
  SELECT jsonb_build_object(
    'repo', v_repo, 'branch', v_branch,
    'inflight_count',  (SELECT count(*)::int FROM changes),
    'cluster_count',   (SELECT count(*)::int FROM clusters),
    -- contention NEIGHBORHOODS (the N-scale unit): each cluster = entangled in-flight PRs to coordinate as
    -- a group. suggested_order = biggest blast-radius first (land the foundational change first so the rest
    -- revise against it ONCE = 手戻り減). A suggestion, not a block (steer, don't hostage).
    'clusters', COALESCE((SELECT jsonb_agg(jsonb_build_object(
        'cluster_id', cl.comp,
        'size', cl.sz,
        -- a cluster SERIALIZES only when a member has a SURVIVING direct collision (the finer engine did not
        -- prove it disjoint) — not merely because some member's claim is physically 'waiting'. A cluster whose
        -- only same-file overlap is symbol-DISJOINT is 'warn' (a neighborhood to coordinate), never a false wait.
        -- PRECISION (the #116 fix + the append false-PAUSE fix, same rule as the per-change verdict): a collision
        -- ONLY on append-mostly build/test-RUNNER files (low_value) OR a PROVEN pure-additive append-vs-append
        -- overlap (append_soft) is a trivial git-order conflict, not logic coupling — it does NOT make the whole
        -- neighborhood serialize. So 'serialize' requires a member with a surviving wait that is neither low_value
        -- NOR append_soft; a cluster whose only surviving overlap is soft (or symbol-disjoint) is 'warn' (coordinate).
        -- Draft-cross-state waits NEVER make a cluster serialize either (PO 2026-06-25 SCOUT-WINDOW): a hard
        -- serialize at cluster level requires a same-state real-source wait, exactly like the per-change verdict.
        'verdict', CASE WHEN EXISTS(SELECT 1 FROM comps cm JOIN waits wk ON wk.waiter=cm.change_id WHERE cm.comp=cl.comp AND NOT wk.cross_draft AND NOT (wk.low_value OR wk.append_soft OR wk.advisory_res)) THEN 'serialize' ELSE 'warn' END,
        'agents', (SELECT jsonb_agg(l.label ORDER BY l.label) FROM comps cm JOIN labels l ON l.change_id=cm.change_id WHERE cm.comp=cl.comp),
        'changes', (SELECT jsonb_agg(cm.change_id ORDER BY cm.change_id) FROM comps cm WHERE cm.comp=cl.comp),
        -- TOPOLOGICAL: most TRANSITIVE in-flight dependents first (= the foundation the rest sit on), then
        -- direct blast, then id — so dependents land AFTER the upstream they will rebase onto (revise once).
        'suggested_order', (SELECT jsonb_agg(x.label ORDER BY x.td DESC, x.ic DESC, x.change_id)
                              FROM (SELECT cm.change_id, l.label, COALESCE(im.ic,0) AS ic, COALESCE(tdp.td,0) AS td
                                      FROM comps cm JOIN labels l ON l.change_id=cm.change_id
                                      LEFT JOIN impacts im ON im.change_id=cm.change_id
                                      LEFT JOIN trans_dep tdp ON tdp.change_id=cm.change_id WHERE cm.comp=cl.comp) x),
        -- HOTSPOTS = the files ≥2 members of this cluster touch (claim or blast-radius) — the crux to
        -- coordinate first (where a big cluster actually collides; the market's "shared hotspot files").
        'hotspots', (SELECT COALESCE(jsonb_agg(h.file ORDER BY h.file), '[]'::jsonb)
                       FROM (SELECT mt.file FROM member_touch mt JOIN comps cm ON cm.change_id=mt.change_id
                              WHERE cm.comp=cl.comp GROUP BY mt.file HAVING count(DISTINCT mt.change_id) >= 2) h)
      ) ORDER BY cl.sz DESC, cl.comp) FROM clusters cl), '[]'::jsonb),
    'warn_count',      (SELECT count(*)::int FROM verdicts WHERE verdict='warn'),
    'serialize_count', (SELECT count(*)::int FROM verdicts WHERE verdict='serialize'),
    -- soft serialize (a surviving collision ONLY on append-mostly runner/registration files = a trivial
    -- append-order git conflict, surfaced as a low-stakes "heads up", never a hard wait). Separate count so a
    -- "precise silence" health metric can track over-serialization without conflating it with real waits.
    'serialize_soft_count', (SELECT count(*)::int FROM verdicts WHERE verdict='serialize_soft'),
    'unknown_count',   (SELECT count(*)::int FROM verdicts WHERE verdict='unknown'),
    'clear_count',     (SELECT count(*)::int FROM verdicts WHERE verdict='clear'),
    'changes', COALESCE((SELECT jsonb_agg(jsonb_build_object(
        'change_id', v.change_id,
        'head_sha', (SELECT ch.head_sha FROM change_head ch WHERE ch.change_id=v.change_id),
        'agent', l.agent,
        'label', l.label,
        'verdict', v.verdict,
        -- SCOUT-WINDOW: per-change draft state (the render layer reads this to suffix partner labels with
        -- "(draft)" so a reader can tell at a glance which referenced PRs are still iterating).
        'is_draft', COALESCE((SELECT cd.is_draft FROM change_draft cd WHERE cd.change_id=v.change_id), false),
        'paths', (SELECT COALESCE(jsonb_agg(DISTINCT i.path ORDER BY i.path),'[]'::jsonb) FROM inflight i WHERE i.change_id=v.change_id),
        -- BLAST RADIUS = structurally DOWNSTREAM + shared only (the bug fix). Upstream dependencies (dir='up')
        -- are what this change DEPENDS ON — editing it cannot affect them — so they are NOT its blast radius.
        'impact', (SELECT COALESCE(jsonb_agg(DISTINCT adj.nbr ORDER BY adj.nbr),'[]'::jsonb) FROM adj WHERE adj.f_change_id=v.change_id AND adj.dir IN ('down','shared')),
        'impact_count', (SELECT count(DISTINCT adj.nbr)::int FROM adj WHERE adj.f_change_id=v.change_id AND adj.dir IN ('down','shared')),
        'contested_with', (SELECT COALESCE(jsonb_agg(DISTINCT lw.label ORDER BY lw.label),'[]'::jsonb) FROM contested c JOIN labels lw ON lw.change_id=c.with_change WHERE c.by_change=v.change_id),
        -- serialize_behind = the HARD-BLOCKER holders this change is queued behind. An ADVISORY (non-release) branch
        -- reservation (#851 ROOT MODEL) is NOT a hard blocker (its collision is downgraded to serialize_soft above),
        -- so it is excluded here — it must never appear as a "wait in line behind" holder (the exact phantom-hold
        -- #851 removes). It is still surfaced as a non-blocking overlap via the serialize_soft verdict + collision_
        -- points. When the active holder is an advisory BR, a LATER PR IS queued behind the EARLIEST real PR (a hard,
        -- non-advisory synthetic wait from waits_all), so serialize_behind correctly names that PR — the real merge queue.
        'serialize_behind', (SELECT COALESCE(jsonb_agg(DISTINCT lh.label ORDER BY lh.label),'[]'::jsonb) FROM waits w JOIN labels lh ON lh.change_id=w.holder WHERE w.waiter=v.change_id AND NOT w.advisory_res),
        -- THE INVERSE of serialize_behind (PO 2026-06-18: tell the lane HOLDER who is now waiting behind it). The
        -- waiter is told "⏸ wait behind PR-A", but PR-A (first in line) was NEVER told anyone is queued on it —
        -- yet "others are blocked on me" is actionable: keep this change focused and land it promptly to free the
        -- lane. Same `waits` rows, flipped: filter on holder=this change, label the WAITERS. Content-free (labels +
        -- the shared lane paths only). Empty when nobody is queued behind this change.
        'queued_behind', (SELECT COALESCE(jsonb_agg(DISTINCT lw.label ORDER BY lw.label),'[]'::jsonb) FROM waits w JOIN labels lw ON lw.change_id=w.waiter WHERE w.holder=v.change_id),
        -- the shared lane(s) (reserved paths) others are waiting on behind this change — the WHERE of "you are holding them up".
        'queued_behind_paths', (SELECT COALESCE(jsonb_agg(DISTINCT w.path ORDER BY w.path),'[]'::jsonb) FROM waits w WHERE w.holder=v.change_id),
        -- FINER COLLISION POINT (the named locus of a SURVIVING direct collision): for each holder this change
        -- still waits behind, WHERE on the file they actually overlap → [{behind, path, symbol, line_lo, line_hi}].
        -- 'symbol' is set when the two sides touch the SAME symbol AND the graph is provably FRESH for the file
        -- (FIX3: spans aligned to the diff lines) — name it: "you both edit `render_pr_check`". Under a STALE
        -- graph the collision is still KEPT but 'symbol' is NULL (we don't name a possibly-wrong symbol); the line
        -- range / file is then the locus ("...both edit lines 140–160", from the claim's OWN ranges). Content-free
        -- (path, symbol NAME, line numbers — never code). Empty when this change has no surviving direct collision.
        'collision_points', (SELECT COALESCE(jsonb_agg(jsonb_build_object(
                                'behind', lh.label, 'path', wp.path, 'symbol', wp.symbol,
                                'line_lo', wp.line_lo, 'line_hi', wp.line_hi)
                                ORDER BY lh.label, wp.path),'[]'::jsonb)
                              FROM wait_points wp JOIN labels lh ON lh.change_id=wp.holder WHERE wp.waiter=v.change_id),
        -- MECHANICAL merge-conflict anticipation (a DIFFERENT axis from severity): does this change's geometry
        -- make a GIT merge conflict on rebase likely against a holder it waits behind? True even for a SOFTENED
        -- low-value collision (the run_gates.sh regression fix) — softening the severity must not drop the
        -- conflict forewarning. HONEST: "likely", not "will" (content-free heuristic, no 3-way merge of bodies).
        'merge_conflict_likely', (SELECT COALESCE(bool_or(wc.conflict_likely), false) FROM wait_conflicts wc WHERE wc.waiter=v.change_id),
        -- WHERE the likely conflict is: [{behind, path, line}] — the holder + file + content-free "near line N"
        -- (the overlap/same-insertion-point line). One row per holder×path with a likely conflict. Empty when no
        -- surviving collision is conflict-likely (disjoint geometry, or no line ranges to anticipate from).
        'conflict_points', (SELECT COALESCE(jsonb_agg(jsonb_build_object(
                                'behind', lh.label, 'path', wc.path, 'line', wc.conflict_line)
                                ORDER BY lh.label, wc.path),'[]'::jsonb)
                              FROM wait_conflicts wc JOIN labels lh ON lh.change_id=wc.holder
                             WHERE wc.waiter=v.change_id AND wc.conflict_likely),
        'unknown_paths', (SELECT COALESCE(jsonb_agg(DISTINCT i.path ORDER BY i.path),'[]'::jsonb) FROM inflight i WHERE i.change_id=v.change_id
                            AND (
                              NOT EXISTS(SELECT 1 FROM _mis_graphfile g WHERE g.path=i.path)
                              OR EXISTS(SELECT 1 FROM _mis_uncertain u WHERE u.path=i.path)
                              OR EXISTS(SELECT 1 FROM inflight_loss_promoted p WHERE p.change_id=v.change_id)
                            )),   -- absent, locally uncertain, or promoted by current in-flight loss: never Clear
        'graph_uncertainty', (SELECT COALESCE(jsonb_agg(
                                jsonb_build_object('path',u.path,'reason',u.reason)
                                ORDER BY u.path,u.reason
                              ),'[]'::jsonb)
                              FROM (
                                SELECT DISTINCT mu.path,mu.reason
                                  FROM _mis_uncertain mu
                                  JOIN inflight i ON i.path=mu.path
                                 WHERE i.change_id=v.change_id
                                UNION
                                SELECT DISTINCT i.path,'inflight_peer_analysis_incomplete'
                                  FROM inflight_loss_promoted p
                                  JOIN inflight i ON i.change_id=p.change_id
                                 WHERE p.change_id=v.change_id
                              ) u),
        -- DAMPENED-but-real couplings hidden by hub dampening (AUDIT3): [{by, via_hub, corroborated}] — makes the
        -- silence VISIBLE so the verdict is honest, not a hole. `corroborated` (co-change lift>=2 & co>=3) marks the
        -- ones promoted to 'warn' ("two signals agree — coordinate") vs the suppressed-only ones that stay 'unknown'
        -- ("not auto-analyzed to avoid noise"). The renderer reads the flag to pick the honest copy per entry.
        'dampened_with', (SELECT COALESCE(jsonb_agg(DISTINCT jsonb_build_object('by', lw.label, 'via_hub', dm.via_hub, 'corroborated', dm.corroborated) ORDER BY jsonb_build_object('by', lw.label, 'via_hub', dm.via_hub, 'corroborated', dm.corroborated)),'[]'::jsonb)
                            FROM dampened dm JOIN labels lw ON lw.change_id=dm.by_change WHERE dm.change_id=v.change_id),
        -- the UPSTREAM warning (the most important one): files this change DEPENDS ON that another in-flight
        -- change is editing right now → [{path, by}]. Render this as the headline, above the downstream blast.
        'depends_on_changing', (SELECT COALESCE(jsonb_agg(jsonb_build_object('path', x.dep_path, 'by', x.label) ORDER BY x.dep_path),'[]'::jsonb)
                                  FROM (SELECT DISTINCT dc.dep_path, lb.label FROM depends_changing dc JOIN labels lb ON lb.change_id=dc.by_change WHERE dc.change_id=v.change_id) x),
        -- SHARED FOUNDATION: the paths THIS change reserves that are load-bearing (many import them AND they change
        -- often) → flagged to whoever touches one, not only on collision. [{path, fan_in, churn}], biggest in-degree first.
        'shared_foundation', (SELECT COALESCE(jsonb_agg(jsonb_build_object('path', hs.path, 'fan_in', hs.fan_in, 'churn', hs.churn, 'symbols', hs.symbols, 'basis', hs.basis) ORDER BY hs.fan_in DESC, hs.symbols DESC, hs.path),'[]'::jsonb)
                                FROM inflight i JOIN hotspot hs ON hs.path=i.path WHERE i.change_id=v.change_id)
      ) ORDER BY l.label) FROM verdicts v JOIN labels l ON l.change_id=v.change_id), '[]'::jsonb)
  ));
  RETURN v_result;
END $$;
ALTER FUNCTION core.main_impact_surface(text,text) OWNER TO veripsa_migrator;
-- LEAST-PRIVILEGE (audit iter-5 P3): strip the PUBLIC-EXECUTE default so a non-tenant role (billing/platform-
-- reader) can't reach the main-impact prediction surface; the GRANT names exactly the tenant roles (the App
-- reads it via veripsa_writer, which it inherits). Function-only DDL → contention-free (no table lock).
REVOKE EXECUTE ON FUNCTION core.main_impact_surface(text,text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION core.main_impact_surface(text,text) TO veripsa_reader, veripsa_writer, veripsa_demo_steward;

-- ============================================================================================
-- split_candidates — PROACTIVE structural advice (not just reactive per-PR warnings; PO: ファイル分割を促す).
-- A file that agents repeatedly have to SERIALIZE on (many collision_held events, across many distinct
-- agents, over a bounded window) is a STRUCTURAL bottleneck, not a one-off — the same lesson that split this
-- schema itself: "1 file = 1 holder was the bottleneck". Veripsa already has the data (the collision ledger,
-- core.event kind='collision_held'), so it can tell the console "this file is a chronic hotspot — consider
-- splitting it into cohesive modules".
--
-- THREE ARMS (PO 2026-06-18: "1ファイルに2本以上依存は効率悪い…お知らせできると良い" + "やって" — add the god-file arm):
--   1. REACTIVE  — a path already SERIALIZED on (held collisions ≥ split_min_collisions). PROVEN pain.
--   3. GOD-FILE/SIZE — a path that DEFINES too many symbols (def/class/table ≥ split_min_symbols) AND churns, even
--      at LOW fan-in (the server.py class: not a widely-imported foundation, just a huge file doing many things, so
--      independent edits keep colliding inside it). Size is the content-free symbol COUNT, never the bodies.
--   2. STRUCTURAL/PREDICTIVE — a path that WILL become a magnet, before the collisions pile up. Veripsa's OWN
--      code graph already knows the blast radius: FAN-IN = how many distinct files import this one (in-degree
--      over the 'imports' edges, resolved to internal FILE nodes — never stdlib symbols like os/len). But fan-in
--      ALONE misleads: a widely-imported file that NEVER changes is GOOD design, not a hotspot (e.g. on this very
--      repo code_graph_extract.py has fan-in 10 yet ~0 churn — it is stable, leave it). The risk is blast-radius ×
--      how-often-it-moves: FAN-IN × CHURN, where CHURN = how many times the file actually LANDED on main in the
--      window (the 'landed' ledger). So the structural arm fires only when fan_in ≥ split_min_fanin AND churn ≥
--      split_min_churn — depended-on AND frequently-changing. EVERY candidate is enriched with fan_in + churn so
--      even a reactive one shows its structural profile, and 'basis' says which arm fired (collisions/structural/both).
-- ADVISORY / notify-only: it ranks, it never blocks. Content-free (paths + counts only, never code). HONEST-EMPTY
-- (no fabricated candidates; on a small healthy repo it is simply empty). Derived purely from the EXISTING event
-- ledger + code graph (NO new table — a signal is a read over KINDs/edges, not a NOUN). All four membership knobs
-- are BOUNDED per-account (core._policy_int — owner-tunable via set_policy, never a free-form config that lets the
-- frame lie). STABLE (read-only). Tenant-pinned (account from resolve_session_identity, like every other surface).
CREATE OR REPLACE FUNCTION core.split_candidates(p_repo text DEFAULT '', p_branch text DEFAULT '') RETURNS jsonb
    LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path TO 'core','pg_catalog' AS $$
DECLARE v_account text; v_repo text; v_branch text; v_min int; v_days int; v_fanin int; v_churn int; v_symbols int; v_result jsonb;
BEGIN
  SELECT account INTO v_account FROM core.resolve_session_identity() AS r(agent,account);
  PERFORM set_config('core.current_account', v_account, true);
  v_repo   := left(COALESCE(p_repo,''),512);
  v_branch := left(COALESCE(p_branch,''),512);
  -- BOUNDED KNOBS (clamped via the EXISTING _policy_int; owner-tunable through set_policy, never out of frame):
  --   split_min_collisions : held collisions on ONE path before the REACTIVE arm fires. default 3, clamp 1..100.
  --   split_window_days    : the lookback window for "chronic" (collisions AND churn). default 30, clamp 1..365.
  --   split_min_fanin      : importers before the STRUCTURAL arm considers a path. default 5, clamp 2..1000
  --                          (2 = the PO's "2本以上" floor; raise it to silence noise on a hub-heavy repo).
  --   split_min_churn      : lands in the window before the STRUCTURAL arm fires. default 3, clamp 1..1000
  --                          (gates OUT stable hubs — high fan-in + ~0 churn is good design, not a hotspot).
  v_min   := core._policy_int('split_min_collisions', 3, 1, 100);
  v_days  := core._policy_int('split_window_days', 30, 1, 365);
  v_fanin := core._policy_int('split_min_fanin', 5, 2, 1000);
  v_churn := core._policy_int('split_min_churn', 3, 1, 1000);
  v_symbols := core._policy_int('split_min_symbols', 25, 5, 100000);  -- GOD-FILE arm: symbols (def/class/table) the file defines
  v_result := (
    WITH coll AS (   -- REACTIVE: proven serialize pain on a path (held collisions over the window)
      SELECT path, repo, branch,
             count(*)::int                          AS collisions,        -- held clobbers on this path (the load)
             count(DISTINCT agent_id)::int          AS distinct_blocked,  -- how many DIFFERENT agents got held here
             count(DISTINCT counterparty_agent)::int AS distinct_holders, -- how many DIFFERENT holders blocked them
             max(occurred_at)                       AS last_seen
        FROM core.event
       WHERE account_id=v_account AND kind='collision_held'
         AND path <> ''                                                   -- a path-level fact (repo-level facts have no file to split)
         AND occurred_at > now() - make_interval(days => v_days)          -- bounded window
         AND (v_repo='' OR repo=v_repo) AND (v_branch='' OR branch=v_branch)
       GROUP BY path, repo, branch),
    fanin AS (       -- STRUCTURAL: how many distinct files import this one (in-degree, internal FILE targets only)
      SELECT e.repo, e.branch, n.path, count(DISTINCT e.src)::int AS fan_in
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
       WHERE e.account_id=v_account AND e.edge_kind='imports'
         AND e.reference_status IS NULL                                  -- only resolved imports are structural evidence
         AND (v_repo='' OR e.repo=v_repo) AND (v_branch='' OR e.branch=v_branch)
       GROUP BY e.repo, e.branch, n.path),
    size AS (        -- GOD-FILE: how many symbols the file DEFINES (def/class/table) — content-free size proxy: one file doing too many things
      SELECT repo, branch, path, count(*)::int AS symbols
        FROM core.code_node
       WHERE account_id=v_account AND node_kind IN ('def','class','table')
         AND (v_repo='' OR repo=v_repo) AND (v_branch='' OR branch=v_branch)
       GROUP BY repo, branch, path),
    churn AS (       -- STRUCTURAL: how often the file actually MOVED (landed on main) in the window
      SELECT path, repo, branch, count(*)::int AS churn
        FROM core.event
       WHERE account_id=v_account AND kind='landed' AND path <> ''
         AND occurred_at > now() - make_interval(days => v_days)
         AND (v_repo='' OR repo=v_repo) AND (v_branch='' OR branch=v_branch)
       GROUP BY path, repo, branch),
    univ AS (        -- every path touched by ANY arm (keyed repo+branch+path)
      SELECT repo, branch, path FROM coll
      UNION SELECT repo, branch, path FROM fanin
      UNION SELECT repo, branch, path FROM size
      UNION SELECT repo, branch, path FROM churn),
    hot AS (
      SELECT u.repo, u.branch, u.path,
             COALESCE(c.collisions,0)      AS collisions,
             COALESCE(c.distinct_blocked,0) AS distinct_blocked,
             COALESCE(c.distinct_holders,0) AS distinct_holders,
             COALESCE(f.fan_in,0)          AS fan_in,
             COALESCE(sz.symbols,0)        AS symbols,
             COALESCE(ch.churn,0)          AS churn,
             c.last_seen,
             CASE WHEN COALESCE(c.collisions,0) >= v_min
                       AND (COALESCE(f.fan_in,0) >= v_fanin OR COALESCE(sz.symbols,0) >= v_symbols)
                       AND COALESCE(ch.churn,0) >= v_churn                           THEN 'both'
                  WHEN COALESCE(c.collisions,0) >= v_min                             THEN 'collisions'
                  WHEN COALESCE(f.fan_in,0) >= v_fanin                               THEN 'structural'
                  ELSE 'god_file' END     AS basis
        FROM univ u
        LEFT JOIN coll  c  ON c.repo=u.repo  AND c.branch=u.branch  AND c.path=u.path
        LEFT JOIN fanin f  ON f.repo=u.repo  AND f.branch=u.branch  AND f.path=u.path
        LEFT JOIN size  sz ON sz.repo=u.repo AND sz.branch=u.branch AND sz.path=u.path
        LEFT JOIN churn ch ON ch.repo=u.repo AND ch.branch=u.branch AND ch.path=u.path
       WHERE COALESCE(c.collisions,0) >= v_min                                          -- REACTIVE arm (proven held collisions — always shown, even on a vendored file), OR
          OR (NOT core._is_generated_or_vendored_path(u.path) AND (                     -- the PREDICTIVE arms suppress a vendored/generated path (poor split target = cry-wolf):
                 (COALESCE(f.fan_in,0) >= v_fanin AND COALESCE(ch.churn,0) >= v_churn)      -- STRUCTURAL (foundation) arm, OR
              OR (COALESCE(sz.symbols,0) >= v_symbols AND COALESCE(ch.churn,0) >= v_churn))))  -- GOD-FILE (size) arm
    SELECT jsonb_build_object(
      'repo', v_repo, 'branch', v_branch,
      'window_days', v_days, 'min_collisions', v_min, 'min_fanin', v_fanin, 'min_churn', v_churn,
      'candidate_count', (SELECT count(*)::int FROM hot),
      'candidates', COALESCE((SELECT jsonb_agg(jsonb_build_object(
          'path', path, 'repo', repo, 'branch', branch,
          'collisions', collisions, 'distinct_blocked', distinct_blocked, 'distinct_holders', distinct_holders,
          'fan_in', fan_in, 'churn', churn, 'symbols', symbols, 'basis', basis,
          'last_seen', last_seen,
          'suggestion', CASE WHEN basis='structural'
            THEN 'structural hotspot — '||fan_in||' files import it and it lands often; consider splitting into cohesive modules before collisions accumulate'
            WHEN basis='god_file'
            THEN 'god-file — defines '||symbols||' symbols and lands often; one file is doing many things, consider splitting it into cohesive modules'
            ELSE 'chronic serialize bottleneck — consider splitting into cohesive modules (1 file = 1 holder)' END
        ) ORDER BY collisions DESC, (GREATEST(fan_in,symbols)*churn) DESC, path) FROM hot), '[]'::jsonb)));
  RETURN v_result;
END $$;
ALTER FUNCTION core.split_candidates(text,text) OWNER TO veripsa_migrator;
-- LEAST-PRIVILEGE (audit iter-5 P3): strip the PUBLIC-EXECUTE default so a non-tenant role (billing/platform-
-- reader) can't reach the split-candidates surface; the GRANT names exactly the tenant roles (App inherits writer).
REVOKE EXECUTE ON FUNCTION core.split_candidates(text,text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION core.split_candidates(text,text) TO veripsa_reader, veripsa_writer, veripsa_demo_steward;

-- ============================================================================================
