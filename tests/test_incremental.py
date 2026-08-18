#!/usr/bin/env python3
"""Incremental-ingest gate — a safe PATCH must equal a FULL re-ingest of the same end-state.

Production reconstructs the bounded retained target-SHA context before persisting a touched-path slice; any
case whose unchanged owners would change falls back to full. This older low-level fixture also exercises the
mechanical SQL patch primitive by extracting changed files against a path universe, then explicitly records
the raw divergence (deletion/inert-reference cases) which the production guard must reject. Correctness is not
traded for the touched-row persistence win.

Scenario (root-level python modules, basename imports so the resolver resolves cleanly):
  base:  alpha (def fa) · beta(import alpha) · gamma(def fg) · epsilon(import delta) · delta(def fd)
  change: MODIFY beta  → import gamma (re-point to another UNCHANGED file)
          ADD    zeta  → import alpha (new file → an UNCHANGED file: OUTGOING resolves)
          MODIFY gamma → import zeta  (an EXISTING file imports the NEW file in the SAME push: the realistic
                                       add — its INCOMING edge to the added file must resolve)
          REMOVE delta                (a file an UNCHANGED file imported → dangling incoming edge cleaned)

Run:  python3 tests/test_incremental.py   (needs local Postgres with the veripsa roles)
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "github-app"))   # exercise the REAL server guard, not a re-implementation
import psycopg2  # noqa: E402
import code_graph_extract as X  # noqa: E402
import cg_schema_contract as C  # noqa: E402
import ingest as I  # noqa: E402
import server as S  # noqa: E402

# PROCESS-UNIQUE (parallel-safe): the gate bootstraps + drops this DB, so a FIXED name lets concurrent runs
# (parallel CI shards / several agents each running run_gates) drop each other's DB mid-run → "does not exist"
# crashes. Per-PID, exactly like db/smoke.sh (veripsa_smoke_$$), run_gates (veripsa_gates_$$), test_server.py.
DB = "veripsa_incrtest_" + str(os.getpid())
SHA1 = "1" * 40
SHA2 = "2" * 40

BASE = {
    "alpha.py": "def fa():\n    return 1\n",
    "beta.py": "import alpha\n\ndef fb():\n    return alpha.fa()\n",
    "gamma.py": "def fg():\n    return 3\n",
    "epsilon.py": "import delta\n\ndef fe():\n    return 5\n",
    "delta.py": "def fd():\n    return 4\n",
}
# the end-state after the push: beta re-points to gamma, zeta is added (importing alpha), gamma is modified
# to import the new zeta, delta is removed.
BETA_NEW = "import gamma\n\ndef fb():\n    return gamma.fg()\n"
ZETA_NEW = "import alpha\n\ndef fz():\n    return alpha.fa()\n"
GAMMA_NEW = "import zeta\n\ndef fg():\n    return zeta.fz()\n"
CHANGED = ["beta.py", "gamma.py", "zeta.py"]   # added + modified
REMOVED = ["delta.py"]


def write_tree(files: dict) -> str:
    d = tempfile.mkdtemp(prefix="vp-incr-")
    for rel, body in files.items():
        dest = os.path.join(d, rel)
        os.makedirs(os.path.dirname(dest), exist_ok=True)   # nested paths (lib/helper.py) for the move cases
        with open(dest, "w") as fh:
            fh.write(body)
    return d


def make_db(role):
    def run(sql, args=()):
        conn = psycopg2.connect(f"postgresql://{role}@localhost/{DB}")
        try:
            with conn, conn.cursor() as cur:
                cur.execute("SET search_path=core")
                cur.execute(sql, args)
                row = cur.fetchone()
                return row[0] if row else None
        finally:
            conn.close()
    return run


def graph_of(admin, repo):
    """(nodes, edges) for a coordinate as comparable sets, read as the owner with RLS pinned.

    Node comparison includes content_hash + start_line + end_line (the content-free freshness key and the
    symbol spans), NOT just (id,kind,path,name,language): those three were the BLIND SPOT that let the #210
    content_hash-drop ship green — a patch that dropped them still equalled a full re-ingest on the narrow
    tuple. Post-#210 the patch path carries all three, so full≡patch must hold on them too (NULLs compare
    equal as the JSON null sentinel). They are appended AFTER the original five so every existing unpack keeps
    its positions (the trailing fields are absorbed with `*_`)."""
    nodes = admin(
        """
        SELECT set_config('core.current_account','ACCT-DEMO',true);
        SELECT COALESCE(json_agg(json_build_array(node_id,node_kind,path,COALESCE(name,''),COALESCE(language,''),
                                                  content_hash,start_line,end_line)
                                 ORDER BY node_id)::text,'[]')
          FROM core.code_node WHERE repo=%s AND branch='main'
        """, (repo,))
    edges = admin(
        """
        SELECT set_config('core.current_account','ACCT-DEMO',true);
        SELECT COALESCE(json_agg(json_build_array(src,dst,edge_kind) ORDER BY src,dst,edge_kind)::text,'[]')
          FROM core.code_edge WHERE repo=%s AND branch='main'
        """, (repo,))
    nset = {tuple(x) for x in json.loads(nodes)}
    eset = {tuple(x) for x in json.loads(edges)}
    return nset, eset


def live_edges_with_paths(eset, file_paths):
    """Drop INERT import edges (dst is not a file path in the graph) — an unresolved module name that names
    no file. A full re-ingest keeps such a string edge for an unchanged file that imported a now-removed file;
    a patch drops it. Both are inert (the adjacency engine joins dst→node and finds nothing), so the LIVE
    graph that actually drives the product is what must match."""
    return {(s, d, k) for (s, d, k) in eset if not (k == "imports" and d not in file_paths)}


def live_imports(eset, fp):
    """The LIVE import edges only — dst is a file path in the graph (a real file→file dependency)."""
    return {(s, d) for (s, d, k) in eset if k == "imports" and d in fp}


def file_hash(admin, repo, path):
    """The stored content_hash for a coordinate's FILE node (the freshness key the symbol-demotion gate reads).
    A patched file node MUST keep this — else freshness_ok (80_contention) can never fire for a normally-pushed
    file and the symbol-level 'disjoint' demotion is unreachable in steady state. NULL == the bug."""
    return admin(
        """
        SELECT set_config('core.current_account','ACCT-DEMO',true);
        SELECT content_hash FROM core.code_node
          WHERE repo=%s AND branch='main' AND node_kind='file' AND path=%s
        """, (repo, path))


def _ingest(db, graph, repo, sha):
    db("SELECT core.ingest_graph_with_authority(%s,%s,%s,%s)", (json.dumps(graph), repo, "main", sha))


def _patch(
    db, sub, repo, changed, removed, sha,
    expected_base_sha=SHA1, expected_base_revision=None,
):
    """Apply core.patch_graph_with_authority exactly as the server's _incremental_ingest does."""
    if expected_base_revision is None:
        raw_version = db(
            "SELECT core.coordinate_graph_sha(%s,%s)", (repo, "main")
        )
        version = (
            raw_version
            if isinstance(raw_version, dict)
            else json.loads(raw_version)
        )
        expected_base_revision = version["graph_revision"]
    return db("SELECT core.patch_graph_with_authority(%s,%s,%s,%s,%s,%s)",
              (
                  json.dumps(
                      {
                          "extractor_version": sub["extractor_version"],
                          "metrics": {
                              "schema_contract_version":
                                  C.SCHEMA_CONTRACT_VERSION,
                          },
                          "expected_base_sha": expected_base_sha,
                          "expected_base_revision": expected_base_revision,
                          "nodes": sub["nodes"],
                          "edges": sub["edges"],
                      }
                  ),
                  repo,
                  "main",
                  changed,
                  removed,
                  sha,
              ))


def safety_unit_cases() -> list:
    """Hermetic regressions for incremental guards and write acknowledgements."""
    checks = []
    dirs = []

    checks.append((
        "incremental resource classification matches the authoritative graph schema contract",
        I._RESOURCE_NODE_KINDS == C.RESOURCE_NODE_KINDS
        and I._RESOURCE_DEFINITION_EDGE_KINDS
        == C.RESOURCE_DEFINITION_EVIDENCE_KINDS,
    ))

    identity_payload = {"repository": {"id": 424242, "full_name": "fixture/identity"}}

    def _identity_db(result):
        def call(sql, args=()):
            if "reconcile_repo_identity_with_authority" not in sql:
                raise AssertionError(f"unexpected identity SQL: {sql}")
            assert args == ("fixture/identity", "424242")
            return result
        return call

    retry_identity = {
        "ok": True,
        "reconciled": False,
        "reason": "repository coordinate lock busy; retry",
    }
    checks.append((
        "identity reconcile does not mistake an ok:true retry/no-op for a durable binding",
        I._reconcile_repo_identity(
            _identity_db(retry_identity), "fixture/identity", identity_payload
        ) is None,
    ))
    mismatched_identity = {
        "ok": True,
        "reconciled": False,
        "repo": "fixture/other",
        "repo_id": "424242",
        "activation_recorded": True,
    }
    checks.append((
        "identity reconcile rejects a readback for a different coordinate",
        I._reconcile_repo_identity(
            _identity_db(mismatched_identity), "fixture/identity", identity_payload
        ) is None,
    ))
    bound_identity = {
        "ok": True,
        "reconciled": False,
        "repo": "fixture/identity",
        "repo_id": "424242",
        "activation_recorded": True,
    }
    checks.append((
        "identity reconcile accepts only the exact activated coordinate readback",
        I._reconcile_repo_identity(
            _identity_db(bound_identity), "fixture/identity", identity_payload
        ) == bound_identity,
    ))
    try:
        I._bind_onboarded_repo_identity(
            _identity_db(retry_identity), "fixture/identity", "424242"
        )
        retry_binding_failed_loud = False
    except I.RepositoryIdentityBindingError:
        retry_binding_failed_loud = True
    checks.append((
        "account onboarding fails loudly when identity reconciliation requests a retry",
        retry_binding_failed_loud,
    ))

    # A synthetic universe node is deliberately resolution-only, but a resolved
    # import to it is still known.  It must not inflate unresolved observability in
    # either build_graph or the path-local slice persisted by the patch writer.
    resolution_dir = write_tree({"consumer.py": "import alpha\n"})
    dirs.append(resolution_dir)
    resolved = X.build_graph(
        resolution_dir, universe_paths=["consumer.py", "alpha.py"])
    checks.append((
        "resolution-only universe target resolves to a concrete import edge",
        ("consumer.py", "alpha.py", "imports") in {
            (edge.get("src"), edge.get("dst"), edge.get("kind"))
            for edge in resolved.get("edges", [])
        },
    ))
    checks.append((
        "build_graph does not count a resolved universe-only target as unresolved",
        resolved.get("metrics", {}).get("unresolved_reference_count") == 0,
    ))
    sliced = I._slice_changed_graph(
        resolved,
        ["consumer.py"],
        known_file_paths=["consumer.py", "alpha.py"],
    )
    checks.append((
        "incremental slice retains the known universe for unresolved observability",
        sliced.get("metrics", {}).get("unresolved_reference_count") == 0,
    ))

    # Path-derived sibling/role eligibility is only a safety probe for ADDED
    # paths.  Existing actual members are supplied by the persisted pairing_paths
    # catalog; an ordinary edit at a merely-eligible path must stay patch-safe.
    candidate_path = "src/services/payment.py"
    candidate_blob = b"PAYMENT = 1\n"
    checks.append((
        "fixture path is genuinely sibling/role-eligible",
        I._pairing_candidate_path(candidate_path) is True,
    ))
    checks.append((
        "existing merely-eligible path does not trigger the added-path fallback",
        I._content_requires_full(
            candidate_path, candidate_blob, is_added=False) is None,
    ))
    checks.append((
        "newly-added eligible path still triggers the symmetric-pair fallback",
        I._content_requires_full(
            candidate_path, candidate_blob, is_added=True)
        == "sibling-stem or role-feature candidate added",
    ))

    graph_hash = "a" * 64

    class _PairingDB:
        def __init__(self, pairing_paths=()):
            self.pairing_paths = list(pairing_paths)

        def __call__(self, sql, args=()):
            if "coordinate_resource_catalog" in sql:
                return {
                    "resources": [],
                    "context_paths": [],
                    "definition_paths": [],
                    "reference_conditioned_paths": [],
                    "pairing_paths": self.pairing_paths,
                    "bidirectional_import_paths": [],
                }
            if "patch_graph_with_authority" in sql:
                return {
                    "ok": True,
                    "mode": "patch",
                    "commit_sha": SHA2,
                    "graph_hash": graph_hash,
                    "edges_total": 0,
                    "semantic_ref_version": C.SEMANTIC_REF_VERSION,
                }
            if "coordinate_graph_sha" in sql:
                return {
                    "commit_sha": SHA2,
                    "graph_revision": 1,
                    "graph_hash": graph_hash,
                    "extractor_version": X.EXTRACTOR_VERSION,
                    "current_extractor_version": X.EXTRACTOR_VERSION,
                    "semantic_ref_version": C.SEMANTIC_REF_VERSION,
                    "current_semantic_ref_version": C.SEMANTIC_REF_VERSION,
                }
            if "coordinate_file_paths" in sql:
                return [candidate_path]
            raise AssertionError(f"unexpected SQL in pairing fixture: {sql}")

    class _CandidateGH:
        def get_file_at(self, repo, path, ref):
            del repo, ref
            return candidate_blob if path == candidate_path else None

        def target_file_modes(self, repo, ref, paths):
            del repo, ref
            return {
                "complete": True,
                "truncated": False,
                "malformed": False,
                "over_cap": False,
                "entries": {
                    path: {"mode": "100644", "type": "blob"}
                    for path in paths
                },
            }

    patch_result = I._incremental_ingest(
        _PairingDB(), _CandidateGH(), "fixture/pairing", "main", SHA2,
        [candidate_path], [], [candidate_path],
    )
    checks.append((
        "existing eligible path absent from pairing catalog stays on patch path",
        patch_result.get("mode") == "patch",
    ))

    class _CasMismatch(Exception):
        pgcode = "40001"

    class _CasMismatchDB(_PairingDB):
        def __call__(self, sql, args=()):
            if "patch_graph_with_authority" in sql:
                raise _CasMismatch("baseline changed")
            return super().__call__(sql, args)

    try:
        I._incremental_ingest(
            _CasMismatchDB(), _CandidateGH(), "fixture/pairing", "main",
            SHA2, [candidate_path], [], [candidate_path],
        )
        cas_reason_is_specific = False
    except I._IncrementalUnsafe as exc:
        cas_reason_is_specific = exc.reason_code == "graph_baseline_changed"
    checks.append((
        "SQLSTATE 40001 SHA+revision CAS mismatch records graph_baseline_changed",
        cas_reason_is_specific,
    ))

    class _LegacyRevisionDB(_PairingDB):
        def __call__(self, sql, args=()):
            value = super().__call__(sql, args)
            if "coordinate_graph_sha" in sql:
                value = dict(value)
                value["graph_revision"] = 0
            return value

    try:
        I._incremental_ingest(
            _LegacyRevisionDB(), _CandidateGH(), "fixture/pairing", "main",
            SHA2, [candidate_path], [], [candidate_path],
        )
        legacy_revision_bailed = False
    except I._IncrementalUnsafe as exc:
        legacy_revision_bailed = "monotonic revision" in str(exc)
    checks.append((
        "migration sentinel graph_revision=0 is never patchable and forces full",
        legacy_revision_bailed,
    ))

    try:
        I._incremental_ingest(
            _PairingDB([candidate_path]), _CandidateGH(),
            "fixture/pairing", "main", SHA2,
            [candidate_path], [], [candidate_path],
        )
        persisted_pair_bailed = False
    except I._IncrementalUnsafe as exc:
        persisted_pair_bailed = "symmetric resource pairing member" in str(exc)
    checks.append((
        "persisted pairing_paths still forces full rebuild for an existing member",
        persisted_pair_bailed,
    ))

    class _ReadbackDB:
        def __init__(self, commit_sha=SHA2, readback_hash=graph_hash):
            self.commit_sha = commit_sha
            self.readback_hash = readback_hash

        def __call__(self, sql, args=()):
            if "coordinate_graph_sha" not in sql:
                raise AssertionError(f"unexpected validation SQL: {sql}")
            return {
                "commit_sha": self.commit_sha,
                "graph_hash": self.readback_hash,
                "semantic_ref_version": C.SEMANTIC_REF_VERSION,
            }

    valid_full = I._validated_graph_write(
        _ReadbackDB(),
        {
            "ok": True,
            "graph_hash": graph_hash,
            "semantic_ref_version": C.SEMANTIC_REF_VERSION,
        },
        "fixture/write", "main", SHA2, "full",
    )
    valid_patch = I._validated_graph_write(
        _ReadbackDB(),
        {
            "ok": True,
            "commit_sha": SHA2,
            "graph_hash": graph_hash,
            "semantic_ref_version": C.SEMANTIC_REF_VERSION,
        },
        "fixture/write", "main", SHA2, "patch",
    )
    checks.append((
        "full and patch acknowledgements require matching coordinate readback",
        valid_full.get("graph_hash") == graph_hash
        and valid_patch.get("commit_sha") == SHA2,
    ))

    def rejected(raw, readback=None, operation="full"):
        try:
            I._validated_graph_write(
                readback or _ReadbackDB(), raw,
                "fixture/write", "main", SHA2, operation,
            )
            return False
        except RuntimeError:
            return True

    checks.append((
        "writer result without ok=true fails closed",
        rejected({
            "ok": False,
            "graph_hash": graph_hash,
            "semantic_ref_version": C.SEMANTIC_REF_VERSION,
        }),
    ))
    checks.append((
        "writer result without a 64-hex graph_hash fails closed",
        rejected({
            "ok": True,
            "graph_hash": "not-a-hash",
            "semantic_ref_version": C.SEMANTIC_REF_VERSION,
        }),
    ))
    checks.append((
        "writer result without the current semantic generation fails closed",
        rejected({"ok": True, "graph_hash": graph_hash}),
    ))
    checks.append((
        "patch writer result without its expected commit fails closed",
        rejected({
            "ok": True,
            "graph_hash": graph_hash,
            "semantic_ref_version": C.SEMANTIC_REF_VERSION,
        }, operation="patch"),
    ))
    checks.append((
        "patch writer result for a different commit fails closed",
        rejected({
            "ok": True,
            "commit_sha": SHA1,
            "graph_hash": graph_hash,
            "semantic_ref_version": C.SEMANTIC_REF_VERSION,
        }, operation="patch"),
    ))
    checks.append((
        "coordinate readback for a different commit fails closed",
        rejected(
            {
                "ok": True,
                "graph_hash": graph_hash,
                "semantic_ref_version": C.SEMANTIC_REF_VERSION,
            },
            _ReadbackDB(commit_sha=SHA1),
        ),
    ))
    checks.append((
        "coordinate hash differing from writer result fails closed",
        rejected(
            {
                "ok": True,
                "graph_hash": graph_hash,
                "semantic_ref_version": C.SEMANTIC_REF_VERSION,
            },
            _ReadbackDB(readback_hash="b" * 64),
        ),
    ))

    for directory in dirs:
        shutil.rmtree(directory, ignore_errors=True)
    return checks


def structural_cases(db, admin) -> list:
    """The risky STRUCTURAL changes the changed/removed sets describe only INDIRECTLY: a rename, a directory
    MOVE, a deleted-but-still-imported file, an emptied file. For each: assert the end-state the PRODUCT must
    see is correct (== a full re-ingest), and — where the raw SQL patch alone would diverge — that the server
    guard (_relocation_breaks_resolution) catches it so the server never patches that push. Each sub-case uses
    its OWN coordinate so they're independent."""
    checks = []
    dirs = []

    def tree(files):
        d = write_tree(files); dirs.append(d); return d

    try:
        # ── CASE 1: a directory MOVE of an imported file (helper.py → lib/helper.py), importer UNCHANGED. ──
        # The latent stale-graph bug: a full re-ingest re-points the unchanged importer's `import helper` to the
        # NEW location (LIVE app→lib/helper.py); the raw SQL patch drops the old edge (dst was a removed path)
        # and never creates the new one → a MISSING live edge. The server must NOT patch this push.
        APP = "import helper\n\ndef a():\n    return helper.h()\n"           # importer — UNCHANGED across the move
        HELP = "def h():\n    return 1\n"
        _ingest(db, X.build_graph(tree({"app.py": APP, "helper.py": HELP})), "incr/move_base", SHA1)
        _ingest(db, X.build_graph(tree({"app.py": APP, "lib/helper.py": HELP})), "incr/move_truth", SHA2)
        mv_changed, mv_removed = ["lib/helper.py"], ["helper.py"]
        uni = [p for p in (db("SELECT core.coordinate_file_paths(%s,%s)", ("incr/move_base", "main")) or [])
               if p not in set(mv_removed)]
        sub = X.build_graph(tree({"lib/helper.py": HELP}), universe_paths=uni)
        _patch(db, sub, "incr/move_base", mv_changed, mv_removed, SHA2)
        nb, eb = graph_of(admin, "incr/move_base"); nt, et = graph_of(admin, "incr/move_truth")
        fp_b = {p for (i, k, p, nm, lg, *_) in nb if k == "file"}; fp_t = {p for (i, k, p, nm, lg, *_) in nt if k == "file"}
        # (a) document the DIVERGENCE the raw SQL patch leaves (the move is the case full re-ingest gets right):
        checks.append(("MOVE: full re-ingest has the LIVE edge app→lib/helper.py (the truth)",
                       ("app.py", "lib/helper.py") in live_imports(et, fp_t)))
        checks.append(("MOVE: raw SQL patch DIVERGES — it loses app→lib/helper.py (the latent stale-graph bug)",
                       live_imports(eb, fp_b) != live_imports(et, fp_t)
                       and ("app.py", "lib/helper.py") not in live_imports(eb, fp_b)))
        # (b) the FIX: the server guard flags exactly this push → _incremental_ingest bails → full re-ingest.
        checks.append(("MOVE: server guard catches the relocation (so the server takes the correct full path)",
                       S._relocation_breaks_resolution(mv_changed, mv_removed) is True))
        # (c) END-TO-END WIRING: the guard is actually INSIDE _incremental_ingest — it raises _IncrementalUnsafe
        # (which ingest_push converts to the always-correct _full_ingest). A correct detector that isn't wired
        # in would be inert, so we exercise the real function (a fake gh that serves the moved file's bytes).
        class _Gh:
            def get_file_at(self, repo, path, ref):
                return HELP.encode()
        try:
            S._incremental_ingest(lambda *a, **k: None, _Gh(), "incr/move_base", "main", SHA2, mv_changed, mv_removed, uni)
            mv_raised = False
        except S._IncrementalUnsafe:
            mv_raised = True
        except Exception:
            mv_raised = False
        checks.append(("MOVE: _incremental_ingest itself bails (raises _IncrementalUnsafe) so the server never patches a move",
                       mv_raised is True))

        # ── CASE 2: the existing add+remove scenario is NOT a relocation → the fast path is preserved. ──
        checks.append(("relocation detector does not misclassify an unrelated add+remove "
                       "(the separate deletion-equivalence guard still full-rebuilds)",
                       S._relocation_breaks_resolution(["beta.py", "gamma.py", "zeta.py"], ["delta.py"]) is False))
        checks.append(("guard does NOT fire for a pure modify (no path-set change → fast path kept)",
                       S._relocation_breaks_resolution(["beta.py"], []) is False))
        checks.append(("guard fires for a same-dir-but-different-path move (pkg/util.py → util.py)",
                       S._relocation_breaks_resolution(["util.py"], ["pkg/util.py"]) is True))
        checks.append(("guard does NOT fire for a pure rename to a NEW basename (a/x.py → a/y.py)",
                       S._relocation_breaks_resolution(["a/y.py"], ["a/x.py"]) is False))

        # ── CASE 3: a changed file that became EMPTY (its symbols + outgoing edges must be removed). ──
        # gamma had a def + an import; it is emptied. gamma is a CHANGED file, so the SQL patch handles it.
        ALPHA = "def fa():\n    return 1\n"
        _ingest(db, X.build_graph(tree({"alpha.py": ALPHA, "gamma.py": "import alpha\n\ndef fg():\n    return alpha.fa()\n"})),
                "incr/empty_base", SHA1)
        _ingest(db, X.build_graph(tree({"alpha.py": ALPHA, "gamma.py": "\n"})), "incr/empty_truth", SHA2)
        uni = [p for p in (db("SELECT core.coordinate_file_paths(%s,%s)", ("incr/empty_base", "main")) or [])]
        sub = X.build_graph(tree({"gamma.py": "\n"}), universe_paths=uni)
        _patch(db, sub, "incr/empty_base", ["gamma.py"], [], SHA2)
        nb, eb = graph_of(admin, "incr/empty_base"); nt, et = graph_of(admin, "incr/empty_truth")
        fp_b = {p for (i, k, p, nm, lg, *_) in nb if k == "file"}; fp_t = {p for (i, k, p, nm, lg, *_) in nt if k == "file"}
        checks.append((f"EMPTIED file: PATCH nodes == FULL re-ingest nodes (base {len(nb)} vs truth {len(nt)})", nb == nt))
        checks.append(("EMPTIED file: PATCH live edges == FULL re-ingest live edges",
                       live_edges_with_paths(eb, fp_b) == live_edges_with_paths(et, fp_t)))
        checks.append(("EMPTIED file: gamma's def + import are gone (no gamma::fg, no gamma→alpha)",
                       not any(p == "gamma.py" and k == "def" for (i, k, p, nm, lg, *_) in nb)
                       and ("gamma.py", "alpha.py", "imports") not in eb))

        # ── CASE 4: a RENAME where the importer IS updated in the SAME push (the common, correct rename). ──
        # helper.py → helper2.py, AND app.py updates its import — app is a CHANGED file, so it re-resolves.
        # Different basename → the guard does NOT fire; the SQL patch must match a full re-ingest on its own.
        APP_OLD = "import helper\n\ndef a():\n    return helper.h()\n"
        APP_NEW = "import helper2\n\ndef a():\n    return helper2.h()\n"
        _ingest(db, X.build_graph(tree({"app.py": APP_OLD, "helper.py": HELP})), "incr/ren_base", SHA1)
        _ingest(db, X.build_graph(tree({"app.py": APP_NEW, "helper2.py": HELP})), "incr/ren_truth", SHA2)
        rn_changed, rn_removed = ["app.py", "helper2.py"], ["helper.py"]
        checks.append(("RENAME(importer updated): guard does NOT fire (different basename → fast path kept)",
                       S._relocation_breaks_resolution(rn_changed, rn_removed) is False))
        uni = [p for p in (db("SELECT core.coordinate_file_paths(%s,%s)", ("incr/ren_base", "main")) or [])
               if p not in set(rn_removed)]
        sub = X.build_graph(tree({"app.py": APP_NEW, "helper2.py": HELP}), universe_paths=uni)
        _patch(db, sub, "incr/ren_base", rn_changed, rn_removed, SHA2)
        nb, eb = graph_of(admin, "incr/ren_base"); nt, et = graph_of(admin, "incr/ren_truth")
        fp_b = {p for (i, k, p, nm, lg, *_) in nb if k == "file"}; fp_t = {p for (i, k, p, nm, lg, *_) in nt if k == "file"}
        checks.append((f"RENAME(importer updated): PATCH nodes == FULL re-ingest nodes (base {len(nb)} vs truth {len(nt)})", nb == nt))
        checks.append(("RENAME(importer updated): PATCH live edges == FULL re-ingest live edges (app→helper2 re-pointed)",
                       live_edges_with_paths(eb, fp_b) == live_edges_with_paths(et, fp_t)))
        checks.append(("RENAME(importer updated): the re-pointed LIVE edge app→helper2.py EXISTS; old helper.py is gone",
                       ("app.py", "helper2.py", "imports") in eb
                       and not any(p == "helper.py" for (i, k, p, nm, lg, *_) in nb)))

        # ── CASE 5: ADD of a file an UNCHANGED file already had a (dead) import for (the residual #75 left). ──
        # base: app.py `import helper`, helper.py ABSENT → app→helper is INERT (a module name naming no file).
        # push: ADD helper.py only (changed=[helper.py], removed=[], app.py UNCHANGED). A full re-ingest re-
        # resolves the unchanged app.py → a LIVE edge app→helper.py; the raw SQL patch (changed-files-only)
        # never re-resolves app.py → the live edge is MISSING. The relocation guard can't catch it (no removed
        # path), so the fix is a SEPARATE detector (_added_import_goes_live) that re-runs the REAL resolver on
        # the coordinate's retained inert imports against the new universe and bails when one goes live.
        APP_I = "import helper\n\ndef a():\n    return helper.h()\n"      # importer — UNCHANGED across the add
        _ingest(db, X.build_graph(tree({"app.py": APP_I})), "incr/addinert_base", SHA1)
        _ingest(db, X.build_graph(tree({"app.py": APP_I, "helper.py": HELP})), "incr/addinert_truth", SHA2)
        ai_changed, ai_removed = ["helper.py"], []
        uni = [p for p in (db("SELECT core.coordinate_file_paths(%s,%s)", ("incr/addinert_base", "main")) or [])]
        sub = X.build_graph(tree({"helper.py": HELP}), universe_paths=uni)
        _patch(db, sub, "incr/addinert_base", ai_changed, ai_removed, SHA2)
        nb, eb = graph_of(admin, "incr/addinert_base"); nt, et = graph_of(admin, "incr/addinert_truth")
        fp_b = {p for (i, k, p, nm, lg, *_) in nb if k == "file"}; fp_t = {p for (i, k, p, nm, lg, *_) in nt if k == "file"}
        # (a) DOCUMENT the divergence the raw SQL patch leaves (the full re-ingest is the truth):
        checks.append(("ADD-inert: full re-ingest has the LIVE edge app→helper.py (the truth)",
                       ("app.py", "helper.py") in live_imports(et, fp_t)))
        checks.append(("ADD-inert: raw SQL patch DIVERGES — it loses app→helper.py (the residual #75 left)",
                       live_imports(eb, fp_b) != live_imports(et, fp_t)
                       and ("app.py", "helper.py") not in live_imports(eb, fp_b)))
        # (b) the relocation guard CANNOT see this (a pure ADD has no removed path) — so it needs its own detector:
        checks.append(("ADD-inert: the relocation guard does NOT fire (a pure ADD has no removed path)",
                       S._relocation_breaks_resolution(ai_changed, ai_removed) is False))
        # (c) the FIX: the coordinate's retained inert imports are readable, and the detector fires for THIS push.
        raw_inert = db("SELECT core.coordinate_inert_imports(%s,%s)", ("incr/addinert_base", "main"))
        inert = raw_inert if isinstance(raw_inert, list) else json.loads(raw_inert or "[]")
        checks.append(("ADD-inert: coordinate_inert_imports surfaces the dead app→helper edge",
                       [tuple(x) for x in inert] == [("app.py", "helper")]))
        checks.append(("ADD-inert: detector fires (adding helper.py turns app→helper LIVE → bail)",
                       S._added_import_goes_live([tuple(x) for x in inert], ai_changed,
                                                 [p for p in uni if p not in set(ai_removed)]) is True))
        # PRECISION: the proven-safe add (zeta added, nothing imports zeta) must NOT fire (fast path kept).
        checks.append(("ADD-inert: detector does NOT fire for a safe add (zeta added, no one imports zeta)",
                       S._added_import_goes_live([("app.py", "helper")], ["zeta.py"], ["app.py"]) is False))
        # (d) END-TO-END WIRING: the detector is INSIDE _incremental_ingest — it raises _IncrementalUnsafe (which
        # ingest_push converts to the always-correct _full_ingest). Exercise the real function (a fake gh serving
        # the added file's bytes), exactly like the MOVE case above.
        class _GhAdd:
            def get_file_at(self, repo, path, ref):
                return HELP.encode() if path == "helper.py" else None
        try:
            S._incremental_ingest(db, _GhAdd(), "incr/addinert_base", "main", SHA2, ai_changed, ai_removed, uni)
            ai_raised = False
        except S._IncrementalUnsafe:
            ai_raised = True
        except Exception:
            ai_raised = False
        checks.append(("ADD-inert: _incremental_ingest itself bails (raises _IncrementalUnsafe) so the server takes the full path",
                       ai_raised is True))
        # (e) AND the result equals a full re-ingest: drive ingest_push (the incremental DECISION) on a FRESH
        # baseline (NOT the coordinate already patched above — there helper.py is now present, so the same push
        # would no longer be an ADD). _incremental_ingest bails → ingest_push falls through to _full_ingest;
        # assert the live edge now matches a full re-ingest of the same end-state.
        _ingest(db, X.build_graph(tree({"app.py": APP_I})), "incr/addinert_e2e", SHA1)
        class _GhFull:                                   # _full_ingest path: download_tarball of the end-state tree
            def __init__(self, tree_dir): self._d = tree_dir
            def download_tarball(self, repo, sha):
                import io as _io, tarfile as _tf
                buf = _io.BytesIO()
                with _tf.open(fileobj=buf, mode="w") as t:
                    t.add(self._d, arcname="repo")
                return buf.getvalue()
            def get_file_at(self, repo, path, ref):
                return HELP.encode() if path == "helper.py" else None
        end_dir = tree({"app.py": APP_I, "helper.py": HELP})
        S.ingest_push(db, _GhFull(end_dir), "incr/addinert_e2e", "main", SHA2,
                      payload={
                          "before": SHA1,
                          "after": SHA2,
                          "commits": [{
                              "added": ["helper.py"],
                              "modified": [],
                              "removed": [],
                          }],
                      },
                      coalesce=None)
        nb2, eb2 = graph_of(admin, "incr/addinert_e2e")
        fp_b2 = {p for (i, k, p, nm, lg, *_) in nb2 if k == "file"}
        checks.append(("ADD-inert: after ingest_push (bail→full) the LIVE edge app→helper.py EXISTS == full re-ingest",
                       ("app.py", "helper.py") in live_imports(eb2, fp_b2)
                       and live_imports(eb2, fp_b2) == live_imports(et, fp_t)))
    finally:
        for d in dirs:
            shutil.rmtree(d, ignore_errors=True)
    return checks


def main() -> int:
    r = subprocess.run(["bash", "db/bootstrap_local.sh", DB], cwd=ROOT, capture_output=True, text=True)
    if r.returncode != 0:
        print("bootstrap failed:\n", r.stderr[-800:]); return 1
    db = make_db("veripsa_app")        # writer-inherited: ingest / patch / read universe
    admin = make_db("veripsa_migrator")
    checks = safety_unit_cases()

    base_dir = write_tree(BASE)
    new_files = dict(BASE)
    new_files["beta.py"] = BETA_NEW; new_files["gamma.py"] = GAMMA_NEW; new_files["zeta.py"] = ZETA_NEW
    del new_files["delta.py"]
    new_dir = write_tree(new_files)
    changed_dir = write_tree({"beta.py": BETA_NEW, "gamma.py": GAMMA_NEW, "zeta.py": ZETA_NEW})
    try:
        # (1) full-ingest the base tree as the coordinate we will PATCH
        gbase = X.build_graph(base_dir)
        db("SELECT core.ingest_graph_with_authority(%s,%s,%s,%s)", (json.dumps(gbase), "incr/base", "main", SHA1))
        # (2) full-ingest the NEW tree as the TRUTH (a full re-ingest of the same end-state)
        gtruth = X.build_graph(new_dir)
        db("SELECT core.ingest_graph_with_authority(%s,%s,%s,%s)", (json.dumps(gtruth), "incr/truth", "main", SHA2))

        # (3) Exercise the low-level touched-file patch primitive against the retained path universe. Production
        # reconstructs all bounded target-SHA context first and rejects this deletion before reaching the writer.
        universe = db("SELECT core.coordinate_file_paths(%s,%s)", ("incr/base", "main"))   # text[] → list
        universe = [p for p in (universe or []) if p not in REMOVED]
        checks.append((f"coordinate_file_paths returns the retained file universe (got {sorted(universe)})",
                       set(universe) == {"alpha.py", "beta.py", "gamma.py", "epsilon.py"}))
        sub = X.build_graph(changed_dir, universe_paths=universe)
        baseline_version = db(
            "SELECT core.coordinate_graph_sha(%s,%s)",
            ("incr/base", "main"),
        )
        baseline_version = (
            baseline_version
            if isinstance(baseline_version, dict)
            else json.loads(baseline_version)
        )
        sub["expected_base_sha"] = SHA1
        sub["expected_base_revision"] = baseline_version["graph_revision"]
        patch = db("SELECT core.patch_graph_with_authority(%s,%s,%s,%s,%s,%s)",
                   (json.dumps(sub), "incr/base", "main", CHANGED, REMOVED, SHA2))
        patch = patch if isinstance(patch, dict) else json.loads(patch)
        checks.append((f"patch ran in incremental mode and inserted the changed subgraph "
                       f"(mode={patch.get('mode')}, nins={patch.get('nodes_inserted')}, ndel={patch.get('nodes_deleted')})",
                       patch.get("mode") == "patch" and patch.get("nodes_inserted", 0) > 0 and patch.get("nodes_deleted", 0) > 0))

        # (4) the patched graph must EQUAL a full re-ingest of the same end-state
        nb, eb = graph_of(admin, "incr/base")
        nt, et = graph_of(admin, "incr/truth")
        fp_b = {p for (nid, k, p, nm, lg, *_) in nb if k == "file"}
        fp_t = {p for (nid, k, p, nm, lg, *_) in nt if k == "file"}
        checks.append((f"PATCH nodes == FULL re-ingest nodes (base {len(nb)} vs truth {len(nt)})", nb == nt))
        checks.append(("PATCH live edges == FULL re-ingest live edges (inert unresolved imports aside)",
                       live_edges_with_paths(eb, fp_b) == live_edges_with_paths(et, fp_t)))
        # RAW semantic equality is stricter than the historical "live edges"
        # comparison above: the full build retains epsilon's now-unresolved
        # `import delta` token, while a path-local delete cannot reconstruct it
        # from unchanged source bytes.  The production incremental entry point
        # must therefore reject every deletion and let the caller full-rebuild.
        checks.append((
            "deletion fixture records the raw Edge-set divergence that requires full fallback",
            eb != et and ("epsilon.py", "delta", "imports") in et,
        ))
        class _ChangedOnlyGH:
            def get_file_at(self, repo, path, ref):
                del repo, ref
                body = {
                    "beta.py": BETA_NEW,
                    "gamma.py": GAMMA_NEW,
                    "zeta.py": ZETA_NEW,
                }.get(path)
                return body.encode() if body is not None else None
        try:
            S._incremental_ingest(
                db, _ChangedOnlyGH(), "incr/base", "main", SHA2,
                CHANGED, REMOVED, universe,
            )
            deletion_bailed = False
        except S._IncrementalUnsafe as exc:
            deletion_bailed = "deletion" in str(exc)
        checks.append((
            "production incremental path rejects deletion with an explicit full-rebuild reason",
            deletion_bailed,
        ))

        # FRESHNESS KEY survives the incremental patch (regression guard for the patch-vs-full INSERT divergence).
        # The patched file node MUST carry its content_hash exactly like a full ingest — otherwise freshness_ok
        # (80_contention) can never be true for any file touched by a normal push, so the marquee symbol-level
        # 'disjoint' demotion is unreachable in steady state. Asserted on a re-patched file (beta.py ∈ CHANGED):
        # NULL here == the bug (the patch INSERT dropped content_hash); the value must MATCH a full re-ingest.
        beta_patched = file_hash(admin, "incr/base", "beta.py")
        beta_truth = file_hash(admin, "incr/truth", "beta.py")
        checks.append((f"FRESHNESS: re-patched file beta.py keeps a NON-NULL content_hash (got {beta_patched!r})",
                       beta_patched is not None and beta_patched != ""))
        checks.append(("FRESHNESS: the patched content_hash MATCHES a full re-ingest (freshness_ok can fire post-push)",
                       beta_patched is not None and beta_patched == beta_truth))

        # (5) the specific re-resolutions the cost win must not break
        checks.append(("changed file re-points: beta→gamma import EXISTS after patch",
                       ("beta.py", "gamma.py", "imports") in eb))
        checks.append(("changed file's OLD edge is gone: beta→alpha import REMOVED",
                       ("beta.py", "alpha.py", "imports") not in eb))
        checks.append(("added file resolves into an unchanged file: zeta→alpha import EXISTS",
                       ("zeta.py", "alpha.py", "imports") in eb))
        checks.append(("existing file's INCOMING edge to the new file resolves: gamma→zeta import EXISTS",
                       ("gamma.py", "zeta.py", "imports") in eb))
        checks.append(("removed file is forgotten: no delta.py node remains",
                       not any(p == "delta.py" for (nid, k, p, nm, lg, *_) in nb)))
        checks.append(("dangling incoming edge cleaned: epsilon→delta.py(resolved) REMOVED",
                       ("epsilon.py", "delta.py", "imports") not in eb))
        # version bumped to the push sha + counts recomputed
        ver = admin(
            """
            SELECT set_config('core.current_account','ACCT-DEMO',true);
            SELECT json_build_array(commit_sha, node_count, edge_count)::text
              FROM core.graph_version WHERE repo='incr/base' AND branch='main'
            """)
        ver = json.loads(ver)
        checks.append((f"version bumped to the push sha + counts recomputed (got {ver})",
                       ver[0] == SHA2 and ver[1] == len(nb)))
    finally:
        for d in (base_dir, new_dir, changed_dir):
            shutil.rmtree(d, ignore_errors=True)

    # STRUCTURAL cases the changed/removed sets describe only indirectly: rename / directory MOVE / emptied
    # file (+ the proof the server guard preserves the fast path for the safe ones).
    checks.extend(structural_cases(db, admin))

    ok = True
    for name, cond in checks:
        print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
        ok = ok and bool(cond)
    print("INCREMENTAL GATE:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    finally:
        subprocess.run(["dropdb", DB], capture_output=True)
