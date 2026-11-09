# Veripsa Core architecture and engineering choices

Veripsa Core makes relationships between parallel pull requests visible before
merge. Current maintenance focuses on useful local tools, runnable evidence,
and public explanations by [RollNuts](https://github.com/RollNuts). The hosted
GitHub App remains suspended since 2026-08-18. No restart, revenue feature, or
billing rollout is part of this work.

## Three layers to keep distinct

1. **Extraction:** `code_graph_extract.build_graph` reads local files and emits
   references such as imports, calls, shared schema/config resources, and
   supported API contracts. Coverage depends on syntax and installed grammars.
2. **Offline evidence:** the [synthetic demo](examples/structural-overlap/README.md)
   uses that graph and `cochange_backtest.coupled_pairs` to expose file-level
   review points next to actual Git and Python outcomes. That helper is a coarse
   approximation, not the PostgreSQL PR verdict. Separately, `evaluate.py`
   compares structural pairs with historical co-change on a local Git checkout.
3. **GitHub App runtime:** the source implements event handling, exact-head
   coordination state, GitHub checks/comments, and acknowledgement behavior.
   It requires configured infrastructure and permissions. Its presence in this
   public mirror is not evidence of a currently running service.

The [MCP experiment](https://github.com/RollNuts/zenn-content/tree/89fe0fe12258c827a9e7fc54cb12fc96401018de/examples/core-local-mcp)
projects a small part of the graph into two read-only tools in a separate repo.
It queries a saved snapshot, not the current worktree. It does not run the App's
PR pipeline or add automatic control over Codex, Claude Code, branches, or merges.

## Why these boundaries exist

- **Expose a review point, preserve uncertainty.** A relationship tells a
  developer what to inspect together. Compatible related changes and missed
  dynamic references are included in the demo so absence is never promoted to
  `Clear`, and a visible edge is never promoted to a correctness verdict.
- **Keep source bodies transient.** Extraction may read files locally. Stored
  graph and rendered App outputs retain structural/operational references, not
  source or diff bodies. Paths and symbols can still be sensitive. See the
  [enforced data boundary](SECURITY.md#content-free-posture).
- **Tie coordination to current heads.** The App implementation regenerates
  relationships as PRs are pushed, merged, or closed. An ACK refers to a
  particular warning snapshot; changed work can invalidate it. This is different
  from the demo's fixed input and from a permanent repository-memory product.
- **Reuse the engine in evidence.** The demo calls the existing extractor and
  adjacency helper. It does not invent a second relationship detector to make
  its examples pass. Its integrated Python runs show concrete outcomes, not
  measured user productivity or general detection accuracy.

## What an AI agent can do with a warning

An agent can inspect the named files and exact revisions, ask which work lands
first, propose a compatibility-preserving change, and run the combined tests.
A human or the repository's existing policy owns the next action. The adapter
has no write or merge tool; an agent's other file/tool permissions are separate.
A fixed snapshot cannot prove that a later edited worktree is safe.

## Implemented GitHub App boundary

The implemented surfaces are installation, PR lifecycle, branch/push traffic,
repository lifecycle, check reruns, merge-group events, GitHub checks, relevant
PR comments, and `veripsa-ack`. PR comments and labels use GitHub's issue-backed
endpoints; that implementation detail does not add Issue management to Core's
product scope. Current coordination is within one repository and target branch.

Default output is advisory. GitHub branch protection and rulesets determine
whether an `action_required` conclusion holds a merge. Code review, CI, and
merge queues retain their own responsibilities. Cross-repository coordination,
automatic conflict fixing, autonomous merge, and a productized MCP are future
work, not capabilities established by this demo.

## Reference implementation outside the current work

Historical entitlement and Marketplace-billing code is retained as reference
infrastructure. It stays outside this improvement scope and does not establish
an active plan, checkout, installation offer, or hosted availability.

For exact product semantics, see [product facts](docs/PRODUCT_FACTS.md). For
security and deployment requirements, see [SECURITY](SECURITY.md) and the
[deployment verification contract](docs/DEPLOYMENT_VERIFICATION.md).
