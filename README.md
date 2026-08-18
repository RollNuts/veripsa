# Veripsa Core

> **Public source mirror.** Production credentials, provider identifiers, deployment
> receipts, incident records, private CI history, and mutation workflows are deliberately
> excluded. Repository visibility is not an availability signal for the hosted service.
>
> **Hosted service status:** intentionally suspended as of 2026-08-18. Do not install
> the GitHub App expecting webhook processing until an explicit resumption notice is
> published. Offline evaluation remains available.

GitHub App for pre-merge PR traffic control in repositories where AI coding
agents and humans open pull requests in parallel.

Two agents open pull requests against the same repo. Each looks fine on its own,
and each passes review and CI. They touch the same code, land in the wrong order,
and `main` breaks anyway. Veripsa Core watches the traffic **between open pull
requests** while there is still time to coordinate, and reports it as a GitHub
check and — when there is traffic worth briefing — a PR comment.

This repository contains the engine and webhook runtime. Public product and
trust information is available at [veripsa.com](https://veripsa.com); the site
must not be treated as an active install path while the suspension notice above
is in effect.

---

## What it does

When several open PRs touch related code, the hard problem is not only whether
one PR is correct. It is whether another open PR changes the assumptions around
it, whether landing order matters, and who should wait or revise before `main`
changes.

Git catches textual conflicts at merge time. Per-PR review and CI evaluate an
individual change. Veripsa Core surfaces direct overlap and supported structural
relationships **between open pull requests** as a GitHub check and, when there is
traffic worth briefing, a managed PR comment.

By default the output is advisory: GitHub branch protection or rulesets decide
whether an `action_required` check conclusion holds a merge, and required checks
on private repositories depend on the applicable GitHub plan.

### Traffic signals

Veripsa Core has four base traffic signals:

- **Clear** — no blocking traffic is visible in the analyzed comparison set.
  Clear is not merge approval, correctness, or safety proof.
- **Heads up** — a relationship is worth reviewing. It is normally advisory and
  acknowledgement-free.
- **Wait in line** — strong overlap or landing-order risk is visible; Veripsa
  suggests that this PR wait behind related work.
- **Unknown** — input, coverage, or comparison context is insufficient for a
  supported call. Unknown is not Clear.

A material coupling can add **Paused (acknowledge to proceed)** as an
`action_required` control overlay. Paused is not a fifth traffic verdict. Adding
the `veripsa-ack` label records that a human saw the current coupling snapshot
and chose to proceed. It is not approval, conflict resolution, a permanent mute,
or a claim that the code is correct. A material change to the related work can
make the acknowledgement stale and pause again.

### What the check looks like

When two open PRs touch the same area, the leading PR's comment reads roughly
like this — content-free, carrying only paths, PR references, and landing order,
never file bodies:

```text
### Veripsa — heading to `main`
**✓ Clear — nothing is blocking you**

This PR reserves: `orders/pricing.py`, `tests/test_pricing.py`

> ⏳ 1 in-flight PR is waiting behind this one on `orders/pricing.py`, `tests/test_pricing.py`.

**Overlapping PRs** — 2 open PRs touch this same area.
Suggested order to land:
1. this PR
2. the other open PR on the same area
```

The PR waiting behind renders **Wait in line**, naming the PR ahead of it. A
clean PR with no related traffic is check-only, with no comment.

---

## Where it fits

Veripsa Core works alongside the tools already in the merge path:

- **Merge queues** serialize ready pull requests near final merge time.
- **AI code reviewers** inspect the implementation inside one pull request.
- **CI and tests** validate an individual change and the policies encoded by the
  repository.
- **Veripsa Core** adds the earlier open-PR relationship and landing-order
  signal before those changes land together.

Veripsa may read file contents transiently to extract the structure needed for a
signal. It does not store or display customer source file bodies or diff
contents, and it does not reuse them for code review. The retained surface is
the minimal structural and operational metadata needed to show and audit the PR
traffic decision.

---

## Current scope

- **One repository at a time.** Current coordination is between open pull
  requests within the installed repository and target branch being evaluated.
  Cross-repository coordination is on the [roadmap](./ROADMAP.md), not a shipped
  claim.
- **Language and file coverage.** The unified extractor covers Python,
  JavaScript, TypeScript, Go, Java, Ruby, PHP, C#, Rust, C, C++, and HTML.
  Coverage quality varies by repository shape; Unity-flavoured C# is partial.
  Unsupported or insufficient input resolves to Unknown rather than a guessed
  Clear.

---

## Draft PR policy

Draft PRs receive the same structural analysis as ready PRs. Two presentation
rules keep the scout window useful without weaponising unfinished work:

- **Draft ↔ ready coupling is softened.** The relationship is still shown, but
  the ready side does not receive a material `action_required` escalation solely
  because of an in-flight draft.
- **Draft participants are labelled `(draft)`.** Reviewers can distinguish work
  that is still scouting from work that is trying to land.

The intent is simple: draft is an early window into PR traffic, not a reason to
blind the analysis or to hold ready work unnecessarily.

---

## Install and first PR (when the hosted service is active)

The following flow applies only after the hosted service is explicitly resumed:

1. Visit [veripsa.com](https://veripsa.com) and follow **Install on GitHub**.
2. Select one repository first.
3. Leave branch protection and rulesets unchanged during the observation window.
4. Open or push to a pull request on a tracked branch.
5. Read the Veripsa check. A clean PR may be check-only; a comment appears when
   there is traffic worth briefing.
6. Make the check required only after your team has reviewed enough examples on
   its own repository.

See:

- [`docs/LOW_FRICTION_TRIAL.md`](./docs/LOW_FRICTION_TRIAL.md) for the selected-
  repository trial and seven-day evidence table.
- [`docs/FIRST_RUN.md`](./docs/FIRST_RUN.md) for first-run behavior, signal
  meanings, and the optional agent rule.

Installing the GitHub App does not create a Veripsa user account or collect an
email address. The service retains content-free GitHub and operational
identifiers needed for the product; some identifiers may be personal data under
applicable law. See the [Privacy Policy](https://veripsa.com/privacy) for the
current data categories, retention, uninstall, and account-erasure behavior.

Veripsa Core is free while in early access, with fair-use limits. Direct checkout
is not part of the active launch path. If paid plans are introduced later, the
expected billing path is GitHub Marketplace.

---

## Offline evaluation

To evaluate the structural signal on a local repository without installing the
GitHub App or transmitting repository contents:

```bash
python3 evaluate.py /path/to/your/repo
python3 evaluate.py /path/to/your/repo --out veripsa-report.md
```

The script uses local file paths and `git log` co-change history. It does not
retain or transmit source file contents. It exits `0` when the measured signal
beats both random and same-folder baselines on the supplied repositories.

---

## Status

- The engine, webhook runtime, and GitHub check/comment renderer source are included.
  The public CI runs a content-free publication boundary and focused unit tests;
  private production mutation workflows and operational receipts are not published.
- The hosted GitHub App is intentionally suspended. Future availability must be
  announced separately; direct checkout is not active.
- Public product facts and boundaries are centralized in
  [`docs/PRODUCT_FACTS.md`](./docs/PRODUCT_FACTS.md). Update that file first when
  category, scope, signal names, data handling, enforcement, or billing posture
  changes.
- [`docs/OPERATIONS_INDEX.md`](./docs/OPERATIONS_INDEX.md) maps first run,
  support, deployment verification, security, and publishing documentation.
- [`docs/DEPLOYMENT_VERIFICATION.md`](./docs/DEPLOYMENT_VERIFICATION.md) defines
  what must be observed before a merged change may be called deployed.
- [`ROADMAP.md`](./ROADMAP.md) separates shipped behavior from the strategic
  frontier.

---

## Security

- [`SECURITY.md`](./SECURITY.md) — threat model, content-free posture, tenant
  isolation, webhook signatures, vulnerability reporting, and incident response.
- [`github-app/RUNBOOK.md`](./github-app/RUNBOOK.md) — provider-neutral
  least-privilege, deploy, rollback, incident, and emergency-brake guidance.

To report a suspected vulnerability, use the private flow in
[`SECURITY.md`](./SECURITY.md#reporting-a-vulnerability).

---

## License

Source-available under the Business Source License 1.1. See [`LICENSE`](./LICENSE)
for the additional-use grant and the change date when the license converts to
Apache 2.0.

---

## Support

- [Support guide](./SUPPORT.md) — support metadata, issue routing, and security
  boundaries.
- [Marketing site, install, and free-access terms](https://veripsa.com).
- Public issues belong in this repository's issue tracker. Do not file security
  reports publicly.
- [Security disclosures](./SECURITY.md).
