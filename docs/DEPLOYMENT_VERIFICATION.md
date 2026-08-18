# Deployment verification

The project-hosted Veripsa service is intentionally suspended as of 2026-08-18.
This document defines the contract for any future deployment; it is not evidence
that a deployment is live.

## State model

Use exactly these states:

1. **Merged** — source is on the default branch. Nothing is claimed about a
   runtime.
2. **Deployed** — an immutable artifact for the exact commit has been promoted.
3. **Verified** — the runtime reports the exact commit, schema compatibility,
   readiness, and the expected public-safe behavior.

Never collapse these states into “shipped.” A green build or provider deploy
record alone is not runtime verification.

## Pre-deploy

- Build from an immutable commit SHA.
- Run the public snapshot and credential hygiene gates.
- Run relevant behavioral tests and the schema contract checks.
- Confirm the production credential boundary: serving processes receive only
  the least-privilege application DSN; schema migration uses a separate,
  short-lived owner/migrator context.
- Confirm rollback points to the preceding immutable artifact.

## Post-deploy

- Prove the runtime reports the intended commit.
- Verify `/healthz` and `/readyz` without publishing their detailed output.
- Verify the public `/statusz` and `/freshz` response shapes remain aggregate and
  content-free.
- Verify webhook signature rejection, bounded ingress, durable queue progress,
  and worker freshness with synthetic evidence.
- Verify the schema generation marker and application contract agree.
- Record no provider resource IDs, deployment IDs, webhook delivery IDs,
  incident selectors, customer coordinates, payloads, source, or diff content.

## Failure and ambiguity

A timeout or ambiguous provider response is not permission to retry a mutation.
Read durable status first. If the exact artifact or state cannot be proven, stop,
preserve the database, and use the previous verified artifact only when its
schema contract remains compatible.

Use `docs/RELEASE_DEPLOY_LEDGER.md` for a content-free record. The public source
repository intentionally contains no production mutation workflow.
