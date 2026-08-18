# Graph Schema Contract

This document defines the compatibility boundary between code-graph extraction,
PostgreSQL persistence, incremental patching, and effective adjacency. The
machine-readable source of truth is
[`cg_schema_contract.py`](../cg_schema_contract.py).

The central invariant is:

> A kind emitted by extraction must not disappear at either the full-ingest or
> incremental-patch persistence wall.

There are no intentionally non-persisted node or edge kinds in contract version
2. A new kind must be added to the contract, both SQL acceptance stages, tests,
and this document in one change. Unknown kinds must fail validation; they must
not be silently filtered.

## Automated inventory

Run:

```bash
python3 scripts/graph_schema_inventory.py
python3 scripts/graph_schema_inventory.py --json
```

The command parses all three PostgreSQL acceptance points:

1. `code_node_kind_check` and `code_edge_kind_check` in
   `db/schema/20_core.sql`;
2. full-ingest `n->>'kind'` / `e->>'kind'` filters in
   `core.ingest_graph_with_authority`;
3. incremental-patch filters in `core.patch_graph_with_authority`.

It also parses the executable `node_kind` and `edge_kind` predicates inside
`core._claim_adjacency` in `db/schema/70_social.sql` and compares them with
`EFFECTIVE_ADJACENCY_NODE_KINDS` and
`EFFECTIVE_ADJACENCY_EDGE_KINDS`. Unsupported/dynamic kind expressions fail
closed. Override the source only for fixtures/audits:

```bash
python3 scripts/graph_schema_inventory.py \
  --adjacency-sql db/schema/70_social.sql
```

It exits nonzero if a stage drops a declared kind, accepts an undeclared kind,
differs from another stage, or adjacency SQL drifts from the declared behavior.
A parse ambiguity is also a failure: inability to prove the acceptance set is
`Unknown`, never `Clear`.
The JSON inventory also publishes semantic-reference version `1`, its persisted
Node/Edge columns, and the fixed full-fallback code used for a legacy
display-only coordinate. Contract version `2` additionally publishes the closed
analysis/reference status enums and declares that effective adjacency consumes
resolved (`reference_status IS NULL`) edges only.

## Node kinds

| Kind | Substrate | First-class resource |
|---|---|---:|
| `file` | code structure/imports/calls | no |
| `def` | code structure/calls | no |
| `class` | code structure/calls | no |
| `table` | database | yes |
| `column` | database | yes |
| `config_file` | config | no |
| `config_key` | config | yes |
| `iac_resource` | Terraform | yes |
| `k8s_resource` | Kubernetes | yes |
| `api_type` | GraphQL | yes |
| `api_message` | protobuf | yes |
| `api_service` | protobuf/gRPC | yes |
| `api_operation` | OpenAPI | yes |
| `api_schema` | OpenAPI | yes |
| `ci_script` | GitHub Actions/package scripts | yes |
| `app_command` | Tauri | yes |
| `job_task` | Celery | yes |
| `job_queue` | BullMQ | yes |
| `sibling_stem` | sibling-stem contract | yes |
| `role_feature` | role-feature contract | yes |

`file` and `config_file` are document nodes. `def` and `class` are code-symbol
nodes. Every other node is a resource/catalog node.

## Per-path uncertainty

Uncertainty is persisted beside the fact it qualifies; it is not inferred from
a missing edge:

- `code_node.analysis_status` is `NULL`, `failed`, `ambiguous`, or
  `incomplete`, and a non-null value is valid only on
  `file`/`config_file`;
- `code_edge.reference_status` is `NULL`, `ambiguous`, or `unresolved`.

`NULL` is the normal resolved/complete state and preserves compatibility for
existing rows. A parser/extractor failure retains one canonical document node
with `analysis_status=failed`. A local reference with more than one possible
target retains bounded edge provenance with
`reference_status=ambiguous`.
An import whose spelling proves it is repository-local (`./`, `../`, `@/`, or
`~/`) and whose resolver candidate set is empty retains its raw edge with
`reference_status=unresolved`. Bare/dotted imports and scoped packages such as
`react`, `vendor.package`, or `@scope/pkg` can be external dependencies and
therefore remain statusless when they have no repository target.
An error-tolerant parser that returns a partial tree retains its document with
`analysis_status=incomplete`; partial syntax can never prove that every
reference was extracted.

Status-bearing edges are stored and hashed but every effective graph consumer
filters them out: within-repo adjacency, dampening/hub classification,
cross-repo/cross-account contract reads, split advice, and structural
dashboard fan-in. `main_impact_surface` materializes status-bearing document
paths and both endpoints of an ambiguous edge when its destination resolves to
a persisted document. An unresolved edge marks its source only because it has
no resolved destination. `analysis_ambiguous`, `reference_ambiguous`, and
`reference_unresolved` are bounded: only their recorded in-flight endpoints
become Unknown. Stored uncertainty remains path-local and is never copied onto
unrelated persisted nodes.

Failed or incomplete document extraction is different: it can omit an
unbounded set of references. At `main_impact_surface` query time, if an actual
`failed` or `incomplete` path is in the current active/waiting set, every
otherwise-Clear change in that same in-flight set becomes Unknown. A historical
failed/incomplete path outside the in-flight set does not activate this wall,
so it cannot permanently poison the coordinate. Existing stronger evidence
keeps its precedence: direct collision remains Serialize and a separate
resolved coupling remains Warn.

Query output includes the actual uncertain path in `unknown_paths` and one of
the fixed content-free reasons
`analysis_failed`, `analysis_ambiguous`, `analysis_incomplete`, or
`reference_ambiguous` or `reference_unresolved` in `graph_uncertainty`. A path
promoted only by the in-flight unbounded-loss wall is included in
`unknown_paths` with reason
`inflight_peer_analysis_incomplete`; actual failure reasons and bounded
endpoint reasons are not overwritten.

Resource canonical keys are untyped join coordinates. If the final extracted
catalog maps one canonical key to more than one Resource Node kind (for example,
a database `table` and a `config_key` both named `database_url`), every
resource-bearing Edge to that key is retained with
`reference_status=ambiguous`. Those Edges are persistence/uncertainty evidence,
never effective adjacency, and every source document is Unknown. Distinct kinds
remain ambiguous even when they belong to the same substrate (for example,
`table` versus `column`). Multiple
definitions of the same Resource Node kind keep their substrate-specific
multi-definer behavior; this cross-kind wall does not change exact same-kind
resolution.

Every guard-approved config file receives a `config_file` node even when it has
zero distinctive config keys. This is the persisted analyzed-file universe
used to reconstruct target-SHA incremental context; omitting an empty
contract-bearing file would make a later patch unknowingly incomplete.

Every bounded, regular-file `.gitattributes` accepted by the extractor is also
persisted as a `config_file` with `language=gitattributes`. These files are
extraction control context: unchanged root and nested
`linguist-generated`/`linguist-vendored` rules, including negative carve-outs,
must be present when the target-SHA tree is reconstructed. Symlinked or
oversized attribute files are never followed or persisted. A changed path that
is absent from the persisted analyzed-file universe is conservatively rebuilt
in full because a path-only patch cannot prove whether an unchanged attribute
rule excludes it.

## Edge kinds

| Kind | Meaning | Persisted | Effective adjacency |
|---|---|---:|---:|
| `contains` | document owns symbol | yes | no |
| `calls` | code references a callable name | yes | yes |
| `imports` | import or resolved cross-tier route | yes | yes |
| `alters` | resource definition/contribution | yes | yes |
| `queries` | resource reference | yes | yes |
| `reads_config` | config-key reference | yes | yes |
| `alters_col` | column definition/change evidence | yes | no |
| `queries_col` | explicit column-reference evidence | yes | no |

`contains` is intentionally structural-only. Call resolution uses persisted
`def`/`class` nodes, so traversing `contains` as another file-to-file relation
would duplicate rather than extend effective adjacency.

`alters_col` and `queries_col` are intentionally evidence-only. They are
persisted so column facts are not lost, but effective adjacency does not consume
them yet. This preserves existing review behavior. Making column evidence affect
review decisions requires a separate, explicit behavior change with precision
and regression evidence.

Routes reuse `imports`, and most resource substrates reuse `alters`/`queries`.
Metrics can distinguish those shared edge kinds only when the resource
destination exists in the same graph or the producer stamps an explicit
`substrate` field. In particular, a route edge is indistinguishable from an
ordinary resolved import without provenance; it must be counted under
`imports` rather than guessed.

## Resource metadata

Every first-class resource record has:

- `kind`: one of the 16 resource node kinds above;
- `canonical_key`: a bounded, display-safe representation of the resource
  coupling key;
- `semantic_key`: SHA-256 over the exact UTF-8 resolver key used for equality;
- `repo`: repository coordinate;
- `path` or `scope`: ownership/provenance boundary;
- `extractor`: responsible extraction pass;
- `confidence`: finite value from `0.0` through `1.0`;
- `provenance`: a content-free object identifying how the fact was produced.

`enrich_resource_node()` derives historical canonical-key shapes without
changing logical node IDs:

- tables use the table name;
- columns use `table.column`;
- config keys use the key name;
- Terraform/Kubernetes strip the node-kind ID prefix;
- API, CI, Tauri, job, sibling, and role contracts use their complete contract
  ID.

Default provenance contains only extractor name, logical node ID, and path. It
must never contain source bodies, configuration values, or other content.
`code_edge.dst` follows the same display rule and
`code_edge.semantic_dst_key` retains its exact equality key. Effective
adjacency, ambiguity detection, fan-in and resource-catalog joins compare the
semantic keys. Legacy rows with a null digest derive one from their already
stored display value only until a safe full rebuild upgrades the coordinate to
`semantic_ref_version=1`; incremental patching never crosses that version
boundary.
When multiple definitions make a resource ambiguous, the resource node remains
first-class with `provenance.ambiguous=true`. Any retained candidate coupling
edge is explicitly marked `reference_status=ambiguous`; it is persistence and
incremental evidence, never effective adjacency.

Every current producer writes top-level `extractor_version: "cg4"` and metric
`schema_contract_version: 2`. PostgreSQL binds those claims strictly: current
cg4 requires contract v2, while the deployed historical cg3 FULL producer is
accepted only with contract v1 and is stamped behind cg4. cg3 claiming v2 and
cg4 claiming v1 are rejected before graph replacement.
Version-absent/cg1/cg2 FULL compatibility remains historical; every PATCH is
current-cg4-only and must land atop a cg4 coordinate. A cg3 coordinate
therefore forces a coherent full rebuild instead of partially upgrading
retained files. Unknown future tokens fail before graph replacement.

## Validation and canonical equivalence

`validate_graph()` and `assert_valid_graph()` reject:

- undeclared node/edge kinds;
- missing node IDs, paths, or edge endpoints;
- duplicate `(id, kind, path)` Node identities and duplicate
  `(src, dst, kind)` edges (an ID may legitimately be shared by a repository
  path and a generated Resource Node);
- status values outside the closed uncertainty enums, or document-analysis
  status attached to a non-document Node;
- incomplete resource metadata when strict metadata validation is requested.

`canonical_normalized_sets()` and `canonical_graph_hash()` compare semantic
graph records. They:

- normalize Unicode to NFC and path separators to `/`;
- ignore node/edge order;
- ignore database row IDs and timestamps;
- retain logical node IDs, paths, kinds, canonical keys, provenance, and
  uncertainty statuses;
- hash sorted normalized sets with SHA-256 and the schema-contract version.

Full and incremental results for the same repository commit are equivalent only
when `canonical_graph_diff(...).equivalent` is true and their canonical hashes
match. Comparing only edges that currently resolve to live files is not an
equivalence proof.

Both governed PostgreSQL writers enforce the same Node and Edge identity
uniqueness before tenant lookup or graph mutation. A duplicate payload raises
SQLSTATE `22023`; full replacement and touched-path patching leave the existing
coordinate byte-for-byte unchanged. This is intentionally a writer-boundary
contract rather than a physical unique index, so schema rollout does not fail
on historical rows produced before the contract existed.

## Observability

`collect_graph_metrics()` records:

- distinct input-file count;
- node counts by kind and substrate;
- edge counts by kind and substrate;
- counts and reasons for persistence exclusions;
- unresolved and ambiguous reference counts;
- full-rebuild fallback reasons;
- canonical extraction graph hash (`extraction_graph_hash`).

Hash names deliberately distinguish two projections:

- `extraction_graph_hash` is the 64-character SHA-256 returned by
  `canonical_graph_hash()` over normalized extractor records;
- `persisted_graph_hash` is the database-computed canonical hash over actual
  persisted rows. `graph_version.graph_hash` and API `graph_hash` refer to this
  persisted hash.

cg4's persisted hash domain is prefixed `cg4-semantic-v2` and includes both
uncertainty columns. The DB-generated
`observability.persistence.graph_hash_contract` carries that same token.
During schema-first rollout, each pre-cg4/historical
`graph_version.graph_hash` without the current contract marker is set to
`NULL` and the duplicate `observability.persisted_graph_hash` key is removed.
This migration is idempotent and never rewrites a hash computed by the current
writer, including a version-absent compatibility ingest; a full cg4 ingest
recomputes the only authoritative value.

The algorithms and serialization can differ, so these hashes are not compared
to one another. A path-local incremental extraction hash describes only its
patch payload and is therefore not compared with a full extraction hash. The
authoritative end-state proof is that the persisted full and incremental
coordinate hashes match after reading the actual DB rows.

Unresolved and ambiguous counts are explicit inputs. The helper deliberately
does not infer zero from arbitrary missing nodes, because bare calls and
external imports can legitimately have edge-only destinations. The extractor
counts unresolved emitted imports, references to retained first-class resources
without definition evidence, retained multi-definer resources, canonical-key
collisions, and resolved import fan-out. `ambiguity_detection_scope` records
that exact scope. Ambiguous Resource nodes and candidate edges are persisted
as evidence; candidate edges carry `reference_status=ambiguous` and never enter
effective adjacency.

Any nonzero persistence exclusion has a machine-readable reason such as
`node_kind_not_accepted:k8s_resource`. Under this lossless contract, a nonzero
exclusion is an integrity failure.

## Incremental slicing

`slice_graph_for_touched_paths()` implements a deterministic path-owned slice
for persistence. It includes touched-path nodes and outgoing edges, plus
incoming imports to removed paths. It is intentionally documented as a
mechanical helper, not an incremental-correctness proof.

Known-resource, ambiguity, selector, route, manifest, script, command, task,
queue, sibling, and role linking requires repository-wide facts. The current
incremental path therefore:

1. binds the changed-path set to its exact base and target commits. Pushes
   preserve `before`, `after`, `size`, and the complete bounded `commits[]`
   metadata through durable replay. Payload-less heals accept only an ancestry
   proof whose response base and merge base equal the stored commit and whose
   response head equals the requested target; a 300-file Compare response is
   ambiguous and forces full rebuild;
2. reads the coordinate version before every baseline-dependent path/catalog
   read, requires it to equal the changed-set base, and carries both that SHA
   (`expected_base_sha`) and its positive, never-reused PostgreSQL-sequence
   token (`expected_base_revision`). Migration sentinel revision `0` is
   intentionally unpatchable and forces one full rebuild;
3. reads the persisted Resource catalog and complete analyzed-file universe;
4. fetches every context file from the immutable target SHA (never baseline
   bytes), bounded to 400 files;
5. verifies through bounded Git-tree metadata that every changed/context path
   is a regular (`100644`) or executable (`100755`) blob before materializing
   Contents API bytes;
6. runs the real extractor over that complete target tree;
7. compares baseline and target Resource identity, metadata, definition paths,
   and reference paths after removing changed-file-owned contributions;
8. obeys the canonical mutation tier order `stable-id → repo →
   account(shared/exclusive) → coordinate`, while each path takes only the
   tiers it needs. Full/patch take `repo → account(shared/live) → coordinate`;
   authenticated identity paths may already hold the stable-id tier; repo
   purge, rename/transfer, and cold retention take every affected repo lock
   before graph mutation; account-wide purge/erase takes the account tier
   exclusively. Cold retention re-checks freshness and active claims after
   acquiring the repo lock. The patch then compare-and-swaps both expected
   tokens before the first delete, so lifecycle deletion cannot interleave
   after CAS. A successful full/patch write receives a new
   `graph_revision_seq` value; sequence values are never reused, including
   after rollback or graph-version deletion/recreation.

Definition changes, retained resources without definition evidence,
reference-conditioned pairs, Kubernetes/routes, deletions, symmetric pair
members, bidirectional imports, target Resource-summary drift, or context above
the cap produce a reasoned full rebuild. Missing, malformed, truncated, or
non-regular target-tree mode evidence does the same; raw Contents API bytes are
not sufficient because that API may dereference a symlink. This is deliberately
conservative:
negative facts such as multi-definer suppression are never reconstructed from
changed files alone, and uncertainty becomes full reconstruction rather than a
stale or falsely Clear graph.

A coordinate that already contains any first-class uncertainty never accepts a
path-local App patch; incremental preflight uses
`stored_graph_uncertainty` and rebuilds the target tree. This does not mark the
coordinate freshness-behind, so a legitimate ambiguous current graph does not
self-heal endlessly. A newly extracted target slice similarly uses
`extractor_file_failed`, `extractor_file_incomplete`, or
`ambiguous_reference_detected` and performs a full rebuild so an unchanged
uncertain neighborhood cannot be mistaken for complete.
A newly extracted zero-candidate local import may be patched with its explicit
`unresolved` edge because the bounded target-SHA universe proves the negative
fact for that changed source. On a later commit, the stored uncertainty forces
one full rebuild before any path-local patch; if an exact target now exists,
the resolver replaces the raw edge with a normal file-to-file edge and the
coordinate uncertainty clears.

Durable observability distinguishes an unproven/diverged Compare history
(`compare_history_unproven`) from a stored coordinate that does not equal the
changed-set base (`changed_set_base_mismatch`). Human/path detail remains only
in bounded runtime logs; these fixed codes survive in
`graph_version.observability`. An under-lock SHA/revision CAS miss is the
expected concurrent case `graph_baseline_changed`, not a generic internal
failure.

## Observability persistence

Extractor observability enters persistence through the single `metrics` field.
The caller-facing `observability` alias is reserved for writer output and a
non-empty input is rejected. PostgreSQL accepts only the closed metric-key
contract, bounded integer counts, fixed enum values, 64-character lowercase
hashes, closed-key Node/Edge/substrate count maps, and the fixed full-rebuild reason codes
published by `core.graph_schema_inventory()`. Human explanations, paths,
exception text, source, diffs, unknown keys, and arbitrary nesting are rejected
before any full-coordinate or touched-path delete.

The only rollout exception is an immediately historical
version-absent/cg1/cg2 **full** writer: a metrics object no larger than 64 KiB
may carry at most 16 string fallback details. PostgreSQL never compares, logs,
or stores those strings; an empty array remains empty and every non-empty array
is collapsed to the fixed `incremental_internal_error` code before closed
validation. A malformed legacy array and the identical cg3/cg4 prose payload
are rejected atomically. Historical patch writers receive no exception.

Producer-reported persistence exclusions must be zero with an empty reason map.
The nested `persistence` object and `persisted_graph_hash` are computed by
PostgreSQL after the write and are the authoritative readback. Full and patch
mode-specific fields are validated against the writer being invoked; a rejected
payload leaves the existing coordinate unchanged.

## Kubernetes runtime dependency

Kubernetes extraction uses `yaml.safe_load_all`, so PyYAML is an explicit,
upper-bounded production dependency (`PyYAML>=6.0.2,<7`) in both
`requirements.txt` and the production Docker image. `tree-sitter-yaml` remains
in the declared grammar ABI closure; it does not expose a semantic object loader.
Reimplementing mappings, sequences, anchors, multi-document streams, and YAML
scalar rules on top of its syntax tree would create a second YAML semantics
layer and a larger correctness/safety surface. PyYAML's safe loader is therefore
the maintained choice, with Veripsa's existing file-size and traversal guards
bounding input.

The dependency gate builds the actual application Dockerfile and runs
`tests/test_kubernetes_runtime_contract.py` inside that image. The test fails
closed if `yaml.safe_load_all` is unavailable and proves Service
selector-to-workload, ConfigMap, and Secret references through `build_graph`.

## Change checklist

When adding or changing a graph kind:

1. update the kind and substrate mapping in `cg_schema_contract.py`;
2. update both PostgreSQL CHECK constraints and both full/patch filters;
3. update effective adjacency or add a formal structural-only reason;
4. add resource metadata and canonical-key handling if applicable;
5. exercise the kind in `tests/test_graph_schema_inventory.py`;
6. run the human and JSON inventory commands;
7. verify canonical full/incremental equality from persisted DB readback.
