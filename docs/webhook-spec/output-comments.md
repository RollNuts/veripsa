# PR comments Veripsa may post

The check run is the primary always-visible PR surface. When a verdict needs
coordination, acknowledgement, partial-analysis disclosure, or a stale warning
clearance, Veripsa upserts a single **PR comment** that carries the
human-readable explanation plus structured pointers an agent can react to.

A clean `Clear` / no-reservation result intentionally posts no PR comment.
That keeps low-risk PRs quiet; the check run carries the visible signal.

## The comment marker

Every Veripsa PR comment **starts** with an invisible HTML marker that uniquely
identifies it as Veripsa's comment for that PR:

```
<!-- veripsa:PR-<number> -->
```

For example, on PR #42 the marker is `<!-- veripsa:PR-42 -->`.

The marker is:

- **Invisible** in GitHub's rendered markdown — it is an HTML comment.
- **Stable** across re-renders of the same PR — `<number>` is the PR number.
- **Sufficient to locate** Veripsa's comment in the PR's comment thread
  programmatically: search the body for the marker substring.

> Integrators MUST locate the comment by the marker, not by author display
> name, posting timestamp, or body text.

## Idempotency

When a PR needs a comment, Veripsa keeps **at most one** marked comment per PR.

- The first analysis of a PR creates the comment with the marker prepended.
- Every subsequent analysis on the same PR **edits the existing comment in
  place** — same marker, replaced body. Veripsa does not append new comments
  on re-analysis.
- The marker is preserved on edit; the rest of the body may change freely
  between events.
- A PR that stays clean from the start may have no Veripsa PR comment at all.

## Where structured info lives in the body

The comment body is GitHub-flavored markdown intended for humans. A few
conventions that agents can rely on:

- The comment **opens** with a bold human header that matches the check title
  (e.g. `**Clear.**`, `**Wait in line.**`, `**Heads up — minor
  overlap.**`). Branch on the leading bold token, not on the full sentence.
- Cross-PR partners are referenced using stable, content-free refs:
  - `PR-<number>` for PRs (e.g. `PR-17`),
  - `BR-<branch-slug>` for branch-only changes that have not yet opened a PR.
- File paths are rendered as inline code spans (`` `path/to/file.ext` ``).
- The body **never** contains file contents, diffs, or excerpts of customer
  source. Only paths, symbol names, line ranges, PR / branch refs.

## The ack snapshot marker

When the 一時停止 (pause-and-acknowledge) mode is in use, the comment body
also carries a second invisible HTML marker that binds an acknowledgement to
the specific coupling it covers:

```
<!-- veripsa-ack-snap:<12-hex-chars> -->
```

This marker is read back by Veripsa on subsequent events to decide whether an
existing `veripsa-ack` label is still valid for the **current** coupling, or
whether the coupling has materially changed and the ack is stale. Integrators
generally do not need to parse this marker; it exists so that ack state can
live entirely in GitHub (label + invisible marker) with no Veripsa-side
database column.

## What Veripsa does NOT post

- Veripsa does not open or close PRs.
- Veripsa does not push commits, suggest commits, or open PRs of its own.
- Veripsa does not post issue comments outside the PR's own comment thread.
- Veripsa does not post review comments on individual diff lines.

## Source

- Marker shape: `github-app/webhook_coercion.py` (`_comment_marker`,
  `_marked_comment`, `_change_id`).
- Idempotent upsert: `github-app/webhook_handlers.py`
  (calls to `gh.upsert_comment(..., _comment_marker(pr), ...)`).
- Ack-snapshot marker: `github-app/render_pauseack.py` (`_ACK_SNAP_PREFIX`,
  `_ACK_SNAP_RE`).
