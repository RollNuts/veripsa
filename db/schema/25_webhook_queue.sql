-- Hosted GitHub App operational inbox.
--
-- WHY THIS EXISTS: the HTTP path returns 202 after accepting a webhook. Once 202 is sent, GitHub is not a
-- reliable retry source for a later worker crash/exception. This queue is the root durability boundary:
-- persist a SANITIZED delivery before ack, process it from the worker, and recover queued/stale-processing
-- rows on boot. It is NOT the product fact ledger (`core.event`) and it is not buyer-readable.
--
-- PRIVACY: payload is the minimized event shape the worker needs, not raw GitHub JSON. On success the payload is
-- replaced with `{}` and only content-free operational metadata remains.

-- HOT-DEPLOY EXPAND PHASE. Render applies this module while the previous image
-- still serves webhooks. Every relation/trigger/index change below therefore
-- commits as its own short statement before the long function-publication
-- transaction starts. Acquiring an AccessExclusive lock must never retain it
-- through thousands of later function/ACL statements and convoy every account.
-- The shape is additive; old workers ignore it.

CREATE TABLE IF NOT EXISTS core.webhook_delivery (
    delivery_key text PRIMARY KEY,
    event_type text NOT NULL,
    account_key text,
    repo text,
    payload jsonb DEFAULT '{}'::jsonb NOT NULL,
    status text DEFAULT 'queued' NOT NULL,
    attempts int DEFAULT 0 NOT NULL,
    received_at timestamptz DEFAULT now() NOT NULL,
    updated_at timestamptz DEFAULT now() NOT NULL,
    locked_at timestamptz,
    done_at timestamptz,
    last_error text,
    not_before timestamptz,
    lease_generation bigint DEFAULT 0 NOT NULL,
    causal_order_version smallint DEFAULT 0 NOT NULL,
    owner_instance text,
    retry_window_expires_at timestamptz,
    auto_rearm_count smallint DEFAULT 0 NOT NULL,
    operator_recovery_id text,
    operator_recovered_at timestamptz,
    operator_recovery_batch_size smallint,
    operator_recovery_batch_token uuid,
    operator_recovery_count smallint DEFAULT 0 NOT NULL,
    operator_continuation_id text,
    operator_continued_at timestamptz,
    operator_continuation_sha text,
    operator_continuation_count smallint DEFAULT 0 NOT NULL,
    operator_github_redelivery_delivery_id bigint,
    operator_github_redelivery_sha text,
    operator_github_redelivery_spent_at timestamptz,
    operator_github_redelivery_outcome text,
    operator_github_redelivery_count smallint DEFAULT 0 NOT NULL,
    CONSTRAINT webhook_delivery_key_len CHECK (length(delivery_key) BETWEEN 1 AND 200),
    CONSTRAINT webhook_delivery_event_len CHECK (length(event_type) BETWEEN 1 AND 80),
    CONSTRAINT webhook_delivery_account_len CHECK (account_key IS NULL OR length(account_key) <= 120),
    CONSTRAINT webhook_delivery_repo_len CHECK (repo IS NULL OR length(repo) <= 512),
    CONSTRAINT webhook_delivery_status_check CHECK (status = ANY (ARRAY['queued','processing','done','failed'])),
    CONSTRAINT webhook_delivery_attempts_ok CHECK (attempts >= 0),
    CONSTRAINT webhook_delivery_auto_rearm_count_ok CHECK (auto_rearm_count >= 0),
    CONSTRAINT webhook_delivery_operator_recovery_ok CHECK (
      (operator_recovery_count=0 AND operator_recovery_id IS NULL AND operator_recovered_at IS NULL
       AND operator_recovery_batch_size IS NULL AND operator_recovery_batch_token IS NULL)
      OR
      (operator_recovery_count=1 AND operator_recovery_id IS NOT NULL AND operator_recovered_at IS NOT NULL
       AND length(operator_recovery_id) BETWEEN 1 AND 120
       AND operator_recovery_id ~ '^[A-Za-z0-9][A-Za-z0-9._:-]{0,119}$'
       AND operator_recovery_batch_size IS NOT NULL
       AND operator_recovery_batch_size BETWEEN 1 AND 10
      AND operator_recovery_batch_token IS NOT NULL)
    ),
    CONSTRAINT webhook_delivery_operator_continuation_ok CHECK (
      (operator_continuation_count=0 AND operator_continuation_id IS NULL
       AND operator_continued_at IS NULL AND operator_continuation_sha IS NULL)
      OR
      (operator_continuation_count=1 AND operator_recovery_count=1
       AND operator_continuation_id IS NOT NULL
       AND length(operator_continuation_id) BETWEEN 1 AND 120
       AND operator_continuation_id ~ '^[A-Za-z0-9][A-Za-z0-9._:-]{0,119}$'
       AND operator_continued_at IS NOT NULL
       AND operator_continuation_sha IS NOT NULL
       AND operator_continuation_sha ~ '^[0-9a-f]{40}$')
    ),
    CONSTRAINT webhook_delivery_operator_github_redelivery_ok CHECK (
      (operator_github_redelivery_count=0
       AND operator_github_redelivery_delivery_id IS NULL
       AND operator_github_redelivery_sha IS NULL
       AND operator_github_redelivery_spent_at IS NULL
       AND operator_github_redelivery_outcome IS NULL)
      OR
      (operator_github_redelivery_count=1
       AND operator_recovery_count=1
       AND operator_continuation_count=1
       AND operator_github_redelivery_delivery_id IS NOT NULL
       AND operator_github_redelivery_delivery_id>0
       AND operator_github_redelivery_sha IS NOT NULL
       AND operator_github_redelivery_sha ~ '^[0-9a-f]{40}$'
       AND operator_github_redelivery_spent_at IS NOT NULL
       AND (operator_github_redelivery_outcome IS NULL
            OR operator_github_redelivery_outcome IN (
              'accepted','transport_ambiguous','redirect_rejected',
              'auth_rejected','rate_limited','request_rejected',
              'server_rejected','unexpected_status')))
    ),
    CONSTRAINT webhook_delivery_error_len CHECK (last_error IS NULL OR length(last_error) <= 300)
);
SELECT core._ensure_column_online(
  'webhook_delivery','not_before','timestamptz');
-- Monotonic claim ownership token. Unlike attempts it is never reset by DLQ rearm, so stale workers cannot ABA
-- finalize a newer owner after attempts returns to zero.
SELECT core._ensure_column_online(
  'webhook_delivery','lease_generation','bigint DEFAULT 0 NOT NULL');
-- 0 means the row may have failed before causal lanes existed and therefore must not be auto-replayed when its
-- relationship to already-completed newer state is unknowable. Authority enqueue/claim stamps protocol 1.
SELECT core._ensure_column_online(
  'webhook_delivery','causal_order_version',
  'smallint DEFAULT 0 NOT NULL');
-- Atomic claim ownership. New workers store a claim-unique owner reference here in the SAME transaction that
-- advances lease_generation. The later dead-instance section adds the length constraint and heartbeat table.
-- Keep the ADD near the claim API so a rolling re-apply cannot publish the /5 function before its column exists.
SELECT core._ensure_column_online(
  'webhook_delivery','owner_instance','text');
-- One absolute execution window spans every ordinary durable replay. An
-- intentional consistency/fanout yield starts a later window because it
-- proves progress or an external wait; an ordinary failure never extends it.
SELECT core._ensure_column_online(
  'webhook_delivery','retry_window_expires_at','timestamptz');
-- Automatic DLQ recovery is finite. A genuine GitHub redelivery is the only
-- ingress authority that resets this counter; background recovery never
-- manufactures an unbounded sequence of fresh attempt epochs.
SELECT core._ensure_column_online(
  'webhook_delivery','auto_rearm_count',
  'smallint DEFAULT 0 NOT NULL');
-- Exact, durable operator-recovery audit. The count is a lifetime one-shot
-- fence, independent of the automatic epoch counter (which a genuine signed
-- GitHub duplicate is still allowed to reset). The DB-generated random batch
-- token is deliberately not derived from delivery GUIDs: a retained sibling
-- cannot reconstruct or correlate a hard-erased receipt. Old workers ignore
-- these additive columns; new recovery never accepts a replacement payload.
SELECT core._ensure_column_online(
  'webhook_delivery','operator_recovery_id','text');
SELECT core._ensure_column_online(
  'webhook_delivery','operator_recovered_at','timestamptz');
SELECT core._ensure_column_online(
  'webhook_delivery','operator_recovery_batch_size','smallint');
SELECT core._ensure_column_online(
  'webhook_delivery','operator_recovery_batch_token','uuid');
SELECT core._ensure_column_online(
  'webhook_delivery','operator_recovery_count',
  'smallint DEFAULT 0 NOT NULL');
-- A failed first operator epoch may be continued exactly once after a reviewed
-- code correction. This audit is distinct from auto_rearm_count: a genuine
-- signed duplicate may reset automatic retry state but may not erase the
-- operator continuation's immutable id/time/exact-SHA proof.
SELECT core._ensure_column_online(
  'webhook_delivery','operator_continuation_id','text');
SELECT core._ensure_column_online(
  'webhook_delivery','operator_continued_at','timestamptz');
SELECT core._ensure_column_online(
  'webhook_delivery','operator_continuation_sha','text');
SELECT core._ensure_column_online(
  'webhook_delivery','operator_continuation_count',
  'smallint DEFAULT 0 NOT NULL');
-- A genuine GitHub App redelivery is an external, non-idempotent mutation.
-- Spend authority durably before its POST and never reset it on a signed
-- duplicate: a lost HTTP acknowledgement must not authorize a second POST.
-- The provider delivery id and exact reviewed artifact stay private DB audit;
-- neither is returned by the SECURITY DEFINER protocol below.
SELECT core._ensure_column_online(
  'webhook_delivery','operator_github_redelivery_delivery_id','bigint');
SELECT core._ensure_column_online(
  'webhook_delivery','operator_github_redelivery_sha','text');
SELECT core._ensure_column_online(
  'webhook_delivery','operator_github_redelivery_spent_at','timestamptz');
SELECT core._ensure_column_online(
  'webhook_delivery','operator_github_redelivery_outcome','text');
SELECT core._ensure_column_online(
  'webhook_delivery','operator_github_redelivery_count',
  'smallint DEFAULT 0 NOT NULL');
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1
      FROM pg_constraint q
      JOIN pg_class c ON c.oid=q.conrelid
      JOIN pg_namespace n ON n.oid=c.relnamespace
     WHERE n.nspname='core' AND c.relname='webhook_delivery'
       AND q.conname='webhook_delivery_auto_rearm_count_ok'
  ) THEN
    ALTER TABLE core.webhook_delivery
      ADD CONSTRAINT webhook_delivery_auto_rearm_count_ok
      CHECK (auto_rearm_count >= 0) NOT VALID;
  END IF;
END $$;
DO $$
BEGIN
  IF EXISTS (
    SELECT 1
      FROM pg_constraint q
      JOIN pg_class c ON c.oid=q.conrelid
      JOIN pg_namespace n ON n.oid=c.relnamespace
     WHERE n.nspname='core' AND c.relname='webhook_delivery'
       AND q.conname='webhook_delivery_auto_rearm_count_ok'
       AND NOT q.convalidated
  ) THEN
    ALTER TABLE core.webhook_delivery
      VALIDATE CONSTRAINT webhook_delivery_auto_rearm_count_ok;
  END IF;
END $$;
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint q
     WHERE q.conrelid='core.webhook_delivery'::regclass
       AND q.conname='webhook_delivery_operator_recovery_ok'
       AND regexp_replace(
             replace(pg_get_constraintdef(q.oid),'::text',''),
             '[[:space:]()]','','g') =
           'CHECKoperator_recovery_count=0ANDoperator_recovery_idISNULLANDoperator_recovered_atISNULLANDoperator_recovery_batch_sizeISNULLANDoperator_recovery_batch_tokenISNULLORoperator_recovery_count=1ANDoperator_recovery_idISNOTNULLANDoperator_recovered_atISNOTNULLANDlengthoperator_recovery_id>=1ANDlengthoperator_recovery_id<=120ANDoperator_recovery_id~''^[A-Za-z0-9][A-Za-z0-9._:-]{0,119}$''ANDoperator_recovery_batch_sizeISNOTNULLANDoperator_recovery_batch_size>=1ANDoperator_recovery_batch_size<=10ANDoperator_recovery_batch_tokenISNOTNULL'
  ) THEN
    ALTER TABLE core.webhook_delivery
      DROP CONSTRAINT IF EXISTS webhook_delivery_operator_recovery_ok;
    ALTER TABLE core.webhook_delivery
      ADD CONSTRAINT webhook_delivery_operator_recovery_ok
      CHECK (
        (operator_recovery_count=0 AND operator_recovery_id IS NULL AND operator_recovered_at IS NULL
         AND operator_recovery_batch_size IS NULL AND operator_recovery_batch_token IS NULL)
        OR
        (operator_recovery_count=1 AND operator_recovery_id IS NOT NULL AND operator_recovered_at IS NOT NULL
         AND length(operator_recovery_id) BETWEEN 1 AND 120
         AND operator_recovery_id ~ '^[A-Za-z0-9][A-Za-z0-9._:-]{0,119}$'
         AND operator_recovery_batch_size IS NOT NULL
         AND operator_recovery_batch_size BETWEEN 1 AND 10
         AND operator_recovery_batch_token IS NOT NULL)
      ) NOT VALID;
  END IF;
END $$;
DO $$
BEGIN
  IF EXISTS (
    SELECT 1
      FROM pg_constraint q
      JOIN pg_class c ON c.oid=q.conrelid
      JOIN pg_namespace n ON n.oid=c.relnamespace
     WHERE n.nspname='core' AND c.relname='webhook_delivery'
       AND q.conname='webhook_delivery_operator_recovery_ok'
       AND NOT q.convalidated
  ) THEN
    ALTER TABLE core.webhook_delivery
      VALIDATE CONSTRAINT webhook_delivery_operator_recovery_ok;
  END IF;
END $$;
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint q
     WHERE q.conrelid='core.webhook_delivery'::regclass
       AND q.conname='webhook_delivery_operator_continuation_ok'
       AND regexp_replace(
             replace(pg_get_constraintdef(q.oid),'::text',''),
             '[[:space:]()]','','g') =
           'CHECKoperator_continuation_count=0ANDoperator_continuation_idISNULLANDoperator_continued_atISNULLANDoperator_continuation_shaISNULLORoperator_continuation_count=1ANDoperator_recovery_count=1ANDoperator_continuation_idISNOTNULLANDlengthoperator_continuation_id>=1ANDlengthoperator_continuation_id<=120ANDoperator_continuation_id~''^[A-Za-z0-9][A-Za-z0-9._:-]{0,119}$''ANDoperator_continued_atISNOTNULLANDoperator_continuation_shaISNOTNULLANDoperator_continuation_sha~''^[0-9a-f]{40}$'''
  ) THEN
    ALTER TABLE core.webhook_delivery
      DROP CONSTRAINT IF EXISTS webhook_delivery_operator_continuation_ok;
    ALTER TABLE core.webhook_delivery
      ADD CONSTRAINT webhook_delivery_operator_continuation_ok
      CHECK (
        (operator_continuation_count=0 AND operator_continuation_id IS NULL
         AND operator_continued_at IS NULL AND operator_continuation_sha IS NULL)
        OR
        (operator_continuation_count=1 AND operator_recovery_count=1
         AND operator_continuation_id IS NOT NULL
         AND length(operator_continuation_id) BETWEEN 1 AND 120
         AND operator_continuation_id ~ '^[A-Za-z0-9][A-Za-z0-9._:-]{0,119}$'
         AND operator_continued_at IS NOT NULL
         AND operator_continuation_sha IS NOT NULL
         AND operator_continuation_sha ~ '^[0-9a-f]{40}$')
      ) NOT VALID;
  END IF;
END $$;
DO $$
BEGIN
  IF EXISTS (
    SELECT 1 FROM pg_constraint q
     WHERE q.conrelid='core.webhook_delivery'::regclass
       AND q.conname='webhook_delivery_operator_continuation_ok'
       AND NOT q.convalidated
  ) THEN
    ALTER TABLE core.webhook_delivery
      VALIDATE CONSTRAINT webhook_delivery_operator_continuation_ok;
  END IF;
END $$;
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint q
     WHERE q.conrelid='core.webhook_delivery'::regclass
       AND q.conname='webhook_delivery_operator_github_redelivery_ok'
       AND regexp_replace(
             replace(pg_get_constraintdef(q.oid),'::text',''),
             '[[:space:]()]','','g') =
           'CHECKoperator_github_redelivery_count=0ANDoperator_github_redelivery_delivery_idISNULLANDoperator_github_redelivery_shaISNULLANDoperator_github_redelivery_spent_atISNULLANDoperator_github_redelivery_outcomeISNULLORoperator_github_redelivery_count=1ANDoperator_recovery_count=1ANDoperator_continuation_count=1ANDoperator_github_redelivery_delivery_idISNOTNULLANDoperator_github_redelivery_delivery_id>0ANDoperator_github_redelivery_shaISNOTNULLANDoperator_github_redelivery_sha~''^[0-9a-f]{40}$''ANDoperator_github_redelivery_spent_atISNOTNULLANDoperator_github_redelivery_outcomeISNULLORoperator_github_redelivery_outcome=ANYARRAY[''accepted'',''transport_ambiguous'',''redirect_rejected'',''auth_rejected'',''rate_limited'',''request_rejected'',''server_rejected'',''unexpected_status'']'
  ) THEN
    ALTER TABLE core.webhook_delivery
      DROP CONSTRAINT IF EXISTS webhook_delivery_operator_github_redelivery_ok;
    ALTER TABLE core.webhook_delivery
      ADD CONSTRAINT webhook_delivery_operator_github_redelivery_ok
      CHECK (
        (operator_github_redelivery_count=0
         AND operator_github_redelivery_delivery_id IS NULL
         AND operator_github_redelivery_sha IS NULL
         AND operator_github_redelivery_spent_at IS NULL
         AND operator_github_redelivery_outcome IS NULL)
        OR
        (operator_github_redelivery_count=1
         AND operator_recovery_count=1
         AND operator_continuation_count=1
         AND operator_github_redelivery_delivery_id IS NOT NULL
         AND operator_github_redelivery_delivery_id>0
         AND operator_github_redelivery_sha IS NOT NULL
         AND operator_github_redelivery_sha ~ '^[0-9a-f]{40}$'
         AND operator_github_redelivery_spent_at IS NOT NULL
         AND (operator_github_redelivery_outcome IS NULL
              OR operator_github_redelivery_outcome IN (
                'accepted','transport_ambiguous','redirect_rejected',
                'auth_rejected','rate_limited','request_rejected',
                'server_rejected','unexpected_status')))
      ) NOT VALID;
  END IF;
END $$;
DO $$
BEGIN
  IF EXISTS (
    SELECT 1 FROM pg_constraint q
     WHERE q.conrelid='core.webhook_delivery'::regclass
       AND q.conname='webhook_delivery_operator_github_redelivery_ok'
       AND NOT q.convalidated
  ) THEN
    ALTER TABLE core.webhook_delivery
      VALIDATE CONSTRAINT webhook_delivery_operator_github_redelivery_ok;
  END IF;
END $$;
-- Every terminal/erased transition, including lifecycle functions in later
-- schema modules, clears the executable-window coordinate centrally.
CREATE OR REPLACE FUNCTION core._clear_terminal_webhook_retry_window()
RETURNS trigger
    LANGUAGE plpgsql SET search_path TO 'pg_catalog' AS $$
BEGIN
  IF NEW.status='done' OR NEW.event_type='erased' THEN
    NEW.retry_window_expires_at := NULL;
  END IF;
  -- Hard erasure removes the operator correlation token as well as tenant
  -- coordinates. Ordinary done/failed transitions retain the recovery audit.
  IF NEW.event_type='erased' THEN
    NEW.operator_recovery_id := NULL;
    NEW.operator_recovered_at := NULL;
    NEW.operator_recovery_batch_size := NULL;
    NEW.operator_recovery_batch_token := NULL;
    NEW.operator_recovery_count := 0;
    NEW.operator_continuation_id := NULL;
    NEW.operator_continued_at := NULL;
    NEW.operator_continuation_sha := NULL;
    NEW.operator_continuation_count := 0;
    NEW.operator_github_redelivery_delivery_id := NULL;
    NEW.operator_github_redelivery_sha := NULL;
    NEW.operator_github_redelivery_spent_at := NULL;
    NEW.operator_github_redelivery_outcome := NULL;
    NEW.operator_github_redelivery_count := 0;
  END IF;
  RETURN NEW;
END $$;
ALTER FUNCTION core._clear_terminal_webhook_retry_window() OWNER TO veripsa_migrator;
REVOKE EXECUTE ON FUNCTION core._clear_terminal_webhook_retry_window()
  FROM PUBLIC,veripsa_app,veripsa_writer;
-- This is a versioned, create-once trigger. Never DROP the live safety
-- boundary before a replacement has acquired its lock: a failed pre-deploy
-- must leave the previous image with at least the old trigger, not no trigger.
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1
      FROM pg_trigger t
      JOIN pg_class c ON c.oid=t.tgrelid
      JOIN pg_namespace n ON n.oid=c.relnamespace
     WHERE n.nspname='core' AND c.relname='webhook_delivery'
       AND t.tgname='webhook_delivery_clear_terminal_retry_window'
       AND NOT t.tgisinternal
  ) THEN
    CREATE TRIGGER webhook_delivery_clear_terminal_retry_window
      BEFORE INSERT OR UPDATE OF status,event_type ON core.webhook_delivery
      FOR EACH ROW EXECUTE FUNCTION core._clear_terminal_webhook_retry_window();
  END IF;
END $$;
-- One-time rolling bridge: a worker that claimed before this column existed owns generation 1. Re-apply is
-- idempotent, while any new reclaim advances to 2 and makes that old worker's generation-1 shim safely no-op.
UPDATE core.webhook_delivery
   SET lease_generation=1
 WHERE status='processing' AND lease_generation=0;
DO $$
BEGIN
  IF EXISTS (
    SELECT 1
      FROM pg_class c
      JOIN pg_namespace n ON n.oid = c.relnamespace
     WHERE n.nspname = 'core'
       AND c.relname = 'webhook_delivery'
       AND c.relowner <> 'veripsa_migrator'::regrole
  ) THEN
    ALTER TABLE core.webhook_delivery OWNER TO veripsa_migrator;
  END IF;
END $$;
-- Concurrent builds do not stop accepted delivery DML. The central
-- 05_online_index_repair registry handles retry shells before any module so
-- uniqueness/table/constraint guards cannot drift between local copies.
CREATE INDEX CONCURRENTLY IF NOT EXISTS webhook_delivery_pending
  ON core.webhook_delivery (status, received_at) WHERE status IN ('queued','processing');
CREATE INDEX CONCURRENTLY IF NOT EXISTS webhook_delivery_due
  ON core.webhook_delivery (status, not_before, received_at) WHERE status IN ('queued','processing');
CREATE INDEX CONCURRENTLY IF NOT EXISTS webhook_delivery_account_pending
  ON core.webhook_delivery ((COALESCE(account_key,'')), received_at)
  WHERE status IN ('queued','processing');
-- Claim/pending/supersession perform parameterized account-head probes. Include the deterministic tuple and every
-- unfinished/DLQ state so the hot head lookup and the rare uninstall absorption are indexed as the inbox grows.
-- v2 is a new name because IF NOT EXISTS could not replace an earlier rollout predicate during a hot re-apply.
CREATE INDEX CONCURRENTLY IF NOT EXISTS webhook_delivery_account_causal_v2
  ON core.webhook_delivery ((COALESCE(account_key,'')), received_at, delivery_key)
  WHERE status IN ('queued','processing','failed');
CREATE INDEX CONCURRENTLY IF NOT EXISTS webhook_delivery_repository_causal_v1
  ON core.webhook_delivery ((payload->'repository'->>'id'), received_at, delivery_key)
  WHERE event_type IN ('repository','pull_request','push','check_suite','check_run','merge_group')
    AND status IN ('queued','processing','failed');
-- Recovery checks aged blocked lanes every bounded tick. Keep its only failed
-- candidate set finite-indexed so an exhausted retained DLQ cannot turn that
-- liveness fix into a fleet-wide table scan.
CREATE INDEX CONCURRENTLY IF NOT EXISTS webhook_delivery_failed_auto_rearm
  ON core.webhook_delivery (received_at, delivery_key)
  WHERE status='failed'
    AND auto_rearm_count<1
    AND (causal_order_version>=1 OR event_type='ping');
-- The signed web audit resolves one operator batch by its private recovery
-- label. Keep that exceptional read off the retained inbox's cold rows while
-- preserving delivery_key order for the forward-only spend proof. The random
-- batch-token index keeps the global anti-collision proof sparse as retention
-- grows; both predicates include every row that could carry the lookup value.
CREATE INDEX CONCURRENTLY IF NOT EXISTS webhook_delivery_operator_recovery_id_v1
  ON core.webhook_delivery (operator_recovery_id, delivery_key)
  WHERE operator_recovery_id IS NOT NULL;
CREATE INDEX CONCURRENTLY IF NOT EXISTS webhook_delivery_operator_batch_token_v1
  ON core.webhook_delivery (operator_recovery_batch_token)
  WHERE operator_recovery_batch_token IS NOT NULL;
CREATE INDEX CONCURRENTLY IF NOT EXISTS webhook_delivery_repo
  ON core.webhook_delivery (repo, updated_at DESC);

-- Dead-instance heartbeat relation, expanded before any liveness functions.
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1
      FROM pg_constraint q
      JOIN pg_class c ON c.oid=q.conrelid
      JOIN pg_namespace n ON n.oid=c.relnamespace
     WHERE n.nspname='core' AND c.relname='webhook_delivery'
       AND q.conname='webhook_delivery_owner_len'
  ) THEN
    ALTER TABLE core.webhook_delivery
      ADD CONSTRAINT webhook_delivery_owner_len
      CHECK (owner_instance IS NULL OR length(owner_instance) BETWEEN 1 AND 64) NOT VALID;
  END IF;
END $$;
DO $$
BEGIN
  IF EXISTS (
    SELECT 1
      FROM pg_constraint q
      JOIN pg_class c ON c.oid=q.conrelid
      JOIN pg_namespace n ON n.oid=c.relnamespace
     WHERE n.nspname='core' AND c.relname='webhook_delivery'
       AND q.conname='webhook_delivery_owner_len'
       AND NOT q.convalidated
  ) THEN
    ALTER TABLE core.webhook_delivery
      VALIDATE CONSTRAINT webhook_delivery_owner_len;
  END IF;
END $$;
CREATE TABLE IF NOT EXISTS core.webhook_worker_instance (
    instance_id text PRIMARY KEY,
    last_heartbeat timestamptz NOT NULL DEFAULT now(),
    started_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT webhook_worker_instance_len CHECK (length(instance_id) BETWEEN 1 AND 64)
);
DO $$
BEGIN
  IF EXISTS (
    SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
     WHERE n.nspname='core' AND c.relname='webhook_worker_instance'
       AND c.relowner <> 'veripsa_migrator'::regrole
  ) THEN
    ALTER TABLE core.webhook_worker_instance OWNER TO veripsa_migrator;
  END IF;
END $$;
REVOKE ALL ON TABLE core.webhook_worker_instance FROM PUBLIC,veripsa_writer,veripsa_app;

-- GitHub failed-delivery recovery control plane. These tables hold only
-- content-free delivery metadata; expand them before publishing their API.
CREATE TABLE IF NOT EXISTS core.github_delivery_recovery_scan (
    singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
    scan_epoch bigint DEFAULT 0 NOT NULL,
    in_progress boolean DEFAULT false NOT NULL,
    cursor text,
    high_water_delivery_id bigint,
    scan_ceiling_delivery_id bigint,
    page_tail_delivery_id bigint,
    page_tail_delivered_at timestamptz,
    cutoff_at timestamptz,
    scan_started_at timestamptz,
    scan_completed_at timestamptz,
    redelivery_not_before timestamptz,
    archived_unresolved_count bigint DEFAULT 0 NOT NULL,
    archived_terminal_count bigint DEFAULT 0 NOT NULL,
    archived_exhausted_count bigint DEFAULT 0 NOT NULL,
    archived_last_at timestamptz,
    updated_at timestamptz DEFAULT now() NOT NULL,
    CONSTRAINT github_delivery_scan_cursor_len CHECK (cursor IS NULL OR length(cursor) <= 2048),
    CONSTRAINT github_delivery_scan_epoch_ok CHECK (scan_epoch >= 0),
    CONSTRAINT github_delivery_scan_hwm_ok CHECK (
      high_water_delivery_id IS NULL OR high_water_delivery_id > 0),
    CONSTRAINT github_delivery_scan_ceiling_ok CHECK (
      scan_ceiling_delivery_id IS NULL OR scan_ceiling_delivery_id > 0),
    CONSTRAINT github_delivery_scan_tail_pair_ok CHECK (
      (page_tail_delivery_id IS NULL) = (page_tail_delivered_at IS NULL)
      AND (page_tail_delivery_id IS NULL OR page_tail_delivery_id > 0)),
    CONSTRAINT github_delivery_scan_archive_counts_ok CHECK (
      archived_unresolved_count >= 0 AND archived_terminal_count >= 0
      AND archived_exhausted_count >= 0)
);
CREATE TABLE IF NOT EXISTS core.github_delivery_recovery (
    delivery_guid text PRIMARY KEY,
    latest_delivery_id bigint NOT NULL,
    latest_delivered_at timestamptz NOT NULL,
    latest_status_code int NOT NULL,
    latest_status text NOT NULL,
    latest_recovery_class text NOT NULL,
    locally_received boolean DEFAULT false NOT NULL,
    resolved boolean DEFAULT false NOT NULL,
    attempt_count int DEFAULT 0 NOT NULL,
    attempt_generation bigint DEFAULT 0 NOT NULL,
    claimed_at timestamptz,
    next_attempt_at timestamptz,
    last_attempt_at timestamptz,
    last_attempt_outcome text,
    window_expires_at timestamptz NOT NULL,
    first_seen_at timestamptz DEFAULT now() NOT NULL,
    last_seen_at timestamptz DEFAULT now() NOT NULL,
    CONSTRAINT github_delivery_recovery_guid_len CHECK (length(delivery_guid) BETWEEN 1 AND 200),
    CONSTRAINT github_delivery_recovery_id_ok CHECK (latest_delivery_id > 0),
    CONSTRAINT github_delivery_recovery_status_code_ok CHECK (latest_status_code BETWEEN -1 AND 599),
    CONSTRAINT github_delivery_recovery_status_enum CHECK (
      latest_status IN ('OK','Timed Out','Other','Unknown')),
    CONSTRAINT github_delivery_recovery_class_ok CHECK (
      latest_recovery_class IN ('resolved','redeliverable','timeout','terminal')),
    CONSTRAINT github_delivery_recovery_attempts_ok CHECK (attempt_count BETWEEN 0 AND 3),
    CONSTRAINT github_delivery_recovery_generation_ok CHECK (attempt_generation >= 0),
    CONSTRAINT github_delivery_recovery_outcome_ok CHECK (
      last_attempt_outcome IS NULL OR last_attempt_outcome IN (
        'accepted_ambiguous','transport_ambiguous','rate_limited','auth_deferred','terminal_rejected'))
);
SELECT core._ensure_column_online(
  'github_delivery_recovery_scan','redelivery_not_before','timestamptz');
SELECT core._ensure_column_online(
  'github_delivery_recovery_scan','page_tail_delivery_id','bigint');
SELECT core._ensure_column_online(
  'github_delivery_recovery_scan','page_tail_delivered_at','timestamptz');
SELECT core._ensure_column_online(
  'github_delivery_recovery_scan','archived_unresolved_count',
  'bigint DEFAULT 0 NOT NULL');
SELECT core._ensure_column_online(
  'github_delivery_recovery_scan','archived_terminal_count',
  'bigint DEFAULT 0 NOT NULL');
SELECT core._ensure_column_online(
  'github_delivery_recovery_scan','archived_exhausted_count',
  'bigint DEFAULT 0 NOT NULL');
SELECT core._ensure_column_online(
  'github_delivery_recovery_scan','archived_last_at','timestamptz');
DO $$
BEGIN
  IF EXISTS (
    SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
     WHERE n.nspname='core' AND c.relname='github_delivery_recovery_scan'
       AND c.relowner <> 'veripsa_migrator'::regrole
  ) THEN
    ALTER TABLE core.github_delivery_recovery_scan OWNER TO veripsa_migrator;
  END IF;
  IF EXISTS (
    SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
     WHERE n.nspname='core' AND c.relname='github_delivery_recovery'
       AND c.relowner <> 'veripsa_migrator'::regrole
  ) THEN
    ALTER TABLE core.github_delivery_recovery OWNER TO veripsa_migrator;
  END IF;
END $$;
CREATE INDEX CONCURRENTLY IF NOT EXISTS github_delivery_recovery_due
  ON core.github_delivery_recovery (next_attempt_at,window_expires_at)
  WHERE NOT resolved AND attempt_count < 3
    AND latest_recovery_class IN ('redeliverable','timeout');
CREATE INDEX CONCURRENTLY IF NOT EXISTS github_delivery_recovery_expiry
  ON core.github_delivery_recovery (window_expires_at);

-- Table ACLs are also short expand statements. Function ACLs remain atomic
-- with their definitions below.
REVOKE ALL ON TABLE core.webhook_delivery FROM PUBLIC;
REVOKE ALL ON TABLE core.github_delivery_recovery_scan FROM PUBLIC;
REVOKE ALL ON TABLE core.github_delivery_recovery FROM PUBLIC;

-- Publish the rolling queue, liveness, and delivery-recovery function protocols
-- as one catalog state. This transaction contains no table/index/trigger DDL
-- and no top-level data mutation, so it cannot retain live DML locks.
BEGIN;

CREATE OR REPLACE FUNCTION core.enqueue_webhook_delivery_with_authority(
    p_key text,
    p_event_type text,
    p_account_key text,
    p_repo text,
    p_payload jsonb,
    p_max_pending int,
    p_protocol int
) RETURNS jsonb
    LANGUAGE plpgsql SECURITY DEFINER SET search_path TO 'core','pg_catalog' AS $$
DECLARE v_status text; v_payload jsonb; v_existing boolean; v_pending int;
        v_account_key text; v_existing_account_key text; v_lock_account_key text;
        v_causal_order_version smallint; v_existing_status text; v_existing_event_type text;
        v_existing_causal_order_version smallint; v_repository_id text;
BEGIN
  IF COALESCE(p_protocol,0) NOT IN (1,2) THEN
    RAISE EXCEPTION 'unsupported durable webhook enqueue protocol: %', p_protocol USING ERRCODE='22023';
  END IF;
  v_causal_order_version := CASE WHEN p_protocol=2 THEN 1 ELSE 0 END;
  IF p_key IS NULL OR length(btrim(p_key)) < 1 THEN
    RAISE EXCEPTION 'webhook delivery key required' USING ERRCODE='23514';
  END IF;
  IF p_event_type IS NULL OR length(btrim(p_event_type)) < 1 THEN
    RAISE EXCEPTION 'webhook event type required' USING ERRCODE='23514';
  END IF;
  v_payload := COALESCE(p_payload, '{}'::jsonb);
  IF jsonb_typeof(v_payload) <> 'object' THEN
    v_payload := '{}'::jsonb;
  END IF;

  v_account_key := NULLIF(left(COALESCE(p_account_key,''),120),'');
  SELECT true,d.account_key,d.status,d.event_type,d.causal_order_version
    INTO v_existing,v_existing_account_key,v_existing_status,v_existing_event_type,
         v_existing_causal_order_version
    FROM core.webhook_delivery d WHERE d.delivery_key=left(p_key,200);
  -- Serialize account admission across enqueue and claim transactions. In particular, a second connection cannot
  -- insert/claim a newer row while an older enqueue has returned but is still uncommitted and therefore invisible.
  -- A duplicate delivery keeps its stored account/order, so lock that account rather than caller-supplied metadata.
  v_lock_account_key := CASE WHEN COALESCE(v_existing,false)
                             THEN COALESCE(v_existing_account_key,v_account_key) ELSE v_account_key END;
  IF v_lock_account_key IS NOT NULL THEN
    PERFORM pg_advisory_xact_lock(
      hashtext('core.webhook_delivery.account'),hashtext(v_lock_account_key));
  END IF;
  v_repository_id := NULLIF(left(COALESCE(v_payload->'repository'->>'id',''),80),'');
  IF left(COALESCE(p_event_type,''),80) IN (
       'repository','pull_request','push','check_suite','check_run','merge_group')
     AND v_repository_id IS NOT NULL THEN
    -- Repository rename/transfer crosses account keys. Share a global stable-id admission lock so received_at is
    -- assigned only after every earlier same-object enqueue commits and becomes visible.
    PERFORM pg_advisory_xact_lock(
      hashtext('core.webhook_delivery.repository'),hashtext(v_repository_id));
  END IF;
  -- The pre-lock probe chooses the immutable account lock. Re-read mutable status/version after waiting: a worker
  -- may have failed the row while this duplicate was blocked, and legacy quarantine must be byte-invariant.
  SELECT true,d.account_key,d.status,d.event_type,d.causal_order_version
    INTO v_existing,v_existing_account_key,v_existing_status,v_existing_event_type,
         v_existing_causal_order_version
    FROM core.webhook_delivery d WHERE d.delivery_key=left(p_key,200);
  v_existing := FOUND;
  IF COALESCE(v_existing,false)
     AND v_existing_status='failed'
     AND COALESCE(v_existing_causal_order_version,0)=0
     AND v_existing_event_type<>'ping' THEN
    RETURN jsonb_build_object('accepted',false,'queued',false,
                              'reason','legacy_failed_quarantined','status','failed');
  END IF;
  IF COALESCE(p_max_pending,0) > 0 AND NOT COALESCE(v_existing,false) THEN
    SELECT count(*)::int INTO v_pending
      FROM core.webhook_delivery
     WHERE status IN ('queued','processing');
    IF v_pending >= p_max_pending THEN
      RETURN jsonb_build_object('accepted', false, 'reason', 'backlog_full', 'pending', v_pending);
    END IF;
  END IF;

  -- received_at is fixed only after acquiring the account lock. ON CONFLICT intentionally never assigns it, so a
  -- duplicate retains the immutable ordering coordinate of its first durable admission.
  INSERT INTO core.webhook_delivery(
      delivery_key,event_type,account_key,repo,payload,status,received_at,updated_at,last_error,locked_at,done_at,
      causal_order_version)
  VALUES (left(p_key,200), left(p_event_type,80), v_account_key,
          NULLIF(left(COALESCE(p_repo,''),512),''), v_payload, 'queued', clock_timestamp(), now(), NULL, NULL, NULL,
          v_causal_order_version)
  ON CONFLICT (delivery_key) DO UPDATE SET
      updated_at = now(),
      status = CASE
          WHEN core.webhook_delivery.status = 'done' THEN 'done'
          WHEN core.webhook_delivery.status = 'processing' THEN 'processing'
          -- A pre-lane stateful failure may already have been overtaken by newer done work. A redelivery of the
          -- same key is not enough proof to replay it safely; keep it visible/inert instead of rewinding state.
          WHEN core.webhook_delivery.status='failed'
               AND core.webhook_delivery.causal_order_version=0
               AND core.webhook_delivery.event_type<>'ping' THEN 'failed'
          ELSE 'queued'
      END,
      payload = CASE
          WHEN core.webhook_delivery.status IN ('done','processing') THEN core.webhook_delivery.payload
          ELSE EXCLUDED.payload
               -- A GitHub redelivery may race the durable retry after one or more repositories already committed.
               -- Refresh the sanitized event envelope, but never erase the immutable plan/checkpoint proof.
               || CASE WHEN core.webhook_delivery.payload ? '_veripsa_fanout_plan'
                       THEN jsonb_build_object(
                         '_veripsa_fanout_plan',
                         core.webhook_delivery.payload->'_veripsa_fanout_plan')
                       ELSE '{}'::jsonb END
               || CASE WHEN core.webhook_delivery.payload ? '_veripsa_fanout_completed'
                       THEN jsonb_build_object(
                         '_veripsa_fanout_completed',
                         core.webhook_delivery.payload->'_veripsa_fanout_completed')
                       ELSE '{}'::jsonb END
      END,
      attempts = CASE
          WHEN (core.webhook_delivery.status='failed'
                OR (core.webhook_delivery.status='queued'
                    AND core.webhook_delivery.auto_rearm_count>0))
               AND (core.webhook_delivery.causal_order_version>=1
                    OR core.webhook_delivery.event_type='ping') THEN 0
          ELSE core.webhook_delivery.attempts END,
      retry_window_expires_at = CASE
          WHEN (core.webhook_delivery.status='failed'
                OR (core.webhook_delivery.status='queued'
                    AND core.webhook_delivery.auto_rearm_count>0))
               AND (core.webhook_delivery.causal_order_version>=1
                    OR core.webhook_delivery.event_type='ping') THEN NULL
          ELSE core.webhook_delivery.retry_window_expires_at END,
      auto_rearm_count = CASE
          -- A signed duplicate racing immediately after automatic re-arm sees
          -- queued+count>0. It is still explicit ingress authority and must
          -- reset the finite epoch exactly as if it had won before re-arm.
          -- If the re-armed generation is already processing, preserve its
          -- exact lease/attempt/window but reset only this counter. A failure
          -- then leaves one fresh automatic epoch for the signed redelivery;
          -- otherwise the accepted duplicate would be silently consumed by
          -- the already-running, ultimately failed generation.
          WHEN (core.webhook_delivery.status='failed'
                OR (core.webhook_delivery.status='queued'
                    AND core.webhook_delivery.auto_rearm_count>0)
                OR (core.webhook_delivery.status='processing'
                    AND core.webhook_delivery.auto_rearm_count>0))
               AND (core.webhook_delivery.causal_order_version>=1
                    OR core.webhook_delivery.event_type='ping') THEN 0
          ELSE core.webhook_delivery.auto_rearm_count END,
      last_error = CASE
          WHEN core.webhook_delivery.status IN ('done','processing') THEN core.webhook_delivery.last_error
          ELSE NULL
      END,
      locked_at = CASE
          WHEN core.webhook_delivery.status IN ('done','processing') THEN core.webhook_delivery.locked_at
          ELSE NULL
      END,
      owner_instance = CASE
          WHEN core.webhook_delivery.status IN ('done','processing') THEN core.webhook_delivery.owner_instance
          ELSE NULL
      END,
      not_before = CASE
          WHEN core.webhook_delivery.status IN ('done','processing') THEN core.webhook_delivery.not_before
          ELSE NULL
      END,
      done_at = CASE WHEN core.webhook_delivery.status = 'done' THEN core.webhook_delivery.done_at ELSE NULL END
  -- Close the re-read→conflict race: release() does not take the admission advisory lock and may turn a row into
  -- causal-v0 failed after the probe above. In that case perform no UPDATE at all—every stored byte is quarantine.
  WHERE NOT (core.webhook_delivery.status='failed'
             AND core.webhook_delivery.causal_order_version=0
             AND core.webhook_delivery.event_type<>'ping')
  RETURNING status INTO v_status;
  IF NOT FOUND THEN
    RETURN jsonb_build_object('accepted',false,'queued',false,
                              'reason','legacy_failed_quarantined','status','failed');
  END IF;
  RETURN jsonb_build_object('accepted', true, 'key', left(p_key,200), 'status', v_status, 'queued', v_status = 'queued');
END $$;
ALTER FUNCTION core.enqueue_webhook_delivery_with_authority(text,text,text,text,jsonb,int,int)
  OWNER TO veripsa_migrator;

-- Schema-first rolling bridge. The old worker's sanitizer predates causal fields such as Marketplace
-- effective_date, so its /6 enqueue must remain protocol 0 and can never make an unsafe legacy failed row replayable.
CREATE OR REPLACE FUNCTION core.enqueue_webhook_delivery_with_authority(
    p_key text,p_event_type text,p_account_key text,p_repo text,p_payload jsonb,p_max_pending int DEFAULT 5000)
RETURNS jsonb
    LANGUAGE sql SECURITY DEFINER SET search_path TO 'core','pg_catalog' AS $$
  SELECT core.enqueue_webhook_delivery_with_authority(
    p_key,p_event_type,p_account_key,p_repo,p_payload,p_max_pending,1)
$$;
ALTER FUNCTION core.enqueue_webhook_delivery_with_authority(text,text,text,text,jsonb,int)
  OWNER TO veripsa_migrator;

-- One canonical durable-lane classifier shared by pending(), claim(), and DLQ escalation. GitHub repository ids
-- are positive decimal integers; malformed/missing ids and event types whose scope cannot be proven are
-- deliberately account-wide barriers. The function returns only a bounded content-free stable id.
CREATE OR REPLACE FUNCTION core._webhook_delivery_repository_lane(
    p_event_type text,
    p_payload jsonb
) RETURNS text
    LANGUAGE sql IMMUTABLE PARALLEL SAFE
    SET search_path TO 'core','pg_catalog' AS $$
  SELECT CASE
    WHEN p_event_type IN ('repository','pull_request','push','check_suite','check_run','merge_group')
     AND (COALESCE(p_payload,'{}'::jsonb)->'repository'->>'id') ~ '^[1-9][0-9]{0,31}$'
    THEN left(COALESCE(p_payload,'{}'::jsonb)->'repository'->>'id',32)
    ELSE NULL
  END
$$;
ALTER FUNCTION core._webhook_delivery_repository_lane(text,jsonb) OWNER TO veripsa_migrator;
REVOKE EXECUTE ON FUNCTION core._webhook_delivery_repository_lane(text,jsonb)
  FROM PUBLIC, veripsa_app, veripsa_writer;

CREATE OR REPLACE FUNCTION core.pending_webhook_deliveries_with_authority(
    p_limit int DEFAULT 100,
    p_stale_seconds int DEFAULT 1800,
    p_max_attempts int DEFAULT 3
) RETURNS jsonb
    LANGUAGE plpgsql SECURITY DEFINER SET search_path TO 'core','pg_catalog' AS $$
DECLARE
  v_limit int := GREATEST(LEAST(COALESCE(p_limit,100),1000),1);
  v_stale_seconds int := GREATEST(COALESCE(p_stale_seconds,1800),1);
  v_max_attempts int := GREATEST(COALESCE(p_max_attempts,3),1);
  v_rows jsonb;
BEGIN
  -- A lowered attempt budget or an exhausted cross-generation wall window can
  -- strand queued rows outside the replay SELECT. Move them onto the visible
  -- failed causal-barrier path before selecting candidates.
  WITH exhausted AS MATERIALIZED (
    SELECT d.delivery_key
      FROM core.webhook_delivery d
     WHERE d.status='queued'
       AND (d.attempts>=v_max_attempts
            OR (d.retry_window_expires_at IS NOT NULL
                AND d.retry_window_expires_at<=clock_timestamp()))
     ORDER BY COALESCE(d.account_key,''),d.received_at,d.delivery_key
     FOR UPDATE
  )
  UPDATE core.webhook_delivery d
     SET status='failed', locked_at=NULL, owner_instance=NULL,
         not_before=NULL, updated_at=clock_timestamp(),
         last_error=CASE
           WHEN d.retry_window_expires_at IS NOT NULL
                AND d.retry_window_expires_at<=clock_timestamp()
             THEN 'retry_window_exhausted'
           ELSE COALESCE(NULLIF(d.last_error,''),'queued delivery exceeded durable attempts')
         END
    FROM exhausted x
   WHERE d.delivery_key=x.delivery_key;

  -- FINAL-ATTEMPT/WINDOW STALE REPAIR. A fresh processing owner is never
  -- touched merely because its absolute window crossed: terminal resolution
  -- may still be committing. Once the ordinary stale fence is crossed, either
  -- exhausted condition becomes a failed causal barrier rather than an
  -- invisible permanent processing row.
  WITH exhausted_processing AS MATERIALIZED (
    SELECT d.delivery_key
      FROM core.webhook_delivery d
     WHERE d.status='processing'
       AND (d.attempts>=v_max_attempts
            OR (d.retry_window_expires_at IS NOT NULL
                AND d.retry_window_expires_at<=clock_timestamp()))
       AND (d.locked_at IS NULL OR d.locked_at<now()-make_interval(secs=>v_stale_seconds))
     ORDER BY COALESCE(d.account_key,''),d.received_at,d.delivery_key
     FOR UPDATE
  )
  UPDATE core.webhook_delivery d
     SET status='failed', locked_at=NULL, owner_instance=NULL,
         not_before=NULL, updated_at=clock_timestamp(),
         last_error=CASE
           WHEN d.retry_window_expires_at IS NOT NULL
                AND d.retry_window_expires_at<=clock_timestamp()
             THEN 'retry_window_exhausted'
           ELSE 'stale processing exceeded durable attempts'
         END
    FROM exhausted_processing x
   WHERE d.delivery_key=x.delivery_key;

  WITH eligible AS MATERIALIZED (
    SELECT d.delivery_key,d.event_type,d.account_key,d.repo,d.payload,d.attempts,d.received_at,
           -- An account uninstall is an urgent terminal boundary once every earlier processing attempt is done.
           -- claim() atomically absorbs its earlier queued/failed rows, so recovery must offer it ahead of them.
           (d.account_key IS NOT NULL
            AND d.status='queued'
            AND d.event_type='installation'
            AND d.payload->>'action'='deleted'
            AND NULLIF(left(COALESCE(d.payload->'installation'->>'id',''),64),'') IS NOT NULL
            -- A different earlier installation generation is a possible replacement, not stale work. It must be
            -- offered first; otherwise a scan full of urgent deletes repeatedly fails claim behind its hidden B
            -- predecessors and starves every replacement forever.
            AND NOT EXISTS (
              SELECT 1 FROM core.webhook_delivery replacement
               WHERE COALESCE(replacement.account_key,'')=COALESCE(d.account_key,'')
                 AND replacement.status IN ('queued','failed')
                 AND NULLIF(left(COALESCE(replacement.payload->'installation'->>'id',''),64),'') IS NOT NULL
                 AND NULLIF(left(COALESCE(replacement.payload->'installation'->>'id',''),64),'')
                       <>NULLIF(left(COALESCE(d.payload->'installation'->>'id',''),64),'')
                 AND (replacement.received_at,replacement.delivery_key)<(d.received_at,d.delivery_key)
            )
            AND NOT EXISTS (
              SELECT 1 FROM core.webhook_delivery processing
               WHERE COALESCE(processing.account_key,'')=COALESCE(d.account_key,'')
                 AND processing.status='processing'
                 AND (processing.received_at,processing.delivery_key)<(d.received_at,d.delivery_key)
            )) AS urgent_uninstall
      FROM core.webhook_delivery d
     WHERE d.attempts<v_max_attempts
       AND (d.not_before IS NULL OR d.not_before<=now())
       AND (d.status='queued'
            OR (d.status='processing'
                AND (d.locked_at IS NULL
                     OR d.locked_at<now()-make_interval(secs=>v_stale_seconds))))
  ), causal AS MATERIALIZED (
    SELECT candidate.*
      FROM eligible candidate
     WHERE candidate.urgent_uninstall
        OR ((candidate.account_key IS NULL OR NOT EXISTS (
          SELECT 1
            FROM core.webhook_delivery earlier
           WHERE COALESCE(earlier.account_key,'')=COALESCE(candidate.account_key,'')
             AND earlier.delivery_key<>candidate.delivery_key
             AND (
               -- Live queued/processing work is a two-sided barrier: a wide target waits for every predecessor,
               -- while a repository target waits for its repository plus any earlier wide work.
               (earlier.status IN ('queued','processing') AND (
                 core._webhook_delivery_repository_lane(candidate.event_type,candidate.payload) IS NULL
                 OR core._webhook_delivery_repository_lane(earlier.event_type,earlier.payload) IS NULL
                 OR core._webhook_delivery_repository_lane(earlier.event_type,earlier.payload)
                      =core._webhook_delivery_repository_lane(candidate.event_type,candidate.payload)
               ))
               OR
               -- A failed/DLQ predecessor is directional. Lifecycle, billing, unknown, and malformed repository
               -- predecessors keep their original conservative authority, but a Marketplace target does not
               -- inherit an unrelated valid-repository failure. This is intentionally distinct from the live
               -- barrier above: finished failure has no in-flight state for that unrelated target to observe.
               (earlier.status='failed' AND earlier.causal_order_version>=1 AND (
                 candidate.event_type IN ('installation','installation_repositories')
                 OR earlier.event_type IN ('installation','installation_repositories')
                 OR earlier.event_type='marketplace_purchase'
                 OR (
                   earlier.event_type IN (
                     'repository','pull_request','push','check_suite','check_run','merge_group')
                   AND candidate.event_type IN (
                     'repository','pull_request','push','check_suite','check_run','merge_group')
                   AND (
                     core._webhook_delivery_repository_lane(earlier.event_type,earlier.payload) IS NULL
                     OR core._webhook_delivery_repository_lane(candidate.event_type,candidate.payload) IS NULL
                     OR core._webhook_delivery_repository_lane(earlier.event_type,earlier.payload)
                          =core._webhook_delivery_repository_lane(candidate.event_type,candidate.payload)
                   )
                 )
                 OR earlier.event_type NOT IN (
                   'installation','installation_repositories','marketplace_purchase','repository',
                   'pull_request','push','check_suite','check_run','merge_group')
                 OR candidate.event_type NOT IN (
                   'installation','installation_repositories','marketplace_purchase','repository',
                   'pull_request','push','check_suite','check_run','merge_group')
               ))
             )
             AND (earlier.received_at,earlier.delivery_key)<(candidate.received_at,candidate.delivery_key)
        ))
        AND NOT EXISTS (
          SELECT 1 FROM core.webhook_delivery earlier_repo
           WHERE core._webhook_delivery_repository_lane(candidate.event_type,candidate.payload) IS NOT NULL
             AND earlier_repo.delivery_key<>candidate.delivery_key
             AND core._webhook_delivery_repository_lane(earlier_repo.event_type,earlier_repo.payload)
                   =core._webhook_delivery_repository_lane(candidate.event_type,candidate.payload)
             AND (earlier_repo.status IN ('queued','processing')
                  OR (earlier_repo.status='failed' AND earlier_repo.causal_order_version>=1))
             AND (earlier_repo.received_at,earlier_repo.delivery_key)
                   <(candidate.received_at,candidate.delivery_key)
        ))
  ), ranked AS (
    SELECT candidate.*,
           row_number() OVER (
             PARTITION BY COALESCE(candidate.account_key,'')
             ORDER BY candidate.urgent_uninstall DESC,candidate.received_at,candidate.delivery_key
           ) AS account_rank
      FROM causal candidate
  )
  SELECT COALESCE(jsonb_agg(jsonb_build_object(
      'key', delivery_key, 'event_type', event_type, 'payload', payload,
      'delivery', delivery_key, 'attempts', attempts
    ) ORDER BY account_rank,received_at,delivery_key), '[]'::jsonb)
    INTO v_rows
    FROM (
      -- Interleave each account's effective head before any account's second item. Urgent uninstall ranks first
      -- only within its own account; unrelated accounts remain fair and independent.
      SELECT * FROM ranked
       ORDER BY account_rank,received_at,delivery_key
       LIMIT v_limit
    ) q;
  RETURN v_rows;
END
$$;
ALTER FUNCTION core.pending_webhook_deliveries_with_authority(int,int,int) OWNER TO veripsa_migrator;

CREATE OR REPLACE FUNCTION core.claim_webhook_delivery_with_authority(
    p_key text,
    p_stale_seconds int,
    p_max_attempts int,
    p_protocol int,
    p_owner_instance text,
    p_window_seconds int
) RETURNS jsonb
    LANGUAGE plpgsql SECURITY DEFINER SET search_path TO 'core','pg_catalog' AS $$
DECLARE
  v_row core.webhook_delivery%ROWTYPE;
  v_head core.webhook_delivery%ROWTYPE;
  v_repository_head core.webhook_delivery%ROWTYPE;
  v_account_key text;
  v_repository_id text;
  v_has_blocker boolean := false;
  v_is_account_uninstall boolean := false;
  v_target_installation_id text;
  v_supersede_eligible boolean := false;
  v_target_key text;
  v_target_received_at timestamptz;
  v_claimed_at timestamptz;
  v_stale_seconds int := GREATEST(COALESCE(p_stale_seconds,1800),1);
  v_max_attempts int := GREATEST(COALESCE(p_max_attempts,3),1);
  v_window_seconds int := GREATEST(LEAST(COALESCE(p_window_seconds,120),86400),1);
  v_legacy_owner_budget_seconds int;
BEGIN
  IF COALESCE(p_protocol,0) NOT IN (1,2,3) THEN
    RAISE EXCEPTION 'unsupported durable webhook claim protocol: %', p_protocol
      USING ERRCODE='22023';
  END IF;
  IF p_owner_instance IS NOT NULL
     AND (length(p_owner_instance) < 1 OR length(p_owner_instance) > 64) THEN
    RAISE EXCEPTION 'invalid durable webhook claim owner_instance'
      USING ERRCODE='22023';
  END IF;
  -- Protocol 3 consumes retry_window_remaining_ms and narrows its monotonic
  -- EventBudget. Protocol 1/2 callers cannot consume that field, and their
  -- supported runtime budget may be configured anywhere from 1 to 900
  -- seconds. The /4 and /3 ABIs carry no budget proof, so schema-first rollout
  -- must reject even their first fresh claim; the previous image leaves the
  -- durable row queued until protocol 3 starts. The intermediate /5 ABI embeds
  -- its total budget plus ambiguity grace in the exact owner nonce. Permit
  -- only that authenticated shape when the claimed allowance fits wholly
  -- inside this DB window, and only for a fresh epoch. This prevents a live
  -- old image configured for 200/900 seconds from overrunning the 120-second
  -- durable boundary before the new image is promoted.
  IF p_protocol<3
     AND p_owner_instance ~
       '^wk-[0-9a-f]{32}\.[0-9]{4}\.[0-9a-f]{22}$' THEN
    v_legacy_owner_budget_seconds :=
      split_part(p_owner_instance,'.',2)::int;
  END IF;
  -- The account key is immutable after first admission. Share enqueue's transaction lock so a claim cannot observe
  -- a newer row while an older same-account enqueue has returned but remains uncommitted/invisible.
  SELECT * INTO v_row
    FROM core.webhook_delivery d
   WHERE d.delivery_key=left(COALESCE(p_key,''),200);
  IF NOT FOUND THEN
    RETURN jsonb_build_object('claimed',false,'reason','missing','status','missing');
  END IF;
  v_account_key := v_row.account_key;
  v_target_key := v_row.delivery_key;
  v_target_received_at := v_row.received_at;
  v_repository_id := core._webhook_delivery_repository_lane(v_row.event_type,v_row.payload);
  v_is_account_uninstall := v_account_key IS NOT NULL
                            AND v_row.event_type='installation'
                            AND v_row.payload->>'action'='deleted';
  v_target_installation_id := CASE WHEN v_is_account_uninstall
    THEN NULLIF(left(COALESCE(v_row.payload->'installation'->>'id',''),64),'') END;
  IF v_account_key IS NOT NULL THEN
    PERFORM pg_advisory_xact_lock(
      hashtext('core.webhook_delivery.account'),hashtext(v_account_key));
  END IF;
  IF v_repository_id IS NOT NULL THEN
    PERFORM pg_advisory_xact_lock(
      hashtext('core.webhook_delivery.repository'),hashtext(v_repository_id));
  END IF;

  -- Normalize exhausted queued rows and only STALE exhausted processing rows
  -- before causal evaluation. A fresh processing generation is never stolen
  -- merely because its window crossed while it terminalizes.
  WITH exhausted AS MATERIALIZED (
    SELECT d.delivery_key
      FROM core.webhook_delivery d
     WHERE (
         (d.status='queued'
          AND (d.attempts>=v_max_attempts
               OR (d.retry_window_expires_at IS NOT NULL
                   AND d.retry_window_expires_at<=clock_timestamp())))
         OR
         (d.status='processing'
          AND (d.locked_at IS NULL
               OR d.locked_at<clock_timestamp()-make_interval(secs=>v_stale_seconds))
          AND (d.attempts>=v_max_attempts
               OR (d.retry_window_expires_at IS NOT NULL
                   AND d.retry_window_expires_at<=clock_timestamp())))
       )
       AND ((v_account_key IS NOT NULL AND COALESCE(d.account_key,'')=v_account_key)
            OR (v_account_key IS NULL AND d.delivery_key=left(COALESCE(p_key,''),200)))
     ORDER BY COALESCE(d.account_key,''),d.received_at,d.delivery_key
     FOR UPDATE
  )
  UPDATE core.webhook_delivery d
     SET status='failed', locked_at=NULL, owner_instance=NULL,
         not_before=NULL, updated_at=clock_timestamp(),
         last_error=CASE
           WHEN d.retry_window_expires_at IS NOT NULL
                AND d.retry_window_expires_at<=clock_timestamp()
             THEN 'retry_window_exhausted'
           WHEN d.status='processing'
             THEN 'stale processing exceeded durable attempts'
           ELSE COALESCE(NULLIF(d.last_error,''),'queued delivery exceeded durable attempts')
         END
   FROM exhausted x
   WHERE d.delivery_key=x.delivery_key;

  -- Supersession is a processing decision, not mere observation. A scheduled/not-due or exhausted uninstall must
  -- not erase older work before it is itself claimable; a stale processing uninstall may be honestly reclaimed.
  SELECT * INTO v_row
    FROM core.webhook_delivery d
   WHERE d.delivery_key=v_target_key;
  IF FOUND THEN
    v_claimed_at := clock_timestamp();
    v_supersede_eligible := v_is_account_uninstall AND (
      (v_row.status='queued'
       AND v_row.attempts<v_max_attempts
       AND (v_row.not_before IS NULL OR v_row.not_before<=v_claimed_at))
      OR
      (v_row.status='processing'
       AND v_row.attempts<v_max_attempts
       AND (v_row.locked_at IS NULL
            OR v_row.locked_at<v_claimed_at-make_interval(secs=>v_stale_seconds)))
    );
  END IF;

  -- Account uninstall is a terminal supersession boundary for its own installation generation. Earlier
  -- queued/failed same-generation work has not begun and may be atomically absorbed; a different generation is a
  -- possible replacement and stays executable. Earlier processing remains a hard barrier. Keep supersession and
  -- both causal-head locks in one exception subtransaction: a NOWAIT miss must roll every prospective absorption
  -- back before returning an attempt-neutral deferral.
  BEGIN
  IF v_supersede_eligible THEN
    WITH superseded AS MATERIALIZED (
      SELECT d.delivery_key
        FROM core.webhook_delivery d
       WHERE COALESCE(d.account_key,'')=v_account_key
         AND d.status IN ('queued','failed')
         AND (d.received_at,d.delivery_key)<(v_target_received_at,v_target_key)
         -- Webhook receive order is not installation-generation order. A delayed uninstall A may be offered
         -- urgently while replacement B's earlier activation/work is still queued; absorbing B would permanently
         -- lose the only event able to publish the new generation. Supersede only A (or generation-unknown) work.
         AND v_target_installation_id IS NOT NULL
         AND (NULLIF(left(COALESCE(d.payload->'installation'->>'id',''),64),'') IS NULL
              OR NULLIF(left(COALESCE(d.payload->'installation'->>'id',''),64),'')
                   =v_target_installation_id)
       ORDER BY COALESCE(d.account_key,''),d.received_at,d.delivery_key
       FOR UPDATE
    )
    UPDATE core.webhook_delivery d
       SET status='done', payload='{}'::jsonb, repo=NULL, locked_at=NULL, not_before=NULL,
           retry_window_expires_at=NULL,
           done_at=clock_timestamp(), updated_at=clock_timestamp(), last_error=NULL
      FROM superseded s
     WHERE d.delivery_key=s.delivery_key;
  END IF;

  -- Lock the durable unfinished HEAD, never target-before-account: enqueue holds account→row on ON CONFLICT, so
  -- advisory→head preserves one lock order and cannot deadlock it. A finishing predecessor is ordinary causal
  -- contention, not a reason to spend this claim's lock_timeout or attempt budget: defer immediately and let durable
  -- recovery retry after the finisher commits. NOWAIT is deliberate; SKIP LOCKED could overtake the true FIFO head.
  IF v_account_key IS NOT NULL THEN
    BEGIN
    SELECT * INTO v_head
     FROM core.webhook_delivery d
     WHERE COALESCE(d.account_key,'')=v_account_key
       AND (
         -- Live work uses the two-sided repository/account-wide barrier.
         (d.status IN ('queued','processing') AND (
           v_repository_id IS NULL
           OR core._webhook_delivery_repository_lane(d.event_type,d.payload) IS NULL
           OR core._webhook_delivery_repository_lane(d.event_type,d.payload)=v_repository_id
         ))
         OR
         -- Failed work preserves the directional predecessor contract used by pending(): most importantly, a
         -- Marketplace target must not inherit a failed row from an unrelated valid repository.
         (d.status='failed' AND d.causal_order_version>=1 AND (
           v_row.event_type IN ('installation','installation_repositories')
           OR d.event_type IN ('installation','installation_repositories')
           OR d.event_type='marketplace_purchase'
           OR (
             d.event_type IN (
               'repository','pull_request','push','check_suite','check_run','merge_group')
             AND v_row.event_type IN (
               'repository','pull_request','push','check_suite','check_run','merge_group')
             AND (
               core._webhook_delivery_repository_lane(d.event_type,d.payload) IS NULL
               OR v_repository_id IS NULL
               OR core._webhook_delivery_repository_lane(d.event_type,d.payload)=v_repository_id
             )
           )
           OR d.event_type NOT IN (
             'installation','installation_repositories','marketplace_purchase','repository',
             'pull_request','push','check_suite','check_run','merge_group')
           OR v_row.event_type NOT IN (
             'installation','installation_repositories','marketplace_purchase','repository',
             'pull_request','push','check_suite','check_run','merge_group')
         ))
       )
     ORDER BY d.received_at,d.delivery_key
     LIMIT 1
     FOR UPDATE NOWAIT;
    EXCEPTION
      WHEN lock_not_available THEN
        RAISE EXCEPTION 'durable account causal head is locked'
          USING ERRCODE='VP001';
    END;
    SELECT * INTO v_row
      FROM core.webhook_delivery d
     WHERE d.delivery_key=left(COALESCE(p_key,''),200);
  ELSE
    -- Honest-unknown accounts are deliberately independent, not one global lane; only serialize the immutable key.
    SELECT * INTO v_row
      FROM core.webhook_delivery d
     WHERE d.delivery_key=left(COALESCE(p_key,''),200)
     FOR UPDATE;
  END IF;
  IF NOT FOUND THEN
    RETURN jsonb_build_object('claimed',false,'reason','missing','status','missing');
  END IF;
  IF v_repository_id IS NOT NULL THEN
    BEGIN
    SELECT * INTO v_repository_head
      FROM core.webhook_delivery d
     WHERE core._webhook_delivery_repository_lane(d.event_type,d.payload)=v_repository_id
       AND (d.status IN ('queued','processing')
            OR (d.status='failed' AND d.causal_order_version>=1))
     ORDER BY d.received_at,d.delivery_key
     LIMIT 1
     FOR UPDATE NOWAIT;
    EXCEPTION
      WHEN lock_not_available THEN
        RAISE EXCEPTION 'durable repository causal head is locked'
          USING ERRCODE='VP001';
    END;
  END IF;
  EXCEPTION
    WHEN SQLSTATE 'VP001' THEN
      RETURN jsonb_build_object(
        'claimed',false,'reason','blocked_by_earlier','status',v_row.status);
  END;
  v_claimed_at := clock_timestamp();

  -- Re-evaluate the absolute window after causal locking. A rolling /5 or /4
  -- caller cannot consume the /6 remaining-ms field, so it must not start a
  -- fresh 90-second handler after the durable epoch has already expired.
  IF v_row.retry_window_expires_at IS NOT NULL
     AND v_row.retry_window_expires_at<=v_claimed_at THEN
    IF v_row.status='queued'
       OR (v_row.status='processing'
           AND (v_row.locked_at IS NULL
                OR v_row.locked_at<
                   v_claimed_at-make_interval(secs=>v_stale_seconds))) THEN
      UPDATE core.webhook_delivery
         SET status='failed', locked_at=NULL, owner_instance=NULL,
             not_before=NULL, updated_at=v_claimed_at,
             last_error='retry_window_exhausted'
       WHERE delivery_key=v_row.delivery_key
         AND retry_window_expires_at IS NOT NULL
         AND retry_window_expires_at<=v_claimed_at
         AND (
           status='queued'
           OR (status='processing'
               AND (locked_at IS NULL
                    OR locked_at<
                       v_claimed_at-make_interval(secs=>v_stale_seconds)))
         );
      RETURN jsonb_build_object(
        'claimed',false,'reason','retry_window_exhausted','status','failed');
    END IF;
  END IF;

  -- DURABLE CAUSAL ORDER. Same-repository work is FIFO; independent valid repository ids may run concurrently.
  -- Lifecycle, billing, unknown event types, and malformed/missing ids are conservative account-wide barriers.
  -- Protocol-1 failures keep the same lane relationship; protocol-0 failures remain quarantined/inert.
  v_has_blocker := v_row.status IN ('queued','processing')
                   AND ((v_row.account_key IS NOT NULL
                         AND v_head.delivery_key IS NOT NULL
                         AND v_head.delivery_key<>v_row.delivery_key
                         AND (v_head.received_at,v_head.delivery_key)
                               <(v_row.received_at,v_row.delivery_key))
                        OR (v_repository_id IS NOT NULL
                            AND v_repository_head.delivery_key IS NOT NULL
                            AND v_repository_head.delivery_key<>v_row.delivery_key
                            AND (v_repository_head.received_at,v_repository_head.delivery_key)
                                  <(v_row.received_at,v_row.delivery_key)));

  IF v_has_blocker THEN
    RETURN jsonb_build_object('claimed',false,'reason','blocked_by_earlier','status',v_row.status);
  ELSIF v_row.status='done' THEN
    RETURN jsonb_build_object('claimed',false,'reason','already_finished','status',v_row.status);
  ELSIF v_row.status='failed' THEN
    RETURN jsonb_build_object('claimed',false,'reason','failed','status',v_row.status);
  ELSIF v_row.status='processing'
        AND v_row.locked_at IS NOT NULL
        AND v_row.locked_at>=v_claimed_at-make_interval(secs=>v_stale_seconds) THEN
    RETURN jsonb_build_object('claimed',false,'reason','already_owned','status',v_row.status);
  ELSIF v_row.attempts>=v_max_attempts THEN
    RETURN jsonb_build_object('claimed',false,'reason','attempts_exhausted','status',v_row.status);
  ELSIF v_row.not_before IS NOT NULL AND v_row.not_before>v_claimed_at THEN
    RETURN jsonb_build_object('claimed',false,'reason','not_due','status',v_row.status);
  ELSIF p_protocol<3
        AND (
          v_row.retry_window_expires_at IS NOT NULL
          OR v_legacy_owner_budget_seconds IS NULL
          OR v_legacy_owner_budget_seconds>v_window_seconds
        ) THEN
    RETURN jsonb_build_object(
      'claimed',false,
      'reason',CASE
        WHEN v_row.retry_window_expires_at IS NOT NULL
          THEN 'legacy_retry_window_unsupported'
        ELSE 'legacy_budget_unproven'
      END,
      'status',v_row.status,
      'retry_window_remaining_ms',
      CASE WHEN v_row.retry_window_expires_at IS NULL THEN NULL
        ELSE GREATEST(0,floor(EXTRACT(EPOCH FROM
          (v_row.retry_window_expires_at-v_claimed_at))*1000)::bigint)
      END);
  ELSIF p_protocol=1
        AND v_row.event_type='marketplace_purchase'
        AND v_row.causal_order_version=0 THEN
    -- The rolling-old sanitizer omitted effective_date, which is the Marketplace high-water mark. Executing that
    -- legacy payload can overwrite newer billing state, so an old worker must fail closed without taking a lease.
    RETURN jsonb_build_object('claimed',false,'reason','legacy_payload_incompatible','status',v_row.status);
  ELSIF v_row.status<>'queued'
        AND NOT (v_row.status='processing'
                 AND (v_row.locked_at IS NULL
                      OR v_row.locked_at<v_claimed_at-make_interval(secs=>v_stale_seconds))) THEN
    RETURN jsonb_build_object('claimed',false,'reason','not_claimable','status',v_row.status);
  END IF;

  UPDATE core.webhook_delivery AS target
     SET status='processing', attempts=target.attempts+1,
         lease_generation=target.lease_generation+1,
         locked_at=v_claimed_at, updated_at=v_claimed_at,
         last_error=NULL, not_before=NULL,
         retry_window_expires_at=COALESCE(
           target.retry_window_expires_at,
           v_claimed_at+make_interval(secs=>v_window_seconds)),
         -- This is deliberately part of the claim UPDATE. If COMMIT succeeds but the response disappears, the
         -- caller can identify exactly this claim generation by its pre-generated unique owner reference.
         owner_instance=p_owner_instance
   WHERE target.delivery_key=v_row.delivery_key
   RETURNING target.* INTO v_row;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'locked webhook delivery disappeared during claim' USING ERRCODE='40001';
  END IF;
  RETURN jsonb_build_object('claimed', true, 'key', v_row.delivery_key, 'event_type', v_row.event_type,
                            'payload', v_row.payload, 'attempts', v_row.attempts,
                            'lease_generation', v_row.lease_generation,
                            'retry_window_remaining_ms',
                            GREATEST(0,floor(EXTRACT(EPOCH FROM
                              (v_row.retry_window_expires_at-clock_timestamp()))*1000)::bigint));
END $$;
ALTER FUNCTION core.claim_webhook_delivery_with_authority(text,int,int,int,text,int)
  OWNER TO veripsa_migrator;

-- Rolling owner-stamping worker shim. Only an exact owner nonce proving a
-- total allowance <=120 seconds may take the first fresh claim; every replay
-- fails closed for protocol 3. The overload remains so an intermediate image
-- queues safely during schema-first publication instead of raising 42883.
CREATE OR REPLACE FUNCTION core.claim_webhook_delivery_with_authority(
    p_key text,
    p_stale_seconds int,
    p_max_attempts int,
    p_protocol int,
    p_owner_instance text
) RETURNS jsonb
    LANGUAGE sql SECURITY DEFINER SET search_path TO 'core','pg_catalog' AS $$
  SELECT core.claim_webhook_delivery_with_authority(
    p_key,p_stale_seconds,p_max_attempts,p_protocol,p_owner_instance,120)
$$;
ALTER FUNCTION core.claim_webhook_delivery_with_authority(text,int,int,int,text)
  OWNER TO veripsa_migrator;

-- Do not fence an explicitly recognized /4 worker ABI while it serves as a
-- rolling predecessor. The previous image remains live while every later
-- schema module runs; replacing that function here could strand its queue if a
-- later module or the new image failed. The public source distribution uses a
-- synthetic predecessor fixture plus the current fail-closed wrapper. Any
-- other definition fails without mutation.
WITH target AS (
  SELECT p.*, l.lanname, r.rolname
    FROM pg_proc p
    JOIN pg_language l ON l.oid=p.prolang
    JOIN pg_roles r ON r.oid=p.proowner
   WHERE p.oid=to_regprocedure(
           'core.claim_webhook_delivery_with_authority(text,integer,integer,integer)'
         )
), classified AS (
  SELECT
    NOT EXISTS (SELECT 1 FROM target) AS missing,
    COALESCE((
      SELECT md5(prosrc)='8bdf79d0259b27c37f48a9f52c0416b6'
             AND lanname='plpgsql'
             AND prosecdef
             AND proconfig=ARRAY['search_path=core, pg_catalog']::text[]
             AND rolname='veripsa_migrator'
             AND prorettype='jsonb'::regtype
             AND NOT proretset
             AND NOT proisstrict
             AND provolatile='v'
             AND proparallel='u'
             AND NOT proleakproof
             AND prokind='f'
             AND proargmodes IS NULL
             AND proallargtypes IS NULL
             AND provariadic=0
             AND pronargdefaults=0
             AND proargnames=ARRAY[
                   'p_key','p_stale_seconds','p_max_attempts','p_protocol'
                 ]::text[]
             AND procost=100
             AND prorows=0
             AND prosupport=0
             AND probin IS NULL
        FROM target
    ),false) AS exact_predecessor,
    COALESCE((
      SELECT md5(prosrc)='877a1f799a016bcd42a7a57b61750c25'
             AND lanname='sql'
             AND prosecdef
             AND proconfig=ARRAY['search_path=core, pg_catalog']::text[]
             AND rolname='veripsa_migrator'
             AND prorettype='jsonb'::regtype
             AND NOT proretset
             AND NOT proisstrict
             AND provolatile='v'
             AND proparallel='u'
             AND NOT proleakproof
             AND prokind='f'
             AND proargmodes IS NULL
             AND proallargtypes IS NULL
             AND provariadic=0
             AND pronargdefaults=0
             AND proargnames=ARRAY[
                   'p_key','p_stale_seconds','p_max_attempts','p_protocol'
                 ]::text[]
             AND procost=100
             AND prorows=0
             AND prosupport=0
             AND probin IS NULL
        FROM target
    ),false) AS exact_safe
), decision AS (
  SELECT exact_predecessor,
         NOT missing AND NOT exact_predecessor AND NOT exact_safe AS unknown
    FROM classified
)
SELECT exact_predecessor AS veripsa_preserve_exact_predecessor_claim_v2,
       unknown AS veripsa_unknown_predecessor_claim_v2
  FROM decision
\gset
\if :veripsa_unknown_predecessor_claim_v2
-- Deliberately fail with a side-effect-free scalar cast. A DO block is
-- forbidden inside the long publication transaction by the hot-deploy guard,
-- and this branch must not acquire a relation lock before refusing the ABI.
SELECT
  'unsupported existing durable webhook /4 claim ABI; refusing non-atomic replacement'
  ::integer;
\elif :veripsa_preserve_exact_predecessor_claim_v2
\else
CREATE OR REPLACE FUNCTION core.claim_webhook_delivery_with_authority(
    p_key text,
    p_stale_seconds int,
    p_max_attempts int,
    p_protocol int
) RETURNS jsonb
    LANGUAGE sql SECURITY DEFINER SET search_path TO 'core','pg_catalog' AS $$
  SELECT core.claim_webhook_delivery_with_authority(
    p_key,p_stale_seconds,p_max_attempts,p_protocol,NULL::text)
$$;
\endif
ALTER FUNCTION core.claim_webhook_delivery_with_authority(text,int,int,int) OWNER TO veripsa_migrator;

-- /3 is semantically identical across the audited predecessor and current
-- wrapper, and the predecessor runtime calls /4. Re-publish the safe wrapper
-- so an unknown or interrupted /3 body is never carried forward.
CREATE OR REPLACE FUNCTION core.claim_webhook_delivery_with_authority(
    p_key text,
    p_stale_seconds int DEFAULT 1800,
    p_max_attempts int DEFAULT 3
) RETURNS jsonb
    LANGUAGE plpgsql SECURITY DEFINER SET search_path TO 'core','pg_catalog' AS $$
DECLARE v_result jsonb; v_reason text;
BEGIN
  v_result := core.claim_webhook_delivery_with_authority(
    p_key,p_stale_seconds,p_max_attempts,1);
  IF COALESCE((v_result->>'claimed')::boolean,false) THEN
    IF COALESCE((v_result->>'lease_generation')::bigint,0) = 1 THEN
      RETURN v_result;
    END IF;
    RAISE EXCEPTION 'durable webhook claim lease unsupported for legacy caller: %',
      COALESCE(v_result->>'lease_generation','missing')
      USING ERRCODE='55000', DETAIL=left(v_result::text,500);
  END IF;
  v_reason := COALESCE(NULLIF(v_result->>'reason',''),'unclassified');
  IF v_reason = 'already_finished' THEN
    RETURN v_result;
  END IF;
  RAISE EXCEPTION 'durable webhook claim rejected for legacy caller: %', v_reason
    USING ERRCODE='55000', DETAIL=left(v_result::text,500);
END $$;
ALTER FUNCTION core.claim_webhook_delivery_with_authority(text,int,int) OWNER TO veripsa_migrator;

-- BEGIN repository-offboarding rollout switch. The module transaction already open above also keeps this private
-- consistency scheduler, compatibility shim, deletion guard, and the lease protocol on one publication boundary.

-- Internal scheduled retry used by repository lifecycle compatibility. Unlike a processor failure, an intentional
-- consistency defer must not consume the durable attempt budget or wait for the generic stale-processing timeout.
-- The row becomes queued but is invisible to pending/claim until not_before. Owner-only: lifecycle SECURITY DEFINER
-- functions call it; neither the App role nor a buyer gets a raw queue-state mutator.
CREATE OR REPLACE FUNCTION core._defer_webhook_delivery_with_authority(
    p_key text, p_not_before timestamptz, p_reason text)
RETURNS boolean
    LANGUAGE plpgsql SECURITY DEFINER SET search_path TO 'core','pg_catalog' AS $$
BEGIN
  UPDATE core.webhook_delivery
     SET status='queued',
         attempts=GREATEST(attempts-1,0),
         locked_at=NULL,
         owner_instance=NULL,
         not_before=COALESCE(p_not_before,clock_timestamp()),
         retry_window_expires_at=NULL,
         updated_at=now(),
         last_error=left(COALESCE(p_reason,'scheduled retry'),300)
   WHERE delivery_key=left(COALESCE(p_key,''),200)
     AND status='processing';
  RETURN FOUND;
END $$;
ALTER FUNCTION core._defer_webhook_delivery_with_authority(text,timestamptz,text)
  OWNER TO veripsa_migrator;
REVOKE EXECUTE ON FUNCTION core._defer_webhook_delivery_with_authority(text,timestamptz,text)
  FROM PUBLIC, veripsa_app, veripsa_writer;

-- App-visible consistency defer with the same exact-lease discipline as finish/release.  A worker which lost its
-- processing lease must not requeue the successor's attempt.  The owner-only /3 helper remains for lifecycle SQL
-- which executes inside the same statement; runtime callers must use this generation-bearing boundary.
CREATE OR REPLACE FUNCTION core.defer_webhook_delivery_with_authority(
    p_key text, p_not_before timestamptz, p_reason text, p_lease_generation bigint)
RETURNS boolean
    LANGUAGE plpgsql SECURITY DEFINER SET search_path TO 'core','pg_catalog' AS $$
BEGIN
  IF p_lease_generation IS NULL OR p_lease_generation<1 THEN
    RETURN false;
  END IF;
  UPDATE core.webhook_delivery
     SET status='queued',
         attempts=GREATEST(attempts-1,0),
         locked_at=NULL,
         owner_instance=NULL,
         not_before=COALESCE(p_not_before,clock_timestamp()),
         retry_window_expires_at=NULL,
         updated_at=now(),
         last_error=left(COALESCE(p_reason,'scheduled retry'),300)
   WHERE delivery_key=left(COALESCE(p_key,''),200)
     AND status='processing'
     AND lease_generation=p_lease_generation;
  RETURN FOUND;
END $$;
ALTER FUNCTION core.defer_webhook_delivery_with_authority(text,timestamptz,text,bigint)
  OWNER TO veripsa_migrator;
REVOKE EXECUTE ON FUNCTION core.defer_webhook_delivery_with_authority(text,timestamptz,text,bigint)
  FROM PUBLIC,veripsa_writer;
GRANT EXECUTE ON FUNCTION core.defer_webhook_delivery_with_authority(text,timestamptz,text,bigint)
  TO veripsa_app;

-- GENERAL DEFER COMMIT-ACK RESOLUTION.  A separate store connection may lose
-- its response on either side of COMMIT.  Retrying the old boolean mutator
-- cannot distinguish "the first call committed" from "this lease was replaced"
-- and used to leave the exact processing generation pinned until the 1800s
-- stale timeout.  Lock the row and make both observations explicit:
--   processing + exact generation => perform the existing exact-lease defer
--   queued + exact generation + exact schedule/reason => prior commit proven
--   another generation/state/arguments => ownership_lost
--   absent key => missing
-- The requested timestamp is stored byte-for-byte (Postgres timestamptz
-- equality), including a past timestamp which simply means "due now".  That
-- makes the proof repeatable after the requested time itself has passed.
CREATE OR REPLACE FUNCTION core.resolve_webhook_delivery_defer_with_authority(
    p_key text,
    p_lease_generation bigint,
    p_not_before timestamptz,
    p_reason text)
RETURNS text
    LANGUAGE plpgsql SECURITY DEFINER SET search_path TO 'core','pg_catalog' AS $$
DECLARE
  v_status text;
  v_generation bigint;
  v_locked_at timestamptz;
  v_owner_instance text;
  v_stored_not_before timestamptz;
  v_stored_reason text;
  v_retry_window_expires_at timestamptz;
  v_reason text := left(COALESCE(p_reason,'consistency deferral'),300);
BEGIN
  IF p_lease_generation IS NULL OR p_lease_generation<1 THEN
    RETURN 'ownership_lost';
  END IF;
  IF p_not_before IS NULL THEN
    RAISE EXCEPTION 'delivery defer resolution requires not_before'
      USING ERRCODE='22023';
  END IF;

  SELECT d.status,d.lease_generation,d.locked_at,d.owner_instance,
         d.not_before,d.last_error,d.retry_window_expires_at
    INTO v_status,v_generation,v_locked_at,v_owner_instance,
         v_stored_not_before,v_stored_reason,v_retry_window_expires_at
    FROM core.webhook_delivery d
   WHERE d.delivery_key=left(COALESCE(p_key,''),200)
   FOR UPDATE;
  IF NOT FOUND THEN
    RETURN 'missing';
  END IF;
  IF v_generation IS DISTINCT FROM p_lease_generation THEN
    RETURN 'ownership_lost';
  END IF;
  IF v_status='processing' THEN
    IF core.defer_webhook_delivery_with_authority(
        p_key,p_not_before,v_reason,p_lease_generation) THEN
      RETURN 'deferred';
    END IF;
    RETURN 'ownership_lost';
  END IF;
  IF v_status='queued'
     AND v_locked_at IS NULL
     AND v_owner_instance IS NULL
     AND v_stored_not_before IS NOT DISTINCT FROM p_not_before
     AND v_retry_window_expires_at IS NULL
     AND v_stored_reason IS NOT DISTINCT FROM v_reason THEN
    RETURN 'deferred';
  END IF;
  RETURN 'ownership_lost';
END $$;
ALTER FUNCTION core.resolve_webhook_delivery_defer_with_authority(
  text,bigint,timestamptz,text) OWNER TO veripsa_migrator;
REVOKE EXECUTE ON FUNCTION core.resolve_webhook_delivery_defer_with_authority(
  text,bigint,timestamptz,text) FROM PUBLIC,veripsa_writer;
GRANT EXECUTE ON FUNCTION core.resolve_webhook_delivery_defer_with_authority(
  text,bigint,timestamptz,text) TO veripsa_app;

CREATE OR REPLACE FUNCTION core.purge_repo_with_authority(p_repo text)
    RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path TO 'core','pg_catalog' AS $$
DECLARE
  v_account text; v_repo text; v_candidate_count int;
  v_delivery_key text; v_repository_id text; v_reason text;
  v_delivery_received_at timestamptz; v_retry_at timestamptz;
BEGIN
  v_repo := left(NULLIF(btrim(COALESCE(p_repo,'')),''),512);
  IF v_repo IS NULL THEN RAISE EXCEPTION 'repository purge needs a repo' USING ERRCODE='23514'; END IF;
  SELECT account INTO v_account
    FROM core.establish_session_write_context() AS c(agent,account);

  WITH candidates AS (
    SELECT d.delivery_key,
           NULLIF(btrim(d.payload->'repository'->>'id'),'') AS repository_id,
           'repository_deleted'::text AS reason,
           d.received_at
      FROM core.webhook_delivery d
     WHERE d.status='processing'
       AND d.account_key IN (
         v_account,
         CASE WHEN v_account LIKE 'ACCT-GH-%' THEN substr(v_account,9) ELSE v_account END)
       AND d.event_type='repository' AND d.payload->>'action'='deleted'
       AND d.repo=v_repo AND d.payload->'repository'->>'full_name'=v_repo
    UNION ALL
    SELECT d.delivery_key,
           NULLIF(btrim(removed.repo->>'id'),'') AS repository_id,
           'installation_removed'::text AS reason,
           d.received_at
      FROM core.webhook_delivery d
      CROSS JOIN LATERAL jsonb_array_elements(
        CASE WHEN jsonb_typeof(d.payload->'repositories_removed')='array'
             THEN d.payload->'repositories_removed' ELSE '[]'::jsonb END
      ) AS removed(repo)
     WHERE d.status='processing'
       AND d.account_key IN (
         v_account,
         CASE WHEN v_account LIKE 'ACCT-GH-%' THEN substr(v_account,9) ELSE v_account END)
       AND d.event_type='installation_repositories' AND d.payload->>'action'='removed'
       AND removed.repo->>'full_name'=v_repo
  )
  SELECT count(*)::int,min(delivery_key),min(repository_id),min(reason),min(received_at)
    INTO v_candidate_count,v_delivery_key,v_repository_id,v_reason,v_delivery_received_at
    FROM candidates;
  IF v_candidate_count<>1 THEN
    RAISE EXCEPTION 'repository purge needs exactly one durable deletion authority'
      USING ERRCODE='42501';
  END IF;
  IF v_repository_id IS NULL THEN
    -- The old production sanitizer omitted repository.id. The old worker cannot perform the installation-token
    -- point read needed to distinguish a removed object from a visible same-name replacement. Return the attempt
    -- to the durable queue without spending budget; the new worker resolves it at received_at+5m (or immediately
    -- when that consistency window already elapsed). finish() below cannot mark a queued row done.
    v_retry_at := GREATEST(v_delivery_received_at+interval '5 minutes',clock_timestamp());
    IF NOT core._defer_webhook_delivery_with_authority(
        v_delivery_key,v_retry_at,'legacy repository identity resolution deferred') THEN
      RAISE EXCEPTION 'legacy repository purge could not schedule current GitHub identity resolution'
        USING ERRCODE='55000';
    END IF;
    RETURN jsonb_build_object('ok',true,'repo',v_repo,'repository_id',NULL,
                              'deferred',true,'defer_reason','legacy_identity_requires_new_worker',
                              'not_before',v_retry_at);
  END IF;
  -- The stable-id path is late-bound intentionally. On a fresh database /4 is created by module 35 later in the
  -- same schema apply. During the brief module gap a call fails closed; the finish guard keeps its row processing
  -- so the new worker can retry after module 35 is installed.
  RETURN core.offboard_repository_with_authority(
    v_repo,v_repository_id,v_reason,v_delivery_key);
END $$;
ALTER FUNCTION core.purge_repo_with_authority(text) OWNER TO veripsa_migrator;
REVOKE EXECUTE ON FUNCTION core.purge_repo_with_authority(text)
  FROM PUBLIC, veripsa_writer;
GRANT EXECUTE ON FUNCTION core.purge_repo_with_authority(text) TO veripsa_app;

-- Canonical durable fan-out plan. The delivery payload is already the minimized operational envelope; these
-- markers add only bounded repository identity/coordinate tokens needed to avoid repeating expensive per-repo
-- work after a later sibling fails. The caller must supply an explicit canonical key for every entry, while this
-- owner-private helper independently derives and verifies it:
--   stable positive repository id => id:<id>
--   genuinely id-less repository  => name:<full_name>
-- Sorting makes equivalent input order byte-identical. Duplicate keys, duplicate coordinates, conflicting
-- identities, ambiguous ids, extra fields, and unbounded/invalid coordinates fail before the payload is mutated.
CREATE OR REPLACE FUNCTION core._canonical_webhook_delivery_fanout_plan(p_plan jsonb)
RETURNS jsonb
    LANGUAGE plpgsql IMMUTABLE SET search_path TO 'core','pg_catalog' AS $$
DECLARE
  v_count int;
  v_item jsonb;
  v_full_name text;
  v_repository_id text;
  v_key text;
  v_entry jsonb;
  v_entries jsonb := '[]'::jsonb;
  v_keys text[] := ARRAY[]::text[];
  v_names text[] := ARRAY[]::text[];
  v_canonical jsonb;
BEGIN
  IF p_plan IS NULL OR jsonb_typeof(p_plan)<>'array' THEN
    RAISE EXCEPTION 'fanout plan must be a JSON array' USING ERRCODE='22023';
  END IF;
  v_count := jsonb_array_length(p_plan);
  IF v_count<1 OR v_count>500 THEN
    RAISE EXCEPTION 'fanout plan must contain between 1 and 500 repositories'
      USING ERRCODE='22023';
  END IF;

  FOR v_item IN SELECT value FROM jsonb_array_elements(p_plan)
  LOOP
    IF jsonb_typeof(v_item)<>'object' THEN
      RAISE EXCEPTION 'fanout plan entries must be JSON objects' USING ERRCODE='22023';
    END IF;
    IF EXISTS (
      SELECT 1 FROM jsonb_object_keys(v_item) AS field(name)
       WHERE field.name NOT IN ('key','full_name','id')
    ) THEN
      RAISE EXCEPTION 'fanout plan entry contains an unsupported field' USING ERRCODE='22023';
    END IF;
    IF NOT (v_item ? 'key') OR jsonb_typeof(v_item->'key')<>'string'
       OR NOT (v_item ? 'full_name') OR jsonb_typeof(v_item->'full_name')<>'string' THEN
      RAISE EXCEPTION 'fanout plan entry requires string key and full_name'
        USING ERRCODE='22023';
    END IF;

    v_full_name := v_item->>'full_name';
    IF length(v_full_name)<1 OR length(v_full_name)>512
       OR v_full_name<>btrim(v_full_name)
       OR v_full_name !~ '^[^/[:cntrl:]]+/[^/[:cntrl:]]+$' THEN
      RAISE EXCEPTION 'fanout plan full_name is invalid or unbounded'
        USING ERRCODE='22023';
    END IF;

    IF v_item ? 'id' THEN
      IF jsonb_typeof(v_item->'id') NOT IN ('string','number') THEN
        RAISE EXCEPTION 'fanout repository id must be a positive decimal integer'
          USING ERRCODE='22023';
      END IF;
      v_repository_id := v_item->>'id';
      IF v_repository_id !~ '^[1-9][0-9]{0,31}$' THEN
        RAISE EXCEPTION 'fanout repository id must be a bounded positive decimal integer'
          USING ERRCODE='22023';
      END IF;
      v_key := 'id:'||v_repository_id;
      v_entry := jsonb_build_object(
        'key',v_key,'full_name',v_full_name,'id',v_repository_id);
    ELSE
      v_repository_id := NULL;
      v_key := 'name:'||v_full_name;
      v_entry := jsonb_build_object('key',v_key,'full_name',v_full_name);
    END IF;

    IF v_item->>'key' IS DISTINCT FROM v_key THEN
      RAISE EXCEPTION 'fanout plan key is not canonical for its repository identity'
        USING ERRCODE='22023';
    END IF;
    IF v_key=ANY(v_keys) THEN
      RAISE EXCEPTION 'fanout plan contains a duplicate or conflicting repository key'
        USING ERRCODE='22023';
    END IF;
    IF v_full_name=ANY(v_names) THEN
      RAISE EXCEPTION 'fanout plan contains a duplicate or conflicting repository coordinate'
        USING ERRCODE='22023';
    END IF;
    v_keys := array_append(v_keys,v_key);
    v_names := array_append(v_names,v_full_name);
    v_entries := v_entries||jsonb_build_array(v_entry);
  END LOOP;

  SELECT jsonb_agg(entry.value ORDER BY entry.value->>'key',entry.value->>'full_name')
    INTO v_canonical
    FROM jsonb_array_elements(v_entries) AS entry(value);
  RETURN v_canonical;
END $$;
ALTER FUNCTION core._canonical_webhook_delivery_fanout_plan(jsonb)
  OWNER TO veripsa_migrator;
REVOKE EXECUTE ON FUNCTION core._canonical_webhook_delivery_fanout_plan(jsonb)
  FROM PUBLIC,veripsa_app,veripsa_writer;

-- A partial completion object may contain only canonical plan keys, each with the literal JSON boolean true.
-- Private and immutable so prepare/complete/finish share one poison-state check without exposing a payload oracle.
CREATE OR REPLACE FUNCTION core._webhook_delivery_fanout_completion_valid(
    p_plan jsonb, p_completed jsonb)
RETURNS boolean
    LANGUAGE sql IMMUTABLE SET search_path TO 'core','pg_catalog' AS $$
  SELECT jsonb_typeof(p_plan)='array'
     AND jsonb_typeof(p_completed)='object'
     AND NOT EXISTS (
       SELECT 1
         FROM jsonb_each(
           CASE WHEN jsonb_typeof(p_completed)='object' THEN p_completed ELSE '{}'::jsonb END
         ) AS completed(repo_key,value)
        WHERE completed.value IS DISTINCT FROM 'true'::jsonb
           OR NOT EXISTS (
             SELECT 1
               FROM jsonb_array_elements(
                 CASE WHEN jsonb_typeof(p_plan)='array' THEN p_plan ELSE '[]'::jsonb END
               ) AS planned(repo)
              WHERE planned.repo->>'key'=completed.repo_key
           )
     )
$$;
ALTER FUNCTION core._webhook_delivery_fanout_completion_valid(jsonb,jsonb)
  OWNER TO veripsa_migrator;
REVOKE EXECUTE ON FUNCTION core._webhook_delivery_fanout_completion_valid(jsonb,jsonb)
  FROM PUBLIC,veripsa_app,veripsa_writer;

-- Freeze a bounded per-repository execution plan under the exact processing lease. A retry may observe a changed
-- GitHub enumeration, but once the marker exists this API ignores the new proposal and returns the original plan
-- plus its completion set. That immutability is the point: completed repositories can be skipped without silently
-- adding/removing siblings midway through one accepted delivery. NULL means the caller no longer owns this lease.
CREATE OR REPLACE FUNCTION core.prepare_webhook_delivery_fanout_with_authority(
    p_key text, p_lease_generation bigint, p_plan jsonb)
RETURNS jsonb
    LANGUAGE plpgsql SECURITY DEFINER SET search_path TO 'core','pg_catalog' AS $$
DECLARE
  v_event_type text;
  v_action text;
  v_payload jsonb;
  v_plan jsonb;
  v_completed jsonb;
BEGIN
  IF p_lease_generation IS NULL OR p_lease_generation<1 THEN
    RETURN NULL;
  END IF;
  SELECT d.event_type,d.payload->>'action',d.payload
    INTO v_event_type,v_action,v_payload
    FROM core.webhook_delivery d
   WHERE d.delivery_key=left(COALESCE(p_key,''),200)
     AND d.status='processing'
     AND d.lease_generation=p_lease_generation
   FOR UPDATE;
  IF NOT FOUND THEN
    RETURN NULL;
  END IF;
  IF NOT (
    (v_event_type='installation'
     AND v_action IN ('created','unsuspend','new_permissions_accepted'))
    OR
    (v_event_type='installation_repositories' AND v_action IN ('added','removed'))
  ) THEN
    RAISE EXCEPTION 'durable fanout plan is not allowed for this webhook event/action'
      USING ERRCODE='22023';
  END IF;

  IF v_payload ? '_veripsa_fanout_plan' THEN
    v_plan := core._canonical_webhook_delivery_fanout_plan(
      v_payload->'_veripsa_fanout_plan');
    IF v_plan IS DISTINCT FROM v_payload->'_veripsa_fanout_plan' THEN
      RAISE EXCEPTION 'stored durable fanout plan is not canonical'
        USING ERRCODE='22023';
    END IF;
    v_completed := COALESCE(v_payload->'_veripsa_fanout_completed','{}'::jsonb);
    IF NOT core._webhook_delivery_fanout_completion_valid(v_plan,v_completed) THEN
      RAISE EXCEPTION 'stored durable fanout completion checkpoint is invalid'
        USING ERRCODE='22023';
    END IF;
    RETURN jsonb_build_object('plan',v_plan,'completed',v_completed);
  END IF;
  IF v_payload ? '_veripsa_fanout_completed' THEN
    RAISE EXCEPTION 'durable fanout completion exists without a plan'
      USING ERRCODE='22023';
  END IF;

  v_plan := core._canonical_webhook_delivery_fanout_plan(p_plan);
  v_completed := '{}'::jsonb;
  UPDATE core.webhook_delivery
     SET payload=payload||jsonb_build_object(
           '_veripsa_fanout_plan',v_plan,
           '_veripsa_fanout_completed',v_completed),
         updated_at=now()
   WHERE delivery_key=left(COALESCE(p_key,''),200)
     AND status='processing'
     AND lease_generation=p_lease_generation;
  IF NOT FOUND THEN
    RETURN NULL;
  END IF;
  RETURN jsonb_build_object('plan',v_plan,'completed',v_completed);
END $$;
ALTER FUNCTION core.prepare_webhook_delivery_fanout_with_authority(text,bigint,jsonb)
  OWNER TO veripsa_migrator;
REVOKE EXECUTE ON FUNCTION core.prepare_webhook_delivery_fanout_with_authority(text,bigint,jsonb)
  FROM PUBLIC,veripsa_writer;
GRANT EXECUTE ON FUNCTION core.prepare_webhook_delivery_fanout_with_authority(text,bigint,jsonb)
  TO veripsa_app;

-- Checkpoint one repository only after its business writes, on the SAME caller transaction/connection. Exact
-- generation fencing makes a delayed completion from a prior attempt an idempotent no-op. Return two independent
-- facts so a partial success can never be confused with lost authority: updated=false is missing/stale authority;
-- updated=true says this exact lease accepted the checkpoint, while all_done says whether finish is now permitted.
-- Replacing the old boolean return requires an explicit drop; the whole module is one transaction, so callers
-- observe either the old complete schema or the new complete schema, never a missing function.
DROP FUNCTION IF EXISTS core.complete_webhook_delivery_fanout_repository_with_authority(text,bigint,text);
CREATE OR REPLACE FUNCTION core.complete_webhook_delivery_fanout_repository_with_authority(
    p_key text, p_lease_generation bigint, p_repo_key text)
RETURNS jsonb
    LANGUAGE plpgsql SECURITY DEFINER SET search_path TO 'core','pg_catalog' AS $$
DECLARE
  v_payload jsonb;
  v_plan jsonb;
  v_completed jsonb;
  v_all_completed boolean;
BEGIN
  IF p_lease_generation IS NULL OR p_lease_generation<1 THEN
    RETURN jsonb_build_object('updated',false,'all_done',false);
  END IF;
  SELECT d.payload
    INTO v_payload
    FROM core.webhook_delivery d
   WHERE d.delivery_key=left(COALESCE(p_key,''),200)
     AND d.status='processing'
     AND d.lease_generation=p_lease_generation
   FOR UPDATE;
  IF NOT FOUND THEN
    RETURN jsonb_build_object('updated',false,'all_done',false);
  END IF;
  IF NOT (v_payload ? '_veripsa_fanout_plan') THEN
    RETURN jsonb_build_object('updated',false,'all_done',false);
  END IF;

  v_plan := core._canonical_webhook_delivery_fanout_plan(
    v_payload->'_veripsa_fanout_plan');
  IF v_plan IS DISTINCT FROM v_payload->'_veripsa_fanout_plan' THEN
    RAISE EXCEPTION 'stored durable fanout plan is not canonical'
      USING ERRCODE='22023';
  END IF;
  v_completed := COALESCE(v_payload->'_veripsa_fanout_completed','{}'::jsonb);
  IF NOT core._webhook_delivery_fanout_completion_valid(v_plan,v_completed) THEN
    RAISE EXCEPTION 'stored durable fanout completion checkpoint is invalid'
      USING ERRCODE='22023';
  END IF;
  IF p_repo_key IS NULL OR NOT EXISTS (
    SELECT 1 FROM jsonb_array_elements(v_plan) AS planned(repo)
     WHERE planned.repo->>'key'=p_repo_key
  ) THEN
    RAISE EXCEPTION 'fanout completion key is not present in the durable plan'
      USING ERRCODE='22023';
  END IF;

  v_completed := v_completed||jsonb_build_object(p_repo_key,true);
  UPDATE core.webhook_delivery
     SET payload=jsonb_set(
           payload,ARRAY['_veripsa_fanout_completed'],v_completed,false),
         updated_at=now()
   WHERE delivery_key=left(COALESCE(p_key,''),200)
     AND status='processing'
     AND lease_generation=p_lease_generation;
  IF NOT FOUND THEN
    RETURN jsonb_build_object('updated',false,'all_done',false);
  END IF;
  SELECT NOT EXISTS (
    SELECT 1 FROM jsonb_array_elements(v_plan) AS planned(repo)
     WHERE v_completed->(planned.repo->>'key') IS DISTINCT FROM 'true'::jsonb
  ) INTO v_all_completed;
  RETURN jsonb_build_object('updated',true,'all_done',v_all_completed);
END $$;
ALTER FUNCTION core.complete_webhook_delivery_fanout_repository_with_authority(text,bigint,text)
  OWNER TO veripsa_migrator;
REVOKE EXECUTE ON FUNCTION core.complete_webhook_delivery_fanout_repository_with_authority(text,bigint,text)
  FROM PUBLIC,veripsa_writer;
GRANT EXECUTE ON FUNCTION core.complete_webhook_delivery_fanout_repository_with_authority(text,bigint,text)
  TO veripsa_app;

-- Commit-ambiguity resolver and normal partial-slice yield boundary. The caller invokes this in the SAME
-- transaction immediately after a successful partial repository checkpoint and its business writes. Processing
-- means that transaction has not yet deferred, so this exact-generation function validates the frozen fanout is
-- still genuinely partial and atomically returns the row to queued without consuming its claimed attempt. Queued
-- with the same generation, schedule, and reason proves the prior complete+yield transaction committed. A stale
-- generation can neither prove nor mutate its successor. Fully-completed fanout state is deliberately refused:
-- the final repository must remain processing so finish can commit in that same terminal transaction.
CREATE OR REPLACE FUNCTION core.resolve_webhook_delivery_fanout_defer_with_authority(
    p_key text,
    p_lease_generation bigint,
    p_not_before timestamptz,
    p_reason text)
RETURNS text
    LANGUAGE plpgsql SECURITY DEFINER SET search_path TO 'core','pg_catalog' AS $$
DECLARE
  v_status text;
  v_generation bigint;
  v_payload jsonb;
  v_stored_not_before timestamptz;
  v_stored_reason text;
  v_retry_window_expires_at timestamptz;
  v_plan jsonb;
  v_completed jsonb;
  v_reason text := left(COALESCE(p_reason,'scheduled retry'),300);
BEGIN
  IF p_lease_generation IS NULL OR p_lease_generation<1 THEN
    RETURN 'ownership_lost';
  END IF;
  IF p_not_before IS NULL THEN
    RAISE EXCEPTION 'fanout defer resolution requires not_before'
      USING ERRCODE='22023';
  END IF;

  SELECT d.status,d.lease_generation,d.payload,d.not_before,d.last_error,
         d.retry_window_expires_at
    INTO v_status,v_generation,v_payload,v_stored_not_before,v_stored_reason,
         v_retry_window_expires_at
    FROM core.webhook_delivery d
   WHERE d.delivery_key=left(COALESCE(p_key,''),200)
   FOR UPDATE;
  IF NOT FOUND THEN
    RETURN 'missing';
  END IF;
  IF v_generation IS DISTINCT FROM p_lease_generation
     OR v_status NOT IN ('processing','queued') THEN
    RETURN 'ownership_lost';
  END IF;

  IF NOT (v_payload ? '_veripsa_fanout_plan') THEN
    RAISE EXCEPTION 'fanout defer resolution requires a prepared plan'
      USING ERRCODE='22023';
  END IF;
  v_plan := core._canonical_webhook_delivery_fanout_plan(
    v_payload->'_veripsa_fanout_plan');
  IF v_plan IS DISTINCT FROM v_payload->'_veripsa_fanout_plan' THEN
    RAISE EXCEPTION 'stored durable fanout plan is not canonical'
      USING ERRCODE='22023';
  END IF;
  v_completed := COALESCE(v_payload->'_veripsa_fanout_completed','{}'::jsonb);
  IF NOT core._webhook_delivery_fanout_completion_valid(v_plan,v_completed) THEN
    RAISE EXCEPTION 'stored durable fanout completion checkpoint is invalid'
      USING ERRCODE='22023';
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM jsonb_array_elements(v_plan) AS planned(repo)
     WHERE v_completed->(planned.repo->>'key') IS DISTINCT FROM 'true'::jsonb
  ) THEN
    RAISE EXCEPTION 'completed fanout cannot be deferred'
      USING ERRCODE='55000';
  END IF;

  IF v_status='queued' THEN
    IF v_stored_not_before>=p_not_before
       AND v_retry_window_expires_at IS NULL
       AND v_stored_reason IS NOT DISTINCT FROM v_reason THEN
      RETURN 'deferred';
    END IF;
    RETURN 'ownership_lost';
  END IF;

  UPDATE core.webhook_delivery
     SET status='queued',
         attempts=GREATEST(attempts-1,0),
         locked_at=NULL,
         owner_instance=NULL,
         not_before=GREATEST(p_not_before,clock_timestamp()),
         retry_window_expires_at=NULL,
         updated_at=now(),
         last_error=v_reason
   WHERE delivery_key=left(COALESCE(p_key,''),200)
     AND status='processing'
     AND lease_generation=p_lease_generation;
  IF NOT FOUND THEN
    RETURN 'ownership_lost';
  END IF;
  RETURN 'deferred';
END $$;
ALTER FUNCTION core.resolve_webhook_delivery_fanout_defer_with_authority(
  text,bigint,timestamptz,text) OWNER TO veripsa_migrator;
REVOKE EXECUTE ON FUNCTION core.resolve_webhook_delivery_fanout_defer_with_authority(
  text,bigint,timestamptz,text) FROM PUBLIC,veripsa_writer;
GRANT EXECUTE ON FUNCTION core.resolve_webhook_delivery_fanout_defer_with_authority(
  text,bigint,timestamptz,text) TO veripsa_app;

CREATE OR REPLACE FUNCTION core.finish_webhook_delivery_with_authority(
    p_key text, p_lease_generation bigint)
RETURNS boolean
    LANGUAGE plpgsql SECURITY DEFINER SET search_path TO 'core','pg_catalog' AS $$
DECLARE
  v_unrouted boolean := false;
  v_payload jsonb;
  v_event_type text;
  v_action text;
  v_fanout_plan jsonb;
  v_fanout_completed jsonb;
BEGIN
  -- A processor deliberately returns without tenant work when the claimed account has no live installation route
  -- (post-erasure/revocation or malformed never-routed work). Finalise that row as an anonymous receipt instead of
  -- retaining newly-arrived account/repository identifiers or leaving lifecycle events stuck in recovery forever.
  SELECT NOT EXISTS (
           SELECT 1 FROM core.installation_account ia
            WHERE ia.revoked_at IS NULL
              AND (ia.installation_id=d.account_key
                   OR ia.account_id=d.account_key
                   OR (d.account_key IS NOT NULL AND ia.account_id='ACCT-GH-'||d.account_key))),
         d.payload,d.event_type,d.payload->>'action'
    INTO v_unrouted,v_payload,v_event_type,v_action
    FROM core.webhook_delivery d
   WHERE d.delivery_key=left(COALESCE(p_key,''),200)
     AND d.status='processing'
     AND d.lease_generation=p_lease_generation
   FOR UPDATE OF d;
  IF NOT FOUND THEN
    RETURN false;
  END IF;

  -- A prepared fan-out is terminal only after the immutable non-empty canonical plan is wholly checkpointed.
  -- Malformed/restored/forged marker state fails closed without erasing the evidence needed for recovery.
  IF v_payload ? '_veripsa_fanout_plan' THEN
    IF NOT (
      (v_event_type='installation'
       AND v_action IN ('created','unsuspend','new_permissions_accepted'))
      OR
      (v_event_type='installation_repositories' AND v_action IN ('added','removed'))
    ) THEN
      RETURN false;
    END IF;
    BEGIN
      v_fanout_plan := core._canonical_webhook_delivery_fanout_plan(
        v_payload->'_veripsa_fanout_plan');
    EXCEPTION WHEN invalid_parameter_value THEN
      RETURN false;
    END;
    IF v_fanout_plan IS DISTINCT FROM v_payload->'_veripsa_fanout_plan' THEN
      RETURN false;
    END IF;
    v_fanout_completed := COALESCE(
      v_payload->'_veripsa_fanout_completed','{}'::jsonb);
    IF NOT core._webhook_delivery_fanout_completion_valid(
      v_fanout_plan,v_fanout_completed) THEN
      RETURN false;
    END IF;
    IF EXISTS (
      SELECT 1 FROM jsonb_array_elements(v_fanout_plan) AS planned(repo)
       WHERE v_fanout_completed->(planned.repo->>'key') IS DISTINCT FROM 'true'::jsonb
    ) THEN
      RETURN false;
    END IF;
  ELSIF v_payload ? '_veripsa_fanout_completed' THEN
    RETURN false;
  END IF;

  UPDATE core.webhook_delivery d
     SET status='done',
         -- A repository.deleted delivery is the one processing row the repo purge must leave for its owner to
         -- finish. Drop its deleted private-repo coordinate at finalisation instead of retaining it until the
         -- fleet age sweep. installation_repositories rows already carry repo=NULL in this column.
         repo=CASE WHEN v_unrouted
                         OR (event_type='repository' AND payload->>'action'='deleted')
                         OR (event_type='installation' AND payload->>'action'='deleted')
                   THEN NULL ELSE repo END,
         -- Keep only an opaque idempotency receipt for the uninstall delivery.  Without it, replaying the same
         -- GitHub delivery id after purge/erase would be admitted with a fresh received_at and could resurrect the
         -- deleted lifecycle generation.  Account/repository/payload are deliberately scrubbed.
         account_key=CASE WHEN v_unrouted
                               OR (event_type='installation' AND payload->>'action'='deleted')
                          THEN NULL ELSE account_key END,
         event_type=CASE WHEN v_unrouted
                              OR (event_type='installation' AND payload->>'action'='deleted')
                         THEN 'erased' ELSE event_type END,
         attempts=CASE WHEN v_unrouted
                            OR (event_type='installation' AND payload->>'action'='deleted')
                       THEN 0 ELSE attempts END,
         payload='{}'::jsonb, done_at=now(), updated_at=now(), locked_at=NULL,
         owner_instance=NULL, last_error=NULL, not_before=NULL,
         retry_window_expires_at=NULL
   WHERE delivery_key=left(COALESCE(p_key,''),200)
     AND status='processing'
     AND lease_generation=p_lease_generation
     AND (
       -- Old workers may catch a repository-offboard DB error and return success. Finalise deletion deliveries
       -- only when the durable lifecycle boundary marked every named repository complete in this same payload.
       v_unrouted
       OR NOT (event_type='repository' AND payload->>'action'='deleted')
       OR (NULLIF(payload->'repository'->>'full_name','') IS NOT NULL
           AND COALESCE(payload->'_veripsa_offboard_completed','{}'::jsonb)
                ? (payload->'repository'->>'full_name')
          AND CASE
            WHEN NULLIF(btrim(payload->'repository'->>'id'),'') IS NULL THEN
              COALESCE(payload->'_veripsa_offboard_completed','{}'::jsonb)
                ->> (payload->'repository'->>'full_name') = 'unknown'
              OR (
                COALESCE(payload->'_veripsa_offboard_completed','{}'::jsonb)
                  ->> (payload->'repository'->>'full_name') ~ '^[1-9][0-9]*$'
                AND length(COALESCE(payload->'_veripsa_offboard_completed','{}'::jsonb)
                  ->> (payload->'repository'->>'full_name')) <= 32)
            WHEN payload->'repository'->>'id' ~ '^[1-9][0-9]*$'
                 AND length(payload->'repository'->>'id') <= 32 THEN
              COALESCE(payload->'_veripsa_offboard_completed','{}'::jsonb)
                ->> (payload->'repository'->>'full_name') = payload->'repository'->>'id'
            ELSE false
          END))
     AND (
       v_unrouted
       OR NOT (event_type='installation_repositories' AND payload->>'action'='removed')
       OR (jsonb_typeof(payload->'repositories_removed')='array'
           AND jsonb_array_length(payload->'repositories_removed')>0
          AND NOT EXISTS (
            SELECT 1
             FROM jsonb_array_elements(payload->'repositories_removed') AS removed(repo)
             WHERE NULLIF(removed.repo->>'full_name','') IS NULL
                 OR NOT (COALESCE(payload->'_veripsa_offboard_completed','{}'::jsonb)
                           ? (removed.repo->>'full_name'))
                 OR NOT CASE
                   WHEN NULLIF(btrim(removed.repo->>'id'),'') IS NULL THEN
                     COALESCE(payload->'_veripsa_offboard_completed','{}'::jsonb)
                       ->> (removed.repo->>'full_name') = 'unknown'
                     OR (
                       COALESCE(payload->'_veripsa_offboard_completed','{}'::jsonb)
                         ->> (removed.repo->>'full_name') ~ '^[1-9][0-9]*$'
                       AND length(COALESCE(payload->'_veripsa_offboard_completed','{}'::jsonb)
                         ->> (removed.repo->>'full_name')) <= 32)
                   WHEN removed.repo->>'id' ~ '^[1-9][0-9]*$'
                        AND length(removed.repo->>'id') <= 32 THEN
                     COALESCE(payload->'_veripsa_offboard_completed','{}'::jsonb)
                       ->> (removed.repo->>'full_name') = removed.repo->>'id'
                   ELSE false
                 END)
          AND NOT EXISTS (
            SELECT 1
              FROM jsonb_array_elements(payload->'repositories_removed') AS removed(repo)
             WHERE NULLIF(removed.repo->>'full_name','') IS NOT NULL
             GROUP BY removed.repo->>'full_name'
            HAVING count(DISTINCT CASE
                     WHEN NULLIF(btrim(removed.repo->>'id'),'') IS NULL THEN 'unknown'
                     WHEN removed.repo->>'id' ~ '^[1-9][0-9]*$'
                          AND length(removed.repo->>'id') <= 32
                       THEN 'id:' || (removed.repo->>'id')
                     ELSE 'invalid'
                   END) > 1)));
                                -- EXACT-LEASE guard: status alone cannot prove ownership after stale reclaim because
                                -- both old and new workers observe 'processing'. The monotonic generation never resets
                                -- on DLQ rearm, so a late prior owner is an idempotent NO-OP even across attempts ABA.
  RETURN FOUND;
END $$;
ALTER FUNCTION core.finish_webhook_delivery_with_authority(text,bigint) OWNER TO veripsa_migrator;

-- Rolling old-worker shim. A pre-upgrade caller carries no generation, so it may finalize only the first lease.
-- Once any stale reclaim advances generation, old finalize is safely delayed to recovery rather than corrupting it.
CREATE OR REPLACE FUNCTION core.finish_webhook_delivery_with_authority(p_key text)
RETURNS boolean
    LANGUAGE sql SECURITY DEFINER SET search_path TO 'core','pg_catalog' AS $$
  SELECT core.finish_webhook_delivery_with_authority(p_key,1::bigint)
$$;
ALTER FUNCTION core.finish_webhook_delivery_with_authority(text) OWNER TO veripsa_migrator;
REVOKE EXECUTE ON FUNCTION core.finish_webhook_delivery_with_authority(text,bigint)
  FROM PUBLIC, veripsa_writer;
REVOKE EXECUTE ON FUNCTION core.finish_webhook_delivery_with_authority(text)
  FROM PUBLIC, veripsa_writer;
GRANT EXECUTE ON FUNCTION core.finish_webhook_delivery_with_authority(text,bigint) TO veripsa_app;
GRANT EXECUTE ON FUNCTION core.finish_webhook_delivery_with_authority(text) TO veripsa_app;

CREATE OR REPLACE FUNCTION core.release_webhook_delivery_with_authority(
    p_key text,
    p_error text,
    p_max_attempts int,
    p_lease_generation bigint
) RETURNS text
    LANGUAGE plpgsql SECURITY DEFINER SET search_path TO 'core','pg_catalog' AS $$
DECLARE
  v_status text;
  v_released_at timestamptz := clock_timestamp();
BEGIN
  UPDATE core.webhook_delivery
     SET status = CASE
           WHEN attempts >= GREATEST(COALESCE(p_max_attempts,3),1)
                OR (retry_window_expires_at IS NOT NULL
                    AND retry_window_expires_at<=v_released_at)
             THEN 'failed'
           ELSE 'queued'
         END,
         locked_at = NULL,
         owner_instance = NULL,
         not_before = NULL,
         updated_at = v_released_at,
         last_error = left(COALESCE(p_error,''),300)
   WHERE delivery_key=left(COALESCE(p_key,''),200)
     AND status='processing'
     AND lease_generation=p_lease_generation
   RETURNING status INTO v_status;
  RETURN COALESCE(v_status, 'missing');
END $$;
ALTER FUNCTION core.release_webhook_delivery_with_authority(text,text,int,bigint) OWNER TO veripsa_migrator;

-- GENERAL RELEASE COMMIT-ACK RESOLUTION.  This is the primary runtime boundary
-- for ordinary processor failure, cancellation, and failed owner stamping.
-- It is deliberately idempotent across a lost response but not across an ABA:
-- the generation is monotonic and the already-terminal proof also requires the
-- exact normalized error, attempt policy, unlocked state, and cleared owner.
CREATE OR REPLACE FUNCTION core.resolve_webhook_delivery_release_with_authority(
    p_key text,
    p_error text,
    p_max_attempts int,
    p_lease_generation bigint
) RETURNS text
    LANGUAGE plpgsql SECURITY DEFINER SET search_path TO 'core','pg_catalog' AS $$
DECLARE
  v_status text;
  v_generation bigint;
  v_attempts int;
  v_locked_at timestamptz;
  v_owner_instance text;
  v_not_before timestamptz;
  v_stored_error text;
  v_retry_window_expires_at timestamptz;
  v_updated_at timestamptz;
  v_error text := left(COALESCE(p_error,''),300);
  v_max_attempts int := GREATEST(COALESCE(p_max_attempts,3),1);
  v_expected_status text;
BEGIN
  IF p_lease_generation IS NULL OR p_lease_generation<1 THEN
    RETURN 'ownership_lost';
  END IF;
  SELECT d.status,d.lease_generation,d.attempts,d.locked_at,
         d.owner_instance,d.not_before,d.last_error,
         d.retry_window_expires_at,d.updated_at
    INTO v_status,v_generation,v_attempts,v_locked_at,
         v_owner_instance,v_not_before,v_stored_error,
         v_retry_window_expires_at,v_updated_at
    FROM core.webhook_delivery d
   WHERE d.delivery_key=left(COALESCE(p_key,''),200)
   FOR UPDATE;
  IF NOT FOUND THEN
    RETURN 'missing';
  END IF;
  IF v_generation IS DISTINCT FROM p_lease_generation THEN
    RETURN 'ownership_lost';
  END IF;
  IF v_status='processing' THEN
    RETURN core.release_webhook_delivery_with_authority(
      p_key,v_error,v_max_attempts,p_lease_generation);
  END IF;
  -- Prove the state chosen by the original release's timestamp. Never use
  -- probe-now: an ACK can disappear while the absolute window crosses, and
  -- that must not turn an exact queued proof into ownership_lost.
  v_expected_status := CASE
    WHEN v_attempts>=v_max_attempts
         OR (v_retry_window_expires_at IS NOT NULL
             AND v_retry_window_expires_at<=v_updated_at)
      THEN 'failed'
    ELSE 'queued'
  END;
  IF v_status=v_expected_status
     AND v_locked_at IS NULL
     AND v_owner_instance IS NULL
     AND v_not_before IS NULL
     AND v_stored_error IS NOT DISTINCT FROM v_error THEN
    RETURN v_status;
  END IF;
  RETURN 'ownership_lost';
END $$;
ALTER FUNCTION core.resolve_webhook_delivery_release_with_authority(
  text,text,int,bigint) OWNER TO veripsa_migrator;
REVOKE EXECUTE ON FUNCTION core.resolve_webhook_delivery_release_with_authority(
  text,text,int,bigint) FROM PUBLIC,veripsa_writer;
GRANT EXECUTE ON FUNCTION core.resolve_webhook_delivery_release_with_authority(
  text,text,int,bigint) TO veripsa_app;

-- COMMIT-RESPONSE AMBIGUITY RESOLUTION. A processor can lose the response after its server-side transaction
-- committed, so treating every commit exception as "work failed" can immediately replay writes that already
-- landed. Resolve against the exact durable lease generation under one row lock:
--   done + same generation       => committed (never requeue already-landed work)
--   processing + same generation => pre-commit/unknown work is released through the ordinary retry/DLQ policy
--   queued/failed + exact release arguments => that release committed and only its response was lost
--   any other state/generation   => ownership_lost (a stale caller has no mutation authority)
--   no key                       => missing (durability invariant violation, distinct from a lost lease)
CREATE OR REPLACE FUNCTION core.resolve_webhook_delivery_commit_with_authority(
    p_key text,
    p_error text,
    p_max_attempts int,
    p_lease_generation bigint
) RETURNS text
    LANGUAGE plpgsql SECURITY DEFINER SET search_path TO 'core','pg_catalog' AS $$
DECLARE
  v_status text;
  v_generation bigint;
BEGIN
  SELECT d.status,d.lease_generation
    INTO v_status,v_generation
    FROM core.webhook_delivery d
   WHERE d.delivery_key=left(COALESCE(p_key,''),200)
   FOR UPDATE;
  IF NOT FOUND THEN
    RETURN 'missing';
  END IF;
  IF v_generation IS DISTINCT FROM p_lease_generation THEN
    RETURN 'ownership_lost';
  END IF;
  IF v_status='done' THEN
    RETURN 'committed';
  END IF;
  IF v_status IN ('processing','queued','failed') THEN
    RETURN core.resolve_webhook_delivery_release_with_authority(
      p_key,p_error,p_max_attempts,p_lease_generation);
  END IF;
  RETURN 'ownership_lost';
END $$;
ALTER FUNCTION core.resolve_webhook_delivery_commit_with_authority(text,text,int,bigint)
  OWNER TO veripsa_migrator;
REVOKE EXECUTE ON FUNCTION core.resolve_webhook_delivery_commit_with_authority(text,text,int,bigint)
  FROM PUBLIC, veripsa_writer;
GRANT EXECUTE ON FUNCTION core.resolve_webhook_delivery_commit_with_authority(text,text,int,bigint)
  TO veripsa_app;

-- CLAIM-RESPONSE AMBIGUITY RECOVERY. A client can lose the response after the /5 or /6 claim transaction committed,
-- leaving it without lease_generation and therefore unable to use the ordinary exact-lease release. New clients
-- pre-generate a claim-UNIQUE owner reference (boot instance + nonce), which the claim UPDATE stores atomically.
-- Matching the complete reference is generation-safe: even a later claim by the SAME boot uses a different nonce,
-- so a delayed recovery from the earlier call cannot release the successor. The function deliberately performs
-- the same bounded retry/DLQ transition as release, and clears ownership to make retries idempotent.
CREATE OR REPLACE FUNCTION core.recover_ambiguous_webhook_claim_with_authority(
    p_key text,
    p_owner_instance text,
    p_error text,
    p_max_attempts int
) RETURNS text
    LANGUAGE plpgsql SECURITY DEFINER SET search_path TO 'core','pg_catalog' AS $$
DECLARE v_status text;
BEGIN
  IF p_owner_instance IS NULL
     OR length(p_owner_instance) < 1 OR length(p_owner_instance) > 64 THEN
    RETURN 'missing';
  END IF;
  UPDATE core.webhook_delivery
     SET status = CASE
           WHEN attempts >= GREATEST(COALESCE(p_max_attempts,3),1)
                OR (retry_window_expires_at IS NOT NULL
                    AND retry_window_expires_at<=clock_timestamp())
             THEN 'failed'
           ELSE 'queued'
         END,
         locked_at = NULL,
         owner_instance = NULL,
         not_before = NULL,
         updated_at = now(),
         last_error = left(COALESCE(p_error,'ambiguous claim response'),300)
   WHERE delivery_key=left(COALESCE(p_key,''),200)
     AND status='processing'
     AND owner_instance=p_owner_instance
   RETURNING status INTO v_status;
  RETURN COALESCE(v_status, 'missing');
END $$;
ALTER FUNCTION core.recover_ambiguous_webhook_claim_with_authority(text,text,text,int)
  OWNER TO veripsa_migrator;
REVOKE EXECUTE ON FUNCTION core.recover_ambiguous_webhook_claim_with_authority(text,text,text,int)
  FROM PUBLIC, veripsa_writer;
GRANT EXECUTE ON FUNCTION core.recover_ambiguous_webhook_claim_with_authority(text,text,text,int)
  TO veripsa_app;

-- Rolling old-worker shim: exactly like finish(/1), no generation can safely mean only generation 1.
CREATE OR REPLACE FUNCTION core.release_webhook_delivery_with_authority(
    p_key text,
    p_error text DEFAULT '',
    p_max_attempts int DEFAULT 3
) RETURNS text
    LANGUAGE sql SECURITY DEFINER SET search_path TO 'core','pg_catalog' AS $$
  SELECT core.release_webhook_delivery_with_authority(p_key,p_error,p_max_attempts,1::bigint)
$$;
ALTER FUNCTION core.release_webhook_delivery_with_authority(text,text,int) OWNER TO veripsa_migrator;
REVOKE EXECUTE ON FUNCTION core.release_webhook_delivery_with_authority(text,text,int,bigint)
  FROM PUBLIC, veripsa_writer;
REVOKE EXECUTE ON FUNCTION core.release_webhook_delivery_with_authority(text,text,int)
  FROM PUBLIC, veripsa_writer;
GRANT EXECUTE ON FUNCTION core.release_webhook_delivery_with_authority(text,text,int,bigint) TO veripsa_app;
GRANT EXECUTE ON FUNCTION core.release_webhook_delivery_with_authority(text,text,int) TO veripsa_app;

-- SHUTDOWN LEASE EXPIRY. A deploy's SIGTERM can interrupt the worker mid-event after the bounded drain, and the
-- dying process may STILL commit+finish in its final moments — so requeueing here (release/defer) would race that
-- commit and re-introduce the audited double-processing defect. But leaving the row untouched blocks its whole
-- account/repository causal lane for the full stale window (the queued=41 lane-freeze incident). The race-free
-- middle: keep status='processing' and the lease intact (finish/release/defer all still work), and only BACKDATE
-- locked_at so the ordinary stale-processing reclaim fires after a short grace instead of the full window. If the
-- worker does finish, the backdated lock is irrelevant; if the process dies, recovery reclaims within the grace
-- with a fresh lease generation. LEAST() never moves an already-older lock forward.
CREATE OR REPLACE FUNCTION core.expire_webhook_delivery_lease_with_authority(
    p_key text,
    p_lease_generation bigint,
    p_stale_seconds int,
    p_grace_seconds int
) RETURNS boolean
    LANGUAGE plpgsql SECURITY DEFINER SET search_path TO 'core','pg_catalog' AS $$
DECLARE
  v_stale int := GREATEST(COALESCE(p_stale_seconds,1800),1);
  v_grace int;
BEGIN
  IF p_lease_generation IS NULL OR p_lease_generation<1 THEN
    RETURN false;
  END IF;
  v_grace := LEAST(GREATEST(COALESCE(p_grace_seconds,60),1),v_stale);
  UPDATE core.webhook_delivery
     SET locked_at=LEAST(locked_at,
                         clock_timestamp()-make_interval(secs=>v_stale-v_grace)),
         updated_at=now(),
         last_error=left('shutdown drain timeout: lease expiring early for recovery',300)
   WHERE delivery_key=left(COALESCE(p_key,''),200)
     AND status='processing'
     AND lease_generation=p_lease_generation;
  RETURN FOUND;
END $$;
ALTER FUNCTION core.expire_webhook_delivery_lease_with_authority(text,bigint,int,int)
  OWNER TO veripsa_migrator;
REVOKE EXECUTE ON FUNCTION core.expire_webhook_delivery_lease_with_authority(text,bigint,int,int)
  FROM PUBLIC, veripsa_writer;
GRANT EXECUTE ON FUNCTION core.expire_webhook_delivery_lease_with_authority(text,bigint,int,int)
  TO veripsa_app;

-- DEAD-INSTANCE FAST LEASE RECLAIM. #840's early expiry (above) runs ONLY from the graceful SIGTERM path, so an
-- UNGRACEFUL death (SIGKILL/OOM/host loss/eviction after Render's ~30s grace) leaves the in-flight row
-- 'processing' with a recent lock and freezes its whole account/repo causal lane for the full stale window
-- (1800s). Give each worker process a liveness heartbeat: a 'processing' row whose OWNING instance has provably
-- stopped beating is orphaned and can be reclaimed in seconds, while a slow-BUT-ALIVE worker (still beating on
-- its dedicated liveness thread) keeps its lease the full window exactly as before. STRICTLY ADDITIVE: the pending()/claim()
-- reclaim/barrier predicates are UNCHANGED; the reaper only BACKDATES locked_at (the proven #840 LEAST()
-- mechanism) for dead owners, so the ordinary stale-processing reclaim fires early. A row with a NULL
-- owner_instance (legacy, an older build, or claimed-but-not-yet-stamped) is NEVER fast-reaped — it keeps
-- today's full-window behavior. Idempotency is inherited: reclaim goes through the unchanged claim() path
-- (advancing lease_generation), and the exact-lease finish/release/defer guard makes any late commit by a
-- presumed-dead owner a NO-OP, so a wrong liveness verdict costs at most one redundant reclaim, never a
-- double-execution.
-- The owner column/constraint and heartbeat relation were expanded before the
-- publication transaction so this liveness API cannot retain table locks.

-- UPSERT this process's heartbeat + cheap housekeeping of long-dead instance rows (bounded).
CREATE OR REPLACE FUNCTION core.beat_webhook_worker_instance_with_authority(p_instance text)
    RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path TO 'core','pg_catalog' AS $$
BEGIN
  IF p_instance IS NULL OR length(p_instance) < 1 OR length(p_instance) > 64 THEN
    RETURN;
  END IF;
  INSERT INTO core.webhook_worker_instance(instance_id, last_heartbeat, started_at)
       VALUES (p_instance, now(), now())
  ON CONFLICT (instance_id) DO UPDATE SET last_heartbeat = now();
  DELETE FROM core.webhook_worker_instance WHERE last_heartbeat < now() - interval '1 day';
END $$;
ALTER FUNCTION core.beat_webhook_worker_instance_with_authority(text) OWNER TO veripsa_migrator;
REVOKE EXECUTE ON FUNCTION core.beat_webhook_worker_instance_with_authority(text) FROM PUBLIC, veripsa_writer;
GRANT EXECUTE ON FUNCTION core.beat_webhook_worker_instance_with_authority(text) TO veripsa_app;

-- Stamp the stable boot instance on a row THIS process just claimed (exact
-- lease). New /6 and bounded rolling /5 callers arrive with a temporary
-- ambiguity nonce and replace it only after the claim response is known. The
-- expand generation temporarily preserves the exact operational predecessor
-- /4, whose legacy claim can remain ownerless until the old worker completes;
-- the following contract generation may fence it only after /6 is live.
-- The heartbeat UPSERT and owner stamp are one transaction. Therefore no committed row can expose a stable
-- owner_instance whose liveness row is still absent — a rolling peer's reaper can never race the first background
-- heartbeat and falsely reclaim a freshly-authorized handler.
CREATE OR REPLACE FUNCTION core.stamp_webhook_owner_instance_with_authority(
    p_key text, p_lease_generation bigint, p_instance text)
    RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER SET search_path TO 'core','pg_catalog' AS $$
BEGIN
  IF p_instance IS NULL OR length(p_instance) < 1 OR length(p_instance) > 64
     OR p_lease_generation IS NULL OR p_lease_generation < 1 THEN
    RETURN false;
  END IF;
  INSERT INTO core.webhook_worker_instance(instance_id,last_heartbeat,started_at)
       VALUES (p_instance,now(),now())
  ON CONFLICT (instance_id) DO UPDATE SET last_heartbeat=now();
  UPDATE core.webhook_delivery
     SET owner_instance = p_instance
   WHERE delivery_key = left(COALESCE(p_key,''),200)
     AND status = 'processing'
     AND lease_generation = p_lease_generation;
  RETURN FOUND;
END $$;
ALTER FUNCTION core.stamp_webhook_owner_instance_with_authority(text,bigint,text) OWNER TO veripsa_migrator;
REVOKE EXECUTE ON FUNCTION core.stamp_webhook_owner_instance_with_authority(text,bigint,text)
  FROM PUBLIC, veripsa_writer;
GRANT EXECUTE ON FUNCTION core.stamp_webhook_owner_instance_with_authority(text,bigint,text) TO veripsa_app;

-- Reap leases owned by a provably-DEAD instance, but never merely because three heartbeats were missed. A valid
-- handler may consume the complete configured event wall budget even while its heartbeat connection is transiently
-- unavailable. Stable owners therefore need BOTH evidence of death (missing/stale heartbeat) AND an exact lease at
-- least the effective safe age old before we backdate locked_at for immediate ordinary reclaim. The app derives
-- that age from event-total + terminal reserve + clock/transport margin; the SQL floor of 100 seconds keeps the
-- shipped 90-second event safe during a rolling schema/runtime transition where an older caller still passes 60.
--
-- A claim-unique nonce is different: it exists only before any handler is authorized. Its caller-encoded ambiguity
-- window is the sole clock. Neither a missing NOR stale boot heartbeat can accelerate that nonce, because the claim
-- response may still arrive and exact-stamp the stable owner. Once its encoded window expires, no handler could have
-- started under that nonce and immediate reclaim is safe.
CREATE OR REPLACE FUNCTION core.reap_dead_instance_leases_with_authority(
    p_dead_seconds int DEFAULT 15,
    p_stale_seconds int DEFAULT 1800,
    -- PostgreSQL treats input parameter names as part of CREATE OR REPLACE
    -- compatibility. Generation 12 published this position as
    -- p_grace_seconds, so keep that external ABI even though generation 18
    -- tightened its meaning from a reclaim grace to a minimum safe lease age.
    p_grace_seconds int DEFAULT 100)
    RETURNS int LANGUAGE plpgsql SECURITY DEFINER SET search_path TO 'core','pg_catalog' AS $$
DECLARE
  v_dead int := GREATEST(COALESCE(p_dead_seconds,15),1);
  v_stale int := GREATEST(COALESCE(p_stale_seconds,1800),1);
  v_safe int := LEAST(GREATEST(COALESCE(p_grace_seconds,100),100),v_stale);
  v_reaped int;
BEGIN
  WITH dead AS (
      SELECT d.delivery_key,
             CASE
               WHEN d.owner_instance ~ '^wk-[0-9a-f]{32}[.][0-9]{4}[.][0-9a-f]{22}$'
                 THEN d.locked_at <= clock_timestamp() - make_interval(
                        secs => substring(d.owner_instance from 37 for 4)::int)
               ELSE false
             END AS ambiguity_expired
        FROM core.webhook_delivery d
        LEFT JOIN core.webhook_worker_instance w
          ON w.instance_id = CASE
               -- Before its response is acknowledged, a /5 claim is owned by
               --   <35-char boot id>.<4-digit ambiguity window>.<22-hex nonce>.
               -- Successful callers immediately replace it with the plain boot id through the exact-lease stamp.
               WHEN d.owner_instance ~ '^wk-[0-9a-f]{32}[.][0-9]{4}[.][0-9a-f]{22}$'
                 THEN left(d.owner_instance,35)
               ELSE d.owner_instance
             END
       WHERE d.status = 'processing'
         AND d.owner_instance IS NOT NULL
         AND CASE
               WHEN d.owner_instance ~ '^wk-[0-9a-f]{32}[.][0-9]{4}[.][0-9a-f]{22}$'
                 -- Pre-handler nonce: heartbeat state is deliberately irrelevant. Only its exact encoded
                 -- ambiguity allowance can prove that no caller was authorized to begin handler work.
                 THEN d.locked_at <= clock_timestamp() - make_interval(
                        secs => substring(d.owner_instance from 37 for 4)::int)
               ELSE (
                 (w.instance_id IS NULL
                  OR w.last_heartbeat < now() - make_interval(secs => v_dead))
                 AND d.locked_at <= clock_timestamp() - make_interval(secs => v_safe)
               )
             END
         -- Already-stale rows belong to the unchanged ordinary claim path and need no repeated reaper update.
         AND d.locked_at > clock_timestamp() - make_interval(secs => v_stale)
  )
  UPDATE core.webhook_delivery t
     SET locked_at = LEAST(
           t.locked_at,
           -- Every candidate already crossed its own safe absolute age above. Make the unchanged stale-processing
           -- claim path authoritative on its next call instead of adding a second grace measured from this sweep.
           clock_timestamp() - make_interval(secs => v_stale + 1)
         ),
         updated_at = now(),
         last_error = left(
           CASE WHEN dead.ambiguity_expired
                THEN 'ambiguous claim response expired: lease reclaimable for recovery'
                ELSE 'owner instance dead: lease reclaimed early for recovery' END,
           300)
    FROM dead
   WHERE t.delivery_key = dead.delivery_key
     AND t.status = 'processing';
  GET DIAGNOSTICS v_reaped = ROW_COUNT;
  RETURN v_reaped;
END $$;
ALTER FUNCTION core.reap_dead_instance_leases_with_authority(int,int,int) OWNER TO veripsa_migrator;
REVOKE EXECUTE ON FUNCTION core.reap_dead_instance_leases_with_authority(int,int,int) FROM PUBLIC, veripsa_writer;
GRANT EXECUTE ON FUNCTION core.reap_dead_instance_leases_with_authority(int,int,int) TO veripsa_app;

-- Function creation grants PUBLIC execute by default. Revoke and grant every callable protocol surface before the
-- transaction commits, so neither a fresh install nor a hot re-apply exposes a partially secured API generation.
REVOKE EXECUTE ON FUNCTION core.enqueue_webhook_delivery_with_authority(text,text,text,text,jsonb,int) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION core.enqueue_webhook_delivery_with_authority(text,text,text,text,jsonb,int,int) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION core.pending_webhook_deliveries_with_authority(int,int,int) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION core.claim_webhook_delivery_with_authority(text,int,int,int,text,int) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION core.claim_webhook_delivery_with_authority(text,int,int,int,text) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION core.claim_webhook_delivery_with_authority(text,int,int,int) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION core.claim_webhook_delivery_with_authority(text,int,int) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION core.enqueue_webhook_delivery_with_authority(text,text,text,text,jsonb,int) TO veripsa_app;
GRANT EXECUTE ON FUNCTION core.enqueue_webhook_delivery_with_authority(text,text,text,text,jsonb,int,int) TO veripsa_app;
GRANT EXECUTE ON FUNCTION core.pending_webhook_deliveries_with_authority(int,int,int) TO veripsa_app;
GRANT EXECUTE ON FUNCTION core.claim_webhook_delivery_with_authority(text,int,int,int,text,int) TO veripsa_app;
GRANT EXECUTE ON FUNCTION core.claim_webhook_delivery_with_authority(text,int,int,int,text) TO veripsa_app;
GRANT EXECUTE ON FUNCTION core.claim_webhook_delivery_with_authority(text,int,int,int) TO veripsa_app;
GRANT EXECUTE ON FUNCTION core.claim_webhook_delivery_with_authority(text,int,int) TO veripsa_app;
-- The module transaction stays open through the remaining observability/rearm definitions and final ACL sweep.

CREATE OR REPLACE FUNCTION core.webhook_delivery_depth_with_authority()
RETURNS jsonb
    LANGUAGE sql SECURITY DEFINER SET search_path TO 'core','pg_catalog' AS $$
  WITH counts AS (
    SELECT status, count(*)::int AS n FROM core.webhook_delivery GROUP BY status
  ),
  processing_age AS (
    SELECT COALESCE(floor(EXTRACT(EPOCH FROM (now() - min(locked_at))))::int, 0) AS oldest_age_seconds
      FROM core.webhook_delivery
     WHERE status='processing'
       AND locked_at IS NOT NULL
  ),
  due_queue AS (
    SELECT count(*)::int AS due,
           COALESCE(floor(EXTRACT(EPOCH FROM (now()-min(received_at))))::int,0)
             AS oldest_due_age_seconds,
           COALESCE(max(attempts),0)::int AS max_attempts
      FROM core.webhook_delivery
     WHERE status='queued'
       AND COALESCE(not_before,'-infinity'::timestamptz)<=now()
  ),
  active_fanout AS (
    SELECT count(*)::int AS active,
           COALESCE(sum(
             jsonb_array_length(payload->'_veripsa_fanout_plan')
             - CASE
                 WHEN jsonb_typeof(payload->'_veripsa_fanout_completed')='object'
                   THEN (
                     SELECT count(*)::int
                       FROM jsonb_each(payload->'_veripsa_fanout_completed') AS done(k,v)
                      WHERE done.v='true'::jsonb
                   )
                 ELSE 0
               END
           ),0)::int AS remaining,
           COALESCE(floor(EXTRACT(EPOCH FROM (now()-min(received_at))))::int,0)
             AS oldest_delivery_age_seconds,
           COALESCE(floor(EXTRACT(EPOCH FROM (now()-min(updated_at))))::int,0)
             AS oldest_progress_age_seconds
      FROM core.webhook_delivery
     WHERE status IN ('queued','processing')
       AND jsonb_typeof(payload->'_veripsa_fanout_plan')='array'
  ),
  retry_window AS (
    SELECT
      count(*) FILTER (
        WHERE status IN ('queued','processing')
          AND retry_window_expires_at IS NOT NULL
          AND retry_window_expires_at>now())::int AS active,
      count(*) FILTER (
        WHERE status='failed'
          AND retry_window_expires_at IS NOT NULL
          AND retry_window_expires_at<=updated_at)::int AS exhausted_failed,
      count(*) FILTER (
        WHERE status='failed'
          AND (event_type='ping' OR causal_order_version>=1)
          AND auto_rearm_count<1)::int AS auto_eligible_failed,
      count(*) FILTER (
        WHERE status='failed'
          AND (event_type='ping' OR causal_order_version>=1)
          AND auto_rearm_count>=1)::int AS auto_exhausted_failed
      FROM core.webhook_delivery
  )
  -- Status groups with no rows are absent from jsonb_object_agg. Publish the
  -- closed status enum as a total shape so an empty healthy inbox is
  -- distinguishable from malformed or unavailable evidence. The dynamic
  -- aggregate on the right replaces each zero with its real count.
  SELECT jsonb_build_object(
           'queued', 0,
           'processing', 0,
           'done', 0,
           'failed', 0
         )
         || COALESCE(jsonb_object_agg(status, n), '{}'::jsonb)
         || jsonb_build_object(
              'processing_oldest_age_seconds',
              COALESCE((SELECT oldest_age_seconds FROM processing_age), 0),
              'queued_due',
              COALESCE((SELECT due FROM due_queue),0),
              'queued_due_oldest_age_seconds',
              COALESCE((SELECT oldest_due_age_seconds FROM due_queue),0),
              'queued_max_attempts',
              COALESCE((SELECT max_attempts FROM due_queue),0),
              'fanout_active',
              COALESCE((SELECT active FROM active_fanout),0),
              'fanout_remaining',
              COALESCE((SELECT remaining FROM active_fanout),0),
              'fanout_oldest_delivery_age_seconds',
              COALESCE((SELECT oldest_delivery_age_seconds FROM active_fanout),0),
              'fanout_oldest_progress_age_seconds',
              COALESCE((SELECT oldest_progress_age_seconds FROM active_fanout),0),
              'retry_window_active',
              COALESCE((SELECT active FROM retry_window),0),
              'retry_window_exhausted_failed',
              COALESCE((SELECT exhausted_failed FROM retry_window),0),
              'auto_rearm_eligible_failed',
              COALESCE((SELECT auto_eligible_failed FROM retry_window),0),
              'auto_rearm_exhausted_failed',
              COALESCE((SELECT auto_exhausted_failed FROM retry_window),0)
            )
    FROM counts
$$;
ALTER FUNCTION core.webhook_delivery_depth_with_authority() OWNER TO veripsa_migrator;

-- rearm_failed_webhook_deliveries_with_authority: the DEAD-LETTER QUEUE re-arm (audit P1 — poison/transient-outage
-- rows were SILENTLY LOST). After a row exhausts its (now larger) durable attempt budget it goes 'failed'; from
-- there pending_webhook_deliveries_with_authority filters attempts<max so boot-recovery NEVER replays it, and
-- GitHub will not redeliver a 202'd event → the delivery is gone AND invisible. This is the controlled re-arm: a
-- 'failed' row OLDER than p_rearm_seconds (it has had time to be alerted on + the transient cause to clear) and
-- below the fixed automatic-epoch cap is requeued once — status→'queued', attempts→0, expiry→NULL, counter+1.
-- The aged-blocker mechanism spends this SAME counter, so a poison row cannot alternate mechanisms forever.
-- A second failure stays visible until an explicit GitHub redelivery resets the epoch. p_limit caps one sweep.
-- No tenant arg → cross-tenant by the App identity (the SAME owner-of-the-inbox
-- discipline as the other queue fns); the table has no per-account RLS. Returns {rearmed, failed_remaining}.
CREATE OR REPLACE FUNCTION core.rearm_failed_webhook_deliveries_with_authority(
    p_rearm_seconds int,
    p_limit int,
    p_max_auto_rearms int
) RETURNS jsonb
    LANGUAGE plpgsql SECURITY DEFINER SET search_path TO 'core','pg_catalog' AS $$
DECLARE
  v_rearmed int;
  v_remaining int;
  v_max_auto_rearms int := LEAST(GREATEST(COALESCE(p_max_auto_rearms,1),0),1);
BEGIN
  WITH due AS (
    SELECT delivery_key FROM core.webhook_delivery
     WHERE status='failed'
       AND updated_at < now() - make_interval(secs => GREATEST(COALESCE(p_rearm_seconds,3600),1))
       AND (event_type='ping' OR causal_order_version>=1)
       AND auto_rearm_count<v_max_auto_rearms
     ORDER BY updated_at
     LIMIT GREATEST(LEAST(COALESCE(p_limit,100),1000),1)
     FOR UPDATE SKIP LOCKED),
  rearmed AS (
    UPDATE core.webhook_delivery d
       SET status='queued', attempts=0, locked_at=NULL, owner_instance=NULL,
           not_before=NULL, retry_window_expires_at=NULL,
           auto_rearm_count=d.auto_rearm_count+1, updated_at=now()
      FROM due WHERE d.delivery_key=due.delivery_key
    RETURNING d.delivery_key)
  SELECT count(*)::int INTO v_rearmed FROM rearmed;
  SELECT count(*)::int INTO v_remaining FROM core.webhook_delivery WHERE status='failed';
  RETURN jsonb_build_object('rearmed', v_rearmed, 'failed_remaining', v_remaining);
END $$;
ALTER FUNCTION core.rearm_failed_webhook_deliveries_with_authority(int,int,int)
  OWNER TO veripsa_migrator;

-- Rolling worker shim. Automatic rearm is intentionally fixed to one epoch.
CREATE OR REPLACE FUNCTION core.rearm_failed_webhook_deliveries_with_authority(
    p_rearm_seconds int DEFAULT 3600,
    p_limit int DEFAULT 100
) RETURNS jsonb
    LANGUAGE sql SECURITY DEFINER SET search_path TO 'core','pg_catalog' AS $$
  SELECT core.rearm_failed_webhook_deliveries_with_authority(
    p_rearm_seconds,p_limit,1)
$$;
ALTER FUNCTION core.rearm_failed_webhook_deliveries_with_authority(int,int)
  OWNER TO veripsa_migrator;

-- Bounded operator recovery for GitHub deliveries which are already outside
-- the provider redelivery window. This is deliberately NOT a generic queue
-- writer: callers supply only canonical GitHub delivery GUIDs and an opaque
-- audit correlation id; the function can only requeue the payload already
-- stored behind the HMAC-verified ingress boundary. Each row has one lifetime
-- operator epoch, even if a later signed duplicate resets auto_rearm_count.
-- Results are positional but content-free: no GUID, recovery id, tenant,
-- repository, event, payload, timestamp, or error text leaves this function.
CREATE OR REPLACE FUNCTION core.recover_terminal_webhook_deliveries_with_authority(
    p_delivery_guids text[],
    p_recovery_id text
) RETURNS jsonb
    LANGUAGE plpgsql SECURITY DEFINER SET search_path TO 'core','pg_catalog' AS $$
DECLARE
  v_guid text;
  v_row core.webhook_delivery%ROWTYPE;
  v_result text;
  v_results jsonb := '[]'::jsonb;
  v_requested int;
  v_batch_token uuid;
  v_replay_tokens int := 0;
  v_candidates int := 0;
  v_rearmed int := 0;
  v_already_recovered int := 0;
  v_ineligible int := 0;
  v_missing int := 0;
BEGIN
  v_requested := COALESCE(cardinality(p_delivery_guids),0);
  IF p_delivery_guids IS NULL
     OR array_ndims(p_delivery_guids) IS DISTINCT FROM 1
     OR v_requested < 1 OR v_requested > 10
     OR p_recovery_id IS NULL
     OR p_recovery_id !~ '^[A-Za-z0-9][A-Za-z0-9._:-]{0,119}$'
     OR EXISTS (
       SELECT 1 FROM unnest(p_delivery_guids) AS supplied(guid)
        WHERE guid IS NULL
           OR guid !~ '^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$'
     )
     OR (SELECT count(DISTINCT supplied.guid)
           FROM unnest(p_delivery_guids) AS supplied(guid)) <> v_requested
  THEN
    RAISE EXCEPTION 'invalid durable delivery recovery request' USING ERRCODE='22023';
  END IF;

  -- Canonical lock order makes overlapping bounded operator requests
  -- deadlock-free while preserving the caller's order in the result array.
  PERFORM d.delivery_key
    FROM core.webhook_delivery d
   WHERE d.delivery_key=ANY(p_delivery_guids)
   ORDER BY d.delivery_key
   FOR UPDATE;

  -- Classify the WHOLE batch before mutation. A batch is one operator
  -- decision: it may not rearm an early row and only then discover that a
  -- later GUID was absent/ineligible. Candidate entries are provisionally
  -- reported as ineligible unless every requested row is a candidate.
  FOREACH v_guid IN ARRAY p_delivery_guids LOOP
    SELECT d.* INTO v_row
      FROM core.webhook_delivery d
     WHERE d.delivery_key=v_guid;
    IF NOT FOUND THEN
      v_result := 'missing';
      v_missing := v_missing+1;
    ELSIF v_row.operator_recovery_count=1 THEN
      IF v_row.operator_recovery_id=p_recovery_id
         AND v_row.operator_recovery_batch_size=v_requested
         AND v_row.operator_recovery_batch_token IS NOT NULL THEN
        v_result := 'already_recovered';
        v_already_recovered := v_already_recovered+1;
      ELSE
        v_result := 'ineligible';
        v_ineligible := v_ineligible+1;
      END IF;
    ELSIF v_row.status='failed'
          AND v_row.locked_at IS NULL
          AND v_row.owner_instance IS NULL
          AND v_row.auto_rearm_count>=1
          AND (v_row.event_type='ping' OR v_row.causal_order_version>=1)
          AND v_row.event_type<>'erased'
          AND jsonb_typeof(v_row.payload)='object'
          AND (v_row.event_type='ping' OR v_row.payload<>'{}'::jsonb)
    THEN
      v_result := 'ineligible';
      v_candidates := v_candidates+1;
      v_ineligible := v_ineligible+1;
    ELSE
      v_result := 'ineligible';
      v_ineligible := v_ineligible+1;
    END IF;
    v_results := v_results || jsonb_build_array(v_result);
  END LOOP;

  -- `recovery_id` is an operator correlation label, not a batch identity: a
  -- caller may accidentally reuse it. Treat already_recovered as exact only
  -- when EVERY requested row carries the same DB-generated opaque token.
  IF v_already_recovered>0 THEN
    SELECT count(DISTINCT d.operator_recovery_batch_token)::int
      INTO v_replay_tokens
      FROM core.webhook_delivery d
     WHERE d.delivery_key=ANY(p_delivery_guids)
       AND d.operator_recovery_count=1
       AND d.operator_recovery_id=p_recovery_id
       AND d.operator_recovery_batch_size=v_requested
       AND d.operator_recovery_batch_token IS NOT NULL;
    IF v_already_recovered<>v_requested OR v_replay_tokens<>1 THEN
      v_ineligible := v_ineligible+v_already_recovered;
      v_already_recovered := 0;
      SELECT COALESCE(
               jsonb_agg(to_jsonb(CASE WHEN item.value='already_recovered'
                                       THEN 'ineligible' ELSE item.value END)
                         ORDER BY item.ordinality),
               '[]'::jsonb)
        INTO v_results
        FROM jsonb_array_elements_text(v_results)
             WITH ORDINALITY AS item(value,ordinality);
    END IF;
  END IF;

  IF v_candidates=v_requested THEN
    v_batch_token := gen_random_uuid();
    UPDATE core.webhook_delivery d
       SET status='queued', attempts=0, locked_at=NULL,
           owner_instance=NULL, not_before=NULL,
           retry_window_expires_at=NULL, done_at=NULL,
           last_error=NULL, auto_rearm_count=2, updated_at=now(),
           operator_recovery_id=p_recovery_id,
           operator_recovered_at=clock_timestamp(),
           operator_recovery_batch_size=v_requested,
           operator_recovery_batch_token=v_batch_token,
           operator_recovery_count=1
     WHERE d.delivery_key=ANY(p_delivery_guids)
       AND d.status='failed'
       AND d.locked_at IS NULL
       AND d.owner_instance IS NULL
       AND d.auto_rearm_count>=1
       AND (d.event_type='ping' OR d.causal_order_version>=1)
       AND d.event_type<>'erased'
       AND jsonb_typeof(d.payload)='object'
       AND (d.event_type='ping' OR d.payload<>'{}'::jsonb)
       AND d.operator_recovery_count=0;
    GET DIAGNOSTICS v_rearmed = ROW_COUNT;
    IF v_rearmed<>v_requested THEN
      RAISE EXCEPTION 'durable delivery recovery batch changed during locked update'
        USING ERRCODE='40001';
    END IF;
    v_ineligible := 0;
    v_results := to_jsonb(array_fill('rearmed'::text,ARRAY[v_requested]));
  END IF;

  RETURN jsonb_build_object(
    'status','ok',
    'requested',v_requested,
    'rearmed',v_rearmed,
    'already_recovered',v_already_recovered,
    'ineligible',v_ineligible,
    'missing',v_missing,
    'results',v_results
  );
END $$;
ALTER FUNCTION core.recover_terminal_webhook_deliveries_with_authority(text[],text)
  OWNER TO veripsa_migrator;

-- One explicit forward-only continuation of an existing operator batch after
-- a reviewed exact-SHA correction. The original recovery id/token/count remain
-- immutable. Every member of that exact batch is locked and authenticated;
-- only failed members execute again, while members already completed after
-- the first recovery are stamped into the same continuation audit but never
-- replayed. The continuation audit is the ACK-loss/replay fence and survives a
-- later genuine signed GitHub duplicate.
CREATE OR REPLACE FUNCTION core.continue_terminal_webhook_deliveries_with_authority(
    p_delivery_guids text[],
    p_continuation_id text,
    p_exact_sha text
) RETURNS jsonb
    LANGUAGE plpgsql SECURITY DEFINER SET search_path TO 'core','pg_catalog' AS $$
DECLARE
  v_guid text;
  v_row core.webhook_delivery%ROWTYPE;
  v_result text;
  v_results jsonb := '[]'::jsonb;
  v_requested int;
  v_failed_candidates int := 0;
  v_completed_candidates int := 0;
  v_already_continued int := 0;
  v_ineligible int := 0;
  v_missing int := 0;
  v_batch_tokens int := 0;
  v_recovery_ids int := 0;
  v_continuation_ids int := 0;
  v_continuation_shas int := 0;
  v_updated int := 0;
  v_continued_at timestamptz;
BEGIN
  v_requested := COALESCE(cardinality(p_delivery_guids),0);
  IF p_delivery_guids IS NULL
     OR array_ndims(p_delivery_guids) IS DISTINCT FROM 1
     OR v_requested < 1 OR v_requested > 10
     OR p_continuation_id IS NULL
     OR p_continuation_id !~ '^[A-Za-z0-9][A-Za-z0-9._:-]{0,119}$'
     OR p_exact_sha IS NULL OR p_exact_sha !~ '^[0-9a-f]{40}$'
     OR EXISTS (
       SELECT 1 FROM unnest(p_delivery_guids) AS supplied(guid)
        WHERE guid IS NULL
           OR guid !~ '^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$'
     )
     OR (SELECT count(DISTINCT supplied.guid)
           FROM unnest(p_delivery_guids) AS supplied(guid)) <> v_requested
  THEN
    RAISE EXCEPTION 'invalid durable delivery continuation request' USING ERRCODE='22023';
  END IF;

  PERFORM d.delivery_key
    FROM core.webhook_delivery d
   WHERE d.delivery_key=ANY(p_delivery_guids)
   ORDER BY d.delivery_key
   FOR UPDATE;

  FOREACH v_guid IN ARRAY p_delivery_guids LOOP
    SELECT d.* INTO v_row
      FROM core.webhook_delivery d
     WHERE d.delivery_key=v_guid;
    IF NOT FOUND THEN
      v_result := 'missing';
      v_missing := v_missing+1;
    ELSIF v_row.operator_recovery_count<>1
          OR v_row.operator_recovery_batch_size<>v_requested
          OR v_row.operator_recovery_batch_token IS NULL
          OR v_row.operator_recovered_at IS NULL THEN
      v_result := 'ineligible';
      v_ineligible := v_ineligible+1;
    ELSIF v_row.operator_continuation_count=1
          AND v_row.operator_continuation_id=p_continuation_id
          AND v_row.operator_continuation_sha=p_exact_sha
          AND v_row.operator_continued_at IS NOT NULL THEN
      v_result := 'already_continued';
      v_already_continued := v_already_continued+1;
    ELSIF v_row.operator_continuation_count=0
          AND v_row.status='done'
          AND v_row.done_at IS NOT NULL
          AND v_row.done_at>=v_row.operator_recovered_at THEN
      v_result := 'already_completed';
      v_completed_candidates := v_completed_candidates+1;
    ELSIF v_row.operator_continuation_count=0
          AND v_row.status='failed'
          AND v_row.locked_at IS NULL
          AND v_row.owner_instance IS NULL
          AND v_row.auto_rearm_count=2
          AND (v_row.event_type='ping' OR v_row.causal_order_version>=1)
          AND v_row.event_type<>'erased'
          AND jsonb_typeof(v_row.payload)='object'
          AND (v_row.event_type='ping' OR v_row.payload<>'{}'::jsonb) THEN
      v_result := 'continued';
      v_failed_candidates := v_failed_candidates+1;
    ELSE
      v_result := 'ineligible';
      v_ineligible := v_ineligible+1;
    END IF;
    v_results := v_results || jsonb_build_array(v_result);
  END LOOP;

  -- Candidate labels are provisional until the complete exact set is
  -- authenticated. If any member is missing/ineligible, no mutation occurs
  -- and every present member is classified ineligible (never "continued" or
  -- "already_completed" for work that was not authorized).
  IF v_missing>0 OR v_ineligible>0 THEN
    v_ineligible:=v_ineligible+v_failed_candidates
                  +v_completed_candidates+v_already_continued;
    v_failed_candidates:=0;
    v_completed_candidates:=0;
    v_already_continued:=0;
    SELECT COALESCE(
             jsonb_agg(to_jsonb(CASE WHEN item.value='missing'
                                     THEN 'missing' ELSE 'ineligible' END)
                       ORDER BY item.ordinality),
             '[]'::jsonb)
      INTO v_results
      FROM jsonb_array_elements_text(v_results)
           WITH ORDINALITY AS item(value,ordinality);
  END IF;

  IF v_missing=0 AND v_ineligible=0 THEN
    SELECT count(DISTINCT d.operator_recovery_batch_token)::int,
           count(DISTINCT d.operator_recovery_id)::int,
           (count(DISTINCT d.operator_continuation_id)
             FILTER (WHERE d.operator_continuation_count=1))::int,
           (count(DISTINCT d.operator_continuation_sha)
             FILTER (WHERE d.operator_continuation_count=1))::int
      INTO v_batch_tokens,v_recovery_ids,v_continuation_ids,v_continuation_shas
      FROM core.webhook_delivery d
     WHERE d.delivery_key=ANY(p_delivery_guids);
    IF v_batch_tokens<>1 OR v_recovery_ids<>1 THEN
      v_ineligible := v_requested;
      v_already_continued := 0;
      v_failed_candidates := 0;
      v_completed_candidates := 0;
      v_results := to_jsonb(array_fill('ineligible'::text,ARRAY[v_requested]));
    ELSIF v_already_continued>0 THEN
      IF v_already_continued<>v_requested
         OR v_continuation_ids<>1 OR v_continuation_shas<>1 THEN
        v_ineligible := v_requested;
        v_already_continued := 0;
        v_failed_candidates := 0;
        v_completed_candidates := 0;
        v_results := to_jsonb(array_fill('ineligible'::text,ARRAY[v_requested]));
      END IF;
    ELSIF v_failed_candidates>0
          AND v_failed_candidates+v_completed_candidates=v_requested THEN
      -- Sample only after the canonical row locks and complete exact-set
      -- classification. A caller can wait behind a worker state transition;
      -- carrying a function-entry timestamp across that wait would regress
      -- both the continuation audit and updated_at behind the state we just
      -- observed. Keep this immediately adjacent to the atomic write.
      v_continued_at := clock_timestamp();
      UPDATE core.webhook_delivery d
         SET status=CASE WHEN d.status='failed' THEN 'queued' ELSE d.status END,
             attempts=CASE WHEN d.status='failed' THEN 0 ELSE d.attempts END,
             locked_at=CASE WHEN d.status='failed' THEN NULL ELSE d.locked_at END,
             owner_instance=CASE WHEN d.status='failed' THEN NULL ELSE d.owner_instance END,
             not_before=CASE WHEN d.status='failed' THEN NULL ELSE d.not_before END,
             retry_window_expires_at=CASE WHEN d.status='failed' THEN NULL
                                          ELSE d.retry_window_expires_at END,
             done_at=CASE WHEN d.status='failed' THEN NULL ELSE d.done_at END,
             last_error=CASE WHEN d.status='failed' THEN NULL ELSE d.last_error END,
             auto_rearm_count=CASE WHEN d.status='failed' THEN 3 ELSE d.auto_rearm_count END,
             updated_at=v_continued_at,
             operator_continuation_id=p_continuation_id,
             operator_continued_at=v_continued_at,
             operator_continuation_sha=p_exact_sha,
             operator_continuation_count=1
       WHERE d.delivery_key=ANY(p_delivery_guids)
         AND d.operator_recovery_count=1
         AND d.operator_recovery_batch_size=v_requested
         AND d.operator_recovery_batch_token IS NOT NULL
         AND d.operator_continuation_count=0
         AND (
           (d.status='done' AND d.done_at IS NOT NULL
            AND d.done_at>=d.operator_recovered_at)
           OR
           (d.status='failed' AND d.locked_at IS NULL
            AND d.owner_instance IS NULL AND d.auto_rearm_count=2
            AND (d.event_type='ping' OR d.causal_order_version>=1)
            AND d.event_type<>'erased' AND jsonb_typeof(d.payload)='object'
            AND (d.event_type='ping' OR d.payload<>'{}'::jsonb))
         );
      GET DIAGNOSTICS v_updated = ROW_COUNT;
      IF v_updated<>v_requested THEN
        RAISE EXCEPTION 'durable delivery continuation batch changed during locked update'
          USING ERRCODE='40001';
      END IF;
    ELSE
      v_ineligible := v_requested;
      v_failed_candidates := 0;
      v_completed_candidates := 0;
      v_results := to_jsonb(array_fill('ineligible'::text,ARRAY[v_requested]));
    END IF;
  END IF;

  RETURN jsonb_build_object(
    'status','ok',
    'requested',v_requested,
    'continued',v_failed_candidates,
    'already_completed',v_completed_candidates,
    'already_continued',v_already_continued,
    'ineligible',v_ineligible,
    'missing',v_missing,
    'results',v_results
  );
END $$;
ALTER FUNCTION core.continue_terminal_webhook_deliveries_with_authority(text[],text,text)
  OWNER TO veripsa_migrator;

-- Spend one member of the single approved three-delivery recovery batch before
-- the caller is allowed to issue GitHub's non-idempotent redelivery POST.  The
-- complete batch is locked and authenticated on every call.  Only two booleans
-- cross the App boundary; provider ids, GUIDs, audit tokens, and SHAs never do.
-- A committed spend is intentionally irreversible (except hard erasure): an
-- HTTP acknowledgement lost after the socket write must not authorize retry.
CREATE OR REPLACE FUNCTION core.claim_operator_github_redelivery_with_authority(
    p_delivery_guid text,
    p_recovery_id text,
    p_delivery_id bigint,
    p_exact_sha text
) RETURNS jsonb
    LANGUAGE plpgsql SECURITY DEFINER SET search_path TO 'core','pg_catalog' AS $$
DECLARE
  v_batch_rows int := 0;
  v_valid_recovery_rows int := 0;
  v_valid_continuation_rows int := 0;
  v_terminal_rows int := 0;
  v_batch_tokens int := 0;
  v_batch_token uuid;
  v_global_token_rows int := 0;
  v_continuation_ids int := 0;
  v_continuation_shas int := 0;
  v_target_rows int := 0;
  v_target_terminal_rows int := 0;
  v_prior_spends int := 0;
  v_spent_delivery_ids int := 0;
  v_spent_shas int := 0;
  v_matching_spent_shas int := 0;
  v_accepted_spends int := 0;
  v_duplicate_delivery_ids int := 0;
  v_target_spend_count smallint;
  v_updated int := 0;
BEGIN
  IF p_delivery_guid IS NULL
     OR p_delivery_guid !~ '^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$'
     OR p_recovery_id IS NULL
     OR p_recovery_id !~ '^[A-Za-z0-9][A-Za-z0-9._:-]{0,119}$'
     OR p_delivery_id IS NULL OR p_delivery_id<=0
     OR p_exact_sha IS NULL OR p_exact_sha !~ '^[0-9a-f]{40}$'
  THEN
    RAISE EXCEPTION 'invalid operator GitHub redelivery claim'
      USING ERRCODE='22023';
  END IF;

  -- A recovery id is only a correlation label, so lock every matching row and
  -- then prove it identifies exactly one DB-token-bound batch of three.
  PERFORM d.delivery_key
    FROM core.webhook_delivery d
   WHERE d.operator_recovery_id=p_recovery_id
   ORDER BY d.delivery_key
   FOR UPDATE;

  SELECT count(*)::int,
         count(*) FILTER (
           WHERE d.operator_recovery_count=1
             AND d.operator_recovery_batch_size=3
             AND d.operator_recovery_batch_token IS NOT NULL
             AND d.operator_recovered_at IS NOT NULL)::int,
         count(*) FILTER (
           WHERE d.operator_continuation_count=1
             AND d.operator_continuation_id IS NOT NULL
             AND d.operator_continued_at IS NOT NULL
             AND d.operator_continued_at>=d.operator_recovered_at
             AND d.operator_continuation_sha IS NOT NULL)::int,
         count(*) FILTER (
           WHERE (d.status='done'
                  AND d.done_at IS NOT NULL
                  AND d.operator_recovered_at IS NOT NULL
                  AND d.done_at>=d.operator_recovered_at
                  AND d.operator_continued_at IS NOT NULL
                  AND d.updated_at>=d.operator_continued_at)
              OR (d.status='failed'
                  AND d.locked_at IS NULL
                  AND d.owner_instance IS NULL
                  AND d.operator_continued_at IS NOT NULL
                  AND d.updated_at>=d.operator_continued_at))::int,
         count(DISTINCT d.operator_recovery_batch_token)::int,
         count(DISTINCT d.operator_continuation_id)::int,
         count(DISTINCT d.operator_continuation_sha)::int,
         count(*) FILTER (WHERE d.delivery_key=p_delivery_guid)::int,
         count(*) FILTER (
           WHERE d.delivery_key=p_delivery_guid
             AND ((d.status='done'
                   AND d.done_at IS NOT NULL
                   AND d.operator_recovered_at IS NOT NULL
                   AND d.done_at>=d.operator_recovered_at
                   AND d.operator_continued_at IS NOT NULL
                   AND d.updated_at>=d.operator_continued_at)
               OR (d.status='failed'
                   AND d.locked_at IS NULL
                   AND d.owner_instance IS NULL
                   AND d.operator_continued_at IS NOT NULL
                   AND d.updated_at>=d.operator_continued_at)))::int,
         count(*) FILTER (WHERE d.operator_github_redelivery_count=1)::int,
         (count(DISTINCT d.operator_github_redelivery_delivery_id)
           FILTER (WHERE d.operator_github_redelivery_count=1))::int,
         (count(DISTINCT d.operator_github_redelivery_sha)
           FILTER (WHERE d.operator_github_redelivery_count=1))::int,
         count(*) FILTER (
           WHERE d.operator_github_redelivery_count=1
             AND d.operator_github_redelivery_sha=p_exact_sha)::int,
         count(*) FILTER (
           WHERE d.operator_github_redelivery_count=1
             AND d.operator_github_redelivery_outcome='accepted')::int,
         count(*) FILTER (
           WHERE d.operator_github_redelivery_count=1
             AND d.operator_github_redelivery_delivery_id=p_delivery_id)::int
    INTO v_batch_rows,v_valid_recovery_rows,v_valid_continuation_rows,
         v_terminal_rows,v_batch_tokens,v_continuation_ids,
         v_continuation_shas,v_target_rows,v_target_terminal_rows,
         v_prior_spends,v_spent_delivery_ids,v_spent_shas,
         v_matching_spent_shas,v_accepted_spends,v_duplicate_delivery_ids
    FROM core.webhook_delivery d
   WHERE d.operator_recovery_id=p_recovery_id;

  IF v_batch_rows<>3
     OR v_valid_recovery_rows<>3
     OR v_valid_continuation_rows<>3
     OR v_batch_tokens<>1
     OR v_continuation_ids<>1
     OR v_continuation_shas<>1
     OR v_target_rows<>1
  THEN
    RETURN jsonb_build_object('claimed',false,'already_spent',false);
  END IF;

  SELECT d.operator_recovery_batch_token
    INTO v_batch_token
    FROM core.webhook_delivery d
   WHERE d.operator_recovery_id=p_recovery_id
   ORDER BY d.delivery_key
   LIMIT 1;
  -- The random token, not the caller's reusable recovery label, is the batch
  -- identity. Lock every occurrence across the table and prove the three
  -- authenticated local rows are its only bindings before returning existing
  -- spend state or creating a new one.
  PERFORM d.delivery_key
    FROM core.webhook_delivery d
   WHERE d.operator_recovery_batch_token=v_batch_token
   ORDER BY d.delivery_key
   FOR UPDATE;
  SELECT count(*)::int
    INTO v_global_token_rows
    FROM core.webhook_delivery d
   WHERE d.operator_recovery_batch_token=v_batch_token;
  IF v_global_token_rows<>3 THEN
    RETURN jsonb_build_object('claimed',false,'already_spent',false);
  END IF;

  SELECT d.operator_github_redelivery_count
    INTO v_target_spend_count
    FROM core.webhook_delivery d
   WHERE d.operator_recovery_id=p_recovery_id
     AND d.delivery_key=p_delivery_guid;

  -- Once spent, every later well-formed call is fenced, including a caller
  -- presenting a different provider id or SHA.  Never recreate POST authority.
  IF v_target_spend_count=1 THEN
    RETURN jsonb_build_object('claimed',false,'already_spent',true);
  END IF;

  -- The first spend proves that the complete old continuation batch is
  -- terminal. A signed receipt from that POST may immediately make its row
  -- queued/processing, so later members rely on the immutable spend ledger and
  -- require only their own old continuation to remain terminal. Every earlier
  -- spend must have a distinct provider id, the same deployed SHA, and a
  -- durably recorded acceptance; ambiguous/rejected work stops forward-only.
  IF (v_prior_spends=0 AND v_terminal_rows<>3)
     OR (v_prior_spends>0 AND v_target_terminal_rows<>1)
     OR (v_prior_spends>0 AND (
       v_spent_delivery_ids<>v_prior_spends
       OR v_spent_shas<>1
       OR v_matching_spent_shas<>v_prior_spends
       OR v_accepted_spends<>v_prior_spends
       OR v_duplicate_delivery_ids<>0))
  THEN
    RETURN jsonb_build_object('claimed',false,'already_spent',false);
  END IF;

  UPDATE core.webhook_delivery d
     SET operator_github_redelivery_delivery_id=p_delivery_id,
         operator_github_redelivery_sha=p_exact_sha,
         operator_github_redelivery_spent_at=clock_timestamp(),
         operator_github_redelivery_outcome=NULL,
         operator_github_redelivery_count=1
   WHERE d.operator_recovery_id=p_recovery_id
     AND d.delivery_key=p_delivery_guid
     AND d.operator_github_redelivery_count=0;
  GET DIAGNOSTICS v_updated = ROW_COUNT;
  IF v_updated<>1 THEN
    RAISE EXCEPTION 'operator GitHub redelivery claim changed during locked update'
      USING ERRCODE='40001';
  END IF;

  RETURN jsonb_build_object('claimed',true,'already_spent',false);
END $$;
ALTER FUNCTION core.claim_operator_github_redelivery_with_authority(text,text,bigint,text)
  OWNER TO veripsa_migrator;

-- Record only a fixed, content-free classification after the caller's one
-- permitted network attempt.  NULL -> value and value -> same value are the
-- only accepted transitions, making ACK recovery idempotent without allowing
-- a later run to rewrite evidence.
CREATE OR REPLACE FUNCTION core.record_operator_github_redelivery_outcome_with_authority(
    p_delivery_guid text,
    p_recovery_id text,
    p_exact_sha text,
    p_outcome text
) RETURNS boolean
    LANGUAGE plpgsql SECURITY DEFINER SET search_path TO 'core','pg_catalog' AS $$
DECLARE
  v_batch_rows int := 0;
  v_valid_recovery_rows int := 0;
  v_valid_continuation_rows int := 0;
  v_batch_tokens int := 0;
  v_batch_token uuid;
  v_global_token_rows int := 0;
  v_continuation_ids int := 0;
  v_continuation_shas int := 0;
  v_target_rows int := 0;
  v_spent_rows int := 0;
  v_spent_delivery_ids int := 0;
  v_spent_shas int := 0;
  v_matching_spent_shas int := 0;
  v_matching_target_spend_rows int := 0;
  v_updated int := 0;
  v_same boolean := false;
BEGIN
  IF p_delivery_guid IS NULL
     OR p_delivery_guid !~ '^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$'
     OR p_recovery_id IS NULL
     OR p_recovery_id !~ '^[A-Za-z0-9][A-Za-z0-9._:-]{0,119}$'
     OR p_exact_sha IS NULL OR p_exact_sha !~ '^[0-9a-f]{40}$'
     OR p_outcome IS NULL
     OR p_outcome NOT IN (
       'accepted','transport_ambiguous','redirect_rejected','auth_rejected',
       'rate_limited','request_rejected','server_rejected','unexpected_status')
  THEN
    RAISE EXCEPTION 'invalid operator GitHub redelivery outcome'
      USING ERRCODE='22023';
  END IF;

  PERFORM d.delivery_key
    FROM core.webhook_delivery d
   WHERE d.operator_recovery_id=p_recovery_id
   ORDER BY d.delivery_key
   FOR UPDATE;

  SELECT count(*)::int,
         count(*) FILTER (
           WHERE d.operator_recovery_count=1
             AND d.operator_recovery_batch_size=3
             AND d.operator_recovery_batch_token IS NOT NULL
             AND d.operator_recovered_at IS NOT NULL)::int,
         count(*) FILTER (
           WHERE d.operator_continuation_count=1
             AND d.operator_continuation_id IS NOT NULL
             AND d.operator_continued_at IS NOT NULL
             AND d.operator_continuation_sha IS NOT NULL)::int,
         count(DISTINCT d.operator_recovery_batch_token)::int,
         count(DISTINCT d.operator_continuation_id)::int,
         count(DISTINCT d.operator_continuation_sha)::int,
         count(*) FILTER (WHERE d.delivery_key=p_delivery_guid)::int,
         count(*) FILTER (WHERE d.operator_github_redelivery_count=1)::int,
         (count(DISTINCT d.operator_github_redelivery_delivery_id)
           FILTER (WHERE d.operator_github_redelivery_count=1))::int,
         (count(DISTINCT d.operator_github_redelivery_sha)
           FILTER (WHERE d.operator_github_redelivery_count=1))::int,
         count(*) FILTER (
           WHERE d.operator_github_redelivery_count=1
             AND d.operator_github_redelivery_sha=p_exact_sha)::int,
         count(*) FILTER (
           WHERE d.delivery_key=p_delivery_guid
             AND d.operator_github_redelivery_count=1
             AND d.operator_github_redelivery_delivery_id IS NOT NULL
             AND d.operator_github_redelivery_spent_at IS NOT NULL
             AND d.operator_github_redelivery_sha=p_exact_sha)::int
    INTO v_batch_rows,v_valid_recovery_rows,v_valid_continuation_rows,
         v_batch_tokens,v_continuation_ids,v_continuation_shas,v_target_rows,
         v_spent_rows,v_spent_delivery_ids,v_spent_shas,
         v_matching_spent_shas,v_matching_target_spend_rows
    FROM core.webhook_delivery d
   WHERE d.operator_recovery_id=p_recovery_id;

  IF v_batch_rows<>3
     OR v_valid_recovery_rows<>3
     OR v_valid_continuation_rows<>3
     OR v_batch_tokens<>1
     OR v_continuation_ids<>1
     OR v_continuation_shas<>1
     OR v_target_rows<>1
     OR v_spent_rows<1
     OR v_spent_delivery_ids<>v_spent_rows
     OR v_spent_shas<>1
     OR v_matching_spent_shas<>v_spent_rows
     OR v_matching_target_spend_rows<>1
  THEN
    RETURN false;
  END IF;

  SELECT d.operator_recovery_batch_token
    INTO v_batch_token
    FROM core.webhook_delivery d
   WHERE d.operator_recovery_id=p_recovery_id
   ORDER BY d.delivery_key
   LIMIT 1;
  PERFORM d.delivery_key
    FROM core.webhook_delivery d
   WHERE d.operator_recovery_batch_token=v_batch_token
   ORDER BY d.delivery_key
   FOR UPDATE;
  SELECT count(*)::int
    INTO v_global_token_rows
    FROM core.webhook_delivery d
   WHERE d.operator_recovery_batch_token=v_batch_token;
  IF v_global_token_rows<>3 THEN
    RETURN false;
  END IF;

  UPDATE core.webhook_delivery d
     SET operator_github_redelivery_outcome=p_outcome
   WHERE d.operator_recovery_id=p_recovery_id
     AND d.delivery_key=p_delivery_guid
     AND d.operator_github_redelivery_count=1
     AND d.operator_github_redelivery_delivery_id IS NOT NULL
     AND d.operator_github_redelivery_spent_at IS NOT NULL
     AND d.operator_github_redelivery_sha=p_exact_sha
     AND d.operator_github_redelivery_outcome IS NULL;
  GET DIAGNOSTICS v_updated = ROW_COUNT;
  IF v_updated=1 THEN
    RETURN true;
  END IF;

  SELECT count(*)=1
    INTO v_same
    FROM core.webhook_delivery d
   WHERE d.operator_recovery_id=p_recovery_id
     AND d.delivery_key=p_delivery_guid
     AND d.operator_github_redelivery_count=1
     AND d.operator_github_redelivery_delivery_id IS NOT NULL
     AND d.operator_github_redelivery_spent_at IS NOT NULL
     AND d.operator_github_redelivery_sha=p_exact_sha
     AND d.operator_github_redelivery_outcome=p_outcome;
  RETURN v_same;
END $$;
ALTER FUNCTION core.record_operator_github_redelivery_outcome_with_authority(text,text,text,text)
  OWNER TO veripsa_migrator;

-- Read one exact external-redelivery ledger through the live web role. The
-- recovery id is only a lookup label: the random DB batch token must still be
-- bound globally to exactly the three local rows. Only the fixed aggregate
-- and the already-persisted continuation authority cross the boundary; GUIDs,
-- provider delivery ids, tokens, payloads, timestamps, and row details do not.
CREATE OR REPLACE FUNCTION core.operator_github_redelivery_audit_with_authority(
    p_recovery_id text,
    p_redelivery_sha text,
    p_expected_continuation_id text,
    p_expected_continuation_sha text
) RETURNS jsonb
    LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path TO 'core','pg_catalog' AS $$
DECLARE
  v_batch_rows int := 0;
  v_valid_rows int := 0;
  v_batch_tokens int := 0;
  v_continuation_ids int := 0;
  v_continuation_shas int := 0;
  v_batch_token uuid;
  v_global_token_rows int := 0;
  v_continuation_id text;
  v_continuation_sha text;
  v_member record;
  v_spent_delivery_ids bigint[] := ARRAY[]::bigint[];
  v_unspent int := 0;
  v_spent int := 0;
  v_accepted int := 0;
  v_non_202 int := 0;
  v_ambiguous int := 0;
  v_saw_unspent boolean := false;
  v_saw_stop boolean := false;
  v_state text;
BEGIN
  IF p_recovery_id IS NULL
     OR p_recovery_id !~ '^deploy-core:[1-9][0-9]{0,19}$'
     OR p_redelivery_sha IS NULL
     OR p_redelivery_sha !~ '^[0-9a-f]{40}$'
     OR ((p_expected_continuation_id IS NULL)
         <> (p_expected_continuation_sha IS NULL))
     OR (p_expected_continuation_id IS NOT NULL
         AND p_expected_continuation_id
             !~ '^deploy-core:[1-9][0-9]{0,19}$')
     OR (p_expected_continuation_sha IS NOT NULL
         AND p_expected_continuation_sha !~ '^[0-9a-f]{40}$')
  THEN
    RAISE EXCEPTION 'invalid operator GitHub redelivery audit request'
      USING ERRCODE='22023';
  END IF;

  SELECT count(*)::int,
         count(*) FILTER (
           WHERE d.event_type<>'erased'
             AND d.delivery_key
                 ~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
             AND d.operator_recovery_count=1
             AND d.operator_recovery_batch_size=3
             AND d.operator_recovery_batch_token IS NOT NULL
             AND d.operator_recovered_at IS NOT NULL
             AND d.operator_continuation_count=1
             AND d.operator_continuation_id
                 ~ '^deploy-core:[1-9][0-9]{0,19}$'
             AND d.operator_continued_at IS NOT NULL
             AND d.operator_continued_at>=d.operator_recovered_at
             AND d.operator_continuation_sha
                 ~ '^[0-9a-f]{40}$')::int,
         count(DISTINCT d.operator_recovery_batch_token)::int,
         count(DISTINCT d.operator_continuation_id)::int,
         count(DISTINCT d.operator_continuation_sha)::int
    INTO v_batch_rows,v_valid_rows,v_batch_tokens,
         v_continuation_ids,v_continuation_shas
    FROM core.webhook_delivery d
   WHERE d.operator_recovery_id=p_recovery_id;

  IF v_batch_rows<>3
     OR v_valid_rows<>3
     OR v_batch_tokens<>1
     OR v_continuation_ids<>1
     OR v_continuation_shas<>1
  THEN
    RETURN jsonb_build_object('status','unverified');
  END IF;

  SELECT d.operator_recovery_batch_token,
         d.operator_continuation_id,
         d.operator_continuation_sha
    INTO v_batch_token,v_continuation_id,v_continuation_sha
    FROM core.webhook_delivery d
   WHERE d.operator_recovery_id=p_recovery_id
   ORDER BY d.delivery_key
   LIMIT 1;

  SELECT count(*)::int
    INTO v_global_token_rows
    FROM core.webhook_delivery d
   WHERE d.operator_recovery_batch_token=v_batch_token;
  IF v_global_token_rows<>3
     OR (p_expected_continuation_id IS NOT NULL
         AND (v_continuation_id IS DISTINCT FROM p_expected_continuation_id
              OR v_continuation_sha
                 IS DISTINCT FROM p_expected_continuation_sha))
  THEN
    RETURN jsonb_build_object('status','unverified');
  END IF;

  FOR v_member IN
    SELECT d.status,d.done_at,d.updated_at,d.owner_instance,d.locked_at,
           d.operator_recovered_at,d.operator_continued_at,
           d.operator_github_redelivery_count,
           d.operator_github_redelivery_delivery_id,
           d.operator_github_redelivery_sha,
           d.operator_github_redelivery_spent_at,
           d.operator_github_redelivery_outcome
      FROM core.webhook_delivery d
     WHERE d.operator_recovery_id=p_recovery_id
     ORDER BY d.delivery_key
  LOOP
    IF v_member.operator_github_redelivery_count=0 THEN
      IF v_member.operator_github_redelivery_delivery_id IS NOT NULL
         OR v_member.operator_github_redelivery_sha IS NOT NULL
         OR v_member.operator_github_redelivery_spent_at IS NOT NULL
         OR v_member.operator_github_redelivery_outcome IS NOT NULL
         OR v_member.owner_instance IS NOT NULL
         OR v_member.locked_at IS NOT NULL
         OR NOT (
           (v_member.status='done'
            AND v_member.done_at IS NOT NULL
            AND v_member.operator_recovered_at IS NOT NULL
            AND v_member.done_at>=v_member.operator_recovered_at
            AND v_member.updated_at>=v_member.operator_continued_at)
           OR
           (v_member.status='failed'
            AND v_member.updated_at IS NOT NULL
            AND v_member.updated_at>=v_member.operator_continued_at)
         )
      THEN
        RETURN jsonb_build_object('status','unverified');
      END IF;
      v_saw_unspent := true;
      v_unspent := v_unspent+1;
      CONTINUE;
    END IF;

    IF v_member.operator_github_redelivery_count<>1
       OR v_saw_unspent
       OR v_saw_stop
       OR v_member.operator_github_redelivery_delivery_id IS NULL
       OR v_member.operator_github_redelivery_delivery_id<=0
       OR v_member.operator_github_redelivery_delivery_id
          =ANY(v_spent_delivery_ids)
       OR v_member.operator_github_redelivery_sha
          IS DISTINCT FROM p_redelivery_sha
       OR v_member.operator_github_redelivery_spent_at IS NULL
       OR v_member.operator_github_redelivery_spent_at
          <v_member.operator_continued_at
       OR (v_member.operator_github_redelivery_outcome IS NOT NULL
           AND v_member.operator_github_redelivery_outcome NOT IN (
             'accepted','transport_ambiguous','redirect_rejected',
             'auth_rejected','rate_limited','request_rejected',
             'server_rejected','unexpected_status'))
    THEN
      RETURN jsonb_build_object('status','unverified');
    END IF;

    v_spent_delivery_ids := array_append(
      v_spent_delivery_ids,
      v_member.operator_github_redelivery_delivery_id
    );
    v_spent := v_spent+1;
    IF v_member.operator_github_redelivery_outcome='accepted' THEN
      v_accepted := v_accepted+1;
    ELSIF v_member.operator_github_redelivery_outcome IN (
        'redirect_rejected','auth_rejected','rate_limited',
        'request_rejected','server_rejected','unexpected_status') THEN
      v_non_202 := v_non_202+1;
      v_saw_stop := true;
    ELSIF v_member.operator_github_redelivery_outcome IS NULL
          OR v_member.operator_github_redelivery_outcome=
             'transport_ambiguous' THEN
      v_ambiguous := v_ambiguous+1;
      v_saw_stop := true;
    ELSE
      RETURN jsonb_build_object('status','unverified');
    END IF;
  END LOOP;

  IF v_unspent+v_spent<>3
     OR v_accepted+v_non_202+v_ambiguous<>v_spent
     OR v_non_202+v_ambiguous>1
  THEN
    RETURN jsonb_build_object('status','unverified');
  END IF;

  IF v_ambiguous=1 THEN
    v_state := 'ambiguous';
  ELSIF v_non_202=1 THEN
    v_state := 'rejected';
  ELSIF v_spent=0 THEN
    v_state := 'pre-spend-unclassified';
  ELSIF v_accepted=3 THEN
    v_state := 'accepted';
  ELSIF v_accepted>0 AND v_accepted<3 AND v_accepted=v_spent THEN
    v_state := 'partial-accepted';
  ELSE
    RETURN jsonb_build_object('status','unverified');
  END IF;

  RETURN jsonb_build_object(
    'status','ok',
    'state',v_state,
    'total',3,
    'unspent',v_unspent,
    'spent',v_spent,
    'accepted',v_accepted,
    'non_202',v_non_202,
    'ambiguous',v_ambiguous,
    'continuation_id',v_continuation_id,
    'continuation_sha',v_continuation_sha
  );
END $$;
ALTER FUNCTION core.operator_github_redelivery_audit_with_authority(text,text,text,text)
  OWNER TO veripsa_migrator;

-- Durable, read-only proof for an ACK-unknown operator recovery. Process-local
-- delivery receipts disappear on restart, but this audit and the row state are
-- committed atomically with the one-shot recovery. Return only one aggregate
-- classification and the requested count: no GUID, recovery id, tenant,
-- repository, payload, timestamp, or error text crosses the App boundary.
CREATE OR REPLACE FUNCTION core.terminal_webhook_delivery_recovery_status_with_authority(
    p_delivery_guids text[]
) RETURNS jsonb
    LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path TO 'core','pg_catalog' AS $$
DECLARE
  v_requested int;
  v_existing int := 0;
  v_never_recovered int := 0;
  v_safe_unspent int := 0;
  v_recovered int := 0;
  v_recovery_epochs int := 0;
  v_recovery_batches int := 0;
  v_never_recovered_done int := 0;
  v_active int := 0;
  v_valid_done int := 0;
  v_failed int := 0;
  v_continuation_eligible_failed int := 0;
  v_continuation_unspent int := 0;
  v_continued int := 0;
  v_continuation_ids int := 0;
  v_continuation_shas int := 0;
  v_status text := 'unverified';
BEGIN
  v_requested := COALESCE(cardinality(p_delivery_guids),0);
  IF p_delivery_guids IS NULL
     OR array_ndims(p_delivery_guids) IS DISTINCT FROM 1
     OR v_requested < 1 OR v_requested > 10
     OR EXISTS (
       SELECT 1 FROM unnest(p_delivery_guids) AS supplied(guid)
        WHERE guid IS NULL
           OR guid !~ '^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$'
     )
     OR (SELECT count(DISTINCT supplied.guid)
           FROM unnest(p_delivery_guids) AS supplied(guid)) <> v_requested
  THEN
    RAISE EXCEPTION 'invalid durable delivery recovery status request' USING ERRCODE='22023';
  END IF;

  -- One aggregate statement gives the whole requested set one MVCC snapshot.
  -- Stored opaque recovery ids and random batch tokens are used only for
  -- equality inside PostgreSQL; neither is supplied by a later workflow run
  -- nor returned to the App.
  SELECT count(*)::int,
         count(*) FILTER (WHERE d.operator_recovery_count=0)::int,
         count(*) FILTER (
           WHERE d.operator_recovery_count=0
             AND d.event_type<>'erased')::int,
         count(*) FILTER (
           WHERE d.operator_recovery_count=1
             AND d.operator_recovery_batch_size=v_requested
             AND d.operator_recovery_batch_token IS NOT NULL)::int,
         count(DISTINCT d.operator_recovery_id)
           FILTER (
             WHERE d.operator_recovery_count=1
               AND d.operator_recovery_batch_size=v_requested
               AND d.operator_recovery_batch_token IS NOT NULL)::int,
         count(DISTINCT d.operator_recovery_batch_token)
           FILTER (
             WHERE d.operator_recovery_count=1
               AND d.operator_recovery_batch_size=v_requested
               AND d.operator_recovery_batch_token IS NOT NULL)::int,
         count(*) FILTER (
           WHERE d.operator_recovery_count=0
             AND d.status='done' AND d.done_at IS NOT NULL
             -- Hard erasure intentionally clears the operator audit while
             -- retaining an opaque done receipt. That is lost evidence, not
             -- proof that no recovery epoch ever existed.
             AND d.event_type<>'erased')::int,
         count(*) FILTER (
           WHERE d.operator_recovery_count=1
             AND d.operator_recovery_batch_size=v_requested
             AND d.operator_recovery_batch_token IS NOT NULL
             AND d.status IN ('queued','processing'))::int,
         count(*) FILTER (
           WHERE d.operator_recovery_count=1
             AND d.operator_recovery_batch_size=v_requested
             AND d.operator_recovery_batch_token IS NOT NULL
             AND d.status='done'
             AND d.done_at IS NOT NULL
             AND d.operator_recovered_at IS NOT NULL
             AND d.done_at>=d.operator_recovered_at)::int,
         count(*) FILTER (
           WHERE d.operator_recovery_count=1
             AND d.operator_recovery_batch_size=v_requested
             AND d.operator_recovery_batch_token IS NOT NULL
             AND d.status='failed')::int,
         count(*) FILTER (
           WHERE d.operator_recovery_count=1
             AND d.operator_recovery_batch_size=v_requested
             AND d.operator_recovery_batch_token IS NOT NULL
             AND d.operator_continuation_count=0
             AND d.status='failed'
             AND d.locked_at IS NULL AND d.owner_instance IS NULL
             AND d.auto_rearm_count=2
             AND (d.event_type='ping' OR d.causal_order_version>=1)
             AND d.event_type<>'erased'
             AND jsonb_typeof(d.payload)='object'
             AND (d.event_type='ping' OR d.payload<>'{}'::jsonb))::int,
         count(*) FILTER (
           WHERE d.operator_recovery_count=1
             AND d.operator_recovery_batch_size=v_requested
             AND d.operator_recovery_batch_token IS NOT NULL
             AND d.operator_continuation_count=0)::int,
         count(*) FILTER (
           WHERE d.operator_recovery_count=1
             AND d.operator_recovery_batch_size=v_requested
             AND d.operator_recovery_batch_token IS NOT NULL
             AND d.operator_continuation_count=1
             AND d.operator_continuation_id IS NOT NULL
             AND d.operator_continued_at IS NOT NULL
             AND d.operator_continuation_sha IS NOT NULL)::int,
         (count(DISTINCT d.operator_continuation_id) FILTER (
           WHERE d.operator_continuation_count=1))::int,
         (count(DISTINCT d.operator_continuation_sha) FILTER (
           WHERE d.operator_continuation_count=1))::int
    INTO v_existing,v_never_recovered,v_safe_unspent,
         v_recovered,v_recovery_epochs,v_recovery_batches,
         v_never_recovered_done,v_active,v_valid_done,v_failed,
         v_continuation_eligible_failed,
         v_continuation_unspent,v_continued,
         v_continuation_ids,v_continuation_shas
    FROM core.webhook_delivery d
   WHERE d.delivery_key=ANY(p_delivery_guids);

  IF v_existing<>v_requested THEN
    v_status := 'unverified';
  ELSIF v_never_recovered=v_requested THEN
    v_status := CASE
      WHEN v_safe_unspent<>v_requested THEN 'unverified'
      WHEN v_never_recovered_done=v_requested THEN 'never_recovered'
      -- No operator epoch exists, but the exact rows are not all complete.
      -- This state may proceed to the mutation endpoint's authoritative
      -- all-or-zero eligibility check; it does not grant rollback authority.
      ELSE 'unspent'
    END;
  ELSIF v_recovered=v_requested
        AND v_recovery_epochs=1 AND v_recovery_batches=1 THEN
    IF v_failed>0 THEN
      v_status := CASE
        WHEN v_continuation_unspent=v_requested
             AND v_active=0
             AND v_continuation_eligible_failed=v_failed
             AND v_failed+v_valid_done=v_requested
          THEN 'continuable'
        WHEN v_continued=v_requested
             AND v_continuation_ids=1 AND v_continuation_shas=1
          THEN 'continuation_failed'
        ELSE 'failed'
      END;
    ELSIF v_valid_done=v_requested THEN
      v_status := 'verified';
    ELSIF v_active>0 AND v_active+v_valid_done=v_requested THEN
      v_status := 'recovering';
    ELSE
      -- Includes missing/invalid done_at evidence and any future row state.
      v_status := 'unverified';
    END IF;
  ELSE
    -- Mixed audit counts or different stored recovery epochs are deliberately
    -- indistinguishable from every other unverifiable inventory.
    v_status := 'unverified';
  END IF;

  RETURN jsonb_build_object(
    'status',v_status,
    'requested',v_requested
  );
END $$;
ALTER FUNCTION core.terminal_webhook_delivery_recovery_status_with_authority(text[])
  OWNER TO veripsa_migrator;

-- escalate_blocked_webhook_deliveries_with_authority (issue #847 — aged-deferred lane liveness). A live claim
-- that answers 'blocked_by_earlier' leaves the delivery 'queued' with NO comeback path of its own: the in-memory
-- worker drops its generation, GitHub never redelivers a 202'd delivery, the App-level redelivery scan classifies
-- it locally-received/OK (never a candidate), and pending() offers only causal lane HEADS — so its replay is
-- entirely hostage to the lane head resolving. When that head ends 'failed' (protocol 1; e.g. a blocker whose
-- durable budget was burned by restart-reclaim churn), the ONLY unfreeze was the slow DLQ re-arm (default 1h;
-- 0 = never) — the deferred delivery behind it sat silently unprocessed for that whole window, or forever.
-- This bounded sweep closes the gap ORDER-PRESERVINGLY: when a due 'queued' row has itself been waiting longer
-- than p_escalate_seconds, re-arm the aged protocol-1 'failed' rows in its account/repository lane NOW (the same
-- action the DLQ sweep would take later) so the lane drains in causal order — never by replaying the deferred row
-- out of order. Deliberately NEVER touches 'done' (a finished delivery stays finished), never touches
-- 'processing' (the stale window remains the double-execution guard), never re-arms quarantined protocol-0
-- failures. A poison head is rate-bounded by the fixed auto_rearm_count<1 epoch, so waiting a second full age after
-- it fails only prolongs the lane outage and cannot add safety. Its failed-predecessor predicate
-- is the same directional relationship as pending() and claim(), including stable-id lanes across a rename.
-- Content-free: only status/attempt metadata changes and only counts are returned.
CREATE OR REPLACE FUNCTION core.escalate_blocked_webhook_deliveries_with_authority(
    p_escalate_seconds int,
    p_limit int,
    p_max_auto_rearms int
) RETURNS jsonb
    LANGUAGE plpgsql SECURITY DEFINER SET search_path TO 'core','pg_catalog' AS $$
DECLARE
  -- floor 60s: a typo'd tiny knob must not hot-loop fresh-failed heads back into the budget every tick.
  v_escalate int := GREATEST(COALESCE(p_escalate_seconds,120),60);
  v_limit int := GREATEST(LEAST(COALESCE(p_limit,100),1000),1);
  v_max_auto_rearms int := LEAST(GREATEST(COALESCE(p_max_auto_rearms,1),0),1);
  v_aged int; v_escalated int;
BEGIN
  WITH aged AS MATERIALIZED (
    SELECT d.delivery_key, d.account_key, d.received_at, d.event_type,
           core._webhook_delivery_repository_lane(d.event_type,d.payload) AS repository_id
      FROM core.webhook_delivery d
     WHERE d.status='queued'
       AND (d.not_before IS NULL OR d.not_before<=now())
       AND d.received_at < now()-make_interval(secs=>v_escalate)
  ), blockers AS MATERIALIZED (
    SELECT f.delivery_key
      FROM core.webhook_delivery f
     WHERE f.status='failed'
       AND (f.causal_order_version>=1 OR f.event_type='ping')
       AND f.auto_rearm_count<v_max_auto_rearms
       AND EXISTS (
         SELECT 1 FROM aged a
          WHERE (f.received_at,f.delivery_key)<(a.received_at,a.delivery_key)
            AND (
              (a.account_key IS NOT NULL
               AND COALESCE(f.account_key,'')=COALESCE(a.account_key,'')
               AND (
                 a.event_type IN ('installation','installation_repositories')
                 OR f.event_type IN ('installation','installation_repositories')
                 OR f.event_type='marketplace_purchase'
                 OR (
                   f.event_type IN (
                     'repository','pull_request','push','check_suite','check_run','merge_group')
                   AND a.event_type IN (
                     'repository','pull_request','push','check_suite','check_run','merge_group')
                   AND (
                     core._webhook_delivery_repository_lane(f.event_type,f.payload) IS NULL
                     OR a.repository_id IS NULL
                     OR core._webhook_delivery_repository_lane(f.event_type,f.payload)=a.repository_id
                   )
                 )
                 OR f.event_type NOT IN (
                   'installation','installation_repositories','marketplace_purchase','repository',
                   'pull_request','push','check_suite','check_run','merge_group')
                 OR a.event_type NOT IN (
                   'installation','installation_repositories','marketplace_purchase','repository',
                   'pull_request','push','check_suite','check_run','merge_group')
               ))
              OR
              (a.repository_id IS NOT NULL
               AND core._webhook_delivery_repository_lane(f.event_type,f.payload)=a.repository_id)
            ))
     ORDER BY f.received_at, f.delivery_key
     LIMIT v_limit
     FOR UPDATE SKIP LOCKED
  ), rearmed AS (
    UPDATE core.webhook_delivery d
       SET status='queued', attempts=0, locked_at=NULL, owner_instance=NULL,
           not_before=NULL, retry_window_expires_at=NULL,
           auto_rearm_count=d.auto_rearm_count+1, updated_at=now(),
           last_error=left('re-armed early: failed lane head was blocking aged queued work',300)
      FROM blockers b WHERE d.delivery_key=b.delivery_key
    RETURNING d.delivery_key)
  SELECT (SELECT count(*)::int FROM aged), count(*)::int INTO v_aged, v_escalated FROM rearmed;
  RETURN jsonb_build_object('aged_queued', v_aged, 'escalated', v_escalated);
END $$;
ALTER FUNCTION core.escalate_blocked_webhook_deliveries_with_authority(int,int,int)
  OWNER TO veripsa_migrator;

-- Rolling worker shim. A blocked poison head receives at most the same one
-- automatic epoch as the slow DLQ sweep, never one epoch per mechanism.
CREATE OR REPLACE FUNCTION core.escalate_blocked_webhook_deliveries_with_authority(
    p_escalate_seconds int DEFAULT 120,
    p_limit int DEFAULT 100
) RETURNS jsonb
    LANGUAGE sql SECURITY DEFINER SET search_path TO 'core','pg_catalog' AS $$
  SELECT core.escalate_blocked_webhook_deliveries_with_authority(
    p_escalate_seconds,p_limit,1)
$$;
ALTER FUNCTION core.escalate_blocked_webhook_deliveries_with_authority(int,int)
  OWNER TO veripsa_migrator;

-- GitHub keeps failed GitHub-App webhook deliveries for only three days and never retries them automatically.
-- This is the content-free control plane for Core's low-priority recovery loop.  It stores only GitHub delivery
-- ids/GUIDs, timestamps, status codes, a bounded status enum, cursors and retry counters.  Event/action names,
-- repository/account metadata and webhook bodies are deliberately absent.  Marketplace and Sponsors webhooks are
-- not exposed by the GitHub-App delivery API and therefore remain outside this recovery path.
-- Relations, additive columns, owner/ACL, and concurrent indexes were expanded
-- before BEGIN. Only the recovery API is published in this transaction.

CREATE OR REPLACE FUNCTION core.begin_github_delivery_recovery_scan_with_authority()
RETURNS jsonb
    LANGUAGE plpgsql SECURITY DEFINER SET search_path TO 'core','pg_catalog' AS $$
DECLARE v_row core.github_delivery_recovery_scan%ROWTYPE;
BEGIN
  INSERT INTO core.github_delivery_recovery_scan(singleton) VALUES (true)
  ON CONFLICT (singleton) DO NOTHING;
  SELECT * INTO v_row FROM core.github_delivery_recovery_scan WHERE singleton FOR UPDATE;
  IF NOT v_row.in_progress THEN
    UPDATE core.github_delivery_recovery_scan
       SET scan_epoch=scan_epoch+1, in_progress=true, cursor=NULL,
           scan_ceiling_delivery_id=NULL,page_tail_delivery_id=NULL,page_tail_delivered_at=NULL,
           cutoff_at=now()-interval '3 days',
           -- Until the first successful completion, preserve the ORIGINAL start across invalid-cursor resets so
           -- repeated restart/reclaim cannot reset the 6h/48h lag clock.  After any success, each new scan records
           -- its own start while lag remains anchored to scan_completed_at below.
           scan_started_at=CASE WHEN scan_completed_at IS NULL
                                THEN COALESCE(scan_started_at,now()) ELSE now() END,
           updated_at=now()
     WHERE singleton RETURNING * INTO v_row;
  END IF;
  RETURN jsonb_build_object(
    'epoch',v_row.scan_epoch, 'cursor',v_row.cursor,
    'high_water_delivery_id',v_row.high_water_delivery_id,
    'page_tail_delivery_id',v_row.page_tail_delivery_id,
    'page_tail_delivered_at',v_row.page_tail_delivered_at,
    'cutoff_at',v_row.cutoff_at, 'started_at',v_row.scan_started_at);
END $$;
ALTER FUNCTION core.begin_github_delivery_recovery_scan_with_authority() OWNER TO veripsa_migrator;

CREATE OR REPLACE FUNCTION core.observe_github_delivery_with_authority(
    p_epoch bigint,
    p_delivery_id bigint,
    p_delivery_guid text,
    p_delivered_at timestamptz,
    p_status_code int,
    p_status text
) RETURNS boolean
    LANGUAGE plpgsql SECURITY DEFINER SET search_path TO 'core','pg_catalog' AS $$
DECLARE v_class text; v_resolved boolean;
BEGIN
  PERFORM 1 FROM core.github_delivery_recovery_scan
   WHERE singleton AND in_progress AND scan_epoch=p_epoch FOR UPDATE;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'stale GitHub delivery scan epoch' USING ERRCODE='40001';
  END IF;
  IF p_delivery_id IS NULL OR p_delivery_id <= 0
     OR p_delivery_guid IS NULL OR length(p_delivery_guid) NOT BETWEEN 1 AND 200
     OR p_delivery_guid !~ '^[A-Za-z0-9_-]+$'
     OR p_delivered_at IS NULL OR p_delivered_at > now()+interval '5 minutes'
     OR p_status_code IS NULL OR p_status_code NOT BETWEEN -1 AND 599
     OR p_status IS NULL OR p_status NOT IN ('OK','Timed Out','Other','Unknown') THEN
    RAISE EXCEPTION 'malformed GitHub delivery metadata' USING ERRCODE='22023';
  END IF;
  -- GitHub's delivery API classifies status_code 200..399 as success, while its failed-delivery guidance uses
  -- status != OK. Require both pieces so a well-formed redirect succeeds but an inconsistent OK/code fails closed.
  v_resolved := p_status='OK' AND p_status_code BETWEEN 200 AND 399;
  v_class := CASE
    WHEN v_resolved THEN 'resolved'
    -- Exact enum from GitHub's delivery-list timeout fixture.  No substring/fuzzy match: unknown status fails closed.
    WHEN p_status_code=0 AND p_status='Timed Out' THEN 'timeout'
    -- GitHub defines every explicit non-OK delivery as failed. A temporarily omitted/malformed code is normalized
    -- to -1 and remains boundedly redeliverable; only Unknown or an inconsistent OK/code is terminal.
    WHEN p_status IN ('Other','Timed Out') AND p_status_code BETWEEN -1 AND 599 THEN 'redeliverable'
    ELSE 'terminal' END;

  INSERT INTO core.github_delivery_recovery(
      delivery_guid,latest_delivery_id,latest_delivered_at,latest_status_code,latest_status,
      latest_recovery_class,resolved,window_expires_at)
  VALUES (p_delivery_guid,p_delivery_id,p_delivered_at,p_status_code,p_status,v_class,v_resolved,
          p_delivered_at+interval '3 days')
  ON CONFLICT (delivery_guid) DO UPDATE SET
      -- The list is newest-first. Keep the newest attempt as display/retry authority, but do not use that ordering
      -- predicate as a WHERE clause: an OLDER successful attempt for the same GUID is still proof that GitHub
      -- reached us and must be absorbed below. A WHERE here would silently discard that success.
      latest_delivery_id=CASE WHEN
        (EXCLUDED.latest_delivered_at,EXCLUDED.latest_delivery_id) >=
        (core.github_delivery_recovery.latest_delivered_at,core.github_delivery_recovery.latest_delivery_id)
        THEN EXCLUDED.latest_delivery_id ELSE core.github_delivery_recovery.latest_delivery_id END,
      latest_delivered_at=CASE WHEN
        (EXCLUDED.latest_delivered_at,EXCLUDED.latest_delivery_id) >=
        (core.github_delivery_recovery.latest_delivered_at,core.github_delivery_recovery.latest_delivery_id)
        THEN EXCLUDED.latest_delivered_at ELSE core.github_delivery_recovery.latest_delivered_at END,
      latest_status_code=CASE WHEN
        (EXCLUDED.latest_delivered_at,EXCLUDED.latest_delivery_id) >=
        (core.github_delivery_recovery.latest_delivered_at,core.github_delivery_recovery.latest_delivery_id)
        THEN EXCLUDED.latest_status_code ELSE core.github_delivery_recovery.latest_status_code END,
      latest_status=CASE WHEN
        (EXCLUDED.latest_delivered_at,EXCLUDED.latest_delivery_id) >=
        (core.github_delivery_recovery.latest_delivered_at,core.github_delivery_recovery.latest_delivery_id)
        THEN EXCLUDED.latest_status ELSE core.github_delivery_recovery.latest_status END,
      latest_recovery_class=CASE WHEN
        (EXCLUDED.latest_delivered_at,EXCLUDED.latest_delivery_id) >=
        (core.github_delivery_recovery.latest_delivered_at,core.github_delivery_recovery.latest_delivery_id)
        THEN EXCLUDED.latest_recovery_class ELSE core.github_delivery_recovery.latest_recovery_class END,
      -- Success/local receipt is sticky for a GUID: neither an older page nor a later failed redelivery attempt
      -- can reopen work already proven delivered. This assignment intentionally runs for BOTH older and newer rows.
      resolved=core.github_delivery_recovery.resolved
               OR core.github_delivery_recovery.locally_received OR EXCLUDED.resolved,
      window_expires_at=CASE WHEN
        (EXCLUDED.latest_delivered_at,EXCLUDED.latest_delivery_id) >=
        (core.github_delivery_recovery.latest_delivered_at,core.github_delivery_recovery.latest_delivery_id)
        THEN EXCLUDED.window_expires_at ELSE core.github_delivery_recovery.window_expires_at END,
      last_seen_at=now();

  UPDATE core.github_delivery_recovery_scan
     SET scan_ceiling_delivery_id=GREATEST(COALESCE(scan_ceiling_delivery_id,p_delivery_id),p_delivery_id),
         updated_at=now()
   WHERE singleton AND scan_epoch=p_epoch AND in_progress;
  RETURN true;
END $$;
ALTER FUNCTION core.observe_github_delivery_with_authority(bigint,bigint,text,timestamptz,int,text)
  OWNER TO veripsa_migrator;

-- Remove the pre-CAS development signatures if this module was applied from an earlier build. Leaving either
-- callable would preserve a bypass around expected-cursor authority even though the runtime uses the new form.
DROP FUNCTION IF EXISTS core.advance_github_delivery_recovery_scan_with_authority(bigint,text,boolean);
CREATE OR REPLACE FUNCTION core.advance_github_delivery_recovery_scan_with_authority(
    p_epoch bigint,
    p_expected_cursor text,
    p_next_cursor text,
    p_complete boolean,
    p_page_head_delivered_at timestamptz,
    p_page_head_delivery_id bigint,
    p_page_tail_delivered_at timestamptz,
    p_page_tail_delivery_id bigint
) RETURNS boolean
    LANGUAGE plpgsql SECURITY DEFINER SET search_path TO 'core','pg_catalog' AS $$
BEGIN
  IF p_complete IS NULL OR (NOT p_complete AND (p_next_cursor IS NULL OR p_next_cursor=''))
     OR length(COALESCE(p_expected_cursor,''))>2048
     OR length(COALESCE(p_next_cursor,''))>2048
     OR ((p_page_head_delivered_at IS NULL) <> (p_page_head_delivery_id IS NULL))
     OR ((p_page_tail_delivered_at IS NULL) <> (p_page_tail_delivery_id IS NULL))
     OR ((p_page_head_delivered_at IS NULL) <> (p_page_tail_delivered_at IS NULL))
     OR (p_page_head_delivery_id IS NOT NULL AND p_page_head_delivery_id<=0)
     OR (p_page_tail_delivery_id IS NOT NULL AND p_page_tail_delivery_id<=0)
     OR (NOT p_complete AND p_page_head_delivery_id IS NULL)
     OR (p_page_head_delivery_id IS NOT NULL AND
         (p_page_head_delivered_at,p_page_head_delivery_id) <
         (p_page_tail_delivered_at,p_page_tail_delivery_id)) THEN
    RAISE EXCEPTION 'malformed GitHub delivery scan advance' USING ERRCODE='22023';
  END IF;
  UPDATE core.github_delivery_recovery_scan
     SET in_progress=NOT p_complete,
         cursor=CASE WHEN p_complete THEN NULL ELSE p_next_cursor END,
         page_tail_delivery_id=COALESCE(p_page_tail_delivery_id,page_tail_delivery_id),
         page_tail_delivered_at=COALESCE(p_page_tail_delivered_at,page_tail_delivered_at),
         high_water_delivery_id=CASE WHEN p_complete
           THEN COALESCE(scan_ceiling_delivery_id,high_water_delivery_id)
           ELSE high_water_delivery_id END,
         scan_completed_at=CASE WHEN p_complete THEN now() ELSE scan_completed_at END,
         updated_at=now()
   WHERE singleton AND in_progress AND scan_epoch=p_epoch
     AND cursor IS NOT DISTINCT FROM p_expected_cursor
     -- Durable cross-page order proof. An empty terminal page has no head and is safe; every non-empty next page
     -- must begin no newer than the tail committed with the current cursor.
     AND (p_page_head_delivered_at IS NULL OR page_tail_delivered_at IS NULL OR
          (page_tail_delivered_at,page_tail_delivery_id) >=
          (p_page_head_delivered_at,p_page_head_delivery_id));
  RETURN FOUND;
END $$;
ALTER FUNCTION core.advance_github_delivery_recovery_scan_with_authority(
  bigint,text,text,boolean,timestamptz,bigint,timestamptz,bigint)
  OWNER TO veripsa_migrator;

DROP FUNCTION IF EXISTS core.reset_github_delivery_recovery_scan_with_authority(bigint);
CREATE OR REPLACE FUNCTION core.reset_github_delivery_recovery_scan_with_authority(
    p_epoch bigint,p_expected_cursor text)
RETURNS boolean
    LANGUAGE plpgsql SECURITY DEFINER SET search_path TO 'core','pg_catalog' AS $$
BEGIN
  UPDATE core.github_delivery_recovery_scan
     SET in_progress=false,cursor=NULL,scan_ceiling_delivery_id=NULL,
         page_tail_delivery_id=NULL,page_tail_delivered_at=NULL,updated_at=now()
   WHERE singleton AND in_progress AND scan_epoch=p_epoch
     AND cursor IS NOT DISTINCT FROM p_expected_cursor;
  RETURN FOUND;
END $$;
ALTER FUNCTION core.reset_github_delivery_recovery_scan_with_authority(bigint,text) OWNER TO veripsa_migrator;

CREATE OR REPLACE FUNCTION core.claim_github_delivery_redelivery_with_authority()
RETURNS jsonb
    LANGUAGE plpgsql SECURITY DEFINER SET search_path TO 'core','pg_catalog' AS $$
DECLARE v_row core.github_delivery_recovery%ROWTYPE;
BEGIN
  -- A locally accepted durable row is stronger evidence than GitHub's eventually updated list metadata.
  UPDATE core.github_delivery_recovery r
     SET locally_received=true,resolved=true,claimed_at=NULL,last_seen_at=now()
   WHERE NOT r.locally_received
     AND EXISTS (SELECT 1 FROM core.webhook_delivery d WHERE d.delivery_key=r.delivery_guid);

  -- A definite GitHub rate-limit/auth response pauses the whole App, durably across restart and instances.
  IF EXISTS (SELECT 1 FROM core.github_delivery_recovery_scan
              WHERE singleton AND redelivery_not_before>now()) THEN
    RETURN '{}'::jsonb;
  END IF;

  SELECT * INTO v_row
    FROM core.github_delivery_recovery
   WHERE NOT resolved
     AND latest_recovery_class IN ('redeliverable','timeout')
     AND window_expires_at>now()
     AND attempt_count<3
     AND (next_attempt_at IS NULL OR next_attempt_at<=now())
     AND (claimed_at IS NULL OR claimed_at<now()-interval '15 minutes')
   ORDER BY window_expires_at,latest_delivered_at,delivery_guid
   LIMIT 1 FOR UPDATE SKIP LOCKED;
  IF NOT FOUND THEN RETURN '{}'::jsonb; END IF;

  UPDATE core.github_delivery_recovery
     SET attempt_count=attempt_count+1,
         attempt_generation=attempt_generation+1,
         claimed_at=now(),last_attempt_at=now(),last_attempt_outcome=NULL,
         next_attempt_at=now()+CASE attempt_count
           WHEN 0 THEN interval '5 minutes'
           WHEN 1 THEN interval '30 minutes'
           ELSE interval '2 hours' END
   WHERE delivery_guid=v_row.delivery_guid
   RETURNING * INTO v_row;
  RETURN jsonb_build_object(
    'delivery_guid',v_row.delivery_guid,'delivery_id',v_row.latest_delivery_id,
    'generation',v_row.attempt_generation,'attempt',v_row.attempt_count);
END $$;
ALTER FUNCTION core.claim_github_delivery_redelivery_with_authority() OWNER TO veripsa_migrator;

CREATE OR REPLACE FUNCTION core.record_github_delivery_redelivery_attempt_with_authority(
    p_delivery_guid text,
    p_generation bigint,
    p_outcome text,
    p_not_before timestamptz DEFAULT NULL
) RETURNS boolean
    LANGUAGE plpgsql SECURITY DEFINER SET search_path TO 'core','pg_catalog' AS $$
BEGIN
  IF p_outcome NOT IN (
      'accepted_ambiguous','transport_ambiguous','rate_limited','auth_deferred','terminal_rejected') THEN
    RAISE EXCEPTION 'invalid GitHub redelivery outcome' USING ERRCODE='22023';
  END IF;
  IF p_outcome IN ('rate_limited','auth_deferred')
     AND (p_not_before IS NULL OR p_not_before<=now() OR p_not_before>now()+interval '6 hours') THEN
    RAISE EXCEPTION 'invalid GitHub redelivery global cooldown' USING ERRCODE='22023';
  END IF;
  UPDATE core.github_delivery_recovery
     SET claimed_at=NULL,last_attempt_outcome=p_outcome,last_attempt_at=now(),last_seen_at=now(),
         next_attempt_at=CASE WHEN p_outcome IN ('rate_limited','auth_deferred')
                              THEN p_not_before ELSE next_attempt_at END,
         latest_recovery_class=CASE WHEN p_outcome='terminal_rejected'
                                    THEN 'terminal' ELSE latest_recovery_class END
   WHERE delivery_guid=p_delivery_guid AND attempt_generation=p_generation AND claimed_at IS NOT NULL;
  IF FOUND AND p_outcome IN ('rate_limited','auth_deferred') THEN
    UPDATE core.github_delivery_recovery_scan
       SET redelivery_not_before=GREATEST(COALESCE(redelivery_not_before,p_not_before),p_not_before),
           updated_at=now()
     WHERE singleton;
  END IF;
  RETURN FOUND;
END $$;
ALTER FUNCTION core.record_github_delivery_redelivery_attempt_with_authority(text,bigint,text,timestamptz)
  OWNER TO veripsa_migrator;

CREATE OR REPLACE FUNCTION core.prune_github_delivery_recovery_with_authority()
RETURNS int
    LANGUAGE plpgsql SECURITY DEFINER SET search_path TO 'core','pg_catalog' AS $$
DECLARE
  v_resolved int := 0;
  v_archived int := 0;
  v_archived_terminal bigint := 0;
  v_archived_exhausted bigint := 0;
BEGIN
  IF EXISTS (SELECT 1 FROM core.github_delivery_recovery_scan WHERE singleton AND in_progress) THEN
    RETURN 0;
  END IF;
  -- Three days ends GitHub's recovery API window, not our evidence obligation.  Drop only resolved rows once
  -- THEIR latest delivery has also left that window. Keeping success sticky until then prevents a later delayed/
  -- manual failure with the same GUID from being inserted as fresh retryable work after an eager prune.
  WITH due AS (
    SELECT delivery_guid FROM core.github_delivery_recovery
     WHERE resolved AND window_expires_at<=now()
     ORDER BY window_expires_at,delivery_guid
     LIMIT 1000 FOR UPDATE SKIP LOCKED
  ), deleted AS (
    DELETE FROM core.github_delivery_recovery r USING due
     WHERE r.delivery_guid=due.delivery_guid RETURNING 1
  ) SELECT count(*)::int INTO v_resolved FROM deleted;

  -- Unresolved evidence must not grow without bound. Keep the full content-free row for THIRTY DAYS AFTER the
  -- three-day GitHub replay window, then atomically compact a bounded batch into host-global counters. Counters
  -- preserve the active audit/alert signal without retaining GUID/id/timestamp/status rows forever.
  WITH due AS (
    SELECT delivery_guid FROM core.github_delivery_recovery
     WHERE NOT resolved AND window_expires_at<=now()-interval '30 days'
     ORDER BY window_expires_at,delivery_guid
     LIMIT 1000 FOR UPDATE SKIP LOCKED
  ), retired AS (
    DELETE FROM core.github_delivery_recovery r USING due
     WHERE r.delivery_guid=due.delivery_guid
     RETURNING r.latest_recovery_class,r.attempt_count
  ) SELECT count(*)::int,
           count(*) FILTER (WHERE latest_recovery_class='terminal')::bigint,
           count(*) FILTER (WHERE attempt_count>=3)::bigint
      INTO v_archived,v_archived_terminal,v_archived_exhausted FROM retired;
  IF v_archived>0 THEN
    INSERT INTO core.github_delivery_recovery_scan(
      singleton,archived_unresolved_count,archived_terminal_count,
      archived_exhausted_count,archived_last_at)
    VALUES (true,v_archived,v_archived_terminal,v_archived_exhausted,now())
    ON CONFLICT (singleton) DO UPDATE SET
      archived_unresolved_count=core.github_delivery_recovery_scan.archived_unresolved_count
                                  + EXCLUDED.archived_unresolved_count,
      archived_terminal_count=core.github_delivery_recovery_scan.archived_terminal_count
                                + EXCLUDED.archived_terminal_count,
      archived_exhausted_count=core.github_delivery_recovery_scan.archived_exhausted_count
                                 + EXCLUDED.archived_exhausted_count,
      archived_last_at=EXCLUDED.archived_last_at,updated_at=now();
  END IF;
  RETURN v_resolved+v_archived;
END $$;
ALTER FUNCTION core.prune_github_delivery_recovery_with_authority() OWNER TO veripsa_migrator;

-- rearm_exhausted_github_delivery_recovery_with_authority: the RECOVERY-QUEUE re-arm (stabilization 2026-07-19,
-- mirrors rearm_failed_webhook_deliveries_with_authority above). A failed GitHub delivery whose redelivery attempts
-- EXHAUSTED the bounded budget (attempt_count>=3) while still REDELIVERABLE (class redeliverable/timeout) and still
-- inside GitHub's three-day replay window has NO comeback path of its own: claim_github_delivery_redelivery() offers
-- only attempt_count<3 rows, prune keeps the unresolved row ~33 days, and the depth alert
-- (github_delivery_redelivery_exhausted, CRITICAL) re-fires every watchdog tick for that whole retention. When the
-- exhaustion cause was a TRANSIENT outbound outage (a fleet-wide 403/429/5xx window that has since cleared) those
-- rows are permanently un-redelivered AND permanently paging, with no operator comeback short of a raw table write.
-- This is the controlled re-arm, IDENTICAL discipline to the DLQ sweep: an EXHAUSTED, still-redeliverable,
-- still-in-window row whose LAST attempt is OLDER than p_rearm_seconds (it has had time to alert on + the transient
-- cause to clear) is given exactly ONE fresh attempt budget — attempt_count->0, a NEW attempt_generation (so a stale
-- in-flight claim is an idempotent no-op via the generation check), claim cleared, next_attempt_at=now() (immediately
-- due). claim_github_delivery_redelivery() then re-offers it and record_attempt() spends the fresh budget; only a
-- GitHub status=OK or a matching local receipt resolves it, so this NEVER fakes delivery. The last-attempt age gate
-- is what stops a genuinely poison delivery from hot-looping redelivery POSTs: one more honest try per p_rearm_seconds,
-- with the CRITICAL exhausted alert as the human signal in between. NEVER touches resolved (a delivered GUID stays
-- delivered), 'terminal' class (GitHub cannot redeliver it — no point), or an out-of-window row (past GitHub's
-- three-day replay API → a redelivery would 404). No tenant arg (the recovery table has no per-account RLS —
-- owner-of-the-inbox discipline, same as the other recovery fns). Content-free: only attempt metadata changes and
-- only counts are returned. Returns {rearmed, exhausted_remaining}.
CREATE OR REPLACE FUNCTION core.rearm_exhausted_github_delivery_recovery_with_authority(
    p_rearm_seconds int DEFAULT 21600,
    p_limit int DEFAULT 100
) RETURNS jsonb
    LANGUAGE plpgsql SECURITY DEFINER SET search_path TO 'core','pg_catalog' AS $$
DECLARE v_rearmed int; v_remaining int;
BEGIN
  WITH due AS (
    SELECT delivery_guid FROM core.github_delivery_recovery
     WHERE NOT resolved
       AND attempt_count >= 3
       AND latest_recovery_class IN ('redeliverable','timeout')
       AND window_expires_at > now()
       AND (last_attempt_at IS NULL
            OR last_attempt_at < now() - make_interval(secs => GREATEST(COALESCE(p_rearm_seconds,21600),1)))
     ORDER BY window_expires_at, delivery_guid
     LIMIT GREATEST(LEAST(COALESCE(p_limit,100),1000),1)
     FOR UPDATE SKIP LOCKED),
  rearmed AS (
    UPDATE core.github_delivery_recovery r
       SET attempt_count=0,
           attempt_generation=attempt_generation+1,
           claimed_at=NULL,
           next_attempt_at=now(),
           last_seen_at=now()
      FROM due WHERE r.delivery_guid=due.delivery_guid
    RETURNING r.delivery_guid)
  SELECT count(*)::int INTO v_rearmed FROM rearmed;
  SELECT count(*)::int INTO v_remaining FROM core.github_delivery_recovery
     WHERE NOT resolved AND attempt_count>=3 AND latest_recovery_class IN ('redeliverable','timeout');
  RETURN jsonb_build_object('rearmed', v_rearmed, 'exhausted_remaining', v_remaining);
END $$;
ALTER FUNCTION core.rearm_exhausted_github_delivery_recovery_with_authority(int,int) OWNER TO veripsa_migrator;

CREATE OR REPLACE FUNCTION core.github_delivery_recovery_depth_with_authority()
RETURNS jsonb
    LANGUAGE sql SECURITY DEFINER SET search_path TO 'core','pg_catalog' AS $$
  SELECT jsonb_build_object(
    'eligible',(SELECT count(*) FROM core.github_delivery_recovery
      WHERE NOT resolved AND attempt_count<3
        AND latest_recovery_class IN ('redeliverable','timeout') AND window_expires_at>now()),
    'exhausted',(SELECT count(*) FROM core.github_delivery_recovery
      WHERE NOT resolved AND attempt_count>=3
        AND latest_recovery_class IN ('redeliverable','timeout')),
    'expiring',(SELECT count(*) FROM core.github_delivery_recovery
      WHERE NOT resolved AND latest_recovery_class IN ('redeliverable','timeout')
        AND window_expires_at BETWEEN now() AND now()+interval '6 hours'),
    'terminal_unrecovered',(SELECT count(*) FROM core.github_delivery_recovery
      WHERE NOT resolved AND latest_recovery_class='terminal'),
    'expired_unrecovered',(SELECT count(*) FROM core.github_delivery_recovery
      WHERE NOT resolved AND window_expires_at<=now()
        AND latest_recovery_class IN ('redeliverable','timeout','terminal')),
    'archived_unresolved',COALESCE((SELECT archived_unresolved_count
      FROM core.github_delivery_recovery_scan WHERE singleton),0),
    'archived_terminal',COALESCE((SELECT archived_terminal_count
      FROM core.github_delivery_recovery_scan WHERE singleton),0),
    'archived_exhausted',COALESCE((SELECT archived_exhausted_count
      FROM core.github_delivery_recovery_scan WHERE singleton),0),
    'archived_last_at',(SELECT archived_last_at
      FROM core.github_delivery_recovery_scan WHERE singleton),
    'cooldown_active',COALESCE((SELECT redelivery_not_before>now()
      FROM core.github_delivery_recovery_scan WHERE singleton),false),
    -- Always anchor to the LAST SUCCESS, not the current attempt's start.  Before the first success the preserved
    -- first scan_started_at is the anchor, so an invalid-cursor reset loop cannot keep this counter young forever.
    'scan_lag_seconds',COALESCE((SELECT GREATEST(0,floor(EXTRACT(EPOCH FROM
      (now()-COALESCE(scan_completed_at,scan_started_at,updated_at))))::bigint)
      FROM core.github_delivery_recovery_scan WHERE singleton),0),
    'scan_in_progress',COALESCE((SELECT in_progress
      FROM core.github_delivery_recovery_scan WHERE singleton),false)
  )
$$;
ALTER FUNCTION core.github_delivery_recovery_depth_with_authority() OWNER TO veripsa_migrator;

REVOKE EXECUTE ON FUNCTION core.enqueue_webhook_delivery_with_authority(text,text,text,text,jsonb,int) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION core.enqueue_webhook_delivery_with_authority(text,text,text,text,jsonb,int,int) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION core.pending_webhook_deliveries_with_authority(int,int,int) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION core.claim_webhook_delivery_with_authority(text,int,int,int,text,int) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION core.claim_webhook_delivery_with_authority(text,int,int,int,text) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION core.claim_webhook_delivery_with_authority(text,int,int,int) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION core.claim_webhook_delivery_with_authority(text,int,int) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION core.finish_webhook_delivery_with_authority(text) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION core.finish_webhook_delivery_with_authority(text,bigint) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION core.release_webhook_delivery_with_authority(text,text,int) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION core.release_webhook_delivery_with_authority(text,text,int,bigint) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION core.resolve_webhook_delivery_release_with_authority(text,text,int,bigint) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION core.resolve_webhook_delivery_defer_with_authority(
  text,bigint,timestamptz,text) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION core.resolve_webhook_delivery_commit_with_authority(text,text,int,bigint) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION core.recover_ambiguous_webhook_claim_with_authority(text,text,text,int) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION core._canonical_webhook_delivery_fanout_plan(jsonb) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION core._webhook_delivery_fanout_completion_valid(jsonb,jsonb) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION core.prepare_webhook_delivery_fanout_with_authority(text,bigint,jsonb) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION core.complete_webhook_delivery_fanout_repository_with_authority(text,bigint,text) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION core.resolve_webhook_delivery_fanout_defer_with_authority(
  text,bigint,timestamptz,text) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION core.webhook_delivery_depth_with_authority() FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION core.rearm_failed_webhook_deliveries_with_authority(int,int,int) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION core.rearm_failed_webhook_deliveries_with_authority(int,int) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION core.recover_terminal_webhook_deliveries_with_authority(text[],text)
  FROM PUBLIC,veripsa_writer;
REVOKE EXECUTE ON FUNCTION core.continue_terminal_webhook_deliveries_with_authority(text[],text,text)
  FROM PUBLIC,veripsa_writer;
REVOKE EXECUTE ON FUNCTION core.claim_operator_github_redelivery_with_authority(text,text,bigint,text)
  FROM PUBLIC,veripsa_writer;
REVOKE EXECUTE ON FUNCTION core.record_operator_github_redelivery_outcome_with_authority(text,text,text,text)
  FROM PUBLIC,veripsa_writer;
REVOKE EXECUTE ON FUNCTION core.operator_github_redelivery_audit_with_authority(text,text,text,text)
  FROM PUBLIC,veripsa_writer;
REVOKE EXECUTE ON FUNCTION core.terminal_webhook_delivery_recovery_status_with_authority(text[])
  FROM PUBLIC,veripsa_writer;
REVOKE EXECUTE ON FUNCTION core.escalate_blocked_webhook_deliveries_with_authority(int,int,int) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION core.escalate_blocked_webhook_deliveries_with_authority(int,int) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION core.begin_github_delivery_recovery_scan_with_authority() FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION core.observe_github_delivery_with_authority(bigint,bigint,text,timestamptz,int,text) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION core.advance_github_delivery_recovery_scan_with_authority(
  bigint,text,text,boolean,timestamptz,bigint,timestamptz,bigint) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION core.reset_github_delivery_recovery_scan_with_authority(bigint,text) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION core.claim_github_delivery_redelivery_with_authority() FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION core.record_github_delivery_redelivery_attempt_with_authority(text,bigint,text,timestamptz) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION core.prune_github_delivery_recovery_with_authority() FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION core.rearm_exhausted_github_delivery_recovery_with_authority(int,int) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION core.github_delivery_recovery_depth_with_authority() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION core.enqueue_webhook_delivery_with_authority(text,text,text,text,jsonb,int) TO veripsa_app;
GRANT EXECUTE ON FUNCTION core.enqueue_webhook_delivery_with_authority(text,text,text,text,jsonb,int,int) TO veripsa_app;
GRANT EXECUTE ON FUNCTION core.pending_webhook_deliveries_with_authority(int,int,int) TO veripsa_app;
GRANT EXECUTE ON FUNCTION core.claim_webhook_delivery_with_authority(text,int,int,int,text,int) TO veripsa_app;
GRANT EXECUTE ON FUNCTION core.claim_webhook_delivery_with_authority(text,int,int,int,text) TO veripsa_app;
GRANT EXECUTE ON FUNCTION core.claim_webhook_delivery_with_authority(text,int,int,int) TO veripsa_app;
GRANT EXECUTE ON FUNCTION core.claim_webhook_delivery_with_authority(text,int,int) TO veripsa_app;
GRANT EXECUTE ON FUNCTION core.finish_webhook_delivery_with_authority(text) TO veripsa_app;
GRANT EXECUTE ON FUNCTION core.finish_webhook_delivery_with_authority(text,bigint) TO veripsa_app;
GRANT EXECUTE ON FUNCTION core.release_webhook_delivery_with_authority(text,text,int) TO veripsa_app;
GRANT EXECUTE ON FUNCTION core.release_webhook_delivery_with_authority(text,text,int,bigint) TO veripsa_app;
GRANT EXECUTE ON FUNCTION core.resolve_webhook_delivery_release_with_authority(text,text,int,bigint) TO veripsa_app;
GRANT EXECUTE ON FUNCTION core.resolve_webhook_delivery_defer_with_authority(
  text,bigint,timestamptz,text) TO veripsa_app;
GRANT EXECUTE ON FUNCTION core.resolve_webhook_delivery_commit_with_authority(text,text,int,bigint) TO veripsa_app;
GRANT EXECUTE ON FUNCTION core.recover_ambiguous_webhook_claim_with_authority(text,text,text,int) TO veripsa_app;
GRANT EXECUTE ON FUNCTION core.prepare_webhook_delivery_fanout_with_authority(text,bigint,jsonb) TO veripsa_app;
GRANT EXECUTE ON FUNCTION core.complete_webhook_delivery_fanout_repository_with_authority(text,bigint,text) TO veripsa_app;
GRANT EXECUTE ON FUNCTION core.resolve_webhook_delivery_fanout_defer_with_authority(
  text,bigint,timestamptz,text) TO veripsa_app;
GRANT EXECUTE ON FUNCTION core.webhook_delivery_depth_with_authority() TO veripsa_app;
GRANT EXECUTE ON FUNCTION core.rearm_failed_webhook_deliveries_with_authority(int,int,int) TO veripsa_app;
GRANT EXECUTE ON FUNCTION core.rearm_failed_webhook_deliveries_with_authority(int,int) TO veripsa_app;  -- DLQ re-arm: requeue 'failed' poison/outage rows past the re-arm age for one fresh attempt-budget (App-only)
GRANT EXECUTE ON FUNCTION core.recover_terminal_webhook_deliveries_with_authority(text[],text)
  TO veripsa_app;
GRANT EXECUTE ON FUNCTION core.continue_terminal_webhook_deliveries_with_authority(text[],text,text)
  TO veripsa_app;
GRANT EXECUTE ON FUNCTION core.claim_operator_github_redelivery_with_authority(text,text,bigint,text)
  TO veripsa_app;
GRANT EXECUTE ON FUNCTION core.record_operator_github_redelivery_outcome_with_authority(text,text,text,text)
  TO veripsa_app;
GRANT EXECUTE ON FUNCTION core.operator_github_redelivery_audit_with_authority(text,text,text,text)
  TO veripsa_app;
GRANT EXECUTE ON FUNCTION core.terminal_webhook_delivery_recovery_status_with_authority(text[])
  TO veripsa_app;
GRANT EXECUTE ON FUNCTION core.escalate_blocked_webhook_deliveries_with_authority(int,int,int) TO veripsa_app;
GRANT EXECUTE ON FUNCTION core.escalate_blocked_webhook_deliveries_with_authority(int,int) TO veripsa_app;  -- issue #847: re-arm an aged failed lane head that is blocking aged queued work (App-only)
GRANT EXECUTE ON FUNCTION core.begin_github_delivery_recovery_scan_with_authority() TO veripsa_app;
GRANT EXECUTE ON FUNCTION core.observe_github_delivery_with_authority(bigint,bigint,text,timestamptz,int,text) TO veripsa_app;
GRANT EXECUTE ON FUNCTION core.advance_github_delivery_recovery_scan_with_authority(
  bigint,text,text,boolean,timestamptz,bigint,timestamptz,bigint) TO veripsa_app;
GRANT EXECUTE ON FUNCTION core.reset_github_delivery_recovery_scan_with_authority(bigint,text) TO veripsa_app;
GRANT EXECUTE ON FUNCTION core.claim_github_delivery_redelivery_with_authority() TO veripsa_app;
GRANT EXECUTE ON FUNCTION core.record_github_delivery_redelivery_attempt_with_authority(text,bigint,text,timestamptz) TO veripsa_app;
GRANT EXECUTE ON FUNCTION core.prune_github_delivery_recovery_with_authority() TO veripsa_app;
GRANT EXECUTE ON FUNCTION core.rearm_exhausted_github_delivery_recovery_with_authority(int,int) TO veripsa_app;  -- recovery-queue re-arm: give an EXHAUSTED still-redeliverable in-window row one fresh attempt-budget past the re-arm age (App-only)
GRANT EXECUTE ON FUNCTION core.github_delivery_recovery_depth_with_authority() TO veripsa_app;

COMMIT;
-- END atomic queue-protocol + repository-offboarding rollout switch.
