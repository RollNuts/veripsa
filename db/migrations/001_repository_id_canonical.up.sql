-- Promote stored GitHub repository ids to the positive canonical ASCII-decimal contract.
--
-- OWNER-ONLY, MANUAL, QUIET WINDOW:
--   pg_dump/Render backup first, then:
--   psql "$OWNER_DSN" -X -v ON_ERROR_STOP=1 -f db/migrations/001_repository_id_canonical.up.sql
--   psql "$OWNER_DSN" -X -v ON_ERROR_STOP=1 -f db/migrations/001_repository_id_canonical.verify.sql
--
-- This migration never rewrites tenant rows. If historical noncanonical values exist, it aborts with aggregate
-- counts and leaves every constraint/RLS setting unchanged. Runtime guards quarantine those coordinates until an
-- authenticated repository add/create resets them; investigate before retrying this constraint-only promotion.
\set ON_ERROR_STOP on
BEGIN;
SET LOCAL lock_timeout='10s';
SET LOCAL statement_timeout='5min';

DO $migration$
DECLARE
  v_graph_invalid bigint; v_tombstone_invalid bigint; v_activation_invalid bigint;
  v_graph_forced boolean; v_activation_forced boolean;
BEGIN
  SELECT relforcerowsecurity INTO v_graph_forced
    FROM pg_class WHERE oid='core.graph_version'::regclass;
  SELECT relforcerowsecurity INTO v_activation_forced
    FROM pg_class WHERE oid='core.repository_lifecycle_activation'::regclass;
  IF v_graph_forced THEN ALTER TABLE core.graph_version NO FORCE ROW LEVEL SECURITY; END IF;
  IF v_activation_forced THEN
    ALTER TABLE core.repository_lifecycle_activation NO FORCE ROW LEVEL SECURITY;
  END IF;

  SELECT count(*) INTO v_graph_invalid FROM core.graph_version
   WHERE repo_id IS NOT NULL AND (length(repo_id)>32 OR repo_id !~ '^[1-9][0-9]*$');
  SELECT count(*) INTO v_tombstone_invalid FROM core.repository_lifecycle_tombstone
   WHERE repository_id<>'unknown'
     AND (length(repository_id)>32 OR repository_id !~ '^[1-9][0-9]*$');
  SELECT count(*) INTO v_activation_invalid FROM core.repository_lifecycle_activation
   WHERE length(repository_id)>32 OR repository_id !~ '^[1-9][0-9]*$';
  IF v_graph_invalid<>0 OR v_tombstone_invalid<>0 OR v_activation_invalid<>0 THEN
    RAISE EXCEPTION 'canonical repository-id migration refused: historical noncanonical rows remain'
      USING ERRCODE='23514',
            DETAIL=format('graph_version=%s tombstone=%s activation=%s',
                          v_graph_invalid,v_tombstone_invalid,v_activation_invalid);
  END IF;

  ALTER TABLE core.graph_version DROP CONSTRAINT IF EXISTS graph_version_repo_id_shape;
  ALTER TABLE core.graph_version ADD CONSTRAINT graph_version_repo_id_shape CHECK (
    repo_id IS NULL OR (length(repo_id) BETWEEN 1 AND 32 AND repo_id ~ '^[1-9][0-9]*$'));
  ALTER TABLE core.repository_lifecycle_tombstone
    DROP CONSTRAINT IF EXISTS repository_tombstone_id_shape;
  ALTER TABLE core.repository_lifecycle_tombstone ADD CONSTRAINT repository_tombstone_id_shape CHECK (
    repository_id='unknown' OR
    (length(repository_id) BETWEEN 1 AND 32 AND repository_id ~ '^[1-9][0-9]*$'));
  ALTER TABLE core.repository_lifecycle_activation
    DROP CONSTRAINT IF EXISTS repository_activation_id_shape;
  ALTER TABLE core.repository_lifecycle_activation ADD CONSTRAINT repository_activation_id_shape CHECK (
    length(repository_id) BETWEEN 1 AND 32 AND repository_id ~ '^[1-9][0-9]*$');

  IF v_graph_forced THEN ALTER TABLE core.graph_version FORCE ROW LEVEL SECURITY; END IF;
  IF v_activation_forced THEN
    ALTER TABLE core.repository_lifecycle_activation FORCE ROW LEVEL SECURITY;
  END IF;
END
$migration$;
COMMIT;
