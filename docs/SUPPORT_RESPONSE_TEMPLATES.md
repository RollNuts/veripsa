# Support response templates

> Availability: the project-hosted service is intentionally suspended as of
> 2026-08-18. Hosted-install templates below are dormant until an explicit
> resumption notice. Public issues must use synthetic examples only.

Use these templates when responding to public issues or install/support reports. Keep the response content-free: ask for metadata, not source code.

Do **not** ask users to paste source file bodies, private diff contents, credentials, private keys, access tokens, webhook secrets, or customer secrets.

## Hesitant first-time installer

You do not need to trust Veripsa Core as a merge gate on day one.

The lowest-risk trial is:

1. install Veripsa Core on one selected repository only
2. leave branch protection/rulesets unchanged
3. watch the `Veripsa` GitHub checks and PR comments for a short observation window
4. promote to required-check mode only if the warnings are useful
5. uninstall from that selected repository if the signal is not useful

Veripsa Core does not store or display source file bodies or diff bodies. It may read repository content transiently to compute the traffic signal, but the customer-facing output and stored state are operational metadata such as PR number, path, signal state, check/comment identifiers, and ACK state.

See `docs/LOW_FRICTION_TRIAL.md` for the seven-day evidence table.

## No Veripsa Check Run observed

Thanks for the report. Please treat this as `not observed`, not as `Clear`.

Could you share a minimal synthetic reproduction only? Do not post a private
repository name, pull-request number or URL, branch, commit, Check Run URL,
comment URL, provider identifier, delivery identifier, source, or diff.
- PR state: draft or ready
- target branch
- approximate timestamp
- whether the GitHub Checks tab shows any `Veripsa` Check Run
- whether `gh pr checks <PR_NUMBER>` shows anything
- whether the GitHub App is installed on this repository

Please do not paste source code, private diffs, tokens, or secrets.

## No PR comment, but Check Run exists

This can be expected for a clean or no-reservation path. Veripsa may publish a GitHub Check Run without adding a PR comment when there is no coordination signal to explain.

Please check the `Veripsa` Check Run result first. If the Check Run completed successfully, no PR comment may be needed.

If the result still looks wrong, please share safe metadata only: PR number, head SHA, Check Run URL, and timestamp.

## `Unknown` result

`Unknown` is not `Clear`. It means Veripsa does not have enough signal to call the PR clear.

Please inspect the related PRs or files that Veripsa listed, then decide whether to wait, rebase, split the PR, or proceed with explicit human acknowledgement.

If you think `Unknown` is too noisy or missing context, please share safe metadata only: PR number, Veripsa Check Run URL, Veripsa PR comment URL, and the paths listed in the Veripsa output.

## `Wait in line` / `action_required`

`Wait in line` means Veripsa sees a real landing-order or collision risk.

The usual action is to let the named PR land first, then rebase or revise. Only use `veripsa-ack` after a human has read the specific warning and deliberately decided to proceed.

`veripsa-ack` is not code review approval and not a mute button.

## Private repo cannot require the Veripsa check

Veripsa is advisory by default. GitHub blocks merges only when the repository's branch protection or ruleset requires the Veripsa check.

Some private repositories cannot require checks depending on the GitHub plan/features available to that repository. In that case, Veripsa can still post visible guidance, but it cannot become the hard merge gate by itself.

Do not treat this as a Veripsa failure; it is a GitHub repository configuration / plan capability boundary.

## Possible stale branch lane

If Veripsa mentions a `BR-<branch>` lane for a branch that no longer exists, please share safe metadata only:

- repository owner/name
- PR number
- the branch name shown by Veripsa
- the Veripsa PR comment URL
- whether the branch still exists on GitHub

Do not paste source code or diffs. Branch names and PR numbers are enough to investigate stale lane cleanup.

## Suspected security issue

Please do not file public details if this might be a vulnerability.

Use the private reporting path in `SECURITY.md`. Include only the minimum metadata needed to reproduce the issue and avoid secrets in the report body.
