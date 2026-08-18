#!/usr/bin/env python3
"""Cross-repo contract-key emission — the SHADOW signal-validation leaf (feasibility-spike STEP 1).

WHAT (the bet): two SEPARATE repos under ONE owner share a contract — repo A (the producer) DEFINES a
REST operation / GraphQL type / protobuf service / route, and repo B (the consumer) REFERENCES it. There
is NO code edge, NO import, NO shared file between the repos at all (they are different checkouts), so the
within-repo coupling graph can NEVER see it. The contract substrate (`_cg_api_contract` / `_cg_openapi` /
`_cg_routes`) ALREADY mints cross-repo-STABLE keys (`api_schema::Order`, `api_operation::confirmOrder`,
`route::GET /api/orders/{}`) that land in `core.code_edge.dst`; and `res_adj` (db/schema/70_social.sql)
ALREADY couples two files that share a `dst`. The ONE missing piece on the CONSUMER side: the extractors are
repo-LOCAL — a reference whose definition is absent from the SAME repo is DISCARDED (the two-pass known-set
guard: `known = frozenset(all_defined)`; a ref not in `known` mints no edge). So repo B's consumer reference
to repo A's contract is dropped before it can land. This leaf is the consumer-side emission that recovers it.

STRICTLY ADDITIVE + FLAG-GATED (the spike flagged extraction PRECISION as THE risk → the within-repo
product must NOT change). Everything here is behind `VERIPSA_CROSS_REPO_KEYS` (default OFF). When OFF,
`cross_repo_keys_enabled()` is False and every caller's cross-repo branch is skipped entirely, so the
within-repo extraction is BYTE-IDENTICAL (the determinism gate still passes — proven by the new gate).

PRECISION DISCIPLINE (unchanged from the within-repo detectors): a cross-repo reference is emitted ONLY when
it passes the SAME specificity / stoplist floors the within-repo path already enforces — the caller is
responsible for applying its own floor (`_GQL_STOPLIST` / `_PROTO_STOPLIST` / `_PATH_STOPLIST` / `_UBIQUITOUS`
/ `_MIN_PATH_SEGMENT_LEN` / the multi-definer guard). This leaf only owns (a) the flag and (b) the additive
helper that lets a caller emit a cross-repo `queries` edge to a stable contract `dst` for a reference whose
DEFINITION is NOT in the same repo. The KEY is the SAME string the producer side already mints, so the two
land on the same `code_edge.dst` and `res_adj` couples them — zero new edge kinds, zero schema change on the
node/edge side. content-free: contract NAMES / paths only, never bodies.

SAME-ACCOUNT ONLY (this phase): both repositories must have one owner. There is NO consent / cross-tenant
machinery here — that is a separate future phase. The cross-
repo coupling READ (the relaxed `res_adj`) is itself behind a separate kill switch `VERIPSA_CROSS_REPO`
(db/schema/70_social.sql), same-account scoped.
"""
from __future__ import annotations

import os

# Env flag name for the CONSUMER-SIDE key emission (extraction). Default OFF.
_FLAG_KEYS = "VERIPSA_CROSS_REPO_KEYS"


def _truthy(v: "str | None") -> bool:
    """A conservative truthy parse for an env flag: 1/true/yes/on (case-insensitive). Anything
    else — including unset, '0', '', 'false' — is OFF. Never-crash (pure string compare)."""
    if not v:
        return False
    return v.strip().lower() in ("1", "true", "yes", "on")


def cross_repo_keys_enabled() -> bool:
    """True iff the consumer-side cross-repo key emission is turned ON via VERIPSA_CROSS_REPO_KEYS.
    Read live from the environment on every call (so a test child process / a single gate run can
    flip it without import-time caching surprises). Default OFF → within-repo extraction unchanged."""
    return _truthy(os.environ.get(_FLAG_KEYS))


def xrepo_reference_edges(rel, dst_ids, *, local_defs):
    """Build the additive cross-repo `queries` edges for ONE consumer file.

    `rel`        : the consumer file's repo-relative path (the edge `src`).
    `dst_ids`    : an iterable of contract node-ids (`api_*::Name` / `route::...`) this file REFERENCES
                   and that ALREADY passed the caller's specificity/stoplist floor. These are the SAME
                   stable keys the producer side mints, so a cross-repo definer's `alters` edge and this
                   `queries` edge land on the same `code_edge.dst` → `res_adj` couples the two files.
    `local_defs`: the set of node-ids DEFINED in THIS repo (so a reference whose definition IS local is
                   left ENTIRELY to the within-repo path — we never double-emit it here). This is the
                   crux of the additive guarantee: cross-repo emission only covers the references the
                   within-repo path DROPPED (definition absent from the same repo).

    Returns a list of {"src","dst","kind":"queries"} edges (possibly empty). When the flag is OFF this
    returns [] WITHOUT reading `dst_ids` (so the caller's cross-repo scan is skipped at the call site,
    keeping the OFF path byte-identical). content-free: ids (names/paths) only. never-crash."""
    if not cross_repo_keys_enabled():
        return []
    out = []
    seen = set()
    for dst in dst_ids:
        if not isinstance(dst, str) or not dst:
            continue
        if dst in local_defs:
            # Definition is in THIS repo → the within-repo path already handles it. Do not double-emit
            # (keeps the cross-repo branch purely ADDITIVE — it only adds the missing no-local-def refs).
            continue
        if dst in seen:
            continue
        seen.add(dst)
        out.append({"src": rel, "dst": dst, "kind": "queries"})
    return out
