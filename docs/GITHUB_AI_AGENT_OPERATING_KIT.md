# GitHub AI agent operating kit

This guide turns the Zenn positioning into an executable repository workflow.

Goal:

> GitHub is the workplace. AI agents are the workers. CI validates. Veripsa briefs the merge lane before merge.

Use this when testing GitHub Copilot coding agent, OpenAI Codex, Claude, or any other GitHub-native coding agent against Veripsa Core.

## 1. Use a bounded work order

Use a GitHub-native work order before starting an agent task. That can be an
Issue, an Agents tab prompt, a PR comment, or a Codex Web task. Issues are an
operating convention for humans and agents; they are not a Veripsa Core product
surface.

Veripsa Core does not claim Issues, deduplicate Issues, subscribe to `issues` or
`issue_comment` webhooks, or try to decide which part of an Issue an agent owns.
Its active product boundary is PR / branch / push / check / PR comment traffic.

Every agent-ready work order should state:

- Goal
- Scope
- Do not edit
- Acceptance criteria
- Validation
- Veripsa notes

The work order is the contract. The agent should not silently widen the diff.

## 2. Put repository rules where agents read them

This repository uses:

- `AGENTS.md` — cross-agent operating contract
- `CLAUDE.md` — Claude Code operating contract
- `.github/copilot-instructions.md` — Copilot-specific repository instructions
- `.github/instructions/veripsa-pr-traffic.instructions.md` — path-wide GitHub custom instructions

Rules should bias toward small PRs, explicit safety boundaries, and Veripsa-aware merge behavior.

## 3. Recommended agent prompt

Use this shape when assigning work to Codex / Copilot / Claude:

```text
Implement this task.

Keep the diff small and inside the stated scope.
Do not edit auth, billing, GitHub App permissions, database migrations, RLS, or content-free boundaries unless the task explicitly allows it.
Run the smallest relevant validation.
If Veripsa reports action_required / Wait in line, stop and explain the reason in the PR.
If Veripsa reports Unknown, do not treat it as Clear.
Do not add veripsa-ack unless the repository owner explicitly asks for it.
```

For docs tasks:

```text
Keep published:false unless explicitly asked.
Use market-keyword-first titles.
Do not expose scoring, routing, feature extraction, graph construction, prioritization details, or unpublished metrics.
Keep content-free precise.
```

## 4. PR handoff checklist

Every AI-authored PR should include:

```md
## Summary

- ...

## Validation

- ...

## Veripsa

- Veripsa state observed: clear / warn / action_required / unknown / not observed
- If action_required: human ACK required before merge

## Deployment

- [ ] No deploy needed
- [ ] Deploy needed; reason:
```

Do not treat missing Veripsa output as Clear. Say `not observed`.

## 5. Required check posture

For a GitHub-native AI workflow, Veripsa should be a required status check on protected branches when the GitHub plan allows it.

Recommended conceptual merge gates:

- Veripsa Core check
- test
- lint
- typecheck
- security / secret scan where available

Veripsa does not replace CI. CI checks the PR. Veripsa checks traffic between PRs.

## 6. Demo path for Veripsa

Use a low-risk docs-only demo first.

### Demo A — same-file collision

Create two low-risk work orders:

- Work A: improve README onboarding section
- Work B: improve README pricing or install section

Assign them to different agents or run them in parallel. Both are likely to touch `README.md`.

Expected Veripsa story:

- both agents create PRs
- Veripsa sees overlapping changed surface
- one PR receives wait / warn / action_required depending on the actual overlap
- human sees the order in PR comments
- human ACKs or merges in the suggested order

### Demo B — semantic adjacency

Create two low-risk work orders:

- Work A: update a public docs page describing GitHub App installation
- Work B: update a related docs page describing branch protection / required checks

Expected Veripsa story:

- PRs may not have identical edits
- Veripsa can still show related changed surface when it has enough context
- Unknown should stay Unknown when context is insufficient

### Demo C — safety boundary

Create a work order that explicitly says `Do not edit db/**, github-app/**, auth, billing, permissions`.

Expected agent behavior:

- PR stays inside docs/tests scope
- PR body says no high-risk areas touched
- Veripsa state is reported

## 7. What not to automate yet

Do not automate:

- adding `veripsa-ack`
- merging after action_required
- force-pushing to resolve queue position
- broad refactors after a docs/test task
- changes to auth, billing, GitHub permissions, DB migrations, RLS, or content-free boundaries

Do not document unshipped Veripsa capabilities as if they exist. Keep public and operator-facing docs focused on the shipped GitHub App surfaces: checks, PR comments when needed, and labels/comments for ACK. Keep repository instructions and issue templates clearly labeled as operator workflow, not Core behavior.

The strongest first product story is not autonomous merge.

It is:

> AI opens PRs. Veripsa stops unsafe merge traffic. Humans ACK the right snapshot.
