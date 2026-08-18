-- Veripsa core schema — split into PHASE modules for parallel work (1 file = 1 holder was the bottleneck).
-- Applied as ONE unit: psql -f db/schema.sql includes each module IN ORDER (\ir = relative to db/).
-- Edit the relevant module, not this index. Order matters (tables before the functions that use them).

-- remove only broken shells from interrupted managed concurrent-index builds before any IF NOT EXISTS check
\ir schema/05_online_index_repair.sql
-- moat substrate: account/agent/credential/grant + RLS + identity (Phase 1)
\ir schema/10_substrate.sql
-- traffic-control core nouns: claim, code graph, event ledger (Phase 2)
\ir schema/20_core.sql
-- operational durability inbox: persist a SANITIZED webhook before the 202 ack, claim/finish/recover from the worker (NOT the product ledger) (Phase 2)
\ir schema/25_webhook_queue.sql
-- the gate (only write path): provisioning, lock, queue, ingest, landing, push (Phase 2)
\ir schema/30_gate.sql
-- gate lifecycle: offboarding (purge/erase), re-coordinate (rename/archive), retention (prune), DR (export), all App-delegation-only (Phase 2)
\ir schema/35_lifecycle.sql
-- read surfaces: collision, board, effect (Phase 2)
\ir schema/40_surfaces.sql
-- records (records-not-correctness): statement (Phase 3)
\ir schema/50_records.sql
-- SaaS plumbing: store_connection, policy (Phase 4a)
\ir schema/60_saas.sql
-- social: follow, standing, discover (Phase 4b)
\ir schema/70_social.sql
-- cross-repo CONSENT / cross-tenant: workspace, workspace_member, bilateral mutual-consent + the CONSENTED cross-tenant contract read (Phase 4c - SHADOW, default OFF; the cross-repo moat-critical layer)
\ir schema/75_workspace.sql
-- structural contention: _claim_adjacency, main_impact_surface, clusters (Phase 2 cont.)
\ir schema/80_contention.sql
-- empirical coupling: co_change cache + ingest + partner read surface (the signal the graph cannot see)
\ir schema/85_cochange.sql
-- web surfaces: console read/owner-write (Phase 5)
\ir schema/90_web.sql
-- owner cost/usage lens: cross-tenant DB-growth + free line (owner-only, the DELIBERATE RLS exception) (Phase 6)
\ir schema/95_owner.sql
-- policy-change refresh outbox (G4): when a policy is committed, enqueue that tenant's open in-flight PRs for a background re-derive under the new policy — durable, content-free, drained OUTSIDE any DB txn (Phase 6b)
\ir schema/97_policy_refresh.sql
-- fail closed before publication unless every managed concurrent index is valid/ready on its expected table
\ir schema/98_online_index_verify.sql
-- LEAST-PRIVILEGE BACKSTOP (runs LAST): strip the PUBLIC-EXECUTE default off the whole core fn surface so a non-tenant role (billing/platform-reader) inherits no ambient reach; explicit role grants survive (Phase final)
\ir schema/99_least_privilege.sql
-- schema cutover + manifest stamp (ABSOLUTELY LAST): preserve the serving image's claim ABI through every fallible expansion and publish the new generation only after all modules succeed
\ir schema/100_schema_cutover.sql
