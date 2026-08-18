# Coordination Brief — internal design (v1)

Status: design + reference implementation (schema, pure serializer, fixtures, tests). **No transport / API is
wired.** Read the *Safe-read-path decision* section before adding one.

## 1. What it is (and what it is not)

The **Coordination Brief** is a stable, read-only, versioned, content-free **JSON form of the exact same
pre-merge coordination judgment Veripsa already renders as a GitHub check + PR comment**. Where
`github-app/render.py` turns `core.main_impact_surface(repo, branch)` into human markdown, the Brief
(`github-app/coordination_brief.py`) turns the **same surface** — the same per-change `me` dict — into JSON an
in-flight AI agent can read *before it merges*, without scraping prose.

It **is**:

- a **serialization** of an existing verdict. It adds no new engine computation; every field is a projection of
  what `render_pr_check` / `apply_pause_ack` already have.
- **read-only**. The function returns a value. No write, no GitHub call, no DB call, no agent action, no merge.

It is **not**: an AI code reviewer, a CI replacement, a merge-queue replacement, a proof a PR is safe, an
auto-fixer, or a write/command channel for agents. It carries the same advisory framing as the check: Veripsa
records and steers; the customer's branch-protection policy decides what blocks.

## 2. Grounding in the real engine (no invented fields)

Every relationship kind maps 1:1 to a **shipped** capability of the engine (the 22-capability atlas). The
serializer reads only fields `main_impact_surface` actually emits (`db/schema/80_contention.sql`) and reuses the
render layer's own logic so the Brief can never drift from the check/comment:

| Brief `relationship_kind` | Engine source (`me.*` unless noted) | Atlas capability |
|---|---|---|
| `conflict_marker` | `conflict_markers` (caller) → `render._conflict_marker_findings` | 14 unresolved marker (hard fail) |
| `direct_collision` | `serialize_behind` + `collision_points` | 1, 2 direct / symbol-locus collision |
| `likely_rebase_conflict` | `merge_conflict_likely` + `conflict_points` | 3 likely rebase/textual conflict |
| `holding_lane` | `queued_behind` + `queued_behind_paths` | 4 holder/waiter (holder side) |
| `depends_on_changing_contract` | `depends_on_changing` | 9 upstream dependency being changed |
| `semantic_coupling` | `contested_with`; corroborated `dampened_with` | 7, 8, 11 semantic coupling / shared table/config / co-change-corroborated |
| `suppressed_coupling` | uncorroborated `dampened_with` | 16 hub-dampened (silence made visible) |
| `blast_radius` | `impact` (downstream) | 10 blast radius |
| `landing_order` | `clusters[].suggested_order` | 5, 6 landing order / cluster |
| `recurring_contention` | `shared_foundation` | 12 recurring contention / split advice |
| `co_change` | cochange rows (caller) | 11 co-change (empirical) |
| `main_moved` | branch∩PR changed paths (caller) → `coordination_brief_for_event` | 13 main moved under the PR |
| `unverified_new_path` / `unverified_gap_path` | `unknown_paths` split by `added_paths` | 15 new-path vs extractor-gap Unknown |
| `truncated_analysis` | `truncated` (caller) | 17 truncated / partial analysis |
| (ack overlay, not a relationship) | `render_pauseack.apply_pause_ack` | 18 ACK snapshot + stale re-ACK |
| (`state: clear` + rewrite) | verdict `clear` | 19, 22 cleared transition / check-only clean PR |
| (`redacted: true`) | `is_fork` redaction | fork info-leak guard |
| (draft `(draft)` softening) | `is_draft` per change | 20 draft scout policy |
| (branch-reservation advisory) | `serialize_soft` → `state: heads_up` | 21 branch reservation advisory |

The five reused render primitives are imported directly (`_effective_verdict`, `is_material_coupling`,
`coupling_snapshot`, `_ack_partner_refs`, `apply_pause_ack`, `_conflict_marker_findings`, `_split_unknown_paths`),
so a change to the verdict/ack contract updates the Brief in lock-step.

## 3. The shape (v1)

Top level (schema: `docs/design/COORDINATION_BRIEF.schema.json`, draft 2020-12):

```json
{
  "schema_version": "1",
  "kind": "veripsa.coordination_brief",
  "repository": "acme/app",
  "base_branch": "main",
  "acting_change": "PR-44",
  "acting_head_sha": "9988aabbcc",
  "base_sha": "base2222cccc",
  "state": "wait_in_line",
  "action_required": true,
  "enforcement": "advisory",
  "relationships": [ /* deterministically ordered */ ],
  "ack":       { "label": "veripsa-ack", "required": true, "present": false,
                 "state": "paused", "coupling_snapshot": "…12hex…", "label_action": null },
  "freshness": { "evaluated_at": "…|null", "head_matches": true, "analysis_truncated": false }
}
```

Each relationship:

```json
{ "relationship_kind": "direct_collision", "related_change": "PR-41",
  "evidence_kind": "structural", "subject": "route_request",
  "locus": { "path": "core/router.py", "symbol": "route_request", "line_lo": 88, "line_hi": 120 },
  "recommended_action": "land_in_order", "requires_human_ack": true, "confidence": "supported" }
```

### Bounded enums (serializer tuples == schema enums, asserted by the test)

- `state`: `clear · heads_up · wait_in_line · unknown` — **the four base states, exactly four.**
- `relationship_kind`: the 15 kinds in the table above.
- `evidence_kind`: `structural · historical · textual · unverified`.
- `recommended_action`: `land_in_order · align_before_merge · coordinate_before_merge · rebase_before_merge ·
  resolve_conflict_marker · free_the_lane · consider_split · investigate · monitor`.
- `confidence`: `certain · corroborated · supported · likely · unverified` (an honest ladder: `certain` only for
  the deterministic build-breaker; `corroborated` = two independent signals agree; `supported` = one direct
  structural edge / surviving collision; `likely` = a content-free heuristic, "likely" not "will"; `unverified` =
  could not compute / suppressed).
- `ack.state`: `not_material · paused · acknowledged · stale_reack · ack_verification_pending`.

### Verdict-contract consistency

`state` maps 1:1 from the engine verdict ladder: `clear→clear`, `warn→heads_up`, `serialize→wait_in_line`,
`serialize_soft→heads_up`, `unknown→unknown`. The Brief carries the **effective (per-path) verdict** —
`render._effective_verdict` — so a PR whose in-graph part is clear but that also adds a new file reports
`state: clear` with an `unverified_new_path` note, exactly like the comment.

### Paused is an overlay, not a fifth state

A material coupling that is un-acked does **not** create a `paused` state. The pause rides three fields, mirroring
how `apply_pause_ack` overlays `action_required` on the check:

- `ack.state` ∈ `{paused, stale_reack}` (or `acknowledged` once the label binds),
- `action_required: true`,
- `requires_human_ack: true` on the specific material relationship(s).

`requires_human_ack` is set on **exactly** the relationships whose `related_change` is in
`coupling_snapshot`'s partner set (`_ack_partner_refs`) and only when the change is material — so the Brief's ack
flags can never disagree with what the label actually binds to. The test asserts this equivalence.

`action_required` is the **only** merge-gating signal, and only when the customer has marked the Veripsa check
required in branch protection. Its two causes: an unresolved conflict marker (a deterministic build-breaker,
`confidence: certain`, but **not** the ack path — resolve the marker, there is no label), or a paused/stale
material coupling.

## 4. Content-free / moat discipline

Allowed (all already customer-facing on the comment): file paths, symbol names, line numbers, in-flight PR/branch
refs, land-order positions, a 12-hex coupling hash, qualitative `reach` (`local`/`wide`) and `basis`
(`foundation`/`god_file`/`both`).

Never present: source bodies, diff bodies, secrets, and **raw graph-size counts** — fan-in, symbol counts,
downstream-file counts, co-occurrence counts, lift multipliers, thresholds. Breadth is qualitative
("wide", `more_related_paths: true`), never a number. In-flight PR **counts** are customer-facing (the comment
numbers them) and are represented as ref lists, not graph size. This is enforced three ways: the schema's
`additionalProperties: false` on every object, a forbidden-key scan, and a sentinel-count leak test that drives
raw `fan_in/churn/symbols/impact_count/lift` inputs through the serializer and asserts none of the numbers reach
the Brief.

## 5. Pinned + deterministic

Every Brief is tied to an exact `acting_head_sha` (from the surface's per-change `head_sha`, overridable by the
caller) and, when the caller supplies it, `base_sha`. `freshness.head_matches` (caller-supplied — the webhook
holds both the analyzed head and the current head) tells a consumer whether the Brief is stale. The serializer
reads no clock, no random, no network; `evaluated_at` is a **caller input**, never `datetime.now()`, precisely so
`same inputs → byte-identical Brief` holds (fixture-regression + determinism tests).

Note on `base_sha`: the engine tracks base **per-path** (`core.claim.base_hash` / `_set_claim_base_hash`), not as
one branch-tip SHA, and `main_impact_surface` does not emit a single base SHA. So `base_sha` is a caller-pinned
field the delivery path supplies from the event's `base.sha`; it is `null` when unavailable rather than
fabricated.

## 6. Safe-read-path decision — **design-only; no API added**

**Audit result: there is no existing authenticated, tenant-isolated, per-repo/per-PR HTTP read path to serve this
Brief through, and adding one would create a new auth/RLS surface on a high-risk boundary. So v1 adds no public
API.**

What exists today (`github-app/server_http.py`): the HTTP surface is **operational probes only** — `/healthz`,
`/readyz`, `/freshz`, `/alarmz`, `/statusz`, `/deliveryz`. Every one is either operator-facing or deliberately
**content-free and tenant-stripped**: `_public_freshness_payload` / `_public_status_payload` allowlist their
responses precisely so no repo identity or per-tenant data leaks. `/deliveryz` is HMAC-gated to an internal probe.
**None** is a per-repository, per-PR, tenant-authenticated data read. This matches the product model: Veripsa is
delivered *as a GitHub App* (`render.py` header: "there is no console to visit for the core job; the real-time
dashboard is deferred/retention-only"). The read surface `main_impact_surface` is a `SECURITY DEFINER` function
that pins `core.current_account` from `resolve_session_identity()` and is `REVOKE`d from `PUBLIC` — it is reached
only inside the webhook worker's already-authenticated, RLS-pinned transaction, not from any HTTP endpoint.

Therefore the Brief's delivery channel in v1 is the **channel that already exists and is already tenant-isolated:
the GitHub Checks / PR-comment surface the webhook worker already writes to.** The wiring point is the poster that
already calls `render.render_pr_check` + `render.apply_pause_ack` for a PR event
(`github-app/webhook.py` / `webhook_posters.py`): it has, in one hand, the same `impact`, `change_ref`, `is_fork`,
`truncated`, `added_paths`, `conflict_markers`, cochange rows, ack label state, and the event's head/base SHAs. It
can call `coordination_brief(...)` and attach the JSON to the **check run's structured `output`** (or a fenced
```json block the agent already reads on the comment). Access to that surface is gated by GitHub's own repo-read
permission — the same gate the human check/comment already rely on. **No new Veripsa-hosted endpoint, no new
token, no new auth, no change to RLS or tenant isolation.**

Tenant isolation, then, is inherited, not re-implemented: (a) the data is produced inside the existing
account-pinned worker transaction; (b) it is delivered onto the repo's own check surface, which only that repo's
readers can see; (c) the serializer itself holds no credentials and makes no cross-tenant read. The fork
redaction (`is_fork`) is preserved end-to-end: a fork PR's Brief drops every cross-change ref and base-repo path,
exactly like the redacted comment.

**Default to design-only was applied deliberately.** If a future dedicated agent-read transport is ever
considered (e.g. an authenticated MCP resource), it must reuse the existing installation-auth +
`resolve_session_identity` + RLS path and stay read-only; it must not become a new unauthenticated API. That is a
separate, PO-gated decision and is out of scope here.

## 7. Public future boundary — what is deliberately NOT in v1

- **No write / command channel.** The Brief is read-only. There is no agent-operation, auto-ack, auto-rebase, or
  autonomous-merge surface, and none is designed. `veripsa-ack` remains a human/agent GitHub-label action outside
  this schema; the Brief only *reports* ack state.
- **No new public API.** See §6. v1 rides the existing tenant-isolated GitHub delivery channel.
- **No cross-repo / cross-account relationships.** The Brief is scoped to one repo + one protected branch, like
  the engine surface. The dormant cross-repo foundation stays out; a Brief never names another account's work.
- **No agent-injection / prompt surface.** The Brief is data, not instructions. It carries no natural-language
  directive an agent should "follow"; `recommended_action` is a bounded enum a consumer interprets, not free text.
- **No engine internals.** No scoring, routing, graph-construction, feature-extraction, prioritization, thresholds
  or unpublished metrics — the moat rules apply to this machine surface as strictly as to the prose.
- **No mutation of the check/comment contract.** The Brief is additive; it does not change what
  `render_pr_check` emits. This design touches no engine code.

## 8. Files

- `docs/design/COORDINATION_BRIEF.schema.json` — the versioned JSON Schema (draft 2020-12).
- `github-app/coordination_brief.py` — the pure, content-free serializer (+ `coordination_brief_for_event`
  adapter that threads the caller-computed `main_moved` overlap).
- `docs/design/coordination_brief_fixtures/*.json` — five representative fixtures (`input` + expected `brief`):
  `clear`, `heads_up_semantic_coupling` (acknowledged overlay), `wait_in_line` (landing order + holder/waiter,
  paused), `unknown` (gap path + suppressed coupling), `action_required_ack` (stale re-ack).
- `tests/test_coordination_brief.py` — schema validity, enum parity + boundedness, content-free, determinism,
  fixture regression, paused-is-overlay, ack↔`apply_pause_ack` consistency, fork redaction, never-crash.
