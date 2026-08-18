# Support

The hosted Veripsa service is intentionally suspended as of 2026-08-18. Install and
first-run guidance below applies only after an explicit resumption notice. During the
suspension, use the offline evaluator and public source; do not expect GitHub App checks
or comments to appear.

Use this page for Veripsa Core support and issue-routing.

## Product support

For install, dashboard, plan, or first-run questions, start at:

- https://veripsa.com
- `docs/FIRST_RUN.md`
- `docs/LOW_FRICTION_TRIAL.md`
- `docs/TROUBLESHOOTING.md`
- `docs/SUPPORT_DEBUG_FLOW.md`
- `docs/SUPPORT_RESPONSE_TEMPLATES.md`

If the issue is about a public repository and does not include sensitive data, open a GitHub issue in this repository.

## Low-friction first trial

If someone is hesitant to install a new GitHub App, recommend the selected-repository advisory trial:

```text
Install Veripsa Core on one selected repository, leave branch protection unchanged, and watch the GitHub checks/comments for a week. If the warnings are not useful, uninstall it.
```

Do not ask a first-time installer to make Veripsa Core a required check before they have observed useful signal. Required-check mode is an optional promotion path controlled by GitHub branch protection/rulesets.

## What to include

Please include:

- repository owner and repository name
- pull request number
- branch and target branch
- GitHub check-run URL, if available
- Veripsa Core comment URL, if available
- expected signal and actual signal
- whether the pull request is draft or ready
- whether branch protection requires the Veripsa check
- timestamp and GitHub check name, if available

Do not include source file bodies, diff bodies, credentials, secrets/tokens, webhook payloads, GitHub App private keys, installation access tokens, or customer secrets.

## Missing check or comment

A missing Veripsa check or PR comment is `not observed`, not `Clear`.

Before reporting, check the PR conversation, GitHub checks UI, and `gh pr checks <PR_NUMBER>` if you use the GitHub CLI. Then report safe metadata only.

After an explicit service-resumption notice, you can also check `/statusz` for
broad service health. An operational `/statusz`
does not prove the PR is clear; it only helps separate service-wide degradation
from repo-specific installation, branch, permission, or timing issues.

See `docs/TROUBLESHOOTING.md`, `docs/SUPPORT_DEBUG_FLOW.md`, and `docs/TRUST_STATUS.md` for the detailed first-run, support-triage, and status-boundary checklist.

For maintainers replying to common support reports, use `docs/SUPPORT_RESPONSE_TEMPLATES.md`.

## Privacy boundary

Do not include source file bodies, diff bodies, credentials, secrets or tokens,
webhook payloads, GitHub App private keys, installation access tokens, webhook
secrets, customer data, or private repository coordinates in support reports.

## Security reports

Do not file security issues publicly.

For vulnerability reports, follow `SECURITY.md`. Security reports should include the minimum metadata needed to reproduce the issue without exposing secrets.

## Billing

Veripsa Core is currently free-first for the GitHub App launch path. Direct checkout is not part of the active launch path. If paid plans are added later, the expected direction is GitHub Marketplace billing.

## Scope reminder

Veripsa Core is advisory by default. It posts a GitHub check and PR comment. The repository's branch protection configuration decides whether that check becomes a hard merge gate.
