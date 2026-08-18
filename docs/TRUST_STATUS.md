# Trust status boundary

Veripsa has two different status surfaces.

> The hosted service is intentionally suspended as of 2026-08-18. The endpoints
> below document the contract used when service is active; an unavailable endpoint
> during the suspension is expected and must not be described as operational.

## Public status

When a deployment has been explicitly resumed, operators may substitute its
approved public base URL:

```bash
APP_URL=https://service.example.invalid
curl "$APP_URL/statusz"
curl "$APP_URL/freshz"
```

`/statusz` is intentionally coarse. It can say whether the public service is
`operational` or `degraded`, and it gives broad signal states for:

- worker
- webhook ingress
- event processing
- Check Run canary handling

It must not expose:

- private repository names
- pull request numbers
- delivery ids
- Check Run ids or URLs
- PR comment ids or URLs
- source file bodies
- diff contents
- secrets or tokens

The endpoint is for first-line trust/support triage. It is not the operator
debug surface.

`/freshz` is also public-safe, but narrower: it exposes only aggregate
coordinate/behind counts plus whether the sample is current, stale, in progress,
or failed. It never returns repository, branch, account, commit, per-coordinate
node/edge counts, or ingestion timestamps. Detailed freshness records remain
internal to the watchdog and operator evidence.

## Operator status

Use operator-only surfaces when diagnosing a specific missing check/comment:

- `/healthz`
- `/readyz`
- protected deployment-verification evidence
- GitHub Checks API on the exact canary or customer PR head SHA
- provider logs filtered through the private incident process
- `github-app/RUNBOOK.md`

A Tier 4 Check Run canary must be verified through the GitHub Checks API. Its
concrete canary PR, head SHA, Check Run id, and run URL are operator evidence.
They do not belong in public `/statusz` until Veripsa has a durable public-safe
canary-result store. The production smoke workflow itself is intentionally absent
from this public source mirror.

## Support wording

When a user reports that no Veripsa signal appeared:

- First say it is `not observed`, not `Clear`.
- Ask for safe metadata only.
- Ask them to check the PR Checks area, not only the PR conversation.
- Use `/statusz` only to distinguish broad service degradation from
  repo-specific installation, branch, permission, or timing issues.
- Do not treat a missing PR comment as failure by itself. A clean PR may be
  check-only.
