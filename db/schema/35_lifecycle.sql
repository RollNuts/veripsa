-- PHASE 2 — THE GATE: LIFECYCLE / OFFBOARDING / RETENTION / DR (split out of 30_gate.sql).
-- The data-LIFECYCLE half of the write gate: forget (purge_repo / erase_account = right-to-deletion),
-- re-coordinate (rename_repo / release_repo_claims = repo rename/archive), bound (prune_events /
-- prune_all_accounts = retention), and protect (export_durable_rows = DR). All App-delegation-only
-- (veripsa_app), except DR export which is isolated to veripsa_backup — never a buyer seat.
-- SECURITY DEFINER; identity from the connection role (never a caller arg → no cross-tenant erase).
-- Applied AFTER 30_gate.sql: plpgsql bodies are late-bound, so the only cross-module callees (the
-- substrate write-context + the named forgery/retention/erasure tokens, the per-account tables) are all
-- already defined by 10_substrate/20_core/30_gate when these run; the trailing GRANT/REVOKE bind each fn
-- here (a GRANT needs the fn to already exist — kept WITH its CREATE, the apply-order-safe unit).
-- ============================================================================================

-- repository_lifecycle_tombstone: durable, content-free proof that one repository is no longer
-- authorized while the owning GitHub App installation may remain live. GitHub deliveries are
-- unordered and the durable inbox may replay an older PR/push after repository.deleted or
-- installation_repositories.removed. Without a repo-scoped tombstone that stale event can rebuild
-- graph/claim rows immediately after the purge. The stable GitHub repository id distinguishes a
-- genuinely recreated same-name repository from a late event for the deleted object. A bounded `unknown`
-- sentinel covers deliveries minimized before repository-id persistence was deployed; it fails closed until
-- an explicit repository.created / installation_repositories.added event clears it. `repo` is kept
-- only as the bounded routing coordinate needed to block legacy minimized deliveries that predate
-- repository-id persistence and to fail closed on repo-level read surfaces. It contains no source,
-- diff, payload body, path, PR content, or token. Account uninstall and explicit erasure remove it.
CREATE TABLE IF NOT EXISTS core.repository_lifecycle_tombstone (
    account_id text NOT NULL,
    repository_id text NOT NULL,
    repo text NOT NULL,
    reason text NOT NULL,
    tombstoned_at timestamptz DEFAULT now() NOT NULL,
    lifecycle_received_at timestamptz DEFAULT now() NOT NULL,
    superseded_at timestamptz,
    -- Preserve the first processing-time boundary for this stable GitHub object across deselect/reselect. The
    -- activation row is live authority and is purged on removal; this content-free timestamp is its durable carry.
    generation_started_at timestamptz,
    PRIMARY KEY (account_id, repository_id, repo),
    CONSTRAINT repository_tombstone_id_shape CHECK (
      repository_id = 'unknown' OR
      (length(repository_id) BETWEEN 1 AND 32 AND repository_id ~ '^[1-9][0-9]*$')),
    CONSTRAINT repository_tombstone_repo_len CHECK (length(repo) BETWEEN 1 AND 512),
    CONSTRAINT repository_tombstone_reason_check CHECK (
      reason = ANY (ARRAY['repository_deleted','installation_removed','repository_transferred']))
);
SELECT core._ensure_column_online(
  'repository_lifecycle_tombstone','superseded_at','timestamptz');
SELECT core._ensure_column_online(
  'repository_lifecycle_tombstone','lifecycle_received_at','timestamptz');
SELECT core._ensure_column_online(
  'repository_lifecycle_tombstone','generation_started_at','timestamptz');
-- Rolling additive reason expansion for cross-account transfer tombstones.  Recreate only when an older catalog
-- still has the two-value check; all historical rows satisfy the expanded constraint.
DO $$
BEGIN
  IF EXISTS (
    SELECT 1 FROM pg_constraint
     WHERE conrelid='core.repository_lifecycle_tombstone'::regclass
       AND conname='repository_tombstone_reason_check'
       AND pg_get_constraintdef(oid) NOT LIKE '%repository_transferred%'
  ) THEN
    ALTER TABLE core.repository_lifecycle_tombstone
      DROP CONSTRAINT repository_tombstone_reason_check;
    ALTER TABLE core.repository_lifecycle_tombstone
      ADD CONSTRAINT repository_tombstone_reason_check CHECK (
        reason = ANY (ARRAY['repository_deleted','installation_removed','repository_transferred']));
  END IF;
END $$;
UPDATE core.repository_lifecycle_tombstone
   SET lifecycle_received_at=tombstoned_at
 WHERE lifecycle_received_at IS NULL;
SELECT core._ensure_column_default_online(
  'repository_lifecycle_tombstone','lifecycle_received_at','now()');
SELECT core._ensure_column_not_null_online(
  'repository_lifecycle_tombstone','lifecycle_received_at');
DO $$
BEGIN
  IF EXISTS (
    SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
     WHERE n.nspname='core' AND c.relname='repository_lifecycle_tombstone'
       AND c.relowner <> 'veripsa_migrator'::regrole
  ) THEN
    ALTER TABLE core.repository_lifecycle_tombstone OWNER TO veripsa_migrator;
  END IF;
END $$;
REVOKE ALL ON TABLE core.repository_lifecycle_tombstone FROM PUBLIC;

-- repository_lifecycle_activation: graph-independent proof of the currently selected GitHub repository object.
-- A replacement can be explicitly added before its first push/backfill creates graph_version, while a delayed
-- deletion for the old stable id is still in flight. Inferring replacement activation only from graph_version
-- makes that ordering ambiguous and can hide the live replacement behind a fresh unsuperseded tombstone. Keep
-- stable ownership separately: one current id per tenant coordinate, plus whether it came from authenticated
-- lifecycle authority or ordinary work observation. No source/diff/payload is stored.
CREATE TABLE IF NOT EXISTS core.repository_lifecycle_activation (
    account_id text NOT NULL,
    repository_id text NOT NULL,
    repo text NOT NULL,
    activated_at timestamptz DEFAULT now() NOT NULL,
    -- Only an authenticated add/create (or the authenticated current-identity resolution for a legacy removal)
    -- may make an older same-ID removal stale. Ordinary work/boot identity observation is useful for routing and
    -- replacement isolation, but is not repository-selection authority.
    lifecycle_authoritative boolean DEFAULT false NOT NULL,
    -- Processing-time boundary for current-generation reads. activated_at is durable RECEIVE order and must not be
    -- reused for this: an old-object event can process after an add was received but before that add is applied.
    generation_started_at timestamptz,
    PRIMARY KEY (account_id, repository_id),
    CONSTRAINT repository_activation_coordinate_unique UNIQUE (account_id, repo),
    CONSTRAINT repository_activation_id_shape CHECK (
      length(repository_id) BETWEEN 1 AND 32 AND repository_id ~ '^[1-9][0-9]*$'),
    CONSTRAINT repository_activation_repo_len CHECK (length(repo) BETWEEN 1 AND 512)
);
-- Upgrade safety for databases that briefly ran the pre-provenance activation table. Existing rows have unknown
-- origin, so classify them conservatively as work-observed. Every authenticated lifecycle writer below opts in
-- explicitly; an omitted value must never gain selection authority.
SELECT core._ensure_column_online(
  'repository_lifecycle_activation','lifecycle_authoritative',
  'boolean DEFAULT false NOT NULL');
SELECT core._ensure_column_online(
  'repository_lifecycle_activation','generation_started_at','timestamptz');
DO $$
BEGIN
  IF EXISTS (
    SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
     WHERE n.nspname='core' AND c.relname='repository_lifecycle_activation'
       AND c.relowner <> 'veripsa_migrator'::regrole
  ) THEN
    ALTER TABLE core.repository_lifecycle_activation OWNER TO veripsa_migrator;
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM pg_class
     WHERE oid='core.repository_lifecycle_activation'::regclass AND relrowsecurity
  ) THEN
    ALTER TABLE core.repository_lifecycle_activation ENABLE ROW LEVEL SECURITY;
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM pg_class
     WHERE oid='core.repository_lifecycle_activation'::regclass AND relforcerowsecurity
  ) THEN
    ALTER TABLE ONLY core.repository_lifecycle_activation FORCE ROW LEVEL SECURITY;
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM pg_policy
     WHERE polrelid='core.repository_lifecycle_activation'::regclass AND polname='tenant_isolation'
  ) THEN
    CREATE POLICY tenant_isolation ON core.repository_lifecycle_activation
      USING (account_id=current_setting('core.current_account',true))
      WITH CHECK (account_id=current_setting('core.current_account',true));
  END IF;
END $$;
REVOKE ALL ON TABLE core.repository_lifecycle_activation FROM PUBLIC;

-- repository_event_allowed_with_authority: the hot-path resurrection guard. Exact repository-id
-- tombstones refuse work. A legacy delivery without an id is refused by coordinate. A signed event
-- for a DIFFERENT id at the same full_name is a genuinely recreated repository, so the new object
-- proceeds while the old object's exact-id marker remains available to reject late redelivery.
-- Identity comes from the connection;
-- callers can never probe or mutate another tenant's tombstones.
CREATE OR REPLACE FUNCTION core.repository_event_allowed_with_authority(p_repo text, p_repository_id text)
    RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER SET search_path TO 'core','pg_catalog' AS $$
DECLARE v_agent text; v_account text; v_repo text; v_id text;
BEGIN
  v_repo := left(NULLIF(btrim(COALESCE(p_repo,'')),''),512);
  v_id := NULLIF(btrim(COALESCE(p_repository_id,'')),'');
  IF v_repo IS NULL THEN RETURN true; END IF;
  -- Treat only GitHub's canonical positive ASCII decimal as stable identity. Reject before any tombstone or
  -- activation decision: truncating or accepting 00123/0/Unicode digits would let a non-matching non-null id skip
  -- the name-only fail-closed fallback and resurrect a removed coordinate.
  IF v_id IS NOT NULL AND (length(v_id) > 32 OR v_id !~ '^[1-9][0-9]*$') THEN RETURN false; END IF;
  SELECT agent, account INTO v_agent, v_account
    FROM core.establish_session_write_context() AS c(agent,account);
  -- Hold stable-object authority through the caller's whole event transaction (gate → graph → identity
  -- stamp). A transfer takes the same old-account/id key before discovering old coordinates, so no same-ID work
  -- can pass this gate and commit a previously invisible coordinate behind the transfer tombstone.
  IF v_id IS NOT NULL THEN
    PERFORM pg_advisory_xact_lock(
      hashtext('github-repository-id'),hashtext(v_id));
  END IF;

  -- Earlier releases admitted zero/leading-zero ids at the storage constraint. Until the explicit owner migration
  -- promotes those constraints, any such row is ambiguous authority for this coordinate. Fail closed instead of
  -- letting a canonical work event route around it. A current add/create lifecycle event can reset it below.
  IF EXISTS (
      SELECT 1 FROM core.repository_lifecycle_tombstone
       WHERE account_id=v_account AND repo=v_repo AND repository_id<>'unknown'
         AND (length(repository_id)>32 OR repository_id !~ '^[1-9][0-9]*$'))
     OR EXISTS (
      SELECT 1 FROM core.repository_lifecycle_activation
       WHERE account_id=v_account AND repo=v_repo
         AND (length(repository_id)>32 OR repository_id !~ '^[1-9][0-9]*$'))
     OR EXISTS (
      SELECT 1 FROM core.graph_version
       WHERE account_id=v_account AND repo=v_repo AND repo_id IS NOT NULL
         AND (length(repo_id)>32 OR repo_id !~ '^[1-9][0-9]*$')) THEN
    RETURN false;
  END IF;

  -- A pre-id deletion cannot safely distinguish a stale event from a same-name replacement. Keep it blocked
  -- until an explicit lifecycle reactivation clears the marker; an ordinary work event is not enough.
  IF EXISTS (
      SELECT 1 FROM core.repository_lifecycle_tombstone
       WHERE account_id=v_account AND repo=v_repo AND repository_id='unknown'
         AND superseded_at IS NULL) THEN
    RETURN false;
  END IF;
  IF v_id IS NOT NULL AND EXISTS (
      SELECT 1 FROM core.repository_lifecycle_tombstone
       WHERE account_id=v_account AND repository_id=v_id) THEN
    RETURN false;
  END IF;
  -- An explicit lifecycle add/created event is stable-id authority even before graph ingest. Once a different
  -- object owns this coordinate, a delayed work event for the old id must not rebuild state ahead of its delete.
  IF EXISTS (
      SELECT 1 FROM core.repository_lifecycle_activation
       WHERE account_id=v_account AND repo=v_repo
         AND (v_id IS NULL OR repository_id<>v_id)) THEN
    RETURN false;
  END IF;
  IF v_id IS NULL THEN
    RETURN NOT EXISTS (
      SELECT 1 FROM core.repository_lifecycle_tombstone
       WHERE account_id=v_account AND repo=v_repo);
  END IF;

  -- A signed work event is enough to converge a legacy graph whose repo_id was never recorded, but it is not
  -- lifecycle authority to replace a DIFFERENT known GitHub object at the same coordinate. The replacement's
  -- repository.created / installation_repositories.added delivery must reset the predecessor first; otherwise a
  -- delayed replacement push could inherit the old object's graph, claims, and live authority. Do not reject the
  -- same stable id under another coordinate here: that is a GitHub rename and the identity reconciler migrates it.
  IF EXISTS (
      SELECT 1 FROM core.graph_version
       WHERE account_id=v_account AND repo=v_repo
         AND repo_id IS NOT NULL AND repo_id<>v_id) THEN
    RETURN false;
  END IF;

  -- With no contradictory live identity, a different stable id than an old superseded marker may be a newly-
  -- created repository rather than resurrection of the old object. Do not retire exact-id markers here: an
  -- ordinary work event is not lifecycle authority, and GitHub may still redeliver an older event afterward.
  RETURN true;
END $$;
ALTER FUNCTION core.repository_event_allowed_with_authority(text,text) OWNER TO veripsa_migrator;
REVOKE EXECUTE ON FUNCTION core.repository_event_allowed_with_authority(text,text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION core.repository_event_allowed_with_authority(text,text) TO veripsa_app;

-- Account-level installation events are valid account authority, but they are not repository-selection authority.
-- A delayed installation.created/unsuspend/new_permissions delivery can still list a repository that was removed
-- later. It may cold-start only when no current removal marker blocks the coordinate and, when GitHub supplied a
-- stable id, that id does not disagree with the explicitly activated object. A superseded old-object marker is not
-- blocking: an already-activated same-name replacement may be refreshed by an All-repositories enumeration. This is
-- a read-only, content-free gate over repo/id/timestamps; it never clears a marker or creates activation state.
CREATE OR REPLACE FUNCTION core.repository_account_onboarding_allowed_with_authority(
    p_repo text, p_repository_id text)
    RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER SET search_path TO 'core','pg_catalog' AS $$
DECLARE v_agent text; v_account text; v_repo text; v_id text;
BEGIN
  v_repo := left(NULLIF(btrim(COALESCE(p_repo,'')),''),512);
  v_id := NULLIF(btrim(COALESCE(p_repository_id,'')),'');
  IF v_repo IS NULL THEN RETURN false; END IF;
  IF v_id IS NOT NULL AND (length(v_id) > 32 OR v_id !~ '^[1-9][0-9]*$') THEN RETURN false; END IF;
  SELECT agent, account INTO v_agent, v_account
    FROM core.establish_session_write_context() AS c(agent,account);
  -- Account-inventory cold starts write graph + identity after this gate in the same transaction. Serialize that
  -- whole sequence with transfer discovery exactly like the live event gate above.
  IF v_id IS NOT NULL THEN
    PERFORM pg_advisory_xact_lock(
      hashtext('github-repository-id'),hashtext(v_id));
  END IF;

  IF EXISTS (
      SELECT 1 FROM core.repository_lifecycle_tombstone
       WHERE account_id=v_account AND repo=v_repo AND repository_id<>'unknown'
         AND (length(repository_id)>32 OR repository_id !~ '^[1-9][0-9]*$'))
     OR EXISTS (
      SELECT 1 FROM core.repository_lifecycle_activation
       WHERE account_id=v_account AND repo=v_repo
         AND (length(repository_id)>32 OR repository_id !~ '^[1-9][0-9]*$'))
     OR EXISTS (
      SELECT 1 FROM core.graph_version
       WHERE account_id=v_account AND repo=v_repo AND repo_id IS NOT NULL
         AND (length(repo_id)>32 OR repo_id !~ '^[1-9][0-9]*$')) THEN
    RETURN false;
  END IF;

  IF EXISTS (
      SELECT 1 FROM core.repository_lifecycle_tombstone
       WHERE account_id=v_account AND repo=v_repo AND superseded_at IS NULL) THEN
    RETURN false;
  END IF;
  IF v_id IS NULL THEN
    -- All-repositories enumeration yields names only. A brand-new coordinate is safe to cold-start, but once any
    -- lifecycle/graph identity exists a name alone cannot prove which GitHub object the old account event meant.
    RETURN NOT EXISTS (
             SELECT 1 FROM core.repository_lifecycle_activation
              WHERE account_id=v_account AND repo=v_repo)
       AND NOT EXISTS (
             SELECT 1 FROM core.graph_version
              WHERE account_id=v_account AND repo=v_repo);
  END IF;
  IF EXISTS (
      SELECT 1 FROM core.repository_lifecycle_activation
       WHERE account_id=v_account
         AND ((repo=v_repo AND repository_id<>v_id)
              OR (repository_id=v_id AND repo<>v_repo))) THEN
    RETURN false;
  END IF;
  IF EXISTS (
      SELECT 1 FROM core.graph_version
       WHERE account_id=v_account
         -- A NULL repo_id is a legacy, not-yet-stamped graph identity. It does not contradict the stable id in
         -- this signed GitHub event and may be converged by reconcile_repo_identity_with_authority after the
         -- cold-start succeeds. A DIFFERENT non-null id is still a hard conflict, as is the same id under another
         -- coordinate; neither can be repaired by an account-level event.
         AND ((repo=v_repo AND repo_id IS NOT NULL AND repo_id<>v_id)
              OR (repo_id=v_id AND repo<>v_repo))) THEN
    RETURN false;
  END IF;
  RETURN true;
END $$;
ALTER FUNCTION core.repository_account_onboarding_allowed_with_authority(text,text) OWNER TO veripsa_migrator;
REVOKE EXECUTE ON FUNCTION core.repository_account_onboarding_allowed_with_authority(text,text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION core.repository_account_onboarding_allowed_with_authority(text,text) TO veripsa_app;

CREATE OR REPLACE FUNCTION core.reactivate_repository_with_authority(
    p_repo text, p_repository_id text, p_delivery_key text)
    RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path TO 'core','pg_catalog' AS $$
DECLARE
  v_agent text; v_account text; v_repo text; v_id text; v_delivery_key text;
  v_cleared int := 0; v_superseded int := 0;
  v_delivery_received_at timestamptz; v_activated_at timestamptz;
  v_latest_activation_at timestamptz; v_latest_tombstone_at timestamptz;
  v_delivery_event_type text; v_delivery_action text;
  v_is_transfer boolean := false; v_transfer_returning boolean := false;
  v_replaced_ids text[] := ARRAY[]::text[];
  v_replacement_reset jsonb := NULL; v_old_id text; v_legacy_replaced boolean := false;
  v_reselection_reset boolean := false; v_invalid_identity_reset boolean := false;
  v_prior_generation_started_at timestamptz; v_generation_started_at timestamptz;
BEGIN
  v_repo := left(NULLIF(btrim(COALESCE(p_repo,'')),''),512);
  v_id := NULLIF(btrim(COALESCE(p_repository_id,'')),'');
  v_delivery_key := left(NULLIF(btrim(COALESCE(p_delivery_key,'')),''),200);
  IF v_id IS NOT NULL AND (length(v_id) > 32 OR v_id !~ '^[1-9][0-9]*$') THEN
    RETURN jsonb_build_object('ok',false,'cleared',0,'reason','invalid repository id');
  END IF;
  IF v_repo IS NULL AND v_id IS NULL THEN
    RETURN jsonb_build_object('ok',false,'cleared',0,'reason','missing repository identity');
  END IF;
  SELECT agent, account INTO v_agent, v_account
    FROM core.establish_session_write_context() AS c(agent,account);
  IF v_id IS NOT NULL THEN
    -- Lifecycle create/add/transfer activation participates in the same stable-object serialization as work,
    -- deletion and transfer discovery. GitHub repository ids are globally unique.
    PERFORM pg_advisory_xact_lock(hashtext('github-repository-id'),hashtext(v_id));
  END IF;
  IF v_delivery_key IS NULL AND session_user='veripsa_app' THEN
    RAISE EXCEPTION 'repository reactivation needs durable delivery authority'
      USING ERRCODE='42501';
  END IF;

  -- Authenticate lifecycle ordering from the durable inbox. GitHub may deliver add/create after a newer remove,
  -- or an older add after a newer replacement add. A stale activation must not clear a newer tombstone, reset a
  -- newer replacement graph, or reopen reads. repository.created, installation_repositories.added, and a
  -- repository.transferred row carrying this transaction's proof-completion marker are repository lifecycle
  -- authority here; the persisted minimized payload must name this exact full name/id.  The transfer marker can
  -- only be written by the proof-required transfer function after its locked live GitHub identity read matched.
  -- Account-level install/unsuspend events reactivate the account only; they cannot clear a repo tombstone because
  -- an All-repositories payload may omit its repository list and therefore cannot bind this call durably.
  IF v_delivery_key IS NOT NULL THEN
    SELECT d.received_at,d.event_type,d.payload->>'action'
      INTO v_delivery_received_at,v_delivery_event_type,v_delivery_action
      FROM core.webhook_delivery d
     WHERE d.delivery_key=v_delivery_key
       AND d.status='processing'
       AND d.account_key IN (
         v_account,
         CASE WHEN v_account LIKE 'ACCT-GH-%' THEN substr(v_account,9) ELSE v_account END)
       AND (
         (d.event_type='repository' AND d.payload->>'action'='created'
          AND d.repo=v_repo AND d.payload->'repository'->>'full_name'=v_repo
          AND (v_id IS NULL OR d.payload->'repository'->>'id'=v_id))
         OR
         (d.event_type='installation_repositories' AND d.payload->>'action'='added'
          AND EXISTS (
            SELECT 1
              FROM jsonb_array_elements(
                CASE WHEN jsonb_typeof(d.payload->'repositories_added')='array'
                     THEN d.payload->'repositories_added' ELSE '[]'::jsonb END
              ) AS added(repo)
             WHERE added.repo->>'full_name'=v_repo
               AND (v_id IS NULL OR added.repo->>'id'=v_id)))
         OR
         (d.event_type='repository' AND d.payload->>'action'='transferred'
          AND v_id IS NOT NULL
          AND d.payload->'repository'->>'id'=v_id
          AND jsonb_typeof(d.payload->'_veripsa_transfer_completed')='object'
          AND d.payload#>>'{_veripsa_transfer_completed,repository_id}'=v_id
          AND d.payload#>>'{_veripsa_transfer_completed,current_full_name}'=v_repo
          -- A rapid A→B→C move may legitimately use B's durable A→B row to
          -- isolate A, but it must never activate C's coordinate inside tenant B.
          -- Bind transfer activation to the live owner proven in the marker.
          AND d.payload#>>'{_veripsa_transfer_completed,current_owner_id}'=
              CASE WHEN v_account LIKE 'ACCT-GH-%' THEN substr(v_account,9) ELSE v_account END
          AND d.payload#>>'{_veripsa_transfer_completed,outcome}' IN ('purged','isolated'))
         )
     LIMIT 1;
    IF v_delivery_received_at IS NULL THEN
      RAISE EXCEPTION 'reactivate delivery key is not authoritative for repository'
        USING ERRCODE='23514';
    END IF;
  END IF;
  v_is_transfer := v_delivery_event_type='repository' AND v_delivery_action='transferred';
  IF v_is_transfer AND v_id IS NOT NULL THEN
    -- An exact transfer tombstone means this stable object is RETURNING to an account it previously left: clear the
    -- marker and start a new generation. With no such marker, already-observed same-ID work belongs to the current
    -- new owner and a delayed transfer must preserve its activation/generation boundary.
    SELECT EXISTS (
      SELECT 1 FROM core.repository_lifecycle_tombstone
       WHERE account_id=v_account AND repository_id=v_id
    ) INTO v_transfer_returning;
  END IF;
  v_activated_at := COALESCE(v_delivery_received_at,clock_timestamp());

  IF v_delivery_received_at IS NOT NULL THEN
    SELECT max(activated_at) INTO v_latest_activation_at
      FROM core.repository_lifecycle_activation
     WHERE account_id=v_account
       AND (repo=v_repo OR (v_id IS NOT NULL AND repository_id=v_id))
       -- A non-authoritative observation of this stable object is not lifecycle ordering authority, including after
       -- a rename to another coordinate. Different-id evidence at the payload coordinate remains contradictory.
       AND (lifecycle_authoritative OR v_id IS NULL OR repository_id<>v_id);
    SELECT max(lifecycle_received_at) INTO v_latest_tombstone_at
      FROM core.repository_lifecycle_tombstone
     WHERE account_id=v_account AND superseded_at IS NULL
       AND ((v_id IS NOT NULL AND repository_id=v_id)
            OR (v_repo IS NOT NULL AND repo=v_repo));
    -- A completed transfer carries a same-transaction CURRENT GitHub point proof, so it outranks an older local
    -- lifecycle high-water even when the transfer webhook itself was received earlier. The event processor holds
    -- the new coordinate lock while the proof and this activation run. created/added remain receive-order based.
    IF NOT v_is_transfer
       AND (v_latest_activation_at > v_activated_at OR v_latest_tombstone_at >= v_activated_at) THEN
      RETURN jsonb_build_object('ok',true,'cleared',0,'superseded',0,'activated',false,
                                'stale_lifecycle_event',true,'replacement_reset',NULL);
    END IF;
  END IF;

  IF v_id IS NOT NULL THEN
    -- Offboarding removes live activation authority, but an exact-id tombstone carries this object's original
    -- generation boundary. Re-selecting the same GitHub object must not hide its retained pre-deselect audit.
    SELECT min(boundary) INTO v_prior_generation_started_at
      FROM (
        SELECT generation_started_at AS boundary
          FROM core.repository_lifecycle_activation
         WHERE account_id=v_account AND repository_id=v_id
        UNION ALL
        SELECT generation_started_at AS boundary
          FROM core.repository_lifecycle_tombstone
         WHERE account_id=v_account AND repository_id=v_id
      ) same_object
     WHERE boundary IS NOT NULL;
  END IF;

  -- A successfully authenticated current lifecycle event is the only runtime authority allowed to recover a
  -- coordinate quarantined by a historical noncanonical id. Forget only this tenant+repo's ambiguous identity rows;
  -- the reselection reset below then removes any graph stamped with the unusable id before backfill.
  IF v_id IS NOT NULL AND v_repo IS NOT NULL THEN
    SELECT EXISTS (
      SELECT 1 FROM core.repository_lifecycle_tombstone
       WHERE account_id=v_account AND repo=v_repo AND repository_id<>'unknown'
         AND (length(repository_id)>32 OR repository_id !~ '^[1-9][0-9]*$')
      UNION ALL
      SELECT 1 FROM core.repository_lifecycle_activation
       WHERE account_id=v_account AND repo=v_repo
         AND (length(repository_id)>32 OR repository_id !~ '^[1-9][0-9]*$')
      UNION ALL
      SELECT 1 FROM core.graph_version
       WHERE account_id=v_account AND repo=v_repo AND repo_id IS NOT NULL
         AND (length(repo_id)>32 OR repo_id !~ '^[1-9][0-9]*$')
    ) INTO v_invalid_identity_reset;
    DELETE FROM core.repository_lifecycle_activation
     WHERE account_id=v_account AND repo=v_repo
       AND (length(repository_id)>32 OR repository_id !~ '^[1-9][0-9]*$');
    DELETE FROM core.repository_lifecycle_tombstone
     WHERE account_id=v_account AND repo=v_repo AND repository_id<>'unknown'
       AND (length(repository_id)>32 OR repository_id !~ '^[1-9][0-9]*$');
  END IF;

  -- A current repository.created / installation_repositories.added event is a real selection boundary. When its
  -- exact delivery has not already been applied, forget any pre-selection working set before backfill, even when
  -- GitHub reused the same stable repository id. This makes reverse processing converge with chronological
  -- remove→add processing: stale graph/claims/authority cannot survive merely because the add ran first. A duplicate
  -- delivery keeps the graph built after its first application, and account-level unsuspend/new-permissions events
  -- retain their graph by product contract.
  v_reselection_reset := v_invalid_identity_reset OR (v_delivery_received_at IS NOT NULL AND v_repo IS NOT NULL
    AND ((v_delivery_event_type='repository' AND v_delivery_action='created')
         OR (v_delivery_event_type='installation_repositories' AND v_delivery_action='added'))
    AND NOT EXISTS (
      SELECT 1 FROM core.repository_lifecycle_activation
       WHERE account_id=v_account AND repo=v_repo
         AND (v_id IS NULL OR repository_id=v_id)
         AND activated_at=v_activated_at));

  IF v_id IS NOT NULL AND v_repo IS NOT NULL THEN
    -- Capture any canonical stable object that still owns this coordinate before forgetting its rebuildable working
    -- set. Historical noncanonical ids are ambiguous quarantine residue, not old-object authority: carrying one into
    -- v_replaced_ids would recreate it as a tombstone after the purge and immediately quarantine the recovered
    -- coordinate again. The explicit GitHub lifecycle event is the authority boundary; backfill for the replacement
    -- runs only after this function returns, so this reset cannot delete replacement graph rows that do not exist yet.
    SELECT COALESCE(array_agg(DISTINCT old_id ORDER BY old_id),ARRAY[]::text[])
      INTO v_replaced_ids
      FROM (
        SELECT repo_id AS old_id FROM core.graph_version
         WHERE account_id=v_account AND repo=v_repo AND repo_id IS NOT NULL AND repo_id<>v_id
           AND length(repo_id)<=32 AND repo_id ~ '^[1-9][0-9]*$'
        UNION
        SELECT repository_id AS old_id FROM core.repository_lifecycle_activation
         WHERE account_id=v_account AND repo=v_repo AND repository_id<>v_id
           AND length(repository_id)<=32 AND repository_id ~ '^[1-9][0-9]*$'
      ) old_objects;
    -- A generic Core full/patch write deliberately clears graph_version.repo_id before the GitHub App can restamp
    -- it. During transfer reactivation that NULL graph may therefore be fresh current-owner work, not a predecessor;
    -- preserve it. A canonical different id remains positive replacement evidence and is reset above/below.
    SELECT NOT v_is_transfer AND EXISTS (
      SELECT 1 FROM core.graph_version
       WHERE account_id=v_account AND repo=v_repo AND repo_id IS NULL
    ) INTO v_legacy_replaced;
    v_generation_started_at := CASE
      WHEN v_is_transfer THEN clock_timestamp()
      WHEN v_invalid_identity_reset OR cardinality(v_replaced_ids)>0 OR v_legacy_replaced
        THEN clock_timestamp()
      WHEN v_prior_generation_started_at IS NOT NULL THEN v_prior_generation_started_at
      ELSE clock_timestamp()
    END;
    IF cardinality(v_replaced_ids) > 0 OR v_legacy_replaced OR v_reselection_reset THEN
      -- Replacement activation must forget the old coordinate before backfill, but it must NOT delete queued
      -- webhooks that may already belong to the new id. The activation mismatch guard safely rejects old queued
      -- work when it is processed; normal offboarding still purges the settled/queued inbox via the public wrapper.
      v_replacement_reset := core._purge_repo_with_authority(v_repo,false);
    END IF;
    -- One stable object owns a coordinate. A duplicate add for the same object preserves the first boundary; a
    -- different id at the same name receives a fresh boundary before any graph row can exist.
    DELETE FROM core.repository_lifecycle_activation
     WHERE account_id=v_account AND repo=v_repo AND repository_id<>v_id;
    INSERT INTO core.repository_lifecycle_activation AS existing(
        account_id,repository_id,repo,activated_at,lifecycle_authoritative,generation_started_at)
    VALUES (v_account,v_id,v_repo,v_activated_at,true,v_generation_started_at)
    ON CONFLICT (account_id,repository_id)
    DO UPDATE SET
                  -- Stable identity makes a work-observed coordinate the same object. Retain that coordinate
                  -- only when it was observed after this add's durable receive time; an older work row must yield to
                  -- the authenticated add's newer name.
                  repo=CASE WHEN v_is_transfer THEN EXCLUDED.repo
                            WHEN NOT existing.lifecycle_authoritative
                                  AND existing.repo IS DISTINCT FROM EXCLUDED.repo
                                  AND existing.activated_at > EXCLUDED.activated_at
                            THEN existing.repo ELSE EXCLUDED.repo END,
                  -- Work observation is not an ordering high-water. When an authenticated add arrives later in
                  -- processing but earlier in durable receive order, promote it at its own received_at so a newer
                  -- remove still wins. Existing lifecycle rows alone may retain the later lifecycle high-water.
                  activated_at=CASE WHEN v_is_transfer AND NOT v_transfer_returning
                                      THEN GREATEST(existing.activated_at,EXCLUDED.activated_at)
                                    WHEN existing.lifecycle_authoritative
                                    THEN GREATEST(existing.activated_at,EXCLUDED.activated_at)
                                    ELSE EXCLUDED.activated_at END,
                  lifecycle_authoritative=true,
                  generation_started_at=CASE
                    WHEN v_is_transfer AND v_transfer_returning
                      THEN EXCLUDED.generation_started_at
                    WHEN v_is_transfer
                      THEN COALESCE(existing.generation_started_at,existing.activated_at,
                                    EXCLUDED.generation_started_at)
                    WHEN v_invalid_identity_reset OR cardinality(v_replaced_ids)>0 OR v_legacy_replaced
                      THEN EXCLUDED.generation_started_at
                    WHEN existing.lifecycle_authoritative THEN existing.generation_started_at
                    ELSE EXCLUDED.generation_started_at END;
  END IF;
  DELETE FROM core.repository_lifecycle_tombstone
   WHERE account_id=v_account
     AND (
       (v_id IS NOT NULL AND repository_id=v_id)
       OR (v_repo IS NOT NULL AND repo=v_repo AND repository_id='unknown' AND superseded_at IS NULL)
     );
  GET DIAGNOSTICS v_cleared = ROW_COUNT;
  -- Replacement markers are written AFTER clearing a blocking legacy marker so duplicate lifecycle events cannot
  -- erase the audit/read boundary. Exact old ids stay blocked; a superseded unknown marker records a pre-id object
  -- for reader filtering while the activation table rejects no-id/old-id work at this coordinate.
  FOREACH v_old_id IN ARRAY v_replaced_ids LOOP
    INSERT INTO core.repository_lifecycle_tombstone AS existing(
        account_id,repository_id,repo,reason,lifecycle_received_at,superseded_at)
    VALUES (v_account,v_old_id,v_repo,'repository_deleted',v_activated_at,v_activated_at)
    ON CONFLICT (account_id,repository_id,repo)
    DO UPDATE SET superseded_at=COALESCE(existing.superseded_at,EXCLUDED.superseded_at);
  END LOOP;
  IF v_legacy_replaced AND v_repo IS NOT NULL THEN
    INSERT INTO core.repository_lifecycle_tombstone AS existing(
        account_id,repository_id,repo,reason,lifecycle_received_at,superseded_at)
    VALUES (v_account,'unknown',v_repo,'repository_deleted',v_activated_at,v_activated_at)
    ON CONFLICT (account_id,repository_id,repo)
    DO UPDATE SET superseded_at=COALESCE(existing.superseded_at,EXCLUDED.superseded_at);
  END IF;
  -- A different-id same-name replacement must not delete the old exact-id marker: that marker still rejects
  -- late old-object deliveries. Mark it superseded only for repo-level read routing and preserve the FIRST
  -- activation boundary so a duplicate lifecycle event cannot hide newer replacement activity.
  IF v_id IS NOT NULL AND v_repo IS NOT NULL THEN
    UPDATE core.repository_lifecycle_tombstone
       SET superseded_at=COALESCE(superseded_at,v_activated_at)
     WHERE account_id=v_account AND repo=v_repo
       AND repository_id<>'unknown' AND repository_id<>v_id;
    GET DIAGNOSTICS v_superseded = ROW_COUNT;
  END IF;
  RETURN jsonb_build_object('ok',true,'cleared',v_cleared,'superseded',v_superseded,
                            'activated',v_id IS NOT NULL AND v_repo IS NOT NULL,
                            'stale_lifecycle_event',false,
                            'lifecycle_reset',v_reselection_reset,
                            'legacy_replacement_reset',v_legacy_replaced,
                            'replacement_reset',v_replacement_reset);
END $$;
ALTER FUNCTION core.reactivate_repository_with_authority(text,text,text) OWNER TO veripsa_migrator;
REVOKE EXECUTE ON FUNCTION core.reactivate_repository_with_authority(text,text,text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION core.reactivate_repository_with_authority(text,text,text) TO veripsa_app;

-- Owner-only compatibility/operator reactivation. The App must never call this unordered overload: a mixed-version
-- worker could otherwise clear a newer tombstone with an old add. Live webhook processing uses the authenticated
-- three-argument function; an old worker fails closed and its durable delivery retries on the new worker.
CREATE OR REPLACE FUNCTION core.reactivate_repository_with_authority(p_repo text, p_repository_id text)
    RETURNS jsonb LANGUAGE sql SECURITY DEFINER SET search_path TO 'core','pg_catalog' AS $$
  SELECT core.reactivate_repository_with_authority(p_repo,p_repository_id,NULL::text)
$$;
ALTER FUNCTION core.reactivate_repository_with_authority(text,text) OWNER TO veripsa_migrator;
REVOKE EXECUTE ON FUNCTION core.reactivate_repository_with_authority(text,text) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION core.reactivate_repository_with_authority(text,text) FROM veripsa_app, veripsa_writer;

-- _purge_repo_with_authority: internal shared scoped reset. p_purge_inbox=true is normal offboarding; false is the
-- replacement-activation reset, which must preserve already-queued webhooks because they may belong to the new
-- stable repository id. Live App offboarding reaches this only through the durable, stable-id-aware four-argument
-- offboard_repository_with_authority boundary below.
-- OFFBOARDING (privacy table-stakes). The App was uninstalled, or the repo was
-- removed / deleted → FORGET the content-free WORKING SET we hold for this repo across ALL its coordinates
-- (every branch): the code graph (nodes/edges/version) + the live claim/lane state. The append-only event
-- ledger (push/landed audit, content-free) is RETAINED by design — a recorded fact is permanent (the
-- trg_append_only_event trigger refuses DELETE/UPDATE on core.event). Whether uninstall should ALSO purge
-- that content-free audit ledger (forget-on-uninstall vs immutable audit) is a PO decision — see RUNBOOK.md.
-- DELETE is admitted by tenant_isolation RLS for the pinned account (establish_session_write_context); the
-- forgery trigger is INSERT/UPDATE-only so it does not gate DELETE. App-delegation grant (veripsa_app only).
CREATE OR REPLACE FUNCTION core._purge_repo_with_authority(p_repo text, p_purge_inbox boolean)
    RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path TO 'core','pg_catalog' AS $$
DECLARE v_agent text; v_account text; v_gh_id text; v_repo text; v_nodes int; v_edges int; v_versions int; v_claims int;
        v_cochange int; v_cc_seen int; v_webhooks int; v_ws_members int; v_grants int; v_stores int;
        v_activations int;
BEGIN
  v_repo := left(NULLIF(btrim(COALESCE(p_repo,'')),''),512);
  IF v_repo IS NULL THEN RAISE EXCEPTION 'purge needs a repo' USING ERRCODE='23514'; END IF;
  SELECT agent, account INTO v_agent, v_account FROM core.establish_session_write_context() AS c(agent,account);
  -- Same repo→account lock order as full/patch writers. Take both before the first graph DELETE so a patch cannot
  -- pass CAS and then lose retained rows here before stamping a partial coordinate.
  PERFORM pg_advisory_xact_lock(
    hashtext(CASE WHEN v_account LIKE 'ACCT-GH-%' THEN substr(v_account,9) ELSE v_account END),
    hashtext(v_repo));
  PERFORM core._take_account_lifecycle_xact_lock_shared(v_account);
  DELETE FROM core.code_node     WHERE account_id=v_account AND repo=v_repo;  GET DIAGNOSTICS v_nodes    = ROW_COUNT;
  DELETE FROM core.code_edge     WHERE account_id=v_account AND repo=v_repo;  GET DIAGNOSTICS v_edges    = ROW_COUNT;
  DELETE FROM core.graph_version WHERE account_id=v_account AND repo=v_repo;  GET DIAGNOSTICS v_versions = ROW_COUNT;
  DELETE FROM core.claim         WHERE account_id=v_account AND repo=v_repo;  GET DIAGNOSTICS v_claims   = ROW_COUNT;
  -- CO-CHANGE (logical-coupling) cache + its idempotency ledger are real per-tenant data with a repo column —
  -- privacy parity with purge_account_working_set (audit P1): a repo REMOVED from an installation must forget its
  -- co-change file-pair paths + seen shas too, not only the code graph. RLS walls each DELETE to v_account+repo;
  -- not forgery-gated (the trg_governed_co_change trigger is INSERT/UPDATE-only, like the code-graph deletes above).
  DELETE FROM core.co_change             WHERE account_id=v_account AND repo=v_repo;  GET DIAGNOSTICS v_cochange = ROW_COUNT;
  DELETE FROM core.co_change_seen_commit WHERE account_id=v_account AND repo=v_repo;  GET DIAGNOSTICS v_cc_seen  = ROW_COUNT;
  -- Repo-scoped consent and authority are LIVE state, not audit history. Keeping an accepted workspace member,
  -- delegation grant, or GitHub store attachment after access is removed could make a same-name replacement
  -- inherit authority it never received. Remove only this tenant's exact coordinate.
  DELETE FROM core.workspace_member WHERE account_id=v_account AND repo=v_repo;
  GET DIAGNOSTICS v_ws_members = ROW_COUNT;
  DELETE FROM core.grant WHERE grantor_account=v_account AND repo=v_repo;
  GET DIAGNOSTICS v_grants = ROW_COUNT;
  DELETE FROM core.store_connection
   WHERE account_id=v_account AND provider='github' AND target=v_repo;
  GET DIAGNOSTICS v_stores = ROW_COUNT;
  DELETE FROM core.repository_lifecycle_activation
   WHERE account_id=v_account AND repo=v_repo;
  GET DIAGNOSTICS v_activations = ROW_COUNT;
  -- The durable inbox is part of the rebuildable repo working set. Remove settled/queued rows for this
  -- coordinate immediately instead of retaining private repo names until the fleet retention sweep. A row
  -- currently being processed is left for its owner to finish; the tombstone guard prevents it from writing.
  IF COALESCE(p_purge_inbox,false) THEN
    v_gh_id := CASE WHEN v_account LIKE 'ACCT-GH-%' THEN substr(v_account,9) ELSE NULL END;
    DELETE FROM core.webhook_delivery
     WHERE repo=v_repo
       AND (account_key=v_account OR (v_gh_id IS NOT NULL AND account_key=v_gh_id))
       AND status <> 'processing';
    GET DIAGNOSTICS v_webhooks = ROW_COUNT;
  ELSE
    v_webhooks := 0;
  END IF;
  RETURN jsonb_build_object('ok',true,'repo',v_repo,'purged',
    jsonb_build_object('nodes',v_nodes,'edges',v_edges,'versions',v_versions,'claims',v_claims,
                       'cochange',v_cochange,'cochange_seen',v_cc_seen,
                       'workspace_members',v_ws_members,'grants',v_grants,
                       'store_connections',v_stores,'repository_activations',v_activations,
                       'webhook_deliveries',v_webhooks,'webhook_inbox_purged',COALESCE(p_purge_inbox,false)));
END $$;
ALTER FUNCTION core._purge_repo_with_authority(text,boolean) OWNER TO veripsa_migrator;
REVOKE EXECUTE ON FUNCTION core._purge_repo_with_authority(text,boolean)
  FROM PUBLIC, veripsa_writer, veripsa_app;

-- offboard_repository_with_authority: stable-id-aware, retry-safe repository deletion. A late delete
-- carrying an OLD full_name resolves every stored coordinate with the same repository.id; a stale delete for
-- an old repository object never purges a same-name replacement whose stored id differs. Each target also takes
-- the exact advisory key used by the live processor, closing rename-name lock asymmetry before purge.
CREATE OR REPLACE FUNCTION core.offboard_repository_with_authority(
    p_repo text, p_repository_id text, p_reason text, p_delivery_key text)
    RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path TO 'core','pg_catalog' AS $$
DECLARE
  v_agent text; v_account text; v_pinned_account text; v_repo text; v_id text; v_reason text;
  v_marker_id text; v_delivery_key text; v_delivery_repository_id text;
  v_delivery_received_at timestamptz; v_lifecycle_received_at timestamptz;
  v_replacement_since timestamptz; v_same_id_activation_at timestamptz; v_same_id_repo text;
  v_prior_generation_started_at timestamptz;
  v_marker_superseded_at timestamptz; v_name_only_stale boolean := false;
  v_unrouted_tombstone_pin boolean := false;
  v_stale_ws int := 0; v_stale_grants int := 0; v_stale_stores int := 0;
  v_webhooks int := 0; v_target_webhooks int := 0; v_gh_id text;
  v_target text; v_targets text[] := ARRAY[]::text[]; v_results jsonb := '[]'::jsonb;
  v_lock_candidates text[] := ARRAY[]::text[];
  v_rechecked_lock_candidates text[] := ARRAY[]::text[];
  v_one jsonb;
BEGIN
  v_repo := left(NULLIF(btrim(COALESCE(p_repo,'')),''),512);
  v_id := NULLIF(btrim(COALESCE(p_repository_id,'')),'');
  v_delivery_key := left(NULLIF(btrim(COALESCE(p_delivery_key,'')),''),200);
  v_reason := CASE WHEN p_reason='repository_deleted' THEN 'repository_deleted' ELSE 'installation_removed' END;
  IF v_repo IS NULL THEN RAISE EXCEPTION 'offboard needs a repo' USING ERRCODE='23514'; END IF;
  IF v_id IS NOT NULL AND (length(v_id) > 32 OR v_id !~ '^[1-9][0-9]*$') THEN
    RAISE EXCEPTION 'offboard repository id must be a positive canonical ASCII decimal' USING ERRCODE='23514';
  END IF;
  IF v_delivery_key IS NULL THEN
    RAISE EXCEPTION 'repository offboard needs durable delivery authority' USING ERRCODE='42501';
  END IF;
  -- GDPR erase removes the installation route, so ordinary identity resolution intentionally fails even though
  -- enter_installation pinned the stable account. Permit only the narrow post-erase cleanup edge here: the exact
  -- processing delivery below must bind that pin, and after the normal stable-id→repo→account lock order the active
  -- tombstone must still exist. No route + no active tombstone remains fail-closed.
  v_pinned_account := NULLIF(current_setting('core.installation_account',true),'');
  BEGIN
    SELECT agent, account INTO v_agent, v_account
      FROM core.establish_session_write_context() AS c(agent,account);
  EXCEPTION WHEN insufficient_privilege THEN
    v_account := v_pinned_account;
    IF v_account IS NULL THEN
      RAISE;
    END IF;
    v_agent := session_user;
    v_unrouted_tombstone_pin := true;
  END;
  -- After GDPR erase the route is gone. establish_session_write_context can then fall through to the App's local
  -- credential, which is a valid identity for a *different* tenant rather than an exception. Never authorize the
  -- webhook against that fallback: retain the trusted installation-derived pin and require the active tombstone
  -- narrow path below. Without an active tombstone this still aborts after the lifecycle lock.
  IF v_pinned_account IS NOT NULL AND v_account IS DISTINCT FROM v_pinned_account THEN
    v_account := v_pinned_account;
    v_agent := session_user;
    v_unrouted_tombstone_pin := true;
  END IF;

  -- The durable delivery id is content-free ordering evidence, not caller-supplied time. Resolve received_at only
  -- from the processing inbox row for this tenant, event kind, and repository. A mismatched key fails loudly so
  -- the durable worker retries instead of making an unauthenticated over-delete/under-delete decision.
  IF v_delivery_key IS NOT NULL THEN
    SELECT d.received_at,
           CASE
             WHEN v_reason='repository_deleted'
               THEN NULLIF(btrim(d.payload->'repository'->>'id'),'')
             ELSE (
               SELECT CASE WHEN count(*)=1
                           THEN min(NULLIF(btrim(removed.repo->>'id'),''))
                           ELSE '__ambiguous__' END
                 FROM jsonb_array_elements(
                   CASE WHEN jsonb_typeof(d.payload->'repositories_removed')='array'
                        THEN d.payload->'repositories_removed' ELSE '[]'::jsonb END
                 ) AS removed(repo)
                WHERE removed.repo->>'full_name'=v_repo
                  AND (v_id IS NULL OR removed.repo->>'id'=v_id))
           END
      INTO v_delivery_received_at,v_delivery_repository_id
      FROM core.webhook_delivery d
     WHERE d.delivery_key=v_delivery_key
       AND d.status='processing'
       AND d.account_key IN (
         v_account,
         CASE WHEN v_account LIKE 'ACCT-GH-%' THEN substr(v_account,9) ELSE v_account END)
       AND (
         (v_reason='repository_deleted'
          AND d.event_type='repository' AND d.payload->>'action'='deleted'
          AND d.repo=v_repo AND d.payload->'repository'->>'full_name'=v_repo
          AND (v_id IS NULL OR d.payload->'repository'->>'id'=v_id))
         OR
         (v_reason='installation_removed'
          AND d.event_type='installation_repositories' AND d.payload->>'action'='removed'
          AND EXISTS (
            SELECT 1
              FROM jsonb_array_elements(
                CASE WHEN jsonb_typeof(d.payload->'repositories_removed')='array'
                     THEN d.payload->'repositories_removed' ELSE '[]'::jsonb END
              ) AS removed(repo)
             WHERE removed.repo->>'full_name'=v_repo
               AND (v_id IS NULL OR removed.repo->>'id'=v_id))))
     LIMIT 1;
    IF v_delivery_received_at IS NULL THEN
      RAISE EXCEPTION 'offboard delivery key is not authoritative for repository'
        USING ERRCODE='23514';
    END IF;
    IF v_delivery_repository_id IS NOT NULL
       AND (length(v_delivery_repository_id) > 32
            OR v_delivery_repository_id !~ '^[1-9][0-9]*$') THEN
      RAISE EXCEPTION 'offboard delivery carries a noncanonical repository id' USING ERRCODE='23514';
    END IF;
    -- The authenticated minimized payload is the source of truth. A caller may omit the id for a mixed-version
    -- worker, but it cannot downgrade an ID-bearing deletion to mutable-name-only semantics.
    IF v_id IS NULL THEN
      v_id := v_delivery_repository_id;
    ELSIF v_delivery_repository_id IS NOT NULL AND v_id<>v_delivery_repository_id THEN
      RAISE EXCEPTION 'offboard repository id does not match durable delivery' USING ERRCODE='23514';
    END IF;
  END IF;
  v_marker_id := COALESCE(v_id, 'unknown');
  v_lifecycle_received_at := v_delivery_received_at;
  IF v_id IS NOT NULL THEN
    PERFORM pg_advisory_xact_lock(hashtext('github-repository-id'),hashtext(v_id));
  END IF;
  -- Discover and lock every coordinate that could carry this stable object (plus the signed name fallback) BEFORE
  -- lifecycle ordering/target selection. Work/onboarding/reconcile is held by the global ID key; explicit rename
  -- is held by these source-coordinate keys. A changed post-lock snapshot retries instead of purging a stale name.
  SELECT COALESCE(array_agg(candidate.repo ORDER BY candidate.repo),ARRAY[]::text[])
    INTO v_lock_candidates
    FROM (
      SELECT v_repo AS repo
      UNION
      SELECT a.repo FROM core.repository_lifecycle_activation a
       WHERE v_id IS NOT NULL AND a.account_id=v_account AND a.repository_id=v_id
      UNION
      SELECT g.repo FROM core.graph_version g
       WHERE v_id IS NOT NULL AND g.account_id=v_account AND g.repo_id=v_id
    ) candidate
   WHERE candidate.repo IS NOT NULL AND candidate.repo<>'';
  FOREACH v_target IN ARRAY v_lock_candidates LOOP
    PERFORM pg_advisory_xact_lock(
      hashtext(CASE WHEN v_account LIKE 'ACCT-GH-%' THEN substr(v_account,9) ELSE v_account END),
      hashtext(v_target));
  END LOOP;
  SELECT COALESCE(array_agg(candidate.repo ORDER BY candidate.repo),ARRAY[]::text[])
    INTO v_rechecked_lock_candidates
    FROM (
      SELECT v_repo AS repo
      UNION
      SELECT a.repo FROM core.repository_lifecycle_activation a
       WHERE v_id IS NOT NULL AND a.account_id=v_account AND a.repository_id=v_id
      UNION
      SELECT g.repo FROM core.graph_version g
       WHERE v_id IS NOT NULL AND g.account_id=v_account AND g.repo_id=v_id
    ) candidate
   WHERE candidate.repo IS NOT NULL AND candidate.repo<>'';
  IF v_rechecked_lock_candidates IS DISTINCT FROM v_lock_candidates THEN
    RAISE EXCEPTION 'offboard repository coordinate snapshot changed while acquiring locks'
      USING ERRCODE='40001';
  END IF;
  -- Account purge/erase owns the broader privacy boundary.  Coordinate locks are acquired first to match boot's
  -- stable-id→repo→account order; the account fence then serializes this whole mutation with purge/erase.  If that
  -- boundary already won, make the claimed repository event a content-free receipt and perform no repo writes —
  -- otherwise a processing installation_repositories.removed worker could recreate repo ids/names after GDPR erase.
  PERFORM core._take_account_lifecycle_xact_lock(v_account);
  IF EXISTS (
    SELECT 1 FROM core.account_lifecycle_tombstone
     WHERE account_id=v_account AND active
  ) THEN
    UPDATE core.webhook_delivery
       SET event_type='erased',account_key=NULL,repo=NULL,payload='{}'::jsonb,updated_at=now()
     WHERE delivery_key=v_delivery_key AND status='processing';
    RETURN jsonb_build_object('ok',true,'repo',v_repo,'repository_id',v_id,
                              'stale_account_lifecycle',true,
                              'targets','[]'::jsonb,'results','[]'::jsonb);
  END IF;
  IF v_unrouted_tombstone_pin THEN
    RAISE EXCEPTION 'repository offboard account lifecycle changed while acquiring locks'
      USING ERRCODE='40001';
  END IF;
  IF v_id IS NOT NULL THEN
    SELECT min(generation_started_at) INTO v_prior_generation_started_at
      FROM core.repository_lifecycle_activation
     WHERE account_id=v_account AND repository_id=v_id;
  END IF;
  -- A pre-fix worker may have persisted an id-less deletion. GitHub's repository/installation view can lag the
  -- webhook briefly, so never make the destructive name-only decision during that consistency window. Return it
  -- to the queue at received_at+5m without consuming an attempt; the new worker then performs a current GitHub
  -- point read before choosing purge vs preserve-current. Generic stale recovery is deliberately not the timer.
  IF v_id IS NULL AND v_delivery_received_at > clock_timestamp()-interval '5 minutes' THEN
    IF NOT core._defer_webhook_delivery_with_authority(
        v_delivery_key,v_delivery_received_at+interval '5 minutes',
        'legacy repository identity consistency window') THEN
      RAISE EXCEPTION 'legacy repository offboard could not schedule identity resolution'
        USING ERRCODE='55000';
    END IF;
    RETURN jsonb_build_object('ok',true,'repo',v_repo,'repository_id',NULL,
                              'marker_repository_id','unknown','revoked',false,
                              'deferred',true,'defer_reason','legacy_identity_consistency_window',
                              'targets','[]'::jsonb,'results','[]'::jsonb);
  END IF;

  -- A later re-add of the SAME selected repository id wins over an older remove that happened to process late.
  -- Do not write a tombstone at all in this case: exact-id markers intentionally block even when superseded, so
  -- writing one would undo the newer activation. Different-id replacement ordering is handled below.
  IF v_id IS NOT NULL AND v_delivery_received_at IS NOT NULL THEN
    SELECT repo,activated_at INTO v_same_id_repo,v_same_id_activation_at
      FROM core.repository_lifecycle_activation
     WHERE account_id=v_account AND repository_id=v_id
       AND lifecycle_authoritative
     ORDER BY activated_at DESC LIMIT 1;
    IF v_same_id_activation_at > v_delivery_received_at THEN
      -- Reverse processing must converge with chronological remove→re-add. The newer activation owns the current
      -- graph, but coordinate authority created before that boundary belonged to the pre-removal selection and must
      -- not leak forward. Preserve authority refreshed at/after reactivation.
      DELETE FROM core.workspace_member
       WHERE account_id=v_account AND repo IN (v_repo,v_same_id_repo)
         AND (CASE WHEN consent_state='accepted' THEN COALESCE(consented_at,joined_at)
                   ELSE joined_at END) < v_same_id_activation_at;
      GET DIAGNOSTICS v_stale_ws = ROW_COUNT;
      DELETE FROM core.grant
       WHERE grantor_account=v_account AND repo IN (v_repo,v_same_id_repo)
         AND granted_at < v_same_id_activation_at;
      GET DIAGNOSTICS v_stale_grants = ROW_COUNT;
      DELETE FROM core.store_connection
       WHERE account_id=v_account AND provider='github' AND target IN (v_repo,v_same_id_repo)
         AND connected_at < v_same_id_activation_at;
      GET DIAGNOSTICS v_stale_stores = ROW_COUNT;
      UPDATE core.webhook_delivery
         SET payload=jsonb_set(
               payload,'{_veripsa_offboard_completed}',
               COALESCE(payload->'_veripsa_offboard_completed','{}'::jsonb)
                 || jsonb_build_object(v_repo,v_marker_id),true),
             updated_at=now()
       WHERE delivery_key=v_delivery_key AND status='processing';
      RETURN jsonb_build_object('ok',true,'repo',v_repo,'repository_id',v_id,
                                'marker_repository_id',v_marker_id,'revoked',false,
                                'stale_lifecycle_event',true,'delivery_order','stale_before_reactivation',
                                'targets','[]'::jsonb,'results','[]'::jsonb,
                                'stale_authority_purged',jsonb_build_object(
                                  'workspace_members',v_stale_ws,'grants',v_stale_grants,
                                  'store_connections',v_stale_stores));
    END IF;
  END IF;

  -- A replacement observation gives a boundary, but it does not by itself prove an id-less delete is stale:
  -- the actual current repository may have been deleted after that observation. Only an authenticated durable
  -- receive time older than the boundary proves this legacy delete belonged to the predecessor.
  SELECT min(observed_at) INTO v_replacement_since
    FROM (
      SELECT activated_at AS observed_at
        FROM core.repository_lifecycle_activation
       WHERE account_id=v_account AND repo=v_repo
         AND length(repository_id)<=32 AND repository_id ~ '^[1-9][0-9]*$'
         AND (v_id IS NULL OR repository_id<>v_id)
      UNION ALL
      SELECT ingested_at AS observed_at
        FROM core.graph_version
       WHERE account_id=v_account AND repo=v_repo AND repo_id IS NOT NULL
         AND length(repo_id)<=32 AND repo_id ~ '^[1-9][0-9]*$'
         AND (v_id IS NULL OR repo_id<>v_id)
    ) replacement_observations;
  v_name_only_stale := v_id IS NULL
                        AND v_delivery_received_at IS NOT NULL
                        AND v_replacement_since IS NOT NULL
                        AND v_delivery_received_at < v_replacement_since;
  v_marker_superseded_at := CASE
    WHEN v_id IS NOT NULL OR v_name_only_stale THEN v_replacement_since
    ELSE NULL
  END;

  IF v_id IS NOT NULL THEN
    SELECT COALESCE(array_agg(DISTINCT repo ORDER BY repo), ARRAY[]::text[])
      INTO v_targets
      FROM (
        SELECT repo FROM core.graph_version
         WHERE account_id=v_account AND repo_id=v_id
        UNION
        SELECT repo FROM core.repository_lifecycle_activation
         WHERE account_id=v_account AND repository_id=v_id
      ) old_coordinate
       WHERE NOT EXISTS (
         SELECT 1 FROM core.repository_lifecycle_activation active
          WHERE active.account_id=v_account AND active.repo=old_coordinate.repo
            AND active.repository_id IS DISTINCT FROM v_id)
       AND NOT EXISTS (
         SELECT 1 FROM core.graph_version replacement_graph
          WHERE replacement_graph.account_id=v_account AND replacement_graph.repo=old_coordinate.repo
            AND replacement_graph.repo_id IS DISTINCT FROM v_id);
  END IF;
  -- Name fallback for a pre-repository-id coordinate. An id-less delete purges the live coordinate unless its
  -- durable receipt predates a later replacement boundary; merely seeing a stable id is not enough to skip a
  -- real delete. For an exact-id delete, every row at a coordinate must positively carry that exact id. A NULL,
  -- different or legacy/noncanonical id is ambiguous replacement state, so preserve/isolate the whole coordinate.
  IF NOT (v_repo = ANY(v_targets)) AND (
      (v_id IS NULL AND NOT v_name_only_stale)
      OR
      (v_id IS NOT NULL
       AND NOT EXISTS (
         SELECT 1 FROM core.graph_version
          WHERE account_id=v_account AND repo=v_repo AND repo_id IS DISTINCT FROM v_id)
       AND NOT EXISTS (
         SELECT 1 FROM core.repository_lifecycle_activation
          WHERE account_id=v_account AND repo=v_repo
            AND repository_id IS DISTINCT FROM v_id))) THEN
    v_targets := array_append(v_targets, v_repo);
  END IF;

  -- Record the signed deletion coordinate even when a same-name replacement means there is no working-set
  -- target to purge. Otherwise a late delete would preserve the replacement correctly but fail to block an
  -- even later work-event redelivery for the deleted stable id.
  -- A late old-object delete must not purge the replacement graph, but repo-name-only authority from BEFORE
  -- the replacement must not carry forward either. Prefer the explicit lifecycle activation boundary (available
  -- before backfill); graph ingest remains the compatibility fallback for repositories activated before this state
  -- existed. Keep authority refreshed after that boundary; remove only rows provably older than it.
  IF v_marker_superseded_at IS NOT NULL THEN
    DELETE FROM core.workspace_member
     WHERE account_id=v_account AND repo=v_repo
       AND (CASE WHEN consent_state='accepted' THEN COALESCE(consented_at,joined_at)
                 ELSE joined_at END) < v_marker_superseded_at;
    GET DIAGNOSTICS v_stale_ws = ROW_COUNT;
    DELETE FROM core.grant
     WHERE grantor_account=v_account AND repo=v_repo AND granted_at < v_marker_superseded_at;
    GET DIAGNOSTICS v_stale_grants = ROW_COUNT;
    DELETE FROM core.store_connection
     WHERE account_id=v_account AND provider='github' AND target=v_repo
       AND connected_at < v_marker_superseded_at;
    GET DIAGNOSTICS v_stale_stores = ROW_COUNT;
  END IF;
  INSERT INTO core.repository_lifecycle_tombstone AS existing(
      account_id,repository_id,repo,reason,lifecycle_received_at,superseded_at,generation_started_at)
  VALUES (v_account,v_marker_id,v_repo,v_reason,v_lifecycle_received_at,v_marker_superseded_at,
          v_prior_generation_started_at)
  ON CONFLICT (account_id,repository_id,repo)
  DO UPDATE SET reason=EXCLUDED.reason,tombstoned_at=now(),
                lifecycle_received_at=EXCLUDED.lifecycle_received_at,
                superseded_at=EXCLUDED.superseded_at,
                generation_started_at=COALESCE(existing.generation_started_at,
                                               EXCLUDED.generation_started_at)
    WHERE existing.lifecycle_received_at <= EXCLUDED.lifecycle_received_at;

  FOREACH v_target IN ARRAY v_targets LOOP
    -- Same two-int advisory key as server_dbops._take_repo_lock(account, repo). This also serializes a
    -- rename-resolved target whose name differs from the deletion payload's already-held session lock.
    PERFORM pg_advisory_xact_lock(hashtext(
      CASE WHEN v_account LIKE 'ACCT-GH-%' THEN substr(v_account,9) ELSE v_account END), hashtext(v_target));
    INSERT INTO core.repository_lifecycle_tombstone AS existing(
        account_id,repository_id,repo,reason,lifecycle_received_at,superseded_at,generation_started_at)
    VALUES (v_account,v_marker_id,v_target,v_reason,v_lifecycle_received_at,NULL,
            v_prior_generation_started_at)
    ON CONFLICT (account_id,repository_id,repo)
    DO UPDATE SET reason=EXCLUDED.reason,tombstoned_at=now(),
                  lifecycle_received_at=EXCLUDED.lifecycle_received_at,superseded_at=NULL,
                  generation_started_at=COALESCE(existing.generation_started_at,
                                                 EXCLUDED.generation_started_at)
      WHERE existing.lifecycle_received_at <= EXCLUDED.lifecycle_received_at;
    -- Purge the working set first, but do not use the coordinate-wide inbox delete. A newer same-name replacement
    -- can already have a repository.created/first-push row queued under this exact full_name while the old object's
    -- delete is processing. GitHub already received 202 for that row, so deleting it would be permanent event loss.
    v_one := core._purge_repo_with_authority(v_target,false);
    v_gh_id := CASE WHEN v_account LIKE 'ACCT-GH-%' THEN substr(v_account,9) ELSE NULL END;
    DELETE FROM core.webhook_delivery d
     WHERE d.repo=v_target
       AND (d.account_key=v_account OR (v_gh_id IS NOT NULL AND d.account_key=v_gh_id))
       AND d.status<>'processing'
       AND (
         -- Modern minimized deliveries retain repository.id. Same old object is safe to remove even when its
         -- redelivery arrived late; a different-id replacement is always preserved regardless of receive order.
         (v_id IS NOT NULL AND d.payload->'repository'->>'id'=v_id)
         OR
         -- A completed row has no executable work left and its payload has already been cleared. Remove only rows
         -- no newer than this lifecycle boundary. Ambiguous queued/failed legacy rows are preserved: the tombstone
         -- guard will reject stale work, while a genuine replacement keeps the 202'd delivery GitHub will not resend.
         (d.status='done'
          AND NULLIF(d.payload->'repository'->>'id','') IS NULL
          AND d.received_at<=v_lifecycle_received_at)
       );
    GET DIAGNOSTICS v_target_webhooks = ROW_COUNT;
    v_webhooks := v_webhooks + v_target_webhooks;
    v_one := jsonb_set(v_one,'{purged,webhook_deliveries}',to_jsonb(v_target_webhooks),true);
    v_one := jsonb_set(v_one,'{purged,webhook_inbox_purged}','true'::jsonb,true);
    v_one := jsonb_set(v_one,'{purged,webhook_inbox_mode}',to_jsonb('stable_id_selective'::text),true);
    v_results := v_results || jsonb_build_array(v_one);
  END LOOP;
  -- The durable owner finalizes only after every repository named by this deletion is marked complete. This marker
  -- is a bounded content-free map (full_name -> stable id/unknown) inside the already-minimized processing payload;
  -- finish clears the payload. It prevents an old mixed-version worker from swallowing an error and marking residue
  -- done, including a multi-repository removal where an earlier sibling succeeded and a later sibling failed.
  UPDATE core.webhook_delivery
     SET payload=jsonb_set(
           payload,'{_veripsa_offboard_completed}',
           COALESCE(payload->'_veripsa_offboard_completed','{}'::jsonb)
             || jsonb_build_object(v_repo,v_marker_id),true),
         updated_at=now()
   WHERE delivery_key=v_delivery_key AND status='processing';
  RETURN jsonb_build_object('ok',true,'repo',v_repo,'repository_id',v_id,
                            'marker_repository_id',v_marker_id,'revoked',true,
                            'stale_lifecycle_event',false,
                            'delivery_order',CASE
                              WHEN v_name_only_stale THEN 'stale_before_replacement'
                              WHEN v_id IS NULL THEN 'current_or_later'
                              ELSE 'stable_id'
                            END,
                            'targets',to_jsonb(v_targets),'results',v_results,
                            'selective_webhook_deliveries_purged',v_webhooks,
                            'stale_authority_purged',jsonb_build_object(
                              'workspace_members',v_stale_ws,'grants',v_stale_grants,
                              'store_connections',v_stale_stores));
END $$;
ALTER FUNCTION core.offboard_repository_with_authority(text,text,text,text) OWNER TO veripsa_migrator;
REVOKE EXECUTE ON FUNCTION core.offboard_repository_with_authority(text,text,text,text)
  FROM PUBLIC, veripsa_writer;
GRANT EXECUTE ON FUNCTION core.offboard_repository_with_authority(text,text,text,text) TO veripsa_app;

-- Before a new worker asks GitHub to resolve a pre-fix ID-less deletion, authenticate the durable row and enforce
-- the bounded consistency window in the database. Fresh work returns to the due-time queue with its claimed attempt
-- restored, so transient GitHub reads cannot burn the durable retry budget before the point read is meaningful.
CREATE OR REPLACE FUNCTION core.prepare_legacy_repository_offboard_with_authority(
    p_repo text, p_reason text, p_delivery_key text)
    RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path TO 'core','pg_catalog' AS $$
DECLARE
  v_account text; v_repo text; v_reason text; v_delivery_key text;
  v_delivery_received_at timestamptz;
BEGIN
  v_repo := left(NULLIF(btrim(COALESCE(p_repo,'')),''),512);
  v_delivery_key := left(NULLIF(btrim(COALESCE(p_delivery_key,'')),''),200);
  v_reason := CASE WHEN p_reason='repository_deleted' THEN 'repository_deleted' ELSE 'installation_removed' END;
  IF v_repo IS NULL OR v_delivery_key IS NULL THEN
    RAISE EXCEPTION 'legacy offboard preparation needs repo and durable delivery authority'
      USING ERRCODE='42501';
  END IF;
  SELECT account INTO v_account
    FROM core.establish_session_write_context() AS c(agent,account);

  SELECT d.received_at INTO v_delivery_received_at
    FROM core.webhook_delivery d
   WHERE d.delivery_key=v_delivery_key AND d.status='processing'
     AND d.account_key IN (
       v_account,
       CASE WHEN v_account LIKE 'ACCT-GH-%' THEN substr(v_account,9) ELSE v_account END)
     AND (
       (v_reason='repository_deleted'
        AND d.event_type='repository' AND d.payload->>'action'='deleted'
        AND d.repo=v_repo AND d.payload->'repository'->>'full_name'=v_repo
        AND NULLIF(btrim(d.payload->'repository'->>'id'),'') IS NULL)
       OR
       (v_reason='installation_removed'
        AND d.event_type='installation_repositories' AND d.payload->>'action'='removed'
        AND EXISTS (
          SELECT 1 FROM jsonb_array_elements(
            CASE WHEN jsonb_typeof(d.payload->'repositories_removed')='array'
                 THEN d.payload->'repositories_removed' ELSE '[]'::jsonb END
          ) AS removed(repo)
          WHERE removed.repo->>'full_name'=v_repo
            AND NULLIF(btrim(removed.repo->>'id'),'') IS NULL)
        AND NOT EXISTS (
          SELECT 1 FROM jsonb_array_elements(
            CASE WHEN jsonb_typeof(d.payload->'repositories_removed')='array'
                 THEN d.payload->'repositories_removed' ELSE '[]'::jsonb END
          ) AS removed(repo)
          WHERE removed.repo->>'full_name'=v_repo
            AND NULLIF(btrim(removed.repo->>'id'),'') IS NOT NULL)))
   LIMIT 1;
  IF v_delivery_received_at IS NULL THEN
    RAISE EXCEPTION 'legacy offboard delivery key is not authoritative for repository'
      USING ERRCODE='23514';
  END IF;
  IF v_delivery_received_at > clock_timestamp()-interval '5 minutes' THEN
    IF NOT core._defer_webhook_delivery_with_authority(
        v_delivery_key,v_delivery_received_at+interval '5 minutes',
        'legacy repository identity consistency window') THEN
      RAISE EXCEPTION 'legacy repository offboard could not schedule identity resolution'
        USING ERRCODE='55000';
    END IF;
    RETURN jsonb_build_object('ok',true,'repo',v_repo,'repository_id',NULL,
                              'deferred',true,
                              'defer_reason','legacy_identity_consistency_window');
  END IF;
  RETURN jsonb_build_object('ok',true,'repo',v_repo,'repository_id',NULL,
                            'deferred',false,'identity_resolution_due',true);
END $$;
ALTER FUNCTION core.prepare_legacy_repository_offboard_with_authority(text,text,text)
  OWNER TO veripsa_migrator;
REVOKE EXECUTE ON FUNCTION core.prepare_legacy_repository_offboard_with_authority(text,text,text)
  FROM PUBLIC, veripsa_writer;
GRANT EXECUTE ON FUNCTION core.prepare_legacy_repository_offboard_with_authority(text,text,text)
  TO veripsa_app;

-- A 404 from the installation-token point read is CURRENT absence authority, not merely evidence about the old
-- delivery's order. Purge the exact name coordinate even when a replacement had been observed after that legacy
-- receipt: the installation can no longer access that replacement either. The unsuperseded unknown marker blocks
-- stale work until a future explicit add/create event proves a new authorized object.
CREATE OR REPLACE FUNCTION core.confirm_absent_legacy_repository_offboard_with_authority(
    p_repo text, p_reason text, p_delivery_key text)
    RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path TO 'core','pg_catalog' AS $$
DECLARE
  v_account text; v_repo text; v_reason text; v_delivery_key text;
  v_gate jsonb; v_purged jsonb; v_confirmed_at timestamptz; v_completed int;
BEGIN
  v_repo := left(NULLIF(btrim(COALESCE(p_repo,'')),''),512);
  v_delivery_key := left(NULLIF(btrim(COALESCE(p_delivery_key,'')),''),200);
  v_reason := CASE WHEN p_reason='repository_deleted' THEN 'repository_deleted' ELSE 'installation_removed' END;
  v_gate := core.prepare_legacy_repository_offboard_with_authority(
    v_repo,v_reason,v_delivery_key);
  IF COALESCE((v_gate->>'deferred')::boolean,false) THEN
    RETURN v_gate;
  END IF;
  SELECT account INTO v_account
    FROM core.establish_session_write_context() AS c(agent,account);
  PERFORM pg_advisory_xact_lock(
    hashtext(CASE WHEN v_account LIKE 'ACCT-GH-%' THEN substr(v_account,9) ELSE v_account END),
    hashtext(v_repo));
  v_confirmed_at := clock_timestamp();
  INSERT INTO core.repository_lifecycle_tombstone AS existing(
      account_id,repository_id,repo,reason,lifecycle_received_at,superseded_at)
  VALUES (v_account,'unknown',v_repo,v_reason,v_confirmed_at,NULL)
  ON CONFLICT (account_id,repository_id,repo)
  DO UPDATE SET reason=EXCLUDED.reason,tombstoned_at=now(),
                lifecycle_received_at=GREATEST(existing.lifecycle_received_at,EXCLUDED.lifecycle_received_at),
                superseded_at=NULL;
  v_purged := core._purge_repo_with_authority(v_repo,false);
  UPDATE core.webhook_delivery
     SET payload=jsonb_set(
           payload,'{_veripsa_offboard_completed}',
           COALESCE(payload->'_veripsa_offboard_completed','{}'::jsonb)
             || jsonb_build_object(v_repo,'unknown'),true),
         updated_at=now()
   WHERE delivery_key=v_delivery_key AND status='processing';
  GET DIAGNOSTICS v_completed = ROW_COUNT;
  IF v_completed<>1 THEN
    RAISE EXCEPTION 'legacy repository absence lost durable delivery ownership'
      USING ERRCODE='55000';
  END IF;
  RETURN jsonb_build_object('ok',true,'repo',v_repo,'repository_id',NULL,
                            'marker_repository_id','unknown','revoked',true,
                            'confirmed_absent',true,'deferred',false,
                            'delivery_order','github_confirmed_absent',
                            'targets',jsonb_build_array(v_repo),
                            'results',jsonb_build_array(v_purged));
END $$;
ALTER FUNCTION core.confirm_absent_legacy_repository_offboard_with_authority(text,text,text)
  OWNER TO veripsa_migrator;
REVOKE EXECUTE ON FUNCTION core.confirm_absent_legacy_repository_offboard_with_authority(text,text,text)
  FROM PUBLIC, veripsa_writer;
GRANT EXECUTE ON FUNCTION core.confirm_absent_legacy_repository_offboard_with_authority(text,text,text)
  TO veripsa_app;

-- Resolve a legacy id-less deletion after the new worker performs a current GitHub installation-token point read.
-- A same-full_name repository that is still visible is current authority (typically a replacement): preserve only
-- working state already bound cleanly to that id, reset ambiguous predecessor state, activate the current object,
-- and supersede the id-less marker. The call is accepted only for an old-enough processing deletion whose minimized
-- payload truly omitted the id. No source/diff/repository body is stored; repo/id/reason/timestamps only.
CREATE OR REPLACE FUNCTION core.resolve_legacy_repository_offboard_with_authority(
    p_repo text, p_current_repository_id text, p_reason text, p_delivery_key text)
    RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path TO 'core','pg_catalog' AS $$
DECLARE
  v_account text; v_repo text; v_current_id text; v_reason text; v_delivery_key text;
  v_delivery_received_at timestamptz; v_observed_at timestamptz;
  v_has_current_identity boolean := false; v_has_conflicting_identity boolean := false;
  v_reset jsonb := NULL; v_reset_needed boolean := false;
BEGIN
  v_repo := left(NULLIF(btrim(COALESCE(p_repo,'')),''),512);
  v_current_id := NULLIF(btrim(COALESCE(p_current_repository_id,'')),'');
  v_delivery_key := left(NULLIF(btrim(COALESCE(p_delivery_key,'')),''),200);
  v_reason := CASE WHEN p_reason='repository_deleted' THEN 'repository_deleted' ELSE 'installation_removed' END;
  IF v_repo IS NULL OR v_delivery_key IS NULL THEN
    RAISE EXCEPTION 'legacy offboard resolution needs repo and durable delivery authority' USING ERRCODE='42501';
  END IF;
  IF v_current_id IS NULL OR length(v_current_id)>32 OR v_current_id !~ '^[1-9][0-9]*$' THEN
    RAISE EXCEPTION 'legacy offboard resolution needs a canonical current repository id' USING ERRCODE='23514';
  END IF;
  SELECT account INTO v_account
    FROM core.establish_session_write_context() AS c(agent,account);

  SELECT d.received_at INTO v_delivery_received_at
    FROM core.webhook_delivery d
   WHERE d.delivery_key=v_delivery_key AND d.status='processing'
     AND d.account_key IN (
       v_account,
       CASE WHEN v_account LIKE 'ACCT-GH-%' THEN substr(v_account,9) ELSE v_account END)
     AND (
       (v_reason='repository_deleted'
        AND d.event_type='repository' AND d.payload->>'action'='deleted'
        AND d.repo=v_repo AND d.payload->'repository'->>'full_name'=v_repo
        AND NULLIF(btrim(d.payload->'repository'->>'id'),'') IS NULL)
       OR
       (v_reason='installation_removed'
        AND d.event_type='installation_repositories' AND d.payload->>'action'='removed'
        AND EXISTS (
          SELECT 1 FROM jsonb_array_elements(
            CASE WHEN jsonb_typeof(d.payload->'repositories_removed')='array'
                 THEN d.payload->'repositories_removed' ELSE '[]'::jsonb END
          ) AS removed(repo)
          WHERE removed.repo->>'full_name'=v_repo
            AND NULLIF(btrim(removed.repo->>'id'),'') IS NULL)
        AND NOT EXISTS (
          SELECT 1 FROM jsonb_array_elements(
            CASE WHEN jsonb_typeof(d.payload->'repositories_removed')='array'
                 THEN d.payload->'repositories_removed' ELSE '[]'::jsonb END
          ) AS removed(repo)
          WHERE removed.repo->>'full_name'=v_repo
            AND NULLIF(btrim(removed.repo->>'id'),'') IS NOT NULL)))
   LIMIT 1;
  IF v_delivery_received_at IS NULL THEN
    RAISE EXCEPTION 'legacy offboard delivery key is not authoritative for repository'
      USING ERRCODE='23514';
  END IF;
  IF v_delivery_received_at > clock_timestamp()-interval '5 minutes' THEN
    IF NOT core._defer_webhook_delivery_with_authority(
        v_delivery_key,v_delivery_received_at+interval '5 minutes',
        'legacy repository identity consistency window') THEN
      RAISE EXCEPTION 'legacy repository offboard could not schedule identity resolution'
        USING ERRCODE='55000';
    END IF;
    RETURN jsonb_build_object('ok',true,'repo',v_repo,'current_repository_id',v_current_id,
                              'preserved_current',false,'deferred',true,
                              'defer_reason','legacy_identity_consistency_window');
  END IF;

  SELECT EXISTS (
           SELECT 1 FROM core.graph_version
            WHERE account_id=v_account AND repo=v_repo AND repo_id=v_current_id
           UNION ALL
           SELECT 1 FROM core.repository_lifecycle_activation
            WHERE account_id=v_account AND repo=v_repo AND repository_id=v_current_id)
    INTO v_has_current_identity;
  SELECT EXISTS (
           SELECT 1 FROM core.graph_version
            WHERE account_id=v_account AND repo=v_repo
              AND (repo_id IS NULL OR repo_id<>v_current_id)
           UNION ALL
           SELECT 1 FROM core.repository_lifecycle_activation
            WHERE account_id=v_account AND repo=v_repo AND repository_id<>v_current_id)
    INTO v_has_conflicting_identity;
  v_reset_needed := NOT v_has_current_identity OR v_has_conflicting_identity;
  IF v_reset_needed THEN
    v_reset := core._purge_repo_with_authority(v_repo,false);
  END IF;

  v_observed_at := clock_timestamp();
  DELETE FROM core.repository_lifecycle_activation
   WHERE account_id=v_account AND repo=v_repo AND repository_id<>v_current_id;
  INSERT INTO core.repository_lifecycle_activation AS existing(
      account_id,repository_id,repo,activated_at,lifecycle_authoritative,generation_started_at)
  VALUES (v_account,v_current_id,v_repo,v_observed_at,true,v_observed_at)
  ON CONFLICT (account_id,repository_id)
  DO UPDATE SET repo=EXCLUDED.repo,
                activated_at=GREATEST(existing.activated_at,EXCLUDED.activated_at),
                lifecycle_authoritative=true,
                generation_started_at=CASE WHEN v_reset_needed THEN EXCLUDED.generation_started_at
                                           ELSE COALESCE(existing.generation_started_at,
                                                         EXCLUDED.generation_started_at) END;
  DELETE FROM core.repository_lifecycle_tombstone
   WHERE account_id=v_account AND repository_id=v_current_id;
  INSERT INTO core.repository_lifecycle_tombstone AS existing(
      account_id,repository_id,repo,reason,lifecycle_received_at,superseded_at)
  VALUES (v_account,'unknown',v_repo,v_reason,v_delivery_received_at,v_observed_at)
  ON CONFLICT (account_id,repository_id,repo)
  DO UPDATE SET reason=EXCLUDED.reason,tombstoned_at=now(),
                lifecycle_received_at=GREATEST(existing.lifecycle_received_at,EXCLUDED.lifecycle_received_at),
                superseded_at=EXCLUDED.superseded_at;
  UPDATE core.webhook_delivery
     SET payload=jsonb_set(
           payload,'{_veripsa_offboard_completed}',
           COALESCE(payload->'_veripsa_offboard_completed','{}'::jsonb)
             || jsonb_build_object(v_repo,v_current_id),true),
         updated_at=now()
   WHERE delivery_key=v_delivery_key AND status='processing';
  RETURN jsonb_build_object('ok',true,'repo',v_repo,'current_repository_id',v_current_id,
                            'preserved_current',true,'deferred',false,
                            'lifecycle_reset',v_reset_needed,'reset',v_reset);
END $$;
ALTER FUNCTION core.resolve_legacy_repository_offboard_with_authority(text,text,text,text)
  OWNER TO veripsa_migrator;
REVOKE EXECUTE ON FUNCTION core.resolve_legacy_repository_offboard_with_authority(text,text,text,text)
  FROM PUBLIC, veripsa_writer;
GRANT EXECUTE ON FUNCTION core.resolve_legacy_repository_offboard_with_authority(text,text,text,text)
  TO veripsa_app;

-- The rollback-compatible purge_repo_with_authority(text) shim is defined in module 25 together with the
-- deletion-delivery finish guard inside one explicit transaction. Keeping that rollout pair atomic prevents a
-- schema-apply interruption from exposing the old mutable-name purge under the new completion protocol.

-- Deprecated compatibility trap for mixed-version workers. Unordered offboarding is disabled for every role;
-- callers must supply authenticated durable delivery authority to the four-argument function.
CREATE OR REPLACE FUNCTION core.offboard_repository_with_authority(
    p_repo text, p_repository_id text, p_reason text)
    RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path TO 'core','pg_catalog' AS $$
BEGIN
  RAISE EXCEPTION 'unordered repository offboard is disabled; durable delivery authority is required'
    USING ERRCODE='42501';
END
$$;
ALTER FUNCTION core.offboard_repository_with_authority(text,text,text) OWNER TO veripsa_migrator;
REVOKE EXECUTE ON FUNCTION core.offboard_repository_with_authority(text,text,text)
  FROM PUBLIC, veripsa_writer, veripsa_app;

-- purge_account_working_set_with_authority: the UNINSTALL purge done ACCOUNT-WIDE (audit r4 — privacy
-- launch-blocker). The old uninstall path purged only the repos NAMED in the `installation.deleted` webhook
-- payload's `repositories` array — but GitHub omits that array for an "All repositories" install (the common
-- case), and a truncated/odd payload can omit it too, so a payload-driven purge could leave a whole tenant's
-- code graph + live claims sitting in our DB after they uninstalled = a broken "we immediately purge on
-- uninstall" privacy claim. This forgets the ENTIRE pinned tenant's content-free working set (code graph +
-- live claim/lane state) with NO repo filter — what GitHub named or not. The append-only `event` ledger
-- (push/landed audit, content-free) is RETAINED by design (the immutable, append-only moat) — the SAME
-- residual purge_repo keeps. NOT erase_account (which also drops the account/agent/event + is the GDPR
-- right-to-deletion): an uninstall forgets the code structure but keeps the content-free audit trail.
-- MIGRATION (audit #328 — data-rights): the working set has a SECOND, derived detector beside the code graph
-- — the CO-CHANGE (logical-coupling) cache core.co_change (per-account; stores file PATHS path_a/path_b = the
-- structure of the customer's PRIVATE repos) and its idempotency ledger core.co_change_seen_commit (per-account
-- commit shas). Both are real per-tenant customer data, so the uninstall purge MUST forget them too (over-retain
-- otherwise = privacy gap #328 found). Additive DELETE pair, exactly like the code_node/claim deletes above:
-- DELETE on co_change is NOT forgery-gated (trg_governed_co_change is INSERT/UPDATE-only, like purge_repo on the
-- code graph) so no token is needed; RLS (account pinned) walls each DELETE to v_account (tenant-scoped).
-- BILLING RESET ON UNINSTALL (audit P2 — stale paid override survives uninstall): purge deletes the content-free
-- WORKING SET but historically left core.account.plan untouched, so a paid label (e.g. 'pro') SURVIVED an
-- uninstall. Reinstalling the SAME installation id re-binds to the existing account (enter_installation_with_
-- authority) and the stale 'pro' is live again → the unlimited abuse-wall override (core._account_over_quota)
-- re-arms with NO active-subscription re-check (the marketplace 'cancelled' path resets plan→'free', but an
-- uninstall without a separate cancel does not). So this purge now ALSO resets plan→'free' (the WALLED default) so
-- the free-tier wall re-arms on reinstall. AUTHORIZED + content-free by the SAME discipline as the plan setter:
-- the account is already pinned (establish_session_write_context set core.current_account=v_account), so the
-- account-RLS WITH CHECK admits the UPDATE for THIS tenant ONLY; the account table is also forgery-gated
-- (trg_governed_account on INSERT/UPDATE), so we arm core.mark_governed_write('account') exactly as the setter
-- does. Content-free: it writes the constant label 'free' (no path/name/body). No new grant — this fn is already
-- veripsa_app-delegation-only. Idempotent (a re-delivered uninstall, or an already-free account, re-sets 'free').
CREATE OR REPLACE FUNCTION core.purge_account_working_set_with_authority(p_delete_proof jsonb)
    RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path TO 'core','pg_catalog' AS $$
DECLARE v_agent text; v_account text; v_gh_id text; v_nodes int; v_edges int; v_versions int; v_claims int;
        v_cochange int; v_cc_seen int; v_intents int; v_plan_reset int; v_webhooks int;
        v_webhook_deleted int; v_policy_refresh int; v_graph_refresh int:=0;
        v_ws_members int; v_workspaces int; v_repo_tombstones int; v_repo_activations int; v_revoked int;
        v_delivery_key text; v_delivery_received_at timestamptz; v_blocked_installation_id text;
        v_delivery_account_id text;
        v_delete_state text; v_proof_deleted_installation_id text; v_proof_account_id text;
        v_replacement_installation_id text; v_replacement_account_id text;
        v_replacement_created_at timestamptz; v_replacement_suspended boolean;
        v_current_installation_id text; v_current_installation_created_at timestamptz;
        v_last_received_at timestamptz; v_last_delivery_key text;
BEGIN
  SELECT agent, account INTO v_agent, v_account FROM core.establish_session_write_context() AS c(agent,account);
  IF v_account IS NULL OR v_account = '' THEN
    RAISE EXCEPTION 'account purge needs a resolved account' USING ERRCODE='23514';
  END IF;
  -- Boot convergence holds the identical session advisory key for its entire multi-statement pass.  Taking the
  -- transaction form before reading lifecycle order makes check/delete/tombstone one account-serialized action.
  PERFORM core._take_account_lifecycle_xact_lock(v_account);
  v_gh_id := CASE WHEN v_account LIKE 'ACCT-GH-%' THEN substr(v_account, 9) ELSE NULL END;
  -- Bind this account-wide forget to one exact processing uninstall.  There is intentionally no latest-row or
  -- maintenance fallback: a destructive account boundary without the worker's durable key is not authoritative.
  v_delivery_key := NULLIF(current_setting('core.current_delivery_key',true),'');
  IF v_delivery_key IS NULL THEN
    RAISE EXCEPTION 'account purge needs exact durable uninstall delivery key' USING ERRCODE='55000';
  END IF;
  SELECT d.received_at,
         NULLIF(left(COALESCE(d.payload->'installation'->>'id',''),64),''),
         NULLIF(left(COALESCE(d.payload->'installation'->'account'->>'id',''),64),'')
    INTO v_delivery_received_at,v_blocked_installation_id,v_delivery_account_id
    FROM core.webhook_delivery d
   WHERE d.delivery_key=v_delivery_key
     AND d.status='processing'
     AND d.event_type='installation'
     AND d.payload->>'action'='deleted'
     AND (d.account_key=v_account OR (v_gh_id IS NOT NULL AND d.account_key=v_gh_id));
  IF NOT FOUND THEN
    RAISE EXCEPTION 'account purge needs processing durable uninstall authority' USING ERRCODE='42501';
  END IF;
  IF v_blocked_installation_id IS NULL OR v_delivery_account_id IS NULL THEN
    RAISE EXCEPTION 'account purge durable uninstall omitted installation.id' USING ERRCODE='42501';
  END IF;

  -- The App-JWT proof has two deliberately disjoint states.  `absent` is the only authority to erase generation A;
  -- `replacement` proves A must be preserved because GitHub currently resolves this account to generation B.
  -- Both repeat the durable target/account so a proof for another delivery or tenant cannot be transplanted.
  IF jsonb_typeof(COALESCE(p_delete_proof,'null'::jsonb))<>'object' THEN
    RAISE EXCEPTION 'account purge needs authoritative deletion proof' USING ERRCODE='42501';
  END IF;
  v_delete_state := NULLIF(left(COALESCE(p_delete_proof->>'state',''),16),'');
  v_proof_deleted_installation_id := NULLIF(left(COALESCE(p_delete_proof->>'deleted_installation_id',''),64),'');
  v_proof_account_id := NULLIF(left(COALESCE(p_delete_proof->>'account_id',''),64),'');
  IF v_delete_state NOT IN ('absent','replacement')
     OR v_proof_deleted_installation_id IS DISTINCT FROM v_blocked_installation_id
     OR v_proof_account_id IS DISTINCT FROM v_delivery_account_id
     OR NOT (v_proof_account_id=v_account OR 'ACCT-GH-'||v_proof_account_id=v_account) THEN
    RAISE EXCEPTION 'account purge deletion proof does not match durable target' USING ERRCODE='42501';
  END IF;

  SELECT github_installation_id,github_installation_created_at
    INTO v_current_installation_id,v_current_installation_created_at
    FROM core.installation_account
   WHERE account_id=v_account
   ORDER BY github_installation_created_at DESC NULLS LAST,installation_id
   LIMIT 1;

  IF v_delete_state='replacement' THEN
    IF jsonb_typeof(COALESCE(p_delete_proof->'current','null'::jsonb))<>'object' THEN
      RAISE EXCEPTION 'replacement purge proof needs current installation' USING ERRCODE='42501';
    END IF;
    v_replacement_installation_id := NULLIF(left(COALESCE(p_delete_proof->'current'->>'installation_id',''),64),'');
    v_replacement_account_id := NULLIF(left(COALESCE(p_delete_proof->'current'->>'account_id',''),64),'');
    BEGIN
      v_replacement_created_at := NULLIF(p_delete_proof->'current'->>'created_at','')::timestamptz;
    EXCEPTION WHEN invalid_datetime_format OR datetime_field_overflow THEN
      v_replacement_created_at := NULL;
    END;
    v_replacement_suspended := COALESCE(p_delete_proof->'current'->>'suspended','false')='true';
    IF v_replacement_installation_id IS NULL OR v_replacement_created_at IS NULL
       OR jsonb_typeof(p_delete_proof->'current'->'suspended') IS DISTINCT FROM 'boolean'
       OR v_replacement_installation_id=v_blocked_installation_id
       OR v_replacement_account_id IS DISTINCT FROM v_delivery_account_id THEN
      RAISE EXCEPTION 'replacement purge proof does not identify a different current generation'
        USING ERRCODE='42501';
    END IF;
    -- Do not downgrade a generation already known newer than this proof.  Otherwise persist the exact current
    -- replacement before returning the delete as a stale no-op; a crash cannot leave the route pointing back at A.
    IF v_current_installation_created_at IS NULL
       OR v_replacement_installation_id=v_current_installation_id
       OR v_replacement_created_at>v_current_installation_created_at THEN
      UPDATE core.installation_account
         SET github_installation_id=v_replacement_installation_id,
             github_installation_created_at=CASE
               WHEN github_installation_id=v_replacement_installation_id
                 THEN GREATEST(github_installation_created_at,v_replacement_created_at)
               ELSE v_replacement_created_at
             END,
             revoked_at=CASE WHEN v_replacement_suspended THEN COALESCE(revoked_at,now()) ELSE revoked_at END
       WHERE account_id=v_account;
    END IF;
    RETURN jsonb_build_object('ok',true,'account_wide',true,'stale_ignored',true,
                              'reason','replacement installation generation is current','purged','{}'::jsonb);
  END IF;

  -- Authoritative absence may erase only the generation named by this delete.  If another generation has already
  -- been recorded, the external absence observation was overtaken and the safe result is a stale no-op.
  IF v_current_installation_id IS NOT NULL
     AND v_current_installation_id<>v_blocked_installation_id THEN
    RETURN jsonb_build_object('ok',true,'account_wide',true,'stale_ignored',true,
                              'reason','replacement installation generation is current','purged','{}'::jsonb);
  END IF;
  -- Preserve the high-water mark after reactivation.  A recovered uninstall older than a completed reinstall is
  -- an idempotent stale no-op; it must not delete the new lifecycle generation before discovering its age.
  SELECT last_event_received_at,last_delivery_key
    INTO v_last_received_at,v_last_delivery_key
    FROM core.account_lifecycle_tombstone WHERE account_id=v_account;
  IF FOUND AND (v_delivery_received_at,v_delivery_key)<=(v_last_received_at,v_last_delivery_key) THEN
    RETURN jsonb_build_object('ok',true,'account_wide',true,'stale_ignored',true,'purged','{}'::jsonb);
  END IF;
  -- SERIALIZE-WITH-WRITERS FIRST (root fix, small-findings sweep — the concurrent-writer-during-purge residue). A
  -- background co-change writer takes FOR SHARE on this account row inside its gated write (see 85_cochange.sql), so
  -- LOCK THE ACCOUNT ROW FOR UPDATE *before* any working-set DELETE below. This makes the purge serialize against an
  -- in-flight writer AT THE TOP: a writer that has already committed is now fully VISIBLE to the DELETEs that follow
  -- (so its rows are reaped, not stranded by the old DELETE-then-lock ordering where the purge's co_change DELETE ran
  -- before it ever blocked on the writer), and a writer still in flight BLOCKS here until the purge commits the
  -- tombstone, then sees it and refuses (42501). Either ordering leaves NO post-purge residue. RLS pins v_account, so
  -- this locks only THIS tenant's row; the row is RETAINED by this working-set purge (only plan is reset below), so
  -- the lock is held to commit. (A concurrent erase that hard-deletes the row would conflict on the SAME row lock.)
  PERFORM 1 FROM core.account WHERE account_id=v_account FOR UPDATE;
  DELETE FROM core.code_node     WHERE account_id=v_account;  GET DIAGNOSTICS v_nodes    = ROW_COUNT;
  DELETE FROM core.code_edge     WHERE account_id=v_account;  GET DIAGNOSTICS v_edges    = ROW_COUNT;
  DELETE FROM core.graph_version WHERE account_id=v_account;  GET DIAGNOSTICS v_versions = ROW_COUNT;
  DELETE FROM core.claim         WHERE account_id=v_account;  GET DIAGNOSTICS v_claims   = ROW_COUNT;
  DELETE FROM core.co_change             WHERE account_id=v_account;  GET DIAGNOSTICS v_cochange = ROW_COUNT;
  DELETE FROM core.co_change_seen_commit WHERE account_id=v_account;  GET DIAGNOSTICS v_cc_seen  = ROW_COUNT;
  -- INTENT (per-agent declared work: summary text + scope_in/out path arrays) is per-tenant data too (audit P1):
  -- the GDPR erase_account path already forgets it, so the UNINSTALL working-set purge must as well — otherwise a
  -- tenant's intent summaries + scoped paths outlive their uninstall. RLS walls the DELETE to v_account.
  DELETE FROM core.intent WHERE account_id=v_account;  GET DIAGNOSTICS v_intents = ROW_COUNT;
  -- CROSS-REPO CONSENT graph (75_workspace.sql, Phase 4c) — the per-account consent rows are real per-tenant data
  -- (workspace_member names the repos this account opted into a workspace; workspace names a space it initiated), so
  -- the uninstall working-set purge MUST forget them too (privacy parity with the erase path above; the enumerate-all
  -- guard FAILS CI on any account-scoped table — workspace_member carries account_id — the purge forgets). NOT
  -- forgery-gated on DELETE (the governed-write triggers are INSERT/UPDATE-only, like the code-graph/co_change
  -- deletes) so no token is needed; RLS (account pinned by establish_session_write_context) walls each DELETE to
  -- v_account via tenant_isolation (account_id / created_by_account). Members first, then this account's workspaces.
  DELETE FROM core.workspace_member WHERE account_id=v_account;        GET DIAGNOSTICS v_ws_members = ROW_COUNT;
  DELETE FROM core.workspace        WHERE created_by_account=v_account; GET DIAGNOSTICS v_workspaces = ROW_COUNT;
  -- G4 POLICY-CHANGE REFRESH OUTBOX (97_policy_refresh.sql): transient per-account refresh-request working set. On
  -- uninstall it is moot (there are no more open PRs to refresh, and the install is being revoked), so forget it
  -- like the rest of the working set — NOT on the retain allowlist (it is not config that must survive a reinstall).
  -- Lock the content-free scheduler route before either lease or outbox. The 97 module's live terminals use the
  -- same route -> lease -> outbox order. Dynamic SQL keeps this 35 module bootstrap-safe: the lease table is
  -- introduced later by 97, but is always present when the completed schema serves an uninstall.
  PERFORM 1 FROM core.installation_account WHERE account_id=v_account FOR UPDATE;
  IF to_regclass('core.graph_convergence_lease') IS NOT NULL THEN
    EXECUTE 'DELETE FROM core.graph_convergence_lease WHERE account_id=$1' USING v_account;
    GET DIAGNOSTICS v_graph_refresh = ROW_COUNT;
  END IF;
  -- FORCE-RLS'd → the account pin walls this DELETE to v_account; DELETE is not forgery-gated (INSERT/UPDATE-only).
  DELETE FROM core.policy_refresh_outbox WHERE account_id=v_account;   GET DIAGNOSTICS v_policy_refresh = ROW_COUNT;
  IF to_regclass('core.graph_convergence_lease') IS NOT NULL
     AND EXISTS (
       SELECT 1
         FROM pg_attribute
        WHERE attrelid='core.installation_account'::regclass
          AND attname='convergence_quota_deferred_count'
          AND attnum>0 AND NOT attisdropped
     ) THEN
    -- The account route survives uninstall for reinstall. Repair it even if the outbox was already empty and the
    -- lease above was an orphan, so stale slot/counter state cannot strand the next lifecycle generation.
    EXECUTE $sql$
      UPDATE core.installation_account
         SET policy_refresh_due_at=NULL,graph_refresh_due_at=NULL,
             convergence_claimed_until=NULL,convergence_claimed_by=NULL,
             convergence_claim_epoch=NULL,convergence_graph_claim_count=0,
             convergence_graph_reclaim_at=NULL,convergence_pending_count=0,
             convergence_retry_exhausted_count=0,convergence_quota_deferred_count=0
       WHERE account_id=$1
    $sql$ USING v_account;
  END IF;
  -- Account-wide uninstall supersedes every repo-scoped marker. Keeping them would retain repo names/ids for no
  -- purpose and could make a later full reinstall inherit stale per-repo revocations.
  DELETE FROM core.repository_lifecycle_tombstone WHERE account_id=v_account;
  GET DIAGNOSTICS v_repo_tombstones = ROW_COUNT;
  DELETE FROM core.repository_lifecycle_activation WHERE account_id=v_account;
  GET DIAGNOSTICS v_repo_activations = ROW_COUNT;
  -- DURABLE WEBHOOK INBOX (audit P1 — uninstall-purge gap, the privacy parity of the erase fix above). On
  -- uninstall we forget the content-free working set; core.webhook_delivery holds the tenant's repo full_names +
  -- GitHub account id on every row (kept even on DONE rows after payload→'{}'), so it must be forgotten here too,
  -- not left to grow forever on the 256 MiB tier (it is otherwise pruned only by age in the retention sweep). SAME
  -- account_key KEY-FORMAT footgun as the erase: account_key is the BARE GH owner id ('7777'), NOT 'ACCT-GH-7777'
  -- = v_account — so match the 'ACCT-GH-'-stripped form (substr(v_account,9), the strip 30_gate.sql's plan setter
  -- uses) as well as v_account, stripping ONLY when the prefix is actually present. No per-account RLS on this
  -- table + DELETE not forgery-gated → the account_key match IS the tenant scope (never another tenant's id).
  -- IN-FLIGHT-ROW EXCLUSION (root fix, small-findings sweep): an uninstall is ITSELF a webhook delivery, and on
  -- the durable-inbox path the `installation.deleted` row driving THIS purge is currently status='processing' (the
  -- worker claimed it, is running the handler that called us, and will finish() it AFTER we return). The unfiltered
  -- DELETE above would delete that very in-flight row mid-run; the subsequent finish() then no-ops on the owned-row
  -- guard (benign — that is why this was 实测-latent), but it is a self-inflicted delete of a live operational row,
  -- and the worker's BEST-EFFORT finalize log would then spuriously report the row "concurrently reclaimed/finalised".
  -- So EXCLUDE the processing row(s): the purge forgets the SETTLED durable rows (queued/done/failed — the tenant's
  -- repo full_names we must not retain) but leaves the in-flight row for the worker's own finish() to clear. NOT a
  -- retention gap: the row carries this tenant's own id (cross-tenant-safe), its payload is already minimized, and
  -- finish() clears it to 'done' moments later; a redelivered uninstall, the next purge, or the age-based retention
  -- sweep (prune_all_accounts) reaps it if the worker died mid-flight. done/failed/queued rows at or before THIS
  -- uninstall's durable (received_at,delivery_key) boundary are cleared. Rows ordered after it belong to a later
  -- lifecycle generation (notably a queued reinstall) and must survive so ordered recovery can reactivate the account.
  -- Preserve a content-free idempotency receipt for settled lifecycle deliveries.  Deleting the delivery key lets
  -- a delayed duplicate acquire a fresh received_at and masquerade as a post-uninstall reinstall.  The opaque key
  -- and original ordering coordinate remain; every tenant/repository/payload field is scrubbed.
  UPDATE core.webhook_delivery
     SET event_type='erased',account_key=NULL,repo=NULL,payload='{}'::jsonb,status='done',attempts=0,
         last_error=NULL,locked_at=NULL,not_before=NULL,done_at=COALESCE(done_at,now()),updated_at=now()
   WHERE (account_key = v_account OR (v_gh_id IS NOT NULL AND account_key = v_gh_id))
     AND delivery_key <> v_delivery_key
     AND event_type IN ('installation','installation_repositories')
     -- Receive order is not installation-generation order. Preserve a queued/processing replacement B even when
     -- it arrived before delayed delete A; its App-JWT proof will decide whether it is still current.
     AND (v_blocked_installation_id IS NULL
          OR NULLIF(left(COALESCE(payload->'installation'->>'id',''),64),'') IS NULL
          OR NULLIF(left(COALESCE(payload->'installation'->>'id',''),64),'')=v_blocked_installation_id)
     AND (v_delivery_received_at IS NULL
          OR (received_at,delivery_key)<=(v_delivery_received_at,v_delivery_key));
  GET DIAGNOSTICS v_webhooks = ROW_COUNT;
  DELETE FROM core.webhook_delivery
   WHERE (account_key = v_account OR (v_gh_id IS NOT NULL AND account_key = v_gh_id))
     AND status <> 'processing'
     AND (status='done'
          OR (status='failed' AND causal_order_version=0)
          OR v_blocked_installation_id IS NULL
          OR NULLIF(left(COALESCE(payload->'installation'->>'id',''),64),'') IS NULL
          OR NULLIF(left(COALESCE(payload->'installation'->>'id',''),64),'')=v_blocked_installation_id)
     AND (v_delivery_received_at IS NULL
          OR (received_at,delivery_key)<=(v_delivery_received_at,v_delivery_key));
  GET DIAGNOSTICS v_webhook_deleted = ROW_COUNT;
  v_webhooks := v_webhooks + v_webhook_deleted;
  -- BILLING RESET (audit P2): downgrade plan→'free' so a reinstall cannot resurrect a stale paid override. The
  -- account row is RETAINED (this is the content-free working-set purge, NOT erase_account) — only the plan column
  -- is reset. Governed-write token armed (trg_governed_account gates UPDATE) + RLS-walled to the pinned v_account.
  PERFORM core.mark_governed_write('account');
  UPDATE core.account SET plan='free' WHERE account_id=v_account;  GET DIAGNOSTICS v_plan_reset = ROW_COUNT;
  -- PLAN-EVENT HIGH-WATER MARK (audit iter-5 P2): forget the per-account last-applied marketplace plan-event time so
  -- a reinstall + a fresh purchase is NOT refused by a STALE high-water mark from the prior lifecycle (the ordering
  -- guard would otherwise see the new purchase as "older than" a mark left over from before the uninstall). FK-free
  -- cross-tenant table (no per-account RLS, no governed-write token) — the account_id PK match IS the tenant scope.
  DELETE FROM core.account_plan_event WHERE account_id=v_account;
  -- RESURRECTION TOMBSTONE (audit iter-4 P1): record that this account was uninstall-purged so a BACKGROUND
  -- per-repo writer (co-change/self-heal/boot-reconcile) racing this account-wide purge — which holds NO advisory
  -- lock, so its per-repo lock can NOT serialize them — is REFUSED by core.assert_account_live_with_authority()
  -- and cannot silently re-populate the working set we just forgot. The purge KEEPS the account row (RLS alone
  -- would therefore NOT block such a re-write), so the tombstone is what closes the resurrection vector here. A
  -- genuine reinstall clears it (core.reactivate_account_with_authority in the onboarding handler). Direct INSERT
  -- on the connection-resolved v_account (no re-resolution mid-purge); cross-tenant-safe (this account's own id).
  INSERT INTO core.account_lifecycle_tombstone(
      account_id, reason, active, last_event_received_at, last_delivery_key, blocked_installation_id)
  VALUES (v_account, 'uninstall_purge', true, v_delivery_received_at, v_delivery_key,
          v_blocked_installation_id)
  ON CONFLICT (account_id) DO UPDATE
    SET reason='uninstall_purge',tombstoned_at=now(),active=true,
        last_event_received_at=EXCLUDED.last_event_received_at,
        last_delivery_key=EXCLUDED.last_delivery_key,
        blocked_installation_id=EXCLUDED.blocked_installation_id;
  -- INSTALLATION LIVENESS (commercial-completeness — the no-billing-without-a-LIVE-link invariant). The uninstall
  -- KEEPS the installation→account ROW (the audit ledger is retained + a reinstall re-binds the SAME id), so a bare
  -- SELECT on core.installation_account (list_installation_ids) would still report this UNINSTALLED installation as
  -- live — and the platform's liveness gates + graph freshness read exactly that seam. STAMP revoked_at=now() on
  -- this account's mapping row(s) so the install drops out of installation_is_live / the live-only enumerator until
  -- a genuine reinstall (reactivate_account_with_authority) clears it. No per-account RLS on this routing table +
  -- the row carries this tenant's own id ⇒ the account_id match IS the scope (never another tenant's row). Stamp
  -- only the still-live rows (revoked_at IS NULL) so a re-delivered uninstall does not churn the timestamp.
  UPDATE core.installation_account SET revoked_at=now()
   WHERE account_id=v_account AND revoked_at IS NULL
     AND (github_installation_id IS NULL OR github_installation_id=v_blocked_installation_id);
  GET DIAGNOSTICS v_revoked = ROW_COUNT;
  RETURN jsonb_build_object('ok',true,'account_wide',true,'purged',
    jsonb_build_object('nodes',v_nodes,'edges',v_edges,'versions',v_versions,'claims',v_claims,
                       'cochange',v_cochange,'cochange_seen',v_cc_seen,'intents',v_intents,
                       'workspace_members',v_ws_members,'workspaces',v_workspaces,
                       'repository_tombstones',v_repo_tombstones,
                       'repository_activations',v_repo_activations,
                       'policy_refresh_outbox',v_policy_refresh,
                       'graph_convergence_leases',v_graph_refresh,
                       'webhook_deliveries',v_webhooks,'plan_reset',v_plan_reset,
                       'installations_revoked',v_revoked));
END $$;
ALTER FUNCTION core.purge_account_working_set_with_authority(jsonb) OWNER TO veripsa_migrator;

-- Schema-first rolling boundary.  An old worker cannot distinguish a GitHub-authoritative 404 from a replacement
-- installation, so allowing it to call the historical proof-less surface would re-open the destructive ambiguity.
CREATE OR REPLACE FUNCTION core.purge_account_working_set_with_authority()
    RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path TO 'core','pg_catalog' AS $$
BEGIN
  RAISE EXCEPTION 'legacy account purge refused: authoritative generation proof required'
    USING ERRCODE='55000';
END $$;
ALTER FUNCTION core.purge_account_working_set_with_authority() OWNER TO veripsa_migrator;

-- erase_account_with_authority: RIGHT-TO-DELETION (GDPR Art.17 / CCPA erasure) — the FULL, account-scoped HARD
-- DELETE of a tenant's data, the thing purge_repo_with_authority is NOT. purge_repo forgets only the content-free
-- WORKING SET (graph + live claims) for one repo and deliberately RETAINS the append-only event ledger, the
-- statement records, and the account/agent identity rows (incl. 'GH-<login>' author agents = PERSONAL DATA). When
-- a customer says "delete ALL my data / we are offboarding", that residual is not enough. This is the controlled
-- erase that reconciles "immutable audit trail" with "erase this tenant":
--   * It is ACCOUNT-SCOPED + un-forgeable. Identity comes from the CONNECTION ROLE (establish_session_write_context),
--     NEVER from a caller argument — a tenant can only erase ITS OWN account, never a victim's. RLS pins the
--     account; the erasure token (mark_account_erasure = THIS account) lets the append-only triggers permit the
--     DELETE for THIS account's rows ONLY. Every OTHER tenant's ledger stays append-only/immutable DURING the
--     erase — the immutability triggers are NEVER dropped (this is crypto-erase-by-scope, not a trigger suspension).
--   * It is COMPLETE. It deletes the tenant's footprint across EVERY per-account table, in FK-safe order
--     (children before the account/agent they reference), and clears the cross-account routing rows
--     (installation_account, credential), BOTH sides of the social follow graph (so no retained tenant is left
--     holding a 'followed_account = <erased>' row that dangles a reference to the erased account), and BOTH
--     directions of the delegation graph — incl. the INBOUND grants another tenant issued whose grantee is this
--     account's agent (grantee_agent REFERENCES core.agent RESTRICT, so leaving them would make the agent DELETE
--     raise foreign_key_violation and ABORT the whole erase → the tenant could never be erased). Finally the
--     account row itself goes (a real hard delete, not just account_state='closed').
--   * It RETURNS a per-table count manifest = auditable EVIDENCE of exactly what was erased (a deletion receipt
--     the operator/PO can show a regulator). Append-only-safe by construction: the named-token DELETE on the
--     immutable streams is the SAME gate/token discipline as retention; a plain DELETE is still refused.
-- App-delegation only (veripsa_app) — the host runs this on an uninstall/offboarding request, never a buyer seat.
-- NOTE (PO policy, not code): whether uninstall AUTO-triggers this hard delete vs retaining the content-free,
-- public-git-equivalent ledger is a PO decision (see RUNBOOK.md §Offboarding). This fn is the MECHANISM that makes
-- a hard "forget everything" honorable WITHOUT dropping the append-only trigger or breaking other tenants.
-- MIGRATION (audit #328 — data-rights): the erase walked the per-account tables but MISSED the CO-CHANGE working
-- set — core.co_change (per-account; file PATHS path_a/path_b = the structure of the customer's PRIVATE repos)
-- and its idempotency ledger core.co_change_seen_commit (per-account commit shas). So a GDPR Art.17 / CCPA "delete
-- ALL my data" left those file paths behind and the deletion receipt was INCOMPLETE. Added to the LIVE/MUTABLE
-- block below (next to claim/code_node): DELETE on co_change is NOT forgery-gated (trg_governed_co_change is
-- INSERT/UPDATE-only, like purge_repo on the code graph) so no token is needed; RLS (account pinned above) walls
-- each DELETE to v_account (tenant-scoped, never another tenant). The counts join the deletion-receipt manifest.
-- MIGRATION (audit P1 — durable-inbox erase gap): the DURABLE WEBHOOK INBOX core.webhook_delivery is a NEW
-- account_key-bearing table the erase originally missed (it carries the tenant's GitHub account id + repo
-- full_names on every row, retained even after the payload is cleared). It is now deleted in the LIVE/MUTABLE
-- block too — see the in-body note for the account_key KEY-FORMAT footgun (bare GH id vs 'ACCT-GH-'||id). The
-- erase-completeness gate now ENUMERATES every account_key-bearing table so the NEXT such table can't silently
-- reopen this gap (a new per-tenant table that the erase forgets is a hard gate FAIL until it is wired here).
CREATE OR REPLACE FUNCTION core.erase_account_with_authority()
    RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path TO 'core','pg_catalog' AS $$
DECLARE v_agent text; v_account text; v_gh_id text;
        v_events int; v_statements int; v_claims int; v_nodes int; v_edges int; v_versions int;
        v_intents int; v_grants int; v_grants_in int; v_policies int; v_stores int; v_follows int;
        v_creds int; v_insts int; v_agents int; v_accounts int;
        v_cochange int; v_cc_seen int; v_webhooks int; v_webhook_deleted int; v_plan_events int;
        v_ws_members int; v_workspaces int; v_repo_tombstones int; v_repo_activations int;
        v_account_tombstones int; v_policy_refresh int; v_graph_refresh int:=0;
BEGIN
  -- identity from the connection role — the account to erase is NEVER a caller argument (no cross-tenant erase).
  SELECT agent, account INTO v_agent, v_account FROM core.establish_session_write_context() AS c(agent,account);
  IF v_account IS NULL OR v_account = '' THEN RAISE EXCEPTION 'erase needs a resolved account' USING ERRCODE='23514'; END IF;
  PERFORM core._take_account_lifecycle_xact_lock(v_account);
  v_gh_id := CASE WHEN v_account LIKE 'ACCT-GH-%' THEN substr(v_account, 9) ELSE NULL END;
  -- Serialize the privacy boundary with durable inbox admission. Enqueue assigns received_at only after taking this
  -- exact account lock; therefore work accepted while erase is in flight waits, receives a post-erase timestamp,
  -- and survives as the next lifecycle generation instead of being inserted and then invisibly deleted below.
  -- Lock both historical key forms in a fixed order so bare and full account-key writers are covered.
  IF v_gh_id IS NOT NULL THEN
    PERFORM pg_advisory_xact_lock(
      hashtext('core.webhook_delivery.account'),hashtext(v_gh_id));
  END IF;
  PERFORM pg_advisory_xact_lock(
    hashtext('core.webhook_delivery.account'),hashtext(v_account));
  -- SERIALIZE-WITH-WRITERS FIRST (root fix, small-findings sweep — parity with the purge): lock the account row FOR
  -- UPDATE before any working-set DELETE, so a concurrent background co-change writer (which takes FOR SHARE on this
  -- row in its gated write) is serialized AT THE TOP — its committed rows are visible to the deletes that follow (so
  -- nothing strands via the old delete-then-lock ordering), and an in-flight writer blocks until this erase commits,
  -- then sees the tombstone (recorded just below) and refuses. The account row is hard-deleted later in this body, so
  -- the lock is held to commit. RLS pins v_account → only this tenant's row is locked.
  PERFORM 1 FROM core.account WHERE account_id=v_account FOR UPDATE;
  -- arm the account-scoped erasure token: the append-only triggers (assert_append_only / assert_statement_immutable)
  -- will permit a DELETE ONLY for rows whose account_id = v_account. Other tenants stay immutable throughout.
  PERFORM core.mark_account_erasure(v_account);

  -- Hard erasure intentionally leaves no account-keyed tombstone. Ordinary webhooks and background work use the
  -- non-provisioning enter_existing_installation_with_authority route; only a fresh App-JWT-proven activation may
  -- create a tenant. That boundary blocks resurrection without retaining the erased GitHub identifiers.

  -- IMMUTABLE STREAMS (token-gated DELETE). event + statement: the records-not-correctness ledgers.
  DELETE FROM core.event     WHERE account_id=v_account;  GET DIAGNOSTICS v_events     = ROW_COUNT;
  DELETE FROM core.statement WHERE account_id=v_account;  GET DIAGNOSTICS v_statements = ROW_COUNT;

  -- LIVE / MUTABLE per-account state (DELETE is not forgery-gated on these — INSERT/UPDATE-only triggers,
  -- exactly as purge_repo deletes graph/claims). RLS (account pinned above) walls each DELETE to v_account.
  DELETE FROM core.claim         WHERE account_id=v_account;  GET DIAGNOSTICS v_claims   = ROW_COUNT;
  DELETE FROM core.code_node     WHERE account_id=v_account;  GET DIAGNOSTICS v_nodes    = ROW_COUNT;
  DELETE FROM core.code_edge     WHERE account_id=v_account;  GET DIAGNOSTICS v_edges    = ROW_COUNT;
  DELETE FROM core.graph_version WHERE account_id=v_account;  GET DIAGNOSTICS v_versions = ROW_COUNT;
  -- CO-CHANGE working set (audit #328): the second derived detector's per-tenant file PATHS + idempotency shas.
  DELETE FROM core.co_change             WHERE account_id=v_account;  GET DIAGNOSTICS v_cochange = ROW_COUNT;
  DELETE FROM core.co_change_seen_commit WHERE account_id=v_account;  GET DIAGNOSTICS v_cc_seen  = ROW_COUNT;
  -- DURABLE WEBHOOK INBOX (audit P1 — GDPR-erase gap). core.webhook_delivery carries account_key + repo
  -- (the private-repo full_name) on EVERY row, and a DONE row keeps both even after the payload is cleared to
  -- '{}' (the operational metadata survives by design). So a tenant's repo full_names + GitHub account id sat in
  -- this table forever — the GDPR Art.17 / CCPA "delete ALL my data" erase walked 17 per-account tables but
  -- never this one, so the receipt was INCOMPLETE and the rows OUTLIVED the erase. KEY-FORMAT FOOTGUN (the load-
  -- bearing detail): webhook_delivery.account_key stores the BARE GitHub owner id STRING (server's
  -- _event_account_key returns repository.owner.id verbatim, e.g. '7777'), NOT the 'ACCT-GH-'||<id> form that
  -- v_account / core.account.account_id use (e.g. 'ACCT-GH-7777'). A naive `account_key=v_account` would match
  -- ZERO rows (silent-false erase). So we match the PREFIX-STRIPPED form too — substr(v_account,9) drops the
  -- literal 'ACCT-GH-' (8 chars), the EXACT strip 30_gate.sql's plan setter uses (v_gh_id := substr(v_account,9)).
  -- We match BOTH v_account (a future writer that stores the full id, or a non-GH test account like 'ACCT-DEMO')
  -- AND the stripped id, but strip ONLY when the 'ACCT-GH-' prefix is actually present (else 'ACCT-DEMO' would
  -- mis-strip to 'O'). This table has NO per-account RLS (it is cross-tenant, written only by the App identity)
  -- and DELETE is not forgery-gated, so the account_key match IS the tenant scope here — never another tenant's
  -- rows (their account_key is a different id). account_key can also be NULL (best-effort at persist time); such
  -- orphan rows are unattributable to a tenant and are reaped by AGE in the retention sweep (prune_all_accounts).
  -- A GitHub delivery id is an opaque idempotency boundary, not tenant content.  Retain lifecycle ids as scrubbed
  -- receipts so an old duplicate cannot be re-admitted as a newer activation after this hard erase.  All customer
  -- coordinates and payload data are removed before the remaining non-lifecycle rows are deleted.
  UPDATE core.webhook_delivery
     SET event_type='erased',account_key=NULL,repo=NULL,payload='{}'::jsonb,status='done',attempts=0,
         last_error=NULL,locked_at=NULL,not_before=NULL,done_at=COALESCE(done_at,now()),updated_at=now()
   WHERE (account_key = v_account OR (v_gh_id IS NOT NULL AND account_key = v_gh_id))
     AND event_type IN ('installation','installation_repositories');
  GET DIAGNOSTICS v_webhooks = ROW_COUNT;
  DELETE FROM core.webhook_delivery
   WHERE account_key = v_account OR (v_gh_id IS NOT NULL AND account_key = v_gh_id);
  GET DIAGNOSTICS v_webhook_deleted = ROW_COUNT;
  v_webhooks := v_webhooks + v_webhook_deleted;
  -- PLAN-EVENT HIGH-WATER MARK (audit iter-5 P2): the per-account last-applied marketplace plan-event time. It is
  -- account_id-keyed per-account state, so the GDPR Art.17 / CCPA hard delete must take it too (the deletion receipt
  -- must be COMPLETE — the enumerate-all guard below FAILS CI on any account-scoped table the erase forgets). FK-free
  -- cross-tenant table (no per-account RLS) — the account_id match IS the tenant scope. Counted in the manifest.
  DELETE FROM core.account_plan_event WHERE account_id=v_account;  GET DIAGNOSTICS v_plan_events = ROW_COUNT;
  DELETE FROM core.intent        WHERE account_id=v_account;  GET DIAGNOSTICS v_intents  = ROW_COUNT;
  -- DELEGATION graph — BOTH directions. OUTBOUND (grantor_account=v_account): the grants this tenant ISSUED,
  -- admitted by tenant_isolation. INBOUND: grants OTHER tenants issued whose grantee is THIS account's agent —
  -- grantee_agent REFERENCES core.agent RESTRICT, so leaving these would make the agent DELETE below raise
  -- foreign_key_violation and ABORT the whole erase (the tenant could never be erased — GDPR/CCPA failure).
  -- The inbound rows live in the OTHER tenant (grantor_account<>v_account) so tenant_isolation hides them; the
  -- grant_erasable permissive policy (10_substrate.sql) ADDS exactly the rows whose grantee agent belongs to the
  -- armed erasure-token account — the SAME un-forgeable token/policy mechanism the both-sides follow erase uses.
  -- The WHERE here matches that policy's predicate exactly (this account's own grantee agents, still present —
  -- the agent rows are not deleted until below), so RLS scopes it to precisely those rows and nothing else.
  DELETE FROM core.grant         WHERE grantor_account=v_account;  GET DIAGNOSTICS v_grants = ROW_COUNT;
  DELETE FROM core.grant
    WHERE grantee_agent IN (SELECT agent_id FROM core.agent WHERE account_id = v_account);
  GET DIAGNOSTICS v_grants_in = ROW_COUNT;
  DELETE FROM core.policy        WHERE account_id=v_account;  GET DIAGNOSTICS v_policies = ROW_COUNT;
  DELETE FROM core.store_connection WHERE account_id=v_account;  GET DIAGNOSTICS v_stores = ROW_COUNT;
  -- G4 POLICY-CHANGE REFRESH OUTBOX (97_policy_refresh.sql): account-scoped operational refresh-request state, so
  -- the GDPR Art.17 / CCPA hard delete must take it too (the enumerate-all guard FAILS CI on any account-scoped
  -- table the erase forgets). FORCE-RLS'd → the account pin above walls this DELETE to v_account; DELETE is not
  -- forgery-gated (the governed-write trigger is INSERT/UPDATE-only). Content-free (account id + epoch + status).
  -- The v2 lease contains private repo/branch/SHA coordinates and can exist without an outbox row after damage;
  -- delete it explicitly under the route lock. Dynamic SQL preserves the 35-before-97 bootstrap order.
  PERFORM 1 FROM core.installation_account WHERE account_id=v_account FOR UPDATE;
  IF to_regclass('core.graph_convergence_lease') IS NOT NULL THEN
    EXECUTE 'DELETE FROM core.graph_convergence_lease WHERE account_id=$1' USING v_account;
    GET DIAGNOSTICS v_graph_refresh = ROW_COUNT;
  END IF;
  DELETE FROM core.policy_refresh_outbox WHERE account_id=v_account;  GET DIAGNOSTICS v_policy_refresh = ROW_COUNT;

  -- SOCIAL follow graph — BOTH sides, scoped by RLS, NOT by a WHERE. tenant_isolation only matches
  -- follower_account=current_account (this account's OUTBOUND follows); the follow_erasable permissive policy
  -- (70_social.sql) ADDS the rows where the armed erasure token names EITHER side (so the INBOUND rows other
  -- tenants hold ON this account also go — no dangling 'followed_account=<erased>' reference is left behind). The
  -- DELETE is intentionally UNqualified: the two OR'd RLS policies are the security barrier that scopes it to
  -- EXACTLY the edges naming v_account, and NOTHING else (proven: an unrelated other-tenant edge is untouched).
  -- A user WHERE on a policy-protected column would narrow which rows the OR'd policy admits (a Postgres RLS
  -- quirk), so we let RLS alone do the scoping — current_account + the account-pinned token already bound it.
  DELETE FROM core.follow;
  GET DIAGNOSTICS v_follows = ROW_COUNT;

  -- CROSS-REPO CONSENT graph (75_workspace.sql, Phase 4c) — this account's consent rows are per-account data
  -- (workspace_member.account_id / workspace.created_by_account), so the GDPR Art.17 / CCPA hard delete must take
  -- them too (the enumerate-all guard FAILS CI on any account-scoped table the erase forgets — workspace_member
  -- carries account_id). Each row is scoped to THIS account by its own column (account_id / created_by_account),
  -- which both tenant_isolation AND the armed workspace_member_erasable / workspace_erasable DELETE policies admit;
  -- the explicit WHERE matches those predicates exactly (a workspace is single-owner + a member row is single-
  -- account, so there is no inbound cross-tenant row naming this account to leave behind). Members first, then the
  -- workspaces this account initiated. Content-free (workspace ids + repo names + a consent_state, never a body).
  DELETE FROM core.workspace_member WHERE account_id=v_account;        GET DIAGNOSTICS v_ws_members = ROW_COUNT;
  DELETE FROM core.workspace        WHERE created_by_account=v_account; GET DIAGNOSTICS v_workspaces = ROW_COUNT;
  DELETE FROM core.repository_lifecycle_tombstone WHERE account_id=v_account;
  GET DIAGNOSTICS v_repo_tombstones = ROW_COUNT;
  DELETE FROM core.repository_lifecycle_activation WHERE account_id=v_account;
  GET DIAGNOSTICS v_repo_activations = ROW_COUNT;
  DELETE FROM core.account_lifecycle_tombstone WHERE account_id=v_account;
  GET DIAGNOSTICS v_account_tombstones = ROW_COUNT;

  -- CROSS-ACCOUNT ROUTING + IDENTITY. credential (the connection-role→identity map; ENABLE-not-FORCE, owner
  -- bypasses RLS) and installation_account (no RLS — the routing table) both reference this account; clear them
  -- so a reinstall provisions cleanly and no stale identity row outlives the erase. Then the agent rows (incl.
  -- the 'GH-<login>' author agents = personal data) and finally the account row itself (a true hard delete).
  DELETE FROM core.credential          WHERE account_id=v_account;  GET DIAGNOSTICS v_creds    = ROW_COUNT;
  DELETE FROM core.installation_account WHERE account_id=v_account;  GET DIAGNOSTICS v_insts    = ROW_COUNT;
  DELETE FROM core.agent               WHERE account_id=v_account;  GET DIAGNOSTICS v_agents   = ROW_COUNT;
  DELETE FROM core.account             WHERE account_id=v_account;  GET DIAGNOSTICS v_accounts = ROW_COUNT;

  -- disarm immediately so nothing later in the txn can ride the erasure token.
  PERFORM set_config('core.account_erasure_token', '', true);
  RETURN jsonb_build_object('ok', true, 'account', v_account, 'erased', jsonb_build_object(
    'events', v_events, 'statements', v_statements, 'claims', v_claims, 'code_nodes', v_nodes,
    'code_edges', v_edges, 'graph_versions', v_versions, 'intents', v_intents, 'grants', v_grants,
    'grants_inbound', v_grants_in,
    'policies', v_policies, 'store_connections', v_stores, 'follows', v_follows,
    'policy_refresh_outbox', v_policy_refresh,
    'graph_convergence_leases', v_graph_refresh,
    'co_change', v_cochange, 'cochange_seen', v_cc_seen, 'webhook_deliveries', v_webhooks,
    'plan_events', v_plan_events,
    'workspace_members', v_ws_members, 'workspaces', v_workspaces,
    'account_tombstones', v_account_tombstones,
    'repository_tombstones', v_repo_tombstones, 'repository_activations', v_repo_activations,
    'credentials', v_creds, 'installations', v_insts, 'agents', v_agents, 'accounts', v_accounts));
END $$;
ALTER FUNCTION core.erase_account_with_authority() OWNER TO veripsa_migrator;

-- rename_repo_coordinate_with_authority: a repo was RENAMED on GitHub (same owner, new full_name). The repo
-- coordinate keys the ENTIRE content-free working set (code graph + live claim/lane state) by full_name, so
-- without re-pointing, a rename would orphan the old coordinate and the renamed repo would read 'unknown'
-- until its next push re-ingests under the new name. This moves the working set old→new within the caller's
-- account (RLS-walled). The append-only EVENT ledger is deliberately NOT rewritten: those landings happened
-- under the old name and that audit fact is permanent + correct (history is honest about the name at the
-- time). Atomic (one txn → all-or-nothing). App-delegation grant only.
--
-- DEDUP-AWARE (the orphan fix, applied to the explicit-rename path too): the old body was a bare UPDATE … SET repo,
-- which (1) PK-COLLIDED (unique_violation → SILENT rename failure) when a NEW coordinate already existed (the
-- renamed repo pushed once under the new name before this webhook arrived), and (2) MISSED core.co_change /
-- co_change_seen_commit (the second derived detector, keyed by repo). It now delegates to core._migrate_repo_
-- coordinate, which migrates EVERY repo-keyed table and KEEPS THE FRESHER side on a per-(branch/pair/sha) overlap
-- (drops the stale loser) — so a rename never collides and never strands the coupling cache. The 2-arg signature +
-- the 'repointed':{nodes,edges,versions,claims} return shape are UNCHANGED (the handler + tests are untouched);
-- the extra co-change counts ride along in the same 'repointed' object. (_migrate_repo_coordinate is defined just
-- below; plpgsql bodies are late-bound — resolved at CALL time — so the forward reference is fine.)
CREATE OR REPLACE FUNCTION core.rename_repo_coordinate_with_authority(p_old_repo text, p_new_repo text)
    RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path TO 'core','pg_catalog' AS $$
DECLARE v_agent text; v_account text; v_old text; v_new text; v_moved jsonb;
BEGIN
  v_old := left(NULLIF(btrim(COALESCE(p_old_repo,'')),''),512);
  v_new := left(NULLIF(btrim(COALESCE(p_new_repo,'')),''),512);
  IF v_old IS NULL OR v_new IS NULL THEN RAISE EXCEPTION 'rename needs old + new repo' USING ERRCODE='23514'; END IF;
  IF v_old = v_new THEN RETURN jsonb_build_object('ok',true,'old',v_old,'new',v_new,'noop',true); END IF;
  SELECT agent, account INTO v_agent, v_account FROM core.establish_session_write_context() AS c(agent,account);
  v_moved := (core._migrate_repo_coordinate(v_account, v_old, v_new))->'moved';
  RETURN jsonb_build_object('ok',true,'old',v_old,'new',v_new,'repointed', COALESCE(v_moved,'{}'::jsonb));
END $$;
ALTER FUNCTION core.rename_repo_coordinate_with_authority(text,text) OWNER TO veripsa_migrator;

-- _migrate_repo_coordinate(account, old_full, new_full): the FULL old→new coordinate move, dedup-aware. The
-- existing rename_repo_coordinate_with_authority does a bare UPDATE … SET repo, which (1) PK-COLLIDES if a NEW
-- coordinate already exists (the renamed repo already pushed once under the new name → a fresh row at the SAME
-- (account, new_repo, branch) PK → the UPDATE raises unique_violation and the rename SILENTLY fails), and (2)
-- MISSES core.co_change / co_change_seen_commit (the second derived detector keyed by repo) — so a renamed repo's
-- coupling cache stranded under the old name. This helper fixes BOTH: it migrates EVERY repo-keyed table and, per
-- (branch / pair / sha) row, KEEPS THE FRESHER side when both old and new already hold a row, then drops the
-- stale old row — so a rename never collides and never strands. INTERNAL (no own grant); the pinned account is the
-- caller's RLS context (current_account already set by the caller). Content-free (paths/branches/shas/counts).
CREATE OR REPLACE FUNCTION core._migrate_repo_coordinate(p_account text, p_old text, p_new text)
    RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path TO 'core','pg_catalog' AS $$
DECLARE v_nodes int; v_edges int; v_versions int; v_claims int; v_cochange int; v_cc_seen int; v_ws_members int; v_grants int;
        v_stores int;
        v_activations int; v_policy_refresh int:=0;
        v_coordinate text;
        v_old_wins text[]; v_old_loses text[];
BEGIN
  IF p_old = p_new THEN
    RETURN jsonb_build_object('migrated',false,'noop',true,'old',p_old,'new',p_new);
  END IF;
  -- PIN GUARD (defense-in-depth): this helper relies on the CALLER having pinned core.current_account to p_account
  -- (RLS walls every read/write below to that tenant). Both real callers do — reconcile_repo_identity_with_authority
  -- and rename_repo_coordinate_with_authority each run establish_session_write_context() first. But a COLD direct
  -- call with NO pin would make RLS hide every row → the migration silently moves NOTHING while still reporting
  -- success (the exact footgun a one-time ops script hit). FAIL LOUD instead: require the pin to equal p_account.
  IF current_setting('core.current_account', true) IS DISTINCT FROM p_account THEN
    RAISE EXCEPTION '_migrate_repo_coordinate needs core.current_account pinned to % (call via a *_with_authority entry point)', p_account
      USING ERRCODE='42501';
  END IF;
  -- Production callers acquire exactly one authenticated payload repository-id lock first. Lifecycle events do
  -- not pre-lock their signed/new mutable coordinate in the outer session; acquire source+destination together in
  -- one deterministic order here. Generic work still holds its one coordinate, but it cannot hold the SAME stable
  -- ID because our caller owns that global key, so waiting on it cannot form a cycle.
  FOR v_coordinate IN
    SELECT coordinate
      FROM unnest(ARRAY[p_old,p_new]) AS coordinates(coordinate)
     GROUP BY coordinate ORDER BY coordinate
  LOOP
    PERFORM pg_advisory_xact_lock(
      hashtext(CASE WHEN p_account LIKE 'ACCT-GH-%' THEN substr(p_account, 9) ELSE p_account END),
      hashtext(v_coordinate));
  END LOOP;
  -- Repo locks exclude full/patch and cold retention for both source and destination. The shared account fence
  -- makes account-wide purge/erase wait for this whole move; cold retention is serialized by those repo locks
  -- instead. Keep the global repo→account order used by every path which needs both tiers.
  PERFORM core._take_account_lifecycle_xact_lock_shared(p_account);
  -- DECIDE THE WINNER PER BRANCH **FIRST** (before any mutation): the graph coordinate for a branch is a UNIT —
  -- its graph_version + code_node + code_edge must move (or drop) TOGETHER, so a single per-branch decision drives
  -- all three. A branch the NEW name already holds with an ingested_at >= the OLD's is FRESHER on the new side (the
  -- post-rename push), so the OLD side LOSES that branch (drop it). Every other old-name branch (new-side absent, or
  -- the old side is fresher) WINS and is re-pointed old→new. Capturing the decision up front avoids the bug where
  -- mutating graph_version first then keying code_node off it reads an ALREADY-CHANGED graph_version (double rows).
  SELECT COALESCE(array_agg(o.branch) FILTER (WHERE NOT EXISTS (
            SELECT 1 FROM core.graph_version n WHERE n.account_id=p_account AND n.repo=p_new AND n.branch=o.branch
              AND COALESCE(n.ingested_at,'epoch'::timestamptz) >= COALESCE(o.ingested_at,'epoch'::timestamptz))),
                  ARRAY[]::text[]),
         COALESCE(array_agg(o.branch) FILTER (WHERE EXISTS (
            SELECT 1 FROM core.graph_version n WHERE n.account_id=p_account AND n.repo=p_new AND n.branch=o.branch
              AND COALESCE(n.ingested_at,'epoch'::timestamptz) >= COALESCE(o.ingested_at,'epoch'::timestamptz))),
                  ARRAY[]::text[])
    INTO v_old_wins, v_old_loses
    FROM core.graph_version o WHERE o.account_id=p_account AND o.repo=p_old;
  -- v_old_wins  = old-name branches to KEEP (re-point old→new; drop any STALE new-name row first so re-point is safe)
  -- v_old_loses = old-name branches the NEW side already has fresher → DROP the old side entirely (new row stays)
  --
  -- GRAPH_VERSION: drop the stale NEW row for a winning branch (so the re-point can't PK-collide), drop the losing
  -- OLD rows, then re-point the winners. mark_governed_write arms the forgery trigger (UPDATE/DELETE gated).
  PERFORM core.mark_governed_write('graph_version');
  DELETE FROM core.graph_version WHERE account_id=p_account AND repo=p_new AND branch = ANY(v_old_wins);
  DELETE FROM core.graph_version WHERE account_id=p_account AND repo=p_old AND branch = ANY(v_old_loses);
  UPDATE core.graph_version SET repo=p_new WHERE account_id=p_account AND repo=p_old AND branch = ANY(v_old_wins);
  GET DIAGNOSTICS v_versions = ROW_COUNT;
  -- CODE_NODE / CODE_EDGE: move with the SAME per-branch decision. For a WINNING branch keep the OLD side's
  -- nodes/edges (they match the kept old graph_version) → drop the NEW side's, then re-point the old. For a LOSING
  -- branch keep the NEW side's → drop the OLD side's. Keyed off the decision arrays, NOT a mutated graph_version, so
  -- exactly ONE side's rows survive per branch (no double rows).
  PERFORM core.mark_governed_write('code_node');
  DELETE FROM core.code_node WHERE account_id=p_account AND repo=p_new AND branch = ANY(v_old_wins);
  DELETE FROM core.code_node WHERE account_id=p_account AND repo=p_old AND branch = ANY(v_old_loses);
  UPDATE core.code_node SET repo=p_new WHERE account_id=p_account AND repo=p_old AND branch = ANY(v_old_wins);
  GET DIAGNOSTICS v_nodes = ROW_COUNT;
  PERFORM core.mark_governed_write('code_edge');
  DELETE FROM core.code_edge WHERE account_id=p_account AND repo=p_new AND branch = ANY(v_old_wins);
  DELETE FROM core.code_edge WHERE account_id=p_account AND repo=p_old AND branch = ANY(v_old_loses);
  UPDATE core.code_edge SET repo=p_new WHERE account_id=p_account AND repo=p_old AND branch = ANY(v_old_wins);
  GET DIAGNOSTICS v_edges = ROW_COUNT;
  -- CLAIM (live lanes): re-point old→new — DEDUP-THEN-REPOINT, exactly like graph_version / workspace_member above.
  -- The claim PK is (account_id, repo, claim_id) — repo is IN the key (widened in 20_core.sql for multi-repo orgs),
  -- so a BARE `UPDATE … SET repo=p_new` PK-COLLIDES whenever BOTH coordinates already hold a claim with the SAME
  -- claim_id (claim_id = 'PR-<n>:<path>', which is per-repo, so the renamed repo's in-flight PRs carry the SAME
  -- claim_ids under both names). 实测 PROD: the orphan-heal example-user/veripsa-core-old → RollNuts/veripsa hit exactly
  -- this — both coords held 'PR-1:…' rows (the OLD one active, the NEW one already released by the post-rename push)
  -- → `duplicate key value violates unique constraint "claim_pkey"` → the WHOLE mover ERRORED (the rename failed).
  -- The old code only de-duped the active-claim INDEX (same target_path, both active) — which does NOT catch a
  -- PK clash where the NEW-side colliding row is released/expired/waiting (not active), so the bare re-point still
  -- aborted. Fix: dedup on the PK FIRST, KEEPING THE FRESHER side (the graph_version winner discipline: the NEW
  -- side wins ties — it is the post-rename live coordinate). Freshness = GREATEST(heartbeat_at, claimed_at) (a live
  -- lane advances its heartbeat; a settled one keeps its last). Drop the OLD row when the NEW coord holds the same
  -- claim_id and is at-least-as-fresh; otherwise drop the STALE NEW row so the fresher OLD one re-points cleanly.
  PERFORM core.mark_governed_write('claim');
  DELETE FROM core.claim o
   WHERE o.account_id=p_account AND o.repo=p_old
     AND EXISTS (SELECT 1 FROM core.claim n
                  WHERE n.account_id=p_account AND n.repo=p_new AND n.claim_id=o.claim_id
                    AND GREATEST(n.heartbeat_at, n.claimed_at) >= GREATEST(o.heartbeat_at, o.claimed_at));
  DELETE FROM core.claim n
   WHERE n.account_id=p_account AND n.repo=p_new
     AND EXISTS (SELECT 1 FROM core.claim o
                  WHERE o.account_id=p_account AND o.repo=p_old AND o.claim_id=n.claim_id
                    AND GREATEST(o.heartbeat_at, o.claimed_at) > GREATEST(n.heartbeat_at, n.claimed_at));
  -- SECOND collision vector — the claim_one_active partial-unique index (account,repo,branch,target_path) WHERE
  -- active: two genuinely-distinct active claims on the same lane under different claim_ids. A rename cannot create
  -- a real such pair (the renamed repo's in-flight PRs are the SAME PRs), but a stray pre-existing one would clash
  -- on re-point even after the PK dedup above — so drop the OLD active row when the NEW coord already holds an
  -- active claim on the same (branch, target_path), keeping the new (post-rename) one. Defensive; never aborts.
  DELETE FROM core.claim o
   WHERE o.account_id=p_account AND o.repo=p_old AND o.claim_state='active'
     AND EXISTS (SELECT 1 FROM core.claim n WHERE n.account_id=p_account AND n.repo=p_new AND n.branch=o.branch
                   AND n.target_path=o.target_path AND n.claim_state='active');
  UPDATE core.claim SET repo=p_new WHERE account_id=p_account AND repo=p_old;
  GET DIAGNOSTICS v_claims = ROW_COUNT;
  -- CO_CHANGE (PK account,repo,path_a,path_b) + its seen-commit ledger: the second derived detector, also keyed by
  -- repo — the same dedup-then-repoint (keep the side with the higher support `co` when a pair exists under both).
  PERFORM core.mark_governed_write('co_change');
  DELETE FROM core.co_change o
   WHERE o.account_id=p_account AND o.repo=p_old
     AND EXISTS (SELECT 1 FROM core.co_change n WHERE n.account_id=p_account AND n.repo=p_new
                   AND n.path_a=o.path_a AND n.path_b=o.path_b AND n.co >= o.co);
  DELETE FROM core.co_change n
   WHERE n.account_id=p_account AND n.repo=p_new
     AND EXISTS (SELECT 1 FROM core.co_change o WHERE o.account_id=p_account AND o.repo=p_old
                   AND o.path_a=n.path_a AND o.path_b=n.path_b);
  UPDATE core.co_change SET repo=p_new WHERE account_id=p_account AND repo=p_old;
  GET DIAGNOSTICS v_cochange = ROW_COUNT;
  PERFORM core.mark_governed_write('co_change_seen_commit');
  -- seen-commit is an idempotency ledger (account,repo,commit_sha): a sha seen under either name is "seen", so
  -- ON CONFLICT-style dedup = drop the old row when the new name already recorded the sha, else re-point it.
  DELETE FROM core.co_change_seen_commit o
   WHERE o.account_id=p_account AND o.repo=p_old
     AND EXISTS (SELECT 1 FROM core.co_change_seen_commit n WHERE n.account_id=p_account AND n.repo=p_new
                   AND n.commit_sha=o.commit_sha);
  UPDATE core.co_change_seen_commit SET repo=p_new WHERE account_id=p_account AND repo=p_old;
  GET DIAGNOSTICS v_cc_seen = ROW_COUNT;
  -- WORKSPACE_MEMBER (the CONSENT layer — moat-critical): a rename ALSO re-keys this account's repo-scoped
  -- consent rows, or the bilateral cross-tenant link goes DARK + a stale 'accepted' row lingers under the dead
  -- coordinate (the same orphan CLASS #436 fixed for the graph, now in the consent substrate). The PK is
  -- (workspace_id, account_id, repo) → repo is IN the key, so a bare re-point can PK-collide if THIS account
  -- already holds a membership for the NEW coord in the SAME workspace (it opted the renamed repo into that
  -- workspace under both names). DEDUP-THEN-REPOINT: drop the OLD row when a NEW-coord row already exists in the
  -- same workspace (the post-rename consent is the live one), then re-point the rest old→new. Content-free
  -- (workspace_id / repo full_name / account id — no code). RLS pins p_account; only THIS tenant's own rows move.
  PERFORM core.mark_governed_write('workspace_member');
  DELETE FROM core.workspace_member o
   WHERE o.account_id=p_account AND o.repo=p_old
     AND EXISTS (SELECT 1 FROM core.workspace_member n WHERE n.account_id=p_account AND n.repo=p_new
                   AND n.workspace_id=o.workspace_id);
  UPDATE core.workspace_member SET repo=p_new WHERE account_id=p_account AND repo=p_old;
  GET DIAGNOSTICS v_ws_members = ROW_COUNT;
  -- GRANT (delegation): only the REPO-SCOPED grants carry this coordinate; the PK is (grantor_account, grant_id)
  -- and grant_id is repo-INDEPENDENT, so re-pointing repo is COLLISION-FREE (no dedup needed). Account-wide grants
  -- have repo='' (the default) and are untouched by the repo=p_old filter. No mark_governed_write: grant is not on
  -- the forgery-trigger set (it is delegation, not a content-free working-set row); a plain owner UPDATE suffices.
  UPDATE core.grant SET repo=p_new WHERE grantor_account=p_account AND repo=p_old;
  GET DIAGNOSTICS v_grants = ROW_COUNT;
  -- GitHub store attachments use `target`, not a literally named `repo` column, but the value is the same mutable
  -- full_name coordinate. Move it with the rest of the live authority or a later deletion at the new name cannot
  -- find and purge an attachment stranded under the old name. connection_id is the PK, so this update cannot
  -- collide; multiple attachment ids may legitimately point at the same repository.
  PERFORM core.mark_governed_write('store_connection');
  UPDATE core.store_connection SET target=p_new
   WHERE account_id=p_account AND provider='github' AND target=p_old;
  GET DIAGNOSTICS v_stores = ROW_COUNT;
  -- Current stable-id lifecycle state follows a same-account rename. If both coordinates somehow exist, keep the
  -- newer activation deterministically before re-pointing so the unique (account,repo) key cannot abort recovery.
  DELETE FROM core.repository_lifecycle_activation old_activation
   WHERE old_activation.account_id=p_account AND old_activation.repo=p_old
     AND EXISTS (
       SELECT 1 FROM core.repository_lifecycle_activation new_activation
        WHERE new_activation.account_id=p_account AND new_activation.repo=p_new
          AND new_activation.activated_at>=old_activation.activated_at);
  DELETE FROM core.repository_lifecycle_activation new_activation
   WHERE new_activation.account_id=p_account AND new_activation.repo=p_new
     AND EXISTS (
       SELECT 1 FROM core.repository_lifecycle_activation old_activation
        WHERE old_activation.account_id=p_account AND old_activation.repo=p_old
          AND old_activation.activated_at>new_activation.activated_at);
  UPDATE core.repository_lifecycle_activation SET repo=p_new
   WHERE account_id=p_account AND repo=p_old;
  GET DIAGNOSTICS v_activations = ROW_COUNT;
  -- 97 scheduler hook (late-bound for the 35-before-97 bootstrap order). This delegate is part of this mover's
  -- atomic coverage: it fences any graph convergence lease snapshot under the old mutable coordinate and
  -- migrates the matching scheduler desired row by stable repository_id to a fresh request
  -- epoch under the new coordinate. It deliberately does not rewrite an old lease in place: that would let the
  -- pre-rename extractor retain authority after its route changed. The exact old token must fail, while the
  -- unchanged lifecycle generation and stable repository_id remain the identity of the replacement request.
  IF to_regprocedure('core._rename_graph_refresh_coordinate(text,text,text)') IS NOT NULL THEN
    EXECUTE 'SELECT core._rename_graph_refresh_coordinate($1,$2,$3)'
      INTO v_policy_refresh USING p_account,p_old,p_new;
  ELSIF to_regclass('core.graph_convergence_lease') IS NOT NULL THEN
    -- Old production schemas have neither the graph lease table nor this hook, so a 35-first rolling apply is a
    -- valid no-op here. Once 97 has published the table, however, silently continuing without its hook would
    -- commit a split rename (graph/lifecycle moved, convergence authority orphaned). Fail transactionally and let
    -- the durable webhook retry after schema cutover instead of creating an unrecoverable mixed coordinate.
    RAISE EXCEPTION 'graph convergence rename hook unavailable; retry after schema cutover'
      USING ERRCODE='55000';
  END IF;
  RETURN jsonb_build_object('migrated',true,'old',p_old,'new',p_new,'moved',
    jsonb_build_object('nodes',v_nodes,'edges',v_edges,'versions',v_versions,'claims',v_claims,
                       'cochange',v_cochange,'cochange_seen',v_cc_seen,
                       'workspace_members',v_ws_members,'grants',v_grants,
                       'store_connections',v_stores,
                       'repository_activations',v_activations,
                       'policy_refresh_outbox',v_policy_refresh));
END $$;
ALTER FUNCTION core._migrate_repo_coordinate(text,text,text) OWNER TO veripsa_migrator;

-- reconcile_repo_identity_with_authority: the LIVE-PATH rename-DETECTION + COORDINATE-MIGRATION. This is the fix
-- for the prod orphan the watchdog could not self-heal: an OWNER-LOGIN rename (the GitHub account `example-user`
-- renamed to `RollNuts`) changes a repo's full_name OWNER segment (example-user/veripsa-core-old → RollNuts/veripsa),
-- but GitHub fires NO `repository` rename/transfer webhook for an owner-login rename — so the existing rename
-- handler never ran, the new push minted a FRESH coordinate, and the OLD coordinate orphaned: stuck at a stale
-- sha, behind main HEAD FOREVER, spamming graph_stale that NEVER self-heals (a missed push that, under the old
-- name, will never arrive). A plain repo rename (same owner) hits the SAME orphan whenever the `repository`
-- renamed webhook is missed/lost. The rename-STABLE signal is GitHub's repository.id (carried on EVERY push / PR /
-- repository payload, fixed across BOTH a repo rename and an owner-login rename). So every live push calls this
-- with the CURRENT full_name + the stable id:
--   1. STAMP the id onto the current coordinate's graph_version row(s) (idempotent UPDATE) so future renames can
--      match by id. (A coordinate ingested before this column existed gets stamped on its next push.)
--   2. PROBE for a DIFFERENTLY-NAMED coordinate under the SAME tenant carrying the SAME stable id — an orphan from
--      a rename we did not see. If found, MIGRATE it old→new (core._migrate_repo_coordinate, dedup-aware: when the
--      new name already has a fresher coordinate from the post-rename push, the stale old row is dropped, not
--      collided). Multiple old names (a repo renamed twice without us catching either) are all migrated.
-- ACCOUNT-SCOPED + un-forgeable: identity is the CONNECTION ROLE (establish_session_write_context), never a caller
-- arg — a tenant can only reconcile ITS OWN coordinates; RLS pins the account. Both p_repo + p_repo_id are
-- content-free git metadata (a full_name + a numeric id). NEVER-CRASH / FAIL-SAFE: a missing/garbage id is inert
-- (no probe → no migration → just behaves as today), so a payload without repository.id never breaks ingest.
-- IDEMPOTENT: once migrated, the old name no longer exists, so a redelivery finds nothing to move (a no-op).
-- App-delegation grant only (the webhook worker calls it; a buyer writer must not re-key another tenant).
CREATE OR REPLACE FUNCTION core.reconcile_repo_identity_with_authority(p_repo text, p_repo_id text)
    RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path TO 'core','pg_catalog' AS $$
DECLARE v_agent text; v_account text; v_repo text; v_id text; v_old text; v_migrations jsonb := '[]'::jsonb;
        v_lock_repos text[]; v_rechecked_lock_repos text[]; v_lock_ok boolean;
        v_stamped int := 0; v_activation_at timestamptz := NULL; v_superseded int := 0;
        v_onboarding_repos text[] := ARRAY[]::text[];
        v_rechecked_onboarding_repos text[] := ARRAY[]::text[];
        v_has_onboarding_outbox boolean := false; v_onboarding_recorded boolean := false;
        v_scheduler_conflict boolean := false;
BEGIN
  v_repo := left(NULLIF(btrim(COALESCE(p_repo,'')),''),512);
  -- the stable id must be a clean positive-integer STRING (GitHub repository.id) — anything else is treated as
  -- ABSENT (the probe + stamp are skipped), so a malformed/missing id can never store junk or mis-match a tenant.
  v_id   := NULLIF(btrim(COALESCE(p_repo_id,'')),'');
  IF v_id IS NOT NULL AND (length(v_id) > 32 OR v_id !~ '^[1-9][0-9]*$') THEN v_id := NULL; END IF;
  IF v_repo IS NULL OR v_id IS NULL THEN
    RETURN jsonb_build_object('ok',true,'reconciled',false,'reason','no usable repo/repo_id (inert)');
  END IF;
  SELECT agent, account INTO v_agent, v_account FROM core.establish_session_write_context() AS c(agent,account);
  IF v_account IS NULL OR v_account = '' THEN
    RETURN jsonb_build_object('ok',true,'reconciled',false,'reason','no resolved account (inert)');
  END IF;
  PERFORM pg_advisory_xact_lock(hashtext('github-repository-id'),hashtext(v_id));
  IF EXISTS (
      SELECT 1 FROM core.repository_lifecycle_tombstone
       WHERE account_id=v_account AND repository_id=v_id) THEN
    RETURN jsonb_build_object('ok',true,'reconciled',false,
                              'reason','repository stable id is tombstoned');
  END IF;
  -- During schema-first rollout 35 is installed before 97, so discover the additive onboarding columns through
  -- pg_catalog and read them dynamically only when the full shape exists. A real installation.created path has
  -- no repository activation and no graph yet; its exact unfinished onboarding row is nevertheless signed,
  -- stable-id-bound durable authority for discovering the pre-HEAD mutable name.
  SELECT to_regclass('core.policy_refresh_outbox') IS NOT NULL
         AND EXISTS (
           SELECT 1 FROM pg_attribute
            WHERE attrelid=to_regclass('core.policy_refresh_outbox')
              AND attname='onboarding_pending' AND attnum>0 AND NOT attisdropped)
         AND EXISTS (
           SELECT 1 FROM pg_attribute
            WHERE attrelid=to_regclass('core.policy_refresh_outbox')
              AND attname='repository_id' AND attnum>0 AND NOT attisdropped)
    INTO v_has_onboarding_outbox;
  IF v_has_onboarding_outbox THEN
    EXECUTE
      'SELECT COALESCE(array_agg(repo ORDER BY repo),ARRAY[]::text[]) '
      'FROM core.policy_refresh_outbox '
      'WHERE account_id=$1 AND request_kind=''graph'' AND repository_id=$2 '
      'AND onboarding_pending AND done_at IS NULL'
      INTO v_onboarding_repos USING v_account,v_id;
  END IF;
  -- Lock the current name plus every old name carrying this stable id BEFORE joining the account fence. A normal
  -- graph writer may already hold current-repo→account(shared) when it calls reconciliation after ingest; in that
  -- late-discovery shape, waiting for an old repo behind a queued account-exclusive purge can deadlock. Therefore
  -- every additional repo acquisition is NON-BLOCKING: contention returns a safe retry/no-op without mutation.
  SELECT array_agg(candidate.repo ORDER BY candidate.repo)
    INTO v_lock_repos
    FROM (
      SELECT v_repo AS repo
      UNION
      SELECT DISTINCT repo FROM core.graph_version
       WHERE account_id=v_account AND repo_id=v_id
      UNION
      -- Fresh installation onboarding has authoritative lifecycle identity before its first graph exists.  The
      -- stable id is already enough to discover a same-owner rename; excluding it here made the HEAD phase retry
      -- forever until an impossible first graph appeared under the new name.
      SELECT repo FROM core.repository_lifecycle_activation
       WHERE account_id=v_account AND repository_id=v_id
      UNION
      SELECT unnest(v_onboarding_repos) AS repo
    ) candidate;
  FOREACH v_old IN ARRAY v_lock_repos LOOP
    v_lock_ok := pg_try_advisory_xact_lock(
      hashtext(CASE WHEN v_account LIKE 'ACCT-GH-%' THEN substr(v_account,9) ELSE v_account END),
      hashtext(v_old));
    IF NOT v_lock_ok THEN
      RETURN jsonb_build_object('ok',true,'reconciled',false,
                                'reason','repository coordinate lock busy; retry');
    END IF;
  END LOOP;
  IF v_has_onboarding_outbox THEN
    EXECUTE
      'SELECT COALESCE(array_agg(repo ORDER BY repo),ARRAY[]::text[]) '
      'FROM core.policy_refresh_outbox '
      'WHERE account_id=$1 AND request_kind=''graph'' AND repository_id=$2 '
      'AND onboarding_pending AND done_at IS NULL'
      INTO v_rechecked_onboarding_repos USING v_account,v_id;
  END IF;
  SELECT array_agg(candidate.repo ORDER BY candidate.repo)
    INTO v_rechecked_lock_repos
    FROM (
      SELECT v_repo AS repo
      UNION
      SELECT DISTINCT repo FROM core.graph_version
       WHERE account_id=v_account AND repo_id=v_id
      UNION
      SELECT repo FROM core.repository_lifecycle_activation
       WHERE account_id=v_account AND repository_id=v_id
      UNION
      SELECT unnest(v_rechecked_onboarding_repos) AS repo
    ) candidate;
  IF v_rechecked_lock_repos IS DISTINCT FROM v_lock_repos THEN
    RETURN jsonb_build_object('ok',true,'reconciled',false,
                              'reason','repository coordinate set changed; retry');
  END IF;
  PERFORM core._take_account_lifecycle_xact_lock_shared(v_account);
  -- A destructive account lifecycle operation may have completed while a direct reconciliation waited for the
  -- shared fence. Re-check after the lock; never stamp/migrate a tombstoned generation.
  IF EXISTS (
      SELECT 1 FROM core.account_lifecycle_tombstone
       WHERE account_id=v_account AND active) THEN
    RETURN jsonb_build_object('ok',true,'reconciled',false,
                              'reason','account is tombstoned');
  END IF;
  -- The authenticated current name may already belong to a different lifecycle object in our durable state
  -- (for example name reuse racing delayed rename observation).  Never freshness-merge that object's working
  -- set into this stable id.  Likewise an unsuperseded coordinate tombstone remains authoritative until an
  -- explicit lifecycle event, not a metadata read, resolves it.
  IF EXISTS (
      SELECT 1 FROM core.repository_lifecycle_activation
       WHERE account_id=v_account AND repo=v_repo AND repository_id<>v_id)
     OR EXISTS (
      SELECT 1 FROM core.graph_version
       WHERE account_id=v_account AND repo=v_repo
         AND repo_id IS NOT NULL AND repo_id<>v_id)
     OR EXISTS (
      SELECT 1 FROM core.repository_lifecycle_tombstone
       WHERE account_id=v_account AND repo=v_repo AND superseded_at IS NULL) THEN
    RETURN jsonb_build_object('ok',true,'reconciled',false,
                              'reason','canonical repository coordinate conflicts; retry');
  END IF;
  IF v_has_onboarding_outbox THEN
    EXECUTE
      'SELECT EXISTS (SELECT 1 FROM core.policy_refresh_outbox '
      'WHERE account_id=$1 AND request_kind=''graph'' AND repo=$2 '
      'AND repository_id<>$3 AND done_at IS NULL)'
      INTO v_scheduler_conflict USING v_account,v_repo,v_id;
    IF v_scheduler_conflict THEN
      RETURN jsonb_build_object('ok',true,'reconciled',false,
                                'reason','canonical scheduler coordinate conflicts; retry');
    END IF;
  END IF;
  -- 1) STAMP the stable id onto the CURRENT coordinate (every branch row at this full_name). Idempotent: re-runs
  --    re-write the same id. Only set when NULL-or-different so a no-op push does no useless write. The forgery
  --    trigger gates this UPDATE → arm the token.
  PERFORM core.mark_governed_write('graph_version');
  UPDATE core.graph_version SET repo_id = v_id
   WHERE account_id = v_account AND repo = v_repo AND (repo_id IS DISTINCT FROM v_id);
  GET DIAGNOSTICS v_stamped = ROW_COUNT;
  -- 2) PROBE for orphaned coordinate(s): the SAME stable id under THIS tenant at a DIFFERENT full_name = a rename
  --    we never saw (an owner-login rename, or a missed `repository` rename webhook). Migrate each old→new. A repo
  --    renamed multiple times without us catching any leaves several old names — loop over all of them. The id
  --    match is exact + tenant-pinned, so this can NEVER touch another repo or another tenant.
  FOREACH v_old IN ARRAY v_lock_repos LOOP
    IF v_old <> v_repo THEN
      v_migrations := v_migrations
        || jsonb_build_array(core._migrate_repo_coordinate(v_account, v_old, v_repo));
    END IF;
  END LOOP;
  -- A signed work event may beat repository.created / installation_repositories.added. Once its graph has
  -- successfully landed, remember that stable id as the coordinate owner so a later stale event for the prior
  -- object cannot flip the coordinate back. A conflicting activation is never overwritten (the hot-path guard
  -- should have rejected that event before ingest); the explicit lifecycle path remains the authority that can
  -- replace an existing owner and reset legacy working state.
  -- `_migrate_repo_coordinate` may already have moved an authenticated activation even when graph_version is
  -- empty (install commit -> GitHub rename -> first HEAD turn).  Treat that exact stable-id/current-name row as
  -- the positive durable proof; only synthesize a work-observed activation when a current graph actually exists.
  SELECT activated_at INTO v_activation_at
    FROM core.repository_lifecycle_activation
   WHERE account_id=v_account AND repository_id=v_id AND repo=v_repo;
  IF v_activation_at IS NULL THEN
    INSERT INTO core.repository_lifecycle_activation AS existing(
        account_id,repository_id,repo,activated_at,lifecycle_authoritative,generation_started_at)
    SELECT v_account,v_id,v_repo,clock_timestamp(),false,NULL
     WHERE EXISTS (
       SELECT 1 FROM core.graph_version
        WHERE account_id=v_account AND repo=v_repo AND repo_id=v_id)
       AND NOT EXISTS (
         SELECT 1 FROM core.repository_lifecycle_activation
          WHERE account_id=v_account AND repo=v_repo AND repository_id<>v_id)
    ON CONFLICT (account_id,repository_id)
    DO UPDATE SET repo=EXCLUDED.repo,
                  -- The stable id proves this is a rename, not a replacement. Preserve an authenticated add's
                  -- durable ordering boundary across the coordinate move; for work-only ownership, a newly observed
                  -- coordinate may take the newer observation time but remains non-authoritative.
                  activated_at=CASE
                    WHEN existing.lifecycle_authoritative OR existing.repo=EXCLUDED.repo
                      THEN existing.activated_at
                    ELSE EXCLUDED.activated_at
                  END,
                  lifecycle_authoritative=existing.lifecycle_authoritative,
                  generation_started_at=existing.generation_started_at
    RETURNING activated_at INTO v_activation_at;
  END IF;
  IF v_activation_at IS NOT NULL THEN
    UPDATE core.repository_lifecycle_tombstone
       SET superseded_at=COALESCE(superseded_at,v_activation_at)
     WHERE account_id=v_account AND repo=v_repo
       AND repository_id<>'unknown' AND repository_id<>v_id;
    GET DIAGNOSTICS v_superseded = ROW_COUNT;
  END IF;
  IF v_has_onboarding_outbox THEN
    EXECUTE
      'SELECT EXISTS (SELECT 1 FROM core.policy_refresh_outbox '
      'WHERE account_id=$1 AND request_kind=''graph'' AND repository_id=$2 '
      'AND repo=$3 AND onboarding_pending AND done_at IS NULL)'
      INTO v_onboarding_recorded USING v_account,v_id,v_repo;
  END IF;
  RETURN jsonb_build_object('ok',true,'reconciled', jsonb_array_length(v_migrations) > 0,
    'repo',v_repo,'repo_id',v_id,'stamped',v_stamped,'migrations',v_migrations,
    'activation_recorded',v_activation_at IS NOT NULL,
    'onboarding_recorded',v_onboarding_recorded,'superseded',v_superseded);
END $$;
ALTER FUNCTION core.reconcile_repo_identity_with_authority(text,text) OWNER TO veripsa_migrator;

-- Publish proof-required transfer + proofless traps/ACLs as one rolling catalog state.  An old worker must never
-- observe the new destructive body while its proofless overload is still executable.
BEGIN;

-- transfer_repo_coordinate_with_authority: purge the former owner's rebuildable working set after a genuine
-- cross-account repository.transferred delivery.  Cross-tenant coordinates are never accepted as naked caller
-- authority: the proof-required surface re-derives old account/full-name, stable repository id, action, and new
-- account from the exact durable PROCESSING row that survived GitHub signature verification.  It then takes the
-- former coordinate's normal advisory lock and re-checks generation state.  A delayed retry may run after GitHub
  -- has already reused old-owner/name for another repository; a different activation/graph id or an unversioned graph
-- makes the coordinate non-destructive. An exact same-id row remains stale old-owner work even when a delayed push
-- wrote it after receipt, because the locked live read proves that id is currently at the new owner. The append-only
-- event ledger stays.
CREATE OR REPLACE FUNCTION core.transfer_repo_coordinate_with_authority(
    p_old_account text, p_repo text, p_repository_id text, p_delivery_key text,
    p_current_state text, p_current_repository_id text, p_current_owner_id text, p_current_full_name text)
    RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path TO 'core','pg_catalog' AS $$
DECLARE
  v_agent text; v_new_account text; v_prev text;
  v_old_account text; v_repo text; v_repository_id text; v_delivery_key text;
  v_current_state text; v_current_repository_id text; v_current_owner_id text; v_current_full_name text;
  v_delivery_received_at timestamptz; v_delivery_repo text; v_payload jsonb; v_completed jsonb; v_outcome text;
  v_probe_token text; v_current_lock_token text;
  v_payload_id text; v_repo_name text; v_old_repo_name text;
  v_new_full text; v_new_owner_id text; v_new_owner_login text;
  v_old_user_id text; v_old_user_login text; v_old_org_id text; v_old_org_login text;
  v_old_owner_id text; v_old_owner_login text; v_derived_old_account text; v_derived_old_repo text;
  v_candidate_repos text[] := ARRAY[]::text[];
  v_rechecked_candidate_repos text[] := ARRAY[]::text[];
  v_lock_owner_id text; v_lock_repo text;
  v_target_repo text; v_target_stale_reason text;
  v_any_isolated boolean := false; v_any_purged boolean := false; v_count int := 0;
  v_point_read_allows_purge boolean := false;
  v_activation_id text; v_activation_generation_started_at timestamptz;
  v_has_activation boolean := false;
  v_newer_or_other_graph boolean := false; v_exact_identity_proven boolean := false;
  v_has_working_set boolean := false; v_stale_reason text;
  v_nodes int := 0; v_edges int := 0; v_versions int := 0; v_claims int := 0;
  v_cochange int := 0; v_cc_seen int := 0; v_activations int := 0;
  v_ws_members int := 0; v_grants int := 0; v_stores int := 0; v_webhooks int := 0;
  v_marked int := 0; v_tombstones int := 0;
BEGIN
  v_old_account := NULLIF(btrim(COALESCE(p_old_account,'')),'');
  v_repo := NULLIF(btrim(COALESCE(p_repo,'')),'');
  v_repository_id := NULLIF(btrim(COALESCE(p_repository_id,'')),'');
  v_delivery_key := NULLIF(btrim(COALESCE(p_delivery_key,'')),'');
  v_current_state := NULLIF(btrim(COALESCE(p_current_state,'')),'');
  v_current_repository_id := NULLIF(btrim(COALESCE(p_current_repository_id,'')),'');
  v_current_owner_id := NULLIF(btrim(COALESCE(p_current_owner_id,'')),'');
  v_current_full_name := NULLIF(btrim(COALESCE(p_current_full_name,'')),'');
  IF v_old_account IS NULL OR length(v_old_account)>512
     OR v_repo IS NULL OR length(v_repo)>512
     OR v_delivery_key IS NULL OR length(v_delivery_key)>200 THEN
    RAISE EXCEPTION 'transfer purge needs bounded old account, repo, and durable delivery authority'
      USING ERRCODE='23514';
  END IF;
  IF v_repository_id IS NULL OR length(v_repository_id)>32
     OR v_repository_id !~ '^[1-9][0-9]*$' THEN
    RAISE EXCEPTION 'transfer repository id must be a positive canonical ASCII decimal'
      USING ERRCODE='23514';
  END IF;
  IF v_current_state NOT IN ('found','absent','probe','lock_current') THEN
    RAISE EXCEPTION 'transfer purge needs a canonical current-identity proof state'
      USING ERRCODE='23514';
  END IF;

  SELECT agent,account INTO v_agent,v_new_account
    FROM core.establish_session_write_context() AS c(agent,account);
  SELECT d.received_at,d.repo,d.payload INTO v_delivery_received_at,v_delivery_repo,v_payload
    FROM core.webhook_delivery d
   WHERE d.delivery_key=v_delivery_key
     AND d.status='processing'
     AND d.event_type='repository'
     AND d.payload->>'action'='transferred'
     AND d.account_key IN (
       v_new_account,
       CASE WHEN v_new_account LIKE 'ACCT-GH-%' THEN substr(v_new_account,9) ELSE v_new_account END)
   FOR UPDATE;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'transfer purge needs an exact processing durable transfer delivery'
      USING ERRCODE='42501';
  END IF;
  IF jsonb_typeof(v_payload->'repository') IS DISTINCT FROM 'object' THEN
    RAISE EXCEPTION 'transfer delivery has no canonical repository object' USING ERRCODE='23514';
  END IF;

  v_payload_id := NULLIF(btrim(v_payload#>>'{repository,id}'),'');
  v_repo_name := NULLIF(btrim(v_payload#>>'{repository,name}'),'');
  -- GitHub permits renaming while transferring. `repository.name` is the NEW short name; when present,
  -- changes.repository.name.from is the durable OLD short name. Older/no-rename payloads omit it.
  v_old_repo_name := COALESCE(
    NULLIF(btrim(v_payload#>>'{changes,repository,name,from}'),''), v_repo_name);
  v_new_full := NULLIF(btrim(v_payload#>>'{repository,full_name}'),'');
  v_new_owner_id := NULLIF(btrim(v_payload#>>'{repository,owner,id}'),'');
  v_new_owner_login := NULLIF(btrim(v_payload#>>'{repository,owner,login}'),'');
  v_old_user_id := NULLIF(btrim(v_payload#>>'{changes,owner,from,user,id}'),'');
  v_old_user_login := NULLIF(btrim(v_payload#>>'{changes,owner,from,user,login}'),'');
  v_old_org_id := NULLIF(btrim(v_payload#>>'{changes,owner,from,organization,id}'),'');
  v_old_org_login := NULLIF(btrim(v_payload#>>'{changes,owner,from,organization,login}'),'');

  IF v_payload_id IS NULL OR length(v_payload_id)>32 OR v_payload_id !~ '^[1-9][0-9]*$'
     OR v_new_owner_id IS NULL OR length(v_new_owner_id)>32 OR v_new_owner_id !~ '^[1-9][0-9]*$'
     OR v_repo_name IS NULL OR position('/' IN v_repo_name)>0
     OR v_old_repo_name IS NULL OR position('/' IN v_old_repo_name)>0
     OR v_new_full IS NULL OR v_new_owner_login IS NULL
     OR v_new_full IS DISTINCT FROM v_new_owner_login||'/'||v_repo_name THEN
    RAISE EXCEPTION 'transfer delivery carries malformed current repository identity'
      USING ERRCODE='23514';
  END IF;
  IF (v_old_user_id IS NOT NULL OR v_old_user_login IS NOT NULL)
     AND (v_old_org_id IS NOT NULL OR v_old_org_login IS NOT NULL) THEN
    RAISE EXCEPTION 'transfer delivery carries ambiguous former owner identity' USING ERRCODE='23514';
  ELSIF v_old_user_id IS NOT NULL OR v_old_user_login IS NOT NULL THEN
    v_old_owner_id := v_old_user_id;
    v_old_owner_login := v_old_user_login;
  ELSE
    v_old_owner_id := v_old_org_id;
    v_old_owner_login := v_old_org_login;
  END IF;
  IF v_old_owner_id IS NULL OR length(v_old_owner_id)>32 OR v_old_owner_id !~ '^[1-9][0-9]*$'
     OR v_old_owner_login IS NULL OR position('/' IN v_old_owner_login)>0 THEN
    RAISE EXCEPTION 'transfer delivery carries malformed former owner identity'
      USING ERRCODE='23514';
  END IF;

  v_derived_old_account := 'ACCT-GH-'||v_old_owner_id;
  v_derived_old_repo := v_old_owner_login||'/'||v_old_repo_name;
  IF v_new_account IS DISTINCT FROM 'ACCT-GH-'||v_new_owner_id
     OR v_old_owner_id=v_new_owner_id
     OR v_old_account IS DISTINCT FROM v_derived_old_account
     OR v_repo IS DISTINCT FROM v_derived_old_repo
     OR v_repository_id IS DISTINCT FROM v_payload_id
     OR v_new_full IS DISTINCT FROM NULLIF(btrim(COALESCE(v_delivery_repo,'')),'') THEN
    RAISE EXCEPTION 'transfer arguments or session do not match durable delivery authority'
      USING ERRCODE='42501';
  END IF;

  -- GitHub repository ids are globally stable/unique. Serialize every work/onboarding/transfer path on this one
  -- object key before discovering mutable coordinates; a rapid reverse transfer uses the same single key rather
  -- than taking tenant-specific keys in opposite order.
  PERFORM pg_advisory_xact_lock(hashtext('github-repository-id'),hashtext(v_repository_id));

  -- GitHub's standard transferred payload does not expose the former short name, and transfer may rename at the
  -- same time. Re-derive every already-observed former-tenant coordinate carrying the stable repository id, plus
  -- the durable owner/current-name fallback. This is DB evidence, not caller authority. Lock the complete snapshot
  -- in deterministic order BEFORE the network proof so an explicit rename/reconcile cannot move a candidate under
  -- us. Exact-id tombstones later stop a delayed old worker from reviving another coordinate after commit.
  v_prev := current_setting('core.current_account',true);
  PERFORM set_config('core.current_account',v_old_account,true);
  SELECT COALESCE(array_agg(candidate.repo ORDER BY candidate.repo),ARRAY[]::text[])
    INTO v_candidate_repos
    FROM (
      SELECT v_repo AS repo
      UNION
      SELECT a.repo FROM core.repository_lifecycle_activation a
       WHERE a.account_id=v_old_account AND a.repository_id=v_repository_id
      UNION
      SELECT g.repo FROM core.graph_version g
       WHERE g.account_id=v_old_account AND g.repo_id=v_repository_id
    ) candidate
   WHERE candidate.repo IS NOT NULL AND candidate.repo<>'';
  PERFORM set_config('core.current_account',COALESCE(v_prev,''),true);
  -- The durable NEW coordinate and every former-tenant candidate are one lock set. Sort by the complete
  -- (owner-id,repo) advisory key so simultaneous A→B and B→A transfers take the same pair in the same order.
  FOR v_lock_owner_id,v_lock_repo IN
    SELECT lock_key.owner_id,lock_key.repo
      FROM (
        SELECT v_old_owner_id AS owner_id,old_repo.repo
          FROM unnest(v_candidate_repos) AS old_repo(repo)
        UNION
        SELECT v_new_owner_id,v_new_full
      ) lock_key
     ORDER BY lock_key.owner_id,lock_key.repo
  LOOP
    PERFORM pg_advisory_xact_lock(hashtext(v_lock_owner_id),hashtext(v_lock_repo));
  END LOOP;
  -- A same-owner rename may have won a candidate's source lock after our first snapshot. Re-read after every
  -- candidate lock is held; any changed set invalidates the external-proof preparation and retries from scratch.
  -- New same-ID work cannot race this recheck because it must hold the global stable-object key above.
  PERFORM set_config('core.current_account',v_old_account,true);
  SELECT COALESCE(array_agg(candidate.repo ORDER BY candidate.repo),ARRAY[]::text[])
    INTO v_rechecked_candidate_repos
    FROM (
      SELECT v_repo AS repo
      UNION
      SELECT a.repo FROM core.repository_lifecycle_activation a
       WHERE a.account_id=v_old_account AND a.repository_id=v_repository_id
      UNION
      SELECT g.repo FROM core.graph_version g
       WHERE g.account_id=v_old_account AND g.repo_id=v_repository_id
    ) candidate
   WHERE candidate.repo IS NOT NULL AND candidate.repo<>'';
  PERFORM set_config('core.current_account',COALESCE(v_prev,''),true);
  IF v_rechecked_candidate_repos IS DISTINCT FROM v_candidate_repos THEN
    RAISE EXCEPTION 'transfer former-coordinate snapshot changed while acquiring locks'
      USING ERRCODE='40001';
  END IF;
  -- Every source repo lock is now held in deterministic order. Join the account lifecycle fence in shared mode
  -- before any later graph deletion so account-wide purge/erase cannot interleave with transfer isolation.
  PERFORM core._take_account_lifecycle_xact_lock_shared(v_old_account);
  -- Probe and destructive call run on the event worker's same DB transaction. A redirect/onward transfer
  -- discovered by the read uses lock_current to bind its canonical coordinate. A crash rolls back all locks/tokens.
  v_probe_token := v_delivery_key||chr(31)||v_old_account||chr(31)
    ||array_to_string(v_candidate_repos,chr(30))||chr(31)||txid_current()::text;

  -- The durable webhook proves what GitHub said at delivery time, but not that the repository has not since moved
  -- back to the former owner (or onward to a third owner) with the SAME stable id.  The scoped installation client
  -- performs a live GET of the payload's NEW coordinate and passes only bounded current identity metadata here.
  -- A canonical same-id result permits isolation whenever its current owner is NOT the former owner. This covers
  -- rapid A→B→C delivery reordering: C proves that A's exact-id residue is stale just as strongly as B does.
  -- Returning to A is non-destructive; an id mismatch or authoritative 404 is stale. Malformed/transient reads
  -- never reach this function: the worker retries them. Only B may later consume this marker for activation.
  IF v_current_state='probe' THEN
    IF v_current_repository_id IS NOT NULL OR v_current_owner_id IS NOT NULL
       OR v_current_full_name IS NOT NULL THEN
      RAISE EXCEPTION 'transfer completion probe must not carry repository metadata'
        USING ERRCODE='23514';
    END IF;
  ELSIF v_current_state='absent' THEN
    IF v_current_repository_id IS NOT NULL OR v_current_owner_id IS NOT NULL
       OR v_current_full_name IS NOT NULL THEN
      RAISE EXCEPTION 'absent transfer identity proof must not carry repository metadata'
        USING ERRCODE='23514';
    END IF;
    v_stale_reason := 'current_target_absent';
  ELSE
    IF v_current_repository_id IS NULL OR length(v_current_repository_id)>32
       OR v_current_repository_id !~ '^[1-9][0-9]*$'
       OR v_current_owner_id IS NULL OR length(v_current_owner_id)>32
       OR v_current_owner_id !~ '^[1-9][0-9]*$'
       OR v_current_full_name IS NULL OR length(v_current_full_name)>512 THEN
      RAISE EXCEPTION 'found transfer identity proof is malformed' USING ERRCODE='23514';
    END IF;
    IF v_current_repository_id<>v_repository_id THEN
      v_stale_reason := 'current_repository_changed';
    ELSIF v_current_owner_id=v_old_owner_id THEN
      v_stale_reason := 'current_owner_changed';
    ELSE
      v_point_read_allows_purge := true;
      v_current_lock_token := v_delivery_key||chr(31)||v_repository_id||chr(31)
        ||v_current_owner_id||chr(31)||v_current_full_name||chr(31)||txid_current()::text;
    END IF;
  END IF;

  -- A committed first pass records its exact content-free identity in the still-processing row.  This is the
  -- idempotency proof after the old graph/activation has gone; never fall back to a broad routing-row assertion.
  v_completed := v_payload->'_veripsa_transfer_completed';
  IF v_completed IS NOT NULL THEN
    IF jsonb_typeof(v_completed) IS DISTINCT FROM 'object'
       OR v_completed->>'old_account' IS DISTINCT FROM v_old_account
       OR v_completed->>'repo' IS DISTINCT FROM v_repo
       OR v_completed->>'repository_id' IS DISTINCT FROM v_repository_id
       OR v_completed->>'outcome' NOT IN ('purged','isolated','stale') THEN
      RAISE EXCEPTION 'transfer completion marker does not match durable delivery'
        USING ERRCODE='42501';
    END IF;
    v_outcome := v_completed->>'outcome';
    RETURN jsonb_build_object(
      'ok',true,'transferred',v_outcome IN ('purged','isolated'),
      'ownership_isolated',v_outcome IN ('purged','isolated'),
      'current_account_owns_repository',
        v_completed->>'current_owner_id'=v_new_owner_id,
      'outcome',v_outcome,'stale_lifecycle_event',v_outcome='stale',
      'stale_reason',v_completed->>'stale_reason',
      'idempotent',true,'old_account',v_old_account,'repo',v_repo,
      'old_repos',COALESCE(v_completed->'old_repos','[]'::jsonb),
      'repository_id',v_repository_id,'reingest_on_next_push',
        COALESCE((v_completed->>'reingest_on_next_push')::boolean,v_outcome='purged'),
      'purged_old',jsonb_build_object('nodes',0,'edges',0,'versions',0,'claims',0,
        'cochange',0,'cochange_seen',0,'repository_activations',0,
        'workspace_members',0,'grants',0,'store_connections',0,'webhook_deliveries',0));
  END IF;

  IF v_current_state='probe' THEN
    -- Transaction-local only: committing/losing this connection erases the token, so a later finalize cannot claim
    -- that the old-coordinate lock spanned its network read.  Exact key/account/repo/XID prevents cross-delivery use.
    PERFORM set_config('core.transfer_probe_token',v_probe_token,true);
    RETURN jsonb_build_object('ok',true,'completed',false,'proof_required',true,
      'old_account',v_old_account,'repo',v_repo,'old_repos',v_candidate_repos,
      'repository_id',v_repository_id);
  END IF;
  IF NULLIF(current_setting('core.transfer_probe_token',true),'') IS DISTINCT FROM v_probe_token THEN
    RAISE EXCEPTION 'transfer purge requires a same-transaction pre-read coordinate lock probe'
      USING ERRCODE='42501';
  END IF;
  IF v_current_state='lock_current' THEN
    IF NOT v_point_read_allows_purge THEN
      RAISE EXCEPTION 'transfer current-coordinate lock needs matching repository and owner proof'
        USING ERRCODE='42501';
    END IF;
    -- The first point read may follow a rename or a rapid onward transfer away from the durable payload identity.
    -- Lock that trusted current coordinate, then require the worker to repeat the GET while this xact lock is held.
    IF NOT pg_try_advisory_xact_lock(hashtext(v_current_owner_id),hashtext(v_current_full_name)) THEN
      RAISE EXCEPTION 'transfer current coordinate is busy; retry the durable proof'
        USING ERRCODE='40001';
    END IF;
    PERFORM set_config('core.transfer_current_lock_token',v_current_lock_token,true);
    RETURN jsonb_build_object('ok',true,'completed',false,'current_reproof_required',true,
      'repository_id',v_repository_id,'owner_id',v_current_owner_id,'full_name',v_current_full_name);
  END IF;
  IF v_point_read_allows_purge
     AND (v_current_owner_id IS DISTINCT FROM v_new_owner_id
          OR v_current_full_name IS DISTINCT FROM v_new_full)
     AND NULLIF(current_setting('core.transfer_current_lock_token',true),'')
           IS DISTINCT FROM v_current_lock_token THEN
    RAISE EXCEPTION 'redirected or onward transfer proof requires a locked same-transaction re-read'
      USING ERRCODE='42501';
  END IF;

  -- Every candidate is independently identity-checked under its already-held live coordinate lock. Exact-ID
  -- working sets are rebuildable and purged; NULL/different-ID replacement evidence is preserved and only the
  -- transferred stable ID is tombstoned. This handles standard A/old-name→B/new-name payloads without guessing.
  PERFORM set_config('core.current_account',v_old_account,true);
  IF NOT v_point_read_allows_purge THEN
    v_stale_reason := COALESCE(v_stale_reason,'current_identity_unproven');
    v_outcome := 'stale';
  ELSE
    FOREACH v_target_repo IN ARRAY v_candidate_repos LOOP
      v_activation_id := NULL;
      v_activation_generation_started_at := NULL;
      v_has_activation := false;
      v_newer_or_other_graph := false;
      v_exact_identity_proven := false;
      v_has_working_set := false;
      v_target_stale_reason := NULL;

      SELECT repository_id,generation_started_at,true
        INTO v_activation_id,v_activation_generation_started_at,v_has_activation
        FROM core.repository_lifecycle_activation
       WHERE account_id=v_old_account AND repo=v_target_repo;
      SELECT EXISTS (
        SELECT 1 FROM core.graph_version
         WHERE account_id=v_old_account AND repo=v_target_repo
           AND repo_id IS DISTINCT FROM v_repository_id
      ) INTO v_newer_or_other_graph;
      SELECT (COALESCE(v_has_activation,false) AND v_activation_id=v_repository_id)
             OR (
               EXISTS (SELECT 1 FROM core.graph_version
                        WHERE account_id=v_old_account AND repo=v_target_repo)
               AND NOT EXISTS (
                 SELECT 1 FROM core.graph_version
                  WHERE account_id=v_old_account AND repo=v_target_repo
                    AND repo_id IS DISTINCT FROM v_repository_id))
        INTO v_exact_identity_proven;
      SELECT EXISTS (
        SELECT 1 FROM core.code_node WHERE account_id=v_old_account AND repo=v_target_repo
        UNION ALL SELECT 1 FROM core.code_edge WHERE account_id=v_old_account AND repo=v_target_repo
        UNION ALL SELECT 1 FROM core.graph_version WHERE account_id=v_old_account AND repo=v_target_repo
        UNION ALL SELECT 1 FROM core.claim WHERE account_id=v_old_account AND repo=v_target_repo
        UNION ALL SELECT 1 FROM core.co_change WHERE account_id=v_old_account AND repo=v_target_repo
        UNION ALL SELECT 1 FROM core.co_change_seen_commit WHERE account_id=v_old_account AND repo=v_target_repo
        UNION ALL SELECT 1 FROM core.repository_lifecycle_activation
          WHERE account_id=v_old_account AND repo=v_target_repo
        UNION ALL SELECT 1 FROM core.workspace_member WHERE account_id=v_old_account AND repo=v_target_repo
        UNION ALL SELECT 1 FROM core.grant WHERE grantor_account=v_old_account AND repo=v_target_repo
        UNION ALL SELECT 1 FROM core.store_connection
          WHERE account_id=v_old_account AND provider='github' AND target=v_target_repo
        UNION ALL SELECT 1 FROM core.webhook_delivery
          WHERE (account_key=v_old_account OR account_key=v_old_owner_id) AND repo=v_target_repo
      ) INTO v_has_working_set;

      IF v_has_activation AND v_activation_id<>v_repository_id THEN
        v_target_stale_reason := 'replacement_activation';
      ELSIF v_newer_or_other_graph THEN
        v_target_stale_reason := 'newer_or_replacement_graph';
      ELSIF v_has_working_set AND NOT v_exact_identity_proven THEN
        v_target_stale_reason := 'unproven_coordinate_identity';
      END IF;

      IF v_target_stale_reason IS NULL THEN
        DELETE FROM core.code_node WHERE account_id=v_old_account AND repo=v_target_repo;
        GET DIAGNOSTICS v_count=ROW_COUNT; v_nodes:=v_nodes+v_count;
        DELETE FROM core.code_edge WHERE account_id=v_old_account AND repo=v_target_repo;
        GET DIAGNOSTICS v_count=ROW_COUNT; v_edges:=v_edges+v_count;
        DELETE FROM core.graph_version WHERE account_id=v_old_account AND repo=v_target_repo;
        GET DIAGNOSTICS v_count=ROW_COUNT; v_versions:=v_versions+v_count;
        -- Unversioned claim/co-change/consent/grant/store rows may belong to a replacement and stay preserved.
        DELETE FROM core.repository_lifecycle_activation
         WHERE account_id=v_old_account AND repo=v_target_repo AND repository_id=v_repository_id;
        GET DIAGNOSTICS v_count=ROW_COUNT; v_activations:=v_activations+v_count;
        DELETE FROM core.webhook_delivery d
         WHERE (d.account_key=v_old_account OR d.account_key=v_old_owner_id)
           AND d.repo=v_target_repo AND d.status<>'processing'
           AND (d.received_at,d.delivery_key)<=(v_delivery_received_at,v_delivery_key)
           AND (d.payload->'repository'->>'id'=v_repository_id
                OR (d.status='done' AND NULLIF(d.payload->'repository'->>'id','') IS NULL));
        GET DIAGNOSTICS v_count=ROW_COUNT; v_webhooks:=v_webhooks+v_count;
        v_any_purged := true;
      ELSE
        v_any_isolated := true;
        v_stale_reason := COALESCE(v_stale_reason,v_target_stale_reason);
      END IF;

      -- Exact-id markers are safe even at a preserved replacement coordinate: the hot gate matches account+id,
      -- so ID2 remains admissible while delayed work for transferred ID1 is refused.
      INSERT INTO core.repository_lifecycle_tombstone AS existing(
          account_id,repository_id,repo,reason,lifecycle_received_at,superseded_at,generation_started_at)
      VALUES (v_old_account,v_repository_id,v_target_repo,'repository_transferred',v_delivery_received_at,
              v_delivery_received_at,
              CASE WHEN v_activation_id=v_repository_id
                   THEN v_activation_generation_started_at ELSE NULL END)
      ON CONFLICT (account_id,repository_id,repo)
      DO UPDATE SET reason=EXCLUDED.reason,tombstoned_at=now(),
                    lifecycle_received_at=EXCLUDED.lifecycle_received_at,
                    superseded_at=EXCLUDED.superseded_at,
                    generation_started_at=COALESCE(existing.generation_started_at,
                                                   EXCLUDED.generation_started_at)
        WHERE existing.lifecycle_received_at<=EXCLUDED.lifecycle_received_at;
      GET DIAGNOSTICS v_count=ROW_COUNT; v_tombstones:=v_tombstones+v_count;
    END LOOP;
    v_outcome := CASE WHEN v_any_isolated THEN 'isolated' ELSE 'purged' END;
  END IF;
  PERFORM set_config('core.current_account',COALESCE(v_prev,''),true);

  UPDATE core.webhook_delivery
     SET payload=jsonb_set(payload,'{_veripsa_transfer_completed}',jsonb_build_object(
           'old_account',v_old_account,'repo',v_repo,'repository_id',v_repository_id,
           'old_repos',v_candidate_repos,'reingest_on_next_push',v_any_purged,
           'outcome',v_outcome,'stale_reason',v_stale_reason,
           'current_full_name',CASE WHEN v_point_read_allows_purge THEN v_current_full_name ELSE NULL END,
           'current_owner_id',CASE WHEN v_point_read_allows_purge THEN v_current_owner_id ELSE NULL END),true),
         updated_at=now()
   WHERE delivery_key=v_delivery_key AND status='processing';
  GET DIAGNOSTICS v_marked=ROW_COUNT;
  IF v_marked<>1 THEN
    RAISE EXCEPTION 'transfer delivery lost processing ownership before completion'
      USING ERRCODE='40001';
  END IF;
  RETURN jsonb_build_object(
    'ok',true,'transferred',v_outcome IN ('purged','isolated'),
    'ownership_isolated',v_outcome IN ('purged','isolated'),
    'current_account_owns_repository',v_point_read_allows_purge AND v_current_owner_id=v_new_owner_id,
    'outcome',v_outcome,'stale_lifecycle_event',v_outcome='stale',
    'stale_reason',v_stale_reason,'idempotent',false,'old_account',v_old_account,'repo',v_repo,
    'old_repos',v_candidate_repos,'repository_id',v_repository_id,
    'reingest_on_next_push',v_any_purged,'purged_old',
    jsonb_build_object('nodes',v_nodes,'edges',v_edges,'versions',v_versions,'claims',v_claims,
      'cochange',v_cochange,'cochange_seen',v_cc_seen,'repository_activations',v_activations,
      'workspace_members',v_ws_members,'grants',v_grants,'store_connections',v_stores,
      'repository_tombstones',v_tombstones,
      'webhook_deliveries',v_webhooks));
END $$;
ALTER FUNCTION core.transfer_repo_coordinate_with_authority(text,text,text,text,text,text,text,text)
  OWNER TO veripsa_migrator;
REVOKE EXECUTE ON FUNCTION core.transfer_repo_coordinate_with_authority(text,text,text,text,text,text,text,text)
  FROM PUBLIC,veripsa_writer;
GRANT EXECUTE ON FUNCTION core.transfer_repo_coordinate_with_authority(text,text,text,text,text,text,text,text)
  TO veripsa_app;

-- Proofless v2 worker surface is intentionally non-destructive.  It existed only on a pre-release branch; retain a
-- fail-closed trap for rolling catalog compatibility, but grant it to nobody (the live worker uses /8).
CREATE OR REPLACE FUNCTION core.transfer_repo_coordinate_with_authority(
    p_old_account text,p_repo text,p_repository_id text,p_delivery_key text)
    RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path TO 'core','pg_catalog' AS $$
BEGIN
  RAISE EXCEPTION 'proofless cross-account transfer purge is disabled; live current identity is required'
    USING ERRCODE='42501';
END $$;
ALTER FUNCTION core.transfer_repo_coordinate_with_authority(text,text,text,text) OWNER TO veripsa_migrator;
REVOKE EXECUTE ON FUNCTION core.transfer_repo_coordinate_with_authority(text,text,text,text)
  FROM PUBLIC,veripsa_writer,veripsa_app;

-- Old rolling workers supplied only old account/full-name and cannot carry the live point-read proof.  They must
-- fail/release so a new worker can drain the durable row; never infer destructive authority from queue uniqueness.
CREATE OR REPLACE FUNCTION core.transfer_repo_coordinate_with_authority(p_old_account text,p_repo text)
    RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path TO 'core','pg_catalog' AS $$
BEGIN
  RAISE EXCEPTION 'legacy proofless cross-account transfer purge is disabled'
    USING ERRCODE='42501';
END $$;
ALTER FUNCTION core.transfer_repo_coordinate_with_authority(text,text) OWNER TO veripsa_migrator;
REVOKE EXECUTE ON FUNCTION core.transfer_repo_coordinate_with_authority(text,text)
  FROM PUBLIC,veripsa_writer,veripsa_app;

COMMIT;

-- release_repo_claims_with_authority: a repo was ARCHIVED on GitHub. An archived repo accepts NO further pushes
-- and merges NOTHING — yet its in-flight lanes (every active/waiting claim across ALL its branches) would stay
-- held FOREVER, blocking nothing real but reading as falsely in-flight (and surviving until each lease expires).
-- This is the cancel/withdraw half of the lifecycle applied REPO-WIDE (not per-change): release every active +
-- waiting claim for this repo in one shot. Unlike a per-change withdraw it does NOT promote a next waiter —
-- the WHOLE repo's lane namespace is being emptied (the archived repo can never land anything, so promoting a
-- waiter would only re-strand it). The code graph + the append-only event ledger are KEPT untouched (an
-- archived repo's structure is still real history; re-key/forget are deletion's job, not archive's). Atomic;
-- governed-write token armed (the forgery trigger gates UPDATE). App-delegation grant only.
CREATE OR REPLACE FUNCTION core.release_repo_claims_with_authority(p_repo text)
    RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path TO 'core','pg_catalog' AS $$
DECLARE v_agent text; v_account text; v_repo text; v_released int := 0;
BEGIN
  v_repo := left(NULLIF(btrim(COALESCE(p_repo,'')),''),512);
  IF v_repo IS NULL THEN RAISE EXCEPTION 'release_repo needs a repo' USING ERRCODE='23514'; END IF;
  SELECT agent, account INTO v_agent, v_account FROM core.establish_session_write_context() AS c(agent,account);
  PERFORM core.mark_governed_write('claim');
  -- Release EVERY in-flight (active OR waiting) claim for this repo, across ALL branches, in one UPDATE. Unlike
  -- a per-change withdraw, an archive releases the WHOLE repo at once, so there is NO next waiter to promote:
  -- promoting a waiter into an active lane would only re-strand it (the archived repo can never land anything).
  -- The waiters are released alongside the active holders — the lane namespace for this repo is now empty.
  WITH freed AS (
    UPDATE core.claim SET claim_state='released', released_at=now()
     WHERE account_id=v_account AND repo=v_repo AND claim_state IN ('active','waiting')
    RETURNING claim_id)
  SELECT count(*)::int INTO v_released FROM freed;
  RETURN jsonb_build_object('ok',true,'repo',v_repo,'archived',true,'released',v_released);
END $$;
ALTER FUNCTION core.release_repo_claims_with_authority(text) OWNER TO veripsa_migrator;

-- release_account_claims_with_authority: the install was SUSPENDED on GitHub (installation.suspend). A suspended
-- install accepts NO further work — GitHub STOPS delivering its webhooks and revokes the token — yet every
-- in-flight lane (active/waiting claim) the account held across ALL its repos would otherwise stay HELD until each
-- lease lapses (default 30 min, up to 24 h, AND a swept active claim is RE-QUEUED as 'waiting' by the recall guard
-- when a waiter exists — so the board does NOT reliably go quiet by lease expiry alone). This is the SAME
-- cancel/withdraw half of the lifecycle that release_repo applies repo-wide, applied ACCOUNT-WIDE instead — the
-- graph-preserving counterpart to purge_account_working_set (which is the uninstall and ALSO drops the code graph;
-- a suspend is REVERSIBLE — unsuspend re-onboards — so it must NOT forget the structure, only quiesce the lanes).
-- Release every active + waiting claim for the pinned tenant in one UPDATE, across every repo + branch. Like the
-- repo-wide archive (and unlike a per-change withdraw) it does NOT promote a next waiter — the WHOLE account's
-- lane namespace is being emptied (a suspended install can land nothing, so promoting a waiter would only
-- re-strand it). The code graph + the append-only event ledger are KEPT untouched (suspend is a temporary mask,
-- not a forget; unsuspend re-onboards from the retained structure). ACCOUNT-SCOPED + un-forgeable: identity comes
-- from the CONNECTION ROLE (establish_session_write_context), NEVER a caller argument (no tenant arg → a tenant
-- can only quiesce ITS OWN lanes; RLS pins the account). Atomic; governed-write token armed (the forgery trigger
-- gates the UPDATE). App-delegation grant only. Idempotent: a re-delivered suspend frees nothing the second time.
CREATE OR REPLACE FUNCTION core.release_account_claims_with_authority(
    p_delivery_key text, p_generation_proof jsonb)
    RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path TO 'core','pg_catalog' AS $$
DECLARE v_agent text; v_account text; v_released int := 0; v_revoked int := 0;
        v_delivery_key text; v_target_installation_id text; v_target_account_id text;
        v_proof_state text; v_proof_target_installation_id text; v_proof_account_id text;
        v_proof_current_installation_id text; v_proof_current_account_id text;
        v_proof_current_created_at timestamptz;
        v_current_installation_id text; v_current_installation_created_at timestamptz;
        v_route_rows int := 0;
BEGIN
  SELECT agent, account INTO v_agent, v_account FROM core.establish_session_write_context() AS c(agent,account);
  IF v_account IS NULL OR v_account = '' THEN RAISE EXCEPTION 'release_account needs a resolved account' USING ERRCODE='23514'; END IF;
  PERFORM core._take_account_lifecycle_xact_lock(v_account);
  v_delivery_key := NULLIF(left(COALESCE(p_delivery_key,''),200),'');
  IF v_delivery_key IS NULL THEN
    RAISE EXCEPTION 'account suspend needs exact durable delivery key' USING ERRCODE='55000';
  END IF;
  SELECT NULLIF(left(COALESCE(d.payload->'installation'->>'id',''),64),''),
         NULLIF(left(COALESCE(d.payload->'installation'->'account'->>'id',''),64),'')
    INTO v_target_installation_id,v_target_account_id
    FROM core.webhook_delivery d
   WHERE d.delivery_key=v_delivery_key
     AND d.status='processing'
     AND d.event_type='installation'
     AND d.payload->>'action'='suspend'
     AND (d.account_key=v_account
          OR (v_account LIKE 'ACCT-GH-%' AND d.account_key=substr(v_account,9)));
  IF NOT FOUND OR v_target_installation_id IS NULL OR v_target_account_id IS NULL
     OR NOT (v_target_account_id=v_account OR 'ACCT-GH-'||v_target_account_id=v_account) THEN
    RAISE EXCEPTION 'account suspend needs matching processing durable authority' USING ERRCODE='42501';
  END IF;

  -- The worker resolved this stable account through an App-JWT current-installation point read BEFORE opening a
  -- tenant connection. Bind that bounded result to this exact processing suspend. `current` names the generation
  -- GitHub exposes (which may be replacement B); `absent` is only a real account-endpoint 404. A missing/malformed
  -- proof is never inferred from the nullable rollout cache.
  IF jsonb_typeof(COALESCE(p_generation_proof,'null'::jsonb))<>'object' THEN
    RAISE EXCEPTION 'account suspend needs authoritative current-generation proof' USING ERRCODE='42501';
  END IF;
  v_proof_state := NULLIF(left(COALESCE(p_generation_proof->>'state',''),16),'');
  v_proof_target_installation_id :=
    NULLIF(left(COALESCE(p_generation_proof->>'suspended_installation_id',''),64),'');
  v_proof_account_id := NULLIF(left(COALESCE(p_generation_proof->>'account_id',''),64),'');
  IF v_proof_state NOT IN ('current','absent')
     OR v_proof_target_installation_id IS DISTINCT FROM v_target_installation_id
     OR v_proof_account_id IS DISTINCT FROM v_target_account_id
     OR NOT (v_proof_account_id=v_account OR 'ACCT-GH-'||v_proof_account_id=v_account) THEN
    RAISE EXCEPTION 'account suspend generation proof does not match durable target' USING ERRCODE='42501';
  END IF;

  IF v_proof_state='current' THEN
    IF jsonb_typeof(COALESCE(p_generation_proof->'current','null'::jsonb))<>'object' THEN
      RAISE EXCEPTION 'account suspend current proof omitted installation identity' USING ERRCODE='42501';
    END IF;
    v_proof_current_installation_id :=
      NULLIF(left(COALESCE(p_generation_proof->'current'->>'installation_id',''),64),'');
    v_proof_current_account_id :=
      NULLIF(left(COALESCE(p_generation_proof->'current'->>'account_id',''),64),'');
    BEGIN
      v_proof_current_created_at :=
        NULLIF(p_generation_proof->'current'->>'created_at','')::timestamptz;
    EXCEPTION WHEN invalid_datetime_format OR datetime_field_overflow THEN
      v_proof_current_created_at := NULL;
    END;
    IF v_proof_current_installation_id IS NULL
       OR v_proof_current_account_id IS DISTINCT FROM v_target_account_id
       OR v_proof_current_created_at IS NULL OR NOT isfinite(v_proof_current_created_at)
       OR jsonb_typeof(p_generation_proof->'current'->'suspended') IS DISTINCT FROM 'boolean' THEN
      RAISE EXCEPTION 'account suspend current proof is malformed or cross-account' USING ERRCODE='42501';
    END IF;
  ELSIF p_generation_proof ? 'current' THEN
    RAISE EXCEPTION 'account suspend absence proof cannot carry a current generation' USING ERRCODE='42501';
  END IF;

  SELECT github_installation_id,github_installation_created_at
    INTO v_current_installation_id,v_current_installation_created_at
    FROM core.installation_account
   WHERE account_id=v_account
   ORDER BY github_installation_created_at DESC NULLS LAST,installation_id
   LIMIT 1;

  IF v_proof_state='current' AND v_proof_current_installation_id<>v_target_installation_id THEN
    -- Delayed suspend A observed replacement B. Persist B under the same account lock before returning stale so a
    -- crash cannot leave a rollout-NULL/A cache behind. Never downgrade a newer durable generation, and critically
    -- do NOT touch revoked_at or claims: authority about B makes A's suspend a no-op, not authority to suspend B.
    IF v_current_installation_id IS NULL OR v_current_installation_created_at IS NULL
       OR v_current_installation_id=v_proof_current_installation_id
       OR v_proof_current_created_at>v_current_installation_created_at THEN
      UPDATE core.installation_account
         SET github_installation_id=v_proof_current_installation_id,
             github_installation_created_at=CASE
               WHEN github_installation_id=v_proof_current_installation_id
                 THEN GREATEST(github_installation_created_at,v_proof_current_created_at)
               ELSE v_proof_current_created_at
             END
       WHERE account_id=v_account;
      GET DIAGNOSTICS v_route_rows = ROW_COUNT;
      IF v_route_rows=0 THEN
        RAISE EXCEPTION 'account suspend generation route disappeared' USING ERRCODE='55000';
      END IF;
    END IF;
    RETURN jsonb_build_object('ok',true,'account_wide',true,'suspended',false,
                              'stale_ignored',true,'released',0,'installations_revoked',0,
                              'reason','replacement installation generation is current');
  END IF;

  IF v_proof_state='current' THEN
    -- Same-target proof is allowed to seed a NULL rollout row or advance the exact generation. If a different,
    -- same/newer durable generation won the lock first, this observation is stale and cannot revoke it.
    IF v_current_installation_id IS NOT NULL
       AND v_current_installation_id<>v_target_installation_id
       AND v_current_installation_created_at IS NOT NULL
       AND v_proof_current_created_at<=v_current_installation_created_at THEN
      RETURN jsonb_build_object('ok',true,'account_wide',true,'suspended',false,
                                'stale_ignored',true,'released',0,'installations_revoked',0,
                                'reason','newer installation generation is already durable');
    END IF;
    UPDATE core.installation_account
       SET github_installation_id=v_target_installation_id,
           github_installation_created_at=CASE
             WHEN github_installation_id=v_target_installation_id
               THEN GREATEST(github_installation_created_at,v_proof_current_created_at)
             ELSE v_proof_current_created_at
           END
     WHERE account_id=v_account;
  ELSE
    -- A real current-account 404 can suspend only A when no replacement is durable. Seed A even when both rollout
    -- columns are NULL, then quiesce it; NULL is never treated as a successful stale no-op. A durable B wins.
    IF v_current_installation_id IS NOT NULL
       AND v_current_installation_id<>v_target_installation_id THEN
      RETURN jsonb_build_object('ok',true,'account_wide',true,'suspended',false,
                                'stale_ignored',true,'released',0,'installations_revoked',0,
                                'reason','replacement installation generation is already durable');
    END IF;
    UPDATE core.installation_account
       SET github_installation_id=v_target_installation_id,
           github_installation_created_at=CASE
             WHEN github_installation_id=v_target_installation_id
               THEN github_installation_created_at ELSE NULL END
     WHERE account_id=v_account;
  END IF;
  GET DIAGNOSTICS v_route_rows = ROW_COUNT;
  IF v_route_rows=0 THEN
    RAISE EXCEPTION 'account suspend generation route disappeared' USING ERRCODE='55000';
  END IF;

  PERFORM core.mark_governed_write('claim');
  -- Release EVERY in-flight (active OR waiting) claim for THIS tenant, across ALL repos + branches, in one UPDATE.
  -- No next waiter is promoted: a suspended account's whole lane namespace is being emptied (it can land nothing),
  -- so promoting a waiter would only re-strand it. RLS (account pinned above) walls the UPDATE to v_account.
  WITH freed AS (
    UPDATE core.claim SET claim_state='released', released_at=now()
     WHERE account_id=v_account AND claim_state IN ('active','waiting')
    RETURNING claim_id)
  SELECT count(*)::int INTO v_released FROM freed;
  -- INSTALLATION LIVENESS (commercial-completeness — the no-billing-without-a-LIVE-link invariant). A SUSPENDED
  -- install accepts no work and must NOT look live to the platform's billing-liveness gates or to graph freshness,
  -- yet suspend KEEPS the installation→account row (it is reversible — unsuspend re-onboards). STAMP revoked_at=now()
  -- so the install drops out of installation_is_live / the live-only enumerator while suspended; unsuspend clears it
  -- via reactivate_account_with_authority (the `unsuspend` onboarding branch already calls it). No per-account RLS on
  -- this routing table; the account_id match IS the scope. Stamp only still-live rows so a re-delivered suspend does
  -- not churn the timestamp (idempotent, matching this fn's "frees nothing the second time" contract).
  UPDATE core.installation_account SET revoked_at=now()
   WHERE account_id=v_account AND github_installation_id=v_target_installation_id
     AND revoked_at IS NULL;  GET DIAGNOSTICS v_revoked = ROW_COUNT;
  RETURN jsonb_build_object('ok',true,'account_wide',true,'suspended',true,'released',v_released,
                            'installations_revoked',v_revoked);
END $$;
ALTER FUNCTION core.release_account_claims_with_authority(text,jsonb) OWNER TO veripsa_migrator;

-- Schema-first rolling boundary. A worker that knows only the durable key cannot distinguish rollout NULL from
-- replacement B and must retry until a new worker carries the App-JWT proof.
CREATE OR REPLACE FUNCTION core.release_account_claims_with_authority(p_delivery_key text)
    RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path TO 'core','pg_catalog' AS $$
BEGIN
  RAISE EXCEPTION 'rolling account suspend requires new-worker generation proof'
    USING ERRCODE='55000';
END $$;
ALTER FUNCTION core.release_account_claims_with_authority(text) OWNER TO veripsa_migrator;

-- Old workers did not carry the durable delivery identity, so they cannot safely decide whether suspend A still
-- belongs to current generation A or has been overtaken by replacement B.
CREATE OR REPLACE FUNCTION core.release_account_claims_with_authority()
    RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path TO 'core','pg_catalog' AS $$
BEGIN
  RAISE EXCEPTION 'legacy account suspend refused: exact durable generation authority required'
    USING ERRCODE='55000';
END $$;
ALTER FUNCTION core.release_account_claims_with_authority() OWNER TO veripsa_migrator;

-- prune_events_with_authority: RETENTION — the ONLY controlled erase on the append-only ledger. The event
-- ledger grows UNBOUNDED (every push/landing forever × all tenants → tens of GB/year), so this gated fn
-- DELETEs OPERATIONAL TELEMETRY older than p_before, FOR THE CALLER'S OWN ACCOUNT ONLY (account-scoped, via
-- establish_session_write_context → RLS-pinned). It is DISTINCT FROM TAMPERING by construction:
--   • only the KINDS named in p_kinds are touched; the default is operational telemetry ('landed','push').
--   • the curated EFFECT records (warn_issued · collision_held — they feed effect_surface) and any other
--     kind are pruned ONLY if explicitly named; the default leaves them immutable.
--   • the 'statement' records are a SEPARATE table (core.statement) — not reachable here AT ALL, and this
--     fn additionally STRIPS 'statement' from p_kinds defensively so it can never be named into the prune.
--   • the prune arms core.retention_token = the caller's OWN account (mark_retention_prune); the
--     append-only trigger permits the DELETE only for rows of THAT account. A plain (non-gated) DELETE
--     never arms the token and is STILL refused — the append-only guarantee on the curated story is preserved.
-- Returns jsonb {ok, pruned, before, kinds}. SECURITY DEFINER; identity from the connection role.
CREATE OR REPLACE FUNCTION core.prune_events_with_authority(p_before timestamptz, p_kinds text[] DEFAULT ARRAY['landed','push'])
    RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path TO 'core','pg_catalog' AS $$
DECLARE v_agent text; v_account text; v_kinds text[]; v_pruned int;
BEGIN
  IF p_before IS NULL THEN RAISE EXCEPTION 'prune needs a before-timestamp (the retention window edge)' USING ERRCODE='23514'; END IF;
  -- never the curated statement stream (it is a different table; strip it defensively even if named),
  -- and require a non-empty, deduped kind list — refuse a blanket "prune everything".
  v_kinds := ARRAY(SELECT DISTINCT k FROM unnest(COALESCE(p_kinds, ARRAY[]::text[])) AS k
                    WHERE k IS NOT NULL AND btrim(k) <> '' AND k <> 'statement');
  IF array_length(v_kinds, 1) IS NULL THEN
    RAISE EXCEPTION 'prune needs at least one prunable event KIND (statement is never prunable)' USING ERRCODE='23514';
  END IF;
  SELECT agent, account INTO v_agent, v_account FROM core.establish_session_write_context() AS c(agent,account);
  -- arm the retention token for THIS account, then erase only the named telemetry kinds past the window.
  -- RLS (account-pinned by establish_session_write_context) walls the DELETE to v_account; the token in the
  -- append-only trigger double-checks the row's account == v_account, so cross-account erase is impossible.
  PERFORM core.mark_retention_prune(v_account);
  DELETE FROM core.event
   WHERE account_id = v_account AND kind = ANY(v_kinds) AND occurred_at < p_before;
  GET DIAGNOSTICS v_pruned = ROW_COUNT;
  -- disarm immediately so no later statement in the txn can ride the token.
  PERFORM set_config('core.retention_token', '', true);
  RETURN jsonb_build_object('ok', true, 'pruned', v_pruned, 'before', p_before, 'kinds', to_jsonb(v_kinds));
END $$;
ALTER FUNCTION core.prune_events_with_authority(timestamptz, text[]) OWNER TO veripsa_migrator;

-- prune_all_accounts_with_authority: the OPERATOR'S retention sweep — the ONE call a scheduled job makes to
-- keep the unbounded ledger bounded ACROSS ALL TENANTS. prune_events_with_authority is per-account (it prunes
-- only the CALLER'S pinned account), which is right for a tenant self-service prune but useless for the host's
-- nightly retention: the host can't be every tenant. So this owner-context fn iterates every core.account and,
-- for each, arms that account's OWN retention token (mark_retention_prune) and DELETEs only THAT account's old
-- telemetry kinds — the SAME per-account safety as the single-tenant prune, just looped server-side. It NEVER
-- weakens the moat: each DELETE is still token-pinned to the row's own account, the append-only trigger still
-- refuses any row whose account ≠ the armed token, the curated effect records + the statement stream are still
-- untouched by the default, and a plain (un-armed) DELETE is still refused. Granted to veripsa_app ONLY (the
-- host's service identity) — a buyer seat keeps only the per-account prune. Returns {ok, accounts, pruned}.
-- It ALSO runs the DURABLE-INBOX REAPER once per sweep (audit P1): a single cross-tenant age-based DELETE of
-- TERMINAL (done/failed) core.webhook_delivery rows past the window — the fourth unbounded grower, the only one
-- with no account_id (it is cross-tenant, keyed by account_key), so it is reaped fleet-wide here, not per-account.
--
-- BOUNDED-BATCH (audit P2-2 — owner sweeps were O(N accounts) sequential). RETENTION must stay COMPLETE for
-- correctness (every tenant's old telemetry MUST eventually go, or the ledger grows unbounded and fills the
-- 256 MiB instance), so — UNLIKE the read lenses (owner_cost_surface / owner_graph_freshness_surface, which may
-- CAP because they are advisory) — this one is NOT allowed to drop work; it is PAGINATED instead. Two OPTIONAL
-- args make each call do BOUNDED work while the caller (retention_prune.py) loops to FULL coverage:
--   p_max_accounts : at most this many tenants per call (NULL = the whole fleet in one call = the EXACT prior
--                    behaviour — a 1-arg / 2-arg call is byte-for-byte unchanged). When set, one giant sweep
--                    becomes bounded chunks, each its OWN transaction, so locks release between batches and no
--                    single transaction scans thousands of tenants (the O(N) → O(chunk) per-call fix).
--   p_after        : a resume CURSOR — process only tenants with account_id > p_after (NULL = from the start).
--                    Tenants are visited in deterministic account_id order, so paging is stable + gap-free.
-- The result adds next_after (the last account_id processed this call, the cursor to pass next) and done (true
-- when this call reached the end → fewer than p_max_accounts tenants remained). COMPLETENESS is provable: the
-- caller pages account_id-ascending with the returned cursor until done — every tenant past every prior cursor
-- is visited exactly once, so the union of the batches equals the full single-sweep set (the test asserts a
-- paginated run prunes IDENTICALLY to a one-shot run). Each per-account DELETE is byte-for-byte the same as
-- before — same token, same RLS pin, same kinds — so the moat/append-only guarantees are untouched by batching.
-- Adding the p_max_accounts + p_after batch args via CREATE OR REPLACE would leave the OLD 2-arg overload behind
-- on a re-apply (dead code + an ambiguous call) — DROP it first, the same signature-change pattern as
-- patch_graph_with_authority / record_collision_with_authority in 30_gate.sql.
DROP FUNCTION IF EXISTS core.prune_all_accounts_with_authority(timestamptz, text[]);
CREATE OR REPLACE FUNCTION core.prune_all_accounts_with_authority(p_before timestamptz, p_kinds text[] DEFAULT ARRAY['landed','push'], p_max_accounts int DEFAULT NULL, p_after text DEFAULT NULL)
    RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path TO 'core','pg_catalog' AS $$
DECLARE v_kinds text[]; v_acct text; v_repo text; v_n int; v_total int := 0; v_accounts int := 0; v_claims_total int := 0; v_preds_total int := 0;
        v_cold_repos int := 0; v_cold_nodes int := 0; v_cold_edges int := 0; v_cold_versions int := 0;
        v_cold_cochange int := 0; v_cold_cc_seen int := 0;
        v_last text := NULL; v_done boolean := true; v_webhooks int := 0;
BEGIN
  IF p_before IS NULL THEN RAISE EXCEPTION 'prune needs a before-timestamp (the retention window edge)' USING ERRCODE='23514'; END IF;
  -- same defensive kind discipline as the per-account prune: never the statement stream, never a blanket erase.
  v_kinds := ARRAY(SELECT DISTINCT k FROM unnest(COALESCE(p_kinds, ARRAY[]::text[])) AS k
                    WHERE k IS NOT NULL AND btrim(k) <> '' AND k <> 'statement');
  IF array_length(v_kinds, 1) IS NULL THEN
    RAISE EXCEPTION 'prune needs at least one prunable event KIND (statement is never prunable)' USING ERRCODE='23514';
  END IF;
  -- Enumerate tenants via the union of:
  --   • core.installation_account — the service's installation→account map for real GitHub App tenants; and
  --   • core.credential — local/dogfood/credential-backed accounts that can own working sets without a GitHub
  --     installation routing row.
  -- Both tables are owner-readable inside this SECURITY DEFINER function before any account is pinned. Every
  -- per-account DELETE below still re-pins RLS + the retention token to the row's own account, so widening
  -- enumeration does not widen deletion authority; it only stops credential-only working sets from living forever.
  -- DISTINCT: an account can hold >1 installation and >1 credential.
  -- BOUNDED-BATCH: p_after is the resume cursor (process only account_id > p_after; NULL = from the start) and
  -- p_max_accounts caps this call's chunk (NULL = the whole fleet = prior behaviour). account_id-ascending order
  -- makes paging stable + gap-free: each call resumes exactly where the last left off, no tenant seen twice or
  -- skipped. v_last tracks the cursor to return; the DELETEs in the body are byte-for-byte unchanged.
  FOR v_acct IN
      SELECT DISTINCT account_id
        FROM (
          SELECT account_id FROM core.installation_account
          UNION
          SELECT account_id FROM core.credential
        ) tenants
       WHERE account_id IS NOT NULL
         AND btrim(account_id) <> ''
         AND (p_after IS NULL OR account_id > p_after)
       ORDER BY account_id
       LIMIT p_max_accounts          -- NULL LIMIT = no limit (the full single-sweep, exact prior behaviour)
  LOOP
    v_accounts := v_accounts + 1;
    v_last := v_acct;                 -- the cursor to hand back (the highest account_id processed this call)
    -- pin RLS to this account AND arm the per-account retention token, then erase only its old named telemetry.
    -- The token == this account, so the append-only trigger lets the DELETE touch only THIS account's rows —
    -- cross-account erase remains impossible even though one call sweeps every tenant.
    PERFORM set_config('core.current_account', v_acct, true);
    PERFORM core.mark_retention_prune(v_acct);
    DELETE FROM core.event WHERE account_id = v_acct AND kind = ANY(v_kinds) AND occurred_at < p_before;
    GET DIAGNOSTICS v_n = ROW_COUNT;
    v_total := v_total + v_n;
    -- SPENT-PREDICTION SWEEP (audit/backup-restore-retention — the THIRD unbounded grower). 'prediction' is an
    -- append-only event KIND written one-per-change at analysis time (record_prediction_with_authority); it is
    -- NOT in the default telemetry kinds, so the event prune above never touches it. Its ONLY reader is the
    -- close-time answer-check (record_advice_outcome_with_authority joins the change's 'prediction' to grade the
    -- advice). Once that close has recorded the matching 'advice_outcome', the prediction is SPENT — never read
    -- again — yet it stays forever, growing one-per-closed-PR across all tenants: the same unbounded shape as
    -- 'landed'/'push'. We do NOT prune it by age alone (a prediction whose PR is still OPEN has no outcome yet and
    -- MUST survive so the eventual close-time join finds it, even if older than the window — a long-lived PR). So
    -- prune a 'prediction' only when BOTH (a) it is past the window AND (b) its answer-check is DONE: an
    -- 'advice_outcome' exists for the SAME coordinate (account, repo, branch, path=change ref). Removing a spent
    -- prediction can never change a verdict (the live surface re-computes from the in-flight set, never from old
    -- 'prediction' rows) and can never blind the answer-check (the outcome is already on record). The retention
    -- token is still armed here (event is forgery+append-only gated — same token as the telemetry prune above),
    -- so this DELETE is permitted and account-pinned exactly like it. Content-free (coordinates + kind only).
    DELETE FROM core.event p
     WHERE p.account_id = v_acct AND p.kind = 'prediction' AND p.occurred_at < p_before
       AND EXISTS (SELECT 1 FROM core.event o
                    WHERE o.account_id = v_acct AND o.kind = 'advice_outcome'
                      AND o.repo = p.repo AND o.branch = p.branch AND o.path = p.path);
    GET DIAGNOSTICS v_n = ROW_COUNT;
    v_total := v_total + v_n;
    v_preds_total := v_preds_total + v_n;
    PERFORM set_config('core.retention_token', '', true);   -- disarm before the next account
    -- DEAD-CLAIM SWEEP (audit2/scale — the second unbounded grower). A landed/withdrawn/crashed PR leaves its
    -- per-file claim rows behind as claim_state 'released'/'expired' FOREVER: the lock fns only flip state, never
    -- delete (only purge_repo on uninstall does). At a real fleet's rate (tens of PRs/day × ~10 files each → at
    -- 10–100× the anchor, thousands of dead claims/day × ~450 B/row, MEASURED) this is megabytes/day of permanent
    -- bloat the 256 MiB Postgres never reclaims — and unlike core.event, NOTHING pruned it. So here, AFTER the
    -- event prune, drop terminal claims (released/expired) older than the SAME window edge. Safety: (a) only
    -- terminal states — never an 'active'/'waiting' lane that is still coordinating; (b) account-pinned by the
    -- RLS set_config above (the same wall as the event prune); (c) DELETE on core.claim is NOT forgery-gated
    -- (trg_governed_claim is INSERT/UPDATE only — purge_repo deletes the same way), so no token is needed. The
    -- live prediction (main_impact_surface / expire_stale_claims) reads only active+waiting via the partial
    -- index, so removing OLD terminal rows can never change a verdict — it only frees disk + cuts table bloat.
    DELETE FROM core.claim
     WHERE account_id = v_acct AND claim_state IN ('released','expired')
       AND COALESCE(released_at, claimed_at) < p_before;
    GET DIAGNOSTICS v_n = ROW_COUNT;
    v_claims_total := v_claims_total + v_n;
    -- COLD-REPO WORKING-SET SWEEP (PO 2026-07-07): Veripsa is pre-merge traffic control for repositories that
    -- are moving now. A repo that has had no activity past the retention edge does NOT need its rebuildable
    -- structural working set kept warm forever: the next default-branch push/PR will re-ingest the code graph
    -- via ingest_push/self-heal (graph_freshness sees stored_sha=NULL and cold-starts before analysis), and the
    -- co-change history cache is queued for async populate. This bounds DB/storage cost during
    -- the free-first Marketplace launch without deleting durable records. Safety:
    --   • only rebuildable derived state is removed: code_node/code_edge/graph_version + co_change caches;
    --   • this cold sweep does not delete event/statement ledgers or curated effect history;
    --   • repos with any active/waiting claim survive, even if old, so an in-flight PR is never blinded;
    --   • "activity" is max(repo events, graph ingested_at), so a recently used repo stays warm even if its
    --     baseline graph is old.
    FOR v_repo IN
      WITH repo_candidates AS (
        SELECT repo FROM core.graph_version WHERE account_id = v_acct AND repo <> ''
        UNION
        SELECT repo FROM core.code_node WHERE account_id = v_acct AND repo <> ''
        UNION
        SELECT repo FROM core.code_edge WHERE account_id = v_acct AND repo <> ''
        UNION
        SELECT repo FROM core.co_change WHERE account_id = v_acct AND repo <> ''
        UNION
        SELECT repo FROM core.co_change_seen_commit WHERE account_id = v_acct AND repo <> ''
      )
      SELECT r.repo
        FROM (
          SELECT rc.repo,
                 GREATEST(COALESCE((SELECT max(gv.ingested_at)
                                       FROM core.graph_version gv
                                      WHERE gv.account_id = v_acct
                                        AND gv.repo = rc.repo),
                                    '-infinity'::timestamptz),
                          COALESCE((SELECT max(e.occurred_at)
                                      FROM core.event e
                                     WHERE e.account_id = v_acct
                                       AND e.repo = rc.repo),
                                   '-infinity'::timestamptz)) AS last_activity
            FROM repo_candidates rc
        ) r
       WHERE r.last_activity < p_before
         AND NOT EXISTS (
           SELECT 1 FROM core.claim c
            WHERE c.account_id = v_acct
              AND c.repo = r.repo
              AND c.claim_state IN ('active','waiting')
         )
       ORDER BY r.repo
    LOOP
      -- Candidate selection and deletion are separated by statements. Serialize on the exact repo key used by
      -- full/patch, then re-check freshness/claims under the post-lock snapshot; a writer which committed while
      -- we waited makes the repo warm and is never erased, while a writer waiting behind us rebuilds coherently
      -- after the whole cold delete.
      PERFORM pg_advisory_xact_lock(
        hashtext(CASE WHEN v_acct LIKE 'ACCT-GH-%' THEN substr(v_acct,9) ELSE v_acct END),
        hashtext(v_repo));
      IF EXISTS (
           SELECT 1 FROM core.claim c
            WHERE c.account_id=v_acct AND c.repo=v_repo
              AND c.claim_state IN ('active','waiting')
         )
         OR GREATEST(
              COALESCE((SELECT max(gv.ingested_at) FROM core.graph_version gv
                         WHERE gv.account_id=v_acct AND gv.repo=v_repo),
                       '-infinity'::timestamptz),
              COALESCE((SELECT max(e.occurred_at) FROM core.event e
                         WHERE e.account_id=v_acct AND e.repo=v_repo),
                       '-infinity'::timestamptz)
            ) >= p_before THEN
        CONTINUE;
      END IF;
      v_cold_repos := v_cold_repos + 1;
      DELETE FROM core.code_edge WHERE account_id = v_acct AND repo = v_repo;
      GET DIAGNOSTICS v_n = ROW_COUNT;
      v_cold_edges := v_cold_edges + v_n;
      DELETE FROM core.code_node WHERE account_id = v_acct AND repo = v_repo;
      GET DIAGNOSTICS v_n = ROW_COUNT;
      v_cold_nodes := v_cold_nodes + v_n;
      DELETE FROM core.graph_version WHERE account_id = v_acct AND repo = v_repo;
      GET DIAGNOSTICS v_n = ROW_COUNT;
      v_cold_versions := v_cold_versions + v_n;
      DELETE FROM core.co_change WHERE account_id = v_acct AND repo = v_repo;
      GET DIAGNOSTICS v_n = ROW_COUNT;
      v_cold_cochange := v_cold_cochange + v_n;
      DELETE FROM core.co_change_seen_commit WHERE account_id = v_acct AND repo = v_repo;
      GET DIAGNOSTICS v_n = ROW_COUNT;
      v_cold_cc_seen := v_cold_cc_seen + v_n;
    END LOOP;
  END LOOP;
  -- DURABLE-INBOX REAPER (audit P1 — the FOURTH unbounded grower, the durable webhook inbox). core.webhook_delivery
  -- persists one row per accepted webhook and KEEPS terminal rows forever: a 'done' row clears its payload to '{}'
  -- but retains account_key + repo + operational columns, and a 'failed' (poison/exhausted) row stays for the DLQ
  -- sweep — neither is ever deleted by the queue fns themselves, and the per-account event prune above does not
  -- touch this table (it has NO account_id — it is cross-tenant, keyed by the bare account_key). On the 256 MiB
  -- tier that is the binding constraint, so reap TERMINAL rows past the SAME window edge. This is a SINGLE
  -- cross-tenant DELETE done ONCE per sweep (NOT inside the per-account loop): the table has no per-account RLS
  -- and the loop only runs when paginating, so reaping here covers EVERY tenant's terminal rows AND the orphan
  -- account_key IS NULL rows (which no per-account erase could attribute) in one statement, every sweep. Done with
  -- p_after IS NULL (the FIRST / only page of a sweep) so a paginated run reaps exactly once, not once per chunk.
  -- SAFETY: only status IN ('done','failed') — never a live 'queued'/'processing' row still awaiting/with a worker
  -- (those are the durability boundary; a too-eager reap would drop an unprocessed event = the very loss the inbox
  -- prevents). A 'failed' row is the dead-letter signal the watchdog also alerts on; reaping it past the window
  -- bounds the table while the alert + the longer-cadence DLQ re-arm catch it first. done_at on a 'done' row,
  -- updated_at on a 'failed' row (which has no done_at) = the terminal timestamp. Content-free (no payload — it is
  -- already '{}' on done; a failed row's payload is the minimized shape, dropped with the row). veripsa_app-only.
  IF p_after IS NULL THEN
    DELETE FROM core.webhook_delivery d
     WHERE d.status IN ('done','failed')
       AND COALESCE(d.done_at, d.updated_at) < p_before
       -- A completed sibling is part of the exact all-or-zero proof for a
       -- still-unfinished operator recovery batch. Preserve it while any
       -- queued/processing/failed member with the same opaque batch identity
       -- remains; otherwise DR or retention could make a safe continuation
       -- permanently unverifiable.
       AND NOT (
         d.status='done' AND d.event_type<>'erased' AND d.operator_recovery_count=1
         AND EXISTS (
           SELECT 1 FROM core.webhook_delivery sibling
            WHERE sibling.delivery_key<>d.delivery_key
              AND sibling.operator_recovery_count=1
              AND sibling.operator_recovery_batch_token=
                  d.operator_recovery_batch_token
              AND sibling.operator_recovery_id=
                  d.operator_recovery_id
              AND sibling.operator_recovery_batch_size=
                  d.operator_recovery_batch_size
              AND sibling.status IN ('queued','processing','failed')
         )
       );
    GET DIAGNOSTICS v_webhooks = ROW_COUNT;
  END IF;
  -- done = did this call reach the END of the tenant list? A NULL chunk cap (the full single-sweep) is always
  -- done. A bounded chunk is done when it processed FEWER than the cap (no full chunk remained) — when it
  -- processed exactly the cap there MAY be more, so done=false and the caller pages once more from v_last (which,
  -- if nothing remains, processes 0 and returns done=true). Gap-free: at most one extra empty call to confirm end.
  v_done := (p_max_accounts IS NULL) OR (v_accounts < p_max_accounts);
  RETURN jsonb_build_object('ok', true, 'accounts', v_accounts, 'pruned', v_total,
                            'dead_claims_pruned', v_claims_total,
                            'spent_predictions_pruned', v_preds_total,
                            'cold_repos_pruned', v_cold_repos,
                            'cold_graph_rows_pruned', jsonb_build_object(
                              'nodes', v_cold_nodes,
                              'edges', v_cold_edges,
                              'versions', v_cold_versions,
                              'cochange', v_cold_cochange,
                              'cochange_seen', v_cold_cc_seen),
                            -- durable-inbox reaper: terminal (done/failed) webhook rows past the window (audit P1).
                            'webhook_deliveries_reaped', v_webhooks,
                            -- pagination cursor + completion flag (NULL/absent when an unbounded one-shot sweep).
                            'next_after', v_last, 'done', v_done,
                            'before', p_before, 'kinds', to_jsonb(v_kinds));
END $$;
ALTER FUNCTION core.prune_all_accounts_with_authority(timestamptz, text[], int, text) OWNER TO veripsa_migrator;

-- export_durable_rows_with_authority: DISASTER-RECOVERY export of the catastrophic-to-lose rows ACROSS ALL
-- TENANTS. The append-only event ledger + the curated statement stream ARE the durable product (records-not-
-- correctness); lifecycle-generation fences and accepted-but-unfinished durable deliveries are equally
-- catastrophic to lose. The live graph/claims are rebuildable from GitHub; these rows are NOT. Render's managed Postgres
-- has backups, but a host-OWNED, off-Render copy + a PROVEN restore is the real DR plan (a backup nobody
-- restored is a hope). Like the retention sweep, this is an OWNER-CONTEXT cross-tenant read: every per-account
-- table (incl. core.account) wears FORCE RLS that hides all rows when no account is pinned, so a client CANNOT
-- dump cross-account from outside — the moat working as designed. So this SECURITY DEFINER fn enumerates
-- tenants via the no-FORCE identity registries (installation_account for GitHub tenants, credential for local/API
-- tenants),
-- pins each account in turn, and returns its content-free durable rows. Read-only (SELECTs only; never writes,
-- so no forgery token is armed — it cannot tamper). Content-free BY SCHEMA (paths/branches/logins/labels;
-- never code/bodies). Returns a SETOF jsonb, one object per row, tagged with its _table. The dedicated
-- veripsa_backup role is the only runtime principal allowed to call it: the live App identity must not be able to
-- dump every tenant. p_account '' = every tenant.
--
-- SELF-SUFFICIENT for restore: it ALSO emits the TENANT-REGISTRY rows (account, agent, credential, and
-- installation_account)
-- the durable rows depend on — without them a restore would re-lay event/statement rows that are ORPHANED
-- (event.account_id has no account; and the DR gate enumerates via installation_account, so un-restored that
-- map would make the restored rows undiscoverable). Those two are content-free (org display name, plan, the
-- public installation id). The registry rows are emitted FIRST so a restore creates the parent (account) and
-- the discovery map before the child rows that FK / RLS-depend on them.
-- Cross-tenant lifecycle fences and durable inbox rows are emitted independently and first for the global export.
-- An uninstalled account may have no live registry row; accepted unfinished work must survive even when its tenant
-- registry is temporarily absent. Scoped exports omit those global safety sets.
CREATE OR REPLACE FUNCTION core.export_durable_rows_with_authority(p_account text DEFAULT '')
    RETURNS SETOF jsonb LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path TO 'core','pg_catalog' AS $$
DECLARE v_acct text;
BEGIN
  IF p_account = '' THEN
    RETURN QUERY
      SELECT to_jsonb(t) || jsonb_build_object('_table','account_lifecycle_tombstone')
        FROM core.account_lifecycle_tombstone t
       ORDER BY t.account_id;
    RETURN QUERY
      SELECT to_jsonb(d)
             || jsonb_build_object(
                  '_table','webhook_delivery',
                  'payload', d.payload
                    - '_veripsa_offboard_completed'
                    - '_veripsa_transfer_completed'
                    - '_veripsa_fanout_plan'
                    - '_veripsa_fanout_completed',
                  'last_error', NULL)
        FROM core.webhook_delivery d
       WHERE d.event_type='erased'
          OR d.status IN ('queued','processing','failed')
          OR (
            d.status='done' AND d.event_type<>'erased'
            AND d.operator_recovery_count=1
            AND EXISTS (
              SELECT 1 FROM core.webhook_delivery sibling
               WHERE sibling.delivery_key<>d.delivery_key
                 AND sibling.operator_recovery_count=1
                 AND sibling.operator_recovery_batch_token=
                     d.operator_recovery_batch_token
                 AND sibling.operator_recovery_id=d.operator_recovery_id
                 AND sibling.operator_recovery_batch_size=
                     d.operator_recovery_batch_size
                 AND sibling.status IN ('queued','processing','failed')
            )
          )
       ORDER BY d.received_at,d.delivery_key;
  END IF;
  FOR v_acct IN
    SELECT identities.account_id
      FROM (
        -- GitHub tenants route through installation_account. Local/API tenants may have no installation at all;
        -- credential is ENABLE-not-FORCE precisely so the owner can resolve those identities before an RLS pin.
        SELECT account_id FROM core.installation_account
        UNION
        SELECT account_id FROM core.credential
      ) identities
     WHERE p_account = '' OR identities.account_id = p_account
     ORDER BY account_id
  LOOP
    PERFORM set_config('core.current_account', v_acct, true);   -- pin RLS to this account; FORCE RLS then admits exactly its rows
    -- tenant registry first (parent + discovery map), so a restore can re-create them before the child rows.
    RETURN QUERY
      SELECT to_jsonb(a) || jsonb_build_object('_table','account')
        FROM core.account a WHERE a.account_id = v_acct;
    RETURN QUERY
      SELECT to_jsonb(ag) || jsonb_build_object('_table','agent')
        FROM core.agent ag WHERE ag.account_id = v_acct;
    RETURN QUERY
      SELECT to_jsonb(c) || jsonb_build_object('_table','credential')
        FROM core.credential c WHERE c.account_id = v_acct;
    RETURN QUERY
      SELECT to_jsonb(ia) || jsonb_build_object('_table','installation_account')
        FROM core.installation_account ia WHERE ia.account_id = v_acct;
    RETURN QUERY
      SELECT to_jsonb(rt) || jsonb_build_object('_table','repository_lifecycle_tombstone')
        FROM core.repository_lifecycle_tombstone rt
       WHERE rt.account_id = v_acct
       ORDER BY rt.repository_id,rt.repo;
    RETURN QUERY
      SELECT to_jsonb(ra) || jsonb_build_object('_table','repository_lifecycle_activation')
        FROM core.repository_lifecycle_activation ra
       WHERE ra.account_id = v_acct
       ORDER BY ra.repository_id;
    RETURN QUERY
      SELECT to_jsonb(pe) || jsonb_build_object('_table','account_plan_event')
        FROM core.account_plan_event pe
       WHERE pe.account_id = v_acct;
    RETURN QUERY
      SELECT to_jsonb(e) || jsonb_build_object('_table','event')
        FROM core.event e WHERE e.account_id = v_acct;
    RETURN QUERY
      SELECT to_jsonb(s) || jsonb_build_object('_table','statement')
        FROM core.statement s WHERE s.account_id = v_acct;
  END LOOP;
  RETURN;
END $$;
ALTER FUNCTION core.export_durable_rows_with_authority(text) OWNER TO veripsa_migrator;

-- ── GRANTs: the lifecycle/retention/DR fns are App-delegation-only (veripsa_app). ────────────────────
REVOKE EXECUTE ON FUNCTION core.purge_account_working_set_with_authority() FROM PUBLIC, veripsa_writer;
GRANT  EXECUTE ON FUNCTION core.purge_account_working_set_with_authority() TO veripsa_app;  -- the App's uninstall handler purges account-wide (delegation-only, like purge_repo)
REVOKE EXECUTE ON FUNCTION core.purge_account_working_set_with_authority(jsonb)
  FROM PUBLIC,veripsa_writer;
GRANT EXECUTE ON FUNCTION core.purge_account_working_set_with_authority(jsonb)
  TO veripsa_app;
REVOKE EXECUTE ON FUNCTION core.purge_repo_with_authority(text)
  FROM PUBLIC, veripsa_writer;
GRANT EXECUTE ON FUNCTION core.purge_repo_with_authority(text) TO veripsa_app;  -- rollback-safe shim: authenticates the unique processing deletion, then delegates to /4
REVOKE EXECUTE ON FUNCTION core.resolve_legacy_repository_offboard_with_authority(text,text,text,text)
  FROM PUBLIC, veripsa_writer;
GRANT EXECUTE ON FUNCTION core.resolve_legacy_repository_offboard_with_authority(text,text,text,text)
  TO veripsa_app;
REVOKE EXECUTE ON FUNCTION core.offboard_repository_with_authority(text,text,text)
  FROM PUBLIC, veripsa_writer, veripsa_app;
REVOKE EXECUTE ON FUNCTION core.erase_account_with_authority() FROM PUBLIC, veripsa_writer;  -- RIGHT-TO-DELETION: full account hard-delete; App-delegation only (account from the connection identity, never a victim arg)
REVOKE EXECUTE ON FUNCTION core.rename_repo_coordinate_with_authority(text,text) FROM PUBLIC, veripsa_writer;
REVOKE EXECUTE ON FUNCTION core.reconcile_repo_identity_with_authority(text,text) FROM PUBLIC, veripsa_writer;  -- RENAME-DETECT + MIGRATE: stamp the stable repo_id + migrate an orphaned coordinate old→new; App-delegation only (a buyer writer must not re-key another tenant's coordinate)
REVOKE EXECUTE ON FUNCTION core._migrate_repo_coordinate(text,text,text) FROM PUBLIC, veripsa_writer;  -- INTERNAL dedup-aware mover (called only by the two rename/reconcile entry points); never a tenant-callable surface
REVOKE EXECUTE ON FUNCTION core.transfer_repo_coordinate_with_authority(text,text)
  FROM PUBLIC,veripsa_writer,veripsa_app;
REVOKE EXECUTE ON FUNCTION core.transfer_repo_coordinate_with_authority(text,text,text,text)
  FROM PUBLIC,veripsa_writer,veripsa_app;
REVOKE EXECUTE ON FUNCTION core.transfer_repo_coordinate_with_authority(text,text,text,text,text,text,text,text)
  FROM PUBLIC,veripsa_writer;
REVOKE EXECUTE ON FUNCTION core.release_repo_claims_with_authority(text) FROM PUBLIC, veripsa_writer;  -- repo ARCHIVE: release ALL in-flight lanes repo-wide; App-delegation only (a buyer writer must not free another's lanes)
REVOKE EXECUTE ON FUNCTION core.release_account_claims_with_authority() FROM PUBLIC, veripsa_writer;  -- install SUSPEND: release ALL in-flight lanes account-wide (graph kept); App-delegation only (account from the connection identity, never a victim arg)
REVOKE EXECUTE ON FUNCTION core.release_account_claims_with_authority(text)
  FROM PUBLIC,veripsa_writer;
REVOKE EXECUTE ON FUNCTION core.release_account_claims_with_authority(text,jsonb)
  FROM PUBLIC,veripsa_writer;
GRANT EXECUTE ON FUNCTION core.erase_account_with_authority() TO veripsa_app;  -- RIGHT-TO-DELETION: the host runs the full account hard-delete on an offboarding/erasure request (App-delegation only)
GRANT EXECUTE ON FUNCTION core.rename_repo_coordinate_with_authority(text,text) TO veripsa_app;  -- repo RENAME: re-point the content-free working set old→new (App-delegation only; ledger keeps historical name)
GRANT EXECUTE ON FUNCTION core.reconcile_repo_identity_with_authority(text,text) TO veripsa_app;  -- RENAME-DETECT + MIGRATE: the live push path stamps repo_id + migrates an orphaned coordinate (App-delegation only; the orphan-self-heal fix)
GRANT EXECUTE ON FUNCTION core.transfer_repo_coordinate_with_authority(text,text,text,text,text,text,text,text)
  TO veripsa_app;  -- exact durable payload + scoped live GitHub identity proof; only destructive transfer surface
GRANT EXECUTE ON FUNCTION core.release_repo_claims_with_authority(text) TO veripsa_app;  -- repo ARCHIVE: release ALL in-flight lanes repo-wide (archived repos land nothing; App-delegation only)
GRANT EXECUTE ON FUNCTION core.release_account_claims_with_authority() TO veripsa_app;  -- install SUSPEND: release ALL in-flight lanes account-wide, keeping the graph (a suspended install lands nothing; unsuspend re-onboards; App-delegation only)
GRANT EXECUTE ON FUNCTION core.release_account_claims_with_authority(text) TO veripsa_app;
GRANT EXECUTE ON FUNCTION core.release_account_claims_with_authority(text,jsonb) TO veripsa_app;
REVOKE EXECUTE ON FUNCTION core.prune_events_with_authority(timestamptz,text[]) FROM PUBLIC;  -- strip the PUBLIC default; only the granted App/steward below
GRANT EXECUTE ON FUNCTION core.prune_events_with_authority(timestamptz,text[]) TO veripsa_app, veripsa_demo_steward;  -- RETENTION: account-scoped prune of operational telemetry past the window (curated records stay immutable)
REVOKE EXECUTE ON FUNCTION core.prune_all_accounts_with_authority(timestamptz,text[],int,text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION core.prune_all_accounts_with_authority(timestamptz,text[],int,text) TO veripsa_app;  -- RETENTION SWEEP: the host's scheduled cross-tenant prune, BOUNDED-BATCH (per-account token-pinned; the cron job calls only this)
REVOKE EXECUTE ON FUNCTION core.export_durable_rows_with_authority(text)
  FROM PUBLIC,veripsa_app;
-- Production predeploy intentionally applies schema.sql only; it does not run the cluster-global roles bootstrap.
-- A cluster upgraded from origin/main may therefore not have the new dedicated role yet.  Keep schema apply
-- additive and fail-safe: the export stays unreachable (PUBLIC/App were revoked above) until an operator creates
-- veripsa_backup and reapplies schema.sql.  Never fall back to App or reader privileges.
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='veripsa_backup') THEN
    EXECUTE 'GRANT USAGE ON SCHEMA core TO veripsa_backup';
    EXECUTE 'GRANT EXECUTE ON FUNCTION core.export_durable_rows_with_authority(text) TO veripsa_backup';
  END IF;
END $$;
