# First run guide

> Availability: the project-hosted service is intentionally suspended as of
> 2026-08-18. This guide applies only after an explicit resumption notice.

After a future resumption, use this after installing Veripsa Core on a repository
for the first time.

Veripsa Core is a GitHub App for pre-merge cross-PR collision control. It does not replace CI, review, or a merge queue. It adds a traffic signal for open PRs that are trying to land in the same area.

## First-run posture

For the first install, start with the smallest safe shape:

1. Install on **one selected repository** first.
2. Leave branch protection/rulesets unchanged.
3. Run Veripsa Core **advisory-only** until the signal earns trust.
4. Promote the check to required only after maintainers agree it is useful.

This reduces both risks: the technical blast radius is one repository, and the workflow blast radius is advisory observation instead of immediate merge enforcement.

## Install

1. Open `https://veripsa.com`.
2. Choose **Install on GitHub**.
3. Select the repositories Veripsa Core should watch. For a first trial, choose one repository instead of all repositories.
4. Open or update a pull request on a tracked branch.

Within a few minutes, the PR should receive a `Veripsa` check. A Veripsa PR
comment is expected only when the result needs coordination, acknowledgement,
partial-analysis disclosure, or stale-warning clearance. A clean PR may be
check-only with no comment.

If no check appears, do not treat that as `Clear`. Use [`docs/TROUBLESHOOTING.md`](./TROUBLESHOOTING.md) and report the missing signal with safe metadata only.

## Recommended observation window

Run the first trial as observation, not enforcement:

- Do not add the Veripsa Core check to branch protection/rulesets yet.
- Track how many PRs receive checks.
- Track how many comments are useful vs noisy.
- Treat `Unknown` as a real signal: Veripsa did not have enough evidence to call the PR clear.
- Use `docs/LOW_FRICTION_TRIAL.md` for the seven-day evidence table.

The decision after the observation window should be explicit:

| Outcome | Next step |
|---|---|
| Warnings were useful | Keep advisory mode or move to required-check mode. |
| Warnings were noisy | Keep evidence, classify the miss, and do not overclaim. |
| No relevant PR concurrency happened | Extend the observation window or try a busier repository. |
| Missing checks happened | Use troubleshooting before judging value. |
| Team is uncomfortable with the App | Uninstall from the selected repository. |

## Signals

| Signal | Meaning |
|---|---|
| `Clear` | No cross-PR collision is visible in the current covered scope. |
| `Heads up` | Another open PR touches a related area. |
| `Wait in line` | A direct collision is ahead. |
| `Unknown` | Veripsa does not have enough signal to call this clear. |

`Unknown` is deliberately separate from `Clear`. If Veripsa cannot see enough, it should say so instead of claiming safety.

## Agent usage rule

If an AI coding agent helps with PR work, add a repository rule that says the agent must read the Veripsa check, and any Veripsa PR comment if one exists, before treating the PR as ready to land.

Recommended wording:

```markdown
Before a PR lands, read the Veripsa check and any Veripsa PR comment. Do not treat `Unknown` as `Clear`. If Veripsa says `Wait in line`, read the named PR first. If no Veripsa check appears, treat it as `not observed`, not as `Clear`. Use `veripsa-ack` only after a human has reviewed the specific warning.
```

## Skeptical-installer answer

Use this when a maintainer is worried about installing a new GitHub App:

```text
Start with one selected repository in advisory mode. Keep branch protection unchanged while you watch the GitHub checks and comments on real PRs for a week, then make the Veripsa Core check required when it fits your workflow. Veripsa Core does not store or display source file bodies or diff bodies.
```

Do not say that Veripsa Core never reads code. The accurate boundary is that source file bodies and diff bodies are not stored, displayed, or used as code-review output; repository content may be read transiently to compute the traffic signal.

## Notes

- Veripsa Core is advisory by default. A hard gate is controlled by the repository's branch protection settings.
- On private repositories, required checks depend on the customer's GitHub plan.
- Source file bodies and diff contents are not stored, displayed, or used as code-review output.

See `SECURITY.md` for the full security posture and threat model.
