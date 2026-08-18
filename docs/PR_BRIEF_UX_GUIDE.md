# PR brief UX guide

This guide defines the information hierarchy for Veripsa Core PR comments.

The PR brief is the primary product surface. It must be short enough for humans and AI agents to scan inside the GitHub PR thread.

## The question the brief must answer

A PR brief should answer five questions in order:

1. Can this PR keep moving now?
2. Which PR or surface is related?
3. What should the author/reviewer do next?
4. Is human ACK required?
5. Is this a correctness claim or only PR-traffic advice?

## Design principles

- Lead with state and action.
- Link or name the related PR early.
- Keep ACK semantics visible but compact.
- Put evidence after the decision/action.
- Use stable terms across check output, comment, dashboard, docs, and support snippets.
- Do not include source file body or diff body.
- Do not call any state a proof of correctness.

## Recommended structure

This section uses illustrative placeholders. Public docs and Marketplace copy should use real public demo PRs, not these placeholders.

### 1. Header

```md
### Veripsa Core — heading to `<target-branch>`
```

Show target branch when available.

### 2. State + action line

Examples:

```md
**✓ Clear — no blocking PR traffic visible**
```

```md
**⚠ Heads up — nearby PR traffic found**
```

```md
**⏸ Wait in line — the named PR should land first**
```

```md
**? Unknown — not enough signal to call this clear**
```

The first bold line should be enough for a busy reviewer to know the next action.

### 3. Primary action

For `clear`:

```md
Continue normal review, CI, and branch-protection flow.
```

For `warn`:

```md
Inspect the related PR before expanding or merging this change.
```

For `action_required`:

```md
Wait for the named PR to land, or ask a human to ACK this warning snapshot before proceeding.
```

For `unknown`:

```md
Do not treat this as Clear. Review the changed surface manually.
```

### 4. Traffic summary

Keep the related surface compact.

```md
This PR reserves: `<path>`
Related traffic: the named PR also touches `<path>`
```

If multiple related PRs exist, show the top few and collapse or summarize the rest.

### 5. Recommended order

```md
Suggested order:
1. `<related PR>`
2. `<this PR>` ← this PR
```

Use “suggested” rather than “guaranteed safe.”

### 6. ACK semantics

Use a compact standard block when ACK is needed.

```md
ACK means a human saw this warning snapshot and chose to proceed. It is not code review approval, not conflict resolution, and not a permanent mute.
```

Avoid repeating a long paragraph in every comment unless the user is seeing ACK for the first time or the product has no other way to expose the semantics.

### 7. Evidence / footer

Keep evidence short and operational.

Allowed:

- PR number
- branch
- path
- line range
- related PR number
- signal state
- ACK state
- check run / event id where useful

Footer:

```md
Veripsa Core records PR traffic and ACK state. It does not assert code correctness. Advisory by default; branch protection/rulesets decide what blocks.
```

## State templates

These templates are product-copy patterns. Replace placeholders with real PR/path values in product output and use real demo links in public marketing/docs.

### Clear

```md
### Veripsa Core — heading to `<target-branch>`
**✓ Clear — no blocking PR traffic visible**

This PR reserves: `<path>`, `<path>`

There is no current action_required traffic for this PR. Continue normal review, CI, and branch-protection flow.

<sub>Clear is not a proof of correctness. Veripsa Core reports PR traffic, not code quality.</sub>
```

### Warn / Heads up

```md
### Veripsa Core — heading to `<target-branch>`
**⚠ Heads up — nearby PR traffic found**

Related traffic: `<related PR>` touches `<path>`.

Review the related PR before expanding or merging this change. No ACK is required by Veripsa Core for this state unless your team policy says otherwise.

<sub>Advisory by default; branch protection/rulesets decide what blocks.</sub>
```

### Wait in line / action_required

```md
### Veripsa Core — heading to `<target-branch>`
**⏸ Wait in line — the named PR should land first**

This PR reserves: `<path>`.
You collide with `<related PR>` around `<path>`.

Suggested order:
1. `<related PR>`
2. `<this PR>` ← this PR

Next action: wait for the related PR to land, or ask a human to add `veripsa-ack` for this warning snapshot.

ACK means a human saw this warning snapshot and chose to proceed. It is not code review approval, not conflict resolution, and not a permanent mute.

<sub>Veripsa Core records PR traffic and ACK state. It does not assert code correctness. Advisory by default; branch protection/rulesets decide what blocks.</sub>
```

### Unknown

```md
### Veripsa Core — heading to `<target-branch>`
**? Unknown — not enough signal to call this clear**

Changed surface includes paths that Veripsa Core cannot confidently judge in this snapshot.

Next action: do not treat this as Clear. Continue manual review and CI, and inspect the changed surface before merge.

<sub>Unknown is not Clear. It is an honest boundary of the current signal.</sub>
```

### Stale ACK / re-ACK required

```md
### Veripsa Core — heading to `<target-branch>`
**⏸ Re-ACK required — the warning snapshot changed**

A previous ACK exists, but the related PR, changed surface, base SHA, head SHA, or warning reason changed.

Next action: a human must review the new warning snapshot and ACK again, or wait for the related PR traffic to clear.
```

## Public demo references

Use real public demo PRs when writing docs or Marketplace copy:

- While the hosted service is suspended, use only the synthetic examples in this repository. Do not present an
  old demo check as current availability or live evidence.

Do not use fake PR numbers, fake installation IDs, or fake screenshots in public copy.

## Open follow-ups

- #649 PR brief readability and length
- #652 support/debug flow
- #630 first-run UI/UX
