# Agent operating rules for Veripsa Core

These rules apply to humans and coding agents working in this public source
repository.

## Product boundary

Veripsa Core is pre-merge PR traffic control for repositories where people and
coding agents open pull requests in parallel. It is advisory by default. It is
not a code reviewer, CI replacement, merge queue replacement, correctness proof,
or automatic conflict resolver.

The project-hosted service is intentionally suspended as of 2026-08-18. Do not
claim that a hosted check, comment, status endpoint, install flow, or support SLA
is currently available. The offline evaluator and source remain available.

## Change discipline

- Keep changes small and inside the stated task scope.
- Use a branch and pull request; never fall back to a direct `main` write after a
  branch-targeted write fails.
- Do not force-push, rewrite history, or weaken security controls unless the
  repository owner explicitly asks for that exact action.
- Treat absent Veripsa output as `not observed`, never as `Clear`.
- Call out authentication, GitHub App permissions, webhook verification,
  database migrations, RLS, `SECURITY DEFINER`, retention, erasure, privacy,
  licensing, and source/diff handling as high-risk changes.

## Public boundary

Never commit or paste credentials, provider resource identifiers, webhook
delivery identifiers, incident selectors, customer data, private repository
names, source bodies, diff bodies, production URLs, or local user paths.

Before opening a pull request, run:

```bash
python3 scripts/public_snapshot_gate.py
python3 tests/test_secret_hygiene.py
python3 -m compileall -q .
```

Run the smallest relevant behavioral tests too. `bash run_gates.sh` is the
deeper local suite and requires an isolated PostgreSQL environment.

## Pull request record

Include the problem, scope, validation, deployment impact, and rollback. If a
hosted Veripsa signal is unavailable, record `not observed`; do not manufacture
or infer a result.
