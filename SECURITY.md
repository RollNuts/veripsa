# Security Policy

Thank you for helping keep Veripsa and its users safe. This document covers what
the service does and does not defend against, the controls that enforce those
boundaries, and how to report a suspected security issue.

---

## Reporting a vulnerability

The affirmative channel for reporting a suspected security vulnerability in
Veripsa Core is **GitHub private vulnerability reporting** — the repository's
**Security → "Report a vulnerability"** advisory flow. It opens a private
advisory visible only to you and the maintainers. Please use it as the first and
primary channel.

If your account cannot access the private GitHub reporting flow, do not post
vulnerability details in a public issue. Instead, open a minimal public issue on
this repository asking for a private disclosure channel, and include no exploit
details, secrets, tokens, source file bodies, private diffs, webhook payloads, or
customer data. A maintainer will then open a private advisory to continue.

Please include enough detail in the private report to reproduce the issue:
affected component, reproduction steps, expected vs. observed behaviour, and the
impact you believe the issue carries.

**Please do not publicly disclose the issue** (no public GitHub issue, blog post,
or social-media post) until we have had a reasonable chance to investigate, ship
a fix, and coordinate a disclosure timeline with you. Coordinated disclosure
protects the users who depend on the service.

If you report in good faith and follow the above, we will not pursue or support
legal action against you for your research.

### Expected response window

This is a small team and we publish only what we can keep. Best-effort targets,
not guarantees:

- **Acknowledgement** of your private report — within 5 business days after it
  reaches a maintainer.
- **Initial triage** (in-scope / out-of-scope, severity assessment) — within
  10 business days.
- **Remediation progress updates** — at least every 14 days until the issue is
  resolved or deferred with rationale.

If we miss one of these windows, the issue stays open — the windows are pace
commitments, not closure commitments.

---

## Threat model

This is the honest "what we defend against vs. what we don't" boundary. Treat
anything outside the "in scope" list as not-yet-defended unless explicitly noted.

### What Veripsa Core DOES defend against

1. **Source-body leak ("content-free" posture).** No file body crosses from
   the extractor into stored state, customer-facing output (the PR comment or
   check), or an outbound alert payload. The only graph values that survive are
   paths, symbol names, line ranges, structural edges (calls / imports / schema
   / config), commit SHAs, and PR-author logins.
2. **Cross-tenant read.** One GitHub account's data cannot be read from another
   account's session — including from the application's own database role. Even
   a forged session GUC or a forged membership row does not cross the boundary.
3. **Webhook replay / forgery.** The public webhook endpoint validates the
   `X-Hub-Signature-256` header in constant time against a shared secret; a
   missing, malformed, attacker-shaped, or non-ASCII forged signature header
   fails closed (returns 401), never crashes the verify path.
4. **Command / path injection from a hostile repo.** A pull request whose tree
   carries unusual paths (path traversal segments, symlinks, control characters,
   oversized blobs) is filtered at ingest; the extractor is not invoked on the
   raw file system shape, and symlinks are skipped in `scan_repo`.
5. **Catastrophic-backtracking / pathological input.** Regex hot paths used in
   route, config, and schema parsing have bounded complexity (one measured
   ReDoS in `_HAPI_ROUTE` is covered by a bounded pathological-input regression gate).
6. **Stale-credential / token-revocation handling.** GitHub installation tokens
   that are revoked or rotated mid-flight are re-minted and the in-flight call
   is retried once; a genuine permission denial is not mistaken for a stale
   credential and does not enter a remint loop.

### What Veripsa Core DOES NOT defend against (out of scope)

1. **Network-layer DDoS.** Volumetric and protocol-layer DDoS protection lives
   at the platform layer (Render in front of the web service). Veripsa Core's
   defenses begin once a request reaches the application.
2. **Compromise of the host platform.** A compromise of GitHub, Render, or the
   managed Postgres provider that gives an attacker direct admin access to the
   underlying account or database is out of scope for application-level
   defenses. Report those issues to the respective vendor.
3. **Findings that require already having owner / administrator access** to
   the GitHub App registration, the Render account, or the database owner role.
4. **Customer-side branch-protection misconfiguration.** Veripsa's advisory
   verdict is a check + comment; whether it is treated as a hard merge gate is
   set by the customer's branch-protection policy. A repository whose policy
   ignores the check will not be hard-gated by Veripsa.
5. **Correctness of the code change itself.** Veripsa records cross-PR
   structural collisions; it does not assert that any one PR's code is correct
   in isolation, and it does not claim a green check means the PR is safe to
   land.

---

## Controls — and where they are enforced in the codebase

Every claim below is exercised by a release gate under `gates.d/*.gate`. The
gate suite runs end-to-end via `./run_gates.sh` and is required to be green
before deploy.

### Content-free posture

- **In-memory + render + alert path** — `gates.d/17-content_free_egress.gate`
  (test: `tests/test_content_free_egress.py`). Asserts that the stored graph,
  the rendered customer text, and any outbound alert payload carry only
  reference-shaped tokens (path / symbol / line / edge / count metadata),
  never a file body.
- **Stored DB egress** — `gates.d/159-content_free_db.gate` (test:
  `tests/test_content_free_egress_db.py`). Drives a real extractor → ingest →
  Postgres pipeline as `veripsa_app` and asserts every stored node/edge text
  column is reference-shaped.
- **Non-code path exclusion** — `gates.d/59-noncode_paths.gate`.

### Tenant isolation (PostgreSQL FORCE row-level security)

- **Two-tenant isolation proof** — `gates.d/27-tenant_isolation.gate` (test:
  `tests/test_tenant_isolation.py`). Two installations become two accounts that
  cannot see each other, including from the application role itself — `FORCE
  ROW LEVEL SECURITY` is set on every per-account table.
- **Cross-tenant consent (default OFF, SHADOW only)** —
  `gates.d/175-cross_tenant_consent.gate` (test:
  `tests/test_cross_tenant_consent.py`). A two-tenant FORCE-RLS proof that
  cross-repo reads require *bilateral* workspace consent; a forged session
  GUC, a forged membership row, and a forge-via-setter all leak nothing. Only
  the contract-key "consumed-by" fact crosses (no path / node / edge of the
  other tenant) and revocation is immediate.
- **Owner-side freshness lens is cross-tenant by design** —
  `gates.d/83-owner_freshness_xtenant.gate` (test:
  `tests/test_owner_freshness_xtenant.py`).
- **Backfill CLI keys by owner id, not install id** —
  `gates.d/82-backfill_tenant_key.gate`.

### Replay / forgery / signature robustness

- **Webhook signature constant-time compare, fail-closed on hostile headers**
  — `gates.d/132-webhook_signature_robust.gate` (test:
  `tests/test_webhook_signature_robust.py`). A forged non-ASCII
  `X-Hub-Signature-256` no longer crashes `hmac.compare_digest` with a
  `TypeError`; the verify path fails closed (False → clean 401) in constant
  time. The webhook secret is never logged on the reject path.
- **Webhook ordering** — `gates.d/11-webhook_ordering.gate`.
- **Auth lifecycle (token expiry, revocation, rate-limit, secret hygiene)**
  — `gates.d/19-auth_lifecycle.gate` (test: `tests/test_auth_lifecycle.py`).
  Asserts: early-revocation 401 remints + retries once everywhere including
  the tarball ingest; a stale-credential 403 (permission re-approval mid-life)
  also remints + retries once; a genuine permission 403 (`Resource not
  accessible by integration`) does NOT enter a remint loop; rate-limit 403 is
  not mistaken for a stale credential; no secret ever appears in a log or
  exception message.
- **Tamper resistance for account authority** —
  `gates.d/32-tamper_account_authority.gate`.

### Command / path injection / hostile inputs

- **Hostile path poisoning** — `gates.d/127-hostile_path_poison.gate`.
- **DoS-pathological inputs (ReDoS, oversized inputs)** —
  `gates.d/22-dos_pathological_inputs.gate`.
- **Ingest extraction safety** — `gates.d/21-ingest_extraction_safety.gate`.
- **Fork-PR untrusted ref handling** — `gates.d/28-fork_pr_path.gate` and
  `gates.d/104-rerun_fork_safety.gate`.
- **Migration safety** — `gates.d/33-migration_safety.gate`.

### Database role separation

- The application connects as `veripsa_app`, which is *not* the database
  owner and *cannot* bypass `FORCE ROW LEVEL SECURITY`. Its DSN
  (`VERIPSA_DSN`) is a hand-set secret, deliberately not auto-wired from the
  database owner connection string.
- The application has **no direct table grants** — it reads and writes only
  through `SECURITY DEFINER` "with_authority" gates (`enter_installation`,
  `act_for_claim`, `record_push`, `land_on_main`, `release_change_on_main`,
  `record_landing`, `purge_repo`, `prune_events`, and the read
  `collisions_on_main`). See `github-app/RUNBOOK.md` § Least-privilege for
  the per-call detail.
- `SECURITY DEFINER` revoke discipline is gated:
  `gates.d/142-sec_definer_revoke.gate`.

### Transport

- The application connects to its database with `sslmode=require` (see
  `render.yaml`). Inbound HTTP is terminated at the Render edge and reaches
  the app over the platform's internal network.
- Public HTTP-header and status-surface evidence is tracked in
  `docs/PUBLIC_SECURITY_BASELINE.md`. That checklist covers the marketing
  platform, the Core webhook/status service, the post-deploy header commands,
  and owner-side repository-security settings that cannot be proven from this
  repository alone.

---

## Authentication and identity

- **GitHub OAuth.** Veripsa Core authenticates the App via the GitHub
  Apps installation-token flow. There is no Veripsa-owned password store.
- **Opaque internal identifiers.** Per-account state is keyed off opaque
  identifiers derived from the GitHub account id (`ACCT-GH-<owner_id>`). The
  application does not store names, emails, or other PII.
- **What we do store about people.** Public git metadata only — the PR-author
  login that GitHub itself attaches to every push and PR (the same string
  already visible in the customer's own git history).
- **Billing path.** Veripsa Core is free while in early access. Future paid
  plans are expected through GitHub Marketplace. The Core repository covered by
  this policy never sees payment card data.

---

## Data retention and purge

- **Working set (paths + edges + claim/co-change state and live repo
  authority).** Purged immediately when the App is uninstalled, when a
  repository is removed from the installation, or when a repository is
  deleted. Repo-scoped removal keeps only a minimal content-free revocation
  marker so late webhook delivery cannot recreate the working set.
- **Operational push/landing telemetry (paths / SHAs / author logins — public
  git metadata).** Retained for a rolling window (30 days by default) and
  pruned nightly.
- **Curated advisory history and statements.** Append-only under the current
  contract and retained until an explicit account erasure. They are not part
  of the default 30-day push/landing prune.
- **GDPR Article 17 / CCPA right-to-erasure.** A full account-scoped hard
  delete is available via the documented operator path
  (`github-app/RUNBOOK.md` § "Offboarding / data purge") and is gated
  end-to-end by `tests/test_cochange_purge_erase_completeness.py`
  (`gates.d/133-cochange_purge_erase_completeness.gate`). The operator
  verification matrix for uninstall, repository removal, repository deletion,
  and erasure lives in `docs/OFFBOARDING_VERIFICATION.md`.

---

## Incident response

Provider-neutral operator guidance lives in `github-app/RUNBOOK.md` (deploy /
rollback, alert classes, the exact-allowlist emergency brake, and post-incident
verification). A mutation requires `--repo`, `--branch`, one or more `--service`
values, and `--yes`. The public-facing summary:

1. **Detection.** `/healthz`, `/readyz`, `/freshz` endpoints; the watchdog +
   the four ops-alert classes (worker stuck, queue depth, freshness stale,
   delivery failure).
2. **Containment.** The emergency-brake command suspends only the explicitly
   allowlisted compute services; in-flight inbox items are durably persisted before the
   202 response and recovered on next boot
   (`gates.d/133-durable_inbox.gate`).
3. **Eradication and recovery.** Function-only schema deltas can be deployed
   contention-free (the `CREATE OR REPLACE FUNCTION` path takes no table
   lock); a redeploy never drops tenant data because the schema apply is
   idempotent.
4. **Notification.** If a confirmed incident affected customer data,
   affected installations will be contacted via the GitHub App's installer
   account on a best-effort basis.

---

## Scope

In scope for this policy:

- The Veripsa GitHub App webhook server and its background worker
  (`github-app/`).
- The code-graph extractor and traffic-control engine
  (`code_graph_extract.py`, `_cg_*.py`, `db/`).
- The hosted deployment (the Render web service, cron, and managed Postgres
  described in `render.yaml` and `github-app/RUNBOOK.md`).

Out of scope:

- Vulnerabilities in third-party platforms Veripsa runs on (GitHub, Render,
  the managed Postgres provider). Report those to the respective vendor.
- Findings that require already having owner / administrator access to the
  host account, the App registration, or the database owner role.
- Separately distributed dashboard, billing, or hosted-platform components,
  which have their own security posture and are not covered here.

---

## Supported versions

The hosted deployment is intentionally suspended as of 2026-08-18, so there is
currently no live production version represented as supported. Security fixes for
the public source target the latest commit on `main`. Any future hosted resumption
must identify the exact deployed commit and re-establish the deployment verification
contract before it is described as supported.

---

## Coordinated disclosure

We aim to acknowledge reports within the windows above, keep you updated on
remediation progress, and credit reporters who wish to be credited once a fix
has shipped.
