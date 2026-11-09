# Contributing

Veripsa welcomes focused bug reports, security reports, documentation fixes, and
well-scoped pull requests.

日本語の参加者も歓迎します。[無料のローカルデモ](examples/structural-overlap/README.md)
で出力を確かめ、[解析報告フォーム](https://github.com/RollNuts/veripsa/issues/new?template=analysis_report.yml)
から誤検出・検出漏れ・解析不備・再現結果を報告できます。コード修正なしの
最小再現、期待値の説明、ドキュメント修正も貢献です。

## Try the smallest local path

Use Git, Python 3.12, and a POSIX shell (macOS/Linux instructions):

```sh
git clone https://github.com/RollNuts/veripsa.git
cd veripsa
python3.12 -m venv .venv
. .venv/bin/activate
python examples/structural-overlap/demo.py
python -m unittest discover -s tests -p 'test_structural_overlap_demo.py' -v
# Optional: score the same four labeled review pairs.
python examples/structural-overlap/benchmark.py
```

This path uses the standard library and disposable synthetic Git repositories.
It needs no App installation, account, database, package download, or model call
after cloning. It compares review candidates and execution outcomes; a passing
demo is not a safe-merge verdict. See the [expected output](examples/structural-overlap/expected-output.json).
The hosted GitHub App is suspended, so record its signal as `not observed`.

## Report an analysis problem or contribute evidence

Search existing issues first. Use the [analysis report form](https://github.com/RollNuts/veripsa/issues/new?template=analysis_report.yml)
for false-positive / false-negative relationships, language/parser gaps,
performance, reproducible experiments, and new analysis-method proposals. Use
the existing bug or support forms for other source/usage problems.

Include the public Core commit or release, input commit or synthetic fixture,
language, Python/OS and parser versions, exact command, expected relationship,
actual result, and a small reproduction. Prefer a link to a small self-authored
synthetic fixture or a permitted public source permalink; do not paste source
or diff bodies, private identifiers, raw graphs, or unreviewed logs into issues.
Explain the definition/use or other evidence behind the expected relationship.
A working change that has a real dependency is not automatically a false
positive; a missing edge is not automatically proof of no dependency.

For experiments, define the candidate universe, expected review points and how
they were selected, complete candidate counts, and unreviewed or unresolved
cases. Separate structural evidence from historical correlation and runtime
failure. Report missing measurements as unmeasured. See the
[research index](docs/RESEARCH_INDEX.md) for existing evidence and its boundaries.

For performance reports, use the same input/analyzer commits, environment,
command, repetitions, elapsed-time scope, and peak-memory method. Omit private
paths and include optional parser availability. A timeout alone does not locate
the cause. Proposals may state that an experiment has not run; distinguish the
hypothesis, proposed comparison, and expected falsifying result from observations.

Suspected vulnerabilities in Core go through [private security reporting](SECURITY.md#reporting-a-vulnerability).
Do not use the analysis form for unpublished vulnerabilities in another project;
follow that project's disclosure policy. Public research discussion is about
permitted examples and methods, not confidential targets or findings.

## Small contributions to start with

- Reproduce one documented command and report the actual environment and outcome.
- Reduce a parser miss or incorrect relationship to a small synthetic fixture,
  with an independently explained expected edge or unresolved state.
- Correct an explanation, broken link, or English/Japanese translation.
- Add a counterexample that reveals a limit; an honest failure is useful evidence.

These are entry points, not claims that pre-labelled tasks are available. A
short issue can agree the acceptance criteria before a larger implementation.

## Make a focused pull request

Fork the repository, clone your fork for contribution work, and create a branch
from current `main`. Use your public GitHub handle and GitHub-provided noreply
address for local commit identity, following the rules below. Do not change
branch protection, required checks, or security settings to make a push succeed.

Read [AGENTS.md](AGENTS.md) and the [code of conduct](CODE_OF_CONDUCT.md), then
check the relevant existing contract before changing behavior:

| Change | Contract / validation entry point |
| --- | --- |
| Graph extraction or uncertainty | [Graph schema contract](docs/GRAPH_SCHEMA_CONTRACT.md), [`cg_schema_contract.py`](cg_schema_contract.py), and the nearest existing extractor test. |
| Co-change counts, filters, or history updates | [Analysis inventory](docs/ANALYSIS_ASSETS.md#history-update-limits), [`_cg_cochange.py`](_cg_cochange.py), and relevant `test_cochange*` tests. |
| Local demo / expected output | [Demo guide](examples/structural-overlap/README.md) and the 13-test command above. Preserve compatible, missed, and incidental relationships alongside successful examples. |
| Persistence / PR runtime | [Architecture](ARCHITECTURE.md), [graph contract](docs/GRAPH_SCHEMA_CONTRACT.md), and the [isolated PostgreSQL test recipe](docs/ANALYSIS_ASSETS.md#reproduce-the-postgresql-checks). |

Add the smallest regression that explains the failure and the expected behavior.
For an accuracy-related change, show which expected relationships change and
which existing counterexamples remain; do not improve a score by silently
dropping cases or tuning to the same labels used for the claim. Link the issue
and public fixture in the PR. Larger research or schema changes need an agreed
scope before implementation.

Other languages and some configuration formats need optional parsers. Use the
existing [dependency manifest](requirements.txt), including its `--no-deps`
strategy, when those tests require them. Missing grammars mean reduced coverage,
not a successful full-language test. No additional dependency is needed for the
first local path above.

## Required checks and review record

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
   `python3 tests/test_secret_hygiene.py`, plus
   `python3 -m compileall -q .`.
5. Run the smallest relevant tests. Runtime changes should also pass
   `bash run_gates.sh` in an isolated local environment.
6. Explain the problem, evidence, scope, validation, deployment impact, and rollback.

The public repository contains no production mutation workflow and accepts no
production credentials. Maintainers may close changes that weaken content-free
boundaries, tenant isolation, fail-closed behavior, or supply-chain controls.

[Public CI](.github/workflows/public-ci.yml) runs the publication boundary and a
focused suite; it does not run every PostgreSQL or optional-language gate.
Use the PR template to state commands and results, unrun checks and their reason,
deployment impact, and rollback. Submit against `main` and respond to review;
do not write directly to `main` or infer hosted availability from merged code.

## Credit and reuse

Keep reports and fixes linked through issues, tests, and PRs so the reasoning and
contribution remain inspectable. Maintainers should preserve accurate commit or
co-author credit when squashing, using the public handle/noreply identity rules
above. For an article or research summary, ask before separately crediting a
reporter by handle and link the relevant public contribution when permitted.
Do not invent external participation or imply that a contribution guarantees
publication, payment, or rights to another person's finding.

The project remains [BSL 1.1 source-available](LICENSE). Review its reuse terms;
do not assume an OSI-approved license or add a license change to an unrelated PR.
Third-party examples must have compatible permission and attribution. No CLA,
license transfer, or bounty-allocation agreement is introduced by this guide.
