# Check runs Veripsa posts

For every analyzed PR, Veripsa upserts a single **check run** on the PR's head
commit. The check run is **advisory** — its GitHub `conclusion` is either
`success` or `neutral`. Veripsa does not post `failure`, `cancelled`, or
`timed_out` as part of its verdict surface.

> Veripsa does not block merges by itself. Whether a Veripsa check is required
> to merge is entirely controlled by your repository's branch-protection rules.

## The verdict ladder

Veripsa surfaces one of five verdicts on the check run. The exact title text
may evolve; the **shape** of the ladder is stable and is what integrators
should branch on (via the check title prefix, not full-string match).

| Verdict             | Check title (prefix)                                         | GitHub `conclusion` |
|---------------------|--------------------------------------------------------------|---------------------|
| Clear               | `Veripsa — Clear`                                            | `success`           |
| Heads up            | `Veripsa — Heads up` (cross-PR overlap)                      | `neutral`           |
| Wait in line        | `Veripsa — Wait in line` (direct collision ahead)            | `neutral`           |
| Heads up (minor)    | `Veripsa — Heads up` (minor overlap on a build / list file)  | `neutral`           |
| Unknown             | `Veripsa — Unknown` (not enough signal to call it clear)     | `neutral`           |

The check title is prefixed with the literal string `Veripsa — `; the word
that follows (`Clear`, `Heads up`, `Wait in line`, `Unknown`) is the
machine-readable signal. The trailing copy after the second em-dash is
human-facing and may vary.

## When the conclusion changes

- **`success`** is posted only for the **Clear** verdict.
- **`neutral`** is posted for every other verdict, plus for the informational
  states below.

A `neutral` check is, by GitHub's definition, a check that has completed and
does not gate a merge. The signal you act on lives in the **check title** and
the **PR comment** Veripsa posts in parallel (see
[output-comments.md](output-comments.md)).

## Informational (non-verdict) check states

Veripsa also posts `neutral` checks for a few operational states that are not
themselves a verdict:

- **"Veripsa is now watching"** — posted on first install / first analysis of a
  PR before a verdict is available.
- **"Veripsa paused — early-access limit reached"** — posted when the installation is
  beyond the current early-access coverage band and Veripsa is not analyzing
  further. This is a visible state, not a silent miss; it is `neutral`, never
  `failure`.
- **"Veripsa — changed files not read, not analyzed"** — posted when Veripsa
  could not read the PR's changed-files list for that event. Veripsa does not
  silently fall back to "Clear" in this case; it surfaces that it did not
  analyze.

## The 一時停止 (pause-and-acknowledge) mode

In addition to the verdicts above, Veripsa supports a `pause-and-acknowledge`
mode that an installation can opt into. In that mode, a verdict of
**Wait in line** posts the check with `conclusion: action_required` until the
PR carries the `veripsa-ack` label. The label is the explicit acknowledgement
signal; see [label-semantics.md](label-semantics.md).

`action_required` is the **only** non-advisory conclusion Veripsa ever posts,
and it is posted only in this mode and only for a verdict that already says
"Wait in line".

## Idempotency

Veripsa upserts **one** check per (PR head commit, App). Re-analysis of the
same head commit updates the existing check in place rather than creating a
new one.

## Source

- Verdict ladder and conclusion mapping: `github-app/render.py`
  (the `_VERDICT_CONCLUSION` / `_VERDICT_TITLE` tables).
- Pause / ack conclusion logic: `github-app/render_pauseack.py`.
- Informational states ("now watching", "paused", "not analyzed"):
  `github-app/render.py`.
