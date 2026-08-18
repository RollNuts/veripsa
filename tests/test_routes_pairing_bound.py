"""Gate: cross-tier ROUTE pairing is BOUNDED (DoS fix) + the normal couples are UNCHANGED.

VERIFIED HIGH DoS (the bug this gate locks down): `_cg_routes.match_pairs` was the nested loop
`for bfile in defs: for ffile in reqs: for br in broutes: for fu in furls: match_routes(br,fu)` — that is
O(B×F×R×U) with NO ceiling and NO time budget. An adversarial repo of N backend files each DEFINING a
distinct SPECIFIC single-definer route + N frontend files each REQUESTING all N routes drives O(N⁴):
MEASURED (before) match 20→0.23s, 40→3.54s, 60→17.84s, 80→55.7s; a full build_graph at B=F=100 took 138.9s
for only 300 nodes; ~800 files ≈ 10 HOURS. There is NO timeout around build_graph, so the single event
worker would hang for hours and STARVE every other repo. This IS the real product hot path
(code_graph_extract.build_graph → _routes_graph → scan_repo → match_pairs).

THE FIX (recall-safe, behaviour-preserving):
  (a) INVERTED INDEX — index every SPECIFIC request URL by its CONCRETE path segments, then probe a
      definer route only against requests that share a concrete token. EVERY match requires a shared
      concrete segment (the concrete-anchor floor in _slots_align), so this can only skip provable
      NON-matches → byte-identical couples on normal inputs, while turning the realistic distinct-resource
      case from O(N⁴) into ~O(total route occurrences).
  (b) OUTPUT CAP `_MAX_ROUTE_PAIRS` (analogue of _cg_schema._MAX_TABLES / _cg_config._MAX_CONFIG_KEYS) —
      at most that many emitted file-pair couples, then stop (degrade gracefully).
  (c) WORK BUDGET `_MAX_ROUTE_PAIR_PROBES` — caps total match_routes comparisons so even a pathological
      token-collision repo (every route shares `api`) that DEFEATS the index can't re-create the blow-up.

This gate (OFFLINE, no Postgres) proves:
  (1) BOUNDED — the adversarial repo at N where it was TENS OF SECONDS now finishes in a few seconds AND
      respects the caps (pairs ≤ _MAX_ROUTE_PAIRS).
  (2) BOUNDED THROUGH THE INDEX — a token-collision repo (every route shares the `api` segment, which
      defeats a naive token index) is ALSO bounded by the work budget (time ~flat as N grows).
  (3) SAME COUPLES — a normal multi-route, multi-framework repo yields EXACTLY the couples the pre-fix
      logic produced (the load-bearing semantic check; the existing route gates encode the rest).
  (4) CAP HOLDS — a repo engineered to exceed _MAX_ROUTE_PAIRS emits no more than the ceiling.

Content-free throughout (route PATH strings + file paths only). Never-crash.
Prints ROUTES PAIRING BOUND GATE: PASS on success, ... FAIL on any failure.
"""
import sys
import os
import time
import tempfile
import shutil

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import _cg_routes as R


def _write(root, path, body):
    fp = os.path.join(root, path)
    os.makedirs(os.path.dirname(fp), exist_ok=True)
    with open(fp, "w") as fh:
        fh.write(body)


def _couples(scan_or_pairs):
    """Canonical {(sorted file pair): sorted shared-route list} from a scan dict or a pairs dict."""
    pairs = scan_or_pairs["pairs"] if isinstance(scan_or_pairs, dict) and "pairs" in scan_or_pairs else scan_or_pairs
    out = {}
    for pair, shared in pairs.items():
        key = tuple(sorted(x.replace(os.sep, "/") for x in pair))
        out[key] = sorted(shared)
    return out


# ---------------------------------------------------------------------------------------------
# A pure-Python re-implementation of the PRE-FIX nested-loop semantics, used ONLY as the SAME-COUPLES
# oracle on SMALL normal inputs. It is the exact O(B×F×R×U) body match_pairs used to run (multi-definer
# suppression + self-pair + cross-tier guard + specificity floor + match_routes), so a byte-identical
# result proves the indexed implementation preserves the contract. (Kept tiny + only run on small repos.)
# ---------------------------------------------------------------------------------------------
def _match_pairs_naive(defs, reqs, specificity_floor=True):
    route_definers = {}
    for bfile, broutes in defs.items():
        bf_reqs = reqs.get(bfile, ())
        for br in broutes:
            if br in bf_reqs:
                continue
            route_definers.setdefault(br, set()).add(bfile)
    pairs = {}
    for bfile, broutes in defs.items():
        for ffile, furls in reqs.items():
            if bfile == ffile:
                continue
            if not R._is_cross_tier(bfile, ffile):
                continue
            shared = set()
            for br in broutes:
                if specificity_floor and not R.is_specific(br):
                    continue
                if len(route_definers.get(br, ())) > 1:
                    continue
                for fu in furls:
                    if specificity_floor and not R.is_specific(fu):
                        continue
                    if R.match_routes(br, fu):
                        shared.add(br if len(br) >= len(fu) else fu)
            if shared:
                pairs[frozenset((bfile, ffile))] = shared
    return pairs


def _flask(route):
    return f"from flask import Flask\napp = Flask(__name__)\n@app.get('{route}')\ndef h(): pass\n"


def main():
    failures = []

    # -------------------------------------------------------------------------------------------------
    # (1) BOUNDED — the verified adversarial shape. N backend files each define ONE distinct specific
    #     single-definer route; N frontend files each request ALL N routes. Pre-fix this was O(N⁴):
    #     measured 60→17.84s, 80→55.7s. After: a few seconds, and the caps hold.
    # -------------------------------------------------------------------------------------------------
    N = 80
    defs = {f"backend/svc{k}.py": {f"/api/resource{k}/{{}}"} for k in range(N)}
    reqs = {f"web/page{j}.ts": {f"/api/resource{k}/5" for k in range(N)} for j in range(N)}
    t0 = time.perf_counter()
    pairs = R.match_pairs(defs, reqs, specificity_floor=True)
    dt = time.perf_counter() - t0
    BOUND_SECS = 8.0   # generous CI headroom; pre-fix was 55.7s at this N, measured ~1s after
    if dt > BOUND_SECS:
        print(f"FAIL [1 bounded]: adversarial N={N} took {dt:.2f}s (> {BOUND_SECS}s) — O(N⁴) not bounded")
        failures.append("adversarial-not-bounded")
    else:
        print(f"  [PASS] 1 bounded: adversarial N={N} match_pairs={dt:.3f}s (pre-fix ~55.7s), pairs={len(pairs)}")
    if len(pairs) > R._MAX_ROUTE_PAIRS:
        print(f"FAIL [1 cap]: emitted {len(pairs)} pairs > _MAX_ROUTE_PAIRS={R._MAX_ROUTE_PAIRS}")
        failures.append("output-cap-exceeded")

    # -------------------------------------------------------------------------------------------------
    # (2) BOUNDED THROUGH A DEFEATED INDEX — every route shares the concrete segment `api`, so a naive
    #     token index degenerates (the `api` bucket holds all requests). The WORK BUDGET must still bound
    #     it: time stays ~flat as N grows (pre-fix this was the same O(N⁴) blow-up).
    # -------------------------------------------------------------------------------------------------
    def _collision(N):
        d = {f"be/svc{k}.py": {f"/api/back{k}_{i}/{{}}" for i in range(N)} for k in range(N)}
        r = {f"fe/p{j}.ts": {f"/api/front{j}_{i}/5" for i in range(N)} for j in range(N)}
        return d, r
    times = []
    for n in (40, 80):
        d, r = _collision(n)
        t0 = time.perf_counter()
        R.match_pairs(d, r, specificity_floor=True)
        times.append(time.perf_counter() - t0)
    if max(times) > BOUND_SECS:
        print(f"FAIL [2 budget]: token-collision repo not bounded; times={[round(t,2) for t in times]}")
        failures.append("collision-not-bounded")
    else:
        print(f"  [PASS] 2 budget: token-collision (defeats index) bounded; times={[round(t,3) for t in times]}")

    # -------------------------------------------------------------------------------------------------
    # (3) SAME COUPLES — a normal multi-route, multi-framework repo: the indexed match_pairs must yield
    #     EXACTLY the pre-fix naive output (byte-identical couples). The load-bearing semantic check.
    # -------------------------------------------------------------------------------------------------
    root = tempfile.mkdtemp(prefix="routes_bound_norm_")
    try:
        # single-definer equal-length contract
        _write(root, "be/orders.py", _flask("/api/orders/{id}"))
        _write(root, "fe/orders.ts", "export const f=()=>fetch('/api/orders/5')\n")
        # tail/prefix-join contract (backend mounted, frontend full path)
        _write(root, "be/items.py", _flask("/items/{id}"))
        _write(root, "fe/items.ts", "export const f=()=>fetch('/api/items/9')\n")
        # multi-definer route → suppressed
        _write(root, "svcA/dup.py", _flask("/api/dup/{id}"))
        _write(root, "svcB/dup.py", _flask("/api/dup/{id}"))
        _write(root, "fe/dup.ts", "export const f=()=>fetch('/api/dup/3')\n")
        # ubiquitous → dropped by the specificity floor
        _write(root, "be/health.py", _flask("/health"))
        _write(root, "fe/health.ts", "export const f=()=>fetch('/health')\n")
        # test-path definer excluded; real coupling preserved
        _write(root, "shop/api.py", _flask("/api/carts/{id}"))
        _write(root, "tests/conftest.py", _flask("/api/carts/{id}"))
        _write(root, "fe/carts.ts", "export const f=()=>fetch('/api/carts/7')\n")
        # express self-def mis-read (axios.get parses as both def + request of the same route)
        _write(root, "server/teams.ts",
               'import {Router} from "express"\nconst r=Router()\nr.get("/api/teams/:id/members",(q,s)=>{})\n')
        _write(root, "web/teams.ts",
               'import axios from "axios"\nexport const m=(id)=>axios.get(`/api/teams/${id}/members`)\n')
        # nestjs prefix-join contract
        _write(root, "nest/orders.controller.ts",
               'import {Controller,Get} from "@nestjs/common";\n@Controller("widgets")\n'
               'export class C{@Get(":id")\nf(){}}\n')
        _write(root, "web/widgets.ts", 'export const f=()=>fetch("/widgets/5")\n')
        # multiple routes in one backend file paired with one frontend (intra-pair cross product)
        _write(root, "be/multi.py",
               "from flask import Flask\napp=Flask(__name__)\n"
               "@app.get('/api/alpha/{id}')\ndef a(): pass\n@app.get('/api/beta/{id}')\ndef b(): pass\n")
        _write(root, "fe/multi.ts", "export const a=()=>fetch('/api/alpha/1')\nexport const b=()=>fetch('/api/beta/2')\n")

        scan = R.scan_repo(root, specificity_floor=True)
        got = _couples(scan)
        want = _couples(_match_pairs_naive(scan["defs"], scan["reqs"], specificity_floor=True))
        if got != want:
            print("FAIL [3 same-couples]: indexed match_pairs DIVERGED from the pre-fix semantics")
            only_got = {k: got[k] for k in got if k not in want or got[k] != want.get(k)}
            only_want = {k: want[k] for k in want if k not in got or want[k] != got.get(k)}
            print(f"        indexed-only/diff: {only_got!r}")
            print(f"        naive-only/diff  : {only_want!r}")
            failures.append("couples-diverged")
        else:
            print(f"  [PASS] 3 same-couples: indexed == naive on a normal repo ({len(got)} couples, byte-identical)")
    finally:
        shutil.rmtree(root, ignore_errors=True)

    # -------------------------------------------------------------------------------------------------
    # (4) CAP HOLDS — engineer MORE than _MAX_ROUTE_PAIRS genuine couples (a dense all-to-all of distinct
    #     single-definer contracts) and confirm the output never exceeds the ceiling.
    # -------------------------------------------------------------------------------------------------
    # M backends each defining ONE distinct route; M frontends each requesting ALL M → M*M genuine pairs.
    M = 80   # 6400 genuine couples > _MAX_ROUTE_PAIRS (5000)
    ddefs = {f"b/svc{k}.py": {f"/api/thing{k}/{{}}"} for k in range(M)}
    dreqs = {f"f/page{j}.ts": {f"/api/thing{k}/5" for k in range(M)} for j in range(M)}
    capped_pairs = R.match_pairs(ddefs, dreqs, specificity_floor=True)
    if len(capped_pairs) > R._MAX_ROUTE_PAIRS:
        print(f"FAIL [4 cap]: {len(capped_pairs)} pairs emitted > ceiling {R._MAX_ROUTE_PAIRS}")
        failures.append("cap-not-enforced")
    else:
        print(f"  [PASS] 4 cap: dense repo capped at {len(capped_pairs)} ≤ _MAX_ROUTE_PAIRS={R._MAX_ROUTE_PAIRS}")

    if failures:
        print(f"ROUTES PAIRING BOUND GATE: FAIL (failures: {failures})")
        sys.exit(1)
    print("ROUTES PAIRING BOUND GATE: PASS")
    sys.exit(0)


if __name__ == "__main__":
    main()
