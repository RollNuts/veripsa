-- ONLINE INDEX RETRY BOUNDARY.
--
-- db/schema.sql is re-applied while the previous Render image is still serving.
-- Plain CREATE INDEX takes a ShareLock before an IF NOT EXISTS name check can
-- return, so both a missing build and an already-valid no-op conflict with
-- ordinary RowExclusive live traffic and can convoy later writers behind the
-- deploy. IF NOT EXISTS is name-idempotence, not online safety. Every permanent
-- index in the schema is therefore created CONCURRENTLY.
--
-- PostgreSQL can leave an invalid/unready catalog shell when a concurrent build
-- is canceled. IF NOT EXISTS would mistake that shell for success forever. Run
-- this registry before any table module so the next generation/retry removes
-- only a Veripsa-managed broken non-unique shell on its expected table, without
-- touching a valid index or an unrelated same-name object. A shell that is
-- UNIQUE in either the expected contract or the actual catalog fails closed:
-- automatically dropping it would open a
-- DROP-to-rebuild interval with no concurrency invariant for live writers.
-- \gexec executes each permitted DROP outside a transaction, which is required
-- by DROP INDEX CONCURRENTLY.
--
-- Keep this session-local registry until 98_online_index_verify.sql. The final
-- verifier refuses to publish the schema marker unless every managed index is
-- attached to its expected table and is valid/ready.
CREATE TEMP TABLE IF NOT EXISTS veripsa_managed_index_contract (
  index_name name PRIMARY KEY,
  table_name name NOT NULL,
  is_unique boolean NOT NULL,
  definition_md5 text NOT NULL
    CHECK (definition_md5 ~ '^[0-9a-f]{32}$')
) ON COMMIT PRESERVE ROWS;
TRUNCATE pg_temp.veripsa_managed_index_contract;
INSERT INTO veripsa_managed_index_contract(
  index_name,table_name,is_unique,definition_md5
)
VALUES
  ('active_alert_by_fired','active_alert',false,'ee9b6e947e4fce06833d910cf0141a27'),
  ('agent_by_account','agent',false,'a2e933c8414f6d0bb816d262e569b6f3'),
  ('claim_active_by_account','claim',false,'70422461c37fe6ce9f6aae2f96d3cf5b'),
  ('claim_draft_by_change','claim',false,'dddd5748472907ae5ff78bc65c583f58'),
  ('claim_one_active','claim',true,'3afdca84ffdfb8951ae9788651edc977'),
  ('claim_open_change','claim',false,'b621f6ab1fc7b425ae92792015f8d3ea'),
  ('co_change_a','co_change',false,'d962d6dc1f3edeb28b9c84ad39e5ec04'),
  ('co_change_b','co_change',false,'5ad35d61d8905fe76648edb5f803b6f9'),
  ('co_change_by_account_repo','co_change',false,'0916e5c888626148d374c5d13dcb4fcc'),
  ('code_edge_coord','code_edge',false,'528b5b2ae27b63561dab74fb5b4b4d81'),
  ('code_edge_coord_kind_dst','code_edge',false,'3b21bd5107757f694108202f17b1ab3b'),
  ('code_edge_coord_kind_effective_semantic_dst','code_edge',false,'4c9e3cb3fbe0dee876e4615f8b4f84f0'),
  ('code_edge_coord_kind_src','code_edge',false,'88df1f6ed9837d99b042579b1e3b6a07'),
  ('code_edge_coord_uncertain','code_edge',false,'ad92be2b521add8b226428e3b526c68c'),
  ('code_node_coord_kind_effective_semantic','code_node',false,'e968c99fc14ede2234882783ff90efe9'),
  ('code_node_coord_nodeid','code_node',false,'ab635d8ccd486fe74a4f2393bd04dda2'),
  ('code_node_coord_path','code_node',false,'105495d90029e6d53cba3af64f51bcae'),
  ('code_node_coord_uncertain','code_node',false,'127f74af452baf40ad2bd7a39b77839f'),
  ('event_by_account_kind','event',false,'4c06260e62ccd993427f0e6aecd1d701'),
  ('event_by_account_repo_occurred','event',false,'6dd04b74e37e809b2edf72b302854090'),
  ('github_delivery_recovery_due','github_delivery_recovery',false,'c46611be95b945695c9e02f11b62dcb7'),
  ('github_delivery_recovery_expiry','github_delivery_recovery',false,'2f7620b775e02eac03cbb870de26a637'),
  ('grant_by_grantee','grant',false,'64b024549d2f52ad681e85e4ab4779f2'),
  ('graph_version_repo_id','graph_version',false,'08e1840b7082efc27cc2f514e71dc585'),
  ('intent_current','intent',false,'1c59db8d87f2772fef10b9ee852893dc'),
  ('policy_refresh_account_claimed','installation_account',false,'ce79019a28dbd76272cb13182665a95c'),
  ('policy_refresh_account_convergence_due','installation_account',false,'2a659f4532a2f388a69c2b303981e5d7'),
  ('policy_refresh_account_exception_depth','installation_account',false,'7e3e7470b9044dad694aa521af6b3154'),
  ('policy_refresh_account_graph_claimed','installation_account',false,'570a5e0c11041689e8a431359bf7b256'),
  ('policy_refresh_account_graph_due','installation_account',false,'09e33b944ac3b42080fc19d6eefee7b5'),
  ('policy_refresh_account_legacy_graph_due','installation_account',false,'7036cb371385c7162de8d771e4551ec4'),
  ('policy_refresh_account_policy_due','installation_account',false,'cefb797ad51bc44e2486189010c9483a'),
  ('policy_refresh_account_route','installation_account',false,'d24db3eae3440d9ce92bfc9f309e844e'),
  ('policy_refresh_account_schema_bridge','installation_account',false,'70dca7ac37517608a94fe2dc01b3839d'),
  ('policy_refresh_account_schema_bridge_v2','installation_account',false,'4f6d67fb2a781d10aacbd8f1ef1bf56c'),
  ('policy_refresh_account_stall_missing','installation_account',false,'b8c4c3b1226f450b3d07a3a5c2ffe000'),
  ('policy_refresh_account_stall_started','installation_account',false,'308feef113607ff9391a09931497ca41'),
  ('policy_refresh_outbox_identity_uq','policy_refresh_outbox',true,'bb5e3381c4fd8d900787bda0a5ff143e'),
  ('policy_refresh_outbox_claimable_due','policy_refresh_outbox',false,'7f784409dd8bd5444b66c499d8d903d6'),
  ('policy_refresh_outbox_slow_retry_due','policy_refresh_outbox',false,'3b48789c38c9e2a21e5cda24f9afce6d'),
  ('policy_refresh_outbox_unfinished_due','policy_refresh_outbox',false,'911a404fb43f1988b40abc798dddf68f'),
  ('policy_refresh_outbox_unfinished_enqueued','policy_refresh_outbox',false,'4c96372fdca973d214eccdf721052865'),
  ('policy_refresh_outbox_pending','policy_refresh_outbox',false,'f11dd84a35f994a956a0da4fc8df7788'),
  ('statement_by_account','statement',false,'041c82634d393433e1ad3bec2751449e'),
  ('statement_current','statement',false,'c64b6ebcf8e05623baa1eb976706b9d5'),
  ('webhook_delivery_account_causal_v2','webhook_delivery',false,'64945a59f00156e5d9c5fa64510e5d4d'),
  ('webhook_delivery_account_pending','webhook_delivery',false,'c6010c259cded04b9a003eb3da9ce90f'),
  ('webhook_delivery_due','webhook_delivery',false,'be764727a666b9d9136126ae6c77934b'),
  ('webhook_delivery_failed_auto_rearm','webhook_delivery',false,'83c9ddd71dcdb01807c982498c8bb124'),
  ('webhook_delivery_operator_batch_token_v1','webhook_delivery',false,'dd03c2c140534c1076cac4e2bcb3bd48'),
  ('webhook_delivery_operator_recovery_id_v1','webhook_delivery',false,'888387d87578fb1fea2dfce8fb7c557e'),
  ('webhook_delivery_pending','webhook_delivery',false,'eefb91b850e554edd8d86e6d7a1891b1'),
  ('webhook_delivery_repo','webhook_delivery',false,'edbe35264e77e49406bb2de3f0b6f9ad'),
  ('webhook_delivery_repository_causal_v1','webhook_delivery',false,'4f3e9bd3c611dfbf419cfb6487ddec28'),
  ('workspace_member_by_ws','workspace_member',false,'2f7336ecacde54a85656789f3ebe5797');

-- Narrow UNIQUE expansion retry. The old account-only (or an interrupted earlier target-shape) PRIMARY KEY
-- still enforces at least the stable-id uniqueness while this unowned invalid shell is removed. This exception
-- is exact-name/exact-table/no-constraint only; the generic path below continues to refuse every UNIQUE shell.
SELECT format('DROP INDEX CONCURRENTLY %I.%I',n.nspname,c.relname)
  FROM pg_class c
  JOIN pg_namespace n ON n.oid=c.relnamespace
  JOIN pg_index i ON i.indexrelid=c.oid
 WHERE n.nspname='core'
   AND c.relname='policy_refresh_outbox_identity_uq'
   AND i.indrelid=to_regclass('core.policy_refresh_outbox')
   AND (NOT i.indisvalid OR NOT i.indisready)
   AND NOT EXISTS (
     SELECT 1 FROM pg_constraint q WHERE q.conindid=i.indexrelid
   )
   AND EXISTS (
     SELECT 1
       FROM pg_constraint p
       CROSS JOIN LATERAL unnest(p.conkey) WITH ORDINALITY AS u(attnum,ord)
       JOIN pg_attribute a ON a.attrelid=p.conrelid AND a.attnum=u.attnum
      WHERE p.conrelid=i.indrelid AND p.contype='p'
      GROUP BY p.oid
     HAVING array_agg(a.attname::text ORDER BY u.ord) IN (
       ARRAY['account_id']::text[],
       ARRAY['account_id','request_kind','repository_id']::text[]
     )
   )
\gexec

SELECT format('DROP INDEX CONCURRENTLY %I.%I', n.nspname, c.relname)
  FROM veripsa_managed_index_contract m
  JOIN pg_class c ON c.relname=m.index_name
  JOIN pg_namespace n ON n.oid=c.relnamespace
  JOIN pg_index i ON i.indexrelid=c.oid
  JOIN pg_class t ON t.oid=i.indrelid AND t.relname=m.table_name
  JOIN pg_namespace tn ON tn.oid=t.relnamespace AND tn.nspname='core'
 WHERE n.nspname='core'
   AND (NOT i.indisvalid OR NOT i.indisready)
   AND NOT m.is_unique
   AND NOT i.indisunique
   AND NOT EXISTS (
     SELECT 1 FROM pg_constraint q WHERE q.conindid=i.indexrelid
   )
\gexec
