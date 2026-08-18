# Low-friction trial guide

> Availability: the project-hosted service is intentionally suspended as of
> 2026-08-18. Do not begin a hosted trial until an explicit resumption notice.

Use this when someone understands the problem but is hesitant to install a new GitHub App on a real repository.

The goal is not to ask for trust up front. The goal is to let a team observe Veripsa Core in the smallest safe shape first, then decide whether it deserves more authority.

## The low-friction promise

Start with this posture:

```text
Install Veripsa Core on one selected repository, keep branch protection unchanged, and run it in advisory mode for a short observation window. Promote the check to required when it fits your workflow.
```

What that means:

- **Selected repository only.** Do not ask the team to install on every repository first.
- **Advisory-only first.** Do not add the Veripsa Core check to branch protection or rulesets during the first observation window.
- **No merge authority by itself.** Veripsa Core reports a GitHub check and, when useful, a PR comment. GitHub branch protection/rulesets are the only hard gate.
- **No source-body storage or display.** Veripsa Core may read repository content transiently to compute signals, but it does not store or display source file bodies or diff bodies.
- **No code writes.** The GitHub App permission model is for observing repository contents and writing PR/check/ACK surfaces, not editing customer source code.

## Recommended first trial

Run the first trial like this:

1. Pick one repository where parallel PRs actually happen.
2. Install Veripsa Core on that selected repository only.
3. Leave branch protection/rulesets unchanged.
4. Open, update, or let normal PRs continue.
5. Confirm a `Veripsa` check appears on PRs that target a covered branch.
6. Read Veripsa PR comments only when one appears; clean PRs may be check-only.
7. Track the observation window with the evidence table below.
8. Decide whether to keep advisory-only, promote to required-check mode, or uninstall.

## Seven-day evidence table

Use this table in a design-partner note, support thread, PR, or Slack summary.

| Metric | Value |
|---|---|
| Observation window | `YYYY-MM-DD` to `YYYY-MM-DD` |
| Repository shape | public/private, primary language, rough PR volume |
| PRs observed |  |
| Veripsa checks seen |  |
| Veripsa comments seen |  |
| `Clear` count |  |
| `Heads up` count |  |
| `Wait in line` / `action_required` count |  |
| `Unknown` count |  |
| ACK count |  |
| Useful warning count |  |
| Noisy warning count |  |
| Missing-check cases |  |
| Human note |  |

A useful warning is one where a maintainer says the signal changed behaviour: they waited, rebased in the suggested order, checked a named PR first, chose to ACK deliberately, closed a superseded PR, or avoided blind merge/rework.

A noisy warning is one where a maintainer says the signal did not represent coordination work they cared about.

## When to promote to required-check mode

Only consider required-check mode after the advisory trial has shown at least one of these:

- `Wait in line` caught a collision the team wanted to see before merge.
- `Unknown` prevented someone from treating unobserved code as safe.
- Maintainers agreed that ACK is a useful human coordination moment.
- AI coding agents are opening enough PRs that humans want a merge-lane signal.

Required-check mode is still enforced by GitHub branch protection/rulesets, not by Veripsa Core alone. Use `docs/REQUIRED_CHECK_SETUP.md` for setup and backout.

## What to say to a skeptical installer

Use this short answer:

```text
Start with one selected repository in advisory mode. Keep branch protection unchanged while you watch the GitHub checks and comments on real PRs for a week, then make the Veripsa Core check required when it fits your workflow. Veripsa Core does not store or display source file bodies or diff bodies.
```

Do not say:

```text
Veripsa never reads code.
```

Say instead:

```text
Veripsa Core may read repository content transiently to compute signals, but source file bodies and diff bodies are not stored, displayed, or used as code-review output.
```

## Exit paths

If the trial is not useful:

1. Remove the GitHub App from the selected repository.
2. Keep the evidence table so we can classify the miss: wrong repository shape, low PR concurrency, unsupported language area, noisy graph signal, missing check, or unclear copy.
3. Do not convert a weak trial into a stronger claim.

If the trial is useful:

1. Capture one public-safe sentence from the maintainer.
2. Capture counts from the evidence table.
3. Decide whether the repository stays advisory-only or moves to required-check mode.
4. Update public copy only with facts that are actually observed.
