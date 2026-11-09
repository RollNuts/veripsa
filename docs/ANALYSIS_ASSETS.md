# Analysis assets: graph, history, and persistent coordination

Veripsa Core contains reusable local analysis and an implemented PostgreSQL / GitHub
App runtime. These are different ways to use the same engineering work. The hosted
App has been suspended since 2026-08-18; this document does not announce a restart.

日本語での要点：まずDB不要のCLIと固定snapshotで解析を試せます。複数イベントにまたがる履歴・世代管理・権限制御まで確かめる場合は、既存のローカルPostgreSQL実装を使います。保存方式の置換やホスト版再開は今回の範囲ではありません。

## Read the status precisely

- **Implemented:** a callable function, SQL contract, or integration exists in this
  source snapshot. That alone is not evidence of current hosted operation.
- **Verified in this change:** a named test actually ran with its dependencies and
  recorded its result. Test source or a missing optional parser is not a pass.
- **Usable locally:** the relevant executable, dependencies, and local input are
  available; the scope still follows that command's documented boundary.
- **Proposed:** a deployment shape or storage alternative described below, not an
  additional shipped backend or integration.

## Existing implementation map

| Asset | Entry points | What it supplies |
| --- | --- | --- |
| Static graph | [`code_graph_extract.build_graph`](../code_graph_extract.py), `blast_radius`, `language_coverage` | Local source parsing, reference-shaped Node/Edge data, resource identity, and analysis uncertainty. Python uses the standard library; other grammars and some configuration formats need optional dependencies. |
| Historical relationships | [`_cg_cochange._git_log_commits`, `fold_commits`, `emit_pairs`, `cochange_pairs`](../_cg_cochange.py) | Changed-path sets from non-merge Git history, rename handling, bounded large-commit exclusion, counts, directional probabilities, and lift. No source or commit-message corpus is required. |
| Local history evaluation | [`cochange_backtest.coupled_pairs`, `analyze`](../cochange_backtest.py), [`evaluate.py`](../evaluate.py) | A coarse static file-pair projection and a retrospective comparison with sampled historical co-change controls. This is not the PostgreSQL open-PR verdict. |
| Persisted graph | [`core.code_node`, `code_edge`, `graph_version`](../db/schema/20_core.sql); [`ingest_graph_with_authority`, `patch_graph_with_authority`](../db/schema/30_gate.sql) | Account/repository/branch-scoped graph state, extraction and semantic versions, uncertainty, authoritative graph identity, and governed full/patch writes. |
| Incremental reconstruction | [`ingest._incremental_ingest_with_slot`, `_reingest_graph`](../github-app/ingest.py) | Changed-consumer extraction with retained resolution context; guarded fallback to full extraction when a path-local replacement cannot preserve the complete graph. |
| Persisted history updates | [`cochange._cochange_pairs_via_clone`, `_cochange_increment_task`, `cochange_partners_for`](../github-app/cochange.py); [`core.co_change`, `co_change_seen_commit`](../db/schema/85_cochange.sql) | Backfill, stored counts, per-push accumulation, commit-SHA deduplication, and account-scoped partner reads. |
| Event-driven PR coordination | [`webhook.handle_pull_request`](../github-app/webhook.py), [`core._claim_adjacency`](../db/schema/70_social.sql), [`core.main_impact_surface`](../db/schema/80_contention.sql), [`render_pr_check`](../github-app/render.py) | Current open-work relationships and advisory output, updated through the App event path. Infrastructure, authenticated GitHub access, and a database are needed to run this layer. |

The [structural-overlap demo](../examples/structural-overlap/README.md) reuses the
local graph helper next to actual Git and Python outcomes. The separate
[MCP/Codex experiment](https://github.com/RollNuts/zenn-content/tree/89fe0fe12258c827a9e7fc54cb12fc96401018de/examples/core-local-mcp)
projects selected graph metadata into two read-only tools over a saved snapshot.
Neither route silently executes the App's full PR-decision pipeline.

## Use the existing local interfaces

```sh
python3.12 code_graph_extract.py /path/to/local/checkout --summary
```

The existing extractor CLI prints coverage and graph counts. Omitting
`--summary` prints the raw reference graph; review local paths and metadata
before sharing that output. No database is needed unless the separate explicit
`--push` persistence path is selected. The library also exposes
`blast_radius(graph, relative_path)` for direct symbol-name caller candidates;
its hub filtering and name matching are a bounded heuristic, not a complete
transitive impact or correctness proof. `_cg_cochange.cochange_pairs(checkout)`
returns historical partners from local Git history. The local demo exercises
the graph and Co-change producers without an App installation or model call.

## Static and historical evidence complement each other

Static analysis can explain a supported import, call, shared configuration key,
API contract, or other resource connection. Historical co-change can suggest
files that repeatedly changed together even without a static edge. They answer
different questions; a historical association does not establish a causal
runtime dependency.

There are two existing App uses of historical evidence:

1. `cochange_partners_for` reads strong historical partners, and
   `render._render_cochange` adds a separate **Historically changes together**
   advisory. It does not turn a missing history row into a safe verdict.
2. `main_impact_surface` can corroborate a real static relationship which hub
   dampening suppressed. Its bounded join to `core.co_change` uses default floors
   of `co >= 3` and `lift >= 2`; a qualifying relationship can move from `unknown`
   to `warn`. Missing or insufficient history keeps the unknown-first behavior.

This is not a general production rule that unions every static edge and every
co-change pair into one PR verdict. A local experiment may compare the two
candidate sets or their union, but that experimental candidate set must be
labelled separately from the App's SQL decision.

### History update limits

`cochange_pairs_incremental` folds new commit path sets onto counters rebuilt
from stored pairs. It uses the same counting helpers as the batch path, so an
identical retained seed and commit set can be compared meaningfully. It is
all-time-additive between backfills, not a continuously evicted 800-commit
rolling window.

Storage keeps repeated pairs at the lower persistence floor (`co >= 2`) before
applying stricter customer-facing read thresholds. Singletons and pairs evicted
by the pair cap are not fully reconstructible from that seed. A relationship
formed one occurrence at a time can therefore require a full backfill to recover.
Commit-SHA deduplication belongs to the persistent caller; blindly folding a
redelivered push twice would double-count.

## What persistence buys

The database retains bounded structural and operational facts, not a stored copy
of source or diff bodies:

- `code_node` / `code_edge`: paths, names, spans, signature shapes, resource keys,
  hashes, reference state, and analysis uncertainty.
- `graph_version`: the coordinate's exact commit, extractor and semantic versions,
  canonical graph hash, observability, and a monotonic `graph_revision`.
- `co_change` / `co_change_seen_commit`: pair counts and the commit identity ledger
  needed to avoid repeated increments.
- Claims, event records, ACK state, durable inbox/outbox work, and lifecycle
  receipts: the state needed to coordinate multiple events and recover work.

These facts remain sensitive even without bodies. Account-scoped FORCE RLS,
governed write functions, lifecycle fences, and deletion rules are material
parts of the implementation, not replaceable details of a JSON cache.

### Full, patch, and generation controls

`_incremental_ingest_with_slot` captures the stored baseline and resolution
context before extracting a patch. The SQL writer checks both
`expected_base_sha` and `expected_base_revision` under the coordinate lock before
its first replacement. The never-reused revision prevents an old patch from
being accepted after a sequence which returns to the same SHA (the ABA case).

Removed or relocated files, changed definitions or symmetric pairing context,
extractor/semantic-generation differences, and unresolved extraction uncertainty
can require full reconstruction. The implementation chooses full extraction
rather than claiming every edit is safely incremental. The canonical graph hash
supports full-versus-patch comparisons after excluding coordinate-only fields.

Repository-generation wrappers and convergence state additionally prevent a
background extraction from repopulating a removed, replaced, or renamed
repository generation. These controls address freshness and lifecycle races;
they do not prove that a code change is correct.

Retention is also differentiated. [`35_lifecycle.sql`](../db/schema/35_lifecycle.sql)
contains repository working-set purge, account erasure, and account-scoped
operational-event pruning. The append-only history and human decisions are not
identical to disposable current graph context. A persistent database does not
mean every record is kept forever.

### Compatibility analysis has a separate status

Signature-shape fields are part of the graph persistence contract. The Python
compatibility lane in [`_compat_analysis.py`](../github-app/_compat_analysis.py)
also computes transient AST shapes at PR heads and baselines, applying the
rules in [`_compat_rules.py`](../github-app/_compat_rules.py).

That lane is **default-off shadow analysis**, enabled only by both its analysis
flag and repository allowlist. Its findings and aggregate telemetry are not a
shipped customer-visible Check/comment feature. Its use of shape dictionaries
must not be presented as proof that the PR renderer consumes every persisted
signature column or that all contract incompatibilities are detected.

## Choose a local usage shape before a storage rewrite

| Shape | Analysis and reanalysis | Persistence | Setup and operation | Status |
| --- | --- | --- | --- | --- |
| On-demand CLI | Parse a selected local checkout; history commands can read local Git each run. Rerun when the input changes. | No long-running coordination store is required. Reports are explicit output files. | Git/Python for the minimal examples; optional parsers expand coverage. No App or model is needed. | Implemented. Recommended first step. |
| Fixed JSON snapshot | Query a bounded projection of a known extraction. Regenerate explicitly for a later revision. | Immutable input can be identified by its hash and source commit; it is not the current worktree. | The existing separate MCP adapter adds its own runtime dependencies. SDK-only checks are model-free; optional agent execution has separate account/usage requirements. | Implemented experiment. Not a new Core cache backend. |
| Local PostgreSQL | Retain and update the existing graph/history state, enforce governed operations, and exercise full/patch and event invariants. | Existing tables, counts, SHA deduplication, generations, locks, RLS, and lifecycle records. | Local server/client tools and the relevant runtime dependencies. Isolated test clusters are supported. Operating this locally does not resume the hosted App. | Implemented backend and test substrate. |
| A SQLite cache | Could retain a deliberately selected local projection and reduce repeated reads, if a concrete workload justified it. Reanalysis and invalidation policy would need to be designed. | No interchangeable SQLite implementation of the App store is established here. | New adapter/migrations/tests would be needed; PostgreSQL functions, RLS, locks, and CAS semantics cannot be assumed portable. | Proposed alternative, not implemented by this change. |

This is a capability and operational comparison, not a measured performance
benchmark. No comparative latency or storage-cost benchmark was run here. On-demand
analysis rereads source/history, snapshot queries reuse an explicit captured result,
and PostgreSQL can reuse governed state while falling back to full reconstruction
when an incremental patch cannot preserve the graph. Start with the DB-free CLI and a fixed snapshot when those satisfy
the question. Use the existing local PostgreSQL path when testing durable
coordination; replacing that backend is a different project.

## CLI, MCP, Actions, and the App can coexist

- **CLI:** create or inspect local evidence on demand without a service.
- **MCP adapter:** let an agent query a selected snapshot. The returned metadata
  can guide inspection, compatibility-preserving edits, and combined tests;
  later actions follow the agent's separately granted permissions.
- **GitHub Actions:** the current public CI automatically validates this
  repository's examples and focused checks. It is not a shipped Action for
  analyzing arbitrary repositories' PRs.
- **GitHub App:** the implemented event-driven layer owns continuous PR state,
  checks/comments, ACK, and lifecycle coordination when its infrastructure is
  configured and running. Current hosted operation remains suspended.

These are roles for the existing assets, not a proposal to rebuild the App as
MCP or move every analysis into a workflow runner. The author's motivation and
communication materials remain in the [Japanese guide](README.ja.md) and
[publishing kit](PUBLISHING_KIT.ja.md).

## Reproduce the PostgreSQL checks

Use a Bash shell from the repository root. Put installed PostgreSQL server and
client tools (`initdb`, `pg_ctl`, `psql`, `createdb`, `dropdb`) on `PATH`, and use
a Python 3.12 environment with the relevant existing dependencies. See
[`requirements.txt`](../requirements.txt) for the full runtime manifest and its
`--no-deps` strategy; [public CI](../.github/workflows/public-ci.yml) intentionally
installs a smaller focused set. Full multi-language extraction tests need their
actual grammars and YAML support; an absent parser is reduced coverage, not a
successful full-language test.

Source the helper and run the checks in the **same Bash shell**, so its exported
libpq settings select the disposable local cluster and its exit trap cleans it
up. These commands are a reproduction recipe, not an execution receipt:

```bash
bash <<'BASH'
set -euo pipefail
export VERIPSA_EPHEMERAL_PG=1
source db/_ephemeral_pg.sh
ephemeral_pg_start
python3.12 tests/test_cochange_store.py
python3.12 tests/test_cochange_persist.py
python3.12 tests/test_cochange_corroborated_warn.py
python3.12 tests/test_graph_persistence_contract_db.py
python3.12 tests/test_graph_full_incremental_equivalence.py
python3.12 tests/test_graph_convergence_lifecycle_cas.py
BASH
```

The helper creates only a private throwaway loopback cluster; each test owns a
scratch database within it. Its local diagnostic paths and raw setup logs should
be reviewed before sharing. This path exercises synthetic data and local SQL;
it does not call the hosted service.

| Existing test | Evidence it targets |
| --- | --- |
| `test_cochange_store.py` | Pair persistence/readback, directional probability, content boundary, tenant separation. |
| `test_cochange_persist.py` | Stored seed plus pushes, matching batch counts for the same retained inputs, SHA redelivery deduplication, governed writes. |
| `test_cochange_corroborated_warn.py` | Actual SQL unknown-first behavior and the narrowly corroborated hub warning. |
| `test_graph_persistence_contract_db.py` | Persisted graph kinds and metadata, full/patch identity, rejected invalid writes. |
| `test_graph_full_incremental_equivalence.py` | Real ingest decision paths compared with full extraction of the same final fixtures. |
| `test_graph_convergence_lifecycle_cas.py` | Generation races: removed, replaced, re-added, and renamed repository state. |
| `test_retention.py` | Gated operational-history pruning while preserving account boundaries and other records. |

### Verification record for this change

Observed on 2026-11-09 using an isolated PostgreSQL 16.14 cluster, with synthetic
fixtures and no external network/model/hosted-service calls. The following targeted gate
scripts completed with exit 0:

- `test_graph_persistence_contract_db.py`
- `test_graph_full_incremental_equivalence.py`
- `test_graph_convergence_lifecycle_cas.py`
- `test_cochange_store.py`
- `test_cochange_persist.py`
- `test_cochange_corroborated_warn.py`
- `test_cochange_denominator.py`
- `test_content_free_egress_db.py`
- `test_tenant_isolation.py`
- `test_finer_collision.py`

The full/incremental extraction test initially lacked optional grammars and YAML
support in the focused public-CI environment. It was rerun with an existing full
runtime environment and passed; a missing parser was not counted as a success.
Other named checks used the focused existing dependencies appropriate to their
fixtures. No dependency manifest, database schema, permissions, threshold, or
production runtime was changed for this work. The private cluster was torn down
on exit. These are targeted local regression results, not a full release-suite
run or evidence of current hosted operation. The retention test listed above was
not run in this focused set.
