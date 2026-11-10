# Review related files before a local change

Given a local Git checkout and one file, this command lists other files worth
inspecting before editing it. It reuses Core's existing static file-pair
projection and Co-change counting/filtering. No database, App installation,
account, model call, or additional Python package is needed for Python input.

## Run

Use a POSIX environment with Git on `PATH` and Python 3.12. From a directory
where you keep local projects, obtain Core and first try it on itself:

```sh
git clone https://github.com/RollNuts/veripsa.git
cd veripsa
python3.12 -B -m tools.review_file . _cg_cochange.py
```

If you already have Core, start from its checkout root and run just the Python
command. Keep the clone's history: a ZIP download is not a Git checkout, and a
shallow clone limits Co-change evidence.

For your own checkout, stay in Core's directory and replace both arguments:

```sh
python3.12 -B -m tools.review_file /path/to/your/repo src/example.py
```

The first argument must be that repository's root. The second is an existing
regular file's path relative to it. The command parses source without importing
or executing the target project. It reads the current worktree and existing Git
history; it does not write a report file, change refs, merge, or edit the target.
Use `-B` at interpreter startup so importing Core does not create Python caches.
Other languages and some configuration formats need the existing optional
parsers; absent parsers are reported as reduced coverage.

## Read the JSON

| Field | What to inspect |
| --- | --- |
| `static.candidates` | Relative paths and `import`, name-matched `call`, shared `schema`, or shared `config` evidence from `cochange_backtest.coupled_pairs`. |
| `history.candidates` | Historical partners with joint-edit counts, directional probabilities, and lift from the existing Co-change helpers. |
| `static.coverage` / `warnings` | Parsed, failed, and skipped file counts, plus unique document paths per failed/incomplete/ambiguous status (including configuration documents); whether the target or checkout has incomplete analysis. |
| `history.parameters` / `warnings` | Actual producer defaults, retained history, insufficient support, and shallow-history limits. |
| `provenance` | Observed HEAD, dirty/shallow flags, target/status hashes, and the distinct worktree/history inputs. |

`target` and candidate `file` use the actual filesystem spelling.
`provenance.history_target` and the history candidate's `git_file`, `a`, and `b`
retain Git's spelling. Counts `n_a`/`n_b` and directional probabilities refer to
those `a`/`b` paths. Git and filesystem spellings can differ under Unicode normalization.

Each evidence section has a status:

- `observed`: candidates passed that section's projection or filters.
- `uncertain`: coverage or history is insufficient, whether or not candidates exist.
- `not_observed`: no candidate was found within the stated scope and filters.

An empty list is not proof of no dependency or merge safety. Static calls use
name matching and can be wrong; dynamic references can be missed. Files edited
together can be unrelated. These are review candidates, not predicted bugs,
complete transitive impact, or the App's PostgreSQL PR verdict.

The static input is the **current worktree**; history describes **committed
HEAD**. Uncommitted changes are not added to the historical counts. A suggested
workflow is to inspect the named files, compare their assumptions with the
planned change, and run the relevant tests. An AI agent can use the same JSON
as input under its separately granted permissions; this command does not
configure MCP or automatically control an agent.

## Bounds and failures

Exit `0` means a report was produced, including possible `uncertain` sections.
It does not approve the change. Exit `2` returns a bounded JSON error code for
invalid input or failed analysis. A failed Git history read is an error, not a
successful empty history.

- Targets larger than 1,500,000 bytes, symlink targets/parents, submodules, and
  hidden index flags are rejected by this small command.
- NFC-normalized graph identities are mapped back to unique actual filesystem
  names. Canonically colliding path spellings are rejected rather than guessed.
- Configured Git filters, including filters loaded through included configuration,
  are rejected before status inspection so clean/process programs cannot run.
- The strict history path rejects Git-quoted/control-character or leading/trailing
  whitespace paths it cannot faithfully parse. Unicode paths are supported.
- The producer's history window, large-commit exclusion, thresholds, and global
  pair cap limit recall. Missing current historical partners are omitted and counted.
- HEAD, status, and target bytes are checked before/after. This is not an atomic
  snapshot: other edits leaving status unchanged, or transient reverted edits,
  can escape detection. Use an isolated checkout without concurrent writers.
- Git transports and lazy object fetching are disabled for the command. Missing
  local objects can cause a controlled failure rather than an automatic fetch.
  Strict history reads also suppress signature display/verification, external
  diff, and textconv programs; repository settings themselves are not changed.
- Output contains paths, counts, hashes, and analysis metadata, not source/diff
  bodies, authors, commit messages, or remote URLs. Paths can still be sensitive;
  inspect the local output before sharing it.

The existing [analysis inventory](ANALYSIS_ASSETS.md) explains the producers and
their limits. The [synthetic merge demo](../examples/structural-overlap/README.md)
compares these kinds of evidence with actual combined execution.
