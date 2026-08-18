# Required-check setup guide

> Availability: the project-hosted service is intentionally suspended as of
> 2026-08-18. Do not configure Veripsa as a required check while it is suspended.

Veripsa Core is advisory by default.

It only becomes a merge gate when the repository owner configures GitHub branch protection or rulesets to require the Veripsa Core check.

## Recommended rollout order

Do not make the first install a hard gate by default.

Use this order:

1. Install Veripsa Core on one selected repository.
2. Leave branch protection/rulesets unchanged.
3. Observe PR checks/comments for a short trial window.
4. Count useful warnings, noisy warnings, `Unknown`, and missing-check cases.
5. Promote to required-check mode only when maintainers agree the signal is worth enforcing.

For the advisory trial script and evidence table, use `docs/LOW_FRICTION_TRIAL.md`.

## Concepts

### Advisory mode

In advisory mode, Veripsa Core reports its state through GitHub checks and PR comments, but GitHub does not block a merge solely because Veripsa Core reports `action_required`.

Use this when:

- evaluating Veripsa Core for the first time
- running on a repository where required checks are not available
- testing PR traffic signals before enforcing them
- lowering the trust barrier for a team that is not ready to let a new check affect merges

### Required-check mode

In required-check mode, the repository owner configures GitHub branch protection or rulesets to require the Veripsa Core check before merging.

In this mode, GitHub is the enforcement layer. Veripsa Core reports state; branch protection/rulesets decide whether a merge can proceed.

Use this when:

- teams want `action_required` to hold merge until ACK or resolution
- AI agents create PRs and humans want an explicit gate
- main branch stability matters more than merge speed
- the advisory trial produced useful warnings the team wants to make visible in policy

## What each state means

| State | Meaning | Required-check implication |
|---|---|---|
| `clear` | No relevant open-PR collision is visible in the analyzed surface | Should not block on Veripsa Core |
| `warn` / `Heads up` | Related or nearby PR traffic exists | Usually informational unless policy changes |
| `action_required` / `Wait in line` | Human coordination or ACK is needed | Should hold merge when Veripsa Core is required |
| `unknown` | Veripsa Core lacks enough signal to judge | Do not treat as clear; policy may vary |

`clear` is not a proof that the PR is correct.

`unknown` is not an error by default and is not the same as clear.

## Setup checklist

Because GitHub UI and plan behavior can change, use this as a conceptual checklist and verify against GitHub's current branch protection/ruleset UI.

1. Install Veripsa Core on the repository.
2. Open or update a small PR.
3. Confirm the Veripsa Core check appears in the PR checks/status area.
4. Note the exact check name shown by GitHub.
5. Finish the advisory observation window first unless the team has already decided to enforce.
6. Open repository branch protection or rulesets for the target branch.
7. Add the Veripsa Core check as a required status check.
8. Save the rule.
9. Open or update a PR that produces a Veripsa Core state.
10. Confirm GitHub shows the required check in the merge box.
11. Confirm merge behavior matches the configured policy.

## Verification path

### Verify check creation

On a PR:

```bash
gh pr checks <PR_NUMBER>
```

Look for the Veripsa Core check in the output.

If it does not appear:

- confirm the GitHub App is installed on the repository
- confirm the PR targets a covered branch
- push a small update to the PR branch
- check GitHub's Checks tab/status rollup
- do not treat missing output as `clear`

### Verify required-check behavior

After adding the Veripsa Core check as required:

1. Create or identify a PR with `action_required`.
2. Confirm the PR merge box shows the Veripsa Core required check as blocking.
3. Add ACK only if a human decides to proceed for the current warning snapshot.
4. Confirm the required check updates after ACK or after the related PR lands/withdraws.

## Backout path

If the team misconfigures required checks and blocks urgent work:

1. Remove Veripsa Core from required checks in branch protection/rulesets, or temporarily adjust the rule.
2. Keep the Veripsa Core PR comment/check visible for advisory review.
3. Record why enforcement was disabled.
4. Re-enable the required check after the immediate incident is resolved.

Do not delete the GitHub App installation merely to bypass a single urgent PR unless that is the owner's explicit decision.

## Copy guardrails

Use:

```text
Veripsa Core is advisory by default. To make action_required hold merges, add the Veripsa Core check to GitHub branch protection or rulesets where your GitHub plan supports required checks.
```

Also safe:

```text
Start with one selected repository in advisory-only mode. Leave branch protection unchanged during the first observation window, then promote the check only if the warnings are useful.
```

Avoid:

```text
Veripsa Core blocks bad merges automatically.
```

Avoid:

```text
Veripsa Core guarantees this PR is safe.
```

## GitHub plan note

GitHub branch protection and ruleset capabilities may vary by repository visibility, plan, and organization settings.

Do not claim that required checks are available in every repository shape. When in doubt, point users to GitHub's current branch protection/rulesets documentation and their repository settings.

## Open follow-ups

- #630 first-run UI/UX
