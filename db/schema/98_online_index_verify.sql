-- ONLINE INDEX PUBLICATION GUARD.
--
-- CREATE INDEX CONCURRENTLY cannot run in a transaction and can leave a
-- name-visible invalid shell if interrupted. IF NOT EXISTS also checks identity
-- by name, not by table/validity. Before the final schema marker is published,
-- prove every index registered by 05_online_index_repair.sql is attached to the
-- expected core table, has the expected uniqueness class and full deparsed
-- definition (method/keys/order/opclass/predicate), and is ready+valid.
DO $$
DECLARE v_bad text[];
BEGIN
  -- pg_get_indexdef qualifies referenced core functions according to the
  -- caller's search_path. Pin it so fingerprints are stable regardless of a
  -- prior module/session setting.
  PERFORM set_config('search_path','pg_catalog',true);
  SELECT array_agg(m.index_name::text ORDER BY m.index_name::text)
    INTO v_bad
    FROM pg_temp.veripsa_managed_index_contract m
    LEFT JOIN pg_namespace n
      ON n.nspname='core'
    LEFT JOIN pg_class c
      ON c.relnamespace=n.oid
     AND c.relname=m.index_name
     AND c.relkind='i'
    LEFT JOIN pg_index i
      ON i.indexrelid=c.oid
    LEFT JOIN pg_class t
      ON t.oid=i.indrelid
     AND t.relname=m.table_name
     AND t.relnamespace=n.oid
   WHERE c.oid IS NULL
      OR i.indexrelid IS NULL
      OR t.oid IS NULL
      OR NOT i.indisvalid
      OR NOT i.indisready
      OR i.indisunique IS DISTINCT FROM m.is_unique
      OR md5(substring(pg_get_indexdef(i.indexrelid) FROM 'USING .*$'))
         IS DISTINCT FROM m.definition_md5;

  IF v_bad IS NOT NULL THEN
    RAISE EXCEPTION
      'managed online index contract failed: %',
      array_to_string(v_bad, ',')
      USING ERRCODE='55000';
  END IF;
END $$;
