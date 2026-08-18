-- PHASE 6b — POLICY-CHANGE REFRESH (G4). A durable OUTBOX that turns "an owner tuned a knob" into "that
-- tenant's OPEN in-flight PRs re-derive their posted Check under the new policy" — WITHOUT any GitHub call
-- inside the policy-write transaction.
--
-- WHY THIS EXISTS. Policy values are read LIVE per evaluation (core._policy_int / _policy_text re-SELECT
-- core.policy every call), so a tenant's FUTURE evaluations already reflect a just-changed knob. But a Check
-- ALREADY POSTED on an OPEN PR reflects the policy at its LAST render — after the owner changes a tuning knob,
-- that open PR's posted Check stays STALE until the PR gets its next push/synchronize (hours / days / never).
-- G4 closes that gap: when a policy is committed, enqueue a content-free refresh request for THAT account; a
-- background drainer (mirroring the durable webhook recovery loop) later re-renders that account's open PRs
-- OUTSIDE the DB transaction (it recomputes core.main_impact_surface — which reads the CURRENT policy live —
-- and re-posts through the existing idempotent _post_refreshes, which PATCHes existing checks/comments and
-- skips merged/closed PRs → never a new comment, never spam).
--
-- THE DESIGN (a durable outbox drained by the worker — NO GitHub API inside any DB txn):
--   * The policy WRITERS (set_policy_with_authority + the owner tuning setters) enqueue an outbox row in the
--     SAME transaction as the policy commit (atomic + causal: if the policy write rolls back, so does the
--     enqueue → "no refresh on rollback"). They call GitHub NEVER — they only INSERT.
--   * The DRAINER (github-app/policy_refresh_queue.py, a sibling of the delivery recovery loop) claims a
--     pending row, then OUTSIDE the txn enumerates that account's LIVE installation→repos and re-posts. All
--     GitHub work happens after the claim transaction has committed and closed.
--
-- CONTENT-FREE: this table holds scheduling metadata and repository coordinates only. It never holds source
-- bodies, diff bodies, paths, symbols, rendered Check text, or webhook payloads. `repository_id` is the stable
-- GitHub object identity; `repo` is only its mutable routing name, and `target_sha` is the exact graph fact that
-- must be durable before this turn may be consumed.
-- ============================================================================================

-- policy_refresh_outbox is the ONE durable account-convergence queue. A policy request uses the sentinel
-- identity (request_kind='policy', repository_id='', repo='', branch=''); graph requests use one latest-wins row
-- per stable repository id. A default-branch rename updates the mutable coordinate on that same identity; it
-- never creates a second durable fact. Rapid repeats collapse onto that identity and bump a monotonic
-- ACCOUNT epoch, while distinct repositories remain independent durable facts. The status is
-- DERIVED from the timestamps: (claimed_at IS NULL, done_at IS NULL) = pending & unclaimed; (claimed_at set,
-- done_at NULL) = a drainer is working it; (done_at set) = drained. Attempts 0..4 use the short retry cadence;
-- attempts >=5 are poison-isolated onto a bounded slow retry cadence (5m→10m→20m→40m→1h cap), but remain
-- autonomously claimable. A transient GitHub/DB outage therefore cannot strand an idle repository forever, while
-- one deterministic bad row consumes at most one globally-fair turn per hour after the bounded ramp. Mutable
-- operational state (NOT append-only, NOT a ledger) — the SAME class as core.webhook_delivery.
CREATE TABLE IF NOT EXISTS core.policy_refresh_outbox (
    account_id   text NOT NULL,
    request_kind text DEFAULT 'policy' NOT NULL,
    repository_id text DEFAULT '' NOT NULL,
    repo         text DEFAULT '' NOT NULL,
    branch       text DEFAULT '' NOT NULL,
    target_sha   text,
    not_before   timestamptz,
    terminal_reason text,
    policy_cursor_repo text DEFAULT '' NOT NULL,
    policy_cursor_branch text DEFAULT '' NOT NULL,
    change_cursor text DEFAULT '' NOT NULL,
    -- Coalescing latch for PR-surface changes that arrive while a graph turn
    -- is part-way through its bounded Check page. It never supersedes graph
    -- extraction authority; exact finish consumes it by scheduling one fresh
    -- full surface pass from the beginning.
    surface_dirty boolean DEFAULT false NOT NULL,
    -- Installation onboarding is a bounded phase on the existing graph row.  The plan contains only immutable,
    -- content-free GitHub PR numbers (never webhook payloads) and is frozen by the convergence worker after the
    -- installation transaction has committed.  One later turn advances one array position through an exact
    -- request+slot+lease CAS, so a repository with hundreds of open PRs cannot monopolize the worker.
    onboarding_pending boolean DEFAULT false NOT NULL,
    -- The reserved branch/SHA coordinate is a phase token, never GitHub authority. Only the onboarding-capable
    -- claim ABI can see it; its first turn resolves the real default-branch HEAD and replaces both atomically.
    onboarding_head_pending boolean DEFAULT false NOT NULL,
    onboarding_plan bigint[],
    onboarding_index integer DEFAULT 0 NOT NULL,
    onboarding_truncated boolean DEFAULT false NOT NULL,
    onboarding_watching_done boolean DEFAULT false NOT NULL,
    -- monotonic per-account provenance token — the policy state that triggered this refresh. Bumped on every
    -- enqueue; used to COALESCE (rapid writes climb the epoch on one row) and as the CAS guard on finish/fail so
    -- a drainer whose epoch was superseded mid-drain cannot mark the NEWER request done (causal ordering). The
    -- refresh ALWAYS recomputes from the current main_impact_surface, so the epoch never gates WHAT is posted —
    -- it can never post an older-policy verdict as current; it only records provenance + drives the CAS.
    policy_epoch bigint DEFAULT 0 NOT NULL,
    enqueued_at  timestamptz DEFAULT now() NOT NULL,
    claimed_at   timestamptz,                        -- NULL = unclaimed; set = a drainer owns it (stale-reclaimable)
    claimed_by   text,                               -- the drainer instance id (content-free worker tag)
    done_at      timestamptz,                        -- set once the refresh completed (drained)
    attempts     int DEFAULT 0 NOT NULL,             -- cumulative failures; >=5 is observable slow-retry isolation
    last_error   text,                               -- a SHORT bounded error CODE (never customer data)
    -- Keep the legacy account-only PK through EXPAND. The contract block below attaches the concurrently-built
    -- stable-id identity only after every old callable ABI has become policy-sentinel-only.
    CONSTRAINT policy_refresh_outbox_pkey PRIMARY KEY (account_id),
    CONSTRAINT policy_refresh_outbox_account_len CHECK (length(account_id) BETWEEN 1 AND 120),
    CONSTRAINT policy_refresh_outbox_request_shape CHECK (
      (request_kind='policy' AND repository_id='' AND repo='' AND branch='' AND target_sha IS NULL)
      OR
      (request_kind='graph'
       AND length(repository_id) BETWEEN 1 AND 32 AND repository_id ~ '^[1-9][0-9]*$'
       AND length(repo) BETWEEN 1 AND 512
       AND length(branch) BETWEEN 1 AND 512
       AND target_sha IS NOT NULL AND length(target_sha) BETWEEN 7 AND 64
       AND target_sha ~ '^[0-9a-f]+$')),
    CONSTRAINT policy_refresh_outbox_attempts_ok CHECK (attempts >= 0),
    CONSTRAINT policy_refresh_outbox_terminal_reason_ok CHECK (
      terminal_reason IS NULL OR (request_kind='graph' AND terminal_reason='quota_paused')),
    CONSTRAINT policy_refresh_outbox_claimed_by_len CHECK (claimed_by IS NULL OR length(claimed_by) <= 120),
    CONSTRAINT policy_refresh_outbox_error_len CHECK (last_error IS NULL OR length(last_error) <= 200),
    CONSTRAINT policy_refresh_outbox_cursor_len CHECK (
      length(policy_cursor_repo)<=512 AND length(policy_cursor_branch)<=512
      AND length(change_cursor)<=120),
    CONSTRAINT policy_refresh_outbox_onboarding_shape CHECK (
      onboarding_index BETWEEN 0 AND 300
      AND (onboarding_plan IS NULL OR cardinality(onboarding_plan)<=300)
      AND (onboarding_plan IS NULL OR onboarding_index<=cardinality(onboarding_plan))
      AND (onboarding_plan IS NOT NULL OR (
        onboarding_index=0 AND NOT onboarding_truncated AND NOT onboarding_watching_done))
      AND (NOT onboarding_watching_done OR (
        onboarding_plan IS NOT NULL AND onboarding_index=cardinality(onboarding_plan)))
      AND (NOT onboarding_head_pending OR (
        onboarding_pending AND request_kind='graph'
        AND onboarding_plan IS NULL AND onboarding_index=0
        AND NOT onboarding_truncated AND NOT onboarding_watching_done))
      AND (branch<>'__veripsa_onboarding_head__' OR target_sha<>'0000000'
        OR onboarding_head_pending)
      AND (onboarding_pending OR (
        NOT onboarding_head_pending AND onboarding_plan IS NULL AND onboarding_index=0
        AND NOT onboarding_truncated AND NOT onboarding_watching_done))),
    CONSTRAINT policy_refresh_outbox_onboarding_active CHECK (
      NOT onboarding_pending OR (request_kind='graph' AND done_at IS NULL))
);
-- Hot-deploy evolution from the original one-row-per-account policy-only queue. Constant defaults make these
-- catalog-only additions; existing rows become the policy sentinel without a data rewrite.
SELECT core._ensure_column_online(
  'policy_refresh_outbox','request_kind','text DEFAULT ''policy'' NOT NULL');
SELECT core._ensure_column_online(
  'policy_refresh_outbox','repository_id','text DEFAULT '''' NOT NULL');
SELECT core._ensure_column_online(
  'policy_refresh_outbox','repo','text DEFAULT '''' NOT NULL');
SELECT core._ensure_column_online(
  'policy_refresh_outbox','branch','text DEFAULT '''' NOT NULL');
SELECT core._ensure_column_online(
  'policy_refresh_outbox','target_sha','text');
SELECT core._ensure_column_online(
  'policy_refresh_outbox','not_before','timestamptz');
SELECT core._ensure_column_online(
  'policy_refresh_outbox','terminal_reason','text');
SELECT core._ensure_column_online(
  'policy_refresh_outbox','policy_cursor_repo','text DEFAULT '''' NOT NULL');
SELECT core._ensure_column_online(
  'policy_refresh_outbox','policy_cursor_branch','text DEFAULT '''' NOT NULL');
SELECT core._ensure_column_online(
  'policy_refresh_outbox','change_cursor','text DEFAULT '''' NOT NULL');
SELECT core._ensure_column_online(
  'policy_refresh_outbox','surface_dirty','boolean DEFAULT false NOT NULL');
SELECT core._ensure_column_online(
  'policy_refresh_outbox','onboarding_pending','boolean DEFAULT false NOT NULL');
SELECT core._ensure_column_online(
  'policy_refresh_outbox','onboarding_head_pending','boolean DEFAULT false NOT NULL');
SELECT core._ensure_column_online(
  'policy_refresh_outbox','onboarding_plan','bigint[]');
SELECT core._ensure_column_online(
  'policy_refresh_outbox','onboarding_index','integer DEFAULT 0 NOT NULL');
SELECT core._ensure_column_online(
  'policy_refresh_outbox','onboarding_truncated','boolean DEFAULT false NOT NULL');
SELECT core._ensure_column_online(
  'policy_refresh_outbox','onboarding_watching_done','boolean DEFAULT false NOT NULL');

-- One graph slot per account. The lease snapshots the exact request generation/coordinate separately from the
-- latest-wins outbox row, so a newer push may update the desired row without freeing the old extractor's slot.
-- The single-slot CHECK is the hard tenant-capacity fence: two Render workers may serve two accounts, but one
-- tenant can never occupy both. Only an exact lease_epoch+slot terminal CAS can release it; late workers after
-- expiry are harmless.
CREATE TABLE IF NOT EXISTS core.graph_convergence_lease (
    account_id text NOT NULL,
    slot smallint NOT NULL,
    lease_epoch bigint NOT NULL,
    request_epoch bigint NOT NULL,
    repository_id text NOT NULL,
    repo text NOT NULL,
    branch text NOT NULL,
    target_sha text NOT NULL,
    claimed_by text NOT NULL,
    claimed_until timestamptz NOT NULL,
    CONSTRAINT graph_convergence_lease_pkey PRIMARY KEY (account_id,slot),
    CONSTRAINT graph_convergence_lease_repo_uq UNIQUE (account_id,repository_id),
    CONSTRAINT graph_convergence_lease_epoch_uq UNIQUE (account_id,lease_epoch),
    -- Retain the original token-domain constraint for rolling-schema compatibility and add the stronger,
    -- separately named capacity contract. Existing databases receive/validate the stronger CHECK at cutover.
    CONSTRAINT graph_convergence_lease_slot_ok CHECK (slot BETWEEN 1 AND 2),
    CONSTRAINT graph_convergence_lease_account_cap_one CHECK (slot = 1),
    CONSTRAINT graph_convergence_lease_id_ok CHECK (
      length(repository_id) BETWEEN 1 AND 32 AND repository_id ~ '^[1-9][0-9]*$'),
    CONSTRAINT graph_convergence_lease_coordinate_ok CHECK (
      length(repo) BETWEEN 1 AND 512 AND length(branch) BETWEEN 1 AND 512
      AND length(target_sha) BETWEEN 7 AND 64 AND target_sha ~ '^[0-9a-f]+$'),
    CONSTRAINT graph_convergence_lease_worker_len CHECK (length(claimed_by)<=120)
);
-- The non-RLS installation router is also the global account scheduler. These content-free timestamps and lease
-- fields let one bounded index query select the oldest account without performing one tenant query per account.
SELECT core._ensure_column_online(
  'installation_account','policy_refresh_due_at','timestamptz');
SELECT core._ensure_column_online(
  'installation_account','graph_refresh_due_at','timestamptz');
-- Capability-specific rolling router. FORCE-RLS prevents a global candidate scan of the tenant outbox before an
-- account is pinned, so the /6 worker consumes this non-RLS scalar derived under `_sync_account_convergence_due`.
SELECT core._ensure_column_online(
  'installation_account','legacy_graph_refresh_due_at','timestamptz');
SELECT core._ensure_column_online(
  'installation_account','convergence_claimed_until','timestamptz');
SELECT core._ensure_column_online(
  'installation_account','convergence_claimed_by','text');
SELECT core._ensure_column_online(
  'installation_account','convergence_claim_epoch','bigint');
SELECT core._ensure_column_online(
  'installation_account','convergence_graph_claim_count','int DEFAULT 0 NOT NULL');
SELECT core._ensure_column_online(
  'installation_account','convergence_graph_reclaim_at','timestamptz');
SELECT core._ensure_column_online(
  'installation_account','convergence_pending_count','int DEFAULT 0 NOT NULL');
SELECT core._ensure_column_online(
  'installation_account','convergence_retry_exhausted_count','int DEFAULT 0 NOT NULL');
SELECT core._ensure_column_online(
  'installation_account','convergence_quota_deferred_count','int DEFAULT 0 NOT NULL');
-- Original wall-clock age of the oldest ordinary unfinished convergence fact.
-- This is deliberately separate from policy_refresh_due_at / graph_refresh_due_at:
-- those are fair-scheduler cursors and move to `now()` when a large account is
-- sliced to the tail.  Alert age must not reset on that move, on a lease
-- reclaim, or when the same exact graph target is observed again.
SELECT core._ensure_column_online(
  'installation_account','convergence_stall_started_at','timestamptz');
-- Existing router rows read as generation 0; accounts inserted after this deploy default to generation 1.
-- The one-time legacy bridge later advances only generation-0 rows. Its partial index stays empty afterwards,
-- making every subsequent schema re-apply O(0) rather than re-enumerating every tenant.
SELECT core._ensure_column_online(
  'installation_account','convergence_schema_version','int DEFAULT 0 NOT NULL');
-- Keep the INSERT default at generation 0 throughout EXPAND. The old live image may provision an account and
-- enqueue through its pre-router writer while this module is still building indexes; marking that route as
-- generation 1 here would make the bridge skip its invisible outbox row. The default flips only after the new
-- router-maintaining enqueue bodies are published, immediately before the locked bridge below.
SELECT core._ensure_column_online(
  'installation_account','convergence_next_epoch','bigint DEFAULT 0 NOT NULL');
-- EXPAND first: prove the future identity is unique while the legacy account-only PK and all old ABIs are still
-- live. The contract step (dropping the old PK) appears only after the policy writer + legacy claim/finish/fail
-- ABIs below have been replaced with policy-sentinel-aware bodies.
-- Invalid-shell cleanup is centralized in 05_online_index_repair.sql, including the narrowly safe UNIQUE
-- expansion exception while a stronger/equivalent PRIMARY KEY remains live.
CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS policy_refresh_outbox_identity_uq
  ON core.policy_refresh_outbox (account_id,request_kind,repository_id);
DO $$ BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
     WHERE conrelid='core.policy_refresh_outbox'::regclass
       AND conname='policy_refresh_outbox_request_shape'
  ) THEN
    ALTER TABLE core.policy_refresh_outbox ADD CONSTRAINT policy_refresh_outbox_request_shape CHECK (
      (request_kind='policy' AND repository_id='' AND repo='' AND branch='' AND target_sha IS NULL)
      OR
      (request_kind='graph'
       AND length(repository_id) BETWEEN 1 AND 32 AND repository_id ~ '^[1-9][0-9]*$'
       AND length(repo) BETWEEN 1 AND 512
       AND length(branch) BETWEEN 1 AND 512
       AND target_sha IS NOT NULL AND length(target_sha) BETWEEN 7 AND 64
       AND target_sha ~ '^[0-9a-f]+$')) NOT VALID;
  END IF;
END $$;
DO $$ BEGIN
  IF EXISTS (
    SELECT 1 FROM pg_constraint
     WHERE conrelid='core.policy_refresh_outbox'::regclass
       AND conname='policy_refresh_outbox_request_shape'
       AND NOT convalidated
  ) THEN
    ALTER TABLE core.policy_refresh_outbox
      VALIDATE CONSTRAINT policy_refresh_outbox_request_shape;
  END IF;
END $$;
DO $$ BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
     WHERE conrelid='core.policy_refresh_outbox'::regclass
       AND conname='policy_refresh_outbox_terminal_reason_ok'
  ) THEN
    ALTER TABLE core.policy_refresh_outbox ADD CONSTRAINT policy_refresh_outbox_terminal_reason_ok
      CHECK (terminal_reason IS NULL OR (request_kind='graph' AND terminal_reason='quota_paused'))
      NOT VALID;
  END IF;
END $$;
DO $$ BEGIN
  IF EXISTS (
    SELECT 1 FROM pg_constraint
     WHERE conrelid='core.policy_refresh_outbox'::regclass
       AND conname='policy_refresh_outbox_terminal_reason_ok'
       AND NOT convalidated
  ) THEN
    ALTER TABLE core.policy_refresh_outbox
      VALIDATE CONSTRAINT policy_refresh_outbox_terminal_reason_ok;
  END IF;
END $$;
DO $$ BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
     WHERE conrelid='core.policy_refresh_outbox'::regclass
       AND conname='policy_refresh_outbox_cursor_len'
  ) THEN
    ALTER TABLE core.policy_refresh_outbox
      ADD CONSTRAINT policy_refresh_outbox_cursor_len CHECK (
        length(policy_cursor_repo)<=512 AND length(policy_cursor_branch)<=512
        AND length(change_cursor)<=120) NOT VALID;
  END IF;
END $$;
DO $$ BEGIN
  IF EXISTS (
    SELECT 1 FROM pg_constraint
     WHERE conrelid='core.policy_refresh_outbox'::regclass
       AND conname='policy_refresh_outbox_cursor_len' AND NOT convalidated
  ) THEN
    ALTER TABLE core.policy_refresh_outbox
      VALIDATE CONSTRAINT policy_refresh_outbox_cursor_len;
  END IF;
END $$;
DO $$ BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
     WHERE conrelid='core.policy_refresh_outbox'::regclass
       AND conname='policy_refresh_outbox_onboarding_shape'
  ) THEN
    ALTER TABLE core.policy_refresh_outbox
      ADD CONSTRAINT policy_refresh_outbox_onboarding_shape CHECK (
        onboarding_index BETWEEN 0 AND 300
        AND (onboarding_plan IS NULL OR cardinality(onboarding_plan)<=300)
        AND (onboarding_plan IS NULL OR onboarding_index<=cardinality(onboarding_plan))
        AND (onboarding_plan IS NOT NULL OR (
          onboarding_index=0 AND NOT onboarding_truncated AND NOT onboarding_watching_done))
        AND (NOT onboarding_watching_done OR (
          onboarding_plan IS NOT NULL AND onboarding_index=cardinality(onboarding_plan)))
        AND (NOT onboarding_head_pending OR (
          onboarding_pending AND request_kind='graph'
          AND onboarding_plan IS NULL AND onboarding_index=0
          AND NOT onboarding_truncated AND NOT onboarding_watching_done))
        AND (branch<>'__veripsa_onboarding_head__' OR target_sha<>'0000000'
          OR onboarding_head_pending)
        AND (onboarding_pending OR (
          NOT onboarding_head_pending AND onboarding_plan IS NULL AND onboarding_index=0
          AND NOT onboarding_truncated AND NOT onboarding_watching_done))) NOT VALID;
  END IF;
END $$;
DO $$ BEGIN
  IF EXISTS (
    SELECT 1 FROM pg_constraint
     WHERE conrelid='core.policy_refresh_outbox'::regclass
       AND conname='policy_refresh_outbox_onboarding_shape' AND NOT convalidated
  ) THEN
    ALTER TABLE core.policy_refresh_outbox
      VALIDATE CONSTRAINT policy_refresh_outbox_onboarding_shape;
  END IF;
END $$;
DO $$ BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
     WHERE conrelid='core.policy_refresh_outbox'::regclass
       AND conname='policy_refresh_outbox_onboarding_active'
  ) THEN
    ALTER TABLE core.policy_refresh_outbox
      ADD CONSTRAINT policy_refresh_outbox_onboarding_active CHECK (
        NOT onboarding_pending OR (request_kind='graph' AND done_at IS NULL)) NOT VALID;
  END IF;
END $$;
DO $$ BEGIN
  IF EXISTS (
    SELECT 1 FROM pg_constraint
     WHERE conrelid='core.policy_refresh_outbox'::regclass
       AND conname='policy_refresh_outbox_onboarding_active' AND NOT convalidated
  ) THEN
    ALTER TABLE core.policy_refresh_outbox
      VALIDATE CONSTRAINT policy_refresh_outbox_onboarding_active;
  END IF;
END $$;
-- the drain scan reads the pending rows oldest-first; partial index keeps it cheap as done rows accumulate.
CREATE INDEX CONCURRENTLY IF NOT EXISTS policy_refresh_outbox_pending
  ON core.policy_refresh_outbox (enqueued_at)
  WHERE done_at IS NULL;
CREATE INDEX CONCURRENTLY IF NOT EXISTS policy_refresh_account_policy_due
  ON core.installation_account (policy_refresh_due_at,account_id)
  WHERE revoked_at IS NULL AND policy_refresh_due_at IS NOT NULL;
CREATE INDEX CONCURRENTLY IF NOT EXISTS policy_refresh_account_graph_due
  ON core.installation_account (graph_refresh_due_at,account_id)
  WHERE revoked_at IS NULL AND graph_refresh_due_at IS NOT NULL;
CREATE INDEX CONCURRENTLY IF NOT EXISTS policy_refresh_account_legacy_graph_due
  ON core.installation_account (legacy_graph_refresh_due_at,account_id)
  WHERE revoked_at IS NULL AND legacy_graph_refresh_due_at IS NOT NULL;
-- account_id is the scheduler's stable tenant key while installation_id is only the routing PK. Every enqueue,
-- lease terminal, offboard repair, and bounded candidate join addresses this key; without this index each one
-- silently scans every tenant even though the queue probes themselves are indexed.
CREATE INDEX CONCURRENTLY IF NOT EXISTS policy_refresh_account_route
  ON core.installation_account (account_id)
  INCLUDE (installation_id);
CREATE INDEX CONCURRENTLY IF NOT EXISTS policy_refresh_account_convergence_due
  ON core.installation_account (
    (LEAST(policy_refresh_due_at,graph_refresh_due_at)),account_id)
  WHERE revoked_at IS NULL
    AND (policy_refresh_due_at IS NOT NULL OR graph_refresh_due_at IS NOT NULL);
CREATE INDEX CONCURRENTLY IF NOT EXISTS policy_refresh_account_claimed
  ON core.installation_account (convergence_claimed_until,account_id)
  WHERE revoked_at IS NULL AND convergence_claimed_until IS NOT NULL;
CREATE INDEX CONCURRENTLY IF NOT EXISTS policy_refresh_account_graph_claimed
  ON core.installation_account (account_id)
  INCLUDE (convergence_graph_claim_count)
  WHERE revoked_at IS NULL AND convergence_graph_claim_count>0;
CREATE INDEX CONCURRENTLY IF NOT EXISTS policy_refresh_account_exception_depth
  ON core.installation_account (account_id)
  INCLUDE (convergence_retry_exhausted_count,convergence_quota_deferred_count)
  WHERE revoked_at IS NULL
    AND (convergence_retry_exhausted_count>0 OR convergence_quota_deferred_count>0);
CREATE INDEX CONCURRENTLY IF NOT EXISTS policy_refresh_account_stall_started
  ON core.installation_account (convergence_stall_started_at,account_id)
  WHERE revoked_at IS NULL AND convergence_stall_started_at IS NOT NULL;
-- One-time rollout bridge.  It is empty once every pre-column unfinished
-- ordinary request has published its original enqueue wall to the router.
CREATE INDEX CONCURRENTLY IF NOT EXISTS policy_refresh_account_stall_missing
  ON core.installation_account (account_id)
  WHERE revoked_at IS NULL AND convergence_stall_started_at IS NULL
    AND (convergence_pending_count>0 OR convergence_retry_exhausted_count>0);
CREATE INDEX CONCURRENTLY IF NOT EXISTS policy_refresh_account_schema_bridge
  ON core.installation_account (account_id)
  WHERE convergence_schema_version<1;
CREATE INDEX CONCURRENTLY IF NOT EXISTS policy_refresh_account_schema_bridge_v2
  ON core.installation_account (account_id)
  WHERE convergence_schema_version<2;
CREATE INDEX CONCURRENTLY IF NOT EXISTS policy_refresh_outbox_unfinished_due
  ON core.policy_refresh_outbox (
    account_id,request_kind,(COALESCE(not_before,enqueued_at)),repository_id)
  WHERE done_at IS NULL;
CREATE INDEX CONCURRENTLY IF NOT EXISTS policy_refresh_outbox_unfinished_enqueued
  ON core.policy_refresh_outbox (account_id,enqueued_at,request_kind,repository_id)
  WHERE done_at IS NULL AND terminal_reason IS NULL;
-- Fixed five-attempt runnable subset. request_kind is fixed in every per-lane probe, so this ordering serves one
-- LIMIT-1 lookup regardless of a fat tenant's completed/exhausted history.
CREATE INDEX CONCURRENTLY IF NOT EXISTS policy_refresh_outbox_claimable_due
  ON core.policy_refresh_outbox (
    account_id,request_kind,(COALESCE(not_before,enqueued_at)),repository_id)
  WHERE done_at IS NULL AND attempts<5;
-- Poison-isolated rows stay autonomously recoverable without polluting the hot runnable index. Their bounded
-- not_before backoff makes this partial index normally cold and lets one LIMIT-1 account probe wake them exactly
-- when due; deterministic failures cannot spin or monopolize either worker.
CREATE INDEX CONCURRENTLY IF NOT EXISTS policy_refresh_outbox_slow_retry_due
  ON core.policy_refresh_outbox (
    account_id,request_kind,(COALESCE(not_before,enqueued_at)),repository_id)
  WHERE done_at IS NULL AND attempts>=5;
-- the moat PATTERN (FORCE RLS + tenant_isolation + forgery block) applied uniformly — the SAME block 60_saas.sql
-- runs for core.policy. Not append-only (mutable operational state). A hot-deploy owner-repair guard first so a
-- table created by a member-of-migrator deploy role is re-owned; then only-create-missing-pieces so a re-apply on
-- a live service never re-takes a table DDL lock.
DO $$
DECLARE t text := 'policy_refresh_outbox';
BEGIN
  IF EXISTS (
    SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
     WHERE n.nspname = 'core' AND c.relname = t AND c.relowner <> 'veripsa_migrator'::regrole
  ) THEN
    EXECUTE format('ALTER TABLE core.%I OWNER TO veripsa_migrator', t);
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM pg_class c WHERE c.oid = format('core.%I', t)::regclass AND c.relrowsecurity
  ) THEN
    EXECUTE format('ALTER TABLE core.%I ENABLE ROW LEVEL SECURITY', t);
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM pg_class c WHERE c.oid = format('core.%I', t)::regclass AND c.relforcerowsecurity
  ) THEN
    EXECUTE format('ALTER TABLE ONLY core.%I FORCE ROW LEVEL SECURITY', t);
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM pg_policy p WHERE p.polrelid = format('core.%I', t)::regclass AND p.polname = 'tenant_isolation'
  ) THEN
    EXECUTE format('CREATE POLICY tenant_isolation ON core.%I USING (account_id = current_setting(''core.current_account'', true)) WITH CHECK (account_id = current_setting(''core.current_account'', true))', t);
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM pg_trigger tr WHERE tr.tgrelid = format('core.%I', t)::regclass
       AND tr.tgname = format('trg_governed_%s', t) AND NOT tr.tgisinternal
  ) THEN
    EXECUTE format('CREATE TRIGGER trg_governed_%I BEFORE INSERT OR UPDATE ON core.%I FOR EACH ROW EXECUTE FUNCTION core.assert_governed_write()', t, t);
  END IF;
END $$;

DO $$
DECLARE t text := 'graph_convergence_lease';
BEGIN
  IF EXISTS (
    SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
     WHERE n.nspname='core' AND c.relname=t AND c.relowner<>'veripsa_migrator'::regrole
  ) THEN
    EXECUTE format('ALTER TABLE core.%I OWNER TO veripsa_migrator',t);
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM pg_class c WHERE c.oid=format('core.%I',t)::regclass AND c.relrowsecurity
  ) THEN
    EXECUTE format('ALTER TABLE core.%I ENABLE ROW LEVEL SECURITY',t);
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM pg_class c WHERE c.oid=format('core.%I',t)::regclass AND c.relforcerowsecurity
  ) THEN
    EXECUTE format('ALTER TABLE ONLY core.%I FORCE ROW LEVEL SECURITY',t);
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM pg_policy p
     WHERE p.polrelid=format('core.%I',t)::regclass AND p.polname='tenant_isolation'
  ) THEN
    EXECUTE format(
      'CREATE POLICY tenant_isolation ON core.%I '
      'USING (account_id=current_setting(''core.current_account'',true)) '
      'WITH CHECK (account_id=current_setting(''core.current_account'',true))',t);
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM pg_trigger tr
     WHERE tr.tgrelid=format('core.%I',t)::regclass
       AND tr.tgname=format('trg_governed_%s',t) AND NOT tr.tgisinternal
  ) THEN
    EXECUTE format(
      'CREATE TRIGGER trg_governed_%I BEFORE INSERT OR UPDATE ON core.%I '
      'FOR EACH ROW EXECUTE FUNCTION core.assert_governed_write()',t,t);
  END IF;
END $$;

-- ── ENQUEUE (in the policy-write txn) ────────────────────────────────────────────────────────────────────
-- _enqueue_policy_refresh: the policy writers call this immediately after committing a policy change, in the
-- SAME transaction (atomic + rollback-safe). INTERNAL — SECURITY DEFINER (migrator), no grant: it is invoked
-- ONLY by the migrator-owned setter functions (which run as the definer, so they may call it as the owner). It
-- pins the target account (save/restore, txn-local) so the governed write lands under RLS WITH CHECK, then
-- UPSERTs the coalescing row (bumping the monotonic epoch + resetting the row to pending). Returns the new
-- epoch, or NULL when there is NOTHING to refresh.
--
-- LIVE-INSTALL GATE (why some writes no-op): a refresh only makes sense for an account that has OPEN PRs on a
-- LIVE installation. So we enqueue ONLY when the account currently has a live installation route (a non-RLS
-- read of the routing map, safe inside this SECURITY DEFINER fn — NO GitHub call). This makes the OWNER tuning
-- setters (free line / plan limits / dev-exempt — all written under the reserved owner account, which has NO
-- installation) a deliberate NO-OP here: a global owner knob affects every tenant's FUTURE (live-read)
-- evaluations, but proactively fanning a refresh out to EVERY tenant on every knob edit is a DIFFERENT,
-- unbounded operation that would violate G4's tenant-scoped + bounded-fan-out contract, so it is intentionally
-- out of scope. It also makes a write for an already-uninstalled account a no-op (nothing live to refresh).
CREATE OR REPLACE FUNCTION core._next_account_convergence_epoch(p_account text) RETURNS bigint
    LANGUAGE plpgsql SECURITY DEFINER SET search_path TO 'core','pg_catalog' AS $$
DECLARE v_epoch bigint;
BEGIN
  -- The non-RLS router row is the account serialization point. Allocation is one indexed row update regardless
  -- of how many historical repository coordinates the account has; enqueue therefore stays O(1).
  UPDATE core.installation_account
     SET convergence_next_epoch=convergence_next_epoch+1
   WHERE account_id=p_account
   RETURNING convergence_next_epoch INTO v_epoch;
  IF v_epoch IS NULL THEN
    RAISE EXCEPTION 'account convergence route is missing' USING ERRCODE='23503';
  END IF;
  RETURN v_epoch;
END $$;
ALTER FUNCTION core._next_account_convergence_epoch(text) OWNER TO veripsa_migrator;
REVOKE ALL ON FUNCTION core._next_account_convergence_epoch(text) FROM PUBLIC;

CREATE OR REPLACE FUNCTION core._enqueue_policy_refresh(p_account text) RETURNS bigint
    LANGUAGE plpgsql SECURITY DEFINER SET search_path TO 'core','pg_catalog' AS $$
DECLARE
  v_prev text; v_epoch bigint; v_due timestamptz:=now();
  v_had boolean:=false; v_old_done timestamptz; v_old_attempts int; v_old_reason text;
BEGIN
  IF p_account IS NULL OR btrim(p_account) = '' THEN RETURN NULL; END IF;
  PERFORM 1 FROM core.installation_account
   WHERE account_id=p_account AND revoked_at IS NULL
   FOR UPDATE;
  IF NOT FOUND THEN
    RETURN NULL;
  END IF;
  v_prev := current_setting('core.current_account', true);
  PERFORM set_config('core.current_account',p_account,true);
  v_epoch := core._next_account_convergence_epoch(p_account);
  SELECT done_at,attempts,terminal_reason
    INTO v_old_done,v_old_attempts,v_old_reason
    FROM core.policy_refresh_outbox
   WHERE account_id=p_account AND request_kind='policy' AND repository_id=''
   FOR UPDATE;
  v_had:=FOUND;
  PERFORM core.mark_governed_write('policy_refresh_outbox');
  INSERT INTO core.policy_refresh_outbox(
      account_id,request_kind,repository_id,repo,branch,target_sha,policy_epoch,enqueued_at)
  VALUES (p_account,'policy','','','',NULL,v_epoch,v_due)
  ON CONFLICT (account_id,request_kind,repository_id) DO UPDATE
    SET repo='',branch='',target_sha=NULL,policy_epoch=EXCLUDED.policy_epoch,enqueued_at=v_due,
        not_before=NULL,terminal_reason=NULL,
        policy_cursor_repo='',policy_cursor_branch='',change_cursor='',
        surface_dirty=false,
        claimed_at=NULL,claimed_by=NULL,done_at=NULL,attempts=0,last_error=NULL
  RETURNING policy_epoch INTO v_epoch;
  UPDATE core.installation_account
     SET policy_refresh_due_at=v_due,
         convergence_stall_started_at=LEAST(
           COALESCE(convergence_stall_started_at,v_due),v_due),
         convergence_pending_count=GREATEST(0,convergence_pending_count
           +CASE WHEN NOT v_had OR v_old_done IS NOT NULL OR v_old_reason IS NOT NULL
                       OR v_old_attempts>=5 THEN 1 ELSE 0 END),
         convergence_retry_exhausted_count=GREATEST(0,convergence_retry_exhausted_count
           -CASE WHEN v_had AND v_old_done IS NULL AND v_old_reason IS NULL
                       AND v_old_attempts>=5 THEN 1 ELSE 0 END),
         convergence_quota_deferred_count=GREATEST(0,convergence_quota_deferred_count
           -CASE WHEN v_had AND v_old_done IS NULL AND v_old_reason='quota_paused'
                 THEN 1 ELSE 0 END)
   WHERE account_id=p_account AND revoked_at IS NULL;
  PERFORM set_config('core.current_account',COALESCE(v_prev,''),true);
  RETURN v_epoch;
END $$;
ALTER FUNCTION core._enqueue_policy_refresh(text) OWNER TO veripsa_migrator;
REVOKE ALL ON FUNCTION core._enqueue_policy_refresh(text) FROM PUBLIC;

-- Central graph-queue lifecycle fence.  The webhook/worker preflight happens
-- on a different statement or connection from the durable enqueue, so it
-- cannot by itself stop an offboard which commits in that gap.  Take the
-- lifecycle-global lock order here and retain it through the caller's route
-- and outbox mutation:
--
--   stable repository id -> mutable repo coordinate -> account(shared)
--   -> installation route -> graph outbox
--
-- Offboard takes the same stable-id/repo keys before its exclusive account
-- fence and queue cleanup.  Therefore either this transaction queues before
-- offboard (and offboard removes it), or it observes the tombstone and
-- refuses; it can never recreate a row after cleanup.  Wake must call this
-- helper before looking at an unfinished row as well, otherwise its early
-- return would preserve stale work and its route lock would invert the order
-- against offboard.
CREATE OR REPLACE FUNCTION core._lock_live_graph_refresh_coordinate_with_authority(
    p_repo text,p_repository_id text
) RETURNS text
    LANGUAGE plpgsql SECURITY DEFINER SET search_path TO 'core','pg_catalog' AS $$
DECLARE
  v_account text; v_repo text; v_id text;
BEGIN
  v_repo:=left(NULLIF(btrim(COALESCE(p_repo,'')),''),512);
  v_id:=NULLIF(btrim(COALESCE(p_repository_id,'')),'');
  IF v_repo IS NULL
     OR v_id IS NULL OR length(v_id)>32 OR v_id !~ '^[1-9][0-9]*$' THEN
    RAISE EXCEPTION 'invalid graph refresh lifecycle coordinate'
      USING ERRCODE='22023';
  END IF;
  SELECT account INTO v_account
    FROM core.establish_session_write_context() AS c(agent,account);
  PERFORM pg_advisory_xact_lock(
    hashtext('github-repository-id'),hashtext(v_id));
  PERFORM pg_advisory_xact_lock(
    hashtext(CASE WHEN v_account LIKE 'ACCT-GH-%'
                  THEN substr(v_account,9) ELSE v_account END),
    hashtext(v_repo));
  PERFORM core.assert_account_live_with_authority();
  IF NOT core.repository_account_onboarding_allowed_with_authority(
      v_repo,v_id)
     OR EXISTS (
       SELECT 1 FROM core.repository_lifecycle_tombstone
        WHERE account_id=v_account AND repository_id=v_id
          AND superseded_at IS NULL
     ) THEN
    RAISE EXCEPTION 'graph refresh lifecycle coordinate is not live'
      USING ERRCODE='55000';
  END IF;
  RETURN v_account;
END $$;
ALTER FUNCTION core._lock_live_graph_refresh_coordinate_with_authority(text,text)
  OWNER TO veripsa_migrator;
REVOKE ALL ON FUNCTION core._lock_live_graph_refresh_coordinate_with_authority(text,text)
  FROM PUBLIC, veripsa_app, veripsa_writer;

-- Graph event enqueue. The App connection must already be pinned to the authenticated installation. The stable
-- repository id is the identity; a rename updates `repo` on the same row. The exact target SHA is latest-wins.
-- This function does not wake the policy sentinel: the graph turn itself refreshes/posts this repo after the
-- graph transaction commits, avoiding a duplicate account-wide render.
CREATE OR REPLACE FUNCTION core.enqueue_graph_refresh_with_authority(
    p_repo text,p_branch text,p_target_sha text,p_repository_id text
) RETURNS bigint
    LANGUAGE plpgsql SECURITY DEFINER SET search_path TO 'core','pg_catalog' AS $$
DECLARE
  v_account text; v_repo text; v_branch text; v_sha text; v_id text; v_epoch bigint;
  v_had boolean:=false; v_old_done timestamptz; v_old_attempts int; v_old_reason text;
  v_old_repo text; v_old_branch text; v_old_target text; v_old_epoch bigint;
  v_due timestamptz:=now();
BEGIN
  v_repo := left(NULLIF(btrim(COALESCE(p_repo,'')),''),512);
  v_branch := left(NULLIF(btrim(COALESCE(p_branch,'')),''),512);
  v_sha := lower(NULLIF(btrim(COALESCE(p_target_sha,'')),''));
  v_id := NULLIF(btrim(COALESCE(p_repository_id,'')),'');
  IF v_repo IS NULL OR v_branch IS NULL
     OR v_sha IS NULL OR length(v_sha) NOT BETWEEN 7 AND 64 OR v_sha !~ '^[0-9a-f]+$'
     OR v_id IS NULL OR length(v_id)>32 OR v_id !~ '^[1-9][0-9]*$' THEN
    RAISE EXCEPTION 'invalid graph refresh coordinate' USING ERRCODE='22023';
  END IF;
  v_account:=core._lock_live_graph_refresh_coordinate_with_authority(
    v_repo,v_id);
  PERFORM 1 FROM core.installation_account
   WHERE account_id=v_account AND revoked_at IS NULL
   FOR UPDATE;
  IF NOT FOUND THEN
    RETURN NULL;
  END IF;
  SELECT done_at,attempts,terminal_reason,repo,branch,target_sha,policy_epoch
    INTO v_old_done,v_old_attempts,v_old_reason,v_old_repo,v_old_branch,v_old_target,v_old_epoch
    FROM core.policy_refresh_outbox
   WHERE account_id=v_account AND request_kind='graph' AND repository_id=v_id
   FOR UPDATE;
  v_had:=FOUND;
  -- Latest-wins is idempotent for the exact same unfinished fact.  In
  -- particular, repeated structural PR events often name the same protected
  -- base SHA with force=True.  They must not advance the request epoch or
  -- clear the live slot: doing so lets a low-authority event stream
  -- perpetually supersede the extractor which is already converging that
  -- exact repository object/branch/target.
  IF v_had AND v_old_done IS NULL
     AND v_old_repo=v_repo AND v_old_branch=v_branch AND v_old_target=v_sha THEN
    RETURN v_old_epoch;
  END IF;
  v_epoch := core._next_account_convergence_epoch(v_account);
  PERFORM core.mark_governed_write('policy_refresh_outbox');
  INSERT INTO core.policy_refresh_outbox AS existing(
      account_id,request_kind,repository_id,repo,branch,target_sha,policy_epoch,
      enqueued_at,surface_dirty)
  VALUES (v_account,'graph',v_id,v_repo,v_branch,v_sha,v_epoch,v_due,true)
  ON CONFLICT (account_id,request_kind,repository_id) DO UPDATE
    SET repo=EXCLUDED.repo,branch=EXCLUDED.branch,target_sha=EXCLUDED.target_sha,
        policy_epoch=EXCLUDED.policy_epoch,enqueued_at=v_due,not_before=NULL,terminal_reason=NULL,
        policy_cursor_repo='',policy_cursor_branch='',change_cursor='',
        surface_dirty=true,onboarding_head_pending=false,
        -- A new protected HEAD invalidates the graph proof but not the immutable inventory of PR numbers nor its
        -- exact cursor. Preserve frozen onboarding progress so a busy repository cannot restart at PR 1 forever;
        -- the new epoch must strictly rebuild this target before advancing the next planned PR. Watching is tied
        -- to the old HEAD and is the only phase receipt deliberately cleared.
        onboarding_plan=CASE WHEN existing.onboarding_pending
                                  AND existing.branch=EXCLUDED.branch
                             THEN existing.onboarding_plan ELSE NULL END,
        onboarding_index=CASE WHEN existing.onboarding_pending
                                   AND existing.branch=EXCLUDED.branch
                              THEN existing.onboarding_index ELSE 0 END,
        onboarding_truncated=CASE WHEN existing.onboarding_pending
                                       AND existing.branch=EXCLUDED.branch
                                  THEN existing.onboarding_truncated ELSE false END,
        onboarding_watching_done=false,
        claimed_at=NULL,claimed_by=NULL,done_at=NULL,attempts=0,last_error=NULL
  RETURNING policy_epoch INTO v_epoch;
  UPDATE core.installation_account
     SET graph_refresh_due_at=LEAST(COALESCE(graph_refresh_due_at,v_due),v_due),
         convergence_stall_started_at=LEAST(
           COALESCE(convergence_stall_started_at,v_due),v_due),
         convergence_pending_count=GREATEST(0,convergence_pending_count
           +CASE WHEN NOT v_had OR v_old_done IS NOT NULL OR v_old_reason IS NOT NULL
                       OR v_old_attempts>=5 THEN 1 ELSE 0 END),
         convergence_retry_exhausted_count=GREATEST(0,convergence_retry_exhausted_count
           -CASE WHEN v_had AND v_old_done IS NULL AND v_old_reason IS NULL
                       AND v_old_attempts>=5 THEN 1 ELSE 0 END),
         convergence_quota_deferred_count=GREATEST(0,convergence_quota_deferred_count
           -CASE WHEN v_had AND v_old_done IS NULL AND v_old_reason='quota_paused'
                 THEN 1 ELSE 0 END)
   WHERE account_id=v_account AND revoked_at IS NULL;
  -- Latest-wins may replace the row that owned the old minimum. Recompute with two indexed LIMIT-1 probes so
  -- the account cannot retain an artificially ancient due pointer and jump ahead of genuinely older tenants.
  PERFORM core._sync_account_convergence_due(v_account,5,300,false);
  RETURN v_epoch;
END $$;
ALTER FUNCTION core.enqueue_graph_refresh_with_authority(text,text,text,text) OWNER TO veripsa_migrator;
REVOKE ALL ON FUNCTION core.enqueue_graph_refresh_with_authority(text,text,text,text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION core.enqueue_graph_refresh_with_authority(text,text,text,text) TO veripsa_app;

-- Installation-only enqueue. Live delivery performs no GitHub I/O: lifecycle activation and this durable phase
-- token commit together, then the convergence worker resolves the authoritative default-branch HEAD. The reserved
-- branch/SHA are never passed to graph freshness/extraction. A duplicate while onboarding is already pending is an
-- exact no-op, preserving its immutable plan/cursor.
CREATE OR REPLACE FUNCTION core.enqueue_repository_onboarding_with_authority(
    p_repo text,p_repository_id text
) RETURNS bigint
    LANGUAGE plpgsql SECURITY DEFINER SET search_path TO 'core','pg_catalog' AS $$
DECLARE
  v_account text; v_repo text; v_id text; v_epoch bigint;
  v_had boolean:=false; v_done timestamptz; v_attempts int; v_reason text;
  v_was_pending boolean; v_old_epoch bigint; v_due timestamptz:=clock_timestamp();
  v_has_live_lease boolean:=false;
  v_installation_generation text; v_installation_generation_at timestamptz;
BEGIN
  v_repo:=left(NULLIF(btrim(COALESCE(p_repo,'')),''),512);
  v_id:=NULLIF(btrim(COALESCE(p_repository_id,'')),'');
  IF v_repo IS NULL OR v_id IS NULL OR length(v_id)>32 OR v_id !~ '^[1-9][0-9]*$' THEN
    RAISE EXCEPTION 'invalid repository onboarding coordinate' USING ERRCODE='22023';
  END IF;
  v_account:=core._lock_live_graph_refresh_coordinate_with_authority(v_repo,v_id);
  SELECT github_installation_id,github_installation_created_at
    INTO v_installation_generation,v_installation_generation_at
    FROM core.installation_account
   WHERE account_id=v_account AND revoked_at IS NULL
   FOR UPDATE;
  IF NOT FOUND THEN RETURN NULL; END IF;
  -- A live onboarding event has already passed exact installation-generation admission. Refuse to mint a row
  -- without that tuple: the worker must route directly to one installation and never fall back to an O(fleet)
  -- account scan or an obsolete reinstall client.
  IF v_installation_generation IS NULL
     OR length(v_installation_generation) NOT BETWEEN 1 AND 64
     OR v_installation_generation !~ '^[1-9][0-9]*$'
     OR v_installation_generation_at IS NULL THEN
    RAISE EXCEPTION 'repository onboarding needs an admitted installation generation'
      USING ERRCODE='55000';
  END IF;
  SELECT done_at,attempts,terminal_reason,onboarding_pending,policy_epoch
    INTO v_done,v_attempts,v_reason,v_was_pending,v_old_epoch
    FROM core.policy_refresh_outbox
   WHERE account_id=v_account AND request_kind='graph'
     AND repository_id=v_id
   FOR UPDATE;
  v_had:=FOUND;
  IF v_had AND v_done IS NULL AND COALESCE(v_was_pending,false) THEN
    RETURN v_old_epoch;
  END IF;
  IF v_had AND v_done IS NULL THEN
    SELECT EXISTS (
      SELECT 1 FROM core.graph_convergence_lease
       WHERE account_id=v_account AND repository_id=v_id
         AND request_epoch=v_old_epoch AND claimed_until>=clock_timestamp())
      INTO v_has_live_lease;
  END IF;
  IF v_has_live_lease THEN
    -- A rolling predecessor may already own this ordinary graph turn. Keep only its immutable LEASE snapshot;
    -- supersede the OUTBOX coordinate with a fresh epoch and the reserved HEAD phase.  Consequently even an old
    -- /6 worker (which does not understand onboarding_head_pending) loses its exact preflight CAS before clone /
    -- extraction.  Its stale finish/retry can delete only its own old lease generation and can never consume or
    -- rewrite the new onboarding request.
    v_epoch:=core._next_account_convergence_epoch(v_account);
    PERFORM core.mark_governed_write('policy_refresh_outbox');
    UPDATE core.policy_refresh_outbox
       SET repo=v_repo,branch='__veripsa_onboarding_head__',target_sha='0000000',
           policy_epoch=v_epoch,enqueued_at=v_due,
           onboarding_pending=true,onboarding_head_pending=true,onboarding_plan=NULL,
           onboarding_index=0,onboarding_truncated=false,onboarding_watching_done=false,
           policy_cursor_repo='',policy_cursor_branch='',change_cursor='',
           surface_dirty=true,claimed_at=NULL,claimed_by=NULL,done_at=NULL,
           attempts=0,last_error=NULL,not_before=NULL,terminal_reason=NULL
     WHERE account_id=v_account AND request_kind='graph'
       AND repository_id=v_id AND policy_epoch=v_old_epoch AND done_at IS NULL;
    UPDATE core.installation_account
       SET convergence_pending_count=GREATEST(0,convergence_pending_count
             -CASE WHEN v_reason IS NULL AND COALESCE(v_attempts,0)<5 THEN 1 ELSE 0 END+1),
           convergence_retry_exhausted_count=GREATEST(0,convergence_retry_exhausted_count
             -CASE WHEN v_reason IS NULL AND COALESCE(v_attempts,0)>=5 THEN 1 ELSE 0 END),
           convergence_quota_deferred_count=GREATEST(0,convergence_quota_deferred_count
             -CASE WHEN v_reason='quota_paused' THEN 1 ELSE 0 END),
           convergence_stall_started_at=LEAST(
             COALESCE(convergence_stall_started_at,v_due),v_due)
    WHERE account_id=v_account AND revoked_at IS NULL;
    PERFORM core._sync_account_convergence_due(v_account,5,300,false);
    RETURN v_epoch;
  END IF;
  v_epoch:=core._next_account_convergence_epoch(v_account);
  PERFORM core.mark_governed_write('policy_refresh_outbox');
  INSERT INTO core.policy_refresh_outbox(
      account_id,request_kind,repository_id,repo,branch,target_sha,policy_epoch,
      enqueued_at,surface_dirty,onboarding_pending,onboarding_head_pending)
  VALUES (
      v_account,'graph',v_id,v_repo,'__veripsa_onboarding_head__','0000000',v_epoch,
      v_due,true,true,true)
  ON CONFLICT (account_id,request_kind,repository_id) DO UPDATE
    SET repo=EXCLUDED.repo,branch=EXCLUDED.branch,target_sha=EXCLUDED.target_sha,
        policy_epoch=EXCLUDED.policy_epoch,enqueued_at=v_due,
        not_before=NULL,terminal_reason=NULL,
        policy_cursor_repo='',policy_cursor_branch='',change_cursor='',surface_dirty=true,
        onboarding_pending=true,onboarding_head_pending=true,onboarding_plan=NULL,
        onboarding_index=0,onboarding_truncated=false,onboarding_watching_done=false,
        claimed_at=NULL,claimed_by=NULL,done_at=NULL,attempts=0,last_error=NULL
  RETURNING policy_epoch INTO v_epoch;
  UPDATE core.installation_account
     SET convergence_pending_count=GREATEST(0,convergence_pending_count
           +CASE WHEN NOT v_had OR v_done IS NOT NULL OR v_reason IS NOT NULL
                       OR COALESCE(v_attempts,0)>=5 THEN 1 ELSE 0 END),
         convergence_retry_exhausted_count=GREATEST(0,convergence_retry_exhausted_count
           -CASE WHEN v_had AND v_done IS NULL AND v_reason IS NULL
                       AND COALESCE(v_attempts,0)>=5 THEN 1 ELSE 0 END),
         convergence_quota_deferred_count=GREATEST(0,convergence_quota_deferred_count
           -CASE WHEN v_had AND v_done IS NULL AND v_reason='quota_paused' THEN 1 ELSE 0 END),
         convergence_stall_started_at=LEAST(
           COALESCE(convergence_stall_started_at,v_due),v_due)
   WHERE account_id=v_account AND revoked_at IS NULL;
  PERFORM core._sync_account_convergence_due(v_account,5,300,false);
  RETURN v_epoch;
END $$;
ALTER FUNCTION core.enqueue_repository_onboarding_with_authority(text,text)
  OWNER TO veripsa_migrator;
REVOKE ALL ON FUNCTION core.enqueue_repository_onboarding_with_authority(text,text)
  FROM PUBLIC;
GRANT EXECUTE ON FUNCTION core.enqueue_repository_onboarding_with_authority(text,text)
  TO veripsa_app;

-- A signed PR payload proves only a base candidate, not the repository's
-- current default-branch HEAD. It may wake a fully idle stable-id row. For an
-- unfinished row it sets only the coalescing surface_dirty latch: a delayed PR
-- can neither replace newer push/boot graph authority nor revoke its lease,
-- while an in-progress >30-PR cursor is guaranteed one later full pass.
CREATE OR REPLACE FUNCTION core.wake_graph_refresh_candidate_with_authority(
    p_repo text,p_branch text,p_target_sha text,p_repository_id text
) RETURNS bigint
    LANGUAGE plpgsql SECURITY DEFINER SET search_path TO 'core','pg_catalog' AS $$
DECLARE
  v_account text; v_repo text; v_branch text; v_target text; v_id text;
  v_epoch bigint; v_done timestamptz; v_surface_dirty boolean;
BEGIN
  v_repo:=left(NULLIF(btrim(COALESCE(p_repo,'')),''),512);
  v_branch:=left(NULLIF(btrim(COALESCE(p_branch,'')),''),512);
  v_target:=lower(NULLIF(btrim(COALESCE(p_target_sha,'')),''));
  v_id:=NULLIF(btrim(COALESCE(p_repository_id,'')),'');
  IF v_repo IS NULL OR v_branch IS NULL
     OR v_target IS NULL OR length(v_target) NOT BETWEEN 7 AND 64
     OR v_target !~ '^[0-9a-f]+$'
     OR v_id IS NULL OR length(v_id)>32 OR v_id !~ '^[1-9][0-9]*$' THEN
    RAISE EXCEPTION 'invalid graph refresh wake coordinate' USING ERRCODE='22023';
  END IF;
  v_account:=core._lock_live_graph_refresh_coordinate_with_authority(
    v_repo,v_id);
  PERFORM 1 FROM core.installation_account
   WHERE account_id=v_account AND revoked_at IS NULL
   FOR UPDATE;
  IF NOT FOUND THEN RETURN NULL; END IF;
  SELECT policy_epoch,done_at,surface_dirty INTO v_epoch,v_done,v_surface_dirty
    FROM core.policy_refresh_outbox
   WHERE account_id=v_account AND request_kind='graph' AND repository_id=v_id
   FOR UPDATE;
  IF FOUND AND v_done IS NULL THEN
    IF NOT COALESCE(v_surface_dirty,false) THEN
      PERFORM core.mark_governed_write('policy_refresh_outbox');
      UPDATE core.policy_refresh_outbox
         SET surface_dirty=true
       WHERE account_id=v_account AND request_kind='graph'
         AND repository_id=v_id AND policy_epoch=v_epoch AND done_at IS NULL;
      IF NOT FOUND THEN
        RAISE EXCEPTION 'graph surface wake lost its exact row'
          USING ERRCODE='40001';
      END IF;
    END IF;
    RETURN v_epoch;
  END IF;
  RETURN core.enqueue_graph_refresh_with_authority(
    v_repo,v_branch,v_target,v_id);
END $$;
ALTER FUNCTION core.wake_graph_refresh_candidate_with_authority(text,text,text,text)
  OWNER TO veripsa_migrator;
REVOKE ALL ON FUNCTION core.wake_graph_refresh_candidate_with_authority(text,text,text,text)
  FROM PUBLIC;
GRANT EXECUTE ON FUNCTION core.wake_graph_refresh_candidate_with_authority(text,text,text,text)
  TO veripsa_app;

-- Atomic state-returning wrapper for new workers. Keep the scalar bigint ABI
-- above for rolling predecessors, but let a structural PR distinguish an
-- ordinary unfinished refresh from one deliberately deferred by the free-tier
-- wall in the SAME SQL statement that performs the wake. The nested scalar
-- function retains its row lock until this wrapper returns, so a worker cannot
-- finish/supersede between the ACK and state observation. No push-owned
-- target/epoch/lease/cursor/not-before field is changed by the unfinished-row
-- path; only surface_dirty may coalesce a later full pass.
-- The result is bounded and content-free: one epoch plus one closed state.
CREATE OR REPLACE FUNCTION core.wake_graph_refresh_candidate_state_with_authority(
    p_repo text,p_branch text,p_target_sha text,p_repository_id text
) RETURNS jsonb
    LANGUAGE plpgsql SECURITY DEFINER SET search_path TO 'core','pg_catalog' AS $$
DECLARE
  v_account text; v_id text; v_epoch bigint; v_reason text; v_state text;
BEGIN
  v_id:=NULLIF(btrim(COALESCE(p_repository_id,'')),'');
  v_epoch:=core.wake_graph_refresh_candidate_with_authority(
    p_repo,p_branch,p_target_sha,p_repository_id);
  IF v_epoch IS NULL OR v_epoch<1 THEN
    RAISE EXCEPTION 'graph refresh wake state was not durably recorded' USING ERRCODE='55000';
  END IF;
  SELECT account INTO v_account
    FROM core.resolve_session_identity() AS c(agent,account);
  -- SECURITY DEFINER does not bypass FORCE RLS. Re-pin the independently
  -- resolved route so this read remains tenant-exact across future callers
  -- whose surrounding statement/transaction did not retain the session GUC.
  PERFORM set_config('core.current_account',v_account,true);
  SELECT terminal_reason,
         CASE WHEN terminal_reason='quota_paused' THEN 'quota_paused' ELSE 'queued' END
    INTO v_reason,v_state
    FROM core.policy_refresh_outbox
   WHERE account_id=v_account AND request_kind='graph'
     AND repository_id=v_id AND policy_epoch=v_epoch AND done_at IS NULL
   FOR UPDATE;
  IF NOT FOUND OR v_state NOT IN ('queued','quota_paused')
     OR (v_state='quota_paused' AND v_reason<>'quota_paused')
     OR (v_state='queued' AND v_reason IS NOT NULL) THEN
    RAISE EXCEPTION 'graph refresh wake state was not durably observed' USING ERRCODE='55000';
  END IF;
  RETURN jsonb_build_object(
    'request_epoch',v_epoch,'unfinished',true,'state',v_state);
END $$;
ALTER FUNCTION core.wake_graph_refresh_candidate_state_with_authority(text,text,text,text)
  OWNER TO veripsa_migrator;
REVOKE ALL ON FUNCTION core.wake_graph_refresh_candidate_state_with_authority(text,text,text,text)
  FROM PUBLIC;
GRANT EXECUTE ON FUNCTION core.wake_graph_refresh_candidate_state_with_authority(text,text,text,text)
  TO veripsa_app;

CREATE OR REPLACE FUNCTION core._graph_refresh_fulfilled(
    p_account text,p_repository_id text,p_repo text,p_branch text,p_target_sha text
) RETURNS boolean
    LANGUAGE sql STABLE SECURITY DEFINER SET search_path TO 'core','pg_catalog' AS $$
  SELECT EXISTS (
    SELECT 1 FROM core.graph_version
     WHERE account_id=p_account AND repo=p_repo AND branch=p_branch
       AND repo_id=p_repository_id AND commit_sha=p_target_sha
       AND extractor_version=core.current_extractor_version()
       AND semantic_ref_version=core.current_semantic_ref_version())
$$;
ALTER FUNCTION core._graph_refresh_fulfilled(text,text,text,text,text) OWNER TO veripsa_migrator;
REVOKE ALL ON FUNCTION core._graph_refresh_fulfilled(text,text,text,text,text) FROM PUBLIC;

-- Exact DB-local proof used after the onboarding PR plan has frozen. Those later fair turns must never clone or
-- extract the unchanged graph again; they verify that stable-id/branch/SHA still names a current-generation graph,
-- then perform at most one lightweight GitHub phase. The bounded observability only renders the final Watching
-- Check honestly and contains no source/path/body data.
CREATE OR REPLACE FUNCTION core.graph_onboarding_snapshot_with_authority(
    p_repo text,p_branch text,p_target_sha text,p_repository_id text
) RETURNS jsonb
    LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path TO 'core','pg_catalog' AS $$
DECLARE v_account text; v_result jsonb;
BEGIN
  SELECT account INTO v_account
    FROM core.resolve_session_identity() AS r(agent,account);
  PERFORM set_config('core.current_account',v_account,true);
  SELECT jsonb_build_object(
           'fulfilled',true,
           'files',COALESCE((gv.observability->>'input_file_count')::int,0),
           'edges',COALESCE(gv.edge_count,0),
           'over_cap',COALESCE((gv.observability->>'over_cap')::boolean,false))
    INTO v_result
    FROM core.graph_version gv
   WHERE gv.account_id=v_account AND gv.repo=p_repo AND gv.branch=p_branch
     AND gv.commit_sha=p_target_sha AND gv.repo_id=p_repository_id
     AND gv.extractor_version=core.current_extractor_version()
     AND gv.semantic_ref_version=core.current_semantic_ref_version();
  RETURN COALESCE(v_result,jsonb_build_object('fulfilled',false));
END $$;
ALTER FUNCTION core.graph_onboarding_snapshot_with_authority(text,text,text,text)
  OWNER TO veripsa_migrator;
REVOKE ALL ON FUNCTION core.graph_onboarding_snapshot_with_authority(text,text,text,text)
  FROM PUBLIC;
GRANT EXECUTE ON FUNCTION core.graph_onboarding_snapshot_with_authority(text,text,text,text)
  TO veripsa_app;

-- Recompute ONE account's two routing due-times after a terminal/retry transition. Each lookup is an ordered
-- LIMIT 1 on policy_refresh_outbox_unfinished_due: runtime cost does not grow with completed graph history.
-- A completed graph row is re-armed only by an explicit authoritative push/boot enqueue; convergence never
-- re-walks every completed repository merely to rediscover that fact. p_tail moves remaining work behind
-- accounts that were already waiting.
CREATE OR REPLACE FUNCTION core._sync_account_convergence_due(
    p_account text,p_max_attempts int,p_retry_seconds int,p_tail boolean
) RETURNS jsonb
    LANGUAGE plpgsql SECURITY DEFINER SET search_path TO 'core','pg_catalog' AS $$
DECLARE
  v_policy timestamptz; v_graph timestamptz; v_legacy_graph timestamptz;
  v_policy_n int:=0; v_graph_n int:=0; v_legacy_graph_has boolean:=false;
  v_stall timestamptz;
  v_graph_claims int:=0; v_graph_reclaim timestamptz;
  v_claim_horizon int:=LEAST(GREATEST(1,COALESCE(p_retry_seconds,300)),300);
BEGIN
  SELECT CASE
           WHEN q.claimed_at IS NULL THEN COALESCE(q.not_before,q.enqueued_at)
           ELSE q.claimed_at
                +make_interval(secs=>v_claim_horizon)
         END
    INTO v_policy
    FROM core.policy_refresh_outbox q
   WHERE q.account_id=p_account AND q.request_kind='policy'
     AND q.done_at IS NULL
   ORDER BY CASE
              WHEN q.claimed_at IS NULL THEN COALESCE(q.not_before,q.enqueued_at)
              ELSE q.claimed_at
                   +make_interval(secs=>v_claim_horizon)
            END,
            q.repository_id
   LIMIT 1;
  IF v_policy IS NOT NULL THEN v_policy_n:=1; END IF;
  SELECT COALESCE(q.not_before,q.enqueued_at)
    INTO v_graph
    FROM core.policy_refresh_outbox q
   WHERE q.account_id=p_account AND q.request_kind='graph'
     AND q.done_at IS NULL
     AND q.claimed_at IS NULL
     AND NOT EXISTS (
       SELECT 1 FROM core.graph_convergence_lease l
        WHERE l.account_id=p_account AND l.repository_id=q.repository_id)
   ORDER BY COALESCE(q.not_before,q.enqueued_at),q.repository_id
   LIMIT 1;
  SELECT EXISTS (
    SELECT 1 FROM core.policy_refresh_outbox q
     WHERE q.account_id=p_account AND q.request_kind='graph'
       AND q.done_at IS NULL AND q.claimed_at IS NULL
       AND NOT q.onboarding_pending)
    INTO v_legacy_graph_has;
  SELECT COALESCE(q.not_before,q.enqueued_at)
    INTO v_legacy_graph
    FROM core.policy_refresh_outbox q
   WHERE q.account_id=p_account AND q.request_kind='graph'
     AND q.done_at IS NULL AND q.claimed_at IS NULL
     AND NOT q.onboarding_pending
     AND NOT EXISTS (
       SELECT 1 FROM core.graph_convergence_lease l
        WHERE l.account_id=p_account AND l.repository_id=q.repository_id)
   ORDER BY COALESCE(q.not_before,q.enqueued_at),q.repository_id
   LIMIT 1;
  SELECT convergence_graph_claim_count,convergence_graph_reclaim_at
    INTO v_graph_claims,v_graph_reclaim
    FROM core.installation_account
   WHERE account_id=p_account;
  -- While the account's one graph lease is live, publish only its reclaim time. A second repository is not due
  -- capacity until that exact lease finishes or expires, so an idle worker remains available for another tenant
  -- even when no other tenant was queued at the instant of the first claim.
  IF COALESCE(v_graph_claims,0)>=1 THEN
    v_graph:=v_graph_reclaim;
    v_legacy_graph:=CASE WHEN v_legacy_graph_has THEN v_graph_reclaim ELSE NULL END;
  ELSIF v_graph_reclaim IS NOT NULL THEN
    v_graph:=LEAST(COALESCE(v_graph,v_graph_reclaim),v_graph_reclaim);
    IF v_legacy_graph_has THEN
      v_legacy_graph:=LEAST(COALESCE(v_legacy_graph,v_graph_reclaim),v_graph_reclaim);
    END IF;
  END IF;
  IF v_graph IS NOT NULL THEN v_graph_n:=1; END IF;
  IF COALESCE(p_tail,false) THEN
    IF v_policy IS NOT NULL THEN v_policy:=GREATEST(v_policy,now()); END IF;
    IF v_graph IS NOT NULL THEN v_graph:=GREATEST(v_graph,now()); END IF;
    IF v_legacy_graph IS NOT NULL THEN
      v_legacy_graph:=GREATEST(v_legacy_graph,now());
    END IF;
  END IF;
  -- Alert provenance is not scheduler position.  Keep the oldest original
  -- enqueue across page-tail moves, retries, and lease reclaims. quota_paused
  -- is expected flow control and remains separately counted rather than
  -- becoming a latency incident.
  SELECT enqueued_at
    INTO v_stall
    FROM core.policy_refresh_outbox
   WHERE account_id=p_account AND done_at IS NULL
     AND terminal_reason IS NULL
   ORDER BY enqueued_at,request_kind,repository_id
   LIMIT 1;
  UPDATE core.installation_account
     SET policy_refresh_due_at=v_policy,
         graph_refresh_due_at=v_graph,
         legacy_graph_refresh_due_at=v_legacy_graph,
         -- Superseding a latest-wins graph row replaces its enqueued_at.  As
         -- long as at least one ordinary fact remains unfinished, retain the
         -- original incident start instead of moving it forward to the new
         -- target.  Only a fully drained/quota-only account resets the clock.
         convergence_stall_started_at=CASE
           WHEN v_stall IS NULL THEN NULL
           ELSE LEAST(COALESCE(convergence_stall_started_at,v_stall),v_stall)
         END
   WHERE account_id=p_account;
  RETURN jsonb_build_object(
    'policy_due_at',v_policy,'graph_due_at',v_graph,
    'legacy_graph_due_at',v_legacy_graph,
    'stall_started_at',v_stall,
    'policy_pending',COALESCE(v_policy_n,0),'graph_pending',COALESCE(v_graph_n,0));
END $$;
ALTER FUNCTION core._sync_account_convergence_due(text,int,int,boolean) OWNER TO veripsa_migrator;
REVOKE ALL ON FUNCTION core._sync_account_convergence_due(text,int,int,boolean) FROM PUBLIC;

-- Upgrade bridge for requests which predate convergence_stall_started_at.
-- The partial router index above limits this to accounts that already publish
-- ordinary unfinished depth and becomes empty after the one-time repair.
DO $$
DECLARE v_prev text; v_account text; v_stall timestamptz;
BEGIN
  v_prev:=current_setting('core.current_account',true);
  FOR v_account IN
      SELECT account_id
        FROM core.installation_account
       WHERE revoked_at IS NULL AND convergence_stall_started_at IS NULL
         AND (convergence_pending_count>0 OR convergence_retry_exhausted_count>0)
       ORDER BY account_id
  LOOP
    PERFORM 1
      FROM core.installation_account
     WHERE account_id=v_account AND convergence_stall_started_at IS NULL
     FOR UPDATE;
    IF NOT FOUND THEN CONTINUE; END IF;
    PERFORM set_config('core.current_account',v_account,true);
    SELECT enqueued_at
      INTO v_stall
      FROM core.policy_refresh_outbox
     WHERE account_id=v_account AND done_at IS NULL
       AND terminal_reason IS NULL
     ORDER BY enqueued_at,request_kind,repository_id
     LIMIT 1;
    UPDATE core.installation_account
       SET convergence_stall_started_at=v_stall
     WHERE account_id=v_account;
  END LOOP;
  PERFORM set_config('core.current_account',COALESCE(v_prev,''),true);
END $$;

CREATE OR REPLACE FUNCTION core._sync_graph_claim_router(
    p_account text,p_stale_seconds int
) RETURNS int
    LANGUAGE plpgsql SECURITY DEFINER SET search_path TO 'core','pg_catalog' AS $$
DECLARE v_n int; v_reclaim timestamptz; v_stale int:=GREATEST(1,COALESCE(p_stale_seconds,300));
BEGIN
  WITH active AS MATERIALIZED (
    SELECT claimed_until
      FROM core.graph_convergence_lease
     WHERE account_id=p_account
     ORDER BY claimed_until,slot
     LIMIT 2
  )
  SELECT count(*)::int,min(claimed_until)
    INTO v_n,v_reclaim FROM active;
  UPDATE core.installation_account
     SET convergence_graph_claim_count=COALESCE(v_n,0),
         convergence_graph_reclaim_at=v_reclaim
   WHERE account_id=p_account;
  RETURN COALESCE(v_n,0);
END $$;
ALTER FUNCTION core._sync_graph_claim_router(text,int) OWNER TO veripsa_migrator;
REVOKE ALL ON FUNCTION core._sync_graph_claim_router(text,int) FROM PUBLIC;

-- Same-owner repository rename hook, called late-bound from 35_lifecycle._migrate_repo_coordinate. The stable
-- repository_id remains the queue identity, while `repo` is mutable routing metadata. Any live old-name lease is
-- superseded under route -> lease -> outbox lock order, and unfinished work is requeued immediately with a fresh
-- request epoch so a returning old-coordinate extractor cannot terminalize the renamed request.
CREATE OR REPLACE FUNCTION core._rename_graph_refresh_coordinate(
    p_account text,p_old_repo text,p_new_repo text
) RETURNS int
    LANGUAGE plpgsql SECURITY DEFINER SET search_path TO 'core','pg_catalog' AS $$
DECLARE
  v_prev text; v_row record; v_epoch bigint; v_due timestamptz:=clock_timestamp();
  v_moved int:=0; v_pending_delta int:=0; v_exhausted_delta int:=0; v_quota_delta int:=0;
  v_revoked timestamptz;
BEGIN
  IF p_account IS NULL OR btrim(p_account)=''
     OR p_old_repo IS NULL OR p_new_repo IS NULL OR p_old_repo=p_new_repo THEN
    RETURN 0;
  END IF;
  v_prev:=current_setting('core.current_account',true);
  PERFORM set_config('core.current_account',p_account,true);
  SELECT revoked_at INTO v_revoked
    FROM core.installation_account
   WHERE account_id=p_account
   FOR UPDATE;
  IF NOT FOUND OR v_revoked IS NOT NULL THEN
    PERFORM set_config('core.current_account',COALESCE(v_prev,''),true);
    RETURN 0;
  END IF;
  -- Snapshot cleanup is exact and bounded (at most one row). If a newer enqueue already changed the outbox
  -- mutable name but the old lease still exists, deleting by the lease snapshot re-arms that row during sync.
  DELETE FROM core.graph_convergence_lease
   WHERE account_id=p_account AND repo=p_old_repo;
  FOR v_row IN
      SELECT repository_id,policy_epoch,done_at,attempts,terminal_reason
        FROM core.policy_refresh_outbox
       WHERE account_id=p_account AND request_kind='graph' AND repo=p_old_repo
       ORDER BY repository_id
       FOR UPDATE
  LOOP
    IF v_row.done_at IS NULL THEN
      v_epoch:=core._next_account_convergence_epoch(p_account);
      v_pending_delta:=v_pending_delta
        +1-CASE WHEN v_row.terminal_reason IS NULL AND v_row.attempts<5 THEN 1 ELSE 0 END;
      v_exhausted_delta:=v_exhausted_delta
        -CASE WHEN v_row.terminal_reason IS NULL AND v_row.attempts>=5 THEN 1 ELSE 0 END;
      v_quota_delta:=v_quota_delta
        -CASE WHEN v_row.terminal_reason='quota_paused' THEN 1 ELSE 0 END;
      PERFORM core.mark_governed_write('policy_refresh_outbox');
      UPDATE core.policy_refresh_outbox
         SET repo=p_new_repo,policy_epoch=v_epoch,enqueued_at=v_due,
             not_before=NULL,terminal_reason=NULL,claimed_at=NULL,claimed_by=NULL,
             done_at=NULL,attempts=0,last_error=NULL,
             policy_cursor_repo='',policy_cursor_branch='',change_cursor='',
             onboarding_head_pending=onboarding_pending,onboarding_plan=NULL,
             onboarding_index=0,onboarding_truncated=false,onboarding_watching_done=false
       WHERE account_id=p_account AND request_kind='graph'
         AND repository_id=v_row.repository_id AND policy_epoch=v_row.policy_epoch;
    ELSE
      PERFORM core.mark_governed_write('policy_refresh_outbox');
      UPDATE core.policy_refresh_outbox
         SET repo=p_new_repo
       WHERE account_id=p_account AND request_kind='graph'
         AND repository_id=v_row.repository_id AND policy_epoch=v_row.policy_epoch;
    END IF;
    IF FOUND THEN v_moved:=v_moved+1; END IF;
  END LOOP;
  UPDATE core.installation_account
     SET convergence_pending_count=GREATEST(
           0,convergence_pending_count+v_pending_delta),
         convergence_retry_exhausted_count=GREATEST(
           0,convergence_retry_exhausted_count+v_exhausted_delta),
         convergence_quota_deferred_count=GREATEST(
           0,convergence_quota_deferred_count+v_quota_delta)
   WHERE account_id=p_account;
  PERFORM core._sync_graph_claim_router(p_account,300);
  PERFORM core._sync_account_convergence_due(p_account,5,300,false);
  PERFORM set_config('core.current_account',COALESCE(v_prev,''),true);
  RETURN v_moved;
END $$;
ALTER FUNCTION core._rename_graph_refresh_coordinate(text,text,text) OWNER TO veripsa_migrator;
REVOKE ALL ON FUNCTION core._rename_graph_refresh_coordinate(text,text,text) FROM PUBLIC;

-- Every delete takes the non-RLS account router before PostgreSQL locks any outbox row. Live terminals take
-- route -> graph lease -> outbox in the same order; this statement trigger keeps lifecycle purge/tombstone
-- paths from forming the inverse outbox -> lease -> route cycle.
CREATE OR REPLACE FUNCTION core._lock_convergence_delete_router()
RETURNS trigger
    LANGUAGE plpgsql SECURITY DEFINER SET search_path TO 'core','pg_catalog' AS $$
DECLARE v_account text;
BEGIN
  v_account:=NULLIF(current_setting('core.current_account',true),'');
  IF v_account IS NULL THEN
    RAISE EXCEPTION 'convergence delete needs a pinned account' USING ERRCODE='42501';
  END IF;
  PERFORM 1
    FROM core.installation_account
   WHERE account_id=v_account
   FOR UPDATE;
  RETURN NULL;
END $$;
ALTER FUNCTION core._lock_convergence_delete_router() OWNER TO veripsa_migrator;
REVOKE ALL ON FUNCTION core._lock_convergence_delete_router() FROM PUBLIC;

DO $$ BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_trigger
     WHERE tgrelid='core.policy_refresh_outbox'::regclass
       AND tgname='trg_lock_convergence_delete_router' AND NOT tgisinternal
  ) THEN
    CREATE TRIGGER trg_lock_convergence_delete_router
    BEFORE DELETE ON core.policy_refresh_outbox
    FOR EACH STATEMENT EXECUTE FUNCTION core._lock_convergence_delete_router();
  END IF;
END $$;

-- Any authoritative request deletion (repository offboard, uninstall working-set purge, or account erasure)
-- also removes its v2 graph slot snapshot and applies the deleted state classes to the scalar router counters.
-- A statement transition table keeps account-wide purges linear: one set delete plus one bounded router repair
-- per affected account, not a row-trigger query cascade.
CREATE OR REPLACE FUNCTION core._cleanup_deleted_convergence_requests()
RETURNS trigger
    LANGUAGE plpgsql SECURITY DEFINER SET search_path TO 'core','pg_catalog' AS $$
DECLARE
  v_prev text; v_account text; v_policy_epochs bigint[];
  v_pending int; v_exhausted int; v_quota int;
BEGIN
  v_prev:=current_setting('core.current_account',true);
  FOR v_account IN
      SELECT DISTINCT account_id FROM deleted_convergence_requests ORDER BY account_id
  LOOP
    PERFORM set_config('core.current_account',v_account,true);
    -- Re-locking is cheap and makes this function safe if PostgreSQL ever invokes it independently of the
    -- companion BEFORE trigger. The route remains first in the global lock order.
    PERFORM 1
      FROM core.installation_account
     WHERE account_id=v_account
     FOR UPDATE;
    SELECT
        COALESCE(array_agg(policy_epoch) FILTER (
          WHERE request_kind='policy'),'{}'::bigint[]),
        count(*) FILTER (
          WHERE done_at IS NULL AND attempts<5 AND terminal_reason IS NULL)::int,
        count(*) FILTER (
          WHERE done_at IS NULL AND attempts>=5 AND terminal_reason IS NULL)::int,
        count(*) FILTER (
          WHERE done_at IS NULL AND terminal_reason='quota_paused')::int
      INTO v_policy_epochs,v_pending,v_exhausted,v_quota
      FROM deleted_convergence_requests
     WHERE account_id=v_account;
    DELETE FROM core.graph_convergence_lease l
     USING deleted_convergence_requests d
     WHERE d.account_id=v_account AND d.request_kind='graph'
       AND l.account_id=d.account_id AND l.repository_id=d.repository_id;
    UPDATE core.installation_account
       SET convergence_pending_count=GREATEST(
             0,convergence_pending_count-COALESCE(v_pending,0)),
           convergence_retry_exhausted_count=GREATEST(
             0,convergence_retry_exhausted_count-COALESCE(v_exhausted,0)),
           convergence_quota_deferred_count=GREATEST(
             0,convergence_quota_deferred_count-COALESCE(v_quota,0)),
           convergence_claimed_until=CASE
             WHEN convergence_claim_epoch=ANY(v_policy_epochs) THEN NULL
             ELSE convergence_claimed_until END,
           convergence_claimed_by=CASE
             WHEN convergence_claim_epoch=ANY(v_policy_epochs) THEN NULL
             ELSE convergence_claimed_by END,
           convergence_claim_epoch=CASE
             WHEN convergence_claim_epoch=ANY(v_policy_epochs) THEN NULL
             ELSE convergence_claim_epoch END
     WHERE account_id=v_account;
    PERFORM core._sync_graph_claim_router(v_account,300);
    PERFORM core._sync_account_convergence_due(v_account,5,300,true);
  END LOOP;
  PERFORM set_config('core.current_account',COALESCE(v_prev,''),true);
  RETURN NULL;
END $$;
ALTER FUNCTION core._cleanup_deleted_convergence_requests() OWNER TO veripsa_migrator;
REVOKE ALL ON FUNCTION core._cleanup_deleted_convergence_requests() FROM PUBLIC;

DO $$ BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_trigger
     WHERE tgrelid='core.policy_refresh_outbox'::regclass
       AND tgname='trg_cleanup_deleted_convergence_requests' AND NOT tgisinternal
  ) THEN
    CREATE TRIGGER trg_cleanup_deleted_convergence_requests
    AFTER DELETE ON core.policy_refresh_outbox
    REFERENCING OLD TABLE AS deleted_convergence_requests
    FOR EACH STATEMENT EXECUTE FUNCTION core._cleanup_deleted_convergence_requests();
  END IF;
END $$;

-- Lifecycle offboarding is authoritative queue cancellation. Stable-id tombstones remove exactly that repository;
-- a GitHub-confirmed legacy absence uses repository_id='unknown' and removes only the exact mutable name. Doing
-- this from the tombstone write keeps every offboard entry point (removed/deleted/confirmed-absent) module-order
-- compatible without weakening the durable-delivery authority gates in 35_lifecycle.sql. Claimed rows are safe:
-- the old worker's later exact CAS misses, while this trigger releases only its matching account lease.
CREATE OR REPLACE FUNCTION core._cancel_tombstoned_graph_refresh()
RETURNS trigger
    LANGUAGE plpgsql SECURITY DEFINER SET search_path TO 'core','pg_catalog' AS $$
DECLARE
  v_prev text;
BEGIN
  IF NEW.superseded_at IS NOT NULL THEN
    RETURN NEW;
  END IF;
  v_prev:=current_setting('core.current_account',true);
  PERFORM set_config('core.current_account',NEW.account_id,true);
  -- Tombstone writers enter route -> lease -> outbox, matching every live terminal. Direct lease deletion also
  -- removes a privacy-orphaned snapshot when its outbox row was already absent or damaged.
  PERFORM 1
    FROM core.installation_account
   WHERE account_id=NEW.account_id
   FOR UPDATE;
  DELETE FROM core.graph_convergence_lease
   WHERE account_id=NEW.account_id
     AND (
       (NEW.repository_id<>'unknown' AND repository_id=NEW.repository_id)
       OR (NEW.repository_id='unknown' AND repo=NEW.repo));
  DELETE FROM core.policy_refresh_outbox
   WHERE account_id=NEW.account_id AND request_kind='graph'
     AND (
       (NEW.repository_id<>'unknown' AND repository_id=NEW.repository_id)
       OR (NEW.repository_id='unknown' AND repo=NEW.repo));
  -- The AFTER DELETE transition trigger handles counters when a row existed. These bounded repairs also cover
  -- the orphan-lease-only case, where the transition table is empty.
  PERFORM core._sync_graph_claim_router(NEW.account_id,300);
  PERFORM core._sync_account_convergence_due(NEW.account_id,5,300,true);
  PERFORM set_config('core.current_account',COALESCE(v_prev,''),true);
  RETURN NEW;
END $$;
ALTER FUNCTION core._cancel_tombstoned_graph_refresh() OWNER TO veripsa_migrator;
REVOKE ALL ON FUNCTION core._cancel_tombstoned_graph_refresh() FROM PUBLIC;

DO $$ BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_trigger
     WHERE tgrelid='core.repository_lifecycle_tombstone'::regclass
       AND tgname='trg_cancel_tombstoned_graph_refresh' AND NOT tgisinternal
  ) THEN
    CREATE TRIGGER trg_cancel_tombstoned_graph_refresh
    AFTER INSERT OR UPDATE OF repository_id,repo,superseded_at
    ON core.repository_lifecycle_tombstone
    FOR EACH ROW EXECUTE FUNCTION core._cancel_tombstoned_graph_refresh();
  END IF;
END $$;

-- Capability-aware claim. Global scheduling is ONE bounded, index-ordered query against the non-RLS installation
-- router; only the selected account is pinned and queried. p_scan_cap is retained for ABI compatibility but is
-- clamped to a small contention retry budget, never an account scan. This EXPAND-window 5-argument ABI is
-- deliberately policy-only: it cannot carry the slot+lease generation required to terminalize graph work.
CREATE OR REPLACE FUNCTION core.claim_policy_refresh_with_authority(
    p_worker text,p_max_attempts int,p_stale_seconds int,p_scan_cap int,p_support_graph boolean
) RETURNS jsonb
    LANGUAGE plpgsql SECURITY DEFINER SET search_path TO 'core','pg_catalog' AS $$
DECLARE
  v_prev text; v_acct text; v_key text; v_epoch bigint; v_old_epoch bigint; v_attempts int;
  v_kind text; v_id text; v_repo text; v_branch text; v_target text;
  v_cursor_repo text; v_cursor_branch text;
  v_live_epoch bigint; v_live_worker text; v_live_claimed timestamptz;
  v_row_claimed timestamptz;
  v_policy_due timestamptz; v_graph_due timestamptz;
  v_graph_claims int; v_graph_reclaim timestamptz;
  v_max CONSTANT int:=5;
  -- Signature retained for replace-compatibility only. This intermediate ABI has no slot token and therefore
  -- remains policy-only even when a predecessor passes p_support_graph=true.
  v_support_graph CONSTANT boolean:=false;
  -- EXPAND-window safety: this body is callable before the final compatibility wrapper is published. Clamp the
  -- retained old-image ABI here too, so a rollback worker cannot mint another 900/1800-second account lease.
  v_stale int := LEAST(GREATEST(1, COALESCE(p_stale_seconds,300)),300);
  v_retry int := LEAST(GREATEST(1,COALESCE(p_scan_cap,4)),8);
  v_try int:=0;
BEGIN
  v_prev := current_setting('core.current_account', true);
  WHILE v_try<v_retry LOOP
    v_try:=v_try+1;
    IF v_support_graph THEN
      SELECT account_id,installation_id,policy_refresh_due_at,graph_refresh_due_at,
             convergence_graph_claim_count,convergence_graph_reclaim_at
        INTO v_acct,v_key,v_policy_due,v_graph_due,v_graph_claims,v_graph_reclaim
        FROM core.installation_account
       WHERE revoked_at IS NULL
         AND (convergence_claimed_until IS NULL OR convergence_claimed_until<now())
         AND (
           (policy_refresh_due_at<=now() AND convergence_graph_claim_count=0)
           OR
           (graph_refresh_due_at<=now()
            AND (convergence_graph_claim_count<1 OR convergence_graph_reclaim_at<now())
            AND (
              convergence_graph_claim_count=0
              OR policy_refresh_due_at IS NULL
              OR graph_refresh_due_at<policy_refresh_due_at
              OR convergence_graph_reclaim_at<now())))
       ORDER BY LEAST(
         CASE WHEN policy_refresh_due_at<=now() AND convergence_graph_claim_count=0
              THEN policy_refresh_due_at END,
         CASE WHEN graph_refresh_due_at<=now()
                    AND (convergence_graph_claim_count<1 OR convergence_graph_reclaim_at<now())
                    AND (convergence_graph_claim_count=0
                         OR policy_refresh_due_at IS NULL
                         OR graph_refresh_due_at<policy_refresh_due_at
                         OR convergence_graph_reclaim_at<now())
              THEN graph_refresh_due_at END),
         account_id
       LIMIT 1 FOR UPDATE SKIP LOCKED;
    ELSE
      SELECT account_id,installation_id,policy_refresh_due_at,graph_refresh_due_at,
             convergence_graph_claim_count,convergence_graph_reclaim_at
        INTO v_acct,v_key,v_policy_due,v_graph_due,v_graph_claims,v_graph_reclaim
        FROM core.installation_account
       WHERE revoked_at IS NULL AND policy_refresh_due_at IS NOT NULL
         AND policy_refresh_due_at<=now()
         AND convergence_graph_claim_count=0
         AND (convergence_claimed_until IS NULL OR convergence_claimed_until<now())
       ORDER BY policy_refresh_due_at,account_id
       LIMIT 1 FOR UPDATE SKIP LOCKED;
    END IF;
    IF v_acct IS NULL THEN
      PERFORM set_config('core.current_account',COALESCE(v_prev,''),true);
      RETURN NULL;
    END IF;
    PERFORM set_config('core.current_account',v_acct,true);

    -- Rolling bridge: an old worker may have claimed an outbox row before routing leases existed. Materialize that
    -- surviving lease in the router and let the next global index probe choose another account.
    SELECT policy_epoch,claimed_by,claimed_at
      INTO v_live_epoch,v_live_worker,v_live_claimed
      FROM core.policy_refresh_outbox
     WHERE account_id=v_acct AND request_kind='policy'
       AND done_at IS NULL AND claimed_at IS NOT NULL
       AND claimed_at>=clock_timestamp()-make_interval(secs=>v_stale)
     ORDER BY claimed_at DESC LIMIT 1;
    IF v_live_epoch IS NOT NULL THEN
      UPDATE core.installation_account
         SET convergence_claimed_until=v_live_claimed+make_interval(secs=>v_stale),
             convergence_claimed_by=v_live_worker,
             convergence_claim_epoch=v_live_epoch
       WHERE account_id=v_acct;
      v_acct:=NULL;
      CONTINUE;
    END IF;

    SELECT q.request_kind,q.repository_id,q.repo,q.branch,q.target_sha,
           q.policy_cursor_repo,q.policy_cursor_branch,
           q.policy_epoch,q.attempts,q.claimed_at
      INTO v_kind,v_id,v_repo,v_branch,v_target,v_cursor_repo,v_cursor_branch,
           v_epoch,v_attempts,v_row_claimed
      FROM core.policy_refresh_outbox q
     WHERE q.account_id=v_acct AND q.attempts<v_max
       AND (q.claimed_at IS NULL OR q.claimed_at<now()-make_interval(secs=>v_stale))
       AND COALESCE(q.not_before,q.enqueued_at)<=now()
       AND (q.request_kind='policy' OR v_support_graph)
       AND (
         (q.request_kind='policy' AND COALESCE(v_graph_claims,0)=0)
         OR
         (q.request_kind='graph' AND v_support_graph
          AND (
            q.claimed_at<now()-make_interval(secs=>v_stale)
            OR (
              COALESCE(v_graph_claims,0)<1
              AND (
                COALESCE(v_graph_claims,0)=0
                OR v_policy_due IS NULL
                OR COALESCE(q.not_before,q.enqueued_at)<v_policy_due)))))
       AND (
         q.done_at IS NULL
         OR (q.request_kind='graph' AND v_support_graph
             AND NOT core._graph_refresh_fulfilled(
               q.account_id,q.repository_id,q.repo,q.branch,q.target_sha)))
     -- Old work wins. Graph-first is only a deterministic tie-break for work enqueued at the same instant; a
     -- continuous stream of new graph events can never starve an older policy sentinel for this account.
     ORDER BY CASE
                WHEN q.request_kind='graph' AND q.claimed_at IS NOT NULL
                     AND q.claimed_at<now()-make_interval(secs=>v_stale)
                THEN 0 ELSE 1 END,
              COALESCE(q.not_before,q.enqueued_at),
              CASE WHEN q.request_kind='graph' THEN 0 ELSE 1 END,q.repository_id
     LIMIT 1
     FOR UPDATE SKIP LOCKED;
    IF FOUND THEN
      -- A stale reclaim is a NEW lease generation. Advancing the shared account epoch makes a worker that wakes
      -- after losing its lease fail every exact CAS; it cannot finish the new owner's turn or clear its route.
      v_old_epoch:=v_epoch;
      IF v_row_claimed IS NOT NULL THEN
        v_epoch:=core._next_account_convergence_epoch(v_acct);
      END IF;
      PERFORM core.mark_governed_write('policy_refresh_outbox');
      UPDATE core.policy_refresh_outbox
         SET policy_epoch=v_epoch,claimed_at=now(),
             claimed_by=left(COALESCE(p_worker,''),120),done_at=NULL
       WHERE account_id=v_acct AND request_kind=v_kind
         AND repository_id=v_id AND policy_epoch=v_old_epoch;
      IF v_kind='policy' THEN
        UPDATE core.installation_account
           SET convergence_claimed_until=now()+make_interval(secs=>v_stale),
               convergence_claimed_by=left(COALESCE(p_worker,''),120),
               convergence_claim_epoch=v_epoch
         WHERE account_id=v_acct;
      ELSE
        PERFORM core._sync_graph_claim_router(v_acct,v_stale);
        PERFORM core._sync_account_convergence_due(v_acct,v_max,v_stale,false);
      END IF;
      PERFORM set_config('core.current_account',COALESCE(v_prev,''),true);
      RETURN jsonb_build_object(
        'account_id',v_acct,'account_key',v_key,'request_kind',v_kind,
        'repository_id',v_id,'repo',v_repo,'branch',v_branch,
        'target_sha',v_target,'policy_epoch',v_epoch,'attempts',v_attempts,
        'policy_cursor_repo',COALESCE(v_cursor_repo,''),
        'policy_cursor_branch',COALESCE(v_cursor_branch,''));
    END IF;
    -- A stale routing pointer (exhausted/revoked terminal row) is repaired once, then cannot head-of-line block.
    PERFORM core._sync_account_convergence_due(v_acct,v_max,v_stale,false);
    UPDATE core.installation_account
       SET convergence_claimed_until=NULL,convergence_claimed_by=NULL,convergence_claim_epoch=NULL
     WHERE account_id=v_acct AND convergence_claim_epoch IS NULL;
    v_acct:=NULL;
  END LOOP;
  PERFORM set_config('core.current_account',COALESCE(v_prev,''),true);
  RETURN NULL;
END $$;
ALTER FUNCTION core.claim_policy_refresh_with_authority(text,int,int,int,boolean) OWNER TO veripsa_migrator;
REVOKE ALL ON FUNCTION core.claim_policy_refresh_with_authority(text,int,int,int,boolean) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION core.claim_policy_refresh_with_authority(text,int,int,int,boolean) TO veripsa_app;

-- Lease-protocol v2 claim (independent of db/schema_generation). Policy remains account-exclusive; graph work
-- uses exactly one repository slot per account.
-- The slot snapshots the request coordinate, so latest-wins enqueue can advance the desired row without freeing
-- an extractor that is still running. Expired slots are reclaimed before selection and late terminals require
-- the exact slot+lease_epoch, never only the mutable request epoch.
CREATE OR REPLACE FUNCTION core.claim_policy_refresh_with_authority(
    p_worker text,p_max_attempts int,p_stale_seconds int,p_scan_cap int,
    p_support_graph boolean,p_graph_slots int,p_support_onboarding boolean
) RETURNS jsonb
    LANGUAGE plpgsql SECURITY DEFINER SET search_path TO 'core','pg_catalog' AS $$
DECLARE
  v_prev text; v_acct text; v_key text; v_kind text; v_id text; v_repo text;
  v_branch text; v_target text; v_request_epoch bigint; v_attempts int;
  v_installation_id text; v_installation_created_at timestamptz;
  v_cursor_repo text; v_cursor_branch text; v_change_cursor text;
  v_surface_dirty boolean;
  v_onboarding_pending boolean; v_onboarding_head_pending boolean;
  v_onboarding_plan bigint[];
  v_onboarding_index int; v_onboarding_truncated boolean;
  v_onboarding_watching_done boolean;
  v_terminal_reason text;
  v_not_before timestamptz;
  v_old_request_epoch bigint; v_row_claimed timestamptz;
  v_slot smallint; v_lease_epoch bigint;
  v_live_epoch bigint; v_live_worker text; v_live_claimed timestamptz;
  v_graph_claims int:=0; v_expired int:=0;
  v_expired_request_epoch bigint; v_expired_id text; v_expired_branch text;
  v_expired_old_attempts int; v_expired_new_attempts int; v_expired_reason text;
  v_probe timestamptz;
  -- Five is a schema-level counter/alert invariant, not a caller tuning knob. Rolling workers may still send an
  -- older value through the retained ABI; ignore it so they cannot strand a row by publishing a due pointer under
  -- one budget while every terminal/counter path classifies under another.
  v_max CONSTANT int:=5;
  -- 300s is the database safety bound (worker turns are hard-killed at 270s). Retained rollback callers used to
  -- send 900s; clamping here prevents them from recreating the observed ~787-900s account stall.
  v_stale int:=LEAST(GREATEST(1,COALESCE(p_stale_seconds,300)),300);
  -- p_graph_slots is retained for rolling ABI compatibility, but is not a capacity knob. A graph-capable caller
  -- receives exactly one account slot regardless of an old/high value; policy-only callers receive none.
  v_slots int:=CASE WHEN COALESCE(p_support_graph,false) THEN 1 ELSE 0 END;
  v_support_onboarding boolean:=COALESCE(p_support_onboarding,false)
    AND COALESCE(p_support_graph,false);
  v_retry int:=LEAST(GREATEST(1,COALESCE(p_scan_cap,4)),8);
  v_try int:=0;
BEGIN
  v_prev:=current_setting('core.current_account',true);
  WHILE v_try<v_retry LOOP
    v_try:=v_try+1;
    -- A PL/pgSQL parameter, unlike a VOLATILE clock_timestamp() expression in the predicate, remains an index
    -- range condition. Refresh it for every contention retry; use a new clock only after locks for lease fencing.
    v_probe:=clock_timestamp();
    -- Each lane takes its own index-ordered SKIP LOCKED head. This avoids both the full-table OR/sort and a fixed
    -- candidate window whose first N rows can all be locked. A graph account is eligible only while idle or once
    -- its sole lease has expired; `_sync_account_convergence_due` hides its queued tail behind that reclaim time.
    WITH policy_candidate AS MATERIALIZED (
      SELECT account_id,installation_id,github_installation_id,
             github_installation_created_at,policy_refresh_due_at AS due_at
       FROM core.installation_account
       WHERE revoked_at IS NULL
         AND policy_refresh_due_at<=v_probe
         AND (convergence_graph_claim_count=0
              OR convergence_graph_reclaim_at<v_probe)
         AND (convergence_claimed_until IS NULL
              OR convergence_claimed_until<v_probe)
       ORDER BY policy_refresh_due_at,account_id
       LIMIT 1 FOR UPDATE SKIP LOCKED
    ), graph_candidate_current AS MATERIALIZED (
      SELECT account_id,installation_id,github_installation_id,
             github_installation_created_at,graph_refresh_due_at AS due_at
       FROM core.installation_account
       WHERE revoked_at IS NULL AND v_slots>0 AND v_support_onboarding
         AND graph_refresh_due_at<=v_probe
         AND (convergence_graph_claim_count=0
              OR convergence_graph_reclaim_at<v_probe)
         AND (convergence_claimed_until IS NULL
              OR convergence_claimed_until<v_probe)
       ORDER BY graph_refresh_due_at,account_id
       LIMIT 1 FOR UPDATE SKIP LOCKED
    ), graph_candidate_legacy AS MATERIALIZED (
      -- This scalar was derived from the oldest executable non-onboarding row while the account was pinned.
      -- Reading tenant outbox rows here would run before set_config under FORCE RLS and is therefore forbidden.
      SELECT account_id,installation_id,github_installation_id,
             github_installation_created_at,legacy_graph_refresh_due_at AS due_at
        FROM core.installation_account
       WHERE NOT v_support_onboarding AND v_slots>0 AND revoked_at IS NULL
         AND legacy_graph_refresh_due_at<=v_probe
         AND (convergence_graph_claim_count=0
              OR convergence_graph_reclaim_at<v_probe)
         AND (convergence_claimed_until IS NULL
              OR convergence_claimed_until<v_probe)
       ORDER BY legacy_graph_refresh_due_at,account_id
       LIMIT 1 FOR UPDATE SKIP LOCKED
    ), graph_candidate AS MATERIALIZED (
      SELECT * FROM graph_candidate_current
      UNION ALL
      SELECT * FROM graph_candidate_legacy
    ), candidates AS MATERIALIZED (
      SELECT * FROM policy_candidate
      UNION ALL
      SELECT * FROM graph_candidate
    )
    SELECT account_id,installation_id,github_installation_id,
           github_installation_created_at
      INTO v_acct,v_key,v_installation_id,v_installation_created_at
      FROM candidates
     ORDER BY due_at,account_id
     LIMIT 1;
    IF v_acct IS NULL THEN
      PERFORM set_config('core.current_account',COALESCE(v_prev,''),true);
      RETURN NULL;
    END IF;
    PERFORM set_config('core.current_account',v_acct,true);

    -- A lease expiry is the generation fence. Delete only expired snapshots, then rebuild the two scalar router
    -- fields from the at-most-one surviving slot before selecting any replacement work.
    v_expired_request_epoch:=NULL; v_expired_id:=NULL; v_expired_branch:=NULL;
    DELETE FROM core.graph_convergence_lease
     WHERE account_id=v_acct AND claimed_until<clock_timestamp()
     RETURNING request_epoch,repository_id,branch
      INTO v_expired_request_epoch,v_expired_id,v_expired_branch;
    GET DIAGNOSTICS v_expired=ROW_COUNT;
    IF v_expired>0 THEN
      -- A hard-killed process cannot call fail_turn. Charge the abandoned exact phase here, before making the
      -- repository claimable again. Successful HEAD/plan/PR/watch boundaries reset this budget, so five crashes
      -- isolate only the offending cursor while other accounts continue through the fair scheduler.
      SELECT attempts,terminal_reason
        INTO v_expired_old_attempts,v_expired_reason
        FROM core.policy_refresh_outbox
       WHERE account_id=v_acct AND request_kind='graph'
         AND repository_id=v_expired_id AND branch=v_expired_branch
         AND policy_epoch=v_expired_request_epoch AND done_at IS NULL
       FOR UPDATE;
      IF FOUND THEN
        PERFORM core.mark_governed_write('policy_refresh_outbox');
        UPDATE core.policy_refresh_outbox
           SET attempts=attempts+1,last_error='turn_abandoned',
               terminal_reason=NULL,
               not_before=clock_timestamp()+make_interval(secs=>
                 CASE
                   WHEN attempts+1<5 THEN 20
                   WHEN attempts+1=5 THEN 300
                   WHEN attempts+1=6 THEN 600
                   WHEN attempts+1=7 THEN 1200
                   WHEN attempts+1=8 THEN 2400
                   ELSE 3600
                 END)
         WHERE account_id=v_acct AND request_kind='graph'
           AND repository_id=v_expired_id AND branch=v_expired_branch
           AND policy_epoch=v_expired_request_epoch AND done_at IS NULL
         RETURNING attempts INTO v_expired_new_attempts;
        IF v_expired_new_attempts IS NOT NULL THEN
          UPDATE core.installation_account
             SET convergence_pending_count=GREATEST(0,convergence_pending_count
                   -CASE WHEN v_expired_reason IS NULL
                              AND v_expired_old_attempts<5 THEN 1 ELSE 0 END
                   +CASE WHEN v_expired_new_attempts<5 THEN 1 ELSE 0 END),
                 convergence_retry_exhausted_count=GREATEST(0,convergence_retry_exhausted_count
                   -CASE WHEN v_expired_reason IS NULL
                              AND v_expired_old_attempts>=5 THEN 1 ELSE 0 END
                   +CASE WHEN v_expired_new_attempts>=5 THEN 1 ELSE 0 END),
                 convergence_quota_deferred_count=GREATEST(0,convergence_quota_deferred_count
                   -CASE WHEN v_expired_reason='quota_paused' THEN 1 ELSE 0 END)
           WHERE account_id=v_acct;
        END IF;
      END IF;
      PERFORM core._sync_graph_claim_router(v_acct,v_stale);
      PERFORM core._sync_account_convergence_due(v_acct,v_max,v_stale,true);
    END IF;
    SELECT convergence_graph_claim_count INTO v_graph_claims
      FROM core.installation_account WHERE account_id=v_acct;

    -- Rolling old-image policy claim: only the sentinel existed before graph slots. Materialize its surviving
    -- claim as the account-exclusive lease; graph v2 never overlaps it.
    SELECT policy_epoch,claimed_by,claimed_at
      INTO v_live_epoch,v_live_worker,v_live_claimed
      FROM core.policy_refresh_outbox
     WHERE account_id=v_acct AND request_kind='policy'
       AND done_at IS NULL AND claimed_at IS NOT NULL
       AND claimed_at>=clock_timestamp()-make_interval(secs=>v_stale)
     LIMIT 1;
    IF v_live_epoch IS NOT NULL THEN
      UPDATE core.installation_account
         SET convergence_claimed_until=v_live_claimed+make_interval(secs=>v_stale),
             convergence_claimed_by=v_live_worker,
             convergence_claim_epoch=v_live_epoch
       WHERE account_id=v_acct;
      v_acct:=NULL;
      CONTINUE;
    END IF;

    WITH policy_work AS MATERIALIZED (
      SELECT q.*,CASE
                   WHEN q.claimed_at IS NULL THEN COALESCE(q.not_before,q.enqueued_at)
                   ELSE q.claimed_at+make_interval(secs=>v_stale)
                 END AS effective_due
        FROM core.policy_refresh_outbox q
       WHERE q.account_id=v_acct AND q.request_kind='policy' AND q.repository_id=''
         AND q.done_at IS NULL AND v_graph_claims=0
         AND (
           (q.claimed_at IS NULL AND COALESCE(q.not_before,q.enqueued_at)<=v_probe)
           OR
           (q.claimed_at IS NOT NULL
            AND q.claimed_at<v_probe-make_interval(secs=>v_stale)))
       LIMIT 1 FOR UPDATE SKIP LOCKED
    ), graph_work AS MATERIALIZED (
      SELECT q.*,COALESCE(q.not_before,q.enqueued_at) AS effective_due
        FROM core.policy_refresh_outbox q
       WHERE q.account_id=v_acct AND q.request_kind='graph'
         AND q.done_at IS NULL AND q.claimed_at IS NULL
         AND (v_support_onboarding OR NOT q.onboarding_pending)
         AND COALESCE(q.not_before,q.enqueued_at)<=v_probe
         AND v_slots>0 AND v_graph_claims<v_slots
         AND NOT EXISTS (
           SELECT 1 FROM core.graph_convergence_lease l
            WHERE l.account_id=v_acct AND l.repository_id=q.repository_id)
       ORDER BY COALESCE(q.not_before,q.enqueued_at),q.repository_id
       LIMIT 1 FOR UPDATE SKIP LOCKED
    ), work AS MATERIALIZED (
      SELECT * FROM policy_work
      UNION ALL
      SELECT * FROM graph_work
    )
    SELECT q.request_kind,q.repository_id,q.repo,q.branch,q.target_sha,
           q.policy_epoch,q.attempts,q.policy_cursor_repo,q.policy_cursor_branch,
           q.change_cursor,q.surface_dirty,q.claimed_at,
           q.onboarding_pending,q.onboarding_head_pending,
           q.onboarding_plan,q.onboarding_index,
           q.onboarding_truncated,q.onboarding_watching_done,
           q.terminal_reason,q.not_before
      INTO v_kind,v_id,v_repo,v_branch,v_target,v_request_epoch,v_attempts,
           v_cursor_repo,v_cursor_branch,v_change_cursor,v_surface_dirty,v_row_claimed,
           v_onboarding_pending,v_onboarding_head_pending,
           v_onboarding_plan,v_onboarding_index,
           v_onboarding_truncated,v_onboarding_watching_done,
           v_terminal_reason,v_not_before
      FROM work q
     ORDER BY q.effective_due,
              CASE WHEN q.request_kind='graph' THEN 0 ELSE 1 END,q.repository_id
     LIMIT 1;
    IF NOT FOUND THEN
      PERFORM core._sync_graph_claim_router(v_acct,v_stale);
      PERFORM core._sync_account_convergence_due(v_acct,v_max,v_stale,false);
      v_acct:=NULL;
      CONTINUE;
    END IF;

    IF v_kind='policy' THEN
      v_old_request_epoch:=v_request_epoch;
      IF v_row_claimed IS NOT NULL THEN
        -- A crashed policy worker loses authority at expiry. Reclaim under a fresh generation while preserving
        -- every durable cursor and attempt; the old worker's later exact terminal/page CAS cannot mutate it.
        v_request_epoch:=core._next_account_convergence_epoch(v_acct);
      END IF;
      PERFORM core.mark_governed_write('policy_refresh_outbox');
      UPDATE core.policy_refresh_outbox
         SET policy_epoch=v_request_epoch,claimed_at=clock_timestamp(),
             claimed_by=left(COALESCE(p_worker,''),120)
       WHERE account_id=v_acct AND request_kind='policy' AND repository_id=''
         AND policy_epoch=v_old_request_epoch
         AND (
           (v_row_claimed IS NULL AND claimed_at IS NULL)
           OR (v_row_claimed IS NOT NULL AND claimed_at=v_row_claimed));
      UPDATE core.installation_account
         SET convergence_claimed_until=clock_timestamp()+make_interval(secs=>v_stale),
             convergence_claimed_by=left(COALESCE(p_worker,''),120),
             convergence_claim_epoch=v_request_epoch
       WHERE account_id=v_acct;
      PERFORM set_config('core.current_account',COALESCE(v_prev,''),true);
      RETURN jsonb_build_object(
        'account_id',v_acct,'account_key',v_key,'request_kind','policy',
        'github_installation_id',v_installation_id,
        'github_installation_created_at',v_installation_created_at,
        'repository_id','','repo','','branch','','target_sha',NULL,
        'policy_epoch',v_request_epoch,'request_epoch',v_request_epoch,
        'lease_epoch',v_request_epoch,'graph_slot',NULL,'attempts',v_attempts,
        'policy_cursor_repo',COALESCE(v_cursor_repo,''),
        'policy_cursor_branch',COALESCE(v_cursor_branch,''),
        'change_cursor',COALESCE(v_change_cursor,''));
    END IF;

    SELECT s::smallint INTO v_slot
      FROM generate_series(1,v_slots) AS s
     WHERE NOT EXISTS (
       SELECT 1 FROM core.graph_convergence_lease l
        WHERE l.account_id=v_acct AND l.slot=s)
     ORDER BY s LIMIT 1;
    IF v_slot IS NULL THEN
      v_acct:=NULL;
      CONTINUE;
    END IF;
    -- A dirty latch present before a full pass begins is covered by the
    -- current snapshot and may be consumed. Mid-page latches deliberately
    -- remain set until exact finish schedules one later pass from cursor ''.
    IF COALESCE(v_change_cursor,'')='' AND COALESCE(v_surface_dirty,false) THEN
      PERFORM core.mark_governed_write('policy_refresh_outbox');
      UPDATE core.policy_refresh_outbox
         SET surface_dirty=false
       WHERE account_id=v_acct AND request_kind='graph'
         AND repository_id=v_id AND policy_epoch=v_request_epoch
         AND done_at IS NULL AND change_cursor='';
      IF NOT FOUND THEN
        v_acct:=NULL;
        CONTINUE;
      END IF;
    END IF;
    v_lease_epoch:=core._next_account_convergence_epoch(v_acct);
    PERFORM core.mark_governed_write('graph_convergence_lease');
    INSERT INTO core.graph_convergence_lease(
      account_id,slot,lease_epoch,request_epoch,repository_id,repo,branch,target_sha,
      claimed_by,claimed_until)
    VALUES (
      v_acct,v_slot,v_lease_epoch,v_request_epoch,v_id,v_repo,v_branch,v_target,
      left(COALESCE(p_worker,''),120),clock_timestamp()+make_interval(secs=>v_stale));
    PERFORM core._sync_graph_claim_router(v_acct,v_stale);
    PERFORM core._sync_account_convergence_due(v_acct,v_max,v_stale,false);
    PERFORM set_config('core.current_account',COALESCE(v_prev,''),true);
    RETURN jsonb_build_object(
      'account_id',v_acct,'account_key',v_key,'request_kind','graph',
      'github_installation_id',v_installation_id,
      'github_installation_created_at',v_installation_created_at,
      'repository_id',v_id,'repo',v_repo,'branch',v_branch,'target_sha',v_target,
      'policy_epoch',v_request_epoch,'request_epoch',v_request_epoch,
      'lease_epoch',v_lease_epoch,'graph_slot',v_slot,'attempts',v_attempts,
      'policy_cursor_repo','','policy_cursor_branch','',
      'change_cursor',COALESCE(v_change_cursor,''),
      'onboarding_pending',COALESCE(v_onboarding_pending,false),
      'onboarding_head_pending',COALESCE(v_onboarding_head_pending,false),
      'onboarding_plan',CASE WHEN v_onboarding_plan IS NULL THEN NULL ELSE to_jsonb(v_onboarding_plan) END,
      'onboarding_index',COALESCE(v_onboarding_index,0),
      'onboarding_truncated',COALESCE(v_onboarding_truncated,false),
      'onboarding_watching_done',COALESCE(v_onboarding_watching_done,false),
      'terminal_reason',v_terminal_reason,
      'not_before',v_not_before);
  END LOOP;
  PERFORM set_config('core.current_account',COALESCE(v_prev,''),true);
  RETURN NULL;
END $$;
ALTER FUNCTION core.claim_policy_refresh_with_authority(text,int,int,int,boolean,int,boolean)
  OWNER TO veripsa_migrator;
REVOKE ALL ON FUNCTION core.claim_policy_refresh_with_authority(text,int,int,int,boolean,int,boolean)
  FROM PUBLIC;
GRANT EXECUTE ON FUNCTION core.claim_policy_refresh_with_authority(text,int,int,int,boolean,int,boolean)
  TO veripsa_app;

-- Rolling predecessor ABI. A six-argument worker understands graph slot tokens but not the installation plan;
-- the capability bit is therefore forced false. The scheduler/body above excludes onboarding rows before claim,
-- so an old image can neither run their GitHub work nor terminalize them as an ordinary graph refresh.
CREATE OR REPLACE FUNCTION core.claim_policy_refresh_with_authority(
    p_worker text,p_max_attempts int,p_stale_seconds int,p_scan_cap int,
    p_support_graph boolean,p_graph_slots int
) RETURNS jsonb
    LANGUAGE sql SECURITY DEFINER SET search_path TO 'core','pg_catalog' AS $$
  SELECT core.claim_policy_refresh_with_authority(
    p_worker,p_max_attempts,p_stale_seconds,p_scan_cap,
    p_support_graph,p_graph_slots,false)
$$;
ALTER FUNCTION core.claim_policy_refresh_with_authority(text,int,int,int,boolean,int)
  OWNER TO veripsa_migrator;
REVOKE ALL ON FUNCTION core.claim_policy_refresh_with_authority(text,int,int,int,boolean,int)
  FROM PUBLIC;
GRANT EXECUTE ON FUNCTION core.claim_policy_refresh_with_authority(text,int,int,int,boolean,int)
  TO veripsa_app;

-- A claim snapshots the exact GitHub installation generation used to mint its
-- client.  Account ids survive uninstall/reinstall, so account equality alone
-- cannot authorize the old client's later GitHub mutation.  Runtime checks
-- this point-in-time predicate before any external work and again while
-- holding the shared account-lifecycle session lock across each mutation.
CREATE OR REPLACE FUNCTION
  core.policy_refresh_install_generation_current_with_authority(
    p_account text,p_installation_id text,p_created_at timestamptz
) RETURNS boolean
    LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path TO 'core','pg_catalog' AS $$
DECLARE v_prev text; v_ok boolean:=false;
BEGIN
  IF p_account IS NULL OR length(p_account) NOT BETWEEN 1 AND 120
     OR p_installation_id IS NULL
     OR length(p_installation_id) NOT BETWEEN 1 AND 64
     OR p_installation_id !~ '^[1-9][0-9]*$'
     OR p_created_at IS NULL THEN
    RETURN false;
  END IF;
  v_prev:=current_setting('core.current_account',true);
  PERFORM set_config('core.current_account',p_account,true);
  SELECT EXISTS (
    SELECT 1 FROM core.installation_account r
     WHERE r.account_id=p_account AND r.revoked_at IS NULL
       AND r.github_installation_id=p_installation_id
       AND r.github_installation_created_at=p_created_at
       AND NOT EXISTS (
         SELECT 1 FROM core.account_lifecycle_tombstone t
          WHERE t.account_id=p_account AND t.active))
    INTO v_ok;
  PERFORM set_config('core.current_account',COALESCE(v_prev,''),true);
  RETURN COALESCE(v_ok,false);
END $$;
ALTER FUNCTION
  core.policy_refresh_install_generation_current_with_authority(
    text,text,timestamptz) OWNER TO veripsa_migrator;
REVOKE ALL ON FUNCTION
  core.policy_refresh_install_generation_current_with_authority(
    text,text,timestamptz) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION
  core.policy_refresh_install_generation_current_with_authority(
    text,text,timestamptz) TO veripsa_app;

-- Transitional 5-argument workers may claim only the policy sentinel. They cannot understand graph slot tokens
-- and therefore must never see a graph request or overlap an active v2 slot.
-- CREATE OR REPLACE preserves pg_description. After the explicit production
-- cutover, publish the NULL fence directly in this module so a later schema
-- failure before module 100 cannot reopen legacy claims. NULL is the normal
-- expand/fresh state; a foreign comment is never guessed compatible.
SELECT
  COALESCE(obj_description(to_regprocedure(
    'core.claim_policy_refresh_with_authority(text,integer,integer,integer,boolean)'),
    'pg_proc')='veripsa-legacy-policy-claim/v1/fenced',false)
    AS veripsa_policy5_cutover_fenced,
  COALESCE(obj_description(to_regprocedure(
    'core.claim_policy_refresh_with_authority(text,integer,integer,integer,boolean)'),
    'pg_proc') NOT IN ('veripsa-legacy-policy-claim/v1/fenced'),false)
    AS veripsa_policy5_cutover_unknown
\gset
\if :veripsa_policy5_cutover_unknown
SELECT 'unknown legacy policy /5 cutover marker; refusing publication'::integer;
\elif :veripsa_policy5_cutover_fenced
CREATE OR REPLACE FUNCTION core.claim_policy_refresh_with_authority(
    p_worker text,p_max_attempts int,p_stale_seconds int,p_scan_cap int,p_support_graph boolean
) RETURNS jsonb
    LANGUAGE sql SECURITY DEFINER SET search_path TO 'core','pg_catalog'
    AS 'SELECT NULL::jsonb';
\else
CREATE OR REPLACE FUNCTION core.claim_policy_refresh_with_authority(
    p_worker text,p_max_attempts int,p_stale_seconds int,p_scan_cap int,p_support_graph boolean
) RETURNS jsonb
    LANGUAGE sql SECURITY DEFINER SET search_path TO 'core','pg_catalog' AS $$
  SELECT core.claim_policy_refresh_with_authority(
    p_worker,p_max_attempts,p_stale_seconds,p_scan_cap,false,0)
$$;
\endif
ALTER FUNCTION core.claim_policy_refresh_with_authority(text,int,int,int,boolean)
  OWNER TO veripsa_migrator;
REVOKE ALL ON FUNCTION core.claim_policy_refresh_with_authority(text,int,int,int,boolean) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION core.claim_policy_refresh_with_authority(text,int,int,int,boolean)
  TO veripsa_app;

-- Rolling legacy ABI: an old Python drainer can see only the policy sentinel. It can never claim a graph fact.
SELECT
  COALESCE(obj_description(to_regprocedure(
    'core.claim_policy_refresh_with_authority(text,integer,integer,integer)'),
    'pg_proc')='veripsa-legacy-policy-claim/v1/fenced',false)
    AS veripsa_policy4_cutover_fenced,
  COALESCE(obj_description(to_regprocedure(
    'core.claim_policy_refresh_with_authority(text,integer,integer,integer)'),
    'pg_proc') NOT IN ('veripsa-legacy-policy-claim/v1/fenced'),false)
    AS veripsa_policy4_cutover_unknown
\gset
\if :veripsa_policy4_cutover_unknown
SELECT 'unknown legacy policy /4 cutover marker; refusing publication'::integer;
\elif :veripsa_policy4_cutover_fenced
CREATE OR REPLACE FUNCTION core.claim_policy_refresh_with_authority(
    p_worker text,p_max_attempts int,p_stale_seconds int,p_scan_cap int
) RETURNS jsonb
    LANGUAGE sql SECURITY DEFINER SET search_path TO 'core','pg_catalog'
    AS 'SELECT NULL::jsonb';
\else
CREATE OR REPLACE FUNCTION core.claim_policy_refresh_with_authority(
    p_worker text,p_max_attempts int,p_stale_seconds int,p_scan_cap int
) RETURNS jsonb
    LANGUAGE sql SECURITY DEFINER SET search_path TO 'core','pg_catalog' AS $$
  SELECT core.claim_policy_refresh_with_authority(
    p_worker,p_max_attempts,p_stale_seconds,p_scan_cap,false)
$$;
\endif
ALTER FUNCTION core.claim_policy_refresh_with_authority(text,int,int,int) OWNER TO veripsa_migrator;
REVOKE ALL ON FUNCTION core.claim_policy_refresh_with_authority(text,int,int,int) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION core.claim_policy_refresh_with_authority(text,int,int,int) TO veripsa_app;

-- SINGLE-TENANT CAPACITY CUTOVER. The six-argument claim above now ignores an old/high p_graph_slots value and
-- can mint only slot 1; the retained five/four-argument ABIs are policy-only. Install the stronger CHECK first
-- as NOT VALID: PostgreSQL applies it to every new write immediately and waits out any already-running old
-- claim transaction, while permitting a pre-cutover slot-2 snapshot to remain long enough for bounded cleanup.
DO $$
DECLARE v_definition text;
BEGIN
  SELECT pg_get_constraintdef(oid)
    INTO v_definition
    FROM pg_constraint
   WHERE conrelid='core.graph_convergence_lease'::regclass
     AND conname='graph_convergence_lease_account_cap_one'
     AND contype='c';
  IF NOT FOUND THEN
    ALTER TABLE core.graph_convergence_lease
      ADD CONSTRAINT graph_convergence_lease_account_cap_one
      CHECK (slot = 1) NOT VALID;
  ELSIF regexp_replace(v_definition,'[[:space:]()]','','g')<>'CHECKslot=1' THEN
    RAISE EXCEPTION 'unexpected graph convergence account-cap constraint: %',v_definition
      USING ERRCODE='55000';
  END IF;
END $$;

-- A rolling predecessor may already hold slot 2. Active graph snapshots are globally bounded by worker count
-- and named by the non-RLS router, so this visits only active accounts, never every tenant. Route locking waits
-- out any old claim that began before function publication. Removing slot 2 makes that worker's exact terminal
-- CAS lose authority; its latest-wins outbox row was never marked claimed and is therefore re-published behind
-- the surviving slot-1 lease (or immediately if slot 2 was the only lease).
DO $$
DECLARE v_prev text; v_account text;
BEGIN
  v_prev:=current_setting('core.current_account',true);
  FOR v_account IN
      SELECT account_id
        FROM core.installation_account
       WHERE convergence_graph_claim_count>0
       ORDER BY account_id
  LOOP
    PERFORM 1
      FROM core.installation_account
     WHERE account_id=v_account
     FOR UPDATE;
    IF NOT FOUND THEN CONTINUE; END IF;
    PERFORM set_config('core.current_account',v_account,true);
    DELETE FROM core.graph_convergence_lease
     WHERE account_id=v_account AND slot<>1;
    IF FOUND THEN
      PERFORM core._sync_graph_claim_router(v_account,300);
      PERFORM core._sync_account_convergence_due(v_account,5,300,true);
    END IF;
  END LOOP;
  PERFORM set_config('core.current_account',COALESCE(v_prev,''),true);
END $$;
SELECT core._ensure_constraint_valid_online(
  'graph_convergence_lease','graph_convergence_lease_account_cap_one');

CREATE OR REPLACE FUNCTION core.policy_refresh_turn_is_current_with_authority(
    p_account text,p_kind text,p_repository_id text,p_branch text,p_epoch bigint
) RETURNS boolean
    LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path TO 'core','pg_catalog' AS $$
DECLARE v_prev text; v_ok boolean;
BEGIN
  IF p_kind<>'policy' OR p_repository_id<>'' OR p_branch<>'' THEN
    RETURN false;
  END IF;
  v_prev:=current_setting('core.current_account',true);
  PERFORM set_config('core.current_account',p_account,true);
  SELECT EXISTS (
    SELECT 1 FROM core.policy_refresh_outbox
     WHERE account_id=p_account AND request_kind=p_kind
       AND repository_id=p_repository_id AND branch=p_branch
       AND policy_epoch=p_epoch AND done_at IS NULL AND claimed_at IS NOT NULL
       AND EXISTS (
         SELECT 1 FROM core.installation_account r
          WHERE r.account_id=p_account AND r.revoked_at IS NULL
            AND r.convergence_claim_epoch=p_epoch
            AND r.convergence_claimed_until>=clock_timestamp()))
    INTO v_ok;
  PERFORM set_config('core.current_account',COALESCE(v_prev,''),true);
  RETURN v_ok;
END $$;
ALTER FUNCTION core.policy_refresh_turn_is_current_with_authority(text,text,text,text,bigint)
  OWNER TO veripsa_migrator;
REVOKE ALL ON FUNCTION core.policy_refresh_turn_is_current_with_authority(text,text,text,text,bigint) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION core.policy_refresh_turn_is_current_with_authority(text,text,text,text,bigint)
  TO veripsa_app;

-- Exact turn completion. Graph work is terminal only after the durable graph row proves the requested SHA,
-- current extractor, stable repository id, and coordinate. CAS misses (including callback re-enqueue of a newer
-- HEAD) leave the superseding row pending. Other unclaimed rows for the account are moved to the queue tail.
CREATE OR REPLACE FUNCTION core.finish_policy_refresh_turn_with_authority(
    p_account text,p_kind text,p_repository_id text,p_branch text,p_epoch bigint
) RETURNS jsonb
    LANGUAGE plpgsql SECURITY DEFINER SET search_path TO 'core','pg_catalog' AS $$
DECLARE
  v_prev text; v_hit int:=0; v_tail int:=0; v_repo text; v_target text;
  v_current_epoch bigint; v_lease_epoch bigint; v_graph_ok boolean:=true; v_due jsonb;
  v_old_attempts int; v_old_reason text; v_claimed_until timestamptz;
  v_revoked timestamptz;
BEGIN
  IF p_account IS NULL OR btrim(p_account)='' THEN
    RETURN jsonb_build_object('finished',false,'tail_rearmed',0,'graph_fulfilled',false,'superseded',false);
  END IF;
  IF p_kind<>'policy' OR p_repository_id<>'' OR p_branch<>'' THEN
    RETURN jsonb_build_object(
      'finished',false,'tail_rearmed',0,'graph_fulfilled',false,
      'superseded',true,'lease_lost',true);
  END IF;
  v_prev:=current_setting('core.current_account',true);
  PERFORM set_config('core.current_account',p_account,true);
  SELECT convergence_claim_epoch,convergence_claimed_until,revoked_at
    INTO v_lease_epoch,v_claimed_until,v_revoked
    FROM core.installation_account
   WHERE account_id=p_account
   LIMIT 1 FOR UPDATE;
  IF NOT FOUND OR v_revoked IS NOT NULL
     OR v_lease_epoch IS DISTINCT FROM p_epoch
     OR v_claimed_until IS NULL OR v_claimed_until<clock_timestamp() THEN
    PERFORM set_config('core.current_account',COALESCE(v_prev,''),true);
    RETURN jsonb_build_object(
      'finished',false,'tail_rearmed',0,'graph_fulfilled',false,
      'superseded',true,'lease_lost',true);
  END IF;
  SELECT policy_epoch,repo,target_sha,attempts,terminal_reason
    INTO v_current_epoch,v_repo,v_target,v_old_attempts,v_old_reason
    FROM core.policy_refresh_outbox
   WHERE account_id=p_account AND request_kind=p_kind
     AND repository_id=p_repository_id AND branch=p_branch
   FOR UPDATE;
  -- The outbox lock can itself wait. Authority is a wall-clock lease, so re-check it after every required lock.
  IF v_claimed_until<clock_timestamp() THEN
    PERFORM set_config('core.current_account',COALESCE(v_prev,''),true);
    RETURN jsonb_build_object(
      'finished',false,'tail_rearmed',0,'graph_fulfilled',false,
      'superseded',true,'lease_lost',true);
  END IF;
  IF v_current_epoch IS NULL OR v_current_epoch<>p_epoch THEN
    -- The callback may have atomically replaced this stable-id row with a newer HEAD/branch. Its enqueue already
    -- published the new due-time. Release ONLY the still-owned old lease generation; never rewrite the row/due,
    -- and never clear a lease that another worker has since acquired.
    IF p_kind='policy' THEN
      UPDATE core.installation_account
         SET convergence_claimed_until=NULL,
             convergence_claimed_by=NULL,
             convergence_claim_epoch=NULL
       WHERE account_id=p_account AND convergence_claim_epoch=p_epoch;
    ELSE
      PERFORM core._sync_graph_claim_router(p_account,300);
      PERFORM core._sync_account_convergence_due(p_account,5,300,true);
    END IF;
    PERFORM set_config('core.current_account',COALESCE(v_prev,''),true);
    RETURN jsonb_build_object(
      'finished',false,'tail_rearmed',0,'graph_fulfilled',false,
      'superseded',true,'lease_released',true);
  END IF;
  IF NOT v_graph_ok THEN
    PERFORM set_config('core.current_account',COALESCE(v_prev,''),true);
    RETURN jsonb_build_object('finished',false,'tail_rearmed',0,'graph_fulfilled',false,'superseded',false);
  END IF;
  PERFORM core.mark_governed_write('policy_refresh_outbox');
  UPDATE core.policy_refresh_outbox
     SET done_at=now(),claimed_at=NULL,claimed_by=NULL,last_error=NULL,
         not_before=NULL,terminal_reason=NULL,
         policy_cursor_repo='',policy_cursor_branch='',change_cursor=''
   WHERE account_id=p_account AND request_kind=p_kind
     AND repository_id=p_repository_id AND branch=p_branch
     AND policy_epoch=p_epoch AND done_at IS NULL AND claimed_at IS NOT NULL;
  GET DIAGNOSTICS v_hit=ROW_COUNT;
  IF v_hit>0 THEN
    UPDATE core.installation_account
       SET convergence_pending_count=GREATEST(0,convergence_pending_count
             -CASE WHEN v_old_reason IS NULL AND v_old_attempts<5 THEN 1 ELSE 0 END),
           convergence_retry_exhausted_count=GREATEST(0,convergence_retry_exhausted_count
             -CASE WHEN v_old_reason IS NULL AND v_old_attempts>=5 THEN 1 ELSE 0 END),
           convergence_quota_deferred_count=GREATEST(0,convergence_quota_deferred_count
             -CASE WHEN v_old_reason='quota_paused' THEN 1 ELSE 0 END)
     WHERE account_id=p_account;
    IF p_kind='graph' THEN
      PERFORM core._sync_graph_claim_router(p_account,300);
      v_due:=core._sync_account_convergence_due(p_account,5,300,true);
    ELSE
      v_due:=core._sync_account_convergence_due(p_account,5,900,true);
    END IF;
    v_tail:=COALESCE((v_due->>'policy_pending')::int,0)
            +COALESCE((v_due->>'graph_pending')::int,0);
    IF p_kind='policy' THEN
      UPDATE core.installation_account
         SET convergence_claimed_until=NULL,
             convergence_claimed_by=NULL,
             convergence_claim_epoch=NULL
       WHERE account_id=p_account AND convergence_claim_epoch=p_epoch;
    END IF;
  END IF;
  PERFORM set_config('core.current_account',COALESCE(v_prev,''),true);
  RETURN jsonb_build_object(
    'finished',v_hit>0,'tail_rearmed',v_tail,'graph_fulfilled',v_graph_ok,'superseded',false);
END $$;
ALTER FUNCTION core.finish_policy_refresh_turn_with_authority(text,text,text,text,bigint)
  OWNER TO veripsa_migrator;
REVOKE ALL ON FUNCTION core.finish_policy_refresh_turn_with_authority(text,text,text,text,bigint) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION core.finish_policy_refresh_turn_with_authority(text,text,text,text,bigint)
  TO veripsa_app;

CREATE OR REPLACE FUNCTION core.fail_policy_refresh_turn_with_authority(
    p_account text,p_kind text,p_repository_id text,p_branch text,p_epoch bigint,p_error text,
    p_retry_seconds int
) RETURNS int
    LANGUAGE plpgsql SECURITY DEFINER SET search_path TO 'core','pg_catalog' AS $$
DECLARE v_prev text; v_n int; v_lease_epoch bigint;
        v_old_attempts int; v_old_reason text; v_retry int;
        v_claimed_until timestamptz; v_revoked timestamptz;
BEGIN
  IF p_account IS NULL OR btrim(p_account)='' THEN RETURN -1; END IF;
  IF p_kind<>'policy' OR p_repository_id<>'' OR p_branch<>'' THEN RETURN -1; END IF;
  v_prev:=current_setting('core.current_account',true);
  PERFORM set_config('core.current_account',p_account,true);
  SELECT convergence_claim_epoch,convergence_claimed_until,revoked_at
    INTO v_lease_epoch,v_claimed_until,v_revoked
    FROM core.installation_account
   WHERE account_id=p_account
   LIMIT 1 FOR UPDATE;
  IF NOT FOUND OR v_revoked IS NOT NULL
     OR v_lease_epoch IS DISTINCT FROM p_epoch
     OR v_claimed_until IS NULL OR v_claimed_until<clock_timestamp() THEN
    PERFORM set_config('core.current_account',COALESCE(v_prev,''),true);
    RETURN -1;
  END IF;
  v_retry:=LEAST(GREATEST(COALESCE(p_retry_seconds,20),15),30);
  SELECT attempts,terminal_reason INTO v_old_attempts,v_old_reason
    FROM core.policy_refresh_outbox
   WHERE account_id=p_account AND request_kind=p_kind
     AND repository_id=p_repository_id AND branch=p_branch
     AND policy_epoch=p_epoch AND done_at IS NULL
   FOR UPDATE;
  IF v_claimed_until<clock_timestamp() THEN
    PERFORM set_config('core.current_account',COALESCE(v_prev,''),true);
    RETURN -1;
  END IF;
  PERFORM core.mark_governed_write('policy_refresh_outbox');
  UPDATE core.policy_refresh_outbox
     SET attempts=attempts+1,last_error=left(COALESCE(p_error,''),200),
         claimed_at=NULL,claimed_by=NULL,terminal_reason=NULL,
         not_before=now()+make_interval(secs=>
           CASE
             WHEN attempts+1<5 THEN v_retry
             WHEN attempts+1=5 THEN 300
             WHEN attempts+1=6 THEN 600
             WHEN attempts+1=7 THEN 1200
             WHEN attempts+1=8 THEN 2400
             ELSE 3600
           END)
   WHERE account_id=p_account AND request_kind=p_kind
     AND repository_id=p_repository_id AND branch=p_branch
     AND policy_epoch=p_epoch AND done_at IS NULL AND claimed_at IS NOT NULL
   RETURNING attempts INTO v_n;
  IF v_n IS NOT NULL THEN
    UPDATE core.installation_account
       SET convergence_pending_count=GREATEST(0,convergence_pending_count
             -CASE WHEN v_old_reason IS NULL AND v_old_attempts<5 THEN 1 ELSE 0 END
             +CASE WHEN v_n<5 THEN 1 ELSE 0 END),
           convergence_retry_exhausted_count=GREATEST(0,convergence_retry_exhausted_count
             -CASE WHEN v_old_reason IS NULL AND v_old_attempts>=5 THEN 1 ELSE 0 END
             +CASE WHEN v_n>=5 THEN 1 ELSE 0 END),
           convergence_quota_deferred_count=GREATEST(0,convergence_quota_deferred_count
             -CASE WHEN v_old_reason='quota_paused' THEN 1 ELSE 0 END)
     WHERE account_id=p_account;
    IF p_kind='graph' THEN
      PERFORM core._sync_graph_claim_router(p_account,300);
      PERFORM core._sync_account_convergence_due(p_account,5,v_retry,true);
    ELSE
      PERFORM core._sync_account_convergence_due(p_account,5,v_retry,true);
      UPDATE core.installation_account
         SET convergence_claimed_until=NULL,
             convergence_claimed_by=NULL,
             convergence_claim_epoch=NULL
       WHERE account_id=p_account AND convergence_claim_epoch=p_epoch;
    END IF;
  ELSE
    -- Offboarding or a newer stable-id/branch enqueue may remove/replace the exact row after the worker's
    -- network step. A row-level CAS miss must still release only the old lease generation; otherwise this
    -- account is artificially frozen until stale reclaim.
    IF p_kind='graph' THEN
      PERFORM core._sync_graph_claim_router(p_account,300);
      PERFORM core._sync_account_convergence_due(p_account,5,v_retry,true);
    ELSE
      UPDATE core.installation_account
         SET convergence_claimed_until=NULL,
             convergence_claimed_by=NULL,
             convergence_claim_epoch=NULL
       WHERE account_id=p_account AND convergence_claim_epoch=p_epoch;
    END IF;
  END IF;
  PERFORM set_config('core.current_account',COALESCE(v_prev,''),true);
  RETURN COALESCE(v_n,-1);
END $$;
ALTER FUNCTION core.fail_policy_refresh_turn_with_authority(text,text,text,text,bigint,text,int)
  OWNER TO veripsa_migrator;
REVOKE ALL ON FUNCTION core.fail_policy_refresh_turn_with_authority(text,text,text,text,bigint,text,int) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION core.fail_policy_refresh_turn_with_authority(text,text,text,text,bigint,text,int)
  TO veripsa_app;

CREATE OR REPLACE FUNCTION core.fail_policy_refresh_turn_with_authority(
    p_account text,p_kind text,p_repository_id text,p_branch text,p_epoch bigint,p_error text
) RETURNS int
    LANGUAGE sql SECURITY DEFINER SET search_path TO 'core','pg_catalog' AS $$
  SELECT core.fail_policy_refresh_turn_with_authority(
    p_account,p_kind,p_repository_id,p_branch,p_epoch,p_error,20)
$$;
ALTER FUNCTION core.fail_policy_refresh_turn_with_authority(text,text,text,text,bigint,text)
  OWNER TO veripsa_migrator;
REVOKE ALL ON FUNCTION core.fail_policy_refresh_turn_with_authority(text,text,text,text,bigint,text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION core.fail_policy_refresh_turn_with_authority(text,text,text,text,bigint,text)
  TO veripsa_app;

-- A strict callback that proves the free-tier wall is not a graph success and must never fake fulfillment. It is
-- a durable, attempts-neutral defer: the worker first idempotently posts the existing fair-use surface to every
-- in-flight PR, then this exact epoch CAS records the reason and schedules another honest convergence probe.
-- A new enqueue clears the reason/not_before and is immediately due.
CREATE OR REPLACE FUNCTION core.defer_graph_refresh_turn_with_authority(
    p_account text,p_repository_id text,p_branch text,p_epoch bigint,
    p_reason text,p_delay_seconds int
) RETURNS jsonb
    LANGUAGE plpgsql SECURITY DEFINER SET search_path TO 'core','pg_catalog' AS $$
DECLARE
  v_prev text; v_lease_epoch bigint; v_hit int:=0; v_delay int;
  v_due jsonb; v_next timestamptz;
  v_old_attempts int; v_old_reason text;
BEGIN
  IF p_reason<>'quota_paused' THEN
    RAISE EXCEPTION 'unsupported graph defer reason' USING ERRCODE='22023';
  END IF;
  v_delay:=LEAST(GREATEST(COALESCE(p_delay_seconds,900),60),86400);
  v_prev:=current_setting('core.current_account',true);
  PERFORM set_config('core.current_account',p_account,true);
  SELECT convergence_claim_epoch INTO v_lease_epoch
    FROM core.installation_account
   WHERE account_id=p_account AND revoked_at IS NULL
   LIMIT 1 FOR UPDATE;
  v_next:=now()+make_interval(secs=>v_delay);
  SELECT attempts,terminal_reason INTO v_old_attempts,v_old_reason
    FROM core.policy_refresh_outbox
   WHERE account_id=p_account AND request_kind='graph'
     AND repository_id=p_repository_id AND branch=p_branch
     AND policy_epoch=p_epoch AND done_at IS NULL AND claimed_at IS NOT NULL;
  PERFORM core.mark_governed_write('policy_refresh_outbox');
  UPDATE core.policy_refresh_outbox
     SET claimed_at=NULL,claimed_by=NULL,done_at=NULL,last_error=NULL,
         terminal_reason='quota_paused',not_before=v_next
   WHERE account_id=p_account AND request_kind='graph'
     AND repository_id=p_repository_id AND branch=p_branch
     AND policy_epoch=p_epoch AND done_at IS NULL;
  GET DIAGNOSTICS v_hit=ROW_COUNT;
  IF v_hit>0 THEN
    UPDATE core.installation_account
       SET convergence_pending_count=GREATEST(0,convergence_pending_count
             -CASE WHEN v_old_reason IS NULL AND v_old_attempts<5 THEN 1 ELSE 0 END),
           convergence_retry_exhausted_count=GREATEST(0,convergence_retry_exhausted_count
             -CASE WHEN v_old_reason IS NULL AND v_old_attempts>=5 THEN 1 ELSE 0 END),
           convergence_quota_deferred_count=GREATEST(0,convergence_quota_deferred_count
             -CASE WHEN v_old_reason='quota_paused' THEN 1 ELSE 0 END+1)
     WHERE account_id=p_account;
    PERFORM core._sync_graph_claim_router(p_account,300);
    v_due:=core._sync_account_convergence_due(p_account,5,v_delay,true);
  END IF;
  PERFORM set_config('core.current_account',COALESCE(v_prev,''),true);
  RETURN jsonb_build_object(
    'deferred',v_hit>0,'superseded',false,'reason','quota_paused','not_before',v_next);
END $$;
ALTER FUNCTION core.defer_graph_refresh_turn_with_authority(text,text,text,bigint,text,int)
  OWNER TO veripsa_migrator;
REVOKE ALL ON FUNCTION core.defer_graph_refresh_turn_with_authority(text,text,text,bigint,text,int) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION core.defer_graph_refresh_turn_with_authority(text,text,text,bigint,text,int)
  TO veripsa_app;

-- Policy refreshes are sliced to one repository per account turn. The cursor is durable and the row/route CAS
-- is exact, so a crash repeats at most one idempotent repository post while a superseding policy write resets
-- the cursor and cannot be overwritten by the old worker. Attempts are unchanged; this is fairness yield, not
-- failure. The remaining sentinel moves to the global account tail.
CREATE OR REPLACE FUNCTION core.requeue_policy_refresh_slice_with_authority(
    p_account text,p_epoch bigint,p_after_repo text,p_after_branch text
) RETURNS jsonb
    LANGUAGE plpgsql SECURITY DEFINER SET search_path TO 'core','pg_catalog' AS $$
DECLARE
  v_prev text; v_lease_epoch bigint; v_current_epoch bigint; v_hit int:=0;
  v_repo text; v_branch text; v_due jsonb;
  v_claimed_until timestamptz; v_revoked timestamptz;
BEGIN
  v_repo:=left(NULLIF(btrim(COALESCE(p_after_repo,'')),''),512);
  v_branch:=left(NULLIF(btrim(COALESCE(p_after_branch,'')),''),512);
  IF v_repo IS NULL OR v_branch IS NULL THEN
    RAISE EXCEPTION 'policy slice cursor needs repo and branch' USING ERRCODE='22023';
  END IF;
  v_prev:=current_setting('core.current_account',true);
  PERFORM set_config('core.current_account',p_account,true);
  SELECT convergence_claim_epoch,convergence_claimed_until,revoked_at
    INTO v_lease_epoch,v_claimed_until,v_revoked
    FROM core.installation_account
   WHERE account_id=p_account
   LIMIT 1 FOR UPDATE;
  IF NOT FOUND OR v_revoked IS NOT NULL
     OR v_lease_epoch IS DISTINCT FROM p_epoch
     OR v_claimed_until IS NULL OR v_claimed_until<clock_timestamp() THEN
    PERFORM set_config('core.current_account',COALESCE(v_prev,''),true);
    RETURN jsonb_build_object(
      'requeued',false,'superseded',true,'lease_released',false,'lease_lost',true);
  END IF;
  SELECT policy_epoch INTO v_current_epoch
    FROM core.policy_refresh_outbox
   WHERE account_id=p_account AND request_kind='policy' AND repository_id=''
   FOR UPDATE;
  IF v_claimed_until<clock_timestamp() THEN
    PERFORM set_config('core.current_account',COALESCE(v_prev,''),true);
    RETURN jsonb_build_object(
      'requeued',false,'superseded',true,'lease_released',false,'lease_lost',true);
  END IF;
  IF v_current_epoch IS NULL OR v_current_epoch<>p_epoch THEN
    UPDATE core.installation_account
       SET convergence_claimed_until=NULL,
           convergence_claimed_by=NULL,
           convergence_claim_epoch=NULL
     WHERE account_id=p_account AND convergence_claim_epoch=p_epoch;
    PERFORM set_config('core.current_account',COALESCE(v_prev,''),true);
    RETURN jsonb_build_object(
      'requeued',false,'superseded',true,'lease_released',true);
  END IF;
  PERFORM core.mark_governed_write('policy_refresh_outbox');
  UPDATE core.policy_refresh_outbox
     SET policy_cursor_repo=v_repo,policy_cursor_branch=v_branch,
         change_cursor='',claimed_at=NULL,claimed_by=NULL,not_before=NULL,last_error=NULL
   WHERE account_id=p_account AND request_kind='policy' AND repository_id=''
     AND branch='' AND policy_epoch=p_epoch AND done_at IS NULL;
  GET DIAGNOSTICS v_hit=ROW_COUNT;
  IF v_hit>0 THEN
    v_due:=core._sync_account_convergence_due(p_account,5,900,true);
  END IF;
  UPDATE core.installation_account
     SET convergence_claimed_until=NULL,
         convergence_claimed_by=NULL,
         convergence_claim_epoch=NULL
   WHERE account_id=p_account AND convergence_claim_epoch=p_epoch;
  PERFORM set_config('core.current_account',COALESCE(v_prev,''),true);
  RETURN jsonb_build_object(
    'requeued',v_hit>0,'superseded',v_hit=0,'lease_released',true,
    'tail_rearmed',CASE WHEN v_hit>0 THEN 1 ELSE 0 END);
END $$;
ALTER FUNCTION core.requeue_policy_refresh_slice_with_authority(text,bigint,text,text)
  OWNER TO veripsa_migrator;
REVOKE ALL ON FUNCTION core.requeue_policy_refresh_slice_with_authority(text,bigint,text,text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION core.requeue_policy_refresh_slice_with_authority(text,bigint,text,text)
  TO veripsa_app;

CREATE OR REPLACE FUNCTION core.requeue_policy_refresh_page_with_authority(
    p_account text,p_epoch bigint,p_repo text,p_branch text,p_change_cursor text,
    p_repo_complete boolean
) RETURNS jsonb
    LANGUAGE plpgsql SECURITY DEFINER SET search_path TO 'core','pg_catalog' AS $$
DECLARE
  v_prev text; v_lease_epoch bigint; v_current_epoch bigint; v_hit int:=0;
  v_repo text; v_branch text; v_change text;
  v_claimed_until timestamptz; v_revoked timestamptz;
BEGIN
  v_repo:=left(NULLIF(btrim(COALESCE(p_repo,'')),''),512);
  v_branch:=left(NULLIF(btrim(COALESCE(p_branch,'')),''),512);
  v_change:=left(NULLIF(btrim(COALESCE(p_change_cursor,'')),''),120);
  IF v_repo IS NULL OR v_branch IS NULL
     OR (NOT COALESCE(p_repo_complete,false) AND v_change IS NULL) THEN
    RAISE EXCEPTION 'policy page cursor is incomplete' USING ERRCODE='22023';
  END IF;
  IF COALESCE(p_repo_complete,false) THEN
    RETURN core.requeue_policy_refresh_slice_with_authority(
      p_account,p_epoch,v_repo,v_branch);
  END IF;
  v_prev:=current_setting('core.current_account',true);
  PERFORM set_config('core.current_account',p_account,true);
  SELECT convergence_claim_epoch,convergence_claimed_until,revoked_at
    INTO v_lease_epoch,v_claimed_until,v_revoked
    FROM core.installation_account
   WHERE account_id=p_account
   LIMIT 1 FOR UPDATE;
  IF NOT FOUND OR v_revoked IS NOT NULL
     OR v_lease_epoch IS DISTINCT FROM p_epoch
     OR v_claimed_until IS NULL OR v_claimed_until<clock_timestamp() THEN
    PERFORM set_config('core.current_account',COALESCE(v_prev,''),true);
    RETURN jsonb_build_object(
      'requeued',false,'superseded',true,'lease_released',false,'lease_lost',true);
  END IF;
  SELECT policy_epoch INTO v_current_epoch
    FROM core.policy_refresh_outbox
   WHERE account_id=p_account AND request_kind='policy' AND repository_id=''
   FOR UPDATE;
  IF v_claimed_until<clock_timestamp() THEN
    PERFORM set_config('core.current_account',COALESCE(v_prev,''),true);
    RETURN jsonb_build_object(
      'requeued',false,'superseded',true,'lease_released',false,'lease_lost',true);
  END IF;
  IF v_current_epoch IS DISTINCT FROM p_epoch THEN
    UPDATE core.installation_account
       SET convergence_claimed_until=NULL,convergence_claimed_by=NULL,
           convergence_claim_epoch=NULL
     WHERE account_id=p_account AND convergence_claim_epoch=p_epoch;
    PERFORM set_config('core.current_account',COALESCE(v_prev,''),true);
    RETURN jsonb_build_object('requeued',false,'superseded',true,'lease_released',true);
  END IF;
  PERFORM core.mark_governed_write('policy_refresh_outbox');
  UPDATE core.policy_refresh_outbox
     SET change_cursor=v_change,claimed_at=NULL,claimed_by=NULL,
         not_before=NULL,last_error=NULL
   WHERE account_id=p_account AND request_kind='policy' AND repository_id=''
     AND policy_epoch=p_epoch AND done_at IS NULL;
  GET DIAGNOSTICS v_hit=ROW_COUNT;
  PERFORM core._sync_account_convergence_due(p_account,5,300,true);
  UPDATE core.installation_account
     SET convergence_claimed_until=NULL,convergence_claimed_by=NULL,
         convergence_claim_epoch=NULL
   WHERE account_id=p_account AND convergence_claim_epoch=p_epoch;
  PERFORM set_config('core.current_account',COALESCE(v_prev,''),true);
  RETURN jsonb_build_object(
    'requeued',v_hit>0,'superseded',v_hit=0,'lease_released',true,
    'tail_rearmed',CASE WHEN v_hit>0 THEN 1 ELSE 0 END,'repo_complete',false);
END $$;
ALTER FUNCTION core.requeue_policy_refresh_page_with_authority(
  text,bigint,text,text,text,boolean) OWNER TO veripsa_migrator;
REVOKE ALL ON FUNCTION core.requeue_policy_refresh_page_with_authority(
  text,bigint,text,text,text,boolean) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION core.requeue_policy_refresh_page_with_authority(
  text,bigint,text,text,text,boolean) TO veripsa_app;

-- Supersession is committed by enqueueing the new HEAD inside graph TXN 1. The ordinary finish CAS intentionally
-- touches neither lease nor due on that miss; this separate lease-generation CAS hands the account turn back
-- without permitting a late worker to clear a newer owner's lease.
CREATE OR REPLACE FUNCTION core.release_superseded_convergence_lease_with_authority(
    p_account text,p_epoch bigint
) RETURNS boolean
    LANGUAGE plpgsql SECURITY DEFINER SET search_path TO 'core','pg_catalog' AS $$
DECLARE
  v_prev text; v_lease_epoch bigint; v_released boolean;
  v_revoked timestamptz;
BEGIN
  v_prev:=current_setting('core.current_account',true);
  PERFORM set_config('core.current_account',p_account,true);
  SELECT convergence_claim_epoch,revoked_at INTO v_lease_epoch,v_revoked
    FROM core.installation_account
   WHERE account_id=p_account
   LIMIT 1 FOR UPDATE;
  IF NOT FOUND THEN
    PERFORM set_config('core.current_account',COALESCE(v_prev,''),true);
    RETURN false;
  END IF;
  UPDATE core.installation_account
     SET convergence_claimed_until=NULL,
         convergence_claimed_by=NULL,
         convergence_claim_epoch=NULL
   WHERE account_id=p_account AND convergence_claim_epoch=p_epoch;
  v_released:=FOUND;
  IF v_revoked IS NULL THEN
    -- Graph turns use row leases, not the policy-exclusive account lease. A superseding enqueue clears the old
    -- row claim; reconcile the bounded single graph claim router and latest due coordinate.
    PERFORM core._sync_graph_claim_router(p_account,300);
    PERFORM core._sync_account_convergence_due(p_account,5,300,true);
  END IF;
  PERFORM set_config('core.current_account',COALESCE(v_prev,''),true);
  RETURN COALESCE(v_released,false);
END $$;
ALTER FUNCTION core.release_superseded_convergence_lease_with_authority(text,bigint)
  OWNER TO veripsa_migrator;
REVOKE ALL ON FUNCTION core.release_superseded_convergence_lease_with_authority(text,bigint) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION core.release_superseded_convergence_lease_with_authority(text,bigint)
  TO veripsa_app;

CREATE OR REPLACE FUNCTION core.policy_refresh_turn_is_current_with_authority(
    p_account text,p_kind text,p_repository_id text,p_branch text,p_request_epoch bigint,
    p_slot smallint,p_lease_epoch bigint
) RETURNS boolean
    LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path TO 'core','pg_catalog' AS $$
DECLARE v_prev text; v_ok boolean;
BEGIN
  IF p_kind<>'graph' THEN RETURN false; END IF;
  v_prev:=current_setting('core.current_account',true);
  PERFORM set_config('core.current_account',p_account,true);
  SELECT EXISTS (
    SELECT 1
      FROM core.graph_convergence_lease l
      JOIN core.policy_refresh_outbox q
        ON q.account_id=l.account_id AND q.request_kind='graph'
       AND q.repository_id=l.repository_id
     WHERE l.account_id=p_account AND l.slot=p_slot
       AND l.lease_epoch=p_lease_epoch AND l.request_epoch=p_request_epoch
       AND l.repository_id=p_repository_id AND l.branch=p_branch
       AND l.claimed_until>=clock_timestamp()
       AND q.policy_epoch=p_request_epoch AND q.branch=p_branch AND q.done_at IS NULL
       AND EXISTS (
         SELECT 1 FROM core.installation_account r
          WHERE r.account_id=p_account AND r.revoked_at IS NULL))
    INTO v_ok;
  PERFORM set_config('core.current_account',COALESCE(v_prev,''),true);
  RETURN v_ok;
END $$;
ALTER FUNCTION core.policy_refresh_turn_is_current_with_authority(
  text,text,text,text,bigint,smallint,bigint) OWNER TO veripsa_migrator;
REVOKE ALL ON FUNCTION core.policy_refresh_turn_is_current_with_authority(
  text,text,text,text,bigint,smallint,bigint) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION core.policy_refresh_turn_is_current_with_authority(
  text,text,text,text,bigint,smallint,bigint) TO veripsa_app;

-- Read authority for one external mutation while the caller holds the live
-- event's identical per-repository SESSION advisory lock.  The required wall
-- is checked after lock acquisition, so time spent waiting cannot leak a
-- stale Check/comment.  These functions intentionally mutate nothing.
CREATE OR REPLACE FUNCTION core.policy_refresh_external_write_fence_with_authority(
    p_account text,p_kind text,p_repository_id text,p_branch text,
    p_request_epoch bigint,p_min_remaining_seconds int
) RETURNS boolean
    LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path TO 'core','pg_catalog' AS $$
DECLARE v_context text; v_until timestamptz; v_ok boolean:=false;
BEGIN
  SELECT account INTO v_context
    FROM core.establish_session_write_context() AS c(agent,account);
  IF v_context IS DISTINCT FROM p_account
     OR p_kind<>'policy' OR p_repository_id<>'' OR p_branch<>''
     OR p_request_epoch IS NULL OR p_request_epoch<1
     OR p_min_remaining_seconds IS NULL
     OR p_min_remaining_seconds NOT BETWEEN 1 AND 270 THEN
    RETURN false;
  END IF;
  SELECT r.convergence_claimed_until INTO v_until
    FROM core.installation_account r
    JOIN core.policy_refresh_outbox q
      ON q.account_id=r.account_id AND q.request_kind='policy'
     AND q.repository_id=''
   WHERE r.account_id=p_account AND r.revoked_at IS NULL
     AND r.convergence_claim_epoch=p_request_epoch
     AND q.policy_epoch=p_request_epoch
     AND q.branch='' AND q.done_at IS NULL AND q.claimed_at IS NOT NULL;
  v_ok:=FOUND AND v_until>=clock_timestamp()
        +make_interval(secs=>p_min_remaining_seconds);
  RETURN COALESCE(v_ok,false);
END $$;
ALTER FUNCTION core.policy_refresh_external_write_fence_with_authority(
  text,text,text,text,bigint,int) OWNER TO veripsa_migrator;
REVOKE ALL ON FUNCTION core.policy_refresh_external_write_fence_with_authority(
  text,text,text,text,bigint,int) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION core.policy_refresh_external_write_fence_with_authority(
  text,text,text,text,bigint,int) TO veripsa_app;

CREATE OR REPLACE FUNCTION core.policy_refresh_external_write_fence_with_authority(
    p_account text,p_kind text,p_repository_id text,p_branch text,
    p_request_epoch bigint,p_slot smallint,p_lease_epoch bigint,
    p_min_remaining_seconds int
) RETURNS boolean
    LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path TO 'core','pg_catalog' AS $$
DECLARE v_context text; v_until timestamptz; v_ok boolean:=false;
BEGIN
  SELECT account INTO v_context
    FROM core.establish_session_write_context() AS c(agent,account);
  IF v_context IS DISTINCT FROM p_account
     OR p_kind<>'graph'
     OR p_repository_id IS NULL OR p_repository_id=''
     OR p_branch IS NULL OR p_branch=''
     OR p_request_epoch IS NULL OR p_request_epoch<1
     OR p_slot IS DISTINCT FROM 1
     OR p_lease_epoch IS NULL OR p_lease_epoch<1
     OR p_min_remaining_seconds IS NULL
     OR p_min_remaining_seconds NOT BETWEEN 1 AND 270 THEN
    RETURN false;
  END IF;
  SELECT l.claimed_until INTO v_until
    FROM core.graph_convergence_lease l
    JOIN core.policy_refresh_outbox q
      ON q.account_id=l.account_id AND q.request_kind='graph'
     AND q.repository_id=l.repository_id
    JOIN core.installation_account r ON r.account_id=l.account_id
   WHERE l.account_id=p_account
     AND l.slot=p_slot
     AND l.lease_epoch=p_lease_epoch
     AND l.request_epoch=p_request_epoch
     AND l.repository_id=p_repository_id
     AND l.branch=p_branch
     AND q.policy_epoch=p_request_epoch
     AND q.branch=p_branch
     AND q.done_at IS NULL
     AND NOT q.onboarding_head_pending
     -- quota_paused is a durable flow-control state, not a terminal failure.
     -- Once its not_before wall is due, the ordinary graph claim above mints
     -- a new exact request+slot+lease authority for one autonomous re-probe.
     -- Keep the quota counter/alert honest until that probe either finishes
     -- or re-defers; the lease tuple (not this reason) is the write fence.
     AND (q.terminal_reason IS NULL OR q.terminal_reason='quota_paused')
     AND r.revoked_at IS NULL;
  v_ok:=FOUND AND v_until>=clock_timestamp()
        +make_interval(secs=>p_min_remaining_seconds);
  RETURN COALESCE(v_ok,false);
END $$;
ALTER FUNCTION core.policy_refresh_external_write_fence_with_authority(
  text,text,text,text,bigint,smallint,bigint,int) OWNER TO veripsa_migrator;
REVOKE ALL ON FUNCTION core.policy_refresh_external_write_fence_with_authority(
  text,text,text,text,bigint,smallint,bigint,int) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION core.policy_refresh_external_write_fence_with_authority(
  text,text,text,text,bigint,smallint,bigint,int) TO veripsa_app;

-- Final graph persistence is permitted only while the exact durable claim that
-- started the extractor still owns its slot with enough lease wall left for
-- one bounded DB statement.  A boolean preflight is not authority: the worker
-- may be descheduled after it.  These locks and checks execute in the SAME
-- transaction as the graph DELETE/INSERT performed by the wrappers below.
CREATE OR REPLACE FUNCTION core._assert_graph_convergence_write_lease_with_authority(
    p_repo text,p_repository_id text,p_branch text,p_target_sha text,
    p_request_epoch bigint,p_slot smallint,p_lease_epoch bigint
) RETURNS text
    LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path TO 'core','pg_catalog' AS $$
DECLARE
  v_account text; v_repo text; v_id text; v_branch text; v_target text;
  v_until timestamptz; v_now timestamptz;
BEGIN
  v_repo:=NULLIF(btrim(COALESCE(p_repo,'')),'');
  v_id:=NULLIF(btrim(COALESCE(p_repository_id,'')),'');
  v_branch:=NULLIF(btrim(COALESCE(p_branch,'')),'');
  v_target:=lower(NULLIF(btrim(COALESCE(p_target_sha,'')),''));
  IF v_repo IS NULL OR length(v_repo)>512
     OR v_id IS NULL OR length(v_id)>32 OR v_id !~ '^[1-9][0-9]*$'
     OR v_branch IS NULL OR length(v_branch)>512
     OR v_target IS NULL OR length(v_target) NOT BETWEEN 7 AND 64
     OR v_target !~ '^[0-9a-f]+$'
     OR p_request_epoch IS NULL OR p_request_epoch<1
     OR p_slot IS DISTINCT FROM 1
     OR p_lease_epoch IS NULL OR p_lease_epoch<1 THEN
    RAISE EXCEPTION 'graph convergence write lease token is malformed'
      USING ERRCODE='55000';
  END IF;
  SELECT account INTO v_account
    FROM core.establish_session_write_context() AS c(agent,account);
  PERFORM core.assert_account_live_with_authority();

  -- The route lock is the enqueue/reclaim serialization boundary.  The exact
  -- lease and desired row stay locked until the caller's graph transaction
  -- commits, so neither a newer target nor a reclaim can cross the mutation.
  PERFORM 1
    FROM core.installation_account
   WHERE account_id=v_account AND revoked_at IS NULL
   FOR UPDATE;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'graph convergence account route is no longer live'
      USING ERRCODE='55000';
  END IF;
  SELECT claimed_until INTO v_until
    FROM core.graph_convergence_lease
   WHERE account_id=v_account
     AND slot=p_slot
     AND lease_epoch=p_lease_epoch
     AND request_epoch=p_request_epoch
     AND repository_id=v_id
     AND repo=v_repo
     AND branch=v_branch
     AND target_sha=v_target
   FOR UPDATE;
  v_now:=clock_timestamp();
  IF NOT FOUND OR v_until < v_now + interval '35 seconds' THEN
    RAISE EXCEPTION 'graph convergence write lease is stale or has insufficient wall'
      USING ERRCODE='55000';
  END IF;
  PERFORM 1
    FROM core.policy_refresh_outbox
   WHERE account_id=v_account
     AND request_kind='graph'
     AND repository_id=v_id
     AND repo=v_repo
     AND branch=v_branch
     AND target_sha=v_target
     AND policy_epoch=p_request_epoch
     AND done_at IS NULL
     AND NOT onboarding_head_pending
     -- A due quota_paused row is deliberately still quota-classified while
     -- its newly claimed extractor runs.  The exact locked lease/request
     -- tuple above is its authority; accepting the reason here avoids turning
     -- a healthy quota re-probe into a synthetic writer failure/attempt.
     AND (terminal_reason IS NULL OR terminal_reason='quota_paused')
   FOR UPDATE;
  IF NOT FOUND OR v_until < clock_timestamp() + interval '35 seconds' THEN
    RAISE EXCEPTION 'graph convergence desired fact changed before persistence'
      USING ERRCODE='55000';
  END IF;
  RETURN v_account;
END $$;
ALTER FUNCTION core._assert_graph_convergence_write_lease_with_authority(
  text,text,text,text,bigint,smallint,bigint) OWNER TO veripsa_migrator;
REVOKE ALL ON FUNCTION core._assert_graph_convergence_write_lease_with_authority(
  text,text,text,text,bigint,smallint,bigint) FROM PUBLIC;

CREATE OR REPLACE FUNCTION core.ingest_graph_with_authority_for_convergence_lease(
    p_graph jsonb,p_repo text,p_branch text,p_commit_sha text,p_captured_at timestamptz,
    p_repository_id text,p_generation jsonb,p_request_epoch bigint,
    p_slot smallint,p_lease_epoch bigint
) RETURNS text
    LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path TO 'core','pg_catalog' AS $$
BEGIN
  -- Lifecycle generation first preserves the global stable-id -> repo ->
  -- account lock order used by signed live events.
  PERFORM core._assert_repository_graph_generation_with_authority(
    p_repo,p_repository_id,p_generation);
  PERFORM core._assert_graph_convergence_write_lease_with_authority(
    p_repo,p_repository_id,p_branch,p_commit_sha,
    p_request_epoch,p_slot,p_lease_epoch);
  RETURN core.ingest_graph_with_authority(
    p_graph,p_repo,p_branch,p_commit_sha,p_captured_at);
END $$;
ALTER FUNCTION core.ingest_graph_with_authority_for_convergence_lease(
  jsonb,text,text,text,timestamptz,text,jsonb,bigint,smallint,bigint)
  OWNER TO veripsa_migrator;
REVOKE ALL ON FUNCTION core.ingest_graph_with_authority_for_convergence_lease(
  jsonb,text,text,text,timestamptz,text,jsonb,bigint,smallint,bigint)
  FROM PUBLIC,veripsa_writer;
GRANT EXECUTE ON FUNCTION core.ingest_graph_with_authority_for_convergence_lease(
  jsonb,text,text,text,timestamptz,text,jsonb,bigint,smallint,bigint)
  TO veripsa_app;

CREATE OR REPLACE FUNCTION core.patch_graph_with_authority_for_convergence_lease(
    p_subgraph jsonb,p_repo text,p_branch text,p_changed_paths text[],
    p_removed_paths text[],p_commit_sha text,p_captured_at timestamptz,
    p_repository_id text,p_generation jsonb,p_request_epoch bigint,
    p_slot smallint,p_lease_epoch bigint
) RETURNS jsonb
    LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path TO 'core','pg_catalog' AS $$
BEGIN
  PERFORM core._assert_repository_graph_generation_with_authority(
    p_repo,p_repository_id,p_generation);
  PERFORM core._assert_graph_convergence_write_lease_with_authority(
    p_repo,p_repository_id,p_branch,p_commit_sha,
    p_request_epoch,p_slot,p_lease_epoch);
  RETURN core.patch_graph_with_authority(
    p_subgraph,p_repo,p_branch,p_changed_paths,p_removed_paths,p_commit_sha,p_captured_at);
END $$;
ALTER FUNCTION core.patch_graph_with_authority_for_convergence_lease(
  jsonb,text,text,text[],text[],text,timestamptz,text,jsonb,bigint,smallint,bigint)
  OWNER TO veripsa_migrator;
REVOKE ALL ON FUNCTION core.patch_graph_with_authority_for_convergence_lease(
  jsonb,text,text,text[],text[],text,timestamptz,text,jsonb,bigint,smallint,bigint)
  FROM PUBLIC,veripsa_writer;
GRANT EXECUTE ON FUNCTION core.patch_graph_with_authority_for_convergence_lease(
  jsonb,text,text,text[],text[],text,timestamptz,text,jsonb,bigint,smallint,bigint)
  TO veripsa_app;

-- Resolve the only GitHub read omitted from live installation ingress. The reserved phase coordinate is replaced
-- by an authoritative default-branch HEAD under exact request+slot+lease CAS, then the same account-fair row moves
-- to the scheduler tail. An empty repository has no Check anchor or PRs, so its phase completes durably here; its
-- first real default-branch push will enqueue the ordinary graph cold-start and Watching surface.
CREATE OR REPLACE FUNCTION core.resolve_graph_onboarding_head_with_authority(
    p_account text,p_repository_id text,p_branch text,p_request_epoch bigint,
    p_slot smallint,p_lease_epoch bigint,
    p_installation_id text,p_installation_created_at timestamptz,
    p_resolved_branch text,p_target_sha text
) RETURNS jsonb
    LANGUAGE plpgsql SECURITY DEFINER SET search_path TO 'core','pg_catalog' AS $$
DECLARE
  v_prev text; v_claimed_until timestamptz; v_revoked timestamptz;
  v_current_installation_id text; v_current_installation_created_at timestamptz;
  v_hit int:=0; v_attempts int; v_reason text;
  v_branch text:=left(NULLIF(btrim(COALESCE(p_resolved_branch,'')),''),512);
  v_target text:=lower(NULLIF(btrim(COALESCE(p_target_sha,'')),''));
  v_empty boolean:=p_target_sha IS NULL;
BEGIN
  IF v_branch IS NULL OR (NOT v_empty AND (v_target IS NULL
       OR length(v_target) NOT BETWEEN 7 AND 64 OR v_target !~ '^[0-9a-f]+$')) THEN
    RAISE EXCEPTION 'invalid resolved onboarding HEAD' USING ERRCODE='22023';
  END IF;
  v_prev:=current_setting('core.current_account',true);
  PERFORM set_config('core.current_account',p_account,true);
  SELECT revoked_at,github_installation_id,github_installation_created_at
    INTO v_revoked,v_current_installation_id,v_current_installation_created_at
    FROM core.installation_account
   WHERE account_id=p_account
   LIMIT 1 FOR UPDATE;
  IF NOT FOUND OR v_revoked IS NOT NULL
     OR p_installation_id IS NULL
     OR length(p_installation_id) NOT BETWEEN 1 AND 64
     OR p_installation_id !~ '^[1-9][0-9]*$'
     OR p_installation_created_at IS NULL
     OR v_current_installation_id IS DISTINCT FROM p_installation_id
     OR v_current_installation_created_at IS DISTINCT FROM p_installation_created_at THEN
    -- The remote HEAD proof belonged to an older installation generation. Preserve the duplicate G2 onboarding
    -- row byte-for-byte and release only G1's exact lease snapshot; reading G2's current tuple here to authorize
    -- G1 would recreate the TOCTOU. The new generation remains due and will resolve HEAD itself.
    DELETE FROM core.graph_convergence_lease
     WHERE account_id=p_account AND slot=p_slot AND lease_epoch=p_lease_epoch
       AND request_epoch=p_request_epoch AND repository_id=p_repository_id
       AND branch=p_branch;
    PERFORM core._sync_graph_claim_router(p_account,300);
    PERFORM core._sync_account_convergence_due(p_account,5,300,true);
    PERFORM set_config('core.current_account',COALESCE(v_prev,''),true);
    RETURN jsonb_build_object(
      'requeued',false,'superseded',true,'lease_released',true,
      'installation_generation_changed',true);
  END IF;
  SELECT claimed_until INTO v_claimed_until
    FROM core.graph_convergence_lease
   WHERE account_id=p_account AND slot=p_slot AND lease_epoch=p_lease_epoch
     AND request_epoch=p_request_epoch AND repository_id=p_repository_id
     AND branch=p_branch
   FOR UPDATE;
  IF NOT FOUND OR v_claimed_until<clock_timestamp() THEN
    PERFORM set_config('core.current_account',COALESCE(v_prev,''),true);
    RETURN jsonb_build_object('requeued',false,'superseded',true,'lease_lost',true);
  END IF;
  SELECT attempts,terminal_reason INTO v_attempts,v_reason
    FROM core.policy_refresh_outbox
   WHERE account_id=p_account AND request_kind='graph'
     AND repository_id=p_repository_id AND branch=p_branch
     AND policy_epoch=p_request_epoch AND done_at IS NULL
     AND onboarding_pending AND onboarding_head_pending
   FOR UPDATE;
  IF NOT FOUND OR v_claimed_until<clock_timestamp() THEN
    DELETE FROM core.graph_convergence_lease
     WHERE account_id=p_account AND slot=p_slot AND lease_epoch=p_lease_epoch
       AND request_epoch=p_request_epoch;
    PERFORM core._sync_graph_claim_router(p_account,300);
    PERFORM core._sync_account_convergence_due(p_account,5,300,true);
    PERFORM set_config('core.current_account',COALESCE(v_prev,''),true);
    RETURN jsonb_build_object('requeued',false,'superseded',true,'lease_released',true);
  END IF;
  PERFORM core.mark_governed_write('policy_refresh_outbox');
  IF v_empty THEN
    -- There is no truthful graph target for an empty repository. Delete the phase row instead of retaining the
    -- reserved token as a completed ordinary coordinate. The governed DELETE trigger removes its exact lease,
    -- decrements the prior attempt classification and repairs both account due routers atomically.
    DELETE FROM core.policy_refresh_outbox
     WHERE account_id=p_account AND request_kind='graph'
       AND repository_id=p_repository_id AND branch=p_branch
       AND policy_epoch=p_request_epoch AND done_at IS NULL
       AND onboarding_pending AND onboarding_head_pending;
  ELSE
    UPDATE core.policy_refresh_outbox
       SET branch=v_branch,target_sha=v_target,onboarding_head_pending=false,
           onboarding_plan=NULL,onboarding_index=0,onboarding_truncated=false,
           onboarding_watching_done=false,enqueued_at=clock_timestamp(),
           attempts=0,last_error=NULL,not_before=NULL,terminal_reason=NULL,
           change_cursor='',surface_dirty=true
     WHERE account_id=p_account AND request_kind='graph'
       AND repository_id=p_repository_id AND branch=p_branch
       AND policy_epoch=p_request_epoch AND done_at IS NULL
       AND onboarding_pending AND onboarding_head_pending;
  END IF;
  GET DIAGNOSTICS v_hit=ROW_COUNT;
  IF NOT v_empty THEN
    DELETE FROM core.graph_convergence_lease
     WHERE account_id=p_account AND slot=p_slot AND lease_epoch=p_lease_epoch
       AND request_epoch=p_request_epoch;
  END IF;
  IF NOT v_empty AND v_hit>0 THEN
    UPDATE core.installation_account
       SET convergence_pending_count=GREATEST(0,convergence_pending_count
             -CASE WHEN v_reason IS NULL AND v_attempts<5 THEN 1 ELSE 0 END
             +1),
           convergence_retry_exhausted_count=GREATEST(0,convergence_retry_exhausted_count
             -CASE WHEN v_reason IS NULL AND v_attempts>=5 THEN 1 ELSE 0 END),
           convergence_quota_deferred_count=GREATEST(0,convergence_quota_deferred_count
             -CASE WHEN v_reason='quota_paused' THEN 1 ELSE 0 END)
     WHERE account_id=p_account;
  END IF;
  PERFORM core._sync_graph_claim_router(p_account,300);
  PERFORM core._sync_account_convergence_due(p_account,5,300,true);
  PERFORM set_config('core.current_account',COALESCE(v_prev,''),true);
  RETURN jsonb_build_object(
    'requeued',v_hit>0 AND NOT v_empty,'finished_empty',v_hit>0 AND v_empty,
    'superseded',v_hit=0,'lease_released',true,
    'tail_rearmed',CASE WHEN v_hit>0 AND NOT v_empty THEN 1 ELSE 0 END);
END $$;
ALTER FUNCTION core.resolve_graph_onboarding_head_with_authority(
  text,text,text,bigint,smallint,bigint,text,timestamptz,text,text) OWNER TO veripsa_migrator;
REVOKE ALL ON FUNCTION core.resolve_graph_onboarding_head_with_authority(
  text,text,text,bigint,smallint,bigint,text,timestamptz,text,text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION core.resolve_graph_onboarding_head_with_authority(
  text,text,text,bigint,smallint,bigint,text,timestamptz,text,text) TO veripsa_app;

-- Rolling predecessor bridge. The old ABI never carried the claimed installation generation, so it cannot
-- authorize a remote result after uninstall/reinstall. Fail closed: retain the outbox and release only this
-- exact old lease. Never fill the missing tuple from the current route (that would bless G1 with G2 authority).
CREATE OR REPLACE FUNCTION core.resolve_graph_onboarding_head_with_authority(
    p_account text,p_repository_id text,p_branch text,p_request_epoch bigint,
    p_slot smallint,p_lease_epoch bigint,p_resolved_branch text,p_target_sha text
) RETURNS jsonb
    LANGUAGE plpgsql SECURITY DEFINER SET search_path TO 'core','pg_catalog' AS $$
DECLARE v_prev text;
BEGIN
  v_prev:=current_setting('core.current_account',true);
  PERFORM set_config('core.current_account',p_account,true);
  PERFORM 1 FROM core.installation_account WHERE account_id=p_account FOR UPDATE;
  DELETE FROM core.graph_convergence_lease
   WHERE account_id=p_account AND slot=p_slot AND lease_epoch=p_lease_epoch
     AND request_epoch=p_request_epoch AND repository_id=p_repository_id
     AND branch=p_branch;
  PERFORM core._sync_graph_claim_router(p_account,300);
  PERFORM core._sync_account_convergence_due(p_account,5,300,true);
  PERFORM set_config('core.current_account',COALESCE(v_prev,''),true);
  RETURN jsonb_build_object(
    'requeued',false,'superseded',true,'lease_released',true,
    'legacy_installation_generation_missing',true);
END $$;
ALTER FUNCTION core.resolve_graph_onboarding_head_with_authority(
  text,text,text,bigint,smallint,bigint,text,text) OWNER TO veripsa_migrator;
REVOKE ALL ON FUNCTION core.resolve_graph_onboarding_head_with_authority(
  text,text,text,bigint,smallint,bigint,text,text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION core.resolve_graph_onboarding_head_with_authority(
  text,text,text,bigint,smallint,bigint,text,text) TO veripsa_app;

-- Exact attempts-neutral continuation for the installation phase carried by a graph row.  `freeze` persists one
-- immutable, content-free, <=300 PR-number plan after a CAP+1 GitHub read; `advance` consumes exactly one planned
-- PR.  `quota_advance` retains the durable quota phase so later PR turns publish only the paused surface and
-- never repeat clone/extraction. `complete` is accepted only after the worker received the idempotent
-- Watching-check receipt. Every action releases the one graph slot and moves this SAME request epoch to the fair
-- scheduler tail.
CREATE OR REPLACE FUNCTION core.requeue_graph_onboarding_with_authority(
    p_account text,p_repository_id text,p_branch text,p_request_epoch bigint,
    p_slot smallint,p_lease_epoch bigint,
    p_installation_id text,p_installation_created_at timestamptz,
    p_action text,p_plan bigint[],
    p_truncated boolean,p_expected_index int,p_pr_number bigint,p_watching_done boolean
) RETURNS jsonb
    LANGUAGE plpgsql SECURITY DEFINER SET search_path TO 'core','pg_catalog' AS $$
DECLARE
  v_prev text; v_claimed_until timestamptz; v_revoked timestamptz;
  v_current_installation_id text; v_current_installation_created_at timestamptz;
  v_plan bigint[]; v_index int; v_pending boolean; v_head_pending boolean;
  v_watching_done boolean; v_attempts int; v_reason text; v_hit int:=0;
  v_action text:=lower(btrim(COALESCE(p_action,'')));
  v_new_reason text;
BEGIN
  IF v_action NOT IN (
      'freeze','advance','quota_advance','watch','complete') THEN
    RAISE EXCEPTION 'unsupported graph onboarding transition' USING ERRCODE='22023';
  END IF;
  v_new_reason:=CASE
    WHEN v_action='quota_advance' THEN 'quota_paused'
    ELSE NULL
  END;
  IF v_action='freeze' THEN
    IF p_plan IS NULL OR cardinality(p_plan)>300 OR COALESCE(p_watching_done,false)
       OR EXISTS (SELECT 1 FROM unnest(p_plan) n WHERE n IS NULL OR n<1)
       OR p_plan IS DISTINCT FROM ARRAY(
         SELECT DISTINCT n FROM unnest(p_plan) n ORDER BY n) THEN
      RAISE EXCEPTION 'invalid graph onboarding plan' USING ERRCODE='22023';
    END IF;
  ELSIF v_action IN ('advance','quota_advance') THEN
    IF p_expected_index IS NULL OR p_expected_index NOT BETWEEN 0 AND 299
       OR p_pr_number IS NULL OR p_pr_number<1 THEN
      RAISE EXCEPTION 'invalid graph onboarding cursor' USING ERRCODE='22023';
    END IF;
  ELSIF v_action='watch' AND NOT COALESCE(p_watching_done,false) THEN
    RAISE EXCEPTION 'graph onboarding watch transition lacks receipt' USING ERRCODE='22023';
  END IF;

  v_prev:=current_setting('core.current_account',true);
  PERFORM set_config('core.current_account',p_account,true);
  SELECT revoked_at,github_installation_id,github_installation_created_at
    INTO v_revoked,v_current_installation_id,v_current_installation_created_at
    FROM core.installation_account
   WHERE account_id=p_account
   LIMIT 1 FOR UPDATE;
  IF NOT FOUND OR v_revoked IS NOT NULL
     OR p_installation_id IS NULL
     OR length(p_installation_id) NOT BETWEEN 1 AND 64
     OR p_installation_id !~ '^[1-9][0-9]*$'
     OR p_installation_created_at IS NULL
     OR v_current_installation_id IS DISTINCT FROM p_installation_id
     OR v_current_installation_created_at IS DISTINCT FROM p_installation_created_at THEN
    DELETE FROM core.graph_convergence_lease
     WHERE account_id=p_account AND slot=p_slot AND lease_epoch=p_lease_epoch
       AND request_epoch=p_request_epoch AND repository_id=p_repository_id
       AND branch=p_branch;
    PERFORM core._sync_graph_claim_router(p_account,300);
    PERFORM core._sync_account_convergence_due(p_account,5,300,true);
    PERFORM set_config('core.current_account',COALESCE(v_prev,''),true);
    RETURN jsonb_build_object(
      'requeued',false,'superseded',true,'lease_released',true,
      'installation_generation_changed',true);
  END IF;
  SELECT claimed_until INTO v_claimed_until
    FROM core.graph_convergence_lease
   WHERE account_id=p_account AND slot=p_slot AND lease_epoch=p_lease_epoch
     AND request_epoch=p_request_epoch AND repository_id=p_repository_id
     AND branch=p_branch
   FOR UPDATE;
  IF NOT FOUND OR v_claimed_until<clock_timestamp() THEN
    PERFORM set_config('core.current_account',COALESCE(v_prev,''),true);
    RETURN jsonb_build_object('requeued',false,'superseded',true,'lease_lost',true);
  END IF;
  SELECT onboarding_pending,onboarding_head_pending,onboarding_plan,onboarding_index,
         onboarding_watching_done,attempts,terminal_reason
    INTO v_pending,v_head_pending,v_plan,v_index,v_watching_done,v_attempts,v_reason
    FROM core.policy_refresh_outbox
   WHERE account_id=p_account AND request_kind='graph'
     AND repository_id=p_repository_id AND branch=p_branch
     AND policy_epoch=p_request_epoch AND done_at IS NULL
   FOR UPDATE;
  IF v_claimed_until<clock_timestamp() THEN
    PERFORM set_config('core.current_account',COALESCE(v_prev,''),true);
    RETURN jsonb_build_object('requeued',false,'superseded',true,'lease_lost',true);
  END IF;
  IF NOT FOUND OR NOT COALESCE(v_pending,false) OR COALESCE(v_head_pending,false)
     OR (v_action='freeze' AND v_plan IS NOT NULL)
     OR (v_action IN ('advance','quota_advance') AND (
       v_plan IS NULL OR v_index<>p_expected_index
       OR v_index>=cardinality(v_plan) OR v_plan[v_index+1]<>p_pr_number))
     OR (v_action='watch' AND (
       v_plan IS NULL OR v_index<>cardinality(v_plan) OR v_watching_done))
     OR (v_action='complete' AND (
       v_plan IS NULL OR v_index<>cardinality(v_plan) OR NOT v_watching_done)) THEN
    DELETE FROM core.graph_convergence_lease
     WHERE account_id=p_account AND slot=p_slot AND lease_epoch=p_lease_epoch
       AND request_epoch=p_request_epoch;
    PERFORM core._sync_graph_claim_router(p_account,300);
    PERFORM core._sync_account_convergence_due(p_account,5,300,true);
    PERFORM set_config('core.current_account',COALESCE(v_prev,''),true);
    RETURN jsonb_build_object(
      'requeued',false,'superseded',true,'lease_released',true);
  END IF;

  PERFORM core.mark_governed_write('policy_refresh_outbox');
  IF v_action='freeze' THEN
    UPDATE core.policy_refresh_outbox
       SET onboarding_plan=p_plan,onboarding_index=0,
           onboarding_truncated=COALESCE(p_truncated,false),
           onboarding_watching_done=false,
           enqueued_at=clock_timestamp(),attempts=0,last_error=NULL,
           not_before=NULL,terminal_reason=v_new_reason
     WHERE account_id=p_account AND request_kind='graph'
       AND repository_id=p_repository_id AND branch=p_branch
       AND policy_epoch=p_request_epoch AND done_at IS NULL
       AND onboarding_pending AND onboarding_plan IS NULL;
  ELSIF v_action IN ('advance','quota_advance') THEN
    UPDATE core.policy_refresh_outbox
       SET onboarding_index=p_expected_index+1,
           enqueued_at=clock_timestamp(),attempts=0,last_error=NULL,
           not_before=NULL,terminal_reason=v_new_reason
     WHERE account_id=p_account AND request_kind='graph'
       AND repository_id=p_repository_id AND branch=p_branch
       AND policy_epoch=p_request_epoch AND done_at IS NULL
       AND onboarding_pending AND onboarding_index=p_expected_index
       AND onboarding_plan[p_expected_index+1]=p_pr_number;
  ELSIF v_action='watch' THEN
    UPDATE core.policy_refresh_outbox
       SET onboarding_watching_done=true,enqueued_at=clock_timestamp(),
           attempts=0,last_error=NULL,not_before=NULL,terminal_reason=NULL
     WHERE account_id=p_account AND request_kind='graph'
       AND repository_id=p_repository_id AND branch=p_branch
       AND policy_epoch=p_request_epoch AND done_at IS NULL
       AND onboarding_pending AND onboarding_plan IS NOT NULL
       AND onboarding_index=cardinality(onboarding_plan)
       AND NOT onboarding_watching_done;
  ELSE
    UPDATE core.policy_refresh_outbox
       SET onboarding_pending=false,onboarding_head_pending=false,
           onboarding_plan=NULL,onboarding_index=0,
           onboarding_truncated=false,onboarding_watching_done=false,
           enqueued_at=clock_timestamp(),attempts=0,last_error=NULL,not_before=NULL,
           terminal_reason=NULL,change_cursor='',surface_dirty=true
     WHERE account_id=p_account AND request_kind='graph'
       AND repository_id=p_repository_id AND branch=p_branch
       AND policy_epoch=p_request_epoch AND done_at IS NULL
       AND onboarding_pending AND onboarding_plan IS NOT NULL
       AND onboarding_index=cardinality(onboarding_plan)
       AND onboarding_watching_done;
  END IF;
  GET DIAGNOSTICS v_hit=ROW_COUNT;
  IF v_hit>0 THEN
    -- Every successful phase boundary gets a fresh retry budget. Sparse failures on different PR cursors must
    -- never accumulate into one poisoned repository; keep the account router's three classifications exact.
    UPDATE core.installation_account
       SET convergence_pending_count=GREATEST(0,convergence_pending_count
             -CASE WHEN v_reason IS NULL AND v_attempts<5 THEN 1 ELSE 0 END
             +CASE WHEN v_new_reason IS NULL THEN 1 ELSE 0 END),
           convergence_retry_exhausted_count=GREATEST(0,convergence_retry_exhausted_count
             -CASE WHEN v_reason IS NULL AND v_attempts>=5 THEN 1 ELSE 0 END),
           convergence_quota_deferred_count=GREATEST(0,convergence_quota_deferred_count
             -CASE WHEN v_reason='quota_paused' THEN 1 ELSE 0 END
             +CASE WHEN v_new_reason='quota_paused' THEN 1 ELSE 0 END)
     WHERE account_id=p_account;
  END IF;
  DELETE FROM core.graph_convergence_lease
   WHERE account_id=p_account AND slot=p_slot AND lease_epoch=p_lease_epoch
     AND request_epoch=p_request_epoch;
  PERFORM core._sync_graph_claim_router(p_account,300);
  PERFORM core._sync_account_convergence_due(p_account,5,300,true);
  PERFORM set_config('core.current_account',COALESCE(v_prev,''),true);
  RETURN jsonb_build_object(
    'requeued',v_hit>0,'superseded',v_hit=0,'lease_released',true,
    'tail_rearmed',CASE WHEN v_hit>0 THEN 1 ELSE 0 END,
    'action',v_action,
    'onboarding_index',CASE
      WHEN v_action IN ('advance','quota_advance') AND v_hit>0
        THEN p_expected_index+1
      WHEN v_action='freeze' AND v_hit>0
        THEN 0 ELSE NULL END);
END $$;
ALTER FUNCTION core.requeue_graph_onboarding_with_authority(
  text,text,text,bigint,smallint,bigint,text,timestamptz,text,bigint[],boolean,int,bigint,boolean)
  OWNER TO veripsa_migrator;
REVOKE ALL ON FUNCTION core.requeue_graph_onboarding_with_authority(
  text,text,text,bigint,smallint,bigint,text,timestamptz,text,bigint[],boolean,int,bigint,boolean)
  FROM PUBLIC;
GRANT EXECUTE ON FUNCTION core.requeue_graph_onboarding_with_authority(
  text,text,text,bigint,smallint,bigint,text,timestamptz,text,bigint[],boolean,int,bigint,boolean)
  TO veripsa_app;

-- Fail-closed rolling bridge for the generation-less predecessor ABI. See the HEAD bridge above: phase results
-- derived through an old installation client can never be authorized with the current route's replacement tuple.
CREATE OR REPLACE FUNCTION core.requeue_graph_onboarding_with_authority(
    p_account text,p_repository_id text,p_branch text,p_request_epoch bigint,
    p_slot smallint,p_lease_epoch bigint,p_action text,p_plan bigint[],
    p_truncated boolean,p_expected_index int,p_pr_number bigint,p_watching_done boolean
) RETURNS jsonb
    LANGUAGE plpgsql SECURITY DEFINER SET search_path TO 'core','pg_catalog' AS $$
DECLARE v_prev text;
BEGIN
  v_prev:=current_setting('core.current_account',true);
  PERFORM set_config('core.current_account',p_account,true);
  PERFORM 1 FROM core.installation_account WHERE account_id=p_account FOR UPDATE;
  DELETE FROM core.graph_convergence_lease
   WHERE account_id=p_account AND slot=p_slot AND lease_epoch=p_lease_epoch
     AND request_epoch=p_request_epoch AND repository_id=p_repository_id
     AND branch=p_branch;
  PERFORM core._sync_graph_claim_router(p_account,300);
  PERFORM core._sync_account_convergence_due(p_account,5,300,true);
  PERFORM set_config('core.current_account',COALESCE(v_prev,''),true);
  RETURN jsonb_build_object(
    'requeued',false,'superseded',true,'lease_released',true,
    'legacy_installation_generation_missing',true);
END $$;
ALTER FUNCTION core.requeue_graph_onboarding_with_authority(
  text,text,text,bigint,smallint,bigint,text,bigint[],boolean,int,bigint,boolean)
  OWNER TO veripsa_migrator;
REVOKE ALL ON FUNCTION core.requeue_graph_onboarding_with_authority(
  text,text,text,bigint,smallint,bigint,text,bigint[],boolean,int,bigint,boolean)
  FROM PUBLIC;
GRANT EXECUTE ON FUNCTION core.requeue_graph_onboarding_with_authority(
  text,text,text,bigint,smallint,bigint,text,bigint[],boolean,int,bigint,boolean)
  TO veripsa_app;

CREATE OR REPLACE FUNCTION core.requeue_graph_refresh_page_with_authority(
    p_account text,p_repository_id text,p_branch text,p_request_epoch bigint,
    p_slot smallint,p_lease_epoch bigint,p_change_cursor text
) RETURNS jsonb
    LANGUAGE plpgsql SECURITY DEFINER SET search_path TO 'core','pg_catalog' AS $$
DECLARE
  v_prev text; v_change text; v_hit int:=0;
  v_claimed_until timestamptz; v_revoked timestamptz;
BEGIN
  v_change:=left(NULLIF(btrim(COALESCE(p_change_cursor,'')),''),120);
  IF v_change IS NULL THEN
    RAISE EXCEPTION 'graph page cursor is incomplete' USING ERRCODE='22023';
  END IF;
  v_prev:=current_setting('core.current_account',true);
  PERFORM set_config('core.current_account',p_account,true);
  SELECT revoked_at INTO v_revoked
    FROM core.installation_account
   WHERE account_id=p_account
   LIMIT 1 FOR UPDATE;
  IF NOT FOUND OR v_revoked IS NOT NULL THEN
    PERFORM set_config('core.current_account',COALESCE(v_prev,''),true);
    RETURN jsonb_build_object('requeued',false,'superseded',true,'lease_lost',true);
  END IF;
  SELECT claimed_until INTO v_claimed_until
    FROM core.graph_convergence_lease
   WHERE account_id=p_account AND slot=p_slot AND lease_epoch=p_lease_epoch
     AND request_epoch=p_request_epoch AND repository_id=p_repository_id
     AND branch=p_branch
   FOR UPDATE;
  IF NOT FOUND OR v_claimed_until<clock_timestamp() THEN
    PERFORM set_config('core.current_account',COALESCE(v_prev,''),true);
    RETURN jsonb_build_object('requeued',false,'superseded',true,'lease_lost',true);
  END IF;
  PERFORM 1
    FROM core.policy_refresh_outbox
   WHERE account_id=p_account AND request_kind='graph'
     AND repository_id=p_repository_id AND branch=p_branch
     AND policy_epoch=p_request_epoch AND done_at IS NULL
   FOR UPDATE;
  IF v_claimed_until<clock_timestamp() THEN
    PERFORM set_config('core.current_account',COALESCE(v_prev,''),true);
    RETURN jsonb_build_object('requeued',false,'superseded',true,'lease_lost',true);
  END IF;
  PERFORM core.mark_governed_write('policy_refresh_outbox');
  UPDATE core.policy_refresh_outbox
     SET change_cursor=v_change,last_error=NULL,not_before=NULL
   WHERE account_id=p_account AND request_kind='graph'
     AND repository_id=p_repository_id AND branch=p_branch
     AND policy_epoch=p_request_epoch AND done_at IS NULL;
  GET DIAGNOSTICS v_hit=ROW_COUNT;
  DELETE FROM core.graph_convergence_lease
   WHERE account_id=p_account AND slot=p_slot AND lease_epoch=p_lease_epoch
     AND request_epoch=p_request_epoch;
  PERFORM core._sync_graph_claim_router(p_account,300);
  PERFORM core._sync_account_convergence_due(p_account,5,300,true);
  PERFORM set_config('core.current_account',COALESCE(v_prev,''),true);
  RETURN jsonb_build_object(
    'requeued',v_hit>0,'superseded',v_hit=0,'lease_released',true,
    'tail_rearmed',CASE WHEN v_hit>0 THEN 1 ELSE 0 END);
END $$;
ALTER FUNCTION core.requeue_graph_refresh_page_with_authority(
  text,text,text,bigint,smallint,bigint,text) OWNER TO veripsa_migrator;
REVOKE ALL ON FUNCTION core.requeue_graph_refresh_page_with_authority(
  text,text,text,bigint,smallint,bigint,text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION core.requeue_graph_refresh_page_with_authority(
  text,text,text,bigint,smallint,bigint,text) TO veripsa_app;

CREATE OR REPLACE FUNCTION core.finish_policy_refresh_turn_with_authority(
    p_account text,p_kind text,p_repository_id text,p_branch text,p_request_epoch bigint,
    p_slot smallint,p_lease_epoch bigint
) RETURNS jsonb
    LANGUAGE plpgsql SECURITY DEFINER SET search_path TO 'core','pg_catalog' AS $$
DECLARE
  v_prev text; v_repo text; v_target text; v_current_epoch bigint;
  v_old_attempts int; v_old_reason text; v_hit int:=0; v_lease_hit int:=0;
  v_graph_ok boolean:=false; v_due jsonb; v_tail int:=0;
  v_claimed_until timestamptz; v_revoked timestamptz;
  v_surface_dirty boolean:=false; v_surface_rearmed boolean:=false;
  v_onboarding_pending boolean:=false; v_onboarding_head_pending boolean:=false;
BEGIN
  IF p_kind<>'graph' THEN
    RETURN jsonb_build_object('finished',false,'superseded',true,'lease_lost',true);
  END IF;
  v_prev:=current_setting('core.current_account',true);
  PERFORM set_config('core.current_account',p_account,true);
  SELECT revoked_at INTO v_revoked
    FROM core.installation_account
   WHERE account_id=p_account
   LIMIT 1 FOR UPDATE;
  IF NOT FOUND OR v_revoked IS NOT NULL THEN
    PERFORM set_config('core.current_account',COALESCE(v_prev,''),true);
    RETURN jsonb_build_object(
      'finished',false,'superseded',true,'lease_lost',true,'tail_rearmed',0,
      'graph_fulfilled',false);
  END IF;
  SELECT repo,target_sha,claimed_until INTO v_repo,v_target,v_claimed_until
    FROM core.graph_convergence_lease
   WHERE account_id=p_account AND slot=p_slot AND lease_epoch=p_lease_epoch
     AND request_epoch=p_request_epoch AND repository_id=p_repository_id
     AND branch=p_branch
   FOR UPDATE;
  IF NOT FOUND OR v_claimed_until<clock_timestamp() THEN
    PERFORM set_config('core.current_account',COALESCE(v_prev,''),true);
    RETURN jsonb_build_object(
      'finished',false,'superseded',true,'lease_lost',true,'tail_rearmed',0,
      'graph_fulfilled',false);
  END IF;
  SELECT policy_epoch,attempts,terminal_reason,surface_dirty,
         onboarding_pending,onboarding_head_pending
    INTO v_current_epoch,v_old_attempts,v_old_reason,v_surface_dirty,
         v_onboarding_pending,v_onboarding_head_pending
    FROM core.policy_refresh_outbox
   WHERE account_id=p_account AND request_kind='graph'
     AND repository_id=p_repository_id
   FOR UPDATE;
  IF v_claimed_until<clock_timestamp() THEN
    PERFORM set_config('core.current_account',COALESCE(v_prev,''),true);
    RETURN jsonb_build_object(
      'finished',false,'superseded',true,'lease_lost',true,'tail_rearmed',0,
      'graph_fulfilled',false);
  END IF;
  IF v_current_epoch IS NULL OR v_current_epoch<>p_request_epoch
     OR NOT EXISTS (
       SELECT 1 FROM core.policy_refresh_outbox
        WHERE account_id=p_account AND request_kind='graph'
          AND repository_id=p_repository_id AND branch=p_branch) THEN
    DELETE FROM core.graph_convergence_lease
     WHERE account_id=p_account AND slot=p_slot AND lease_epoch=p_lease_epoch
       AND request_epoch=p_request_epoch;
    PERFORM core._sync_graph_claim_router(p_account,300);
    PERFORM core._sync_account_convergence_due(p_account,5,300,true);
    PERFORM set_config('core.current_account',COALESCE(v_prev,''),true);
    RETURN jsonb_build_object(
      'finished',false,'superseded',true,'lease_released',true,'tail_rearmed',1,
      'graph_fulfilled',false);
  END IF;
  IF COALESCE(v_onboarding_head_pending,false) THEN
    -- Rolling bridge: an older /6 worker may have claimed this ordinary graph row immediately before the live
    -- install transaction latched onboarding. It must never consume that latch. Release its exact snapshot and
    -- publish the dedicated HEAD phase at the fair tail with a fresh per-phase retry budget.
    PERFORM core.mark_governed_write('policy_refresh_outbox');
    UPDATE core.policy_refresh_outbox
       SET branch='__veripsa_onboarding_head__',target_sha='0000000',
           onboarding_pending=true,onboarding_head_pending=true,onboarding_plan=NULL,
           onboarding_index=0,onboarding_truncated=false,onboarding_watching_done=false,
           done_at=NULL,enqueued_at=clock_timestamp(),attempts=0,last_error=NULL,
           not_before=NULL,terminal_reason=NULL,change_cursor='',surface_dirty=true
     WHERE account_id=p_account AND request_kind='graph'
       AND repository_id=p_repository_id AND branch=p_branch
       AND policy_epoch=p_request_epoch AND done_at IS NULL
       AND onboarding_pending AND onboarding_head_pending;
    GET DIAGNOSTICS v_hit=ROW_COUNT;
    IF v_hit>0 THEN
      UPDATE core.installation_account
         SET convergence_pending_count=GREATEST(0,convergence_pending_count
               -CASE WHEN v_old_reason IS NULL AND v_old_attempts<5 THEN 1 ELSE 0 END+1),
             convergence_retry_exhausted_count=GREATEST(0,convergence_retry_exhausted_count
               -CASE WHEN v_old_reason IS NULL AND v_old_attempts>=5 THEN 1 ELSE 0 END),
             convergence_quota_deferred_count=GREATEST(0,convergence_quota_deferred_count
               -CASE WHEN v_old_reason='quota_paused' THEN 1 ELSE 0 END)
       WHERE account_id=p_account;
    END IF;
    DELETE FROM core.graph_convergence_lease
     WHERE account_id=p_account AND slot=p_slot AND lease_epoch=p_lease_epoch
       AND request_epoch=p_request_epoch;
    GET DIAGNOSTICS v_lease_hit=ROW_COUNT;
    PERFORM core._sync_graph_claim_router(p_account,300);
    v_due:=core._sync_account_convergence_due(p_account,5,300,true);
    v_tail:=COALESCE((v_due->>'policy_pending')::int,0)
            +COALESCE((v_due->>'graph_pending')::int,0);
    PERFORM set_config('core.current_account',COALESCE(v_prev,''),true);
    RETURN jsonb_build_object(
      'finished',v_hit>0,'superseded',v_hit=0,'lease_released',v_lease_hit>0,
      'tail_rearmed',v_tail,'graph_fulfilled',false,
      'surface_rearmed',false,'onboarding_rearmed',v_hit>0);
  END IF;
  v_graph_ok:=core._graph_refresh_fulfilled(
    p_account,p_repository_id,v_repo,p_branch,v_target);
  IF NOT v_graph_ok THEN
    PERFORM set_config('core.current_account',COALESCE(v_prev,''),true);
    RETURN jsonb_build_object(
      'finished',false,'superseded',false,'lease_lost',false,'tail_rearmed',0,
      'graph_fulfilled',false);
  END IF;
  -- Fulfillment proof may wait on graph state. Authority is wall-clock bounded even while this transaction holds
  -- the route lock, so a proof obtained after expiry cannot be used to terminalize the request.
  IF v_claimed_until<clock_timestamp() THEN
    PERFORM set_config('core.current_account',COALESCE(v_prev,''),true);
    RETURN jsonb_build_object(
      'finished',false,'superseded',true,'lease_lost',true,'tail_rearmed',0,
      'graph_fulfilled',false);
  END IF;
  PERFORM core.mark_governed_write('policy_refresh_outbox');
  IF COALESCE(v_surface_dirty,false) OR COALESCE(v_onboarding_pending,false) THEN
    -- A PR event landed after this pass began (or while it was paginating).
    -- The current tail completed successfully, so do not discard that work or
    -- supersede graph extraction. Re-arm one attempts-neutral full surface
    -- pass from the beginning and consume the coalesced latch atomically.
    UPDATE core.policy_refresh_outbox
       SET done_at=NULL,enqueued_at=clock_timestamp(),attempts=0,last_error=NULL,
           not_before=NULL,terminal_reason=NULL,change_cursor='',surface_dirty=false
     WHERE account_id=p_account AND request_kind='graph'
       AND repository_id=p_repository_id AND branch=p_branch
       AND policy_epoch=p_request_epoch AND done_at IS NULL;
    GET DIAGNOSTICS v_hit=ROW_COUNT;
    v_surface_rearmed:=v_hit>0;
    IF v_hit>0 THEN
      UPDATE core.installation_account
         SET convergence_pending_count=GREATEST(0,convergence_pending_count
               -CASE WHEN v_old_reason IS NULL AND v_old_attempts<5 THEN 1 ELSE 0 END+1),
             convergence_retry_exhausted_count=GREATEST(0,convergence_retry_exhausted_count
               -CASE WHEN v_old_reason IS NULL AND v_old_attempts>=5 THEN 1 ELSE 0 END),
             convergence_quota_deferred_count=GREATEST(0,convergence_quota_deferred_count
               -CASE WHEN v_old_reason='quota_paused' THEN 1 ELSE 0 END)
       WHERE account_id=p_account;
    END IF;
  ELSE
    UPDATE core.policy_refresh_outbox
       SET done_at=now(),last_error=NULL,not_before=NULL,terminal_reason=NULL,
           change_cursor='',surface_dirty=false
     WHERE account_id=p_account AND request_kind='graph'
       AND repository_id=p_repository_id AND branch=p_branch
       AND policy_epoch=p_request_epoch AND done_at IS NULL;
    GET DIAGNOSTICS v_hit=ROW_COUNT;
    IF v_hit>0 THEN
      UPDATE core.installation_account
         SET convergence_pending_count=GREATEST(0,convergence_pending_count
               -CASE WHEN v_old_reason IS NULL AND v_old_attempts<5 THEN 1 ELSE 0 END),
             convergence_retry_exhausted_count=GREATEST(0,convergence_retry_exhausted_count
               -CASE WHEN v_old_reason IS NULL AND v_old_attempts>=5 THEN 1 ELSE 0 END),
             convergence_quota_deferred_count=GREATEST(0,convergence_quota_deferred_count
               -CASE WHEN v_old_reason='quota_paused' THEN 1 ELSE 0 END)
       WHERE account_id=p_account;
    END IF;
  END IF;
  DELETE FROM core.graph_convergence_lease
   WHERE account_id=p_account AND slot=p_slot AND lease_epoch=p_lease_epoch
     AND request_epoch=p_request_epoch;
  GET DIAGNOSTICS v_lease_hit=ROW_COUNT;
  PERFORM core._sync_graph_claim_router(p_account,300);
  v_due:=core._sync_account_convergence_due(p_account,5,300,true);
  v_tail:=COALESCE((v_due->>'policy_pending')::int,0)
          +COALESCE((v_due->>'graph_pending')::int,0);
  PERFORM set_config('core.current_account',COALESCE(v_prev,''),true);
  RETURN jsonb_build_object(
    'finished',v_hit>0,'superseded',false,'lease_released',v_lease_hit>0,
    'tail_rearmed',v_tail,'graph_fulfilled',v_graph_ok,
    'surface_rearmed',v_surface_rearmed,
    'onboarding_rearmed',COALESCE(v_onboarding_pending,false) AND v_surface_rearmed);
END $$;
ALTER FUNCTION core.finish_policy_refresh_turn_with_authority(
  text,text,text,text,bigint,smallint,bigint) OWNER TO veripsa_migrator;
REVOKE ALL ON FUNCTION core.finish_policy_refresh_turn_with_authority(
  text,text,text,text,bigint,smallint,bigint) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION core.finish_policy_refresh_turn_with_authority(
  text,text,text,text,bigint,smallint,bigint) TO veripsa_app;

CREATE OR REPLACE FUNCTION core.fail_policy_refresh_turn_with_authority(
    p_account text,p_kind text,p_repository_id text,p_branch text,p_request_epoch bigint,
    p_slot smallint,p_lease_epoch bigint,p_error text,p_retry_seconds int
) RETURNS int
    LANGUAGE plpgsql SECURITY DEFINER SET search_path TO 'core','pg_catalog' AS $$
DECLARE
  v_prev text; v_n int; v_old_attempts int; v_old_reason text;
  v_retry int:=LEAST(GREATEST(COALESCE(p_retry_seconds,20),15),30);
  v_claimed_until timestamptz; v_revoked timestamptz;
  v_row_found boolean;
BEGIN
  IF p_kind<>'graph' THEN RETURN -1; END IF;
  v_prev:=current_setting('core.current_account',true);
  PERFORM set_config('core.current_account',p_account,true);
  SELECT revoked_at INTO v_revoked
    FROM core.installation_account
   WHERE account_id=p_account
   LIMIT 1 FOR UPDATE;
  IF NOT FOUND OR v_revoked IS NOT NULL THEN
    PERFORM set_config('core.current_account',COALESCE(v_prev,''),true);
    RETURN -1;
  END IF;
  SELECT claimed_until INTO v_claimed_until
    FROM core.graph_convergence_lease
   WHERE account_id=p_account AND slot=p_slot AND lease_epoch=p_lease_epoch
     AND request_epoch=p_request_epoch AND repository_id=p_repository_id
     AND branch=p_branch
   FOR UPDATE;
  IF NOT FOUND OR v_claimed_until<clock_timestamp() THEN
    PERFORM set_config('core.current_account',COALESCE(v_prev,''),true);
    RETURN -1;
  END IF;
  SELECT attempts,terminal_reason INTO v_old_attempts,v_old_reason
    FROM core.policy_refresh_outbox
   WHERE account_id=p_account AND request_kind='graph'
     AND repository_id=p_repository_id AND branch=p_branch
     AND policy_epoch=p_request_epoch AND done_at IS NULL
   FOR UPDATE;
  v_row_found:=FOUND;
  IF v_claimed_until<clock_timestamp() THEN
    PERFORM set_config('core.current_account',COALESCE(v_prev,''),true);
    RETURN -1;
  END IF;
  IF NOT v_row_found THEN
    DELETE FROM core.graph_convergence_lease
     WHERE account_id=p_account AND slot=p_slot AND lease_epoch=p_lease_epoch
       AND request_epoch=p_request_epoch;
    PERFORM core._sync_graph_claim_router(p_account,300);
    PERFORM core._sync_account_convergence_due(p_account,5,300,true);
    PERFORM set_config('core.current_account',COALESCE(v_prev,''),true);
    RETURN -1;
  END IF;
  PERFORM core.mark_governed_write('policy_refresh_outbox');
  UPDATE core.policy_refresh_outbox
     SET attempts=attempts+1,last_error=left(COALESCE(p_error,''),200),
         not_before=now()+make_interval(secs=>
           CASE
             WHEN attempts+1<5 THEN v_retry
             WHEN attempts+1=5 THEN 300
             WHEN attempts+1=6 THEN 600
             WHEN attempts+1=7 THEN 1200
             WHEN attempts+1=8 THEN 2400
             ELSE 3600
           END),
         terminal_reason=NULL
   WHERE account_id=p_account AND request_kind='graph'
     AND repository_id=p_repository_id AND branch=p_branch
     AND policy_epoch=p_request_epoch AND done_at IS NULL
   RETURNING attempts INTO v_n;
  DELETE FROM core.graph_convergence_lease
   WHERE account_id=p_account AND slot=p_slot AND lease_epoch=p_lease_epoch
     AND request_epoch=p_request_epoch;
  IF v_n IS NOT NULL THEN
    UPDATE core.installation_account
       SET convergence_pending_count=GREATEST(0,convergence_pending_count
             -CASE WHEN v_old_reason IS NULL AND v_old_attempts<5 THEN 1 ELSE 0 END
             +CASE WHEN v_n<5 THEN 1 ELSE 0 END),
           convergence_retry_exhausted_count=GREATEST(0,convergence_retry_exhausted_count
             -CASE WHEN v_old_reason IS NULL AND v_old_attempts>=5 THEN 1 ELSE 0 END
             +CASE WHEN v_n>=5 THEN 1 ELSE 0 END),
           convergence_quota_deferred_count=GREATEST(0,convergence_quota_deferred_count
             -CASE WHEN v_old_reason='quota_paused' THEN 1 ELSE 0 END)
     WHERE account_id=p_account;
  END IF;
  PERFORM core._sync_graph_claim_router(p_account,300);
  PERFORM core._sync_account_convergence_due(p_account,5,v_retry,true);
  PERFORM set_config('core.current_account',COALESCE(v_prev,''),true);
  RETURN COALESCE(v_n,-1);
END $$;
ALTER FUNCTION core.fail_policy_refresh_turn_with_authority(
  text,text,text,text,bigint,smallint,bigint,text,int) OWNER TO veripsa_migrator;
REVOKE ALL ON FUNCTION core.fail_policy_refresh_turn_with_authority(
  text,text,text,text,bigint,smallint,bigint,text,int) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION core.fail_policy_refresh_turn_with_authority(
  text,text,text,text,bigint,smallint,bigint,text,int) TO veripsa_app;

CREATE OR REPLACE FUNCTION core.defer_graph_refresh_turn_with_authority(
    p_account text,p_repository_id text,p_branch text,p_request_epoch bigint,
    p_slot smallint,p_lease_epoch bigint,p_reason text,p_delay_seconds int
) RETURNS jsonb
    LANGUAGE plpgsql SECURITY DEFINER SET search_path TO 'core','pg_catalog' AS $$
DECLARE
  v_prev text; v_hit int:=0; v_old_attempts int; v_old_reason text;
  v_delay int:=LEAST(GREATEST(COALESCE(p_delay_seconds,900),60),86400);
  v_next timestamptz:=now()+make_interval(secs=>v_delay);
  v_claimed_until timestamptz; v_revoked timestamptz;
  v_row_found boolean;
BEGIN
  IF p_reason<>'quota_paused' THEN
    RAISE EXCEPTION 'unsupported graph defer reason' USING ERRCODE='22023';
  END IF;
  v_prev:=current_setting('core.current_account',true);
  PERFORM set_config('core.current_account',p_account,true);
  SELECT revoked_at INTO v_revoked
    FROM core.installation_account
   WHERE account_id=p_account
   LIMIT 1 FOR UPDATE;
  IF NOT FOUND OR v_revoked IS NOT NULL THEN
    PERFORM set_config('core.current_account',COALESCE(v_prev,''),true);
    RETURN jsonb_build_object('deferred',false,'superseded',true,'lease_lost',true);
  END IF;
  SELECT claimed_until INTO v_claimed_until
    FROM core.graph_convergence_lease
   WHERE account_id=p_account AND slot=p_slot AND lease_epoch=p_lease_epoch
     AND request_epoch=p_request_epoch AND repository_id=p_repository_id
     AND branch=p_branch
   FOR UPDATE;
  IF NOT FOUND OR v_claimed_until<clock_timestamp() THEN
    PERFORM set_config('core.current_account',COALESCE(v_prev,''),true);
    RETURN jsonb_build_object('deferred',false,'superseded',true,'lease_lost',true);
  END IF;
  SELECT attempts,terminal_reason INTO v_old_attempts,v_old_reason
    FROM core.policy_refresh_outbox
   WHERE account_id=p_account AND request_kind='graph'
     AND repository_id=p_repository_id AND branch=p_branch
     AND policy_epoch=p_request_epoch AND done_at IS NULL
   FOR UPDATE;
  v_row_found:=FOUND;
  IF v_claimed_until<clock_timestamp() THEN
    PERFORM set_config('core.current_account',COALESCE(v_prev,''),true);
    RETURN jsonb_build_object('deferred',false,'superseded',true,'lease_lost',true);
  END IF;
  IF v_row_found THEN
    PERFORM core.mark_governed_write('policy_refresh_outbox');
    UPDATE core.policy_refresh_outbox
       SET done_at=NULL,last_error=NULL,terminal_reason='quota_paused',
           not_before=v_next,change_cursor=''
     WHERE account_id=p_account AND request_kind='graph'
       AND repository_id=p_repository_id AND branch=p_branch
       AND policy_epoch=p_request_epoch AND done_at IS NULL;
    GET DIAGNOSTICS v_hit=ROW_COUNT;
  END IF;
  DELETE FROM core.graph_convergence_lease
   WHERE account_id=p_account AND slot=p_slot AND lease_epoch=p_lease_epoch
     AND request_epoch=p_request_epoch;
  IF v_hit>0 THEN
    UPDATE core.installation_account
       SET convergence_pending_count=GREATEST(0,convergence_pending_count
             -CASE WHEN v_old_reason IS NULL AND v_old_attempts<5 THEN 1 ELSE 0 END),
           convergence_retry_exhausted_count=GREATEST(0,convergence_retry_exhausted_count
             -CASE WHEN v_old_reason IS NULL AND v_old_attempts>=5 THEN 1 ELSE 0 END),
           convergence_quota_deferred_count=GREATEST(0,convergence_quota_deferred_count
             -CASE WHEN v_old_reason='quota_paused' THEN 1 ELSE 0 END+1)
     WHERE account_id=p_account;
  END IF;
  PERFORM core._sync_graph_claim_router(p_account,300);
  PERFORM core._sync_account_convergence_due(p_account,5,v_delay,true);
  PERFORM set_config('core.current_account',COALESCE(v_prev,''),true);
  RETURN jsonb_build_object(
    'deferred',v_hit>0,'superseded',v_hit=0,'lease_released',true,
    'reason','quota_paused','not_before',v_next);
END $$;
ALTER FUNCTION core.defer_graph_refresh_turn_with_authority(
  text,text,text,bigint,smallint,bigint,text,int) OWNER TO veripsa_migrator;
REVOKE ALL ON FUNCTION core.defer_graph_refresh_turn_with_authority(
  text,text,text,bigint,smallint,bigint,text,int) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION core.defer_graph_refresh_turn_with_authority(
  text,text,text,bigint,smallint,bigint,text,int) TO veripsa_app;

-- Transitional graph defer has no slot token and is therefore incapable of touching lease-protocol-v2 work.
CREATE OR REPLACE FUNCTION core.defer_graph_refresh_turn_with_authority(
    p_account text,p_repository_id text,p_branch text,p_epoch bigint,
    p_reason text,p_delay_seconds int
) RETURNS jsonb
    LANGUAGE sql SECURITY DEFINER SET search_path TO 'core','pg_catalog' AS $$
  SELECT jsonb_build_object(
    'deferred',false,'superseded',true,'lease_lost',true)
$$;
ALTER FUNCTION core.defer_graph_refresh_turn_with_authority(text,text,text,bigint,text,int)
  OWNER TO veripsa_migrator;
REVOKE ALL ON FUNCTION core.defer_graph_refresh_turn_with_authority(
  text,text,text,bigint,text,int) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION core.defer_graph_refresh_turn_with_authority(
  text,text,text,bigint,text,int) TO veripsa_app;

CREATE OR REPLACE FUNCTION core.release_superseded_convergence_lease_with_authority(
    p_account text,p_request_epoch bigint,p_slot smallint,p_lease_epoch bigint
) RETURNS boolean
    LANGUAGE plpgsql SECURITY DEFINER SET search_path TO 'core','pg_catalog' AS $$
DECLARE v_prev text; v_hit int:=0; v_revoked timestamptz;
BEGIN
  v_prev:=current_setting('core.current_account',true);
  PERFORM set_config('core.current_account',p_account,true);
  SELECT revoked_at INTO v_revoked
    FROM core.installation_account
   WHERE account_id=p_account
   LIMIT 1 FOR UPDATE;
  IF NOT FOUND THEN
    PERFORM set_config('core.current_account',COALESCE(v_prev,''),true);
    RETURN false;
  END IF;
  DELETE FROM core.graph_convergence_lease
   WHERE account_id=p_account AND slot=p_slot AND lease_epoch=p_lease_epoch
     AND request_epoch=p_request_epoch;
  GET DIAGNOSTICS v_hit=ROW_COUNT;
  IF v_revoked IS NULL THEN
    PERFORM core._sync_graph_claim_router(p_account,300);
    PERFORM core._sync_account_convergence_due(p_account,5,300,true);
  END IF;
  PERFORM set_config('core.current_account',COALESCE(v_prev,''),true);
  RETURN v_hit>0;
END $$;
ALTER FUNCTION core.release_superseded_convergence_lease_with_authority(
  text,bigint,smallint,bigint) OWNER TO veripsa_migrator;
REVOKE ALL ON FUNCTION core.release_superseded_convergence_lease_with_authority(
  text,bigint,smallint,bigint) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION core.release_superseded_convergence_lease_with_authority(
  text,bigint,smallint,bigint) TO veripsa_app;

-- Aggregate-only watchdog surface: no tenant/repository identity leaves the scheduler. Every input is an ordered
-- partial-index probe capped at 1001 rows (1000 reported + one truncation sentinel). A watchdog tick is therefore
-- bounded even at millions of accounts; counts are documented lower bounds whenever the corresponding truncated
-- flag is true. Scheduler due time is intentionally NOT alert age: a claimed turn, reclaim delay, account-tail
-- move, or latest-target replacement may move due_at. The separate indexed stall pointer retains the original
-- oldest unfinished fact until the account fully drains, so the global first row gives an exact latency age.
CREATE OR REPLACE FUNCTION core.account_convergence_depth_with_authority()
RETURNS jsonb
    LANGUAGE sql STABLE SECURITY DEFINER SET search_path TO 'core','pg_catalog' AS $$
  WITH scheduled AS MATERIALIZED (
    SELECT LEAST(policy_refresh_due_at,graph_refresh_due_at) AS due_at,
           convergence_pending_count AS pending
      FROM core.installation_account
     WHERE revoked_at IS NULL
       AND (policy_refresh_due_at IS NOT NULL OR graph_refresh_due_at IS NOT NULL)
     ORDER BY LEAST(policy_refresh_due_at,graph_refresh_due_at),account_id
     LIMIT 1001
  ), scheduled_sample AS MATERIALIZED (
    SELECT * FROM scheduled LIMIT 1000
  ), stalled AS MATERIALIZED (
    SELECT convergence_stall_started_at AS started_at
      FROM core.installation_account
     WHERE revoked_at IS NULL
       AND convergence_stall_started_at IS NOT NULL
     ORDER BY convergence_stall_started_at,account_id
     LIMIT 1001
  ), stalled_sample AS MATERIALIZED (
    SELECT * FROM stalled LIMIT 1000
  ), claimed AS MATERIALIZED (
    SELECT convergence_claimed_until
      FROM core.installation_account
     WHERE revoked_at IS NULL AND convergence_claimed_until>=now()
     ORDER BY convergence_claimed_until,account_id
     LIMIT 1001
  ), graph_claimed AS MATERIALIZED (
    SELECT convergence_graph_claim_count AS claimed
      FROM core.installation_account
     WHERE revoked_at IS NULL AND convergence_graph_claim_count>0
     ORDER BY account_id
     LIMIT 1001
  ), graph_claimed_sample AS MATERIALIZED (
    SELECT * FROM graph_claimed LIMIT 1000
  ), exceptions AS MATERIALIZED (
    SELECT convergence_retry_exhausted_count AS exhausted,
           convergence_quota_deferred_count AS quota
      FROM core.installation_account
     WHERE revoked_at IS NULL
       AND (convergence_retry_exhausted_count>0 OR convergence_quota_deferred_count>0)
     ORDER BY account_id
     LIMIT 1001
  ), exception_sample AS MATERIALIZED (
    SELECT * FROM exceptions LIMIT 1000
  )
  SELECT jsonb_build_object(
    'pending',COALESCE((SELECT sum(pending)::bigint FROM scheduled_sample),0),
    'claimed',LEAST((SELECT count(*)::bigint FROM claimed),1000)
              +COALESCE((SELECT sum(claimed)::bigint FROM graph_claimed_sample),0),
    'retry_exhausted',COALESCE((SELECT sum(exhausted)::bigint FROM exception_sample),0),
    'quota_deferred',COALESCE((SELECT sum(quota)::bigint FROM exception_sample),0),
    'due_accounts',COALESCE((SELECT count(*)::bigint FROM scheduled_sample
      WHERE due_at<=now()),0),
    'stalled_accounts',COALESCE((SELECT count(*)::bigint FROM stalled_sample),0),
    'oldest_age_seconds',COALESCE((
      SELECT GREATEST(0,extract(epoch FROM now()-min(started_at)))::bigint
        FROM stalled_sample),0),
    'sample_cap',1000,
    'scheduled_truncated',(SELECT count(*)>1000 FROM scheduled),
    'stalled_truncated',(SELECT count(*)>1000 FROM stalled),
    'claimed_truncated',(
      (SELECT count(*)>1000 FROM claimed)
      OR (SELECT count(*)>1000 FROM graph_claimed)),
    'exceptions_truncated',(SELECT count(*)>1000 FROM exceptions))
$$;
ALTER FUNCTION core.account_convergence_depth_with_authority() OWNER TO veripsa_migrator;
REVOKE ALL ON FUNCTION core.account_convergence_depth_with_authority() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION core.account_convergence_depth_with_authority()
  TO veripsa_app,example_platform_reader;

-- Rolling legacy terminal ABIs are policy-sentinel-only. A graph row is unreachable even if an old worker holds
-- only account+epoch, so schema-first rollout cannot erase graph work.
CREATE OR REPLACE FUNCTION core._materialize_legacy_policy_refresh_lease(
    p_account text,p_epoch bigint
) RETURNS boolean
    LANGUAGE plpgsql SECURITY DEFINER SET search_path TO 'core','pg_catalog' AS $$
DECLARE
  v_prev text; v_route_epoch bigint; v_claimed timestamptz; v_worker text; v_ok boolean;
  v_route_until timestamptz; v_revoked timestamptz; v_deadline timestamptz;
BEGIN
  v_prev:=current_setting('core.current_account',true);
  PERFORM set_config('core.current_account',p_account,true);
  SELECT convergence_claim_epoch,convergence_claimed_until,revoked_at
    INTO v_route_epoch,v_route_until,v_revoked
    FROM core.installation_account
   WHERE account_id=p_account
   LIMIT 1 FOR UPDATE;
  IF NOT FOUND OR v_revoked IS NOT NULL THEN
    PERFORM set_config('core.current_account',COALESCE(v_prev,''),true);
    RETURN false;
  END IF;
  IF v_route_epoch=p_epoch THEN
    PERFORM set_config('core.current_account',COALESCE(v_prev,''),true);
    RETURN v_route_until IS NOT NULL AND v_route_until>=clock_timestamp();
  END IF;
  IF v_route_epoch IS NOT NULL THEN
    PERFORM set_config('core.current_account',COALESCE(v_prev,''),true);
    RETURN false;
  END IF;
  SELECT claimed_at,claimed_by INTO v_claimed,v_worker
    FROM core.policy_refresh_outbox
   WHERE account_id=p_account AND request_kind='policy' AND repository_id='' AND branch=''
     AND policy_epoch=p_epoch AND done_at IS NULL AND claimed_at IS NOT NULL
   FOR UPDATE;
  -- Pre-router workers had only claimed_at. Give that compatibility row the same 300s hard authority bound as
  -- the current lease protocol; never resurrect the historical 15-minute stall window.
  v_deadline:=v_claimed+interval '5 minutes';
  IF v_claimed IS NULL OR v_deadline<clock_timestamp() THEN
    PERFORM set_config('core.current_account',COALESCE(v_prev,''),true);
    RETURN false;
  END IF;
  UPDATE core.installation_account
     SET convergence_claim_epoch=p_epoch,
         convergence_claimed_by=v_worker,
         convergence_claimed_until=v_deadline
   WHERE account_id=p_account AND convergence_claim_epoch IS NULL;
  v_ok:=FOUND;
  PERFORM set_config('core.current_account',COALESCE(v_prev,''),true);
  RETURN v_ok;
END $$;
ALTER FUNCTION core._materialize_legacy_policy_refresh_lease(text,bigint)
  OWNER TO veripsa_migrator;
REVOKE ALL ON FUNCTION core._materialize_legacy_policy_refresh_lease(text,bigint) FROM PUBLIC;

CREATE OR REPLACE FUNCTION core.finish_policy_refresh_with_authority(
    p_account text,p_epoch bigint
) RETURNS boolean
    LANGUAGE plpgsql SECURITY DEFINER SET search_path TO 'core','pg_catalog' AS $$
DECLARE v_result jsonb;
BEGIN
  IF NOT core._materialize_legacy_policy_refresh_lease(p_account,p_epoch) THEN
    RETURN false;
  END IF;
  v_result:=core.finish_policy_refresh_turn_with_authority(p_account,'policy','','',p_epoch);
  RETURN COALESCE((v_result->>'finished')::boolean,false);
END $$;
ALTER FUNCTION core.finish_policy_refresh_with_authority(text,bigint) OWNER TO veripsa_migrator;
REVOKE ALL ON FUNCTION core.finish_policy_refresh_with_authority(text,bigint) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION core.finish_policy_refresh_with_authority(text,bigint) TO veripsa_app;

CREATE OR REPLACE FUNCTION core.fail_policy_refresh_with_authority(
    p_account text,p_epoch bigint,p_error text
) RETURNS int
    LANGUAGE plpgsql SECURITY DEFINER SET search_path TO 'core','pg_catalog' AS $$
BEGIN
  IF NOT core._materialize_legacy_policy_refresh_lease(p_account,p_epoch) THEN
    RETURN -1;
  END IF;
  RETURN core.fail_policy_refresh_turn_with_authority(
    p_account,'policy','','',p_epoch,p_error);
END
$$;
ALTER FUNCTION core.fail_policy_refresh_with_authority(text,bigint,text) OWNER TO veripsa_migrator;
REVOKE ALL ON FUNCTION core.fail_policy_refresh_with_authority(text,bigint,text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION core.fail_policy_refresh_with_authority(text,bigint,text) TO veripsa_app;

-- Rolling repair: an old web worker can mint a 900/1800-second lease immediately before its claim ABI is
-- replaced above. Visit only accounts named by the two active-lease indexes, lock each route, and cap the
-- surviving snapshots. Policy claims have claimed_at, so an already-old claim expires at claimed_at+300 rather
-- than receiving 300 extra seconds at deploy time. After this point every callable claim ABI is itself capped.
DO $$
DECLARE
  v_prev text; v_account text; v_cutoff timestamptz:=clock_timestamp()+interval '5 minutes';
  v_retry_cutoff timestamptz:=clock_timestamp()+interval '30 seconds';
  v_policy_deadline timestamptz;
BEGIN
  v_prev:=current_setting('core.current_account',true);
  FOR v_account IN
      SELECT account_id
        FROM (
          SELECT account_id
            FROM core.installation_account
           WHERE convergence_claimed_until>v_cutoff
          UNION
          SELECT account_id
            FROM core.installation_account
           WHERE convergence_graph_claim_count>0
          UNION
          SELECT account_id
            FROM core.installation_account
           WHERE policy_refresh_due_at>v_retry_cutoff
        ) active
       ORDER BY account_id
  LOOP
    PERFORM 1
      FROM core.installation_account
     WHERE account_id=v_account
     FOR UPDATE;
    IF NOT FOUND THEN CONTINUE; END IF;
    PERFORM set_config('core.current_account',v_account,true);
    SELECT claimed_at+interval '5 minutes'
      INTO v_policy_deadline
      FROM core.policy_refresh_outbox q
      JOIN core.installation_account r ON r.account_id=q.account_id
     WHERE q.account_id=v_account AND q.request_kind='policy' AND q.repository_id=''
       AND q.done_at IS NULL AND q.claimed_at IS NOT NULL
       AND q.policy_epoch=r.convergence_claim_epoch
     FOR UPDATE OF q;
    UPDATE core.installation_account
       SET convergence_claimed_until=LEAST(
             convergence_claimed_until,
             COALESCE(v_policy_deadline,v_cutoff),
             v_cutoff)
     WHERE account_id=v_account AND convergence_claimed_until IS NOT NULL;
    PERFORM core.mark_governed_write('graph_convergence_lease');
    UPDATE core.graph_convergence_lease
       SET claimed_until=LEAST(claimed_until,v_cutoff)
     WHERE account_id=v_account AND claimed_until>v_cutoff;
    PERFORM core.mark_governed_write('policy_refresh_outbox');
    UPDATE core.policy_refresh_outbox
       SET not_before=v_retry_cutoff
     WHERE account_id=v_account AND request_kind='policy' AND repository_id=''
       AND done_at IS NULL AND claimed_at IS NULL AND attempts<5
       AND not_before>v_retry_cutoff;
    PERFORM core._sync_graph_claim_router(v_account,300);
    PERFORM core._sync_account_convergence_due(v_account,5,300,false);
  END LOOP;
  PERFORM set_config('core.current_account',COALESCE(v_prev,''),true);
END $$;

-- CONTRACT CUTOVER: only now, after every retained writer/claim/terminal/page ABI maintains the router, may a
-- newly inserted route skip the generation-0 bridge. The default flip takes the installation table DDL lock, so
-- no old-image provision can cross the cutoff while still receiving generation 0/1 ambiguously.
SELECT core._ensure_column_default_online(
  'installation_account','convergence_schema_version','2');

-- One-time bridge for requests queued by the old image before routing due-times existed. A generation-0 account
-- is locked before its outbox aggregate and then advanced to 1. Every new mutator takes this same route lock
-- first, so it is wholly before or after the snapshot; no post-snapshot delta can be hidden from the router.
-- The partial bridge index is empty after rollout, making later schema re-apply O(0).
DO $$
DECLARE
  v_prev text; v_account text; v_policy timestamptz; v_graph timestamptz;
  v_legacy_graph timestamptz;
  v_stall timestamptz; v_pending int; v_exhausted int; v_quota int; v_epoch bigint;
BEGIN
  v_prev:=current_setting('core.current_account',true);
  FOR v_account IN
      SELECT account_id FROM core.installation_account
       WHERE convergence_schema_version<2
       ORDER BY account_id
  LOOP
    PERFORM 1
      FROM core.installation_account
     WHERE account_id=v_account AND convergence_schema_version<2
     FOR UPDATE;
    IF NOT FOUND THEN
      CONTINUE;
    END IF;
    PERFORM set_config('core.current_account',v_account,true);
    SELECT
        min(CASE
              WHEN claimed_at IS NULL THEN COALESCE(not_before,enqueued_at)
              ELSE claimed_at+interval '5 minutes'
            END) FILTER (
          WHERE request_kind='policy' AND done_at IS NULL),
        min(COALESCE(not_before,enqueued_at)) FILTER (
          WHERE request_kind='graph' AND done_at IS NULL
            AND claimed_at IS NULL),
        min(COALESCE(not_before,enqueued_at)) FILTER (
          WHERE request_kind='graph' AND done_at IS NULL
            AND claimed_at IS NULL AND NOT onboarding_pending),
        count(*) FILTER (
          WHERE done_at IS NULL AND attempts<5 AND terminal_reason IS NULL)::int,
        count(*) FILTER (
          WHERE done_at IS NULL AND attempts>=5 AND terminal_reason IS NULL)::int,
        count(*) FILTER (
          WHERE done_at IS NULL AND terminal_reason='quota_paused')::int,
        COALESCE(max(policy_epoch),0),
        min(enqueued_at) FILTER (
          WHERE done_at IS NULL AND terminal_reason IS NULL)
      INTO v_policy,v_graph,v_legacy_graph,v_pending,v_exhausted,v_quota,v_epoch,v_stall
      FROM core.policy_refresh_outbox
     WHERE account_id=v_account;
    UPDATE core.installation_account
       SET policy_refresh_due_at=v_policy,
           graph_refresh_due_at=v_graph,
           legacy_graph_refresh_due_at=v_legacy_graph,
           convergence_pending_count=COALESCE(v_pending,0),
           convergence_retry_exhausted_count=COALESCE(v_exhausted,0),
           convergence_quota_deferred_count=COALESCE(v_quota,0),
           convergence_stall_started_at=v_stall,
           convergence_next_epoch=GREATEST(convergence_next_epoch,COALESCE(v_epoch,0)),
           convergence_schema_version=2
     WHERE account_id=v_account AND convergence_schema_version<2;
  END LOOP;
  PERFORM set_config('core.current_account',COALESCE(v_prev,''),true);
END $$;

-- CONTRACT last: only after every legacy callable ABI above is sentinel-safe may multiple graph identities coexist.
DO $$
DECLARE v_pk text; v_cols text[];
BEGIN
  SELECT c.conname,array_agg(a.attname ORDER BY u.ord)
    INTO v_pk,v_cols
    FROM pg_constraint c
    CROSS JOIN LATERAL unnest(c.conkey) WITH ORDINALITY AS u(attnum,ord)
    JOIN pg_attribute a ON a.attrelid=c.conrelid AND a.attnum=u.attnum
   WHERE c.conrelid='core.policy_refresh_outbox'::regclass AND c.contype='p'
   GROUP BY c.conname;
  IF v_pk IS NOT NULL AND (
       v_cols=ARRAY['account_id']::text[]
       OR v_cols=ARRAY['account_id','request_kind','repository_id','branch']::text[]
       OR (
         v_cols=ARRAY['account_id','request_kind','repository_id']::text[]
         AND v_pk<>'policy_refresh_outbox_identity_uq')
     ) THEN
    EXECUTE format('ALTER TABLE core.policy_refresh_outbox DROP CONSTRAINT %I',v_pk);
    v_pk:=NULL;
  END IF;
  IF v_pk IS NULL THEN
    IF to_regclass('core.policy_refresh_outbox_identity_uq') IS NULL THEN
      RAISE EXCEPTION 'stable-id identity index missing before policy_refresh_outbox PK contract'
        USING ERRCODE='55000';
    END IF;
    ALTER TABLE core.policy_refresh_outbox
      ADD CONSTRAINT policy_refresh_outbox_identity_uq
      PRIMARY KEY USING INDEX policy_refresh_outbox_identity_uq;
  ELSIF v_cols<>ARRAY['account_id','request_kind','repository_id']::text[]
        OR v_pk<>'policy_refresh_outbox_identity_uq' THEN
    RAISE EXCEPTION 'unexpected policy_refresh_outbox primary key shape: %',v_cols;
  END IF;
END $$;

-- account_inflight_refresh_coordinates_with_authority: the drainer's enumerator — the distinct (repo, branch)
-- coordinates for the CURRENTLY-PINNED account that have IN-FLIGHT work (an active/waiting claim), so the
-- drainer refreshes ONLY repos with an open PR to re-derive (a repo with nothing in flight costs zero posts).
-- Resolves the session account the SAME way main_impact_surface does (resolve_session_identity → the App's
-- pinned installation route), so it reads inside that tenant's RLS wall — no cross-account leak. Bounded by
-- p_cap. Content-free: repo/branch coordinate identifiers only (the same shape owner_graph_freshness_surface
-- already returns) — never a path, symbol, or body. App-delegation + read roles.
CREATE OR REPLACE FUNCTION core.account_inflight_refresh_coordinates_with_authority(p_cap int DEFAULT 200) RETURNS jsonb
    LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path TO 'core','pg_catalog' AS $$
DECLARE v_account text; v_cap int := GREATEST(1, LEAST(COALESCE(p_cap, 200), 100000));
BEGIN
  SELECT account INTO v_account FROM core.resolve_session_identity() AS r(agent, account);
  PERFORM set_config('core.current_account', v_account, true);
  RETURN COALESCE((
    SELECT jsonb_agg(jsonb_build_object('repo', repo, 'branch', branch) ORDER BY repo, branch)
      FROM (
        SELECT DISTINCT repo, branch
          FROM core.claim
         WHERE account_id = v_account
           AND claim_state IN ('active', 'waiting')
           AND repo <> ''
         ORDER BY repo, branch
         LIMIT v_cap
      ) c
  ), '[]'::jsonb);
END $$;
ALTER FUNCTION core.account_inflight_refresh_coordinates_with_authority(int) OWNER TO veripsa_migrator;
REVOKE EXECUTE ON FUNCTION core.account_inflight_refresh_coordinates_with_authority(int) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION core.account_inflight_refresh_coordinates_with_authority(int)
  TO veripsa_app, veripsa_reader, veripsa_writer;

-- Cursor-aware policy slice enumerator. A look-ahead cap of two lets the worker process one coordinate and know
-- whether to yield/requeue without rescanning all repositories. The strict tuple cursor makes progress durable.
CREATE OR REPLACE FUNCTION core.account_inflight_refresh_coordinates_after_with_authority(
    p_after_repo text,p_after_branch text,p_cap int DEFAULT 2
) RETURNS jsonb
    LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path TO 'core','pg_catalog' AS $$
DECLARE
  v_account text; v_cap int:=GREATEST(1,LEAST(COALESCE(p_cap,2),1000));
  v_repo text:=COALESCE(p_after_repo,''); v_branch text:=COALESCE(p_after_branch,'');
BEGIN
  SELECT account INTO v_account FROM core.resolve_session_identity() AS r(agent,account);
  PERFORM set_config('core.current_account',v_account,true);
  RETURN COALESCE((
    SELECT jsonb_agg(jsonb_build_object('repo',repo,'branch',branch) ORDER BY repo,branch)
      FROM (
        SELECT DISTINCT repo,branch
          FROM core.claim
         WHERE account_id=v_account
           AND claim_state IN ('active','waiting')
           AND repo<>''
           AND (repo,branch)>(v_repo,v_branch)
         ORDER BY repo,branch
         LIMIT v_cap
      ) c
  ),'[]'::jsonb);
END $$;
ALTER FUNCTION core.account_inflight_refresh_coordinates_after_with_authority(text,text,int)
  OWNER TO veripsa_migrator;
REVOKE EXECUTE ON FUNCTION core.account_inflight_refresh_coordinates_after_with_authority(text,text,int)
  FROM PUBLIC;
GRANT EXECUTE ON FUNCTION core.account_inflight_refresh_coordinates_after_with_authority(text,text,int)
  TO veripsa_app,veripsa_reader,veripsa_writer;

-- ============================================================================================
