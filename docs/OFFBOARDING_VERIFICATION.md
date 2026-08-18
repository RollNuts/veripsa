# Offboarding and purge verification

This is the operator-facing verification map for uninstall, repository removal,
repository deletion, and account erasure. It is intentionally content-free:
verification records may mention repo names, paths, ids, counts, verdict tokens,
and table names, but must never include source bodies, diff bodies, secrets, or
customer webhook payloads.

## Current product contract

Veripsa Core separates the rebuildable working set from retained audit metadata.

- A GitHub App uninstall purges the account's current working set.
- Removing a repository from an installation purges that repository's working
  set only.
- A repository deletion purges that repository's working set.
- A repository rename or same-account transfer repoints the coordinate instead
  of treating it as a deletion.
- A right-to-erasure request uses the explicit account hard-delete path.
- Operational push/landing telemetry is pruned by the retention job; default
  retention is 30 days with a 14-day floor. Curated advisory history and
  statements remain append-only until an explicit account erasure.

Working set means rebuildable content-free structures such as graph rows,
claim/lane state, co-change cache, attributable durable webhook inbox rows, and live
repo-scoped authority such as workspace consent, outbound grants, and GitHub
store attachments for the affected scope. A repo-scoped removal leaves only a
minimal content-free revocation marker (account id, repository id or an
`unknown` legacy marker, repo coordinate, reason, timestamps) so an unordered
old webhook cannot recreate that working set. Lifecycle ordering is monotonic
in both directions using the authenticated durable-inbox receipt: an older
queued add cannot clear a newer removal, and an older queued removal cannot
purge or tombstone a newer same-id re-add. A stale add also skips cold graph,
open-PR backfill, and the watching signal. A newer explicit same-id re-add
clears its marker and resets the pre-selection working set before backfill. A
reverse-processed stale removal drops only authority older than that re-add and
preserves authority refreshed afterward. A different-id same-name replacement keeps the old exact-id marker
for stale-redelivery rejection, marks it superseded for repo reads, and excludes
pre-replacement audit rows from the replacement's dashboard. The explicit
GitHub lifecycle add/created event records a bounded stable-ID activation before
graph backfill, so a late old-object delete cannot hide an empty or not-yet-
ingested replacement. That delete removes only repo-name authority older than
the lifecycle activation boundary; consent/grants/connections refreshed for the
replacement survive. If the predecessor predates stable repository-id storage,
activation first clears its coordinate-keyed working set and records a
superseded `unknown` boundary. Replacement activation preserves queued webhook
deliveries because they may already carry the new stable id; the activation
guard rejects queued work whose id does not own the coordinate. Normal removal
uses a stable-ID-selective inbox purge: old-object rows and settled pre-boundary
rows are removed, while a different-ID replacement is preserved regardless of
receive order. Ambiguous queued legacy rows are retained for normal processing,
where the tombstone rejects stale work before the minimized payload is finalized.
If a signed replacement push completes
before the lifecycle add event, successful identity reconciliation records the
same stable-ID ownership boundary so delayed old-ID work is rejected. A
pre-fix name-only delete received before a replacement boundary cannot purge
that replacement, while a current/later name-only delete still purges the live
coordinate. The ordering source is the authenticated durable-inbox row, not a
caller-provided timestamp, and repository-scoped authority binds both the full
name and stable GitHub repository id. An account uninstall supersedes repo markers with
the account lifecycle marker. Account-level install/unsuspend events reactivate
the account only; they cannot clear a repo-level marker or cold-start a coordinate
with a current marker, mismatched stable ID, renamed identity, or ambiguous name-only
lifecycle state. The App cannot call the unordered compatibility overload.
Retained `pr_failing` audit facts are also hidden from the current stuck-PR
surface while the repository is removed; a replacement sees only post-boundary
facts.

Audit metadata means content-free operational records such as
push/landing/advice rows, check/comment ids, ACK-related state, and public git
metadata. Push/landing telemetry follows the normal retention window. Curated
advisory history and statements remain append-only until explicit account
erasure under the current contract.

## Verification matrix

| Flow | Expected behavior | Primary automated evidence |
|---|---|---|
| `installation.deleted` | Account-wide working set is forgotten, installation liveness is revoked, routing row remains for reinstall continuity, content-free audit ledger remains unless explicit erasure is requested. | `tests/test_lifecycle_e2e.py`, `tests/test_installation_liveness.py`, `tests/test_offboarding.py`, `tests/test_cochange_purge_erase_completeness.py`, `tests/test_uninstall_account_purge.py`, `tests/test_uninstall_resurrection.py` |
| `installation.deleted` dashboard reads | Operational dashboard reads keyed by installation id fail closed after liveness is revoked: effect, coverage, repo/file insights, now, repos, and graph insights do not expose stale data from the retained routing row. Owner-admin roster reads may still show the revoked row for churn awareness. | `tests/test_installation_liveness.py` |
| `installation.suspend` / `installation.unsuspend` | Suspend releases live lanes and revokes installation liveness without hard-deleting retained graph/audit metadata; unsuspend/reactivate clears the revoke. Dashboard reads fail closed while suspended and become readable again only after reactivation. | `tests/test_installation_liveness.py`, `github-app/webhook_handlers.py` |
| `installation.created` / reinstall | Reinstall or the next authenticated App event reuses the retained installation routing row and clears revoked account liveness so the working set can be rebuilt under the same account id. It does not independently clear repo-level markers. | `tests/test_installation_liveness.py`, `tests/test_lifecycle_e2e.py`, `tests/test_repository_offboarding.py` |
| `installation_repositories.added` / `.removed` | Stable repository id resolves renamed coordinates. A current removal forgets the named repository working set and live authority; a minimal marker blocks stale redelivery; an explicit graph-independent activation keeps same-name replacements visible before their first graph ingest. Authenticated durable receipt order makes the pair monotonic: stale add after remove and stale remove after re-add are safe no-ops, and stale adds do not run cold graph/PR backfill. Legacy null-id predecessor state is reset, queued replacement deliveries survive activation, and retained old-coordinate history is not attributed to the replacement. | `tests/test_repository_offboarding.py`, `tests/test_offboarding.py`, `tests/test_uninstall_account_purge.py`, `tests/test_webhook_ordering.py` |
| `repository.created` / `.deleted` | Uses the same stable-id-aware, bidirectionally ordered lifecycle path. DB failure propagates to the durable inbox for retry; the processing delete delivery drops its repo coordinate when finalized. | `tests/test_repository_offboarding.py`, `tests/test_webhook_ordering.py`, `github-app/webhook_handlers.py` |
| `repository.renamed` | Old coordinate is migrated to the new full name; graph, claims, consent, grants, lifecycle activation, co-change, and GitHub store attachments are not orphaned under the old name. A later stable-id delete can resolve an activation-only renamed coordinate even before graph ingest. | `tests/test_rename_completeness_enumeration.py`, `tests/test_rename_coordinate_orphan.py`, `tests/test_repository_offboarding.py`, `tests/test_rename_lane.py` |
| `repository.transferred` | Same-account transfer repoints the coordinate; cross-account transfer does not silently move private working set across tenants. | `github-app/webhook_handlers.py`, tenant-isolation and rename/coordinate gates |
| Branch deletion | `BR-*` branch lanes are released without touching PR lanes for unrelated work. | `tests/test_webhook_ordering.py`, `tests/test_draft_branch_lane_leak.py` |
| Account erasure | Explicit erasure deletes all tenant data, including answer-check ledger rows, and leaves neighbor tenants untouched. | `tests/test_offboarding.py`, `tests/test_account_erasure.py`, `tests/test_account_scope_erase_enumeration.py`, `tests/test_cochange_purge_erase_completeness.py` |
| Retention prune | Old operational telemetry is pruned by a scheduled job; curated records and statements remain immutable unless a gated account erasure is executed. | `github-app/retention_prune.py`, `github-app/RUNBOOK.md`, retention gates in `run_gates.sh` |

## Safe staging verification

Use a disposable GitHub account or organization and a disposable repository. Do
not run destructive verification against customer installations.

1. Install Veripsa Core on the disposable account.
2. Select one disposable repository and open a small PR that touches a harmless
   file.
3. Confirm Veripsa posts a check or comment, or record `not observed` if the
   test is intentionally limited to lifecycle webhooks.
4. Remove the repository from the installation.
5. Confirm a later stale webhook/redelivery does not recreate durable watched
   state for that repository.
6. Confirm repo/file dashboard reads fail closed while the repository is
   removed.
7. Re-add the repository and confirm the first new eligible PR or push can
   rebuild the working set and dashboard reads resume.
8. Uninstall the App from the disposable account.
9. Confirm the installation is no longer live, no repositories are listed as
   active, and any remaining support/audit metadata matches the retention
   boundary.
10. For an explicit erasure drill, run the documented account hard-delete path
   only on the disposable account and retain the deletion-receipt manifest.

Evidence to keep in the internal issue or deploy ledger:

- date/time
- disposable account id or sanitized account label
- disposable repository full name
- event type being verified
- before/after counts
- check/comment ids if relevant
- deletion receipt for explicit erasure

Evidence not to keep:

- source bodies
- diff bodies
- webhook payload bodies
- installation access tokens
- GitHub App private keys
- customer-owned examples without permission

## Operator decision boundary

Plain uninstall and repository removal are not the same as explicit
right-to-erasure. Plain lifecycle removal forgets the current working set and
stops future processing. Explicit erasure removes the account-wide durable
footprint through `core.erase_account_with_authority()`.

Do not wire hard-delete to `installation.deleted` without owner approval. The
current product posture keeps uninstall low-risk and reversible while preserving
content-free operational evidence within the retention boundary.

## Public wording

Safe:

```text
When the GitHub App is uninstalled or a repository is removed, Veripsa Core
stops processing that scope and purges the current rebuildable working set for
that scope. A minimal content-free revocation marker prevents stale webhook
replay until the repository is explicitly re-added. Operational push/landing
metadata follows the documented retention window; curated advisory records and
statements remain until an explicit account erasure is processed.
```

Avoid:

```text
Uninstall deletes everything immediately.
```

Reason: explicit account erasure is a separate path, and limited content-free
audit metadata may be retained according to policy.

Avoid:

```text
Veripsa stores no repository data.
```

Reason: Veripsa stores content-free operational metadata and rebuildable
structural working-set data while a repository is active.
