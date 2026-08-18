# Roadmap

This is direction-signaling, not a commitment. There are no dates here, and
"in flight" / "next" are best-guess priorities that can change as we learn from
real usage. The Veripsa Core team is small; we publish only what we can keep.

For the security posture and threat model behind every shipped item, see
[`SECURITY.md`](./SECURITY.md). For incident response and operator
procedures, see [`github-app/RUNBOOK.md`](./github-app/RUNBOOK.md).

---

## Implemented

Items below are implemented in this source snapshot and covered by the release
gate suite (`./run_gates.sh`). The hosted service is intentionally suspended;
implementation status is not an availability claim.

- **Within-repo cross-PR collision detection.** Pre-merge structural
  collision warnings across covered open PRs on a tracked branch — direct
  (same-file reservation) and structural (call / import / schema / config
  coupling). Posted as a GitHub PR check + comment.
- **Advisory posture by default, with a pause-ack tier.** A `Clear` or
  non-material verdict is `success` / `neutral`. A *material* coupling
  (a direct collision, or a warn whose counterpart is actually in flight)
  posts `action_required` — a soft pause that the customer can clear with the
  `veripsa-ack` label. Veripsa never blocks a merge by itself; the customer's
  branch-protection policy decides whether `action_required` is a hard gate.
- **Content-free design, enforced.** File bodies never cross into the
  stored graph, the rendered PR comment, or any outbound alert payload —
  enforced by `gates.d/17-content_free_egress.gate` and
  `gates.d/159-content_free_db.gate`.
- **Per-account tenant isolation.** PostgreSQL `FORCE ROW LEVEL SECURITY`
  on every per-account table; the application connects as a non-owner role
  (`veripsa_app`) with no direct table grants. Enforced by
  `gates.d/27-tenant_isolation.gate`.
- **Durable webhook inbox.** Incoming GitHub webhook deliveries are
  persisted before the 202 response and recovered on boot, so a crash mid-burst
  does not drop work. Enforced by `gates.d/133-durable_inbox.gate`.
- **Free-first distribution model.** When hosted service resumes, the intended
  entry point is a free GitHub App with fair-use limits. Direct checkout is not
  active. Future paid plans, if introduced, are expected to use GitHub
  Marketplace billing.
- **Draft PR analysis.** Draft PRs run the same structural analysis as ready
  PRs. Cross-state draft↔ready findings are softened to warn-only so draft
  scout work can surface early without forcing a ready PR to wait behind a
  draft.
- **Co-change backtest.** A content-free local script
  (`python3 evaluate.py /path/to/your/repo`) that runs the same structural
  signal Veripsa uses against the customer's own git history and reports
  whether the flagged file pairs co-change more than chance and more than
  same-folder baseline. Nothing leaves the customer's machine.

## In flight

Items below have code or design in progress. They may ship in their current
form, change shape, or be deferred — none of these are commitments.

- **Hub-file wallpaper grouping.** Improve how widely-imported "hub" files
  (utilities, base classes, shared types) are handled in the contention
  cluster view so that they do not wallpaper across the rendered PR comment
  when many PRs share them.
- **`details_url` better target.** Make the `details_url` on the PR check
  point at a more useful per-collision drill-down than the current generic
  target.
- **Render wording precision.** Tighten the customer-facing language used
  in the warn / serialize comment paths (cluster headings, blast-radius
  framing, ack instructions) — driven by direct dogfood and adjacent-team
  feedback.

## Next

Direction-only — code may not be started yet.

- **Within-org cross-repo coordination (SHADOW first).** The foundation
  is fully landed dormant (route-tier substrate — consumer-side
  contract-key emission in `code_edge.dst` + relaxed adjacency couple —
  both behind default-OFF flags so within-repo behaviour is byte-identical
  to flag-off main). What is open is not code: it is finding a real
  same-owner pair of dogfood repos with a non-empty shared contract
  surface so the shadow measurement of co-change lift is meaningful before
  building a customer-facing surface. Cross-tenant (different-owner)
  consent is also landed dormant behind
  `gates.d/175-cross_tenant_consent.gate` and stays default-OFF until that
  same-owner shadow evidence is in.
- **Unity / C# extractor coverage.** Extend the unified extractor's
  language coverage to Unity-flavoured C# (`.cs` + meta files + ScriptableObject
  references). Today's language matrix is listed in the README "How it works"
  section.
- **Observability hardening.** Expand the watchdog → alert classes and the
  `/healthz` / `/readyz` / `/freshz` surface so that quiet-failure modes
  (a single tenant's graph going stale; a single language's extractor
  silently degrading) page faster.

## Strategic frontier

Direction-only; significant uncertainty on both customer demand and the
right shape.

- **Cross-owner consent-based collision.** The AI-outsourcing use case —
  an external coding-agent vendor opens PRs into the customer's repo, and
  Veripsa coordinates the two sides without either side reading the other's
  graph. The cross-tenant consent layer
  (`gates.d/175-cross_tenant_consent.gate`) is the moat-critical building
  block; the strategic question is which buyers / which workflow first.

---

## What is deliberately NOT on the roadmap

- **Storing or displaying file bodies.** The content-free posture is a
  deliberate product boundary, not a temporary state. Core may read repository
  files transiently to produce collision signals, but source file bodies do
  not belong in the stored graph, rendered comments, outbound alerts, or code
  review surface.
- **Becoming a merge queue.** Veripsa Core is pre-merge cross-PR collision
  control, not a merge queue (Graphite / Mergify / Aviator / GitHub Merge
  Queue). The "soft pause" with `veripsa-ack` is intentionally advisory;
  hard merge gating belongs in the customer's branch-protection policy.
- **Becoming an AI code reviewer.** Veripsa Core does not review the code
  inside a PR (CodeRabbit / Greptile / Qodo do that). It coordinates
  *between* PRs.
- **Off-Marketplace checkout for the GitHub App.** Distribution is the free
  GitHub App install today. If paid plans are introduced for the GitHub App,
  GitHub Marketplace is the expected billing path.
