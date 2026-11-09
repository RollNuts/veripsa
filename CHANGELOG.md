# Changelog

## Unreleased — reproducible local evidence and project navigation

### Added

- A local structural-overlap demo using Git, Python 3.12, the existing Core extractor, the offline evaluator's coarse adjacency helper, and the existing co-change producer. Synthetic cases compare individual execution, actual clean Git merges, static and historical candidates, their offline union/intersection, and combined execution.
- Compatible related changes, incidental co-edits of unrelated files, dynamic lookup missed by static analysis, and a cold-start history alongside the failing function-contract example. The demo shows complementary evidence and limits without presenting a candidate as a bug verdict or absence as Clear.
- Small API-route and configuration-key probes using existing parsers.
- An implementation inventory for code graph, co-change, and persistence, distinguishing local candidate-set comparisons from the App's separate historical advisory and bounded SQL corroboration.
- An English diagram, a Japanese 30-second / 3-minute / 10-minute guide, and author links to RollNuts.
- Draft repository descriptions, three X drafts, and a background article grounded in the reproducible cases. These materials have not been posted externally.
- The author-provided development history: recurring Git-conflict friction, an early expectation of parallel-AI demand, the earlier MCP idea, the App event trigger choice, and the decision to defer active expansion. AI-assisted development and the current technical-asset objective are stated explicitly.

### Changed

- README entry points now lead to the local demo, then the existing pinned Flask evaluation, engineering choices, and the separate read-only MCP experiment.
- Architecture and product-fact documentation clarify the current maintenance goal: useful engineering, reproducible evidence, and understandable public work. Revenue, sales, new billing flows, and a hosted-service restart are outside this work.
- Roadmap wording distinguishes implemented source from a currently available GitHub App.
- Japanese background material separates the original motivation from current experiments. Code graph, co-change, and persistence are presented as accumulated implementations to inspect and verify, with no invented measurements or plan to remove the App database.

### Availability and verification scope

The hosted GitHub App remains suspended since 2026-08-18. The local demo requires a POSIX shell (macOS/Linux instructions); this change was measured on macOS and does not establish Windows support. The local demo does not execute the PostgreSQL PR-decision pipeline or establish a live hosted check. Its expected output includes a related change that works, a short history that misses a static relationship, incidental co-edits without a functional dependency, and dynamic lookup recovered only by the constructed history. Offline union/intersection are evidence comparisons, not the production PR policy or a general precision/recall measurement. The separate MCP adapter queries a saved snapshot; its optional Codex path consumes the user's model allowance and is not needed for the free local demo.

### Observed local validation

- The graph/history demo's 13 tests passed in the standard-library-only environment, the focused public-CI environment, and the existing full-runtime environment.
- Ten targeted PostgreSQL gate scripts passed on an isolated PostgreSQL 16.14 cluster, including graph persistence, full/incremental equivalence, lifecycle CAS, Co-change storage/push/deduplication/corroboration/denominators, content-free DB egress, tenant isolation, and finer symbol collision. The named scope and dependency caveat are in [the analysis inventory](docs/ANALYSIS_ASSETS.md#verification-record-for-this-change).
- Public snapshot, credential/local-path hygiene, Python compilation, and the existing public-CI commands passed locally. Hosted signals remain not observed; this is not a full release-suite or production validation claim.
