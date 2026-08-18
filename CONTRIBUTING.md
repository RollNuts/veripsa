# Contributing

Veripsa welcomes focused bug reports, security reports, documentation fixes, and
well-scoped pull requests.

Before opening a pull request:

1. Read `SECURITY.md`. Report vulnerabilities through private vulnerability
   reporting, never a public issue.
2. Do not include credentials, provider resource identifiers, webhook delivery
   identifiers, customer data, private repository names, payloads, or source bodies.
3. Use your public GitHub handle as the Git author and committer name, with the
   matching GitHub-provided `users.noreply.github.com` address. Apply the same
   rule to sign-off and co-author trailers; personal names and email addresses
   are rejected by CI.
4. Run `python3 scripts/public_snapshot_gate.py` and
   `python3 tests/test_secret_hygiene.py`.
5. Run the smallest relevant tests. Runtime changes should also pass
   `bash run_gates.sh` in an isolated local environment.
6. Explain the problem, evidence, scope, validation, deployment impact, and rollback.

The public repository contains no production mutation workflow and accepts no
production credentials. Maintainers may close changes that weaken content-free
boundaries, tenant isolation, fail-closed behavior, or supply-chain controls.
