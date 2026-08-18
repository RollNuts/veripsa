# Retention and audit boundary

This document defines the product boundary for retaining Veripsa Core PR traffic records.

Veripsa Core needs enough history to explain merge-before decisions and ACK snapshots, but not so much retained data that it undermines the content-free trust model.

## Product principle

Retain only operational metadata needed to answer:

1. What PR traffic did Veripsa Core see?
2. What state did it publish?
3. Which snapshot did a human ACK?
4. Which check/comment represented that state?
5. What should support inspect without seeing source or diff bodies?

Do not retain source file bodies or diff bodies for audit convenience.

## Metadata categories

### Installation and repository metadata

May retain:

- installation id
- account id/type where needed
- repository id
- owner/name where needed for UI/support
- selected repository state
- created/updated timestamps

Purpose:

- route webhook events
- show repositories in dashboard
- support install/debug flow
- determine product activation funnel

### Pull request metadata

May retain:

- PR number
- base branch
- head branch reference where needed
- base SHA
- head SHA
- opened/synchronized timestamps where needed
- state: open/closed/merged where needed

Purpose:

- associate checks/comments with a PR
- determine whether a warning snapshot is stale
- support ACK semantics

### Changed surface metadata

May retain:

- path
- line range where applicable
- file-level or surface-level identifier
- signal state
- related PR number

Must not retain:

- source file body
- diff body
- code excerpt
- secret value
- environment variable value

Purpose:

- show where PR traffic overlaps
- explain land order
- preserve content-free product output

### Check/comment metadata

May retain:

- GitHub check run id
- check status/conclusion
- PR comment id
- last published signal
- last update timestamp

Purpose:

- update existing PR comment instead of spamming
- debug missing/no-comment cases
- support post-deploy smoke and canary verification

### ACK metadata

May retain:

- ACK actor
- ACK timestamp
- warning snapshot identifier
- PR number
- related PR/surface identifiers
- base/head SHA or equivalent snapshot inputs

Purpose:

- record that a human saw this warning snapshot and chose to proceed
- detect stale ACK when the warning snapshot changes

ACK is not code review approval, correctness proof, or a permanent mute.

## Lifecycle events

### PR opened or synchronized

Create or update PR traffic records for the current head/base snapshot.

### PR closed or merged

Recommended behavior:

- stop treating the PR as active traffic
- retain minimal historical metadata for audit/support for a defined period
- release or expire active lane reservations associated with the closed/merged PR where appropriate

### Branch deleted

Recommended behavior:

- release `BR-*` lanes for deleted branches
- avoid leaving ghost blockers in current PR comments
- retain minimal event evidence only if needed for support/debugging

See #612 for stale branch lane cleanup.

### GitHub App installation removed

Recommended behavior:

- stop processing future events for that installation
- remove or hide dashboard views tied to the installation
- delete or schedule deletion of retained installation/repo metadata according to the retention policy
- keep only records legally/operationally required for billing, abuse prevention, or security incident investigation, if applicable

## Suggested retention tiers

These are product-policy placeholders. They require owner approval before being treated as public promises.

### Active operational records

Retention: while PR/repo/installation is active.

Used for:

- current checks
- PR comments
- ACK state
- dashboard current traffic

### Short-term support records

Retention: TBD.

Used for:

- support debugging
- post-deploy smoke verification
- incident review
- Marketplace reviewer questions

### Aggregated product metrics

Retention: TBD.

Used for:

- activation funnel
- install-to-first-check conversion
- aggregate state counts

Must not contain source/diff bodies.

## Public copy

Safe public wording:

```text
Veripsa Core does not store or display source file bodies or diff bodies. It retains operational metadata such as PR number, path, signal state, check/comment identifiers, and ACK state to show and audit PR traffic decisions.
```

Avoid:

```text
Veripsa Core stores nothing.
```

Reason: operational metadata is retained.

Avoid:

```text
Veripsa Core never reads file content.
```

Reason: file content may be read transiently for signal generation. The public boundary is storage/display/code-review output, not transient read behavior.

## User deletion / uninstall questions

Prepare support answers for:

- What happens when I uninstall the GitHub App?
- Can I delete installation metadata?
- How long does ACK history remain?
- Can I export an audit trail?
- What exact metadata do you keep?

Do not answer these publicly until implementation and policy are confirmed.

## Open follow-ups

- #651 define retention and audit-trail boundaries
- #619 public security baseline
- #648 activation funnel
