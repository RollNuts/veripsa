# GitHub events Veripsa subscribes to

The Veripsa GitHub App registers for the events listed below. The authoritative
source is the App manifest (`github-app/app-manifest.json`); this page is a
human-readable summary of what each event is used for from the **integrator's**
point of view. It does **not** describe the engine's internal processing.

## Permissions

The App requests the following GitHub permissions (read-only unless noted):

| Permission       | Level | Why                                                   |
|------------------|-------|-------------------------------------------------------|
| `contents`       | read  | Read repository structure and file content transiently where needed to compute content-free PR traffic signals; source and diff bodies are not stored, displayed, or used as code-review output. |
| `pull_requests`  | write | Read PR metadata/files, post PR comments, and manage the ack label. |
| `checks`         | write | Post advisory check runs on PR head commits.          |
| `merge_queues`   | read  | Receive merge queue `merge_group` events for batch-head checks. |
| `metadata`       | read  | Standard GitHub App requirement.                      |

Veripsa never requests `contents: write`. It does not push commits, open PRs,
or change branch protection.

GitHub implements PR conversation comments and labels on issue-backed REST
endpoints. Veripsa uses those endpoints only for the PR's own conversation and
`veripsa-ack` label. It does not request the `issues` permission and does not
subscribe to `issues`, `issue_comment`, or `sub_issues` webhooks.

## Subscribed events

### `pull_request`

The primary event. Veripsa reads PR-level structure (head SHA, base ref,
changed files) to maintain its pre-merge view of in-flight work.

Triggers analysis on: `opened`, `synchronize`, `reopened`, `ready_for_review`,
`converted_to_draft`, `edited` (when the base ref retargets), `labeled` /
`unlabeled` (only when the changed label is `veripsa-ack` — see
[label-semantics.md](label-semantics.md)), and on transitions that retire a PR
from the in-flight set (`closed`, `merged`).

### `push`

Used to detect direct-to-branch changes (including branch-only work that has
not yet opened a PR) so the in-flight view stays current.

### `repository`

Used to track repository lifecycle (renamed, transferred, archived, deleted)
so Veripsa's view of which repositories it watches stays consistent.

### `check_suite` and `check_run`

Used so Veripsa can observe whether other checks on a PR have completed. The
App posts its **own** check runs on PR head commits (see
[output-check-runs.md](output-check-runs.md)). Veripsa also treats its own
`check_suite.requested` event as a recovery signal: if GitHub has created the
Veripsa suite for a fresh PR head but the normal `pull_request` / `push` path
has not produced a check-run, Veripsa replays the associated PR through the
normal PR router. Non-Veripsa suites are ignored.

### `merge_group`

Subscribed so Veripsa can stay consistent with repositories that use GitHub's
merge queue feature. Veripsa is **not** a merge queue itself; this subscription
exists only to keep the in-flight view honest when one is in use.

## Automatic App lifecycle deliveries

GitHub delivers the App lifecycle events below to the App. They are not listed
in `default_events` in `github-app/app-manifest.json`, and operators should not
add Issue permissions or Issue webhook subscriptions to receive them.

### `installation` and `installation_repositories`

Lifecycle events for the App itself. Veripsa uses these to:

- onboard newly added repositories,
- release in-flight reservations when a repository is removed or the App is
  uninstalled / suspended,
- clean up data when the App is uninstalled (`installation.deleted`).

## What Veripsa does **not** subscribe to

Among the events Veripsa explicitly does not request: `issues`, `issue_comment`,
`sub_issues`, `release`,
`workflow_run`, `deployment`, `deployment_status`, `repository_dispatch`,
organization-level events, and anything that is only needed for source-code
review rather than PR collision control. `deployment` / `deployment_status`
remain intentionally out of scope until there is a documented deployment-signal
handler.

## Source

- App manifest: `github-app/app-manifest.json`
- Event dispatch and handlers: `github-app/server.py`,
  `github-app/webhook_handlers.py`

If the manifest ever diverges from this page, the manifest is the truth.
