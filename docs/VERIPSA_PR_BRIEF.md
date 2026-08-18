# Veripsa Core PR Brief contract

This document defines the public-facing shape Veripsa Core should use when it comments on a GitHub pull request.

Goal:

> A reviewer should know whether an AI-authored PR can keep moving without leaving the GitHub PR page.

The PR brief is a GitHub-native operating surface. It is not a dashboard replacement, a code review, or a proof that the PR is safe.

## Status vocabulary

Use a small, stable vocabulary.

| Status | Meaning | Merge behavior |
|---|---|---|
| `clear` | No relevant open-PR collision is visible in the current snapshot | Continue normal review / CI |
| `warn` | Related or nearby open-PR traffic exists | Reviewer should inspect the named PR(s) |
| `action_required` | Proceeding now likely needs human coordination or ACK | Required check may block merge until ACK |
| `unknown` | Veripsa Core lacks enough signal to judge this surface | Do not treat as clear |

Never use `clear` to mean the PR is correct.

## Public demo shape

While the hosted service is suspended, the examples in this brief are synthetic and illustrative. Historical
demo checks must not be described as current availability or live evidence.

```text
### Veripsa — heading to `main`
**✓ Clear — nothing is blocking you**

This PR reserves: `orders/pricing.py`, `tests/test_pricing.py`

> **⏳ 1 in-flight PR is waiting behind this one** on: `orders/pricing.py`, `tests/test_pricing.py` — **GH-RollNuts PR-2**.

**Blast radius** (structurally downstream on `main`): `tests/test_pricing.py`

**Overlapping PRs** — 2 open PRs touch this same area.
Suggested order to land them:

1. GH-RollNuts PR-1 ← this PR
2. GH-RollNuts PR-2
```

Use real public demo output when writing public docs or Zenn articles. Avoid placeholder PR numbers, fake paths, or fake installation IDs in user-facing copy.

## What the brief may include

The brief may include:

- PR number
- base / head SHA
- installation id
- event id
- actor class when known: human / copilot / codex / claude / github-actions / other
- changed path
- line range
- related PR number
- status
- recommendation
- ACK state

## What the brief must not include

The brief must not include:

- source file body
- diff body
- secret values
- environment variable values
- private customer content beyond path / operational metadata
- implementation details about scoring, routing, feature extraction, or graph construction

## ACK semantics

ACK is not approval.

ACK means:

> A human saw this warning snapshot and chose to proceed anyway.

ACK does not mean:

- code is correct
- review is complete
- conflict is resolved
- future warnings should be muted
- future snapshots are approved

If the related PR, changed surface, base SHA, head SHA, or warning reason changes, the ACK may become stale and should be requested again.

## Actor semantics

When possible, classify the actor that created or updated the PR.

Suggested classes:

- `human`
- `copilot`
- `codex`
- `claude`
- `github-actions`
- `other-ai-agent`
- `unknown`

Actor labels are for traffic visibility, not trust. A human PR can be risky. An AI PR can be clear.

## Required check behavior

When Veripsa Core is configured as a required status check:

- `clear` should pass
- `warn` may pass while surfacing the related traffic
- `action_required` should fail or stay pending until ACK, depending on product policy
- `unknown` should not be silently converted to pass-as-clear

The exact GitHub check conclusion may vary by plan and deployment mode, but the PR comment should keep the meaning stable.

## Design principle

The brief should help answer one question:

> Can this PR keep moving now, or should it wait for another PR / human ACK?

Keep it short enough for humans and agents to read inside the GitHub PR thread.
