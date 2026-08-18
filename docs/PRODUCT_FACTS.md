# Product facts

> Availability note: the hosted service is intentionally suspended as of 2026-08-18.
> Capability descriptions below document the implemented source contract, not a claim
> that the hosted GitHub App is currently processing events.

This file is the first place to update when a public Veripsa Core product fact changes.

Use it to keep README, public documentation, support snippets, and repository copy aligned.

Last reviewed: 2026-07-21.

## How to use this file

- Keep each fact short and reusable.
- Link to this file from docs that repeat these facts.
- Do not copy internal implementation details into public copy.
- When a fact changes, update this file first, then search and update every public surface that repeats it.

## Public copy principle: truthful confidence

One sentence: **do not claim unproven effects, but do not advertise that things are unproven either.**

- State the value Veripsa Core delivers now — the real GitHub output, the current scope, how to install — in positive, concrete language, first.
- Do not present an unproven effect as if it were measured fact.
- Do not present unmeasured effects, customer outcomes, or ROI as established facts.
- State a constraint once, in the place where it changes the reader's decision or operation (Compare, Docs, Trust, Pricing, Procurement) — never as a stacked list of negations, and never repeated across every page.
- Prefer the affirmative "current scope" form over "what this does not do."
- Keep legally or security-required disclosures — they live on Trust / Legal / Procurement, stated once.
- These facts are public-safe. Internal-only facts (proof status, design-partner progress, conversion, effect-measurement plans, paid-plan candidates, certification roadmap) do not belong in any public surface. Classify before you publish.

## Shipped product facts

### F-001 Product name

Canonical wording:

```text
Veripsa Core
```

Use `Veripsa Core` when describing the GitHub App behavior. Use `Veripsa` only for the broader brand/site context.

### F-002 Product category

Canonical wording:

```text
Veripsa Core is a GitHub App for pre-merge PR traffic control.
```

Expanded wording:

```text
Veripsa Core shows traffic between open pull requests, suggested landing-order attention, and optional acknowledgement controls on GitHub before merge.
```

The category is **pre-merge PR traffic control** and does not change. The value that category delivers is **shared coordination context** (F-023): traffic control is the action derived from that context. Do not rewrite the category into "multi-agent orchestration platform", "AI operating system", "agent memory platform", "AI code reviewer", or "automatic merge tool".

### F-003 Primary product surface

Canonical wording:

```text
Veripsa Core reports through GitHub checks and, when useful, PR comments.
```

Current shipped surfaces:

- GitHub App installation
- PR opened/synchronized processing
- branch/push traffic where it updates open PR state
- GitHub check runs
- PR comments when there is traffic worth briefing
- ACK labels/comments/check actions where supported
- dashboard/support views where shipped

### F-004 Not GitHub Issues as product surface

Canonical wording:

```text
GitHub Issues may be used as optional task intake, but Veripsa Core's product surface is PR / branch / push / GitHub check / PR comment / ACK.
```

Do not write copy that implies Veripsa Core manages Issues.

### F-005 Not AI code review

Canonical wording:

```text
Veripsa Core is not an AI code reviewer. It does not review implementation quality; it shows merge-order and overlap signals between PRs.
```

Use this mainly in comparison, Marketplace, support, and product overview contexts. Do not insert it abruptly into unrelated SEO articles.

### F-006 Not CI replacement

Canonical wording:

```text
Veripsa Core does not replace CI. CI checks one PR; Veripsa Core checks traffic between open PRs before merge.
```

### F-007 Not merge queue replacement

Canonical wording:

```text
Veripsa Core does not replace merge queue. Merge queue stabilizes ready PRs at final merge time; Veripsa Core surfaces open-PR traffic earlier.
```

### F-008 Advisory by default

Canonical wording:

```text
Veripsa Core is advisory by default. To make action_required hold merges, add the Veripsa Core check to GitHub branch protection or rulesets where your GitHub plan supports required checks.
```

Do not claim Veripsa Core blocks merges automatically.

### F-009 Clear is not correctness

Canonical wording:

```text
Clear means no blocking PR traffic is currently visible in the analyzed surface. It is not proof that the PR is correct.
```

### F-010 Unknown is not Clear

Canonical wording:

```text
Unknown means Veripsa Core does not have enough signal to judge this surface. Do not treat Unknown as Clear.
```

### F-011 Clean PRs may be check-only

Canonical wording:

```text
Clean PRs may only show a GitHub check. A PR comment appears when there is traffic worth briefing.
```

Do not promise a PR comment on every PR.

### F-012 ACK semantics

Canonical wording:

```text
ACK records that a human saw the current warning snapshot and chose to proceed. It is not code review approval, not conflict resolution, and not a permanent mute.
```

### F-013 Content-free storage boundary

Canonical wording:

```text
Veripsa Core does not store or display source file bodies or diff bodies. It retains operational metadata such as PR number, path, signal state, check/comment identifiers, and ACK state to show and audit PR traffic decisions.
```

Avoid:

```text
Veripsa Core stores nothing.
```

Avoid:

```text
Veripsa Core never reads file content.
```

Reason: file data may be read transiently for signal generation. The public boundary is storage/display/code-review output, not transient read behavior.

### F-014 Allowed operational metadata

Canonical categories:

- installation id
- repository id / owner/name where needed
- PR number
- branch / commit SHA
- path / line range where applicable
- signal state
- check/comment identifiers
- ACK actor/state/timestamp
- event identifiers where useful

### F-015 GitHub App permission and event boundary

Canonical wording:

```text
Veripsa Core requests repository `contents: read`, `pull_requests: write`, `checks: write`, `merge_queues: read`, and `metadata: read`; it subscribes to pull_request, push, repository, check_suite, check_run, and merge_group events. It does not request Issues permission and does not subscribe to issues, issue_comment, or sub_issues webhooks.
```

GitHub PR conversation comments and labels are issue-backed REST endpoints. Explain that implementation detail only in permission, security, Marketplace reviewer, or webhook-spec contexts; do not turn it into user-facing product scope.

### F-016 Demonstration boundary

During the hosted-service suspension, no historical check run is evidence of current availability. Use the
offline evaluator and the synthetic fixtures in this repository. Any future hosted demonstration must be
labelled with its observation time and service state; never present fixture output as a live result.

### F-017 Marketplace review state

Canonical wording:

```text
The project-hosted Veripsa service is intentionally suspended as of 2026-08-18. Marketplace review or prior App registration is not evidence that installation or hosted processing is currently available.
```

Do not claim a current review state, live install path, or available hosted
service until a separately verified resumption announcement is published.

### F-018 Pricing posture

Canonical safe wording:

```text
Early access / free-first while Veripsa Core is being validated.
```

Avoid:

```text
Free forever
```

Avoid paid-plan claims unless implemented and approved.

### F-019 Current scope

Canonical wording:

```text
Veripsa Core's current scope is the open pull requests within a single repository — their relationships and landing order. Coordination across separate repositories, even under the same GitHub owner, is not shipped yet.
```

Prefer "within a single repository" over "same-owner" in public copy: "same-owner" reads as if repositories are coordinated across the account, which is not shipped. Do not claim cross-repository or cross-owner coordination, write-capable MCP, or autonomous merge as shipped.

### F-020 Low-friction trial posture

Canonical wording:

```text
Start with one selected repository in advisory mode. Keep branch protection unchanged while you observe the signal on real PRs, then make the Veripsa Core check required when it fits your workflow.
```

Japanese canonical wording:

```text
まずは1つの selected repository で advisory mode から始めます。branch protection は変更せずに signal を確認し、運用に合うと判断してから Veripsa Core の check を required へ昇格できます。
```

Use this affirmative wording in onboarding, support replies, Marketplace reviewer responses, and public copy that addresses installation. It describes how a team starts small; it is not a claim of proven effect.

Uninstall is an offboarding fact, not marketing copy. Do not write "if the warnings are not useful, uninstall it" (or its Japanese equivalent) on marketing surfaces. Explain how to uninstall, and what uninstall does to data, only in Docs offboarding, Support, and Trust — see F-021.

### F-021 Uninstall and account erasure

Canonical wording:

```text
Uninstalling Veripsa Core from a repository stops all processing for that account and purges the content-free working set Veripsa Core can rebuild from GitHub. Account erasure is a separate, complete request that also removes append-only advisory history and account lifecycle records.
```

This is a data-handling fact. Place it in Docs offboarding, Support, Trust, Privacy, and DPA. Do not place uninstall instructions on marketing hero/CTA surfaces.

### F-022 Traffic signals and acknowledgement overlay

Canonical wording:

```text
Veripsa Core has four base traffic signals: Clear, Heads up, Wait in line, and Unknown. A material coupling may add Paused (acknowledge to proceed) as an action_required acknowledgement control overlay; Paused is not a fifth traffic verdict.
```

Use these definitions consistently:

- `Clear`: no blocking traffic is visible in the analyzed comparison set; not approval, correctness, or safety proof.
- `Heads up`: a relationship worth reviewing; normally advisory and acknowledgement-free.
- `Wait in line`: strong overlap or landing-order risk; the base traffic recommendation is advisory by default.
- `Unknown`: insufficient input, coverage, or comparison context; never equivalent to Clear.
- `Paused (acknowledge to proceed)`: a material-coupling control overlay that produces `action_required`; it holds a merge only when the customer made the Veripsa check required through GitHub policy.
- `Acknowledged`: the human recorded that they saw the specific coupling snapshot and chose to proceed; it is not approval or conflict resolution.

Avoid presenting `Warn`, `Soft pause`, `Clear to land`, bare `Wait`, `Blocked`, or `Paused` as alternative base verdict names.

### F-023 Core value: shared coordination context

Canonical wording:

```text
Veripsa Core keeps shared coordination context on GitHub for parallel coding agents and humans: what other in-flight work is changing, why it relates to the current PR, and what should happen next before merge.
```

Japanese canonical wording:

```text
Veripsa Core は、並行して動く coding agents と人間のために、他の in-flight work が何を変えているか、なぜ現在の PR と関係するか、merge 前に次にどう動くべきかを、GitHub 上の共有 coordination context として維持します。
```

This is the **value** the `pre-merge PR traffic control` category (F-002) delivers. The four traffic signals (F-022) are the actions derived from that shared context; the context is why they are actionable. Lead public value copy with this, not with the storage boundary.

Boundaries on this fact:

- It is repository-wide, continuous, vendor-neutral, shared on GitHub, updated on push/merge/close, tied to specific SHAs, and connectable to ACK / required checks. State those properties; they are what distinguishes it from asking one agent to read every open PR once.
- Do not describe it as automatic injection into an agent's session, agents talking to each other, cross-repository shared memory, or Veripsa being "smarter than" Claude/Codex. Shared coordination context is a customer value, not a licence to expand the product category.
- Keep it within shipped scope: the shared context is the GitHub check, the PR comment, ACK state, and open-PR/branch/push traffic state — readable by a human and by an agent that can read GitHub state. It is not pushed into any model's context window by Veripsa.

### F-024 Primary initial user includes solo and small multi-agent developers

Canonical wording:

```text
A solo developer or small team running several coding agents (Claude Code, Codex, Copilot) in parallel is a primary user of Veripsa Core, not only large teams. The value appears whenever separate work contexts run in parallel, not when a certain headcount is reached.
```

Japanese canonical wording:

```text
複数の coding agent を同時に動かす一人または少人数の開発者も、Veripsa Core の主要な利用対象です。価値が生まれる条件は人数ではなく、分離した作業 context が複数並行して存在することです。
```

Do not write copy that assumes a multi-person team ("Team decision", "your team reviews", "for teams") as the only unit. Use a neutral actor ("you", "あなた") and name the solo-multi-agent case explicitly on the hero support line and product intro.

### F-025 Not a one-shot analyzer

Canonical wording:

```text
Veripsa Core's value is not comparing two PRs once. It keeps the shared coordination state current as PRs are opened, pushed, merged, closed, acknowledged, and as heads change.
```

Japanese canonical wording:

```text
Veripsa Core の価値は、二つの PR を一度だけ比較することではありません。PR 作成・push・merge・close・ACK・head 更新に合わせて、共有された coordination state を更新し続けることです。
```

Use this to distinguish Veripsa Core from asking an agent to read all open PRs once: the state is continuous and re-evaluated on each event, tied to the current head, and stale acknowledgements are re-paused (F-012). Do not claim it re-evaluates instantly or with zero latency.

### F-026 Persistent history, disposable context

Canonical wording:

```text
Veripsa Core does not build a permanent memory of your repository. It derives current coordination context from open pull requests and their exact heads, then invalidates and regenerates that context as the repository changes. History and human decisions persist; current meaning and recommended action are regenerated.
```

Short principle:

```text
Persistent history. Disposable context.
```

Japanese canonical wording:

```text
Veripsa Core は、現在 open な PR とその exact head から、今有効な coordination context を生成します。履歴と人間の判断は残しますが、現在の意味と推奨行動は状態が変わるたびに作り直します。
```

This explains *how* the shared coordination context (F-023) stays current: Veripsa persists facts, history, and human decisions (events, exact SHAs, ACK decisions), and re-derives current relationships, landing order, and recommended actions from current GitHub state each event. It is why a Veripsa judgment is tied to an exact head and why a stale acknowledgement is re-paused (F-012).

Boundaries on this fact:

- Do **not** call this "shared memory", "agent memory", "memory platform", "repository knowledge base", or say Veripsa "remembers your repo" or "permanently stores the meaning of your code". Veripsa retains bounded coordination records, not a durable copy of repository meaning.
- The product category is unchanged (F-002): pre-merge PR traffic control. This fact describes freshness, not a new product surface.
- Content-free (F-013) still holds: what persists is operational metadata, exact SHAs, and human decisions — never source file bodies, diff bodies, model-generated summaries treated as truth, or free-form agent memory.

### F-027 Very short listing description

Canonical very-short description (the GitHub Marketplace tagline / listing-header field):

```text
Pre-merge PR traffic control for parallel AI-agent pull requests
```

This is the single source of truth for the very-short description. It is functionality-first and aligned to the category (F-002) and single-repository scope (F-019); do not fork a second tagline.

Avoid CTA or safety-overclaiming taglines such as "Install Veripsa Core to make AI PRs safe".

## Future candidates / not shipped claims

Do not describe these as shipped unless implementation and product approval exist:

- Veripsa MCP
- cross-owner coordination
- autonomous merge
- automatic conflict fixing
- paid Marketplace plans
- enterprise audit export
- broad cross-repo governance
- source-aware code review

## Update checklist

When a product fact changes:

1. Update this file.
2. Search affected public source surfaces.
3. Run `python3 scripts/public_snapshot_gate.py`.
4. Run the smallest relevant public-source tests.

## Related docs

- `README.md` is the canonical public availability surface.
