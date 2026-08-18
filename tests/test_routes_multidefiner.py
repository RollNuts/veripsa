"""Gate: cross-tier ROUTE multi-definer suppression (precision) + recall preservation.

The cross-tier route detector (_cg_routes) couples a backend file that DEFINES a route to a frontend
file that ISSUES a request to it. Before this fix, match_pairs coupled EVERY (backend, frontend) pair
sharing a route with NO multi-definer guard: when the same route path was declared by >1 backend file
(a monorepo's multiple services, or a real app + a second copy), a single frontend call false-coupled
to ALL of them. That is the exact bug class its sibling detectors were fixed for (openapi #329, iac
#330, api-contract #332, config #339 — each measured 66-91% of their false couples were this class).

This gate measures the fix on a crafted repo (offline, no Postgres):
  (1) PRECISION  — two REAL backend files declaring the SAME route + one frontend caller: the caller
                   couples to NEITHER, while candidate imports edges survive with
                   reference_status=ambiguous.
  (2) RECALL     — a SINGLE-definer route still couples its one backend to its one frontend caller.
  (3) RECALL-SAFE TEST SHADOW — a real backend + a TEST app declaring the same route: the test app is
                   excluded as a DEFINER, so the route stays single-definer and the REAL coupling is
                   preserved (the frontend couples to the real backend ONLY, never the test file).
  (4) RECALL-SAFE TEST REQUESTER — a frontend TEST file that ISSUES a request to a real route stays
                   coupled to the backend (test files are excluded only as DEFINERS, kept as REQUESTERS;
                   editing the route breaks the test = a real coupling).

Content-free throughout: only path strings + file paths are read.
Prints ROUTES MULTI-DEFINER GATE: PASS on success, ... FAIL on any failure.
"""
import sys
import os
import tempfile
import shutil

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import _cg_routes as R
import code_graph_extract as X


def _write(root, path, body):
    fp = os.path.join(root, path)
    os.makedirs(os.path.dirname(fp), exist_ok=True)
    with open(fp, "w") as fh:
        fh.write(body)


def _coupled_to(scan, f):
    """The set of files coupled to `f` (repo-relative, forward-slash)."""
    f = f.replace(os.sep, "/")
    out = set()
    for pair in scan["pairs"]:
        ps = {x.replace(os.sep, "/") for x in pair}
        if f in ps:
            out |= (ps - {f})
    return out


def _flask(route):
    return f"from flask import Flask\napp = Flask(__name__)\n@app.get('{route}')\ndef h(): pass\n"


def main():
    failures = []
    root = tempfile.mkdtemp(prefix="routes_multidefiner_")
    try:
        # (1) PRECISION: two REAL services declare /api/orders/{id}; one frontend caller.
        _write(root, "svc_a/api.py", _flask("/api/orders/{id}"))
        _write(root, "svc_b/api.py", _flask("/api/orders/{id}"))
        _write(root, "web/orders.ts", "export const f = () => fetch('/api/orders/5')\n")
        # (2) RECALL control: single-definer /api/invoices/{id}.
        _write(root, "svc_c/api.py", _flask("/api/invoices/{id}"))
        _write(root, "web/invoices.ts", "export const g = () => fetch('/api/invoices/9')\n")
        # (3) RECALL-SAFE TEST SHADOW: real app + a test app declare /api/carts/{id}.
        _write(root, "shop/api.py", _flask("/api/carts/{id}"))
        _write(root, "tests/conftest.py", _flask("/api/carts/{id}"))
        _write(root, "web/carts.ts", "export const h = () => fetch('/api/carts/7')\n")
        # (4) RECALL-SAFE TEST REQUESTER: a frontend test issues a request to a real route.
        _write(root, "shop2/api.py", _flask("/api/wishlist/{id}"))
        _write(root, "web/__tests__/wishlist.test.ts", "export const t = () => fetch('/api/wishlist/3')\n")
        # (6) MIS-READ SELF-DEF: an Express backend defines a route; a frontend `axios.get` parses as BOTH
        # a route-def and a request of the SAME route. The mis-read must NOT count as a 2nd definer (that
        # would suppress the genuine single-backend contract). The real coupling must hold.
        _write(root, "server/teams.ts",
               'import {Router} from "express"\nconst r = Router()\nr.get("/api/teams/:id/members", (q,s)=>{})\n')
        _write(root, "web/teams.ts",
               'import axios from "axios"\nexport const m = (id) => axios.get(`/api/teams/${id}/members`)\n')
        # (7) EXACT-OVER-AMBIGUOUS DEDUP: svc_d and svc_e both define one
        # ambiguous route, while svc_d alone also defines a different exact
        # route to the same requester file.
        _write(
            root,
            "svc_d/api.py",
            _flask("/api/mixed/{id}")
            + "\n@app.get('/api/mixed/special/detail')\ndef special(): pass\n",
        )
        _write(root, "svc_e/api.py", _flask("/api/mixed/{id}"))
        _write(
            root,
            "web/mixed.ts",
            "fetch('/api/mixed/1')\nfetch('/api/mixed/special/detail')\n",
        )

        scan = R.scan_repo(root, specificity_floor=True)

        # (1) the multi-real-definer caller must couple to NEITHER backend.
        c1 = _coupled_to(scan, "web/orders.ts")
        if c1:
            print(f"FAIL [1 precision]: multi-definer route should suppress; web/orders.ts coupled to {sorted(c1)!r}")
            failures.append("multi-definer-not-suppressed")

        graph = X.build_graph(root)
        graph_edges = graph["edges"]
        ambiguous_route_edges = [
            e for e in graph_edges
            if e.get("reference_status") == "ambiguous"
            and (
                "web/orders.ts" in (e.get("src"), e.get("dst"))
                or "web/orders.ts" in str(e.get("src"))
                or "web/orders.ts" in str(e.get("dst"))
            )
        ]
        expected_ambiguous_directions = {
            ("svc_a/api.py", "web/orders.ts"),
            ("web/orders.ts", "svc_a/api.py"),
            ("svc_b/api.py", "web/orders.ts"),
            ("web/orders.ts", "svc_b/api.py"),
        }
        got_ambiguous_directions = {
            (str(e.get("src")), str(e.get("dst")))
            for e in ambiguous_route_edges
        }
        if (
            got_ambiguous_directions != expected_ambiguous_directions
            or any(e.get("kind") != "imports" for e in ambiguous_route_edges)
        ):
            print(
                "FAIL [1 evidence]: ambiguous route candidates must survive as "
                f"inert imports edges; got {ambiguous_route_edges!r}"
            )
            failures.append("multi-definer-evidence-dropped")
        ambiguous_statuses = {
            n.get("path"): n.get("analysis_status")
            for n in graph["nodes"]
            if n.get("kind") == "file"
            and n.get("path") in {
                "svc_a/api.py", "svc_b/api.py", "web/orders.ts"
            }
        }
        if (
            set(ambiguous_statuses)
            != {"svc_a/api.py", "svc_b/api.py", "web/orders.ts"}
            or set(ambiguous_statuses.values()) != {"ambiguous"}
        ):
            print(
                "FAIL [1 unknown-source]: ambiguous route candidate files must "
                f"be locally Unknown; got {ambiguous_statuses!r}"
            )
            failures.append("multi-definer-source-status")

        # (2) the single-definer contract must still couple.
        c2 = _coupled_to(scan, "web/invoices.ts")
        if c2 != {"svc_c/api.py"}:
            print(f"FAIL [2 recall]: single-definer must couple to svc_c/api.py; got {sorted(c2)!r}")
            failures.append("single-definer-recall")
        resolved_invoice_edges = [
            e for e in graph_edges
            if {str(e.get("src")), str(e.get("dst"))}
            == {"svc_c/api.py", "web/invoices.ts"}
        ]
        if (
            len(resolved_invoice_edges) != 2
            or any("reference_status" in e for e in resolved_invoice_edges)
        ):
            print(
                "FAIL [2 resolved-status]: unambiguous route edges must remain "
                f"unchanged; got {resolved_invoice_edges!r}"
            )
            failures.append("single-definer-status-regression")
        mixed_d_edges = [
            e for e in graph_edges
            if {str(e.get("src")), str(e.get("dst"))}
            == {"svc_d/api.py", "web/mixed.ts"}
        ]
        mixed_e_edges = [
            e for e in graph_edges
            if {str(e.get("src")), str(e.get("dst"))}
            == {"svc_e/api.py", "web/mixed.ts"}
        ]
        if (
            len(mixed_d_edges) != 2
            or any("reference_status" in e for e in mixed_d_edges)
            or len(mixed_e_edges) != 2
            or any(
                e.get("reference_status") != "ambiguous"
                for e in mixed_e_edges
            )
        ):
            print(
                "FAIL [7 exact-priority]: an exact edge must win over an "
                "ambiguous duplicate coordinate; "
                f"svc_d={mixed_d_edges!r}, svc_e={mixed_e_edges!r}"
            )
            failures.append("exact-over-ambiguous-priority")

        # (3) the test app must NOT count as a definer → real coupling preserved, test file never coupled.
        c3 = _coupled_to(scan, "web/carts.ts")
        if c3 != {"shop/api.py"}:
            print(f"FAIL [3 test-shadow recall]: real coupling must survive a test-app shadow; "
                  f"expected {{'shop/api.py'}}, got {sorted(c3)!r}")
            failures.append("test-shadow-recall")

        # (4) a frontend test ISSUING a request stays coupled (kept as requester).
        c4 = _coupled_to(scan, "web/__tests__/wishlist.test.ts")
        if c4 != {"shop2/api.py"}:
            print(f"FAIL [4 test-requester recall]: a frontend test that fetches a real route must "
                  f"stay coupled; expected {{'shop2/api.py'}}, got {sorted(c4)!r}")
            failures.append("test-requester-recall")

        # (6) the Express↔axios single-backend contract must couple despite the axios self-def mis-read.
        c6 = _coupled_to(scan, "web/teams.ts")
        if c6 != {"server/teams.ts"}:
            print(f"FAIL [6 mis-read self-def]: a frontend axios.get mis-read as a route def must not "
                  f"suppress the genuine backend coupling; expected {{'server/teams.ts'}}, got {sorted(c6)!r}")
            failures.append("misread-selfdef-suppression")

        # Direct unit check of the suppression at the match_pairs level (independent of the walk).
        defs = {"a/x.py": {"/api/things/{}"}, "b/y.py": {"/api/things/{}"}, "c/z.py": {"/api/solo/{}"}}
        reqs = {"web/u.ts": {"/api/things/1", "/api/solo/2"}}
        pairs = R.match_pairs(defs, reqs, specificity_floor=True)
        coupled = {tuple(sorted(p)) for p in pairs}
        # /api/things is multi-definer (a,b) → no couple; /api/solo is single (c) → couple.
        if any("a/x.py" in p or "b/y.py" in p for p in coupled):
            print(f"FAIL [5 unit precision]: multi-definer /api/things must not couple; got {coupled!r}")
            failures.append("unit-multi-definer")
        if ("c/z.py", "web/u.ts") not in coupled:
            print(f"FAIL [5 unit recall]: single-definer /api/solo must couple; got {coupled!r}")
            failures.append("unit-single-definer")

        # Cross-repo shared-key producer follows the same evidence contract.
        old_xrepo = os.environ.get("VERIPSA_CROSS_REPO_KEYS")
        os.environ["VERIPSA_CROSS_REPO_KEYS"] = "1"
        try:
            xrepo_edges = R._routes_xrepo_edges({
                "xrepo_defs": {
                    "a/x.py": {"/api/things/{}"},
                    "b/y.py": {"/api/things/{}"},
                },
                "xrepo_reqs": {"web/u.ts": {"/api/things/{}"}},
            })
        finally:
            if old_xrepo is None:
                os.environ.pop("VERIPSA_CROSS_REPO_KEYS", None)
            else:
                os.environ["VERIPSA_CROSS_REPO_KEYS"] = old_xrepo
        things_edges = [
            e for e in xrepo_edges
            if e.get("dst") == "route::api/things"
        ]
        if (
            {e.get("src") for e in things_edges}
            != {"a/x.py", "b/y.py", "web/u.ts"}
            or any(e.get("reference_status") != "ambiguous" for e in things_edges)
        ):
            print(
                "FAIL [8 xrepo evidence]: ambiguous shared route key must "
                f"retain inert alters/queries edges; got {things_edges!r}"
            )
            failures.append("xrepo-multi-definer-evidence-dropped")
    finally:
        shutil.rmtree(root, ignore_errors=True)

    if failures:
        print(f"ROUTES MULTI-DEFINER GATE: FAIL (failures: {failures})")
        sys.exit(1)
    print("ROUTES MULTI-DEFINER GATE: PASS")
    sys.exit(0)


if __name__ == "__main__":
    main()
