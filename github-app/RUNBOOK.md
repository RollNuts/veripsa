# Public operator runbook

This public source mirror intentionally excludes Veripsa production service identifiers,
deployment receipts, incident selectors, credentials, and private GitHub Actions history.
Never copy production values into issues, pull requests, workflow inputs, logs, or examples.

## Local verification

Run the complete release gate suite against an isolated local PostgreSQL instance:

```bash
bash run_gates.sh
```

The suite is fail-closed. A failing or incomplete gate is not a release result.

## Least privilege

The hosted application uses a GitHub App installation token and a non-owner PostgreSQL
role. Keep the App permissions and subscribed events aligned with the public product
contract in `README.md` and `SECURITY.md`. Store the webhook secret, App private key,
database DSNs, and provider API credentials only in the deployment platform's secret
store. Do not put them in source-controlled configuration.

## Deploy and rollback

Build from an immutable commit SHA. Before routing traffic, prove that the deployed
artifact reports that exact SHA, the schema contract is compatible, `/healthz` is live,
and `/readyz` is ready. Preserve the previous immutable artifact as the rollback target.
Use the content-free template in `docs/RELEASE_DEPLOY_LEDGER.md`; record no provider IDs,
tokens, payloads, selectors, or customer data.

The public mirror contains no production mutation workflow. Operators must implement
their own protected deployment lane and keep untrusted pull requests away from
self-hosted runners and production credentials.

## Emergency brake

Implement the emergency brake in a private, protected operator surface. It should suspend
compute while leaving PostgreSQL intact, display its resolved target set before mutation,
and require an exact allowlist of repository, branch, service name, and service type so an
unrelated service cannot be stopped. No provider mutation helper is shipped in this mirror.

## Offboarding / data purge

GitHub installation lifecycle events revoke installation liveness. Account erasure and
retention operations must run through the schema's authority-checked functions and be
verified as the least-privilege application role. Never substitute direct table writes
for the gated path.

## Destructive schema changes

Additive idempotent schema changes use the generation contract documented in
`docs/SCHEMA_GENERATION_MANIFEST.md`. A destructive migration requires a tested backup,
an explicit maintenance window, a rollback artifact, and a restore rehearsal. It must
not be inferred from an ordinary application deploy.

## Incident handling

Suspend ingress before stopping workers when practical, preserve the database, and use
read-only evidence before any replay or redelivery. A provider POST is never retried
after an ambiguous response until a durable status endpoint proves that it was not
accepted. Keep all incident identifiers out of public logs and reports.
