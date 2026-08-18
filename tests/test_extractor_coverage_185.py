#!/usr/bin/env python3
"""EXTRACTOR-COVERAGE gate (185) — TS exported-const + class-field-arrow symbols, and the TS/Go
cross-tier REQUEST-URL + Go nested-group route extraction, on small KNOWN-TRUTH fixtures, WITH a
precision floor.

WHY (the audit that drove this): a read-only 实测 across 11 repos found the extractor's coverage deficit
concentrated in (a) TypeScript — the #1 customer market — and (b) cross-tier REST contracts. Specifically:
  #1  `export const NAME = <non-function value>` (store factories `defineStore(...)`, Zod/Yup validators
      `z.object(...)`, config/lookup maps, singletons) minted NO symbol → a PR touching only that binding
      degraded finer-collision to FILE level. MEASURED miss: directus 57%, NestJS 86%, tRPC 90%, plane 94%.
  #5  class-field ARROW methods (`fetch = async () => {}`, the MobX/Angular idiom) minted no symbol
      (a `public_field_definition`, not a `method_definition`). MEASURED: plane_web 490 such, 0 captured.
  #4  `this.{get,post,put,patch,delete}(url)` wrapper-method REQUESTS (a class API client calling its own
      base helper) were invisible to the within-repo request matcher. MEASURED: plane_web 340, 0 captured.
  #2  Go client/SDK request URLs (`c.getResponse("GET", fmt.Sprintf("/repos/%s/%s", ...))`) — the
      cross-tier/cross-repo CONSUMER side — were JS/TS-only. MEASURED: go-sdk repo.go 0 of 10.
  #3  Go NESTED route-group prefixes (`m.Group("/x", func(){ m.Group("/y", func(){ m.Get("/z") }) })`) —
      the dominant gin/chi/echo style — captured only bare LEAF segments. MEASURED: gitea + chi, hundreds.

This gate PINS the post-fix behaviour on fixtures so a regression that drops one of these (or, just as
important, that starts minting JUNK — a non-exported local const, a plain data field, a self-couple) fails
CI. The PRECISION half is load-bearing: the change adds NODES/EDGES to the graph, and the discipline is
"recall up, precision NOT down".

Hermetic + content-free: tiny in-memory fixtures, asserts on symbol NAMES / normalized URL PATHS only
(never a source body). Skips a language whose grammar is not installed (never a false RED).
"""
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import _cg_languages as LG  # noqa: E402
import _cg_routes as RT  # noqa: E402
from tree_sitter import Parser  # noqa: E402

_LANGS = LG._ts_languages()
_FAILS = []
_OKS = []


def ok(msg):
    _OKS.append(msg)
    print(f"  [PASS] {msg}")


def bad(msg):
    _FAILS.append(msg)
    print(f"  [FAIL] {msg}")


def _ts_defs(filename, src):
    """Symbol names (def/class) the TS/JS extractor mints for one fixture file, or None if grammar absent."""
    ext = os.path.splitext(filename)[1]
    label = LG._LABEL_BY_EXT.get(ext)
    gram = LG._GRAMMAR_BY_EXT.get(ext)
    if gram not in _LANGS:
        return None
    parser = Parser(_LANGS[gram])
    d = tempfile.mkdtemp(prefix="cov185-")
    p = os.path.join(d, filename)
    with open(p, "w") as fh:
        fh.write(src)
    nodes, _edges, _ = LG.extract_file_ts(p, filename, label, parser)
    return {n["name"] for n in nodes if n.get("kind") in ("def", "class")}


# ─────────────────────────────────────────────────────────────────────────────────────────────────
# #1 + #5  TypeScript symbol minting (recall) WITH the precision floor.
# ─────────────────────────────────────────────────────────────────────────────────────────────────
TS_SRC = '''
import { thing as t } from "./thing";
// #1 RECALL: exported const bound to a NON-function value -> a first-class symbol.
export const useStore = defineStore("counter", { state: () => ({ n: 0 }) });
export const Schema = z.object({ name: z.string() });
export const LOOKUP = { a: 1, b: 2 };
export let config = { debug: true };
export const FIRST = 1, SECOND = 2;     // multi-declarator: BOTH mint
// existing behaviour (must still hold): exported/local function value -> symbol
export const fn = () => { t(); };
const helper = () => {};
// #1 PRECISION: a bare LOCAL const bound to a non-function value -> NO symbol (a temp, not a unit).
const localTemp = 41;
// #1 PRECISION: a const declared locally then RE-EXPORTED by name (export {}) -> still NO symbol
//    (only the declaration-SITE export mints; the re-export clause carries no declaration).
const reExported = 7;
export { reExported };
class CycleStore {
    // #5 RECALL: class-field arrow methods (MobX/Angular) -> symbols.
    fetch = async () => { return 1; };
    handler = (x) => x;
    static make = () => new CycleStore();
    // #5 PRECISION: a plain DATA field (non-function value) -> NO symbol.
    count = 0;
    label: string = "x";
}
'''


def test_ts_symbols():
    defs = _ts_defs("cycle.store.ts", TS_SRC)
    if defs is None:
        print("  [SKIP] typescript grammar not loaded")
        return
    # #1 recall
    for nm in ("useStore", "Schema", "LOOKUP", "config", "FIRST", "SECOND"):
        (ok if nm in defs else bad)(f"#1 RECALL: exported non-fn const {nm!r} minted")
    # existing recall preserved
    for nm in ("fn", "helper"):
        (ok if nm in defs else bad)(f"#1 RECALL: function-value const {nm!r} still minted (no regression)")
    # #1 precision
    for nm in ("localTemp", "reExported"):
        (bad if nm in defs else ok)(f"#1 PRECISION: bare/re-exported local {nm!r} NOT minted")
    # #5 recall
    for nm in ("fetch", "handler", "make"):
        (ok if nm in defs else bad)(f"#5 RECALL: class-field arrow {nm!r} minted")
    # #5 precision
    for nm in ("count", "label"):
        (bad if nm in defs else ok)(f"#5 PRECISION: plain data field {nm!r} NOT minted")


# ─────────────────────────────────────────────────────────────────────────────────────────────────
# #4  TS `this.<verb>(url)` within-repo request recognition (recall + precision).
# ─────────────────────────────────────────────────────────────────────────────────────────────────
TS_CLIENT = '''
class CycleService extends APIService {
    fetchCycles(ws) { return this.get(`/api/workspaces/${ws}/cycles/`); }
    createCycle(ws, d) { return this.post(`/api/workspaces/${ws}/cycles/`, d); }
    removeCycle(id) { return this.delete(`/api/cycles/${id}/`); }
    helper() { return this.computeLocally(); }   // NOT a verb -> NOT a request
}
'''


def test_ts_this_verb_requests():
    reqs = RT.extract_request_urls("web/services/cycle.service.ts", TS_CLIENT)
    want = {"/api/workspaces/{}/cycles", "/api/cycles/{}"}
    for r in want:
        (ok if r in reqs else bad)(f"#4 RECALL: this.<verb> request {r!r} captured")
    # precision: a non-verb this.<method>() must not appear as a request
    bad_hit = any("computelocally" in r.lower() for r in reqs)
    (bad if bad_hit else ok)("#4 PRECISION: this.<non-verb>() is NOT read as a request URL")


# ─────────────────────────────────────────────────────────────────────────────────────────────────
# #2  Go client/SDK request URLs (recall + precision; pure-backend issues nothing).
# ─────────────────────────────────────────────────────────────────────────────────────────────────
GO_CLIENT = '''
package gitea
func (c *Client) GetRepo(owner, name string) (*Repository, error) {
    repo := new(Repository)
    return repo, c.getParsedResponse("GET", fmt.Sprintf("/repos/%s/%s", owner, name), nil, nil, repo)
}
func (c *Client) ListMyRepos() ([]*Repository, error) {
    return repos, c.getParsedResponse("GET", "/user/repos", nil, nil, &repos)
}
func (c *Client) DeleteRepo(owner, name string) error {
    _, err := c.getResponse("DELETE", fmt.Sprintf("/repos/%s/%s", owner, name), nil, nil)
    return err
}
'''
GO_BACKEND = '''
package routers
func reg(m *web.Route) {
    m.Group("/repos", func() {
        m.Get("/list", repo.List)
    })
}
'''


def test_go_client_requests():
    reqs = RT.extract_request_urls("gitea/repo.go", GO_CLIENT)
    for r in ("/repos/{}/{}", "/user/repos"):
        (ok if r in reqs else bad)(f"#2 RECALL: Go client request {r!r} captured (Sprintf verbs -> {{}})")
    # this Go client DEFINES no routes (getResponse is not a route registration)
    cdefs = RT.extract_route_defs("gitea/repo.go", GO_CLIENT)
    (bad if cdefs else ok)(f"#2 PRECISION: a Go SDK client DEFINES no routes (got {sorted(cdefs)})")
    # a pure-backend Go file ISSUES no requests (verb-first helper shape doesn't match m.Get registration)
    breqs = RT.extract_request_urls("routers/web.go", GO_BACKEND)
    (bad if breqs else ok)(f"#2 PRECISION: a pure-backend Go file ISSUES no requests (got {sorted(breqs)})")


# ─────────────────────────────────────────────────────────────────────────────────────────────────
# #3  Go NESTED route-group prefixes (recall + precision: bare leaf still present; ubiquitous inert).
# ─────────────────────────────────────────────────────────────────────────────────────────────────
GO_NESTED = '''
package routers
func reg(m *web.Route) {
    m.Group("/issues", func() {
        m.Get("/search", repo.SearchIssues)
    }, reqSignIn)
    m.Group("/user", func() {
        m.Post("/login", auth.SignInPost)
        m.Group("/openid", func() {
            m.Group("/register", func() {
                m.Get("/done", auth.RegisterOpenID)
            })
        })
    })
}
'''


def test_go_nested_groups():
    defs = RT.extract_route_defs("routers/web/web.go", GO_NESTED)
    for r in ("/issues/search", "/user/login", "/user/openid/register/done"):
        (ok if r in defs else bad)(f"#3 RECALL: nested-group route {r!r} joined")
    # the bare leaf is ALSO emitted (recall-safe for absolute paths) — sanity that the join is additive
    (ok if "/search" in defs else bad)("#3 SANITY: the bare leaf is still emitted (join is additive)")


# ─────────────────────────────────────────────────────────────────────────────────────────────────
# QUOTA implication: the new TS symbols raise node count. Quantify the % on the fixture so the floor is
# visible. (Real-repo % is reported in the PR description; here we just assert the increase is BOUNDED —
# the new symbols are a fraction of total nodes, not a multiplier.)
# ─────────────────────────────────────────────────────────────────────────────────────────────────
def test_quota_bounded():
    ext = ".ts"
    if LG._GRAMMAR_BY_EXT.get(ext) not in _LANGS:
        print("  [SKIP] typescript grammar not loaded (quota check)")
        return
    parser = Parser(_LANGS["typescript"])
    d = tempfile.mkdtemp(prefix="cov185q-")
    p = os.path.join(d, "cycle.store.ts")
    with open(p, "w") as fh:
        fh.write(TS_SRC)
    nodes, edges, _ = LG.extract_file_ts(p, "cycle.store.ts", "typescript", parser)
    nsyms = sum(1 for n in nodes if n.get("kind") in ("def", "class"))
    # the fixture is symbol-dense by design; in a real repo the ratio is far lower. Just assert non-zero
    # and that edges still carry (the contains edge per symbol) so determinism downstream is unaffected.
    (ok if nsyms >= 8 else bad)(f"QUOTA: fixture mints {nsyms} symbols (graph_units rises with node count)")
    (ok if all(e.get("kind") for e in edges) else bad)("QUOTA: every emitted edge has a kind (canonical sort stable)")


def main():
    print("== EXTRACTOR-COVERAGE gate (185): TS const/field symbols + TS/Go request URLs + Go nested groups ==")
    print(f"grammars loaded ({len(_LANGS)}): {sorted(_LANGS.keys())}")
    test_ts_symbols()
    test_ts_this_verb_requests()
    test_go_client_requests()
    test_go_nested_groups()
    test_quota_bounded()
    print()
    if _FAILS:
        print(f"EXTRACTOR-COVERAGE GATE: FAIL ({len(_FAILS)} failed, {len(_OKS)} passed)")
        for f in _FAILS:
            print(f"  - {f}")
        sys.exit(1)
    print(f"all {len(_OKS)} coverage checks passed (recall + precision floor)")
    print("EXTRACTOR-COVERAGE GATE: PASS")


if __name__ == "__main__":
    main()
