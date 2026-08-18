# Public operations index

This source mirror contains public contracts and local verification material. It
does not contain production credentials, service identifiers, incident receipts,
private Actions history, or production mutation workflows.

The project-hosted service is intentionally suspended as of 2026-08-18.

## Start here

- `README.md` — product boundary, offline evaluator, and current availability.
- `docs/FIRST_RUN.md` — signal semantics and first-run expectations for a future
  hosted-service resumption.
- `docs/TROUBLESHOOTING.md` — public-safe troubleshooting.
- `SUPPORT.md` — support routing and data-sharing limits.

## Security and data boundary

- `SECURITY.md` — threat model and vulnerability reporting.
- `docs/PUBLIC_SECURITY_BASELINE.md` — public release security checklist.
- `docs/OFFBOARDING_VERIFICATION.md` — uninstall and erasure contract.
- `docs/TRUST_STATUS.md` — public status versus operator evidence.

## Build and deploy

- `github-app/RUNBOOK.md` — provider-neutral least-privilege and incident rules.
- `docs/DEPLOYMENT_VERIFICATION.md` — merged/deployed/verified state contract.
- `docs/SCHEMA_GENERATION_MANIFEST.md` — schema generation contract.
- `docs/RELEASE_DEPLOY_LEDGER.md` — content-free ledger template.
- `render.yaml` — disabled-by-default example deployment blueprint.

## Local verification

```bash
python3 scripts/public_snapshot_gate.py
python3 tests/test_secret_hygiene.py
python3 -m compileall -q .
python3 tests/test_auth_lifecycle.py
python3 tests/test_split_advice.py
```

The deeper `bash run_gates.sh` suite requires local PostgreSQL. A partial or
failing run is not a release result.
