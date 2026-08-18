#!/usr/bin/env python3
"""OPENAPI SUBSTRATE GATE — OpenAPI/Swagger REST-contract cross-substrate coupling extraction.

WHAT THIS GATE PINS (hermetic synthetic fixtures; content-free: operation / schema / path NAMES +
file paths + edge kinds only — no descriptions, examples, default values, secrets, no DB, no
network):

  (1) CROWN JEWEL — an openapi.yaml defines operationId `getUser` on `/users/{id}` and a schema
      `Order`; a backend handler file defines a function `getUser`; a frontend client file hits the
      `/users/:id` route literal. The spec ALTERS the api_operation/api_schema nodes; the handler
      and the client QUERY the shared `api_operation::getUser` node — all couple with NO import/call
      edge between the three files.
  (2) MARKER GATE — a plain .yaml and a plain .json WITHOUT an openapi/swagger marker are NOT
      treated as specs (no api_operation / api_schema nodes minted from them).
  (3) PRECISION — generic schema names (Error/Response) and generic paths (/health) do NOT create
      couplings; an unanchored prose mention does not couple.
  (4) PARAM-STYLE NORMALIZATION — `/users/{id}` in the spec couples to a `/users/:id` code route
      (and to `/users/{userId}` — a different param name).
  (5) NEVER-CRASH on empty + malformed + binary-ish spec files.
  (6) CONTENT-FREE — a `description:` / `example:` value or a secret in the spec never appears in
      graph output.
  (7) ADDITIVE — a pure-Python repo with ordinary config json/yaml gets ZERO api_operation /
      api_schema nodes (no regression to _cg_config).
  (8) JSON SPEC — an openapi.json (Swagger 2 `definitions`/OpenAPI 3) is parsed the same way.
  (9) LINE-SCANNER FALLBACK — with PyYAML simulated absent, a .yaml spec still yields operations +
      schemas via the bounded line scanner (robustness, not completeness).

Print `OPENAPI SUBSTRATE GATE: PASS` or `OPENAPI SUBSTRATE GATE: FAIL`.
"""
from __future__ import annotations

import builtins
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import code_graph_extract as X  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _w(d: str, rel: str, body: str) -> None:
    p = os.path.join(d, rel)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as fh:
        fh.write(body)


def _wb(d: str, rel: str, body: bytes) -> None:
    p = os.path.join(d, rel)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "wb") as fh:
        fh.write(body)


def _build(files: dict) -> dict:
    """Write text files to a temp dir and call build_graph."""
    with tempfile.TemporaryDirectory() as d:
        for rel, body in files.items():
            _w(d, rel, body)
        return X.build_graph(d)


def _nodes_of_kind(g: dict, kind: str) -> dict:
    return {n["id"]: n for n in g["nodes"] if n.get("kind") == kind}


def _edges_of_kind(g: dict, kind: str) -> list:
    return [(e["src"], e["dst"]) for e in g["edges"] if e["kind"] == kind]


def _alters_to(g: dict, node_id: str) -> list:
    return [src for src, dst in _edges_of_kind(g, "alters") if dst == node_id]


def _queries_to(g: dict, node_id: str) -> list:
    return [src for src, dst in _edges_of_kind(g, "queries") if dst == node_id]


def _edge_records_to(g: dict, node_id: str) -> list:
    return [
        e for e in g["edges"]
        if e.get("kind") in ("alters", "queries") and e.get("dst") == node_id
    ]


def _couples_via_api(g: dict, a_sub: str, b_sub: str) -> list:
    """Return the api_operation/api_schema node ids through which file `a_sub` and file `b_sub`
    would COUPLE under res_adj: both files have an alters/queries edge to the SAME api node (and
    that node is not a hot res-hub). This mirrors the contention SQL's shared-resource adjacency
    so a precision assertion can check that two files do NOT couple through the contract graph.
    (No res-hub filter needed at this fixture scale: a hub needs >8 distinct files on one node.)"""
    touch = {}  # node_id -> set of files (substring-matched) touching it via alters/queries
    for e in g["edges"]:
        if e.get("kind") not in ("alters", "queries"):
            continue
        if e.get("reference_status") == "ambiguous":
            continue
        dst = str(e.get("dst", ""))
        if not (dst.startswith("api_operation::") or dst.startswith("api_schema::")):
            continue
        src = str(e.get("src", ""))
        which = touch.setdefault(dst, set())
        if a_sub in src:
            which.add("A")
        if b_sub in src:
            which.add("B")
    return [dst for dst, w in touch.items() if "A" in w and "B" in w]


def _direct_code_edges_between(g: dict, *substrs: str) -> list:
    """calls/imports edges whose src and dst together span the given path substrings (used to
    prove the coupling is via the contract node, not a code edge)."""
    out = []
    for e in g["edges"]:
        if e.get("kind") not in ("calls", "imports"):
            continue
        s = str(e.get("src", ""))
        d = str(e.get("dst", ""))
        for a in substrs:
            for b in substrs:
                if a != b and a in s and b in d:
                    out.append(e)
    return out


# A canonical OpenAPI 3 spec used by several cases. operationId `getUser` on `/users/{id}`,
# operationId `createOrder` on `/orders`, schemas Order + Error; a description and an example carry
# a SECRET marker (content-free check). NOTE: no f-strings here — SECRET is injected by .format.
_SPEC_YAML = """openapi: 3.0.0
info:
  title: shop api {SECRET}
  version: 1.0.0
paths:
  /users/{{id}}:
    get:
      operationId: getUser
      description: returns the {SECRET} user
      responses:
        '200':
          description: {SECRET}
          content:
            application/json:
              example: {{"token": "{SECRET}"}}
  /orders:
    post:
      operationId: createOrder
      responses:
        '201':
          description: created
  /health:
    get:
      responses:
        '200':
          description: ok
components:
  schemas:
    Order:
      type: object
      description: an order holding {SECRET}
      properties:
        id:
          type: string
          default: {SECRET}
    Error:
      type: object
"""


# ---------------------------------------------------------------------------
# Test cases
# ---------------------------------------------------------------------------

def main() -> int:
    failures: list = []

    def check(cond: bool, msg: str) -> None:
        if not cond:
            failures.append(msg)

    SECRET = "SUPER-SECRET-TOKEN-zzz999-should-not-appear"

    # -------------------------------------------------------------------------
    # (1) CROWN JEWEL: spec defines getUser on /users/{id} + schema Order; backend
    #     handler defines getUser(); frontend client hits /users/:id. All couple via
    #     api_operation::getUser with NO code edge between the three files.
    # -------------------------------------------------------------------------
    g1 = _build({
        "api/openapi.yaml": _SPEC_YAML.format(SECRET=SECRET),
        "server/handlers.py": (
            "def getUser(request, id):\n"
            "    return db.find(id)\n"
        ),
        "web/client.js": (
            "export async function loadUser(id) {\n"
            "  return fetch('/users/' + id);\n"
            "}\n"
            "const ROUTE = '/users/:id';\n"
        ),
    })
    op_id = "api_operation::getUser"
    order_id = "api_schema::Order"
    ops1 = _nodes_of_kind(g1, "api_operation")
    schemas1 = _nodes_of_kind(g1, "api_schema")
    check(op_id in ops1, f"(1) api_operation::getUser node missing; operations: {list(ops1)}")
    check(order_id in schemas1, f"(1) api_schema::Order node missing; schemas: {list(schemas1)}")
    # spec is the definer (alters) for both
    check(any("openapi.yaml" in s for s in _alters_to(g1, op_id)),
          f"(1) alters from openapi.yaml to api_operation::getUser missing; alters: {_alters_to(g1, op_id)}")
    check(any("openapi.yaml" in s for s in _alters_to(g1, order_id)),
          f"(1) alters from openapi.yaml to api_schema::Order missing; alters: {_alters_to(g1, order_id)}")
    # backend handler references the operationId as a DEFINED function (queries)
    q_op = _queries_to(g1, op_id)
    check(any("handlers.py" in s for s in q_op),
          f"(1) queries from handlers.py (def getUser) to api_operation::getUser missing; queries: {q_op}")
    # frontend client references the path literal (queries) — couples to the same operation node
    check(any("client.js" in s for s in q_op),
          f"(1) queries from client.js (/users/:id literal) to api_operation::getUser missing; queries: {q_op}")
    # CROWN JEWEL: no direct code edge among the three files (coupling is via the contract node)
    direct1 = _direct_code_edges_between(g1, "openapi.yaml", "handlers.py", "client.js")
    check(not direct1, f"(1) crown-jewel: unexpected direct code edge between the three files: {direct1}")

    # -------------------------------------------------------------------------
    # (2) MARKER GATE: plain .yaml / .json WITHOUT openapi/swagger marker are NOT specs.
    # -------------------------------------------------------------------------
    g2 = _build({
        "config/app.yaml": (
            "server:\n  host: localhost\n  port: 8080\n"
            "paths:\n  data: /var/lib\n"   # a `paths:` key that is NOT an OpenAPI paths block
        ),
        "config/settings.json": (
            '{"name": "svc", "version": "1.0", "operationId": "notReally", '
            '"paths": {"/x": {"get": {}}}}'   # has paths/operationId shapes but NO marker
        ),
        "app/main.py": "def run(): pass\n",
    })
    check(not _nodes_of_kind(g2, "api_operation"),
          f"(2) marker-gate: non-spec yaml/json must mint NO api_operation; got {list(_nodes_of_kind(g2, 'api_operation'))}")
    check(not _nodes_of_kind(g2, "api_schema"),
          f"(2) marker-gate: non-spec yaml/json must mint NO api_schema; got {list(_nodes_of_kind(g2, 'api_schema'))}")

    # -------------------------------------------------------------------------
    # (3) PRECISION: generic schema (Error) + generic path (/health) + unanchored prose.
    # -------------------------------------------------------------------------
    g3 = _build({
        "openapi.yaml": _SPEC_YAML.format(SECRET="x"),
        "srv/util.py": (
            "import requests\n"
            "def healthcheck():\n"
            "    requests.get('/health')\n"          # generic path literal -> must NOT couple
            "    raise Error('boom')\n"              # generic schema name use -> must NOT couple
        ),
        "docs/notes_as_code.py": (
            "# getUser fetches a user; the Order schema is documented here.\n"   # prose only (comment)
            "x = 1\n"
        ),
    })
    # /health op was never minted (no operationId, stoplisted path) -> nothing to couple to.
    health_ops = [oid for oid in _nodes_of_kind(g3, "api_operation") if "/health" in oid]
    check(not health_ops, f"(3) precision: /health must NOT mint an operation node; got {health_ops}")
    # Error schema node mints (it IS defined) but NOTHING couples to it (stoplisted reference name).
    err_id = "api_schema::Error"
    check(err_id in _nodes_of_kind(g3, "api_schema"),
          "(3) sanity: Error schema node should mint from the definition")
    check(not _queries_to(g3, err_id),
          f"(3) precision: generic schema 'Error' must have NO queries edges; got {_queries_to(g3, err_id)}")
    # The Order schema (distinctive) must not be coupled by the COMMENT-only mention in notes.
    q_order_schema = _queries_to(g3, "api_schema::Order")
    check(not any("notes_as_code" in s for s in q_order_schema),
          f"(3) precision: comment-only 'Order' mention must NOT couple; got {q_order_schema}")
    # getUser must not be coupled by the COMMENT-only mention either.
    q_getuser = _queries_to(g3, "api_operation::getUser")
    check(not any("notes_as_code" in s for s in q_getuser),
          f"(3) precision: comment-only 'getUser' mention must NOT couple; got {q_getuser}")

    # -------------------------------------------------------------------------
    # (4) PARAM-STYLE NORMALIZATION: spec /users/{id} couples to /users/:id and /users/{userId}.
    # -------------------------------------------------------------------------
    g4 = _build({
        "openapi.yaml": (
            "openapi: 3.0.0\n"
            "paths:\n"
            "  /users/{id}:\n"
            "    get:\n"
            "      responses:\n"
            "        '200':\n"
            "          description: ok\n"
        ),
        "a.js": "const r = '/users/:id';\n",          # express/rails colon style
        "b.py": "ROUTE = '/users/{userId}'\n",        # brace style, different param name
    })
    norm_op = "api_operation::GET /users/{}"
    check(norm_op in _nodes_of_kind(g4, "api_operation"),
          f"(4) normalized operation node missing; operations: {list(_nodes_of_kind(g4, 'api_operation'))}")
    q4 = _queries_to(g4, norm_op)
    check(any("a.js" in s for s in q4),
          f"(4) /users/:id (colon) must couple to GET /users/{{}}; queries: {q4}")
    check(any("b.py" in s for s in q4),
          f"(4) /users/{{userId}} (diff param name) must couple to GET /users/{{}}; queries: {q4}")

    # -------------------------------------------------------------------------
    # (5) NEVER-CRASH on empty + malformed + binary-ish spec content.
    # -------------------------------------------------------------------------
    crashed = False
    try:
        with tempfile.TemporaryDirectory() as d:
            _w(d, "empty.yaml", "")
            _w(d, "empty.json", "")
            _w(d, "malformed.yaml", "openapi: 3.0.0\npaths:\n  - : : not : valid {{[[\n")
            _w(d, "malformed.json", '{openapi: "3.0.0", paths: [[[ broken')
            _wb(d, "blob.yaml", b"\x00\x01\x02\xff\xfe openapi: 3.0.0 \x00 paths:")
            _wb(d, "blob.json", b'\x00\x00 {"openapi":"3.0.0"} \x00\x00')
            g5 = X.build_graph(d)
            check("nodes" in g5 and "edges" in g5, "(5) build_graph result must have nodes/edges keys")
    except Exception as exc:  # noqa: BLE001
        crashed = True
        check(False, f"(5) never-crash: build_graph raised on empty/malformed/binary spec files: {exc!r}")
    check(not crashed, "(5) never-crash: an exception was raised")

    # -------------------------------------------------------------------------
    # (6) CONTENT-FREE: a description / example / secret in the spec never appears in output.
    # -------------------------------------------------------------------------
    leaked = False
    for n in g1["nodes"]:
        if str(n.get("kind", "")).startswith("api_") and SECRET in str(n):
            leaked = True
            check(False, f"(6) content-free: secret leaked into api node: {n}")
    for e in g1["edges"]:
        if e.get("kind") in ("alters", "queries") and str(e.get("dst", "")).startswith("api_"):
            if SECRET in str(e):
                leaked = True
                check(False, f"(6) content-free: secret leaked into edge: {e}")
    check(not leaked, "(6) content-free: a secret leaked into the openapi graph output")
    # sanity: the legitimate operation/schema names ARE present (so we did not just emit nothing)
    names6 = {n.get("name") for n in g1["nodes"] if str(n.get("kind", "")).startswith("api_")}
    check("getUser" in names6 and "Order" in names6,
          f"(6) sanity: expected getUser + Order names; got {names6}")

    # -------------------------------------------------------------------------
    # (7) ADDITIVE regression: pure-Python repo with ordinary config json/yaml → NO api_* nodes.
    # -------------------------------------------------------------------------
    g7 = _build({
        "app/views.py": "def index(): return 'ok'\n",
        "app/models.py": "class User: pass\n",
        "config/app.yaml": "database:\n  pool_size: 20\n  timeout: 30\n",
        "config/settings.json": '{"feature_alpha": true, "max_retries": 3}',
        "package.json": '{"name": "app", "scripts": {"build": "x"}}',
    })
    for kind in ("api_operation", "api_schema"):
        got = _nodes_of_kind(g7, kind)
        check(not got, f"(7) additive: pure-Python+config repo must produce NO {kind} nodes; got: {list(got)}")

    # -------------------------------------------------------------------------
    # (8) JSON SPEC: an openapi.json is parsed the same way (operations + schemas).
    # -------------------------------------------------------------------------
    g8 = _build({
        "openapi.json": (
            '{\n'
            '  "openapi": "3.0.0",\n'
            '  "paths": {\n'
            '    "/widgets": {\n'
            '      "get": {"operationId": "listWidgets", "responses": {"200": {"description": "ok"}}}\n'
            '    }\n'
            '  },\n'
            '  "components": {"schemas": {"Widget": {"type": "object"}}}\n'
            '}\n'
        ),
        "server/widgets.py": "def listWidgets():\n    return []\n",
    })
    lw_id = "api_operation::listWidgets"
    check(lw_id in _nodes_of_kind(g8, "api_operation"),
          f"(8) json spec: api_operation::listWidgets missing; operations: {list(_nodes_of_kind(g8, 'api_operation'))}")
    check("api_schema::Widget" in _nodes_of_kind(g8, "api_schema"),
          f"(8) json spec: api_schema::Widget missing; schemas: {list(_nodes_of_kind(g8, 'api_schema'))}")
    check(any("widgets.py" in s for s in _queries_to(g8, lw_id)),
          f"(8) json spec: handler def listWidgets should couple; queries: {_queries_to(g8, lw_id)}")
    # Swagger 2 `definitions:` schema container also recognized.
    g8b = _build({
        "swagger.json": (
            '{"swagger": "2.0", "paths": {"/pets": {"get": {"operationId": "listPets"}}}, '
            '"definitions": {"Pet": {"type": "object"}}}'
        ),
        "h.py": "def listPets():\n    return []\n",
    })
    check("api_schema::Pet" in _nodes_of_kind(g8b, "api_schema"),
          f"(8b) swagger2 definitions: api_schema::Pet missing; schemas: {list(_nodes_of_kind(g8b, 'api_schema'))}")
    check("api_operation::listPets" in _nodes_of_kind(g8b, "api_operation"),
          f"(8b) swagger2: api_operation::listPets missing; operations: {list(_nodes_of_kind(g8b, 'api_operation'))}")

    # -------------------------------------------------------------------------
    # (9) LINE-SCANNER FALLBACK: with PyYAML simulated absent, a .yaml spec still yields
    #     operations + schemas via the bounded line scanner (robustness over completeness).
    # -------------------------------------------------------------------------
    _orig_import = builtins.__import__

    def _no_yaml(name, *a, **k):
        if name == "yaml":
            raise ImportError("simulated: PyYAML absent")
        return _orig_import(name, *a, **k)

    builtins.__import__ = _no_yaml
    try:
        g9 = _build({
            "openapi.yaml": (
                "openapi: 3.0.0\n"
                "paths:\n"
                "  /users/{id}:\n"
                "    get:\n"
                "      operationId: getUser\n"
                "      responses:\n"
                "        '200':\n"
                "          description: ok\n"
                "components:\n"
                "  schemas:\n"
                "    Order:\n"
                "      type: object\n"
            ),
            "h.py": "def getUser(id):\n    return id\n",
            "c.js": "const r = '/users/:id';\n",
        })
    finally:
        builtins.__import__ = _orig_import
    fb_ops = _nodes_of_kind(g9, "api_operation")
    fb_schemas = _nodes_of_kind(g9, "api_schema")
    check("api_operation::getUser" in fb_ops,
          f"(9) fallback: operationId getUser must mint without PyYAML; operations: {list(fb_ops)}")
    check("api_schema::Order" in fb_schemas,
          f"(9) fallback: schema Order must mint without PyYAML; schemas: {list(fb_schemas)}")
    check(any("h.py" in s for s in _queries_to(g9, "api_operation::getUser")),
          f"(9) fallback: handler def getUser should couple; queries: {_queries_to(g9, 'api_operation::getUser')}")
    check(any("c.js" in s for s in _queries_to(g9, "api_operation::getUser")),
          f"(9) fallback: /users/:id literal should couple via the path; "
          f"queries: {_queries_to(g9, 'api_operation::getUser')}")

    # -------------------------------------------------------------------------
    # (10) MULTI-DEFINER SUPPRESSION (P0) — real-repo audit found ~66% of false couples are the SAME
    #      operation/schema DEFINED in MULTIPLE unrelated spec files (different microservices /
    #      test fixtures). Two SEPARATE specs each defining `GET /testapi/application` (no
    #      operationId) and a schema `Widget` must NOT couple through those shared nodes; a
    #      SINGLE-definer spec↔handler crown-jewel in the same repo must STILL couple (recall).
    # -------------------------------------------------------------------------
    g10 = _build({
        # two unrelated services, each declaring the SAME path operation + the SAME schema name
        "svcA/openapi.yaml": (
            "openapi: 3.0.0\n"
            "paths:\n"
            "  /testapi/application:\n"
            "    get:\n"
            "      responses:\n"
            "        '200':\n"
            "          description: ok\n"
            "components:\n"
            "  schemas:\n"
            "    Widget:\n"
            "      type: object\n"
        ),
        "svcB/openapi.yaml": (
            "openapi: 3.0.0\n"
            "paths:\n"
            "  /testapi/application:\n"
            "    get:\n"
            "      responses:\n"
            "        '200':\n"
            "          description: ok\n"
            "components:\n"
            "  schemas:\n"
            "    Widget:\n"
            "      type: object\n"
        ),
        # a THIRD, UNRELATED service whose single spec uniquely defines a distinctive op + schema,
        # referenced by its OWN handler — the genuine single-definer crown jewel that must survive.
        "svcC/openapi.yaml": (
            "openapi: 3.0.0\n"
            "paths:\n"
            "  /examples/calc:\n"
            "    get:\n"
            "      operationId: runCalcUnique\n"
            "      responses:\n"
            "        '200':\n"
            "          description: ok\n"
            "components:\n"
            "  schemas:\n"
            "    CalcResultUnique:\n"
            "      type: object\n"
        ),
        "svcC/handler.go": (
            "package svcc\n"
            "func runCalcUnique() CalcResultUnique {\n"
            "    return CalcResultUnique{}\n"
            "}\n"
        ),
        "consumer/ambiguous.ts": (
            "export async function load(): Promise<Widget> {\n"
            "  return fetch('/testapi/application').then(r => r.json())\n"
            "}\n"
        ),
    })
    # The two unrelated specs must NOT couple via the duplicated operation/schema nodes.
    dup_couple = _couples_via_api(g10, "svcA/openapi.yaml", "svcB/openapi.yaml")
    check(not dup_couple,
          f"(10) multi-definer: two specs each defining GET /testapi/application + schema Widget "
          f"must NOT couple; coupled via {dup_couple}")
    # The duplicated nodes and their anchored references remain, but every edge
    # is explicitly inert and therefore absent from effective adjacency.
    dup_op = "api_operation::GET /testapi/application"
    dup_schema = "api_schema::Widget"
    for node_id, kind in ((dup_op, "api_operation"), (dup_schema, "api_schema")):
        node = _nodes_of_kind(g10, kind).get(node_id)
        evidence = _edge_records_to(g10, node_id)
        check(
            node is not None
            and (node.get("provenance") or {}).get("ambiguous") is True,
            f"(10) {node_id} must remain a provenance-bearing ambiguous node",
        )
        check(
            {"svcA/openapi.yaml", "svcB/openapi.yaml", "consumer/ambiguous.ts"}
            <= {e.get("src") for e in evidence}
            and all(e.get("reference_status") == "ambiguous" for e in evidence),
            f"(10) {node_id} definition/reference evidence must survive inert; got {evidence}",
        )
        file_statuses = {
            n.get("path"): n.get("analysis_status")
            for n in g10["nodes"]
            if n.get("kind") in {"file", "config_file"}
            and n.get("path") in {e.get("src") for e in evidence}
        }
        check(
            set(file_statuses) == {e.get("src") for e in evidence}
            and set(file_statuses.values()) == {"ambiguous"},
            f"(10) {node_id} sources must be locally Unknown; statuses={file_statuses}",
        )
    # RECALL: the single-definer crown jewel STILL couples spec↔handler.
    calc_op = "api_operation::runCalcUnique"
    calc_couple = _couples_via_api(g10, "svcC/openapi.yaml", "svcC/handler.go")
    check(calc_op in calc_couple,
          f"(10) recall: single-definer runCalcUnique spec<->handler must STILL couple; "
          f"coupled via {calc_couple}")
    check(any("openapi.yaml" in s for s in _alters_to(g10, calc_op)),
          f"(10) recall: single-definer op keeps its alters edge; got {_alters_to(g10, calc_op)}")
    check(any("handler.go" in s for s in _queries_to(g10, calc_op)),
          f"(10) recall: handler keeps its queries edge to the single-definer op; "
          f"got {_queries_to(g10, calc_op)}")
    check(
        all("reference_status" not in e for e in _edge_records_to(g10, calc_op)),
        "(10) unambiguous OpenAPI edges must remain unchanged (no status marker)",
    )

    # -------------------------------------------------------------------------
    # (11) INFRASTRUCTURE/ROLE SCHEMA STOPLIST (P3) + ULTRA-SHORT PATH (P3) — a schema named
    #      `Server`/`API`/`Config` (codegen struct, http.Server) and a path `/a` are too generic to
    #      anchor a coupling. The NODE may mint from the definition, but NOTHING couples through it.
    # -------------------------------------------------------------------------
    g11 = _build({
        "openapi.yaml": (
            "openapi: 3.0.0\n"
            "paths:\n"
            "  /a:\n"
            "    get:\n"
            "      responses:\n"
            "        '200':\n"
            "          description: ok\n"
            "components:\n"
            "  schemas:\n"
            "    Server:\n"
            "      type: object\n"
            "    API:\n"
            "      type: object\n"
            "    Config:\n"
            "      type: object\n"
        ),
        # code that uses the generic schema NAMES as bare identifiers and the short path literal —
        # none of these must couple to the spec.
        "srv/server.go": (
            "package srv\n"
            "type Server struct{}\n"
            "type API struct{}\n"
            "type Config struct{}\n"
            "var route = \"/a\"\n"
        ),
    })
    for nm in ("Server", "API", "Config"):
        sid = "api_schema::" + nm
        q = _queries_to(g11, sid)
        check(not q,
              f"(11) infra-stoplist: generic schema {nm!r} must have NO queries edges; got {q}")
    # the short single-segment path /a must NOT mint a coupling (no operationId on it).
    short_path_couple = _couples_via_api(g11, "openapi.yaml", "server.go")
    short_ops = [oid for oid in _nodes_of_kind(g11, "api_operation") if oid.endswith(" /a")]
    check(not short_ops,
          f"(11) short-path: GET /a must NOT mint an operation node; got {short_ops}")
    check(not short_path_couple,
          f"(11) short-path + infra-stoplist: spec and code must NOT couple through /a or Server/API/Config; "
          f"coupled via {short_path_couple}")

    # -------------------------------------------------------------------------
    # Summary
    # -------------------------------------------------------------------------
    if failures:
        for f in failures:
            print(f"  FAIL: {f}")
        print("OPENAPI SUBSTRATE GATE: FAIL")
        return 1

    print("openapi substrate: spec operationId/path operations + component schemas couple to "
          "anchored references (backend def of operationId + frontend path literal, crown-jewel, no code edge);")
    print("  marker-gate: plain yaml/json without openapi/swagger marker mint NO api_operation/api_schema nodes;")
    print("  precision: generic paths (/health) + generic schema names (Error) + comment-only mentions do NOT couple;")
    print("  param-style normalization: /users/{id} couples to /users/:id and /users/{userId};")
    print("  json spec + Swagger 2 definitions parsed the same way;")
    print("  never-crash: empty + malformed + binary-ish specs do not raise;")
    print("  content-free: a description/example value or a secret never appears in graph output;")
    print("  additive: pure-Python repo with ordinary config json/yaml gets zero api_* nodes (no _cg_config regression);")
    print("  line-scanner fallback: a .yaml spec still yields operations + schemas with PyYAML absent;")
    print("  multi-definer suppression: an operation/schema defined in MORE THAN ONE spec file keeps "
          "explicitly ambiguous evidence-only edges, so unrelated specs do NOT couple, while a "
          "single-definer spec-to-handler crown jewel STILL couples (recall);")
    print("  infra/role schema stoplist + ultra-short path: a schema named Server/API/Config and a path /a do NOT couple.")
    print("OPENAPI SUBSTRATE GATE: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
