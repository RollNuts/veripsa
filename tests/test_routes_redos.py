"""Gate: the cross-tier ROUTE extractor is ReDoS-safe + does not read symlinked files.

Two CONFIRMED, MEASURED bugs in _cg_routes.py (parser/fuzzing audit) — both on the LIVE ingest path
(code_graph_extract.build_graph → _routes_graph → scan_repo → extract_route_defs / _walk):

P1 (HIGH — quadratic ReDoS, MEASURED 46s): _HAPI_ROUTE was
    re.compile(r'''\\.route\\s*\\(\\s*[\\[{].*?path\\s*:\\s*["'`]([^"'`]+)["'`]''', re.S)
The `.*?` under re.S (DOTALL) bridges from each `.route([`/`.route({` start to a DISTANT `path:`, across
the WHOLE file. extract_route_defs runs it via finditer over every .js/.jsx/.ts/.tsx file (per-file cap
MAX_FILE_BYTES = 400KB). A file of `.route({` × 50000 (== the 400KB cap) has 50000 start positions with NO
following `path:`; each re-scans lazily to EOF → O(N²). MEASURED before-fix: ~46s for that single 400KB
file; a few such files hang the SINGLE ingest/co-change worker for minutes → stalls EVERY tenant's webhook
deliveries (and the durable inbox behind it). FIX: cap the gap + drop re.S — `[^}]{0,512}?` (NO re.S),
brace-bounded so it stops at the route-object's closing `}` (≤512 chars), the same bounded style as
_cg_schema_orm.py (`[^}]{0,512}` / `[^;]{0,512}`). Linear (~0.1s) and byte-identical routes on legit input.

P3 (path-traversal — symlinked FILES): scan_repo's private _walk did NOT skip symlinked FILES (only
os.walk's default dir-symlink non-recursion + MAX_FILE_BYTES). A checked-out tree with `evil.js ->
/etc/passwd` (or `/proc/self/environ`) would be open()/read()/route-scanned, pulling a host file's content
into the scan. NOT reachable from prod App ingest (the tarball _safe_extractall drops symlink members;
co-change uses --no-checkout) but IS reachable from the local CLI (dogfood.sh / watch.py / evaluate.py /
build_graph) over a real tree. FIX: `if os.path.islink(full): continue` in _walk (every other engine walk
already routes through _passes_file_guards/islink — this was the one exception).

This gate (OFFLINE, no Postgres) proves:
  (1) ReDoS BOUND — the adversarial `.route({` × N (== the 400KB MAX_FILE_BYTES cap) input that took ~46s
      now completes in < 1s, via the REAL product entrypoint extract_route_defs (the regression guard, with
      the live 実測 timing), AND _HAPI_ROUTE no longer carries the re.S flag.
  (2) LEGIT ROUTES PRESERVED — real single-line, multi-line, and array-form `.route({path:'/x'})` defs
      still extract the SAME route strings (no recall regression), and the brace bound does NOT bridge a
      pathless `.route({...})` into a later unrelated `path:` (precision held).
  (3) SYMLINK SKIPPED — a symlinked file planted in a scanned tree is NOT walked/read by scan_repo (its
      route is absent from the scan), while the real sibling file IS scanned.

Content-free throughout (route PATH strings + file paths only). Never-crash.
Prints ROUTES REDOS GATE: PASS on success, ... FAIL on any failure.
"""
import sys
import os
import re
import time
import tempfile
import shutil

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import _cg_routes as R


def main():
    failures = []

    # -------------------------------------------------------------------------------------------------
    # (1) ReDoS BOUND — the verified adversarial input: `.route({` repeated to the 400KB per-file cap
    #     (MAX_FILE_BYTES), with NO following `path:`. Pre-fix _HAPI_ROUTE.finditer over this was O(N²):
    #     MEASURED ~46s. After: linear, well under 1s. Run through the REAL entrypoint extract_route_defs
    #     (the exact call the live build_graph → _routes_graph → scan_repo path makes per .ts/.js file).
    # -------------------------------------------------------------------------------------------------
    unit = ".route({"
    n = R.MAX_FILE_BYTES // len(unit)          # fill the 400KB per-file cap, like a real worst-case file
    body = unit * n
    assert len(body) <= R.MAX_FILE_BYTES and n >= 40_000, "adversarial body must fill the per-file cap"
    BOUND_SECS = 1.0                            # was ~46s; fix is ~0.1s. Generous CI headroom.
    t0 = time.perf_counter()
    routes = R.extract_route_defs("evil.ts", body)
    dt = time.perf_counter() - t0
    if dt > BOUND_SECS:
        print(f"FAIL [1 redos]: extract_route_defs on `.route({{`×{n} ({len(body)}B) took {dt:.2f}s "
              f"(> {BOUND_SECS}s) — the quadratic ReDoS is NOT bounded (pre-fix ~46s)")
        failures.append("redos-not-bounded")
    else:
        print(f"  [PASS] 1 redos: extract_route_defs on `.route({{`×{n} ({len(body)}B) = {dt:.3f}s "
              f"(pre-fix ~46s), routes={len(routes)}")
    # Regression guard on the PATTERN itself: re.S (DOTALL) is what let `.` bridge across the file.
    if R._HAPI_ROUTE.flags & re.S:
        print("FAIL [1 flag]: _HAPI_ROUTE still carries re.S (DOTALL) — the span can re-scan past newlines")
        failures.append("hapi-still-dotall")
    else:
        print("  [PASS] 1 flag: _HAPI_ROUTE has NO re.S (span is newline-aware + brace-bounded)")
    # And a second adversarial shape: `.route([` (array open) × N, also pathless — same blow-up class.
    body2 = ".route([" * n
    t0 = time.perf_counter()
    R.extract_route_defs("evil2.js", body2)
    dt2 = time.perf_counter() - t0
    if dt2 > BOUND_SECS:
        print(f"FAIL [1b redos]: `.route([`×{n} took {dt2:.2f}s (> {BOUND_SECS}s)")
        failures.append("redos-array-not-bounded")
    else:
        print(f"  [PASS] 1b redos: `.route([`×{n} = {dt2:.3f}s (bounded)")

    # -------------------------------------------------------------------------------------------------
    # (2) LEGIT ROUTES PRESERVED — the fix must keep extracting real Hapi route defs (no recall loss),
    #     including multi-line objects (newline is not `}`, so [^}] still spans lines) and array form;
    #     and must NOT bridge a pathless `.route({...})` into a later unrelated `path:` (precision).
    # -------------------------------------------------------------------------------------------------
    legit = (
        # single-line object
        ("server.route({ method: 'GET', path: '/api/orders/{id}', handler: h })", {"/api/orders/{}"}),
        # compact
        ('server.route({path:"/x/y"})', {"/x/y"}),
        # multi-line object (the common real-world style) — must still match
        ("server.route({\n  method: 'POST',\n  path: '/api/users/{id}',\n  handler: create\n})",
         {"/api/users/{}"}),
        # template-literal path
        ("server.route({ path: `/tpl/{id}` })", {"/tpl/{}"}),
        # array form: first element's path (identical to the pre-fix lazy `.*?` behaviour)
        ("server.route([{ method:'GET', path:'/a/b', handler:h }, { method:'POST', path:'/c/d', handler:g }])",
         {"/a/b"}),
    )
    for src, want in legit:
        got = R.extract_route_defs("app.ts", src)
        if not want.issubset(got):
            print(f"FAIL [2 recall]: legit Hapi def lost a route. want⊆ {want}, got {got} for {src[:60]!r}")
            failures.append("legit-route-lost")
            break
    else:
        print(f"  [PASS] 2 recall: legit single/multi-line/array `.route({{path:...}})` defs preserved")
    # Precision: a pathless route object must NOT bridge its trailing gap into a later config's `path:`.
    bridge = R.extract_route_defs(
        "app.ts", "server.route({ method: 'GET' })\nconst webpackCfg = { path: '/should_not_couple' }")
    if any("should_not_couple" in r for r in bridge):
        print(f"FAIL [2 precision]: pathless `.route({{}})` bridged into a later `path:` — got {bridge}")
        failures.append("brace-bridged")
    else:
        print(f"  [PASS] 2 precision: pathless `.route({{}})` does NOT bridge to a later unrelated path:")

    # -------------------------------------------------------------------------------------------------
    # (3) SYMLINK SKIPPED — plant `evil.ts -> <host file>` in a scanned tree alongside a REAL sibling.
    #     scan_repo's _walk must skip the symlinked file (its route absent), while the real file IS read.
    # -------------------------------------------------------------------------------------------------
    root = tempfile.mkdtemp(prefix="routes_redos_scanroot_")
    # The SECRET host file lives OUTSIDE the scanned root (mimics /etc/passwd, /proc/self/environ): the
    # ONLY path to it from inside the tree is the planted symlink, so if its route shows up in the scan it
    # PROVES _walk followed the link and read a host file. Its route-def line is what an attacker would get
    # back were the target a real source file.
    secret_dir = tempfile.mkdtemp(prefix="routes_redos_OUTSIDE_")
    secret = os.path.join(secret_dir, "host_secret.py")
    with open(secret, "w") as fh:
        fh.write("from flask import Flask\napp=Flask(__name__)\n"
                 "@app.get('/api/leaked_secret/{id}')\ndef h(): pass\n")
    try:
        os.makedirs(os.path.join(root, "be"), exist_ok=True)
        os.makedirs(os.path.join(root, "fe"), exist_ok=True)
        # A REAL backend file defining a route (this MUST be scanned) + a frontend requester for it.
        with open(os.path.join(root, "be", "real.py"), "w") as fh:
            fh.write("from flask import Flask\napp=Flask(__name__)\n"
                     "@app.get('/api/real/{id}')\ndef h(): pass\n")
        with open(os.path.join(root, "fe", "real.ts"), "w") as fh:
            fh.write("export const f=()=>fetch('/api/real/5')\n")

        link = os.path.join(root, "be", "evil.py")     # a scannable extension, but it's a symlink out
        try:
            os.symlink(secret, link)
        except (OSError, NotImplementedError):
            print("  [SKIP] 3 symlink: platform cannot create symlinks; skipping (fix is islink-guarded)")
        else:
            assert os.path.islink(link) and os.path.exists(link), "test symlink must resolve to the secret"
            scan = R.scan_repo(root, specificity_floor=True)
            scanned_files = {p.replace(os.sep, "/") for p in (set(scan["defs"]) | set(scan["reqs"]))}
            all_routes = set()
            for s in scan["defs"].values():
                all_routes |= s
            # Leak signal: the out-of-tree secret's route present, OR the symlink path itself was scanned.
            leaked = any("leaked_secret" in r for r in all_routes) or \
                any(f.replace("\\", "/").endswith("be/evil.py") for f in scanned_files)
            real_scanned = any(f.replace("\\", "/").endswith("be/real.py") for f in scanned_files)
            if leaked:
                print(f"FAIL [3 symlink]: scan_repo FOLLOWED the symlink and read the out-of-tree host file. "
                      f"files={sorted(scanned_files)} routes={sorted(all_routes)}")
                failures.append("symlink-read")
            elif not real_scanned:
                print(f"FAIL [3 symlink]: the REAL sibling be/real.py was not scanned — over-skip. "
                      f"files={sorted(scanned_files)}")
                failures.append("real-over-skipped")
            else:
                print("  [PASS] 3 symlink: out-of-tree symlink SKIPPED (host file not read), real sibling scanned")
    finally:
        shutil.rmtree(root, ignore_errors=True)
        shutil.rmtree(secret_dir, ignore_errors=True)

    if failures:
        print(f"ROUTES REDOS GATE: FAIL (failures: {failures})")
        sys.exit(1)
    print("ROUTES REDOS GATE: PASS")
    sys.exit(0)


if __name__ == "__main__":
    main()
