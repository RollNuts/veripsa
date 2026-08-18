# Veripsa webhook spec

This directory describes the public contract between the Veripsa GitHub App and
the repositories where it is installed:

- which **GitHub webhook events Veripsa subscribes to**, and
- the shape of **what Veripsa posts back** — check runs, PR comments, and the
  `veripsa-ack` label.

> This is the **integration contract**. It is **not** documentation of how the
> engine decides anything (no graph algorithm, no scoring formula, no internal
> data model). Those stay private.

## Who this is for

- Developers building tooling on top of Veripsa output (dashboards, agent
  rules, log shippers).
- AI-agent maintainers who want their agent to **read Veripsa's check / comment
  / label** and react to it correctly.
- Security and compliance reviewers who want to know exactly which GitHub
  permissions and events the App uses.

## What this is NOT

- It is **not** a description of Veripsa's internal engine (graph extraction,
  blast-radius scoring, contention SQL, anything that is the moat).
- It is **not** an API to call into Veripsa. The App is event-driven over
  GitHub webhooks; there is no public REST surface for integrators today.
- It is **not** an SLA. Veripsa is **advisory** — the check it posts is
  non-blocking by default; your branch-protection policy decides what is
  required to merge.

## Honest scope

- Veripsa is **content-free**: source file bodies may be read transiently to
  produce collision signals, but they are not stored, displayed, or used for
  code review. The retained and rendered data is limited to paths, symbol
  names, line ranges, structural relationships, and GitHub identifiers.
- Veripsa runs **same-owner per-repo** today. Cross-owner / cross-repo
  collision surfacing is on the roadmap, not a claim made by this spec.
- Veripsa is **not a merge queue** and **not an AI reviewer**. It records
  who-is-changing-what across in-flight PRs and surfaces structural overlap
  before merge.

## Contents

- [events-subscribed.md](events-subscribed.md) — the GitHub webhook events
  Veripsa receives and what it does with each.
- [output-check-runs.md](output-check-runs.md) — the check-run verdict ladder
  and its GitHub `conclusion` mapping.
- [output-comments.md](output-comments.md) — the PR-comment marker convention
  and how Veripsa keeps its comment idempotent.
- [label-semantics.md](label-semantics.md) — the `veripsa-ack` label: what
  adding it means, what removing it means, who can add it.

## Status

**Draft.** This spec lives in the private Core repo while the wording is
reviewed. It will be published to a separate public repo once finalized.
