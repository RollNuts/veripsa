# Schema generation manifest

This is the operator and reviewer contract for the Render pre-deploy schema guard. It is internal operational documentation.

## Why it exists

`db/schema.sql` is idempotent, but replaying every module on every code-only deploy still takes locks and repeats DDL against a live database. The pre-deploy guard therefore remembers which exact schema generation is live and runs the full additive schema only when an upgrade needs it.

The live state is a content-free `COMMENT ON SCHEMA core` marker:

```text
veripsa-schema/v1/<generation>/<sha256>/<adopted|applied>
```

The digest is deterministic canonical JSON over the generation plus the path, byte count, and SHA-256 of `db/schema.sql` and each ordered `\ir` include. The database stores only the final marker. There is no manifest table and no tenant row is read or written.

## State transitions

| Live state | Image decision |
| --- | --- |
| `core` absent | Apply the full schema under the session lock, then stamp `applied` |
| Existing `core`, no marker, image generation 1 | Run `schema_contract.check_schema_contract(VERIPSA_DSN)` as the least-privilege App role; only a healthy, non-skipped result may stamp `adopted`; full-schema DDL is zero |
| Existing `core`, no marker, image generation 2+ | Treat as a DR/legacy bring-current state: apply the full idempotent schema, then stamp `applied` |
| Same generation and digest | Skip all full-schema DDL |
| Same generation, different digest | Fail closed; a generation was reused for different SQL |
| Older live generation | Apply once under the session advisory lock; the last schema module stamps only after every preceding module succeeds |
| Newer live generation | Treat the image as a rollback and skip DDL and marker writes |
| Malformed or foreign schema comment | Fail closed |

The lock spans inspection and the complete apply. The exact target marker is
passed to `psql`; `db/schema/100_schema_cutover.sql` writes it as the final
schema transaction, and the manifest helper verifies the exact value before
reporting success. Duplicate Render deploys therefore re-read the marker in
order and cannot both apply one generation. A failed or partial apply leaves
the old marker untouched, so the next attempt retries rather than claiming
success.

## Changing schema

Every pull request that changes `db/schema.sql` or any included `db/schema/*.sql` file must also increment the positive integer in `db/schema_generation`. Do not reuse a generation for another digest. Code-only changes do not bump it.

The generation guard is for additive, idempotent schema changes only. Destructive changes still require the migration pair, backup, maintenance, rollback, and post-verification process in `github-app/RUNBOOK.md`.

### Expand/contract is a two-deploy invariant

Render runs pre-deploy SQL while the previous image is still serving, and it
may keep that image after either a schema failure or a new-image boot/health
failure. A PostgreSQL transaction is not atomic with Render promotion.
Therefore one generation must never both publish the API required by a new
runtime and remove or fail-close an API used by its predecessor.

Use two separately authorized publication phases. The contract phase may be a
later schema generation, or an exact-artifact production one-off after the
expanded runtime has been independently promoted and proved ready:

1. **Expand:** add the new API and preserve the exact catalog fingerprint of
   the audited operational predecessor ABI. An unknown body or metadata drift
   fails before DDL; it is never overwritten on the assumption that matching
   arity means compatibility. Promote the new runtime, then verify its exact
   SHA, health/readiness, delivery progress, and dogfood smoke.
2. **Contract:** only after that verified runtime is live. A normal later
   generation may remove/fail-close the old ABI. For the isolated convergence
   worker rollout, the exact target artifact instead runs the dedicated
   production one-off after worker + web + full smoke + final worker
   verification. That one-off requires exact target SHA agreement from
   `RENDER_GIT_COMMIT` and the baked `BUILD_SHA`, holds the schema-manifest
   advisory lock, requires the exact already-live applied marker/digest and a
   fully known catalog state, then replays only the final cutover transaction.
   It reports success only after exact post-state readback. No rollback to the
   legacy drainer is allowed after this boundary.

The expand deploy must remain usable if every later schema module fails, if
the final marker response is lost, or if the target image never becomes live.
Fresh databases may create a fail-closed compatibility shim because no
predecessor exists. A retry from a known partial-publication state must either
repair a proven-compatible bridge or fail loudly; function existence alone is
not compatibility proof. The normal manifest path classifies before any DDL.
A direct `psql -f db/schema.sql` apply has the same non-destructive boundary
for the serving `/4` ABI and marker, although earlier additive modules may
already have completed before the SQL backstop rejects an unknown fingerprint.

### Generation 15: graph uncertainty contract

Generation 15 adds nullable, no-default `core.code_node.analysis_status` and
`core.code_edge.reference_status` columns plus closed-enum checks. Existing
rows remain normal (`NULL`); edge status accepts only `ambiguous` or
`unresolved`. The checks are added `NOT VALID` and validated in
a separate statement, so an interrupted apply never advances the generation
marker and the next pre-deploy retries idempotently.

The extraction-semantic token advances from cg3/contract-v1 to
cg4/contract-v2. Schema-first rollout still accepts a complete cg3/v1 FULL
payload and stamps it behind; PATCH remains current-only. Because the cg4
canonical hash adds both status columns, the apply sets every pre-cg4
`graph_version.graph_hash` to `NULL` and removes its duplicate
`observability.persisted_graph_hash` key. The DB writer stamps
`observability.persistence.graph_hash_contract = "cg4-semantic-v2"` on every
hash it computes, including version-absent compatibility ingests, so a hot
reapply never mistakes a current-algorithm hash for a historical one.

Two sparse production indexes cover coordinate uncertainty probes:
`code_node_coord_uncertain` indexes only rows whose `analysis_status` is
non-NULL, and `code_edge_coord_uncertain` indexes only rows whose
`reference_status` is non-NULL. This keeps the normal all-clear
`coordinate_graph_sha()`/impact check from scanning an entire coordinate to
prove absence while adding no index entry for normal rows. Both are built with
`CREATE INDEX CONCURRENTLY` and are part of the runtime validity/readiness
contract.

Every permanent schema index follows the same online contract, not only these
two graph indexes. `05_online_index_repair.sql` owns the exact managed
name/table/uniqueness/definition fingerprint registry and removes only
invalid/unready non-unique, non-constraint shells from interrupted concurrent
builds. A shell marked UNIQUE by either the registry or the live catalog is
never auto-dropped: the verifier fails the deploy before marker publication so
an operator can repair it without opening a live uniqueness gap. Every module
creates its permanent indexes with
`CONCURRENTLY`; then
`98_online_index_verify.sql` proves all registered definitions valid and ready
before the final marker can publish. Replayed column/default/not-null additions
likewise consult `pg_catalog` and execute `ALTER TABLE` only when the contract is
actually absent; replayed sequence ownership also checks `pg_class` before
`ALTER SEQUENCE`, so live graph `nextval()` users are not convoyed. The
hot-deploy gate rejects raw top-level table/sequence ALTER and non-concurrent
permanent indexes, including multiline spellings.

### Generation 21: durable graph convergence

Generation 21 moves repository graph convergence out of latency-sensitive
webhook transactions and into an isolated Render background worker. Signed
push/PR facts and an exact stable-repository/default-branch/target-SHA request
commit atomically. A bounded background turn then verifies the current
repository generation, performs killable extraction, commits graph facts,
refreshes planner statistics, and only afterwards re-renders affected Checks.
Queue completion is epoch-fenced and requires SHA, stable repository identity,
extractor version, and semantic-reference version to match. Missing evidence
remains Unknown.

The generic schema pre-deploy preserves the policy-only claim/finish/fail ABI
used by the predecessor image. Old workers can claim only the policy sentinel;
graph rows require the new capability-bearing claim and exact-turn
terminalizers. The
scheduler selects the globally oldest eligible live account from an indexed
routing surface rather than scanning every tenant per claim, and processes at
most one repository before rotating the account to the tail. Its DB-validated
capacity contract permits exactly one active graph lease per account: queued
repository A2 remains behind A1's reclaim/finish boundary, leaving the second
worker available even when account B arrives only after A1 started.

After the exact target worker and web service are live, endpoint/full smoke has
passed, and both worker instances have been re-verified, the deployment runs
an access-controlled, provider-private one-off finalizer
from that exact artifact. Its final PostgreSQL transaction makes the old web
ingress-only:

- durable webhook claim `/4` becomes the audited safe wrapper whose NULL owner
  reaches `/6` and returns `legacy_budget_unproven` without leasing a due row;
- policy-refresh claim `/4` and transitional `/5` return SQL NULL, while their
  existing exact-epoch finish/fail CAS remains available for work claimed
  before the transaction;
- inherited `veripsa_writer`/`veripsa_app` EXECUTE on the generic, versionless
  full and patch graph writers is revoked. The SECURITY DEFINER
  repository-generation and convergence-lease wrappers remain App-callable.

Exact function comments are the durable cutover bit. `CREATE OR REPLACE`
preserves their OIDs/comments, module 30 refuses to re-grant a marked generic
graph writer, module 97 republishes marked legacy policy overloads as NULL
fences directly, and final module 100 automatically verifies/restores the
complete contract on every later full schema replay. A partial or foreign
marker set fails closed.
The first cutover, all ACL changes/comments, and the same-value manifest stamp
commit together; an injected marker failure rolls them all back.

### Generation 22: total durable-inbox readiness counters

Generation 22 makes `core.webhook_delivery_depth_with_authority()` return
explicit integer zeroes for the complete closed status enum (`queued`,
`processing`, `done`, and `failed`) when those groups have no rows.
PostgreSQL's `jsonb_object_agg` omits absent groups; the runtime correctly
treats a missing required counter as Unknown, so an empty healthy inbox
previously became permanently non-ready. The SQL surface now owns the total
response shape while preserving strict runtime validation: malformed, missing,
boolean, negative, or non-integer evidence remains Unknown.

This is an additive, schema-first-compatible `CREATE OR REPLACE` change. Older
images tolerate the extra zero-valued JSON keys, while generation-22 images can
distinguish a real empty queue from an unavailable depth sample.

### Generation 25: lossless graph-surface wake and audited recovery continuation

Generation 25 adds `policy_refresh_outbox.surface_dirty` as a non-null,
false-defaulted coalescing latch. A signed pull-request wake against an
unfinished graph row sets only this latch: it does not replace the push-owned
target, advance the request epoch, revoke the exact graph lease, reset the
current Check cursor, or discard quota/backoff authority. A latch present when
a full surface pass begins is consumed by that pass. A wake arriving after the
pass begins remains latched, and exact successful finish releases the lease and
atomically schedules one attempts-neutral full pass from the empty cursor.
Repeated wakes therefore coalesce without losing a pull request behind a
partially advanced Check page. Existing workers ignore the additive column and
the additive `surface_rearmed` result field; the database functions preserve
the latch semantics during a mixed-image deploy.

The same generation adds four immutable continuation-audit fields to
`webhook_delivery` (`operator_continuation_id`, `operator_continued_at`,
`operator_continuation_sha`, and `operator_continuation_count`) plus their
closed consistency constraint. The new App-only
`continue_terminal_webhook_deliveries_with_authority(text[],text,text)` API is
a forward-only, one-shot continuation of one already operator-recovered exact
batch after a reviewed code correction. It validates and locks the complete
canonical GUID set, the original shared recovery identity and batch size, and
an exact lowercase 40-hex runtime SHA before mutation. Eligible failed members
move back to `queued` with the final automatic-rearm epoch; members already
completed in the first epoch are not replayed, but every member receives the
same continuation id, timestamp, and SHA. The durable status API adds the
closed `continuable` and `continuation_failed` states so ACK loss is read back
instead of making a second mutation possible. Missing, mixed, partial,
wrong-SHA, or replayed sets fail closed and never clear either recovery audit.

Retention and global DR export preserve a non-erased `done` member while a
queued, processing, or failed sibling of the same operator-recovery batch still
exists. That minimized row is executable-data-free but is part of the exact
all-or-zero batch proof; pruning or omitting it would make the remaining failed
sibling unsafe to continue after restore. The matching generation-25 backup
validator accepts this dedicated recovery-proof shape and restores it without
requeueing it. Restore must retain the complete batch and both audit epochs;
fabricating a missing proof row, resetting either count, or restoring only the
failed subset is prohibited.

All catalog changes are additive and are applied before image promotion. The
ordinary claim/finish ABIs remain callable by the predecessor image, which does
not invoke the new continuation API. Do not submit a continuation during the
mixed-image window: first promote and verify the exact corrected web/worker
artifact and its schema contract, then invoke the continuation with that exact
SHA under separate production authorization. A predecessor backup exporter may
fail closed, without mutating rows, if it encounters the newly retained proof
shape during this short window; backup/restore operations for such a split
batch require the generation-25 runtime.

Rollback never removes these columns, functions, constraints, latch state, or
audit records: an older image sees a newer marker and skips DDL. Runtime
rollback before any continuation remains catalog-compatible. After a
continuation audit has been written, or while a split recovery batch depends on
the retained proof row, roll back only to an image that understands the
generation-25 status and backup contracts. There is no supported reset-and-
retry path; correct forward, preserve the exact audit, and verify durable drain
from the recorded SHA.

### Generation 26: attempt-neutral causal-head lock contention

Generation 26 republishes only the canonical
`core.claim_webhook_delivery_with_authority(text,int,int,int,text,int)` body.
Its account causal-head and stable-repository causal-head row locks use
`FOR UPDATE NOWAIT`. A lock held by the predecessor delivery is ordinary FIFO
contention, so only those two local `lock_not_available` boundaries return the
existing closed result `claimed=false, reason=blocked_by_earlier`. The return
occurs before the target claim update: it does not increment attempts or lease
generation, set an owner, or start a new retry window. The earlier exhausted-row
normalization retains its independent existing behavior. Recovery later
re-evaluates the same true head after the finisher commits. The fixed
`blocked_by_earlier` reason covers a locked predecessor or the currently owned
target when that row is itself the causal head. Urgent-uninstall supersession and
both causal-head locks share one exception subtransaction, so a NOWAIT miss
also rolls back every prospective queued/failed absorption before that false
claim result can commit.

`SKIP LOCKED` is prohibited because it could overtake the true account or
repository head. No Python-wide or function-wide SQLSTATE handler is added:
target-row locks for unknown-account work, advisory locks, unrelated queries,
and every other `55P03` remain fail-loud. The `/3`, `/4`, and `/5` compatibility
wrappers still delegate to the same canonical `/6` body, and no callable ABI,
table, column, privilege, or stored payload shape changes.

The change is an idempotent `CREATE OR REPLACE FUNCTION` publication. The
predecessor runtime already treats `blocked_by_earlier` as an attempt-neutral
durable deferral, so schema-first rollout is behaviorally compatible during
the mixed-image window. An older rollback image may run against the generation
26 catalog; it receives the same bounded result shape and does not require a
catalog rollback. Reverting the function body is neither necessary nor safe
while a newer runtime is live.

### Generation 27: fair installation convergence and forward-only external redelivery

Generation 27 removes installation onboarding's clone, graph extraction, open-PR enumeration, and Check publication
from the webhook request path. Signed installation lifecycle changes now commit only their durable activation and an
account-scoped convergence request. Two background worker processes rotate accounts fairly, admit at most one graph
turn per account, and process at most one planned PR per turn. Repository generation, default-branch, and exact HEAD
are revalidated before external reads and immediately before each Check mutation. A branch drift resets the plan;
same-branch HEAD drift preserves reusable planning evidence and requeues the new target. Expired work spends one
bounded crash/backoff turn so a sibling account can progress instead of waiting behind one tenant.

The account routing row adds `legacy_graph_refresh_due_at` and advances `convergence_schema_version` to 2. Two
concurrent indexes cover the legacy-graph bridge and schema-version bridge without a table-blocking build; their
fingerprints are registered in the online repair allowlist. Backup restore clears every live due/claim pointer and
restores the v2 empty-routing default, so recovery cannot manufacture old onboarding work.

The same unreleased generation adds a one-shot ledger for the separately authorized exact-three GitHub App
redelivery. Five closed audit columns on `webhook_delivery` store the provider delivery id, exact deployed SHA,
spend timestamp, fixed content-free outcome, and a 0/1 spend count. App-only SECURITY DEFINER claim and outcome
functions authenticate and lock the full three-row recovery/continuation batch. The initial claim requires all
three rows terminal. After a `202` lets a signed receipt move an already-spent row back to queued/processing, later
claims require the target unspent row to remain terminal and every prior spend to have a distinct provider id, the
same release SHA, and an immutable `accepted` outcome. A missing, mixed, partial, rejected, response-unknown, or
concurrent authority never reopens a GitHub POST.

These additions are schema-first compatible: predecessor images ignore the new nullable/defaulted fields and do not
call the new functions. Once any spend exists, rollback to code that omits the ledger is unsupported even though the
catalog remains readable; recovery is forward-only. Global backup/restore preserves coherent un-erased spend state,
legacy format-v2 rows normalize only to an unspent fence, and the hard-erasure trigger clears every recovery,
continuation, and external-redelivery audit field together.

For disaster recovery, run the guard against the genuinely empty target after roles are provisioned, before
restoring durable rows. If a restored existing `core` has no marker, generation
2+ deliberately reapplies the complete idempotent schema before stamping; it
never adopts the unknown state without DDL.

Before merge, run:

```bash
python3 tests/test_schema_generation_manifest.py
python3 tests/test_schema_hot_deploy_guards.py
python3 tests/test_predeploy_schema_wiring.py
python3 tests/test_schema_contract_cutover.py
python3 tests/test_predeploy_schema_apply_e2e.py
```

The E2E test uses scratch Postgres databases and proves zero-DDL
current/adoption paths, fail-closed states, rollback behavior, failure
non-advancement, exactly-once concurrent apply, full replay under live writers,
non-convoying missing-index builds, and wrong-definition marker rejection.

## Operational behavior

`OWNER_DSN` missing preserves the historical explicit degrade: the shell logs the skip and exits zero, leaving boot-time schema contract as the backstop. An existing unmarked database cannot be adopted without `VERIPSA_DSN`, and `VERIPSA_SCHEMA_CONTRACT=0` cannot authorize adoption because a skipped contract is rejected.

Safe logs contain only generation, digest, file count, decision, runtime metadata-check count, and coarse failure class. They never print a DSN, source body, tenant row, config value, or secret.
