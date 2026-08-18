---
applyTo: "**"
---

# Veripsa PR traffic instructions

This repository is used to build and dogfood Veripsa Core.

When working here, assume multiple human and AI contributors may have open PRs at the same time. Optimize for safe merge order, not just local task completion.

## Before editing

- Read the task scope from the source task, prompt, or PR comment.
- Identify the files you expect to change.
- Avoid high-risk areas unless explicitly scoped.
- Prefer the smallest diff that satisfies the acceptance criteria.

## While editing

- Do not opportunistically refactor unrelated files.
- Do not mix documentation, runtime code, database migrations, billing, and permission changes in one PR unless explicitly requested.
- Keep public copy outcome-level; do not publish implementation internals.

## Before handing off

- Read Veripsa check/comment if the PR exists.
- If Veripsa reports `Heads up`, `warn`, or `Unknown`, investigate the related or unverified area and do not call it Clear; these states do not require ACK.
- If Veripsa reports `Wait in line` / `action_required` for a material collision, follow the suggested landing order. Proceed out of order only after the repository owner decides to do so and the single `veripsa-ack` label is added.
- If CI fails, fix only the failing scope unless the owner asks for broader cleanup.
- Include validation and deployment notes in the PR body.

## Never do automatically

- Add `veripsa-ack` without an explicit repository-owner decision on the material collision.
- Ask a human to type a coupling snapshot hash or post a separate ACK rationale; the label is the complete acknowledgement signal.
- Force-push over someone else's branch.
- Mark `Unknown` as safe.
- Change auth, billing, GitHub App permissions, RLS, migrations, or content-free boundaries unless explicitly scoped.
