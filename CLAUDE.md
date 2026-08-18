# Claude Code instructions for Veripsa Core

These instructions apply when Claude Code works in this repository.

## Product framing

Veripsa Core is a GitHub App for pre-merge PR traffic control for parallel AI-agent pull requests.

It is not:

- an AI code reviewer
- a CI replacement
- a merge queue replacement
- a tool that proves a PR is safe
- a tool that automatically fixes conflicts

Use this positioning:

> GitHub is the workplace. AI agents are the workers. CI validates. Veripsa briefs the merge lane before merge.

## Operating rules

- Keep diffs small and reviewable.
- Prefer one concern per PR.
- Stay inside the stated task scope.
- Do not change unrelated files.
- Do not bypass Veripsa, CI, branch protection, or required checks.
- Do not force-push or rewrite history unless explicitly requested by the repository owner.
- If the task is ambiguous, ask in the source task thread or PR instead of widening the diff.

## High-risk areas

Do not touch these unless the task explicitly allows it:

- authentication / session handling
- billing, plans, quota, and Marketplace entitlement handling
- GitHub App permissions, webhook verification, installation routing
- database migrations, RLS, SECURITY DEFINER functions, tenant isolation
- retention, erasure, audit, privacy, legal documents
- source-body / diff-body handling boundaries

If a task touches any high-risk area, keep the diff extra small and call it out in the PR body.

## Veripsa merge rules

Before treating a PR as ready to land, read the Veripsa check and PR comment.

Use GitHub CLI when available:

```bash
gh pr checks <PR_NUMBER>
gh pr view <PR_NUMBER> --comments
```

Interpret Veripsa signals this way:

- `Clear`: continue normal review and CI. This is not proof that the PR is correct.
- `Heads up` / `warn`: inspect the related PR before expanding the change. This does not require ACK.
- `Wait in line` / `action_required`: resolve the reported condition. For a material cross-PR collision, follow the suggested landing order unless the repository owner explicitly decides to proceed.
- `Unknown`: investigate what was not verified and do not treat it as clear. This does not require ACK.

Only add `veripsa-ack` after the repository owner explicitly decides to proceed with a material collision. The GitHub label is the complete acknowledgement signal; do not request a snapshot hash or separate rationale.

If Veripsa says another PR is touching the same path or a related path, coordinate the landing order and rebase or revise as needed.

## GitHub-native AI workflow

Use a bounded work order.

The work order can be a prompt, PR comment, optional GitHub Issue, or another
source task. GitHub Issues are operator intake when a repository chooses to use
them; they are not a Veripsa Core product surface.

Before starting a task, confirm the work order states:

- Goal
- Scope
- Do not edit
- Acceptance criteria
- Validation
- Veripsa notes

The work order is the contract. Do not silently widen it.

## Public copy rules

When editing public docs, Zenn articles, README, Marketplace copy, or marketing copy:

- Start with user pain and searched market terms, not the Veripsa brand.
- Use GitHub-native language: Issues, PRs, checks, branch protection, Actions, Copilot, Claude, Codex.
- Keep claims advisory and evidence-bounded.
- Do not claim Veripsa proves a PR is safe.
- Do not expose internal scoring, routing, graph-construction, feature-extraction, prioritization details, or unpublished metrics.
- State content-free precisely: source file bodies and diff bodies are not stored, displayed, or used as code-review output.

For Zenn work, read:

- `docs/ZENN_MARKET_KEYWORDS.md`
- `docs/ZENN_TITLE_RULES.md`
- `docs/ZENN_PREPUBLISH_CHECKLIST.md`

## PR body checklist

Every PR should include:

```md
## Summary

- ...

## Validation

- ...

## Veripsa

- Veripsa state observed: clear / warn / action_required / unknown / not observed
- If a material collision is action_required: wait for the suggested order, or record the owner's decision with one `veripsa-ack` label

## Deployment

- [ ] No deploy needed
- [ ] Deploy needed; reason:
```

Do not treat missing Veripsa output as Clear. Say `not observed`.
