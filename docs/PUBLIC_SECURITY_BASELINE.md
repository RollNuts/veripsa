# Public security baseline

This checklist applies to a public source release and to any future hosted
service resumption. The hosted service is intentionally suspended as of
2026-08-18; no endpoint availability is claimed here.

## Repository controls

- Publish from a new, squashed, sanitized root commit. Do not expose the private
  development repository's history, pull requests, issues, Actions logs,
  artifacts, environments, secrets, variables, or refs.
- Run public CI only on GitHub-hosted runners with read-only workflow
  permissions. Pin every third-party Action to an approved immutable SHA.
- Enable secret scanning, push protection, Dependabot alerts, private
  vulnerability reporting, and branch protection.
- Never attach a persistent self-hosted runner to untrusted public pull requests.
- Keep production credentials, provider identifiers, customer data, and
  mutation workflows out of this repository.

## HTTP contract for a future deployment

Public JSON endpoints must set at least:

- `Content-Security-Policy: default-src 'none'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'`
- `Strict-Transport-Security: max-age=63072000; includeSubDomains; preload`
- `X-Frame-Options: DENY`
- `X-Content-Type-Options: nosniff`
- `Referrer-Policy: no-referrer`
- `Permissions-Policy: camera=(), microphone=(), geolocation=(), browsing-topics=()`
- `Cache-Control: no-store`

`/statusz` and `/freshz` may expose only coarse, aggregate state. They must not
return repository, branch, account, commit, pull-request, delivery, Check Run,
comment, source, diff, secret, or token values. `/healthz` and `/readyz` are
operator probes and should not be treated as public support evidence.

## Ingress and data controls

- Bound request bodies before parsing and verify webhook signatures before
  accepting work.
- Keep GitHub installation routing tenant-scoped.
- Connect the runtime with the least-privilege application database role; never
  expose the owner/migrator credential to the serving process.
- Enforce RLS, `SECURITY DEFINER` revoke discipline, bounded queues, fail-closed
  freshness, and authority-checked erasure.
- Store no source or diff bodies as product output.

## Release evidence

Run `python3 scripts/public_snapshot_gate.py`,
`python3 tests/test_secret_hygiene.py`, the relevant behavioral tests, and the
provider-neutral checks in `docs/DEPLOYMENT_VERIFICATION.md`. Record only
content-free evidence. An incomplete check is not a passing result.
