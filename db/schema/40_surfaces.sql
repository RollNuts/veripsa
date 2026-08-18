-- PHASE 2 — SURFACES (the read side). Content-free reads; resolve identity → pin account → query →
-- jsonb. Surfaces are FUNCTIONS (no tables grow). Granted to reader/writer/steward.
-- ============================================================================================

-- agent_name: the humanized display name for an agent id (falls back to the id). account already pinned.
CREATE OR REPLACE FUNCTION core.agent_name(p_agent text) RETURNS text
    LANGUAGE sql STABLE SECURITY DEFINER SET search_path TO 'core','pg_catalog' AS $$
  SELECT COALESCE((SELECT display_name FROM core.agent WHERE agent_id = p_agent), p_agent)
$$;
ALTER FUNCTION core.agent_name(text) OWNER TO veripsa_migrator;

-- collision_surface: the hero metric, both sides of traffic control.
--   held    (事故回避) = collisions the lock held at the door (collision_held events).
--   steered (誘導成功) = held collisions where the turned-away agent then claimed a DIFFERENT path
--                        (a reroute) — a FACT (it did claim other work), not a causation claim.
-- CROSS-AGENT ONLY: a 'collision_held' fires whenever a lane is already held by a DIFFERENT (agent,change)
-- pair — INCLUDING the SAME author opening a SECOND change on a path they already hold (the gate's holder
-- check only excludes same-agent-AND-same-change). That self-collision is the doer serializing THEIR OWN
-- two PRs — NOT a prevented CROSS-agent clobber, which is the value Veripsa sells ("Cross-agent, pre-merge"
-- — README). Counting it inflates the hero metric AND contradicts the paired collisions_on_main, which
-- already excludes same-author (e2.agent_id <> e.agent_id). So 'held'/'held_7d'/'steered' count only true
-- cross-agent holds. `counterparty_agent IS DISTINCT FROM agent_id` keeps a NULL-holder row (never drops a
-- legitimate cross-agent fact) while excluding the agent_id=counterparty_agent self rows.
CREATE OR REPLACE FUNCTION core.collision_surface() RETURNS jsonb
    LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path TO 'core','pg_catalog' AS $$
DECLARE v_account text; v_result jsonb;
BEGIN
  SELECT account INTO v_account FROM core.resolve_session_identity() AS r(agent,account);
  PERFORM set_config('core.current_account', v_account, true);
  SELECT jsonb_build_object(
    'held',    (SELECT count(*)::int FROM core.event WHERE account_id=v_account AND kind='collision_held' AND counterparty_agent IS DISTINCT FROM agent_id),
    'held_7d', (SELECT count(*)::int FROM core.event WHERE account_id=v_account AND kind='collision_held' AND counterparty_agent IS DISTINCT FROM agent_id AND occurred_at > now()-interval '7 days'),
    'steered', (SELECT count(*)::int FROM core.event e WHERE e.account_id=v_account AND e.kind='collision_held' AND e.counterparty_agent IS DISTINCT FROM e.agent_id
                  AND EXISTS (SELECT 1 FROM core.claim c WHERE c.account_id=v_account AND c.agent_id=e.agent_id
                               AND c.target_path<>e.path AND c.claimed_at >= e.occurred_at)),
    'recent', COALESCE((SELECT jsonb_agg(jsonb_build_object(
        'blocked', core.agent_name(e.agent_id), 'holder', core.agent_name(e.counterparty_agent),
        'path', e.path, 'repo', e.repo, 'branch', e.branch, 'at', e.occurred_at) ORDER BY e.occurred_at DESC)
      FROM (SELECT * FROM core.event WHERE account_id=v_account AND kind='collision_held' AND counterparty_agent IS DISTINCT FROM agent_id
             ORDER BY occurred_at DESC LIMIT 20) e), '[]'::jsonb)
  ) INTO v_result;
  RETURN v_result;
END $$;
ALTER FUNCTION core.collision_surface() OWNER TO veripsa_migrator;

-- board_surface: THE LIVE BOARD. Who is active (the fleet) + the files each holds (their lane) + a fleet
-- summary. Sweeps crashed claims first (a write) so a dead session is never shown live — so NOT STABLE.
CREATE OR REPLACE FUNCTION core.board_surface() RETURNS jsonb
    LANGUAGE plpgsql SECURITY DEFINER SET search_path TO 'core','pg_catalog' AS $$
DECLARE v_account text; v_result jsonb;
BEGIN
  SELECT account INTO v_account FROM core.resolve_session_identity() AS r(agent,account);
  PERFORM set_config('core.current_account', v_account, true);
  PERFORM core.expire_stale_claims();
  SELECT jsonb_build_object(
    'summary', jsonb_build_object(
      'agents',       (SELECT count(DISTINCT agent_id)::int FROM core.claim WHERE account_id=v_account AND claim_state='active'),
      'active_claims',(SELECT count(*)::int FROM core.claim WHERE account_id=v_account AND claim_state='active'),
      'waiting',      (SELECT count(*)::int FROM core.claim WHERE account_id=v_account AND claim_state='waiting'),
      'pushes',       (SELECT count(*)::int FROM core.event WHERE account_id=v_account AND kind='push')
    ),
    'fleet', COALESCE((SELECT jsonb_agg(a.obj ORDER BY a.disp) FROM (
        SELECT core.agent_name(c.agent_id) AS disp,
               jsonb_build_object('agent', core.agent_name(c.agent_id), 'agent_id', c.agent_id,
                 'live', bool_or(c.heartbeat_at > now()-interval '10 minutes'),
                 'last_seen', max(c.heartbeat_at),
                 'holds', jsonb_agg(jsonb_build_object('path', c.target_path, 'repo', c.repo, 'branch', c.branch)
                                    ORDER BY c.target_path)) AS obj
        FROM core.claim c WHERE c.account_id=v_account AND c.claim_state='active'
        GROUP BY c.agent_id) a), '[]'::jsonb),
    -- the LINES: lanes with cars waiting (the on-ramp queue = the exit congestion, made honest).
    'queues', COALESCE((SELECT jsonb_agg(jsonb_build_object(
        'path', q.target_path, 'repo', q.repo, 'branch', q.branch, 'depth', q.depth,
        'holder', core.agent_name((SELECT h.agent_id FROM core.claim h WHERE h.account_id=v_account
                    AND h.repo=q.repo AND h.branch=q.branch AND h.target_path=q.target_path AND h.claim_state='active' LIMIT 1)),
        'waiting', q.waiters) ORDER BY q.depth DESC, q.target_path)
      FROM (SELECT repo, branch, target_path, count(*)::int AS depth,
                   jsonb_agg(core.agent_name(agent_id) ORDER BY claimed_at) AS waiters
            FROM core.claim WHERE account_id=v_account AND claim_state='waiting'
            GROUP BY repo, branch, target_path) q), '[]'::jsonb)
  ) INTO v_result;
  RETURN v_result;
END $$;
ALTER FUNCTION core.board_surface() OWNER TO veripsa_migrator;

-- record_warn_with_authority: persist a PREDICTION — Veripsa warned a change that `p_path` is structurally
-- exposed to another in-flight change (a steer, not a block). Persisting predictions is what makes EFFECT
-- auditable (you can later ask: did the warned coupling become real rework?). 'warn_issued' event KIND
-- (a new VALUE in the no-乱立 ledger, not a new table). Content-free (a path + a short label). Append-only.
CREATE OR REPLACE FUNCTION core.record_warn_with_authority(p_path text, p_repo text DEFAULT '', p_branch text DEFAULT '', p_detail text DEFAULT NULL)
    RETURNS text LANGUAGE plpgsql SECURITY DEFINER SET search_path TO 'core','pg_catalog' AS $$
DECLARE v_agent text; v_account text; v_id text;
BEGIN
  IF p_path IS NULL OR btrim(p_path)='' OR length(p_path)>1024 THEN RETURN NULL; END IF;
  SELECT agent, account INTO v_agent, v_account FROM core.establish_session_write_context() AS c(agent,account);
  -- THE LEDGER WALL (#95 follow-up): over the events cap → record nothing. warn_issued is an append-only event
  -- row; an over-quota account opening fresh PRs would otherwise keep growing core.event past the cap. Advisory.
  IF core._ledger_write_blocked(v_account) THEN RETURN NULL; END IF;
  -- IDEMPOTENT id: a warn is uniquely (account,repo,branch,path,change) — detail carries the change_id. So a
  -- redelivered PR-opened webhook records the same warn ONCE (no inflated warns_issued). Append-only-safe.
  v_id := 'EV-WARN-'||substr(md5(v_account||'|'||left(COALESCE(p_repo,''),512)||'|'||left(COALESCE(p_branch,''),512)||'|'||p_path||'|'||COALESCE(p_detail,'')),1,24);
  PERFORM core.mark_governed_write('event');
  INSERT INTO core.event(event_id, account_id, kind, agent_id, repo, branch, path, detail)
  VALUES (v_id, v_account, 'warn_issued', v_agent, left(COALESCE(p_repo,''),512), left(COALESCE(p_branch,''),512), p_path, left(p_detail,200))
  ON CONFLICT (account_id, event_id) DO NOTHING;
  RETURN v_id;
END $$;
ALTER FUNCTION core.record_warn_with_authority(text,text,text,text) OWNER TO veripsa_migrator;

-- record_check_published_with_authority: a Veripsa Check Run was actually PUBLISHED on a PR head — A4 of the
-- owner activation funnel (Issue #648), the transition "PR observed (A3) -> first Check Run posted". The Checks
-- API result is otherwise only LOGGED (webhook_posters._check_result_from_response -> _log_pr_surface), never
-- persisted, so "did this install ever get a Check on a PR" is not DB-derivable. This records that ONE content-
-- free fact as the 'check_published' event KIND (a new VALUE in the no-乱立 ledger, NOT a new table). NOTIFY/
-- REPORT-ONLY: it NEVER blocks the PR and is called ONLY after a SUCCESSFUL post (posted=True) -- a failed/
-- skipped post writes NOTHING (a failure is never recorded as a success). Content-free: repo + branch + the PR
-- change ref (PR-<n>, an opaque id — NOT code) in `path` (exactly as 'pr_failing' carries its ref there), the
-- head commit_sha, and a SHORT signal TOKEN from a bounded allow-list in `detail`
-- ('clear'|'heads_up'|'wait_in_line'|'unknown'|'paused'). NEVER a title/body/summary/log/diff.
--   * p_change_id : the PR/lane bundle id ('PR-<n>'), the same content-free change ref the lock uses; stored in
--                   `path` (the event's per-change ref slot, exactly as 'pr_failing'/'landed' carry a content-
--                    free ref there — it is NOT a filesystem path here, so no code leaks).
--   * p_signal    : normalized to the allow-list; anything else collapses to 'unknown' (so a future caller can
--                   pass a new verdict string without ever leaking free text into the ledger).
-- IDEMPOTENT id keyed by (account,repo,head_sha): GitHub redelivers check webhooks at-least-once and the acting
-- + neighbor-refresh + rerun paths can all post on the SAME head — a deterministic id + ON CONFLICT DO NOTHING
-- records the first-check fact ONCE per head (the surface takes the EARLIEST such event per repo). A NEW head sha
-- => a new id => a new row. Append-only. SECURITY DEFINER; App-delegation-only, structural sibling of
-- record_pr_failing_with_authority (the App observes the Checks post server-side; a buyer writer must not forge a
-- 'a Check was published' fact). Identity from the connection role (account pinned by enter_installation).
CREATE OR REPLACE FUNCTION core.record_check_published_with_authority(
    p_change_id text, p_repo text, p_branch text, p_commit_sha text, p_signal text DEFAULT 'unknown')
    RETURNS text LANGUAGE plpgsql SECURITY DEFINER SET search_path TO 'core','pg_catalog' AS $$
DECLARE v_agent text; v_account text; v_change text; v_repo text; v_branch text; v_sha text; v_signal text; v_id text;
BEGIN
  v_change := left(NULLIF(btrim(COALESCE(p_change_id,'')),''),200);
  IF v_change IS NULL THEN RAISE EXCEPTION 'record_check_published needs a change_id (the PR ref)' USING ERRCODE='23514'; END IF;
  v_repo := left(COALESCE(p_repo,''),512); v_branch := left(COALESCE(p_branch,''),512);
  -- head sha is REQUIRED (it is the idempotency key component) and must be hex (the event CHECK enforces it too).
  v_sha := NULLIF(btrim(COALESCE(p_commit_sha,'')),'');
  IF v_sha IS NULL OR length(v_sha)>64 OR v_sha !~ '^[0-9a-fA-F]+$' THEN
    RAISE EXCEPTION 'record_check_published needs a hex head sha' USING ERRCODE='23514'; END IF;
  -- BOUNDED signal: only known content-free tokens; anything else → 'unknown' (never free text in the ledger).
  v_signal := lower(btrim(COALESCE(p_signal,'')));
  IF v_signal NOT IN ('clear','heads_up','wait_in_line','unknown','paused') THEN v_signal := 'unknown'; END IF;
  SELECT agent, account INTO v_agent, v_account FROM core.establish_session_write_context() AS c(agent,account);
  -- THE LEDGER WALL: over the events cap → record nothing (append-only event ledger growth past the #95 wall).
  IF core._ledger_write_blocked(v_account) THEN RETURN NULL; END IF;
  v_id := 'EV-CHKPUB-'||substr(md5(v_account||'|'||v_repo||'|'||v_sha),1,22);
  PERFORM core.mark_governed_write('event');
  INSERT INTO core.event(event_id, account_id, kind, agent_id, repo, branch, path, commit_sha, detail)
  VALUES (v_id, v_account, 'check_published', v_agent, v_repo, v_branch, v_change, v_sha, v_signal)
  ON CONFLICT (account_id, event_id) DO NOTHING;
  RETURN v_id;
END $$;
ALTER FUNCTION core.record_check_published_with_authority(text,text,text,text,text) OWNER TO veripsa_migrator;
-- App-delegation-only (the App observes the Checks post server-side); a buyer writer must not forge a
-- 'a Veripsa Check was published' activation fact. Mirror the record_pr_failing_with_authority wall.
REVOKE EXECUTE ON FUNCTION core.record_check_published_with_authority(text,text,text,text,text) FROM PUBLIC, veripsa_writer;
GRANT  EXECUTE ON FUNCTION core.record_check_published_with_authority(text,text,text,text,text) TO veripsa_app;

-- effect_surface — IS IT WORKING? (PO 2026-06-17: 動くのは分かった、効果は?). The product's OWN effect ledger:
-- both halves of the value, from FACTS it recorded (never a causation claim). 'It detects' is proven live by
-- the other surfaces; THIS answers 'is it having effect' the only honest way a running system can — by tallying
-- what it actually prevented + predicted:
--   prevented_clobbers  = same-file races the lock HELD (a clobber that did NOT happen) — the serialize half.
--   warns_issued        = structural A→B exposures flagged BEFORE merge (predictions on record) — the warn half.
--   steered             = a held agent then claimed DIFFERENT work afterwards (a reroute fact, not causation).
--   landings            = changes that reached main through the gate (push events recorded).
--   collisions_occurred = REAL collisions that HAPPENED on main (same path, different authors, landed within
--                         14d). This is the flip side of prevented_clobbers: the lock prevented SOME, but
--                         direct pushes bypass the claim gate — these are what slipped through. The UNIFIED
--                         LANDING MODEL (record_landing_with_authority on both PR-merge and direct-push) feeds
--                         this. An honest signal: prevented_clobbers shows Veripsa working; collisions_occurred
--                         shows what is still happening without the gate or via direct push.
-- The realized-rate (did a warned coupling become real downstream rework) needs accrued post-deploy history;
-- the pre-deploy ESTIMATE of that is the co-change backtest (tests/backtest_cochange.py) on the repo's own
-- git history — proven on a real repo to track real coupling ~2x chance. This surface stays honest-empty
-- until the gate actually prevents/predicts something. Content-free; tenant-pinned.
-- _effect_for_account — the effect ledger COMPUTED FOR AN EXPLICIT account (the WHO is the parameter, never the
-- session). This is the shared body BOTH effect_surface() (after it resolves the SESSION account) and
-- effect_for_installation() (after it resolves the INSTALLATION's account) call, so the two surfaces can never
-- drift. It does the ONE thing a body that is reused across two identity sources must do differently from the
-- old inline: it NEVER calls a sibling surface that re-derives identity. The old inline computed
-- collisions_occurred via core.collisions_on_main('','','14 days') — but that fn re-runs
-- resolve_session_identity() and re-pins core.current_account from the SESSION, which would (a) ignore the
-- account we were handed and (b) 42501 for a credential-less reader role. So the collisions_count is INLINED
-- here, byte-identical to collisions_on_main's `collisions_count` for (repo='',branch='',window='14 days'):
-- distinct 'landed' paths another author also landed within 14d. Every other tally already reads core.event /
-- core.claim filtered by the pinned account, so they are unchanged. The gate proves effect_surface()'s output
-- is byte-identical before/after this extraction.
--
-- INTERNAL helper: SECURITY DEFINER (runs as the migrator owner, so the FORCE-RLS reads see rows for the pinned
-- account), STABLE, pinned search_path, and — like _policy_int / _policy_text — its PUBLIC EXECUTE default is
-- STRIPPED below so no buyer/seat role can pass an arbitrary p_account and read a cross-tenant ledger directly.
-- It is reachable ONLY from the two SECURITY DEFINER surfaces, which resolve the account from a TRUSTED source
-- (session identity / the routing table) before calling it — never from a caller-supplied account.
CREATE OR REPLACE FUNCTION core._effect_for_account(p_account text) RETURNS jsonb
    LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path TO 'core','pg_catalog' AS $$
DECLARE v_account text := p_account; v_result jsonb;
BEGIN
  IF v_account IS NULL OR v_account = '' THEN RETURN NULL; END IF;
  PERFORM set_config('core.current_account', v_account, true);
  -- prevented_clobbers counts only TRUE CROSS-AGENT holds (counterparty_agent IS DISTINCT FROM agent_id) — a
  -- same-author self-collision (one doer opening two changes on a path they already hold) is them serializing
  -- THEIR OWN work, NOT a prevented cross-agent clobber (the value Veripsa sells). Counting it inflates the
  -- effect AND contradicts the paired collisions_occurred (which already excludes same-author).
  -- IS DISTINCT FROM keeps a NULL-holder row (never drops a legitimate fact). Same guard on the steered subset
  -- and the collision_held half of interventions, so every collision tally agrees on the cross-agent denominator.
  SELECT jsonb_build_object(
    'prevented_clobbers',    (SELECT count(*)::int FROM core.event WHERE account_id=v_account AND kind='collision_held' AND counterparty_agent IS DISTINCT FROM agent_id),
    'prevented_clobbers_7d', (SELECT count(*)::int FROM core.event WHERE account_id=v_account AND kind='collision_held' AND counterparty_agent IS DISTINCT FROM agent_id AND occurred_at > now()-interval '7 days'),
    'warns_issued',          (SELECT count(*)::int FROM core.event WHERE account_id=v_account AND kind='warn_issued'),
    'warns_issued_7d',       (SELECT count(*)::int FROM core.event WHERE account_id=v_account AND kind='warn_issued' AND occurred_at > now()-interval '7 days'),
    'steered',               (SELECT count(*)::int FROM core.event e WHERE e.account_id=v_account AND e.kind='collision_held' AND e.counterparty_agent IS DISTINCT FROM e.agent_id
                                AND EXISTS (SELECT 1 FROM core.claim c WHERE c.account_id=v_account AND c.agent_id=e.agent_id
                                             AND c.target_path<>e.path AND c.claimed_at >= e.occurred_at)),
    'landings',              (SELECT count(*)::int FROM core.event WHERE account_id=v_account AND kind='push'),
    'interventions',         (SELECT count(*)::int FROM core.event WHERE account_id=v_account AND ((kind='collision_held' AND counterparty_agent IS DISTINCT FROM agent_id) OR kind='warn_issued')),
    -- collisions_occurred: real same-path collisions on main (from the unified 'landed' ledger). Unlike
    -- prevented_clobbers (the lock stopped them BEFORE), these already landed — the ground truth of what
    -- actually slipped through. Counts distinct paths that two different authors both landed on within 14d.
    -- INLINED (not core.collisions_on_main) so this body never re-derives identity — byte-identical to that
    -- fn's collisions_count for repo='',branch='',window='14 days' (the exact args effect_surface passed).
    'collisions_occurred',   (SELECT count(DISTINCT e.path)::int FROM core.event e
                                WHERE e.account_id=v_account AND e.kind='landed'
                                  AND e.occurred_at > now()-interval '14 days'
                                  AND EXISTS (SELECT 1 FROM core.event e2
                                               WHERE e2.account_id=v_account AND e2.kind='landed' AND e2.path=e.path
                                                 AND e2.agent_id <> e.agent_id
                                                 AND e2.occurred_at > now()-interval '14 days')),
    'recent', COALESCE((SELECT jsonb_agg(jsonb_build_object(
        'kind', CASE e.kind WHEN 'collision_held' THEN 'serialized' WHEN 'warn_issued' THEN 'warned' ELSE e.kind END,
        'by', core.agent_name(e.agent_id), 'path', e.path, 'repo', e.repo, 'branch', e.branch, 'at', e.occurred_at,
        -- the PR this catch was about (event.detail carries the change_id 'PR-<n>'; null for a pre-attribution
        -- hold), so the dashboard's "Lately" feed can link each row straight to its GitHub PR. content-free.
        'change', NULLIF(left(COALESCE(e.detail,''),64),'')) ORDER BY e.occurred_at DESC)
      FROM (SELECT * FROM core.event WHERE account_id=v_account
              AND (kind='warn_issued' OR (kind='collision_held' AND counterparty_agent IS DISTINCT FROM agent_id))  -- self-collisions are not cross-agent prevented clobbers (see prevented_clobbers above)
             ORDER BY occurred_at DESC LIMIT 20) e), '[]'::jsonb)
  ) INTO v_result;
  RETURN v_result;
END $$;
ALTER FUNCTION core._effect_for_account(text) OWNER TO veripsa_migrator;
-- strip the PUBLIC EXECUTE default: an internal helper that takes an explicit account must NOT be callable by a
-- buyer/seat role (which could pass a victim account). Reached only from the two SECURITY DEFINER surfaces below.
REVOKE EXECUTE ON FUNCTION core._effect_for_account(text) FROM PUBLIC;

-- effect_surface — IS IT WORKING? (PO 2026-06-17: 動くのは分かった、効果は?), for the SESSION's tenant. Resolves the
-- session identity → pins the account → returns the shared _effect_for_account body. Behavior/signature/output
-- are UNCHANGED (the gate asserts byte-identical jsonb vs the pre-extraction inline); the body just moved into
-- the shared helper so effect_for_installation cannot drift from it.
CREATE OR REPLACE FUNCTION core.effect_surface() RETURNS jsonb
    LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path TO 'core','pg_catalog' AS $$
DECLARE v_account text;
BEGIN
  SELECT account INTO v_account FROM core.resolve_session_identity() AS r(agent,account);
  PERFORM set_config('core.current_account', v_account, true);
  RETURN core._effect_for_account(v_account);
END $$;
ALTER FUNCTION core.effect_surface() OWNER TO veripsa_migrator;
REVOKE EXECUTE ON FUNCTION core.record_warn_with_authority(text,text,text,text) FROM PUBLIC;  -- strip the PUBLIC default; only the granted buyer writer below
GRANT EXECUTE ON FUNCTION core.record_warn_with_authority(text,text,text,text) TO veripsa_writer;
-- LEAST-PRIVILEGE (audit iter-5 P3): strip the PUBLIC-EXECUTE default so a non-tenant role (billing/platform-
-- reader) can't reach this tenant read surface; the GRANT names exactly the tenant roles (App inherits writer).
REVOKE EXECUTE ON FUNCTION core.effect_surface() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION core.effect_surface() TO veripsa_reader, veripsa_writer, veripsa_demo_steward;

-- ── PLATFORM read path: a SEPARATE service (the example platform / dashboard) reads a SPECIFIC installation's
--    EFFECT, read-only + content-free + least-privilege. effect_surface() above resolves the account from the
--    SESSION identity, which only works for the App role (or a credentialed seat) — a dashboard reader connecting
--    as its own role cannot pin an ARBITRARY tenant. These two functions give it the minimum it needs:
--    (1) enumerate the owner's installation ids, (2) read ONE installation's content-free effect — and NOTHING
--    else (no code graph, no file contents, no write). They are the ONLY surface the platform-reader role is
--    granted, so the moat (graph / contents / writes stay unreachable) holds by construction.

-- effect_for_installation — the SAME content-free jsonb as effect_surface(), but for the account behind an
-- explicit INSTALLATION id (the dashboard's selector), resolved from the no-RLS routing table — NOT from the
-- session. SECURITY DEFINER (so it can read core.installation_account, which carries no table grants), STABLE,
-- pinned search_path. Unknown/unmapped installation → NULL (an unknown id reveals nothing). It pins
-- core.current_account to the resolved account and returns _effect_for_account, so a platform reader sees ONLY
-- effect_surface's content-free output for that tenant — never any graph/contents/write path.
CREATE OR REPLACE FUNCTION core.effect_for_installation(p_installation_id text) RETURNS jsonb
    LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path TO 'core','pg_catalog' AS $$
DECLARE v_inst text; v_account text;
BEGIN
  v_inst := left(NULLIF(btrim(COALESCE(p_installation_id,'')),''),64);  -- bound + normalize, like enter_installation_with_authority
  IF v_inst IS NULL THEN RETURN NULL; END IF;
  SELECT account_id INTO v_account FROM core.installation_account WHERE installation_id = v_inst AND revoked_at IS NULL;
  IF v_account IS NULL THEN RETURN NULL; END IF;                        -- unmapped/revoked installation → reveal nothing
  PERFORM set_config('core.current_account', v_account, true);
  RETURN core._effect_for_account(v_account);
END $$;
ALTER FUNCTION core.effect_for_installation(text) OWNER TO veripsa_migrator;
-- strip the PUBLIC default, then grant ONLY to the platform reader (and the App, which already crosses tenants).
-- A buyer/seat role can NOT call it (it would otherwise let one tenant read another by id).
REVOKE EXECUTE ON FUNCTION core.effect_for_installation(text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION core.effect_for_installation(text) TO example_platform_reader, veripsa_app;

-- list_installation_ids — the content-free enumerator the platform reader needs to know WHICH installations
-- exist, returning installation_ids ONLY (no account ids, no counts, no anything else). Given to the reader
-- INSTEAD of a raw SELECT on core.installation_account (which would also expose the account mapping). SECURITY
-- DEFINER (the routing table has no grants), STABLE.
--
-- SINGLE-TENANT TODAY: there is one owner/platform, so this returns ALL installation ids. MULTI-TENANT FOLLOW-UP:
-- when the platform serves more than the owner, scope which installations a given reader may enumerate (e.g. by a
-- reader→owner mapping) — do NOT hand a multi-customer platform the unscoped list.
--
-- LIVENESS FILTER (commercial-completeness — the no-billing-without-a-LIVE-link invariant): enumerate only the LIVE
-- installations (revoked_at IS NULL, 30_gate.sql). The uninstall + suspend handlers KEEP the routing row but stamp
-- revoked_at; without this filter a bare SELECT reported an uninstalled/suspended install as if it were still live,
-- which is the exact read the platform's billing-liveness gates and graph freshness depend on (a dead install would
-- look billable + would be kept warm). A reinstall/unsuspend clears revoked_at and the install returns to this list.
CREATE OR REPLACE FUNCTION core.list_installation_ids() RETURNS SETOF text
    LANGUAGE sql STABLE SECURITY DEFINER SET search_path TO 'core','pg_catalog' AS $$
  SELECT installation_id FROM core.installation_account WHERE revoked_at IS NULL ORDER BY installation_id
$$;
ALTER FUNCTION core.list_installation_ids() OWNER TO veripsa_migrator;
REVOKE EXECUTE ON FUNCTION core.list_installation_ids() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION core.list_installation_ids() TO example_platform_reader, veripsa_app;

-- list_installation_accounts — owner-admin ACCOUNT roster for the separate platform. Same security posture as
-- list_installation_ids(), but returns the account id that owns each known installation so the admin screen can be
-- account-centered instead of showing opaque "Installation <id>" rows. Content-free: GitHub numeric account id,
-- public GitHub account login/type, GitHub App installation id, and a live boolean only; no repo names, no graph,
-- no customer data. Unlike list_installation_ids(), this owner cockpit surface intentionally includes non-live
-- rows with live=false so the operator can see uninstall/suspend churn without counting it as active coverage.
DROP FUNCTION IF EXISTS core.list_installation_accounts();
CREATE OR REPLACE FUNCTION core.list_installation_accounts()
RETURNS TABLE(installation_id text, account_id text, account_login text, account_type text, live boolean)
    LANGUAGE sql STABLE SECURITY DEFINER SET search_path TO 'core','pg_catalog' AS $$
  SELECT installation_id, account_id, account_login, account_type, (revoked_at IS NULL) AS live
    FROM core.installation_account
   ORDER BY account_id, (revoked_at IS NOT NULL), installation_id
$$;
ALTER FUNCTION core.list_installation_accounts() OWNER TO veripsa_migrator;
REVOKE EXECUTE ON FUNCTION core.list_installation_accounts() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION core.list_installation_accounts() TO example_platform_reader, veripsa_app;

-- the platform reader also needs USAGE on the schema to call anything in it (granted once here; no table grants,
-- no write grants — the functions above are its entire reachable surface).
GRANT USAGE ON SCHEMA core TO example_platform_reader;

-- _repository_generation_boundary: one same-name repository can be deleted and recreated with PR numbers and
-- paths reused. Retained audit is intentionally append-only, so every current-repository read needs one shared
-- lower bound. A superseded tombstone records when a predecessor stopped owning the coordinate; an authenticated
-- lifecycle activation for a DIFFERENT stable id can establish a later replacement boundary (for example when work
-- observed the replacement before its add webhook was processed). Re-selecting the same stable object continues its
-- retained audit, and work-only activation is routing evidence rather than generation authority.
CREATE OR REPLACE FUNCTION core._repository_generation_boundary(p_repo text) RETURNS timestamptz
    LANGUAGE sql STABLE SECURITY DEFINER SET search_path TO 'core','pg_catalog' AS $$
  SELECT COALESCE(max(b.boundary), '-infinity'::timestamptz)
    FROM (
      SELECT max(t.superseded_at) AS boundary
        FROM core.repository_lifecycle_tombstone t
       WHERE t.account_id=current_setting('core.current_account', true) AND t.repo=p_repo
      UNION ALL
      SELECT max(a.generation_started_at) AS boundary
        FROM core.repository_lifecycle_activation a
       WHERE a.account_id=current_setting('core.current_account', true) AND a.repo=p_repo
         AND a.lifecycle_authoritative
         AND EXISTS (
           SELECT 1 FROM core.repository_lifecycle_tombstone t
            WHERE t.account_id=a.account_id AND t.repo=a.repo AND t.superseded_at IS NOT NULL
              AND t.repository_id<>a.repository_id)
    ) b
$$;
ALTER FUNCTION core._repository_generation_boundary(text) OWNER TO veripsa_migrator;
REVOKE EXECUTE ON FUNCTION core._repository_generation_boundary(text) FROM PUBLIC;

-- ── PLATFORM repo-level SUBSTANCE (content-free): the dashboard's Repository screen shows the MEANINGFUL things
--    Core actually does — not counts. Three real signals, all scoped to ONE installation × ONE repo, all content-
--    free (file PATHS + behavioural COUNTS + co-change strength — the same insights Core already posts on PRs;
--    NEVER the code graph itself, no node/edge counts, no coverage %). HONEST BY CONSTRUCTION: each section is
--    only as full as the real data — empty arrays where Core hasn't caught/learned anything yet (the customer's
--    repos are mostly empty today; we do NOT pad).

-- repos_for_installation — the repos an installation has REAL activity on, newest-touched first, with a
-- content-free last-activity timestamp (so the repo list can say "active 2d ago", not a bare name). Distinct
-- repos seen in the event ledger. SECURITY DEFINER + routed-account pin (no session dependency). Unmapped → '[]'.
CREATE OR REPLACE FUNCTION core.repos_for_installation(p_installation_id text) RETURNS jsonb
    LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path TO 'core','pg_catalog' AS $$
DECLARE v_inst text; v_account text; v_result jsonb;
BEGIN
  v_inst := left(NULLIF(btrim(COALESCE(p_installation_id,'')),''),64);
  IF v_inst IS NULL THEN RETURN '[]'::jsonb; END IF;
  SELECT account_id INTO v_account FROM core.installation_account WHERE installation_id = v_inst AND revoked_at IS NULL;
  IF v_account IS NULL THEN RETURN '[]'::jsonb; END IF;
  PERFORM set_config('core.current_account', v_account, true);
  SELECT COALESCE(jsonb_agg(jsonb_build_object('repo', repo, 'last_at', last_at) ORDER BY last_at DESC), '[]'::jsonb)
    INTO v_result
    FROM (SELECT e.repo, max(e.occurred_at) AS last_at FROM core.event e
           WHERE e.account_id = v_account AND e.repo <> ''
             AND NOT EXISTS (
               SELECT 1 FROM core.repository_lifecycle_tombstone t
                WHERE t.account_id=v_account AND t.repo=e.repo AND t.superseded_at IS NULL)
             AND e.occurred_at >= core._repository_generation_boundary(e.repo)
           GROUP BY e.repo) r;
  RETURN v_result;
END $$;
ALTER FUNCTION core.repos_for_installation(text) OWNER TO veripsa_migrator;
REVOKE EXECUTE ON FUNCTION core.repos_for_installation(text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION core.repos_for_installation(text) TO example_platform_reader, veripsa_app;

-- repo_insights_for_installation — the Repository screen's substance for ONE installation × ONE repo:
--   • recent     — what Core CAUGHT here lately: cross-agent collisions it serialized + overlaps it warned
--                  (same-author self-serialization is NOT a "catch" — see effect_surface; excluded so the feed
--                  is honest, not inflated). {kind serialized|warned, path, branch, at}. Newest first, ≤12.
--   • hotspots   — WHERE code collides: paths most often coordinated here (collision_held OR warn_issued),
--                  {path, count}. This is the contention signal ("server.py 70×") — counts ALL coordination on
--                  the path (a file repeatedly in-flight IS a hotspot regardless of who), ≤8 by count desc.
--   • couplings  — WHAT changes together, at the CUSTOMER PRECISION FLOOR (co>=5 AND lift>=2, identical to
--                  co_change_partners_with_authority's render floor) so sub-floor noise never surfaces — empty
--                  when nothing real has formed yet. {a, b, strength, lift, co}, ≤8 by lift desc.
-- SECURITY DEFINER + routed-account pin; unmapped installation → NULL. Content-free; bounded.
DROP FUNCTION IF EXISTS core.repo_insights_for_installation(text, text);       -- superseded by the windowed 3-arg
DROP FUNCTION IF EXISTS core.repo_insights_for_installation(text, text, int);  -- superseded: window unit days → hours (a param rename needs the drop)
CREATE OR REPLACE FUNCTION core.repo_insights_for_installation(p_installation_id text, p_repo text, p_window_hours int DEFAULT 168) RETURNS jsonb
    LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path TO 'core','pg_catalog' AS $$
DECLARE v_inst text; v_account text; v_repo text; v_hours int; v_win interval;
        v_active_after timestamptz; v_result jsonb;
BEGIN
  v_inst := left(NULLIF(btrim(COALESCE(p_installation_id,'')),''),64);
  IF v_inst IS NULL THEN RETURN NULL; END IF;
  SELECT account_id INTO v_account FROM core.installation_account WHERE installation_id = v_inst AND revoked_at IS NULL;
  IF v_account IS NULL THEN RETURN NULL; END IF;
  PERFORM set_config('core.current_account', v_account, true);
  v_repo := left(COALESCE(p_repo, ''), 512);
  IF v_repo = '' THEN RETURN NULL; END IF;
  IF EXISTS (SELECT 1 FROM core.repository_lifecycle_tombstone
              WHERE account_id=v_account AND repo=v_repo AND superseded_at IS NULL) THEN RETURN NULL; END IF;
  SELECT core._repository_generation_boundary(v_repo) INTO v_active_after;
  -- filterable window in HOURS (the fine→coarse range 1h/3h/6h/12h/24h/7d/30d/90d), bounded 1h..2160h (90d).
  v_hours := GREATEST(1, LEAST(COALESCE(p_window_hours, 168), 2160));
  v_win   := make_interval(hours => v_hours);
  SELECT jsonb_build_object(
    'repo', v_repo,
    'window_hours', v_hours,
    'recent', COALESCE((SELECT jsonb_agg(jsonb_build_object(
        'kind', CASE e.kind WHEN 'collision_held' THEN 'serialized' WHEN 'warn_issued' THEN 'warned' ELSE e.kind END,
        'path', e.path, 'branch', e.branch, 'at', e.occurred_at) ORDER BY e.occurred_at DESC)
      FROM (SELECT * FROM core.event WHERE account_id = v_account AND repo = v_repo
              AND occurred_at >= v_active_after
              AND (kind = 'warn_issued' OR (kind = 'collision_held' AND counterparty_agent IS DISTINCT FROM agent_id))
             ORDER BY occurred_at DESC LIMIT 12) e), '[]'::jsonb),
    -- WINDOWED + trend (a cumulative count only grows — it can't show a file improving). recent = within the
    -- chosen window, prior = the window before it (the trend), total = all-time (context). Only files contended
    -- IN-window surface (HAVING recent>0), ranked by recent — a file that quieted drops OFF (= how improvement shows).
    'hotspots', COALESCE((SELECT jsonb_agg(jsonb_build_object('path', path, 'recent', recent, 'prior', prior, 'total', total, 'first_at', first_at, 'last_at', last_at) ORDER BY recent DESC, total DESC)
      FROM (SELECT path,
                   count(*) FILTER (WHERE occurred_at > now() - v_win)::int AS recent,
                   count(*) FILTER (WHERE occurred_at > now() - v_win * 2 AND occurred_at <= now() - v_win)::int AS prior,
                   count(*)::int AS total,
                   min(occurred_at) AS first_at, max(occurred_at) AS last_at
             FROM core.event
             WHERE account_id = v_account AND repo = v_repo AND occurred_at >= v_active_after
               AND kind IN ('warn_issued','collision_held') AND path <> ''
             GROUP BY path
            HAVING count(*) FILTER (WHERE occurred_at > now() - v_win) > 0
             ORDER BY recent DESC, total DESC, path LIMIT 8) h), '[]'::jsonb),
    'couplings', COALESCE((SELECT jsonb_agg(jsonb_build_object('a', path_a, 'b', path_b,
        'strength', round(strength::numeric, 2), 'lift', round(lift::numeric, 1), 'co', co) ORDER BY lift DESC, co DESC)
      FROM (SELECT * FROM core.co_change WHERE account_id = v_account AND repo = v_repo
              AND (v_active_after='-infinity'::timestamptz
                   OR generation_observed_at >= v_active_after)
              AND co >= 5 AND lift >= 2::real ORDER BY lift DESC, co DESC, path_a LIMIT 8) cc), '[]'::jsonb)
  ) INTO v_result;
  RETURN v_result;
END $$;
ALTER FUNCTION core.repo_insights_for_installation(text, text, int) OWNER TO veripsa_migrator;
REVOKE EXECUTE ON FUNCTION core.repo_insights_for_installation(text, text, int) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION core.repo_insights_for_installation(text, text, int) TO example_platform_reader, veripsa_app;

-- file_insights_for_installation — the FILE-detail screen for ONE installation × repo × path:
--   • events: the individual coordinations on this file, newest-first + bounded — each {at (the full instant),
--     kind (warned|serialized), change (the PR, 'PR-<n>', or null for a pre-attribution hold)}. The platform
--     GROUPS them by day, renders each `at` at the VIEWER's own local time, and links change → the actual PR.
--     total/first_at/last_at give the full span (burst vs chronic) beyond the bounded events window.
--   • co-change partners of THIS file (what moves with it) at the customer render floor (co>=5, lift>=2).
-- Deliberately NO "how to split": Core never reads code, so it cannot honestly suggest a decomposition — the
-- screen states the contention, never a fabricated direction. SECURITY DEFINER + routed-account pin; content-free.
CREATE OR REPLACE FUNCTION core.file_insights_for_installation(p_installation_id text, p_repo text, p_path text) RETURNS jsonb
    LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path TO 'core','pg_catalog' AS $$
DECLARE v_inst text; v_account text; v_repo text; v_path text;
        v_active_after timestamptz; v_result jsonb;
BEGIN
  v_inst := left(NULLIF(btrim(COALESCE(p_installation_id,'')),''),64);
  IF v_inst IS NULL THEN RETURN NULL; END IF;
  SELECT account_id INTO v_account FROM core.installation_account WHERE installation_id = v_inst AND revoked_at IS NULL;
  IF v_account IS NULL THEN RETURN NULL; END IF;
  PERFORM set_config('core.current_account', v_account, true);
  v_repo := left(COALESCE(p_repo, ''), 512);
  v_path := left(COALESCE(p_path, ''), 1024);
  IF v_repo = '' OR v_path = '' THEN RETURN NULL; END IF;
  IF EXISTS (SELECT 1 FROM core.repository_lifecycle_tombstone
              WHERE account_id=v_account AND repo=v_repo AND superseded_at IS NULL) THEN RETURN NULL; END IF;
  SELECT core._repository_generation_boundary(v_repo) INTO v_active_after;
  SELECT jsonb_build_object(
    'repo', v_repo, 'path', v_path,
    'total',    (SELECT count(*)::int FROM core.event WHERE account_id=v_account AND repo=v_repo AND path=v_path AND occurred_at >= v_active_after AND kind IN ('warn_issued','collision_held')),
    'first_at', (SELECT min(occurred_at) FROM core.event WHERE account_id=v_account AND repo=v_repo AND path=v_path AND occurred_at >= v_active_after AND kind IN ('warn_issued','collision_held')),
    'last_at',  (SELECT max(occurred_at) FROM core.event WHERE account_id=v_account AND repo=v_repo AND path=v_path AND occurred_at >= v_active_after AND kind IN ('warn_issued','collision_held')),
    'events', COALESCE((SELECT jsonb_agg(jsonb_build_object(
        'at', occurred_at,
        'kind', CASE kind WHEN 'collision_held' THEN 'serialized' WHEN 'warn_issued' THEN 'warned' ELSE kind END,
        'change', NULLIF(left(COALESCE(detail,''),64),'')) ORDER BY occurred_at DESC)
      FROM (SELECT occurred_at, kind, detail FROM core.event
              WHERE account_id=v_account AND repo=v_repo AND path=v_path AND kind IN ('warn_issued','collision_held')
                AND occurred_at >= v_active_after
              ORDER BY occurred_at DESC LIMIT 60) e), '[]'::jsonb),
    'partners', COALESCE((SELECT jsonb_agg(jsonb_build_object('partner', partner, 'strength', round(strength::numeric,2), 'lift', round(lift::numeric,1), 'co', co) ORDER BY lift DESC, co DESC)
      FROM (SELECT CASE WHEN path_a=v_path THEN path_b ELSE path_a END AS partner, strength, lift, co
              FROM core.co_change WHERE account_id=v_account AND repo=v_repo AND v_path IN (path_a, path_b)
                AND (v_active_after='-infinity'::timestamptz
                     OR generation_observed_at >= v_active_after)
                AND co >= 5 AND lift >= 2::real ORDER BY lift DESC, co DESC LIMIT 10) p), '[]'::jsonb)
  ) INTO v_result;
  RETURN v_result;
END $$;
ALTER FUNCTION core.file_insights_for_installation(text,text,text) OWNER TO veripsa_migrator;
REVOKE EXECUTE ON FUNCTION core.file_insights_for_installation(text,text,text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION core.file_insights_for_installation(text,text,text) TO example_platform_reader, veripsa_app;

-- now_for_installation — the "NOW / needs attention" landing read: the CURRENT in-flight contention, not history.
--   • waiting / active = how many changes are queued behind a collision right now vs currently held (counts).
--   • contended = files where >=2 DISTINCT in-flight CHANGES (PRs/branches) hold/await a claim right now — a live
--     collision this moment. Keyed on CHANGE, not author: the gate itself serializes a 2nd change on a held lane
--     regardless of who opened it (its holder check excludes only same-agent-AND-same-change), so ONE author's two
--     PRs on a file DO collide (one waits) — and AI fleets often commit under a single shared login, so an agent
--     check would hide exactly that. {repo, path, agents, changes_count, waiting, since, changes}. `changes` = the
--     DISTINCT PR ids ('PR-<n>') colliding on that path right now, so the screen links straight to the real PRs
--     (content-free — a PR number, never a body); `agents` = distinct authors (≥1; a cross-agent extra-signal).
--     Bounded, ranked by #changes then oldest. Can be EMPTY ("all clear"). Content-free. SECURITY DEFINER + routed pin.
CREATE OR REPLACE FUNCTION core.now_for_installation(p_installation_id text) RETURNS jsonb
    LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path TO 'core','pg_catalog' AS $$
DECLARE v_inst text; v_account text; v_result jsonb;
BEGIN
  v_inst := left(NULLIF(btrim(COALESCE(p_installation_id,'')),''),64);
  IF v_inst IS NULL THEN RETURN NULL; END IF;
  SELECT account_id INTO v_account FROM core.installation_account WHERE installation_id = v_inst AND revoked_at IS NULL;
  IF v_account IS NULL THEN RETURN NULL; END IF;
  PERFORM set_config('core.current_account', v_account, true);
  SELECT jsonb_build_object(
    -- in_flight = DISTINCT in-flight CHANGES (PRs / pushed branches), the honest "N changes in progress".
    -- Two corrections to the old frozen number: (1) count DISTINCT change_id, NOT claim ROWS — one PR
    -- touching many files is ONE change, not many (the row count inflated a few PRs into ~40); (2) FRESH
    -- only (lease_expires_at>now()) — a lapsed lease is an abandoned/leaked claim the activity-driven sweep
    -- (expire_stale_claims) hasn't reaped because the repo has had no events since, which froze the count.
    'in_flight', (SELECT count(DISTINCT change_id)::int FROM core.claim
                   WHERE account_id = v_account AND claim_state IN ('active','waiting')
                     AND lease_expires_at > now() AND change_id <> ''),
    'contended', COALESCE((SELECT jsonb_agg(jsonb_build_object(
        'repo', repo, 'path', target_path, 'agents', agents, 'changes_count', changes_count, 'waiting', waiting, 'since', since, 'changes', changes) ORDER BY changes_count DESC, since ASC)
      FROM (SELECT repo, target_path,
                   count(DISTINCT agent_id)::int AS agents,
                   count(DISTINCT change_id)::int AS changes_count,
                   count(*) FILTER (WHERE claim_state = 'waiting')::int AS waiting,
                   min(claimed_at) AS since,
                   COALESCE(jsonb_agg(DISTINCT change_id) FILTER (WHERE change_id <> ''), '[]'::jsonb) AS changes
              FROM core.claim
             WHERE account_id = v_account AND claim_state IN ('active','waiting') AND target_path <> ''
               AND lease_expires_at > now()   -- live lanes only — a lapsed lease is not a "collision right now"
             GROUP BY repo, target_path
            HAVING count(DISTINCT change_id) >= 2   -- ≥2 distinct in-flight CHANGES (what the gate serializes), author-agnostic
             ORDER BY count(DISTINCT change_id) DESC, min(claimed_at) ASC LIMIT 12) c), '[]'::jsonb)
  ) INTO v_result;
  RETURN v_result;
END $$;
ALTER FUNCTION core.now_for_installation(text) OWNER TO veripsa_migrator;
REVOKE EXECUTE ON FUNCTION core.now_for_installation(text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION core.now_for_installation(text) TO example_platform_reader, veripsa_app;

-- _policy_int: read a numeric tuning knob from the per-account `core.policy` table (the EXISTING owner-key
-- config store — no new config table), parse it, and CLAMP it into a bounded [lo,hi] frame, falling back to
-- a default when unset/garbage. This is the BOUNDED-knob pattern: an owner can tune a threshold via
-- set_policy_with_authority, but never out of a sane frame (a free-form config that lets the owner set
-- "0 minutes" or "10 years" would make a surface lie). NULL/non-numeric → default; out-of-range → clamped.
-- Account already pinned by the caller. STABLE; not granted to buyer roles (an internal helper for surfaces).
CREATE OR REPLACE FUNCTION core._policy_int(p_key text, p_default int, p_lo int, p_hi int) RETURNS int
    LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path TO 'core','pg_catalog' AS $$
DECLARE v_account text; v_raw text; v_val int;
BEGIN
  v_account := current_setting('core.current_account', true);
  IF v_account IS NULL OR v_account = '' THEN RETURN GREATEST(p_lo, LEAST(p_hi, p_default)); END IF;
  SELECT policy_value INTO v_raw FROM core.policy WHERE account_id=v_account AND policy_key=p_key;
  IF v_raw IS NULL OR btrim(v_raw) !~ '^-?[0-9]+$' THEN
    RETURN GREATEST(p_lo, LEAST(p_hi, p_default));
  END IF;
  v_val := btrim(v_raw)::int;
  RETURN GREATEST(p_lo, LEAST(p_hi, v_val));        -- clamp into the frame (never out of bounds)
END $$;
ALTER FUNCTION core._policy_int(text,int,int,int) OWNER TO veripsa_migrator;
-- INTERNAL-ONLY (cross-tenant policy-read fix): SECURITY DEFINER + reads core.policy WHERE account_id = the
-- caller-set core.current_account GUC (no re-pin — it's an internal helper, the surfaces that call it pin the
-- account first). Postgres grants EXECUTE to PUBLIC by DEFAULT; without this REVOKE any role with the PUBLIC
-- default (a future customer DB seat) could `SET core.current_account='VICTIM'` then call it to read another
-- tenant's tuning value. Callable ONLY from the SECURITY DEFINER surfaces (running as owner, account pinned).
REVOKE EXECUTE ON FUNCTION core._policy_int(text,int,int,int) FROM PUBLIC;

-- _policy_text: read a STRING tuning knob from the same per-account `core.policy` store, falling back to a
-- default when unset/blank, and LENGTH-CAP it (a config string can never blow a surface or a regexp). Sibling
-- of _policy_int for non-numeric knobs (e.g. a small list of basename globs). Account already pinned by the
-- caller; STABLE; internal helper (not granted to buyer roles). The value is treated as opaque text by the
-- caller — content-free (a path-pattern list, never file bytes).
CREATE OR REPLACE FUNCTION core._policy_text(p_key text, p_default text) RETURNS text
    LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path TO 'core','pg_catalog' AS $$
DECLARE v_account text; v_raw text;
BEGIN
  v_account := current_setting('core.current_account', true);
  IF v_account IS NULL OR v_account = '' THEN RETURN p_default; END IF;
  SELECT policy_value INTO v_raw FROM core.policy WHERE account_id=v_account AND policy_key=p_key;
  IF v_raw IS NULL OR btrim(v_raw) = '' THEN RETURN p_default; END IF;
  RETURN left(v_raw, 4096);   -- bounded: a config string is never unbounded input to a surface
END $$;
ALTER FUNCTION core._policy_text(text,text) OWNER TO veripsa_migrator;
-- INTERNAL-ONLY (cross-tenant policy-read fix): sibling of _policy_int — SECURITY DEFINER reading core.policy
-- by the caller-set core.current_account GUC. Strip the PUBLIC default so a non-owner seat cannot spoof
-- current_account=VICTIM and read another tenant's config string. Callable only from the owner-run surfaces.
REVOKE EXECUTE ON FUNCTION core._policy_text(text,text) FROM PUBLIC;

-- stalled_work_surface: NEGLECTED WORK — the handoff signal made ACTIVE. Veripsa's whole thesis is that work
-- must not rot when the doer vanishes; the gate already reserves lanes for in-flight work and EXPIRES stale
-- leases, but nobody WATCHES for the reserved-then-abandoned case. This surface LISTS in-flight work that has
-- stalled, so the console / notifications can say "these are being ignored — pick up, finish, or close". Two
-- reasons, both derived PURELY from the existing claim lifecycle (no new table; content-free — change_id /
-- coordinate / who held it / how long, NEVER code):
--   * 'abandoned'      — a claim whose lease LAPSED and the work never landed. expire_stale_claims() sweeps a
--                        lapsed active claim to state='expired'; a LANDING or WITHDRAW instead sets
--                        state='released'. So state='expired' is EXACTLY "the doer
--                        reserved a lane and disappeared without reaching main" — the core handoff signal. We
--                        also catch an active claim already past its lease but not yet swept (lease_expires_at
--                        < the threshold), so it surfaces even between sweeps. how_long = since the lease
--                        lapsed. Excludes a coordinate that already has a fresh ACTIVE claim (someone picked
--                        it back up) so a resumed lane isn't shown as neglected.
--   * 'starved-waiting'— a claim stuck in 'waiting' well past a bounded threshold: the waiter has been in line
--                        too long (the lane holder isn't moving / the auto-promote never fired for it). It is
--                        starving. how_long = since it got in line (claimed_at).
-- BOUNDED KNOBS (clamped via _policy_int, owner-tunable through set_policy, never out of frame):
--   stalled_abandoned_minutes : grace after a lease lapses before the work counts as abandoned. default 0
--                               (report as soon as the lease is gone), clamp 0..10080 (≤ 7 days).
--   stalled_waiting_minutes   : how long in line counts as starved. default 60, clamp 5..10080.
-- HONEST-EMPTY (no fabricated items). STABLE (read-only; it does NOT sweep — board_surface owns the sweep, so
-- this surface reports the swept-expired AND the not-yet-swept-but-lapsed without itself mutating state).
-- Tenant-pinned (account derived from the connection role, like every sibling surface).
CREATE OR REPLACE FUNCTION core.stalled_work_surface() RETURNS jsonb
    LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path TO 'core','pg_catalog' AS $$
DECLARE v_account text; v_aband_min int; v_wait_min int; v_aband_before timestamptz; v_result jsonb;
BEGIN
  SELECT account INTO v_account FROM core.resolve_session_identity() AS r(agent,account);
  PERFORM set_config('core.current_account', v_account, true);
  v_aband_min := core._policy_int('stalled_abandoned_minutes', 0, 0, 10080);
  v_wait_min  := core._policy_int('stalled_waiting_minutes', 60, 5, 10080);
  v_aband_before := now() - make_interval(mins => v_aband_min);
  SELECT jsonb_build_object(
    'thresholds', jsonb_build_object('abandoned_minutes', v_aband_min, 'waiting_minutes', v_wait_min),
    'abandoned_count', (SELECT count(*)::int FROM core.claim c WHERE c.account_id=v_account
        AND ( c.claim_state='expired'
              OR (c.claim_state='active' AND c.lease_expires_at < v_aband_before) )
        AND NOT EXISTS (SELECT 1 FROM core.claim a WHERE a.account_id=v_account AND a.repo=c.repo
                          AND a.branch=c.branch AND a.target_path=c.target_path AND a.claim_state='active'
                          AND a.lease_expires_at >= v_aband_before)),
    'starved_count', (SELECT count(*)::int FROM core.claim c WHERE c.account_id=v_account
        AND c.claim_state='waiting' AND c.claimed_at < now() - make_interval(mins => v_wait_min)),
    'items', COALESCE((
      SELECT jsonb_agg(x.obj ORDER BY x.stalled_seconds DESC) FROM (
        -- abandoned: lapsed lease, never landed/withdrawn, no fresh holder picked it back up. "since"/how-long
        -- is measured from lease_expires_at — the moment the lease LAPSED (= the moment of neglect). NOT from
        -- released_at: the sweep stamps released_at=now() when it flips active→expired, so released_at is just
        -- "when we noticed", whereas lease_expires_at is when the doer actually went dark (the honest age).
        SELECT EXTRACT(EPOCH FROM (now() - c.lease_expires_at))::bigint AS stalled_seconds,
               jsonb_build_object(
                 'reason', 'abandoned', 'change_id', c.change_id, 'repo', c.repo, 'branch', c.branch,
                 'path', c.target_path, 'held_by', core.agent_name(c.agent_id),
                 'since', c.lease_expires_at,
                 'stalled_seconds', EXTRACT(EPOCH FROM (now() - c.lease_expires_at))::bigint) AS obj
        FROM core.claim c WHERE c.account_id=v_account
          AND ( c.claim_state='expired'
                OR (c.claim_state='active' AND c.lease_expires_at < v_aband_before) )
          AND NOT EXISTS (SELECT 1 FROM core.claim a WHERE a.account_id=v_account AND a.repo=c.repo
                            AND a.branch=c.branch AND a.target_path=c.target_path AND a.claim_state='active'
                            AND a.lease_expires_at >= v_aband_before)
        UNION ALL
        -- starved-waiting: in line past the bounded threshold (the waiter is starving).
        SELECT EXTRACT(EPOCH FROM (now() - c.claimed_at))::bigint AS stalled_seconds,
               jsonb_build_object(
                 'reason', 'starved-waiting', 'change_id', c.change_id, 'repo', c.repo, 'branch', c.branch,
                 'path', c.target_path, 'held_by', core.agent_name(c.agent_id),
                 'since', c.claimed_at,
                 'stalled_seconds', EXTRACT(EPOCH FROM (now() - c.claimed_at))::bigint) AS obj
        FROM core.claim c WHERE c.account_id=v_account
          AND c.claim_state='waiting' AND c.claimed_at < now() - make_interval(mins => v_wait_min)
      ) x), '[]'::jsonb)
  ) INTO v_result;
  RETURN v_result;
END $$;
ALTER FUNCTION core.stalled_work_surface() OWNER TO veripsa_migrator;
GRANT EXECUTE ON FUNCTION core.stalled_work_surface() TO veripsa_reader, veripsa_writer, veripsa_demo_steward;

-- stuck_prs_surface: RED / STUCK PRs — the complement to stalled_work_surface, made ACTIVE. stalled_work_surface
-- catches the in-flight lane whose doer VANISHED (abandoned/starved, derived from the claim lifecycle); THIS
-- catches the in-flight PR that is RED — CI failing or merge-conflicting — and being IGNORED, because nobody
-- watches the GitHub inbox (the App observes server-side; the human does not). It LISTS the recent 'pr_failing'
-- facts the gate recorded (record_pr_failing_with_authority), so the console / notifications can say "these PRs
-- are blocked — fix CI or resolve the conflict". Derived PURELY from the existing event ledger (no new table; a
-- signal is a READ over a KIND). Content-free: the PR change ref (PR-<n>) + repo/branch + head sha + the bounded
-- reason TOKEN, NEVER a log/diagnostic/code. ADVISORY + NOTIFY-ONLY (it only reports; it never blocks anything).
-- DEDUP: a single PR can record several pr_failing facts over time (a rerun, a new push, a different reason). We
-- collapse to ONE row per (repo,branch,PR) = its LATEST fact, so the surface shows each stuck PR ONCE with its
-- most recent reason/sha/time (count = distinct stuck PRs, not raw events). A PR that later goes green simply
-- stops recording new pr_failing facts; the LANDING path is the resolution — a landed PR is no longer in-flight,
-- so we EXCLUDE any PR whose change ref already 'landed' on this coordinate (it reached main → not stuck anymore).
-- BOUNDED KNOB (clamped via _policy_int, owner-tunable through set_policy, never out of frame):
--   stuck_pr_window_minutes : how far back a pr_failing fact still counts as "currently stuck". default 10080
--                             (7 days), clamp 60..43200 (1 hour .. 30 days). An older red signal ages out (the
--                             PR was likely closed/fixed without the App seeing a green check) rather than
--                             haunting the board forever. HONEST-EMPTY otherwise (no fabricated rows).
-- REPO LIFECYCLE: retained audit facts for a removed repository are not current work. Unsuperseded tombstones hide
-- the coordinate; a same-name replacement only sees facts at/after its superseded boundary, matching repo insights.
-- STABLE (read-only; never sweeps). Tenant-pinned (account from the connection role, like every sibling surface).
CREATE OR REPLACE FUNCTION core.stuck_prs_surface() RETURNS jsonb
    LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path TO 'core','pg_catalog' AS $$
DECLARE v_account text; v_window_min int; v_since timestamptz; v_result jsonb;
BEGIN
  SELECT account INTO v_account FROM core.resolve_session_identity() AS r(agent,account);
  PERFORM set_config('core.current_account', v_account, true);
  v_window_min := core._policy_int('stuck_pr_window_minutes', 10080, 60, 43200);
  v_since := now() - make_interval(mins => v_window_min);
  WITH failing AS (
    -- DISTINCT-ON the latest pr_failing fact per (repo,branch,PR-ref); drop any PR already landed on this coord.
    SELECT DISTINCT ON (e.repo, e.branch, e.path)
           e.repo, e.branch, e.path AS pr, e.detail AS reason, e.commit_sha, e.agent_id, e.occurred_at
      FROM core.event e
     WHERE e.account_id=v_account AND e.kind='pr_failing' AND e.occurred_at > v_since
       AND NOT EXISTS (
         SELECT 1 FROM core.repository_lifecycle_tombstone t
          WHERE t.account_id=v_account AND t.repo=e.repo AND t.superseded_at IS NULL)
       AND e.occurred_at >= core._repository_generation_boundary(e.repo)
       AND NOT EXISTS (
         SELECT 1 FROM core.event l
          WHERE l.account_id=v_account AND l.kind='landed'
            AND l.repo=e.repo AND l.branch=e.branch AND l.path=e.path
            -- A recreated same-name repository restarts PR numbering. Retained predecessor audit (for example its
            -- landed PR-1) must not hide the replacement's new PR-1 failure.
            AND l.occurred_at >= core._repository_generation_boundary(e.repo))
     ORDER BY e.repo, e.branch, e.path, e.occurred_at DESC)
  SELECT jsonb_build_object(
    'window_minutes', v_window_min,
    'stuck_count', (SELECT count(*)::int FROM failing),
    'items', COALESCE((
      SELECT jsonb_agg(jsonb_build_object(
               'pr', f.pr, 'repo', f.repo, 'branch', f.branch, 'reason', f.reason,
               'commit_sha', f.commit_sha, 'author', core.agent_name(f.agent_id),
               'since', f.occurred_at,
               'stuck_seconds', EXTRACT(EPOCH FROM (now() - f.occurred_at))::bigint)
             ORDER BY f.occurred_at DESC)
      FROM failing f), '[]'::jsonb)
  ) INTO v_result;
  RETURN v_result;
END $$;
ALTER FUNCTION core.stuck_prs_surface() OWNER TO veripsa_migrator;
GRANT EXECUTE ON FUNCTION core.stuck_prs_surface() TO veripsa_reader, veripsa_writer, veripsa_demo_steward;

-- change_concluded: ORDER-INDEPENDENCE guard for the webhook (GitHub does NOT guarantee delivery order). A PR
-- (change) is "concluded" once it has landed or withdrawn — its claims exist but NONE are live (active/waiting).
-- The App calls this to IGNORE a stale 'opened'/'synchronize' that GitHub redelivers AFTER the PR already
-- closed: re-declaring would resurrect a merged PR's claims and show it as falsely in-flight until the lease
-- expires. A genuine `reopened` action is exempt (it SHOULD re-activate). A never-seen PR has no claims →
-- not concluded → processed normally. Content-free; tenant-pinned (the moat unchanged).
CREATE OR REPLACE FUNCTION core.change_concluded(p_repo text, p_change_id text) RETURNS boolean
    LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path TO 'core','pg_catalog' AS $$
DECLARE v_account text; v_total int; v_live int;
BEGIN
  SELECT account INTO v_account FROM core.resolve_session_identity() AS r(agent,account);
  PERFORM set_config('core.current_account', v_account, true);
  SELECT count(*)::int, count(*) FILTER (WHERE claim_state IN ('active','waiting'))::int
    INTO v_total, v_live
    FROM core.claim WHERE account_id=v_account AND repo=left(COALESCE(p_repo,''),512)
      AND change_id=left(COALESCE(p_change_id,''),200);
  RETURN v_total > 0 AND v_live = 0;
END $$;
ALTER FUNCTION core.change_concluded(text,text) OWNER TO veripsa_migrator;
GRANT EXECUTE ON FUNCTION core.change_concluded(text,text) TO veripsa_reader, veripsa_writer, veripsa_demo_steward;

-- ════════════════════════════════════════════════════════════════════════════════════════════
-- THE ANSWER-CHECK (答え合わせ) — RESULTS / OUTCOMES of Veripsa's advice. (PO 2026-06-18)
-- Today Veripsa records only its PREDICTION (the serialize/warn/clear verdict). To measure its OWN accuracy
-- on real data, capture the eventual OUTCOME at PR-close — content-free, append-only, records-not-correctness
-- (we record FACTS; we NEVER claim "we were right"). Two new event KINDS in the one ledger (no new table —
-- the no-乱立 law): 'prediction' (the verdict + recorded land order at analysis time) and 'advice_outcome'
-- (followed/ignored × clean/conflicted/reverted + a CONFIDENCE label, recorded when the PR closes).
-- ════════════════════════════════════════════════════════════════════════════════════════════

-- record_prediction_with_authority: persist THIS change's PREDICTION so the close-time answer-check has
-- something to join against. The live surface (main_impact_surface) re-computes the verdict every event from
-- the CURRENT in-flight set — which is GONE once a PR closes (its claims released). So the verdict + the land
-- order MUST be snapshotted at analysis time or the outcome can never be measured. We record it as the
-- 'prediction' event KIND: path = the change ref ('PR-<n>', content-free, same convention as pr_failing),
-- detail = 'verdict=<v>;behind=<csv of change refs this change was told to land AFTER>'. The behind list is
-- the recorded SUGGESTED LAND ORDER / serialize-behind (content-free change refs only). Idempotent on
-- (account,repo,branch,change) — the FIRST analysis wins (a re-analysis on synchronize must not overwrite the
-- original prediction we are grading; ON CONFLICT DO NOTHING). App-delegation-only. Append-only; tenant-pinned.
CREATE OR REPLACE FUNCTION core.record_prediction_with_authority(p_change_id text, p_repo text, p_branch text, p_verdict text, p_behind text[] DEFAULT NULL)
    RETURNS text LANGUAGE plpgsql SECURITY DEFINER SET search_path TO 'core','pg_catalog' AS $$
DECLARE v_agent text; v_account text; v_change text; v_repo text; v_branch text; v_verdict text; v_behind text; v_detail text; v_id text;
BEGIN
  v_change := left(NULLIF(btrim(COALESCE(p_change_id,'')),''),200);
  IF v_change IS NULL THEN RETURN NULL; END IF;                 -- fail-open: no change ref → record nothing (never raise into the webhook)
  v_repo := left(COALESCE(p_repo,''),512); v_branch := left(COALESCE(p_branch,''),512);
  -- BOUNDED verdict: only the known content-free verdict tokens main_impact_surface emits; anything else → 'unknown'
  -- (never free text in the ledger). Keeps the confusion matrix's predicted-axis a closed vocabulary.
  v_verdict := lower(btrim(COALESCE(p_verdict,'')));
  IF v_verdict NOT IN ('clear','warn','serialize','serialize_soft','unknown') THEN v_verdict := 'unknown'; END IF;
  -- the recorded land order: the change refs (content-free) THIS change was told to land AFTER. Sanitised to the
  -- 'PR-<n>'/'BR-<x>' shape, deduped, bounded (≤8, keeps detail ≤200), comma-joined. NULL/empty → no predecessors.
  SELECT string_agg(DISTINCT b, ',') INTO v_behind FROM (
    SELECT left(regexp_replace(unnest, '[^A-Za-z0-9_:.\-]', '', 'g'), 40) AS b
      FROM unnest(COALESCE(p_behind, ARRAY[]::text[]))) s WHERE b <> '' LIMIT 8;
  v_detail := left('verdict='||v_verdict||';behind='||COALESCE(v_behind,''), 200);
  SELECT agent, account INTO v_agent, v_account FROM core.establish_session_write_context() AS c(agent,account);
  -- THE LEDGER WALL (#95 follow-up): over the events cap → record nothing (append-only event ledger growth).
  IF core._ledger_write_blocked(v_account) THEN RETURN NULL; END IF;
  v_id := 'EV-PRED-'||substr(md5(v_account||'|'||v_repo||'|'||v_branch||'|'||v_change),1,24);  -- ONE prediction per change (first wins)
  PERFORM core.mark_governed_write('event');
  INSERT INTO core.event(event_id, account_id, kind, agent_id, repo, branch, path, detail)
  VALUES (v_id, v_account, 'prediction', v_agent, v_repo, v_branch, v_change, v_detail)
  ON CONFLICT (account_id, event_id) DO NOTHING;
  RETURN v_id;
END $$;
ALTER FUNCTION core.record_prediction_with_authority(text,text,text,text,text[]) OWNER TO veripsa_migrator;

-- record_advice_outcome_with_authority: THE ANSWER-CHECK. A PR closed — grade Veripsa's advice on it against
-- the eventual outcome, content-free. Joins the change's recorded 'prediction' (verdict + the land order it was
-- told to follow) against TWO content-free FACTS the App passes from the close/merge signals:
--   p_conflicted : the merge needed conflict resolution OR a later revert references it OR main's required
--                  check went red on the merge commit. A FACT label, NOT a correctness claim (a follow-up
--                  "fix" may be unrelated) — so it always carries a CONFIDENCE. (Derived by the App from
--                  content-free signals: pull_request.merged + check_suite/push red on the merge sha + revert ref.)
--   p_confidence : 'observed' (a direct signal — a revert ref / a red required check on the merge sha) vs
--                  'inferred' (a weaker heuristic). records-not-correctness: we never assert we were right.
-- The 'followed/ignored' axis is computed HERE (not passed in) from the ledger truth: a change was told to land
-- AFTER predecessors (its recorded 'behind' list); if ANY of those predecessors had NOT yet landed when THIS
-- change landed, the advice was IGNORED (merged out of the recorded suggested order). A warn that BOTH sides
-- merged also = ignored. Everything else (no predecessors outstanding, or a clear/clean land) = followed.
-- Records ONE 'advice_outcome' event: path = the change ref, detail = 'pred=<verdict>;adv=<followed|ignored>;
-- land=<clean|conflicted|reverted>;conf=<observed|inferred>'. Idempotent on (account,repo,branch,change).
-- App-delegation-only; append-only; tenant-pinned; content-free.
CREATE OR REPLACE FUNCTION core.record_advice_outcome_with_authority(p_change_id text, p_repo text, p_branch text, p_conflicted boolean DEFAULT false, p_reverted boolean DEFAULT false, p_confidence text DEFAULT 'inferred')
    RETURNS text LANGUAGE plpgsql SECURITY DEFINER SET search_path TO 'core','pg_catalog' AS $$
DECLARE v_agent text; v_account text; v_change text; v_repo text; v_branch text; v_id text;
        v_verdict text; v_behind text[]; v_pred_detail text; v_followed text; v_land text; v_conf text;
        v_outstanding int := 0; v_detail text;
BEGIN
  v_change := left(NULLIF(btrim(COALESCE(p_change_id,'')),''),200);
  IF v_change IS NULL THEN RETURN NULL; END IF;                 -- fail-open
  v_repo := left(COALESCE(p_repo,''),512); v_branch := left(COALESCE(p_branch,''),512);
  SELECT agent, account INTO v_agent, v_account FROM core.establish_session_write_context() AS c(agent,account);
  -- THE LEDGER WALL (#95 follow-up): over the events cap → record nothing (append-only event ledger growth).
  IF core._ledger_write_blocked(v_account) THEN RETURN NULL; END IF;
  -- pull THIS change's recorded prediction (verdict + behind list). No prediction on record (e.g. the PR was a
  -- draft / out of scope at open) → grade as predicted='unknown' with no predecessors (an honest record, not a miss).
  SELECT detail INTO v_pred_detail FROM core.event
    WHERE account_id=v_account AND kind='prediction' AND repo=v_repo AND branch=v_branch AND path=v_change
    ORDER BY occurred_at ASC LIMIT 1;
  IF v_pred_detail IS NOT NULL THEN
    v_verdict := COALESCE(substring(v_pred_detail FROM 'verdict=([a-z_]+)'), 'unknown');
    v_behind  := string_to_array(NULLIF(COALESCE(substring(v_pred_detail FROM 'behind=([^;]*)'), ''), ''), ',');
  ELSE
    v_verdict := 'unknown';
  END IF;
  -- FOLLOWED vs IGNORED (computed from ledger truth, never passed in): this change was told to land AFTER its
  -- predecessors (its recorded 'behind' list). It IGNORED that order if, at the moment it closes, ANY named
  -- predecessor is STILL IN-FLIGHT (has a live active/waiting claim) = has NOT landed yet = this change jumped
  -- the queue ahead of one it was told to wait for. A serialize/warn whose every predecessor has already
  -- concluded (no live claim), or a change with no predecessors, = followed. Content-free; tenant-pinned.
  -- The capture is wired AT THE PR-CLOSE event, BEFORE this change's own claims are released by land_change —
  -- and a predecessor still holding its lane is the honest signal it had not landed first.
  IF v_behind IS NOT NULL AND array_length(v_behind,1) IS NOT NULL THEN
    SELECT count(*)::int INTO v_outstanding FROM (
      SELECT DISTINCT btrim(p) AS pred FROM unnest(v_behind) p WHERE btrim(p) <> '' AND btrim(p) <> v_change
    ) b WHERE EXISTS (
      -- predecessor STILL in-flight (a live claim) when this change closes ⇒ it had NOT landed ahead ⇒ jumped queue.
      SELECT 1 FROM core.claim c WHERE c.account_id=v_account AND c.repo=v_repo AND c.branch=v_branch
        AND c.change_id=b.pred AND c.claim_state IN ('active','waiting'));
  END IF;
  v_followed := CASE WHEN v_outstanding > 0 THEN 'ignored' ELSE 'followed' END;
  -- the land outcome FACT (content-free): reverted (strongest) > conflicted > clean. A revert IS a kind of
  -- conflicted-outcome but is recorded distinctly (a higher-signal, observed fact).
  v_land := CASE WHEN COALESCE(p_reverted,false) THEN 'reverted'
                 WHEN COALESCE(p_conflicted,false) THEN 'conflicted'
                 ELSE 'clean' END;
  v_conf := lower(btrim(COALESCE(p_confidence,'')));
  IF v_conf NOT IN ('observed','inferred') THEN v_conf := 'inferred'; END IF;
  v_detail := left('pred='||v_verdict||';adv='||v_followed||';land='||v_land||';conf='||v_conf, 200);
  v_id := 'EV-OUTCOME-'||substr(md5(v_account||'|'||v_repo||'|'||v_branch||'|'||v_change),1,21);  -- ONE outcome per change
  PERFORM core.mark_governed_write('event');
  INSERT INTO core.event(event_id, account_id, kind, agent_id, repo, branch, path, detail)
  VALUES (v_id, v_account, 'advice_outcome', v_agent, v_repo, v_branch, v_change, v_detail)
  ON CONFLICT (account_id, event_id) DO NOTHING;
  RETURN v_id;
END $$;
ALTER FUNCTION core.record_advice_outcome_with_authority(text,text,text,boolean,boolean,text) OWNER TO veripsa_migrator;

-- record_human_judgment_with_authority: THE HUMAN half of the answer-check. record_advice_outcome above
-- grades what HAPPENED to a change (did it conflict, was it reverted) and marks that `inferred` -- it is
-- automation observing automation. It can never say whether a person found the advice any good. The
-- External Validation Gate counts exactly one thing as market validation: an EXTERNAL human confirming a
-- signal was USEFUL. That claim needs a human to have made it, so it needs its own recording path.
--
-- CONTENT-FREE BY CONSTRUCTION: two closed enums, the change/repo/branch coordinates the ledger already
-- carries, and NOTHING else. There is deliberately no free-text field -- not a truncated one, not an
-- optional one. A judgment form that accepts prose would eventually carry a diff excerpt or a customer
-- quote into the ledger, and the content-free boundary is not worth trading for nuance. If nuance is
-- needed it belongs in the public-safe lead issue, not here.
--
-- Both enums are EXCLUSIVE (one value each) and validated in-function: an unknown value returns NULL
-- (fail-open, like the rest of this surface) rather than silently recording a value the surfaces cannot
-- aggregate. `not-observed` / `unknown` are first-class values so "nobody looked" is recordable as itself
-- and never has to be faked as a weak positive.
CREATE OR REPLACE FUNCTION core.record_human_judgment_with_authority(
    p_change_id text, p_repo text, p_branch text, p_judgment text, p_action text DEFAULT 'unknown')
    RETURNS text LANGUAGE plpgsql SECURITY DEFINER SET search_path TO 'core','pg_catalog' AS $$
DECLARE v_agent text; v_account text; v_change text; v_repo text; v_branch text; v_id text;
        v_judgment text; v_action text; v_detail text;
BEGIN
  v_change := left(NULLIF(btrim(COALESCE(p_change_id,'')),''),200);
  IF v_change IS NULL THEN RETURN NULL; END IF;                 -- fail-open
  v_judgment := lower(btrim(COALESCE(p_judgment,'')));
  v_action   := lower(btrim(COALESCE(p_action,'unknown')));
  IF v_judgment NOT IN ('useful','correct-but-no-action-needed','already-knew','unclear','noisy',
                        'incorrect','missing-relationship','insufficient-unknown','not-observed') THEN
    RETURN NULL;                                                -- unknown judgment: refuse, do not guess
  END IF;
  IF v_action NOT IN ('changed-merge-order','held-a-pr','rebased-or-regenerated','split-a-pr',
                      'reassigned-overlapping-work','added-review','acknowledged-and-proceeded',
                      'no-action-needed','ignored','unknown') THEN
    RETURN NULL;                                                -- unknown action: refuse, do not guess
  END IF;
  v_repo := left(COALESCE(p_repo,''),512); v_branch := left(COALESCE(p_branch,''),512);
  SELECT agent, account INTO v_agent, v_account FROM core.establish_session_write_context() AS c(agent,account);
  IF core._ledger_write_blocked(v_account) THEN RETURN NULL; END IF;
  -- Enum pair only. Never a body, never a quote, never a path beyond the change coordinate above.
  v_detail := 'judgment='||v_judgment||';action='||v_action;
  -- APPEND-ONLY, deliberately. core.event guarantees "a recorded fact is permanent; only its visibility
  -- may change" (assert_append_only), so a re-answer must NOT overwrite the earlier one -- it appends a
  -- NEW fact. That is also the more honest model: "they first called it noisy, then useful once they
  -- understood it" is real history worth keeping, not a correction to erase. The event_id therefore
  -- includes the judgment pair, so the SAME answer given twice is idempotent (ON CONFLICT DO NOTHING)
  -- while a CHANGED answer lands as a new row. Aggregation takes the LATEST judgment per change, so
  -- re-answering still never double-counts a person in the funnel.
  v_id := 'EV-JUDGMENT-'||substr(md5(v_account||'|'||v_repo||'|'||v_branch||'|'||v_change||'|'||v_detail),1,21);
  PERFORM core.mark_governed_write('event');
  INSERT INTO core.event(event_id, account_id, kind, agent_id, repo, branch, path, detail)
  VALUES (v_id, v_account, 'human_judgment', v_agent, v_repo, v_branch, v_change, v_detail)
  ON CONFLICT (account_id, event_id) DO NOTHING;
  RETURN v_id;
END $$;
ALTER FUNCTION core.record_human_judgment_with_authority(text,text,text,text,text) OWNER TO veripsa_migrator;

-- change_failing: a content-free OUTCOME signal for the answer-check — does THIS change have a recorded
-- 'pr_failing' fact (its required CI check went RED / it could not merge cleanly) on this coordinate? The App
-- already records pr_failing facts server-side from the check_suite/check_run path; this read lets the
-- merge-time outcome capture ask "did it land badly?" from in-system FACTS only (never a guess). Returns
-- boolean (true = a red/conflict fact on record). App-delegation read (the App derives the outcome signal);
-- tenant-pinned; STABLE.
CREATE OR REPLACE FUNCTION core.change_failing(p_change_id text, p_repo text, p_branch text) RETURNS boolean
    LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path TO 'core','pg_catalog' AS $$
DECLARE v_account text;
BEGIN
  SELECT account INTO v_account FROM core.resolve_session_identity() AS r(agent,account);
  PERFORM set_config('core.current_account', v_account, true);
  RETURN EXISTS (SELECT 1 FROM core.event WHERE account_id=v_account AND kind='pr_failing'
    AND repo=left(COALESCE(p_repo,''),512) AND branch=left(COALESCE(p_branch,''),512)
    AND path=left(COALESCE(p_change_id,''),200));
END $$;
ALTER FUNCTION core.change_failing(text,text,text) OWNER TO veripsa_migrator;

-- change_failing (SHA-AWARE overload) — the answer-check's STALE-RED-FACT fix (#334). The 3-arg form above is
-- sha-BLIND: it returns true iff ANY 'pr_failing' fact exists on the change's coordinate, and the append-only
-- ledger never RETRACTS a red fact when CI later goes green. So a PR that went transiently RED at an early head
-- (a flaky test / a typo the author then FIXED) and merged CLEANLY at the fixed head still carried that early
-- red fact → the clean merge was graded land=conflicted (conf=observed) → a FALSE silent-miss that INFLATES the
-- effect-ledger numbers Veripsa sells on (records-not-correctness: never record an outcome the facts do not
-- support). This overload scopes the red fact to the MERGED head sha so a transient earlier red no longer
-- mis-grades a clean merge, while a PR genuinely merged-WHILE-red (a red fact AT the final head) is STILL caught.
-- RECALL-SAFE: p_head_sha IS NULL (or blank) → falls back to the existing any-red behavior (the 3-arg semantics),
-- so an unknown head can never silently hide a real bad landing. Additive / idempotent; the 3-arg form stays for
-- callers that have no sha. App-delegation read; tenant-pinned; STABLE.
CREATE OR REPLACE FUNCTION core.change_failing(p_change_id text, p_repo text, p_branch text, p_head_sha text) RETURNS boolean
    LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path TO 'core','pg_catalog' AS $$
DECLARE v_account text; v_sha text;
BEGIN
  SELECT account INTO v_account FROM core.resolve_session_identity() AS r(agent,account);
  PERFORM set_config('core.current_account', v_account, true);
  v_sha := NULLIF(btrim(COALESCE(p_head_sha,'')),'');
  RETURN EXISTS (SELECT 1 FROM core.event WHERE account_id=v_account AND kind='pr_failing'
    AND repo=left(COALESCE(p_repo,''),512) AND branch=left(COALESCE(p_branch,''),512)
    AND path=left(COALESCE(p_change_id,''),200)
    AND (v_sha IS NULL OR commit_sha = v_sha));   -- scope to the MERGED head; any-red fallback when sha unknown
END $$;
ALTER FUNCTION core.change_failing(text,text,text,text) OWNER TO veripsa_migrator;

-- outcome_results_surface — THE ANSWER-CHECK SCREEN (答え合わせ). Aggregates every recorded 'advice_outcome'
-- into a CONFUSION MATRIX (predicted verdict × land outcome × advice followed/ignored) plus the two headline
-- stats. records-not-correctness: every cell is a COUNT OF FACTS, never a correctness verdict on Veripsa.
--   THE PREDICTION OUTCOME CLASSES (the matrix's diagnostic cells):
--     • ignored → conflicted  = TRUE POSITIVE (Veripsa warned/serialized, the dev ignored it, it conflicted).
--     • warned  → clean        = possible FALSE POSITIVE / over-warn (we warned, nothing went wrong in-window).
--     • CLEARED → conflicted   = FALSE NEGATIVE / SILENT MISS (we said clear, it conflicted anyway) — the most
--                                important to capture; a missed coupling is the failure Veripsa sells against.
--   THE TWO HEADLINE STATS:
--     • ignored_advice_conflicted_rate = of the changes that IGNORED a non-clear verdict, the fraction that then
--                                        conflicted/reverted (= "when they ignore us, how often were we right").
--     • silent_miss_count              = cleared-but-conflicted (the false negatives) — the number to drive to 0.
-- Honest-empty until outcomes accrue. Content-free; tenant-pinned. STABLE read (no writes).
CREATE OR REPLACE FUNCTION core.outcome_results_surface() RETURNS jsonb
    LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path TO 'core','pg_catalog' AS $$
DECLARE v_account text; v_result jsonb; v_ignored_nonclear int; v_ignored_bad int;
BEGIN
  SELECT account INTO v_account FROM core.resolve_session_identity() AS r(agent,account);
  PERFORM set_config('core.current_account', v_account, true);
  -- parse each advice_outcome's detail into its facets once (a small per-account set). The headline-stat
  -- numerator/denominator are computed first into locals (the matrix SELECT below repeats the CTE — both are
  -- STABLE reads over the same small set; PL/pgSQL CTEs do not survive across statements, so we re-declare).
  WITH o AS (
    SELECT COALESCE(substring(detail FROM 'pred=([a-z_]+)'),'unknown')      AS pred,
           COALESCE(substring(detail FROM 'adv=([a-z]+)'),'followed')        AS adv,
           COALESCE(substring(detail FROM 'land=([a-z]+)'),'clean')          AS land,
           COALESCE(substring(detail FROM 'conf=([a-z]+)'),'inferred')       AS conf
      FROM core.event WHERE account_id=v_account AND kind='advice_outcome'),
  oc AS (SELECT pred, adv, land, conf, (land IN ('conflicted','reverted')) AS bad,
                (pred IN ('warn','serialize','serialize_soft')) AS warned FROM o)
  SELECT
    count(*) FILTER (WHERE adv='ignored' AND warned),                                    -- denom of headline stat 1
    count(*) FILTER (WHERE adv='ignored' AND warned AND bad)                             -- numer of headline stat 1
    INTO v_ignored_nonclear, v_ignored_bad
    FROM oc;
  WITH o AS (
    SELECT COALESCE(substring(detail FROM 'pred=([a-z_]+)'),'unknown')      AS pred,
           COALESCE(substring(detail FROM 'adv=([a-z]+)'),'followed')        AS adv,
           COALESCE(substring(detail FROM 'land=([a-z]+)'),'clean')          AS land,
           COALESCE(substring(detail FROM 'conf=([a-z]+)'),'inferred')       AS conf
      FROM core.event WHERE account_id=v_account AND kind='advice_outcome'),
  -- a land outcome is "bad" (the thing the advice was trying to avoid) when it conflicted OR reverted.
  oc AS (SELECT pred, adv, land, conf, (land IN ('conflicted','reverted')) AS bad,
                (pred IN ('warn','serialize','serialize_soft')) AS warned FROM o)
  SELECT jsonb_build_object(
    'total_outcomes', (SELECT count(*)::int FROM core.event WHERE account_id=v_account AND kind='advice_outcome'),
    -- THE CONFUSION MATRIX (predicted verdict × land outcome × advice followed/ignored) — every diagnostic cell.
    'matrix', (SELECT jsonb_build_object(
        -- TP: we flagged it (warn/serialize), they ignored, it went bad. Veripsa was right (a recorded fact).
        'ignored_conflicted_tp', count(*) FILTER (WHERE adv='ignored' AND warned AND bad),
        -- followed → clean: they took the advice and it landed cleanly (the intended good path).
        'followed_clean',        count(*) FILTER (WHERE adv='followed' AND NOT bad),
        -- followed → conflicted: they followed the advice but it conflicted anyway (advice insufficient / unrelated cause).
        'followed_conflicted',   count(*) FILTER (WHERE adv='followed' AND bad),
        -- warned → clean (possible over-warn / false positive in this window): we warned, nothing went wrong.
        'warned_clean_fp',       count(*) FILTER (WHERE warned AND NOT bad),
        -- CLEARED → conflicted = the SILENT MISS (false negative): we said clear, it conflicted/reverted anyway.
        'cleared_conflicted_fn', count(*) FILTER (WHERE pred='clear' AND bad),
        -- cleared → clean: a true negative (we said clear, it was clean).
        'cleared_clean_tn',      count(*) FILTER (WHERE pred='clear' AND NOT bad),
        -- ignored → clean: they ignored a warn/serialize and got away with it (this window) — context for stat 1.
        'ignored_clean',         count(*) FILTER (WHERE adv='ignored' AND warned AND NOT bad)
      ) FROM oc),
    -- per-verdict × land breakdown (the full grid, for a console heat-table). Content-free counts only.
    'by_verdict', COALESCE((SELECT jsonb_object_agg(pred, cells) FROM (
        SELECT pred, jsonb_build_object(
            'clean',      count(*) FILTER (WHERE land='clean'),
            'conflicted', count(*) FILTER (WHERE land='conflicted'),
            'reverted',   count(*) FILTER (WHERE land='reverted'),
            'ignored',    count(*) FILTER (WHERE adv='ignored'),
            'followed',   count(*) FILTER (WHERE adv='followed')) AS cells
          FROM oc GROUP BY pred) g), '{}'::jsonb),
    -- ── THE TWO HEADLINE STATS ──────────────────────────────────────────────────────────────
    -- 1) ignored-advice → conflicted rate: of changes that IGNORED a non-clear verdict, the fraction that then
    --    conflicted/reverted. NULL (not 0) when nobody ignored a non-clear verdict yet (honest-empty, not "0%").
    'ignored_advice_conflicted_rate', CASE WHEN v_ignored_nonclear > 0
        THEN round(v_ignored_bad::numeric / v_ignored_nonclear, 3) ELSE NULL END,
    'ignored_advice_denominator',     v_ignored_nonclear,        -- the n behind the rate (a rate over 1 case is weak — show n)
    -- 2) SILENT MISS count: cleared-but-conflicted (the false negatives) — the number to drive to 0.
    'silent_miss_count', (SELECT count(*)::int FROM oc WHERE pred='clear' AND bad),
    -- the recent outcomes (content-free: change ref + the parsed facets + when). For the console feed.
    'recent', COALESCE((SELECT jsonb_agg(jsonb_build_object(
        'change', e.path, 'repo', e.repo, 'branch', e.branch,
        'predicted', COALESCE(substring(e.detail FROM 'pred=([a-z_]+)'),'unknown'),
        'advice',    COALESCE(substring(e.detail FROM 'adv=([a-z]+)'),'followed'),
        'land',      COALESCE(substring(e.detail FROM 'land=([a-z]+)'),'clean'),
        'confidence',COALESCE(substring(e.detail FROM 'conf=([a-z]+)'),'inferred'),
        'at', e.occurred_at) ORDER BY e.occurred_at DESC)
      FROM (SELECT * FROM core.event WHERE account_id=v_account AND kind='advice_outcome'
             ORDER BY occurred_at DESC LIMIT 20) e), '[]'::jsonb)
  ) INTO v_result;
  RETURN v_result;
END $$;
ALTER FUNCTION core.outcome_results_surface() OWNER TO veripsa_migrator;

-- GRANTS — the answer-check fns follow the App-delegation pattern of the other landing/outcome gates: the
-- App service identity (veripsa_app) records facts server-side (a buyer's writer must not FORGE a "we were
-- right/wrong" record); the read surface is granted to the read roles (console / owner). Mirrors
-- record_pr_failing_with_authority (record = App-only) + the surfaces (read = reader/writer/steward).
REVOKE EXECUTE ON FUNCTION core.record_prediction_with_authority(text,text,text,text,text[]) FROM PUBLIC, veripsa_writer;
REVOKE EXECUTE ON FUNCTION core.record_advice_outcome_with_authority(text,text,text,boolean,boolean,text) FROM PUBLIC, veripsa_writer;
REVOKE EXECUTE ON FUNCTION core.record_human_judgment_with_authority(text,text,text,text,text) FROM PUBLIC, veripsa_writer;
GRANT EXECUTE ON FUNCTION core.record_prediction_with_authority(text,text,text,text,text[]) TO veripsa_app;
GRANT EXECUTE ON FUNCTION core.record_advice_outcome_with_authority(text,text,text,boolean,boolean,text) TO veripsa_app;
GRANT  EXECUTE ON FUNCTION core.record_human_judgment_with_authority(text,text,text,text,text) TO veripsa_app;
GRANT EXECUTE ON FUNCTION core.change_failing(text,text,text) TO veripsa_app, veripsa_reader, veripsa_writer, veripsa_demo_steward;
GRANT EXECUTE ON FUNCTION core.change_failing(text,text,text,text) TO veripsa_app, veripsa_reader, veripsa_writer, veripsa_demo_steward;  -- sha-aware overload (#334): match the 3-arg's grant
GRANT EXECUTE ON FUNCTION core.outcome_results_surface() TO veripsa_reader, veripsa_writer, veripsa_demo_steward, veripsa_app;

GRANT EXECUTE ON FUNCTION core.agent_name(text) TO veripsa_reader, veripsa_writer, veripsa_demo_steward;
GRANT EXECUTE ON FUNCTION core.collision_surface() TO veripsa_reader, veripsa_writer, veripsa_demo_steward;
-- LEAST-PRIVILEGE (audit iter-5 P3): strip the PUBLIC-EXECUTE default on the board read surface so a non-tenant
-- role (billing/platform-reader) can't reach it; the GRANT names exactly the tenant roles (App inherits writer).
REVOKE EXECUTE ON FUNCTION core.board_surface() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION core.board_surface() TO veripsa_reader, veripsa_writer, veripsa_demo_steward;

-- ============================================================================================
