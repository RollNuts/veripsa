-- Post-verify 001_repository_id_canonical.up.sql without exposing tenant coordinates or repository ids.
\set ON_ERROR_STOP on
BEGIN;
SET LOCAL lock_timeout='10s';
SET LOCAL statement_timeout='5min';

DO $verify$
DECLARE
  v_invalid bigint; v_shape text; v_validated boolean;
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

  SELECT (SELECT count(*) FROM core.graph_version
           WHERE repo_id IS NOT NULL AND (length(repo_id)>32 OR repo_id !~ '^[1-9][0-9]*$'))
       + (SELECT count(*) FROM core.repository_lifecycle_tombstone
           WHERE repository_id<>'unknown'
             AND (length(repository_id)>32 OR repository_id !~ '^[1-9][0-9]*$'))
       + (SELECT count(*) FROM core.repository_lifecycle_activation
           WHERE length(repository_id)>32 OR repository_id !~ '^[1-9][0-9]*$')
    INTO v_invalid;
  IF v_invalid<>0 THEN
    RAISE EXCEPTION 'canonical repository-id verification failed: % noncanonical rows remain',v_invalid
      USING ERRCODE='23514';
  END IF;

  FOR v_shape,v_validated IN
    SELECT pg_get_constraintdef(oid),convalidated FROM pg_constraint
     WHERE conname IN ('graph_version_repo_id_shape','repository_tombstone_id_shape',
                       'repository_activation_id_shape')
  LOOP
    IF NOT v_validated OR position('^[1-9][0-9]*$' IN v_shape)=0 THEN
      RAISE EXCEPTION 'canonical repository-id verification failed: constraint drift'
        USING ERRCODE='23514';
    END IF;
  END LOOP;
  IF (SELECT count(*) FROM pg_constraint
       WHERE conname IN ('graph_version_repo_id_shape','repository_tombstone_id_shape',
                         'repository_activation_id_shape'))<>3 THEN
    RAISE EXCEPTION 'canonical repository-id verification failed: expected three constraints'
      USING ERRCODE='23514';
  END IF;

  IF v_graph_forced THEN ALTER TABLE core.graph_version FORCE ROW LEVEL SECURITY; END IF;
  IF v_activation_forced THEN
    ALTER TABLE core.repository_lifecycle_activation FORCE ROW LEVEL SECURITY;
  END IF;
END
$verify$;
COMMIT;
