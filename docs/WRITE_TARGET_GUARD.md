# Write target guard

Use this guard before any GitHub file write.

The goal is to prevent a repeated failure mode: attempting a write to a branch that does not exist, then accidentally retrying against `main` or leaving a placeholder on the default branch.

## Rule

Never write to `main` as a fallback after a branch-targeted write fails.

If a write fails with `Branch <name> not found`, stop the write sequence and create or verify the branch first.

## Required sequence

Before `create_file`, `update_file`, or `delete_file` for implementation work:

1. Pick the intended branch name.
2. Create the branch with `create_branch`, or verify it exists with branch search / fetch against that ref.
3. Perform writes only with the explicit branch parameter.
4. If any write returns `Branch ... not found`, do not retry on `main`.
5. Re-run branch verification.
6. Continue only after the branch exists.

## Prohibited fallback

Do not do this:

```text
write to branch -> branch not found -> retry same write on main
```

Even a docs-only placeholder is a process exception because it bypasses:

- branch review
- PR body validation
- Veripsa observation
- direct-main exception discipline

## Recovery if it happens

If a default-branch write happens by mistake:

1. Stop further writes.
2. Create the intended feature branch from current `main`.
3. Replace or delete the accidental placeholder on that branch through a PR.
4. Record the default-branch commit in `docs/DIRECT_MAIN_EXCEPTION_LOG.md`.
5. Mention the exception and validation gap in the PR body.
6. If runtime code was touched, complete an explicit post-merge audit before
   further runtime work.

## PR body wording

Use this when a branch-targeting exception happened during a docs-only change:

```text
Process note: a placeholder was accidentally written to `main` after a branch-targeted write failed. This PR replaces the placeholder with the intended content and records the direct-main exception in `docs/DIRECT_MAIN_EXCEPTION_LOG.md`.
```

Use this when no exception happened:

```text
Write target guard: branch was created/verified before file writes; no direct-main fallback was used.
```

## Related docs

- `AGENTS.md`
- `docs/DIRECT_MAIN_EXCEPTION_LOG.md`
- `docs/OPERATIONS_INDEX.md`
