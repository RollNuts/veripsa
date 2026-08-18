-- Roll back only the constraint promotion from 001_repository_id_canonical.up.sql.
-- No tenant row was changed by the up migration, so this restores the exact earlier acceptance domain.
\set ON_ERROR_STOP on
BEGIN;
SET LOCAL lock_timeout='10s';
SET LOCAL statement_timeout='5min';

ALTER TABLE core.graph_version DROP CONSTRAINT IF EXISTS graph_version_repo_id_shape;
ALTER TABLE core.graph_version ADD CONSTRAINT graph_version_repo_id_shape CHECK (
  repo_id IS NULL OR (length(repo_id) BETWEEN 1 AND 32 AND repo_id ~ '^[0-9]+$'));
ALTER TABLE core.repository_lifecycle_tombstone
  DROP CONSTRAINT IF EXISTS repository_tombstone_id_shape;
ALTER TABLE core.repository_lifecycle_tombstone ADD CONSTRAINT repository_tombstone_id_shape CHECK (
  repository_id='unknown' OR
  (length(repository_id) BETWEEN 1 AND 32 AND repository_id ~ '^[0-9]+$'));
ALTER TABLE core.repository_lifecycle_activation
  DROP CONSTRAINT IF EXISTS repository_activation_id_shape;
ALTER TABLE core.repository_lifecycle_activation ADD CONSTRAINT repository_activation_id_shape CHECK (
  length(repository_id) BETWEEN 1 AND 32 AND repository_id ~ '^[0-9]+$');
COMMIT;
