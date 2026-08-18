# Troubleshooting Veripsa Core

Use this when a first PR does not show the signal you expected.

## Safety first

Do not paste source file bodies, diff bodies, credentials, secrets/tokens, webhook payloads, GitHub App private keys, installation access tokens, webhook secrets, or customer secrets into public issues.

Safe metadata is usually enough:

- repository owner/name
- pull request number
- branch name
- target branch
- check run URL
- Veripsa Core comment URL
- check name
- timestamp
- file paths
- observed Veripsa signal

For suspected vulnerabilities, follow `SECURITY.md` instead of opening a public issue.
For support triage language and safe escalation levels, see `docs/SUPPORT_DEBUG_FLOW.md`.

## No Veripsa check appears

Treat this as `not observed`, not as `Clear`.

A missing check can mean several different things:

- the GitHub App is not installed on that repository
- the live GitHub App registration is missing a required permission/event, or
  still has an unexpected legacy permission/event that should be removed
- the PR branch or target branch is not covered
- the webhook delivery has not been processed yet
- the PR changed only unsupported or intentionally skipped surfaces
- the App is delayed by worker or delivery recovery backlog
- the check exists on a GitHub Checks surface that your command did not query
- the service is not running the expected deployed commit yet

Do not merge solely because no Veripsa result appeared.

## First checks

1. Confirm the App is installed on the repository.
2. Confirm the PR targets the branch you expect, usually `main`.
3. Wait at least 3 minutes for webhook processing, especially after a deploy or backlog recovery.
4. Check the broad service state at `/statusz`. A degraded status suggests a service-level issue; an
   operational status does not prove the PR is clear.
5. Check both the PR conversation and GitHub checks UI.
6. If you use the GitHub CLI, check both comments and checks:

```bash
gh pr view <PR_NUMBER> --comments
gh pr checks <PR_NUMBER>
```

If both are empty, report it as missing signal with safe metadata only.

A clean PR may have a `Veripsa` check with no PR comment. That is normal. PR
comments are used when the result needs coordination, acknowledgement,
partial-analysis disclosure, or stale-warning clearance.

## App registration drift

If the App is installed and the service is healthy, verify that the live GitHub
App registration still matches the product contract:

```bash
python3 github-app/scripts/verify_app_registration.py
```

Interpret the result before investigating runtime logs:

- **PASS** means the live registration has the expected events and
  permissions. Continue with webhook delivery, durable inbox, worker logs, and
  the Checks API observer.
- **FAIL with missing `check_suite`, `check_run`, or `checks: write`** means the
  App may be unable to recover a head that has a queued Veripsa suite but no
  check-run. Fix the GitHub App registration first, then rerun post-deploy
  smoke.
- **FAIL with extra `issues`, `issue_comment`, `sub_issues`, or `issues`
  permission** is a Marketplace/product-boundary drift. Veripsa Core does not
  manage Issues as a product surface. Remove the legacy registration item in
  GitHub App settings, then rerun the script.

This check reads GitHub's public App registration endpoint. It does not require
secrets and does not inspect customer repository content.

## Draft PRs

Draft PRs are still useful as a scout window. Veripsa may analyze them so authors can see possible contention early.

A draft PR should not unnecessarily block a ready PR just because both are in the same area. If a draft-related result looks too strong, report the PR number and observed signal.

## `Unknown` is not `Clear`

`Unknown` means Veripsa does not have enough signal to call the PR clear. It can happen when coverage is limited, a file type is unsupported, or a coupling is suppressed to avoid noisy over-warning.

Do not treat `Unknown` as safe. Inspect manually or ask the related author to coordinate.

## `Wait in line`

`Wait in line` means Veripsa sees a direct collision or a landing-order risk. The usual move is to let the named PR land first, then rebase or revise.

Only use `veripsa-ack` after a human has reviewed the specific warning and deliberately decided to proceed. It is not a mute button.

## Required checks on private repositories

Veripsa is advisory by default. GitHub only blocks merge when branch protection requires the Veripsa check.

On private repositories, required status checks depend on the customer's GitHub plan. If the plan does not allow required checks, Veripsa can still post visible guidance but cannot become the hard gate by itself.

## After deploys

Merging a fix does not always mean production is running it. This repository currently uses explicit deploys.

For operator-side verification, use `docs/DEPLOYMENT_VERIFICATION.md` and confirm:

- the expected commit is deployed
- `/healthz` reports the expected version
- `/statusz` is operational or names a coarse degradation
- post-deploy smoke passes
- a canary PR receives an observable Veripsa check and/or comment

## Reporting a missing signal

Use the bug report or support issue form. Include safe metadata only:

- repository owner/name
- pull request number
- PR state: draft or ready
- target branch
- timestamp
- whether any Veripsa check/comment appeared
- check run URL, if available
- Veripsa Core comment URL, if available
- expected signal and actual signal
- whether branch protection requires Veripsa

If this is about this repository's dogfood path, see #573 and #575 for current investigation state.
