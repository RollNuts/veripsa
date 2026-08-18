-- FINAL SCHEMA CUTOVER
--
-- The ordinary Render pre-deploy path reaches this module while the previous
-- web image is still the only proven drainer. It therefore stamps only the
-- exact content-free schema manifest after every fallible expansion module
-- succeeds; the audited predecessor claim ABIs remain callable.
--
-- A separate, explicit one-off job replays this final module with
-- veripsa_schema_contract_cutover=worker-ready-v1 only AFTER the exact target
-- worker and web image have both been promoted, smoked, and re-verified. That
-- job passes the already-live exact manifest marker. The legacy claim fences,
-- direct graph-writer revocations, and same-value marker stamp then share this
-- one PostgreSQL transaction: an error publishes none of them.
--
-- Do not move these fences into an earlier module or make them implicit in a
-- generic schema apply. A worker boot/readiness failure must leave the old web
-- image able to drain, not merely able to accept more ingress.

BEGIN;

-- Function comments are the durable contract bit. CREATE OR REPLACE preserves
-- the function OID/comment, so every later full schema replay preserves or
-- restores the fences as expansion modules revisit those surfaces. A fresh or
-- not-yet-cut-over database has all five comments NULL. Any mixed or foreign
-- comment set is an unknown publication and fails before mutation.
WITH markers AS (
  SELECT
    obj_description(to_regprocedure(
      'core.claim_webhook_delivery_with_authority(text,integer,integer,integer)'),
      'pg_proc') AS webhook,
    obj_description(to_regprocedure(
      'core.claim_policy_refresh_with_authority(text,integer,integer,integer)'),
      'pg_proc') AS policy4,
    obj_description(to_regprocedure(
      'core.claim_policy_refresh_with_authority(text,integer,integer,integer,boolean)'),
      'pg_proc') AS policy5,
    obj_description(to_regprocedure(
      'core.ingest_graph_with_authority(jsonb,text,text,text,timestamptz)'),
      'pg_proc') AS graph_full,
    obj_description(to_regprocedure(
      'core.patch_graph_with_authority(jsonb,text,text,text[],text[],text,timestamptz)'),
      'pg_proc') AS graph_patch
)
SELECT
  COALESCE((
    webhook='veripsa-legacy-webhook-claim/v1/fenced'
    AND policy4='veripsa-legacy-policy-claim/v1/fenced'
    AND policy5='veripsa-legacy-policy-claim/v1/fenced'
    AND graph_full='veripsa-graph-direct/v1/fenced'
    AND graph_patch='veripsa-graph-direct/v1/fenced'
  ),false) AS veripsa_contract_cutover_execute,
  NOT (
    (
      webhook IS NULL AND policy4 IS NULL AND policy5 IS NULL
      AND graph_full IS NULL AND graph_patch IS NULL
    )
    OR
    (
      webhook='veripsa-legacy-webhook-claim/v1/fenced'
      AND policy4='veripsa-legacy-policy-claim/v1/fenced'
      AND policy5='veripsa-legacy-policy-claim/v1/fenced'
      AND graph_full='veripsa-graph-direct/v1/fenced'
      AND graph_patch='veripsa-graph-direct/v1/fenced'
    )
  ) AS veripsa_contract_cutover_marker_unknown
FROM markers
\gset
\if :veripsa_contract_cutover_marker_unknown
SELECT
  'unknown or partial legacy cutover marker set; refusing schema publication'
  ::integer;
\endif

-- The production manifest supplies this psql variable. An explicit first
-- cutover requires both it and the exact confirmation; direct/manual schema
-- applies with no prior cutover leave the production contract untouched.
\if :{?veripsa_schema_contract_cutover}
\if :{?veripsa_schema_marker}
SELECT :'veripsa_schema_contract_cutover' = 'worker-ready-v1'
       AS veripsa_contract_cutover_authorized
\gset
\if :veripsa_contract_cutover_authorized
SELECT true AS veripsa_contract_cutover_execute
\gset
\else
SELECT
  'invalid veripsa_schema_contract_cutover confirmation; refusing cutover'
  ::integer;
\endif
\else
SELECT
  'veripsa_schema_contract_cutover requires the exact live manifest marker'
  ::integer;
\endif
\endif

\if :veripsa_contract_cutover_execute

-- Re-check the complete legacy-claim state inside the mutating transaction.
-- The one-off finalizer performs the same catalog classification while holding
-- the schema-manifest advisory lock; this backstop closes the interval between
-- that read and this transaction against an uncooperative/out-of-band DDL
-- writer. Accept either the exact operational predecessor state (the fresh-DB
-- webhook /4 is already safe) or the exact fully-cut-over idempotent state.
-- Every mixed/unknown state raises before any CREATE/REVOKE.
WITH webhook AS (
  SELECT CASE
           WHEN md5(p.prosrc)='8bdf79d0259b27c37f48a9f52c0416b6'
                AND l.lanname='plpgsql' THEN 'operational'
           WHEN md5(p.prosrc)='877a1f799a016bcd42a7a57b61750c25'
                AND l.lanname='sql' THEN 'safe'
           ELSE 'unknown'
         END AS state,
         obj_description(p.oid,'pg_proc') AS marker
    FROM pg_proc p
    JOIN pg_language l ON l.oid=p.prolang
    JOIN pg_roles r ON r.oid=p.proowner
   WHERE p.oid=to_regprocedure(
           'core.claim_webhook_delivery_with_authority(text,integer,integer,integer)')
     AND p.prosecdef
     AND p.proconfig=ARRAY['search_path=core, pg_catalog']::text[]
     AND r.rolname='veripsa_migrator'
     AND p.prorettype='jsonb'::regtype
     AND NOT p.proretset
     AND NOT p.proisstrict
     AND p.provolatile='v'
     AND p.proparallel='u'
     AND NOT p.proleakproof
     AND p.prokind='f'
     AND p.proargmodes IS NULL
     AND p.proallargtypes IS NULL
     AND p.provariadic=0
     AND p.pronargdefaults=0
     AND p.proargnames=ARRAY[
           'p_key','p_stale_seconds','p_max_attempts','p_protocol'
         ]::text[]
     AND p.procost=100
     AND p.prorows=0
     AND p.prosupport=0
     AND p.probin IS NULL
), policy4 AS (
  SELECT CASE
           WHEN md5(p.prosrc)='e4f049d3a611657dc301b3ed3bb65238'
             THEN 'operational'
           WHEN p.prosrc='SELECT NULL::jsonb' THEN 'safe'
           ELSE 'unknown'
         END AS state,
         obj_description(p.oid,'pg_proc') AS marker
    FROM pg_proc p
    JOIN pg_language l ON l.oid=p.prolang
    JOIN pg_roles r ON r.oid=p.proowner
   WHERE p.oid=to_regprocedure(
           'core.claim_policy_refresh_with_authority(text,integer,integer,integer)')
     AND l.lanname='sql'
     AND p.prosecdef
     AND p.proconfig=ARRAY['search_path=core, pg_catalog']::text[]
     AND r.rolname='veripsa_migrator'
     AND p.prorettype='jsonb'::regtype
     AND NOT p.proretset
     AND NOT p.proisstrict
     AND p.provolatile='v'
     AND p.proparallel='u'
     AND NOT p.proleakproof
     AND p.prokind='f'
     AND p.proargmodes IS NULL
     AND p.proallargtypes IS NULL
     AND p.provariadic=0
     AND p.pronargdefaults=0
     AND p.proargnames=ARRAY[
           'p_worker','p_max_attempts','p_stale_seconds','p_scan_cap'
         ]::text[]
     AND p.procost=100
     AND p.prorows=0
     AND p.prosupport=0
     AND p.probin IS NULL
), policy5 AS (
  SELECT CASE
           WHEN md5(p.prosrc)='27f6397f7200b51cafe4134b8ff48fbf'
             THEN 'operational'
           WHEN p.prosrc='SELECT NULL::jsonb' THEN 'safe'
           ELSE 'unknown'
         END AS state,
         obj_description(p.oid,'pg_proc') AS marker
    FROM pg_proc p
    JOIN pg_language l ON l.oid=p.prolang
    JOIN pg_roles r ON r.oid=p.proowner
   WHERE p.oid=to_regprocedure(
           'core.claim_policy_refresh_with_authority(text,integer,integer,integer,boolean)')
     AND l.lanname='sql'
     AND p.prosecdef
     AND p.proconfig=ARRAY['search_path=core, pg_catalog']::text[]
     AND r.rolname='veripsa_migrator'
     AND p.prorettype='jsonb'::regtype
     AND NOT p.proretset
     AND NOT p.proisstrict
     AND p.provolatile='v'
     AND p.proparallel='u'
     AND NOT p.proleakproof
     AND p.prokind='f'
     AND p.proargmodes IS NULL
     AND p.proallargtypes IS NULL
     AND p.provariadic=0
     AND p.pronargdefaults=0
     AND p.proargnames=ARRAY[
           'p_worker','p_max_attempts','p_stale_seconds','p_scan_cap',
           'p_support_graph'
         ]::text[]
     AND p.procost=100
     AND p.prorows=0
     AND p.prosupport=0
     AND p.probin IS NULL
), state AS (
  SELECT
    COALESCE((SELECT state FROM webhook),'unknown') AS webhook,
    (SELECT marker FROM webhook) AS webhook_marker,
    COALESCE((SELECT state FROM policy4),'unknown') AS policy4,
    (SELECT marker FROM policy4) AS policy4_marker,
    COALESCE((SELECT state FROM policy5),'unknown') AS policy5,
    (SELECT marker FROM policy5) AS policy5_marker,
    obj_description(to_regprocedure(
      'core.ingest_graph_with_authority(jsonb,text,text,text,timestamptz)'),
      'pg_proc') AS full_marker,
    obj_description(to_regprocedure(
      'core.patch_graph_with_authority(jsonb,text,text,text[],text[],text,timestamptz)'),
      'pg_proc') AS patch_marker,
    has_function_privilege(
      'veripsa_writer',
      'core.ingest_graph_with_authority(jsonb,text,text,text,timestamptz)',
      'EXECUTE') AS full_writer,
    has_function_privilege(
      'veripsa_app',
      'core.ingest_graph_with_authority(jsonb,text,text,text,timestamptz)',
      'EXECUTE') AS full_app,
    has_function_privilege(
      'veripsa_writer',
      'core.patch_graph_with_authority(jsonb,text,text,text[],text[],text,timestamptz)',
      'EXECUTE') AS patch_writer,
    has_function_privilege(
      'veripsa_app',
      'core.patch_graph_with_authority(jsonb,text,text,text[],text[],text,timestamptz)',
      'EXECUTE') AS patch_app
)
SELECT NOT COALESCE(
  (
    webhook IN ('operational','safe')
    AND policy4='operational'
    AND policy5='operational'
    AND full_writer AND full_app AND patch_writer AND patch_app
  )
  OR
  (
    webhook='safe'
    AND policy4='safe'
    AND policy5='safe'
    AND NOT full_writer AND NOT full_app
    AND NOT patch_writer AND NOT patch_app
  )
  OR
  (
    -- A later/previously interrupted full schema replay preserves the durable
    -- comments, module 30 keeps graph ACLs revoked, and module 97 temporarily
    -- republishes only its exact audited operational policy wrappers. This is
    -- the sole accepted mixed state; final module 100 closes it again.
    webhook='safe'
    AND webhook_marker='veripsa-legacy-webhook-claim/v1/fenced'
    AND policy4='operational'
    AND policy4_marker='veripsa-legacy-policy-claim/v1/fenced'
    AND policy5='operational'
    AND policy5_marker='veripsa-legacy-policy-claim/v1/fenced'
    AND full_marker='veripsa-graph-direct/v1/fenced'
    AND patch_marker='veripsa-graph-direct/v1/fenced'
    AND NOT full_writer AND NOT full_app
    AND NOT patch_writer AND NOT patch_app
  ),
  false
) AS veripsa_contract_cutover_catalog_unknown
FROM state
\gset
\if :veripsa_contract_cutover_catalog_unknown
SELECT
  'unsupported legacy claim/graph-writer contract state; refusing production cutover'
  ::integer;
\endif

-- Old production web calls /4. Route it through the current /5 -> /6 wrapper
-- with a NULL owner nonce; a fresh due row therefore remains queued with
-- reason=legacy_budget_unproven. Exact pre-cutover leases retain their terminal
-- generation and are deliberately not touched here.
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
ALTER FUNCTION core.claim_webhook_delivery_with_authority(text,int,int,int)
  OWNER TO veripsa_migrator;
REVOKE EXECUTE ON FUNCTION
  core.claim_webhook_delivery_with_authority(text,int,int,int)
  FROM PUBLIC,veripsa_writer;
GRANT EXECUTE ON FUNCTION
  core.claim_webhook_delivery_with_authority(text,int,int,int)
  TO veripsa_app;

-- Both predecessor policy claim overloads become ingress-only NULL probes.
-- Keep the exact terminal ABIs published by module 97: a turn claimed before
-- this transaction can still finish/fail only its exact policy epoch.
CREATE OR REPLACE FUNCTION core.claim_policy_refresh_with_authority(
    p_worker text,p_max_attempts int,p_stale_seconds int,p_scan_cap int,
    p_support_graph boolean
) RETURNS jsonb
    LANGUAGE sql SECURITY DEFINER SET search_path TO 'core','pg_catalog'
    AS 'SELECT NULL::jsonb';
ALTER FUNCTION core.claim_policy_refresh_with_authority(
  text,int,int,int,boolean) OWNER TO veripsa_migrator;
REVOKE ALL ON FUNCTION core.claim_policy_refresh_with_authority(
  text,int,int,int,boolean) FROM PUBLIC,veripsa_writer;
GRANT EXECUTE ON FUNCTION core.claim_policy_refresh_with_authority(
  text,int,int,int,boolean) TO veripsa_app;

CREATE OR REPLACE FUNCTION core.claim_policy_refresh_with_authority(
    p_worker text,p_max_attempts int,p_stale_seconds int,p_scan_cap int
) RETURNS jsonb
    LANGUAGE sql SECURITY DEFINER SET search_path TO 'core','pg_catalog'
    AS 'SELECT NULL::jsonb';
ALTER FUNCTION core.claim_policy_refresh_with_authority(
  text,int,int,int) OWNER TO veripsa_migrator;
REVOKE ALL ON FUNCTION core.claim_policy_refresh_with_authority(
  text,int,int,int) FROM PUBLIC,veripsa_writer;
GRANT EXECUTE ON FUNCTION core.claim_policy_refresh_with_authority(
  text,int,int,int) TO veripsa_app;

-- CREATE OR REPLACE preserves proacl. A role added by an old migration or an
-- out-of-band GRANT would therefore retain SECURITY DEFINER reach if we only
-- named PUBLIC/veripsa_writer here. Normalize the complete direct ACL for all
-- three retired claim ABIs inside this same cutover transaction: revoke every
-- non-owner grantee (including App, so WITH GRANT OPTION cannot survive), then
-- normalize the actual owner's row to one non-grantable EXECUTE, then publish
-- the sole required legacy-claim App grant without grant option. Include both
-- versionless graph writers in the same normalization; they require no
-- non-owner direct grant after the convergence-lease cutover. The finalizer
-- verifies each complete aclexplode() set, not a hand-picked role list.
--
-- A DO block is deliberately forbidden inside this long publication
-- transaction because the hot-deploy guard cannot prove that its opaque body
-- takes no relation lock. Publish an invoker-rights helper transaction-locally,
-- call it, and drop it before commit instead. CREATE (not OR REPLACE) fails
-- closed if an unexpected helper already exists; the create/call/drop can
-- never become externally visible independently.
CREATE FUNCTION core._normalize_cutover_acl_v1()
RETURNS void
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path TO 'pg_catalog'
AS $veripsa_cutover_acl$
DECLARE
  v_target record;
  v_grantee record;
BEGIN
  FOR v_target IN
    SELECT
      p.oid::regprocedure AS function_identity,
      p.proowner,
      p.proacl,
      owner_role.rolname AS owner_name
      FROM pg_proc p
      JOIN pg_roles owner_role ON owner_role.oid=p.proowner
     WHERE p.oid IN (
       'core.claim_webhook_delivery_with_authority(text,integer,integer,integer)'
         ::regprocedure,
       'core.claim_policy_refresh_with_authority(text,integer,integer,integer)'
         ::regprocedure,
       'core.claim_policy_refresh_with_authority(text,integer,integer,integer,boolean)'
         ::regprocedure,
       'core.ingest_graph_with_authority(jsonb,text,text,text,timestamptz)'
         ::regprocedure,
       'core.patch_graph_with_authority(jsonb,text,text,text[],text[],text,timestamptz)'
         ::regprocedure
     )
  LOOP
    FOR v_grantee IN
      SELECT DISTINCT acl.grantee AS grantee_oid, role_grantee.rolname
        FROM aclexplode(
          COALESCE(v_target.proacl,acldefault('f',v_target.proowner))
        ) acl
        LEFT JOIN pg_roles role_grantee ON role_grantee.oid=acl.grantee
       WHERE acl.grantee <> v_target.proowner
    LOOP
      IF v_grantee.grantee_oid=0 THEN
        EXECUTE format(
          'REVOKE ALL PRIVILEGES ON FUNCTION %s FROM PUBLIC CASCADE',
          v_target.function_identity
        );
      ELSE
        EXECUTE format(
          'REVOKE ALL PRIVILEGES ON FUNCTION %s FROM %I CASCADE',
          v_target.function_identity,
          v_grantee.rolname
        );
      END IF;
    END LOOP;

    -- Ownership itself survives a same-owner ALTER FUNCTION, but its explicit
    -- ACL row can be absent or carry WITH GRANT OPTION. Rebuild it exactly.
    EXECUTE format(
      'REVOKE ALL PRIVILEGES ON FUNCTION %s FROM %I CASCADE',
      v_target.function_identity,
      v_target.owner_name
    );
    EXECUTE format(
      'GRANT EXECUTE ON FUNCTION %s TO %I',
      v_target.function_identity,
      v_target.owner_name
    );
  END LOOP;
END
$veripsa_cutover_acl$;
ALTER FUNCTION core._normalize_cutover_acl_v1()
  OWNER TO veripsa_migrator;
REVOKE ALL PRIVILEGES ON FUNCTION core._normalize_cutover_acl_v1()
  FROM PUBLIC;
SELECT core._normalize_cutover_acl_v1();
DROP FUNCTION core._normalize_cutover_acl_v1();

REVOKE ALL PRIVILEGES ON FUNCTION
  core.claim_webhook_delivery_with_authority(text,int,int,int)
  FROM PUBLIC;
REVOKE ALL PRIVILEGES ON FUNCTION
  core.claim_policy_refresh_with_authority(text,int,int,int)
  FROM PUBLIC;
REVOKE ALL PRIVILEGES ON FUNCTION
  core.claim_policy_refresh_with_authority(text,int,int,int,boolean)
  FROM PUBLIC;
GRANT EXECUTE ON FUNCTION
  core.claim_webhook_delivery_with_authority(text,int,int,int)
  TO veripsa_app;
GRANT EXECUTE ON FUNCTION
  core.claim_policy_refresh_with_authority(text,int,int,int)
  TO veripsa_app;
GRANT EXECUTE ON FUNCTION
  core.claim_policy_refresh_with_authority(text,int,int,int,boolean)
  TO veripsa_app;

-- The release workflow waits at least 95 seconds after target-web promotion
-- (Render's 60s routing grace plus the strict/default 30s old-process shutdown
-- bound) and re-verifies both services before reaching this transaction. Thus
-- no already-admitted predecessor graph call remains; REVOKE fences only
-- future legacy calls. The App inherits veripsa_writer, so remove both paths to
-- the versionless writer. SECURITY DEFINER repository-generation/convergence-
-- lease wrappers remain callable by veripsa_app and execute the generic writer
-- as their migrator owner.
REVOKE EXECUTE ON FUNCTION
  core.ingest_graph_with_authority(jsonb,text,text,text,timestamptz)
  FROM PUBLIC,veripsa_writer,veripsa_app;
REVOKE EXECUTE ON FUNCTION
  core.patch_graph_with_authority(
    jsonb,text,text,text[],text[],text,timestamptz)
  FROM PUBLIC,veripsa_writer,veripsa_app;

COMMENT ON FUNCTION
  core.claim_webhook_delivery_with_authority(text,int,int,int)
  IS 'veripsa-legacy-webhook-claim/v1/fenced';
COMMENT ON FUNCTION
  core.claim_policy_refresh_with_authority(text,int,int,int)
  IS 'veripsa-legacy-policy-claim/v1/fenced';
COMMENT ON FUNCTION
  core.claim_policy_refresh_with_authority(text,int,int,int,boolean)
  IS 'veripsa-legacy-policy-claim/v1/fenced';
COMMENT ON FUNCTION
  core.ingest_graph_with_authority(jsonb,text,text,text,timestamptz)
  IS 'veripsa-graph-direct/v1/fenced';
COMMENT ON FUNCTION
  core.patch_graph_with_authority(
    jsonb,text,text,text[],text[],text,timestamptz)
  IS 'veripsa-graph-direct/v1/fenced';

\endif

\if :{?veripsa_schema_marker}
COMMENT ON SCHEMA core IS :'veripsa_schema_marker';
\endif

COMMIT;
