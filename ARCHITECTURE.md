# Veripsa Architecture

This file records the current public product boundary for Veripsa Core. It is
intentionally narrower than older architecture experiments: Marketplace review
and GitHub App installation trust now matter more than keeping speculative
surfaces in the live App.

## Core GitHub App Boundary

Veripsa Core is a GitHub App for pre-merge PR traffic control. Its active
surface is:

- GitHub App installation
- pull request opened/synchronized/lifecycle events
- branch push events where they update open-PR traffic
- repository lifecycle events
- check suite / check run rerun and observation events
- merge queue `merge_group` events
- GitHub check runs posted by Veripsa
- PR comments and the `veripsa-ack` label when a PR needs a brief or ACK state

GitHub PR conversations, comments, and labels are implemented by GitHub on
issue-backed REST endpoints. That does not make GitHub Issues a Veripsa Core
product surface, and it does not require Veripsa Core to subscribe to `issues`,
`issue_comment`, or `sub_issues` webhooks.

## Explicit Non-Surfaces

The current Core App must not request or market the following as shipped:

- GitHub Issues management, issue deduplication, or issue work-slice ownership
- `issues`, `issue_comment`, or `sub_issues` webhook subscriptions
- `deployment` or `deployment_status` webhook subscriptions
- an AI code reviewer
- a CI replacement
- a merge queue replacement
- write-capable MCP
- autonomous merge or branch protection changes

## Future Billing

Billing remains a neutral entitlement axis in the codebase. The active launch is
GitHub App first and free while in early access. Future paid plans are expected
through GitHub Marketplace billing; direct checkout is not part of the active
launch path.

Marketplace purchase handling is preserved as future billing infrastructure and
must remain default-off until the listing, entitlement reconciliation, and paid
plan operations are ready.
