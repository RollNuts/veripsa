# Support debug flow

> Availability: the project-hosted service is intentionally suspended as of
> 2026-08-18. During the suspension, missing hosted output is expected; do not
> request repository or pull-request identifiers in a public issue.

This document gives user-facing and support-facing language for common Veripsa Core confusion states.

It is written for public support, Marketplace reviewer response, and first-run troubleshooting.

Do not include secrets, webhook payloads, installation tokens, source file bodies, or diff bodies in user-facing support responses.

## 1. No Veripsa Core check appeared

### What the user sees

A PR was opened or updated, but no Veripsa Core check appears in the GitHub PR checks/status area.

### User-facing explanation

```text
Veripsa Core reports through GitHub checks. If you do not see a check yet, first confirm the GitHub App is installed on this repository and that the PR targets a covered branch. Missing output is not the same as Clear.
```

### Support checklist

Ask or verify:

- Is the GitHub App installed on the account/org?
- Is this repository selected in the GitHub App installation settings?
- Does the PR target the expected base branch?
- Was the PR opened or synchronized after installation?
- Does the Checks tab/status rollup show any Veripsa Core check?
- Is there known worker delay or delivery lag?
- Is this related to #611 opened-event consistency?

### Safe next step

Ask the user to push a harmless update to the PR branch only if appropriate. Do not imply that a push should be required for normal operation; this is a diagnostic step.

## 2. No PR comment appeared

### What the user sees

A Veripsa Core check may exist, but there is no PR comment.

### User-facing explanation

```text
Clean PRs may be check-only. Veripsa Core adds a PR comment when there is PR traffic worth briefing, such as a wait state, overlap, Unknown, or ACK-related state.
```

### Support checklist

- Does the Veripsa Core check exist?
- Is the state clear/check-only?
- Is there related PR traffic that should have produced a comment?
- Is this a comment-upsert issue or an expected no-comment path?

### Demo links

- While the hosted service is suspended, use the repository's synthetic fixtures. Historical demo checks are
  not current availability evidence.

## 3. Why is this PR paused?

### What the user sees

The PR shows `action_required`, `Wait in line`, or similar language.

### User-facing explanation

```text
Veripsa Core sees this PR as overlapping or queued behind another in-flight PR. The suggested path is to wait for the named PR to land or have a human ACK the current warning snapshot before proceeding.
```

### Support checklist

- Which related PR is named?
- Is the related PR still open?
- Does the branch still exist?
- Is the wait state based on stale branch-lane data? See #612 if stale branch refs appear.
- Has the user already ACKed this exact warning snapshot?
- Did the base/head SHA or related PR set change after ACK?

### ACK response snippet

```text
ACK records that a human saw this warning snapshot and chose to proceed. It is not code review approval, not conflict resolution, and not a permanent mute. If the warning changes, Veripsa Core may ask for ACK again.
```

## 4. Why did branch protection not block the merge?

### What the user sees

Veripsa Core reported a warning or `action_required`, but GitHub still allowed the merge.

### User-facing explanation

```text
Veripsa Core is advisory by default. To make action_required hold merges, add the Veripsa Core check to GitHub branch protection or rulesets where your GitHub plan supports required checks.
```

### Support checklist

- Is Veripsa Core configured as a required status check for the target branch?
- What exact check name did GitHub show?
- Does the branch protection/ruleset apply to this branch?
- Does the repository plan/support required checks in this configuration?
- Did the PR merge through an admin override or bypass rule?

Link:

- `docs/REQUIRED_CHECK_SETUP.md`

## 5. What does Unknown mean?

### User-facing explanation

```text
Unknown means Veripsa Core does not have enough signal to judge this surface. It is not Clear, and it does not necessarily mean the PR is dangerous. It means the tool is not claiming a clear result for that surface.
```

### Support checklist

- What path or file type was involved?
- Was the file generated, vendored, too large, or otherwise outside the analysis surface?
- Did GitHub return enough file/change metadata?
- Is this a known coverage limitation or a temporary event/API gap?

## 6. Why does Veripsa Core mention a branch or PR that no longer exists?

### User-facing explanation

```text
Veripsa Core may be showing stale branch traffic if a branch was deleted before its lane was released. We are checking whether the referenced branch or PR is still active.
```

### Support checklist

- Confirm whether the named branch still exists.
- Confirm whether the named PR is open or closed.
- Check whether #612 or related stale-lane cleanup applies.
- Do not tell the user the PR is clear until the stale reference is resolved or expired.

## 7. What information should support never ask for?

Do not ask the user to paste:

- private source file bodies
- diff bodies
- secrets or tokens
- GitHub App private keys
- installation access tokens
- private webhook payloads containing sensitive data

Ask for safe metadata instead:

- repository owner/name
- PR number
- check run URL
- Veripsa Core comment URL
- signal state shown
- related PR number
- installation/account if needed

## 8. Escalation levels

### L1 — expected behavior

Examples:

- clean PR has check only, no comment
- advisory mode does not block merge
- Unknown is not Clear

### L2 — configuration issue

Examples:

- GitHub App not installed on repo
- branch protection does not require Veripsa Core check
- repo not selected in installation settings

### L3 — product issue

Examples:

- PR opened event did not create expected check
- stale branch lane appears
- PR comment contradicts check state
- ACK appears stale incorrectly

Create or link to a GitHub issue for L3 cases.

## Open follow-ups

- #611 opened-event consistency
- #612 stale branch lane cleanup
