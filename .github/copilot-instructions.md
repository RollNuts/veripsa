# Copilot instructions for Veripsa Core

Use these instructions when GitHub Copilot, Copilot coding agent, or another GitHub-native coding agent works in this repository.

## Product framing

Veripsa Core is a GitHub App for pre-merge PR traffic control for parallel AI-agent pull requests.

It is not an AI code reviewer, CI replacement, or merge queue replacement. It watches the space between open PRs and helps humans decide whether a PR should proceed, wait, or receive explicit ACK.

## Operating rules

- Keep every change small and reviewable.
- Prefer one concern per PR.
- Do not change unrelated files.
- Stay inside the stated task scope.
- Do not bypass Veripsa, CI, branch protection, or required checks.
- Do not force-push or rewrite history unless explicitly requested by the repository owner.
- If the task is ambiguous, ask in the source task thread, prompt thread, or PR instead of widening the diff.

## Veripsa merge rules

Before treating a PR as ready to land, read the Veripsa check and PR comment.

If Veripsa reports:

- `Clear`: continue normal review and CI.
- `Heads up` or `warn`: inspect the related PR before expanding the change; no ACK is required.
- `Wait in line` or `action_required`: resolve the reported condition. For a material collision, follow the suggested landing order unless the repository owner explicitly decides to proceed and adds `veripsa-ack`.
- `Unknown`: do not treat it as clear; investigate what was not verified. No ACK is required.

Never add `veripsa-ack` unless the repository owner explicitly decides to proceed with the material collision. The label is the complete acknowledgement; do not request a snapshot hash or separate rationale.

## High-risk areas

Do not touch these unless the task explicitly asks for it:

- authentication / session handling
- billing, plan, quota, and Marketplace entitlement code
- GitHub App permissions, webhook verification, installation routing
- database migrations, RLS, SECURITY DEFINER functions, tenant isolation
- retention, erasure, audit, privacy, legal documents
- source-body / diff-body handling boundaries

If a task requires touching one of these, keep the diff extra small and call it out in the PR body.

## Public copy rules

When editing public docs, Zenn articles, README, Marketplace copy, or marketing copy:

- Start with user pain and searched market terms, not the Veripsa brand.
- Use GitHub-native language: Issues, PRs, checks, branch protection, Actions, Copilot, Claude, Codex.
- Keep the claim honest: Veripsa is advisory unless branch protection requires its check.
- Do not claim Veripsa proves a PR is safe.
- Do not expose internal scoring, routing, graph-construction, feature-extraction, or prioritization details.
- State content-free precisely: source file bodies and diff bodies are not stored, displayed, or used as code-review output.

## PR body checklist

Every PR should include:

- Summary
- Validation performed
- Veripsa state observed, or `not observed`
- Whether deployment is needed
