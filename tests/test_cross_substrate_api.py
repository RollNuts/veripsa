#!/usr/bin/env python3
"""API-CONTRACT SUBSTRATE GATE — GraphQL + protobuf/gRPC cross-substrate coupling extraction.

WHAT THIS GATE PINS (hermetic synthetic fixtures; content-free: contract symbol NAMES + file
paths + edge kinds only — no field values, no message bodies, no comment content, no DB, no
network):

GraphQL:
  (1) CROWN JEWEL — a `.graphql` schema defines `type Order`, a resolver file references it
      via a resolver-map key (`Order: {...}`), and a frontend file issues a `gql`{ order { id } }``
      query referencing Order. All three couple via the `api_type::Order` node with NO import/call
      edge between them.
  (2) PRECISION — the GraphQL root operation types `Query`/`Mutation` and built-in scalars
      `String`/`ID` do NOT mint api_type nodes (ubiquitous → would couple everything).

protobuf:
  (3) CROWN JEWEL — a `.proto` defining `service OrderService` + `message CreateOrderRequest`
      couples to a hand-written client file that references `OrderServiceClient` /
      `CreateOrderRequest` via the shared api_service/api_message nodes, with NO code edge.
  (4) PRECISION — a plain English sentence containing a word equal to a type name (unanchored)
      does NOT create a queries edge (no resolver map, no gql template, no proto stub form).

Robustness / discipline:
  (5) Never-crash on empty + binary-ish .graphql/.proto content.
  (6) Content-free — a secret in a `.proto`/`.graphql` COMMENT does not appear in graph output.

Orchestrator regression:
  (7) build_graph on a pure-.py repo produces NO api_type / api_message / api_service nodes (the
      API-contract pass is additive, never regresses code-only repos).

Print `API-CONTRACT SUBSTRATE GATE: PASS` or `API-CONTRACT SUBSTRATE GATE: FAIL`.
"""
from __future__ import annotations

import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import code_graph_extract as X  # noqa: E402
import _cg_api_contract as API  # noqa: E402


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


def _build(files: dict[str, str]):
    """Write text files to a temp dir and call build_graph."""
    with tempfile.TemporaryDirectory() as d:
        for rel, body in files.items():
            _w(d, rel, body)
        return X.build_graph(d)


def _nodes_of_kind(g: dict, kind: str) -> dict[str, dict]:
    return {n["id"]: n for n in g["nodes"] if n.get("kind") == kind}


def _edges_of_kind(g: dict, kind: str) -> list[tuple[str, str]]:
    return [(e["src"], e["dst"]) for e in g["edges"] if e["kind"] == kind]


def _alters_to(g: dict, node_id: str) -> list[str]:
    return [src for src, dst in _edges_of_kind(g, "alters") if dst == node_id]


def _queries_to(g: dict, node_id: str) -> list[str]:
    return [src for src, dst in _edges_of_kind(g, "queries") if dst == node_id]


def _edge_records_to(g: dict, node_id: str) -> list[dict]:
    return [
        e for e in g["edges"]
        if e.get("kind") in ("alters", "queries") and e.get("dst") == node_id
    ]


def _files_touching(g: dict, node_id: str) -> set[str]:
    """Every file that has an alters OR queries edge to node_id. Per db/schema/70_social.sql
    `res_adj`, any TWO distinct files in this set COUPLE (share the resource node) — so a couple
    exists for node_id iff len(_files_touching) >= 2. Mirrors the real coupling rule exactly."""
    out: set[str] = set()
    for e in g["edges"]:
        if (
            e.get("kind") in ("alters", "queries")
            and e.get("dst") == node_id
            and e.get("reference_status") != "ambiguous"
        ):
            out.add(str(e.get("src", "")))
    return out


def _couples(g: dict, node_id: str) -> bool:
    """True iff >=2 distinct files touch node_id (i.e. it produces a cross-file coupling)."""
    return len(_files_touching(g, node_id)) >= 2


def _node_exists(g: dict, node_id: str) -> bool:
    return any(n.get("id") == node_id for n in g["nodes"])


def _direct_code_edges_between(g: dict, *substrs: str) -> list:
    """calls/imports edges whose src and dst together span the given path substrings
    (used to prove the coupling is via the contract node, not a code edge)."""
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


# ---------------------------------------------------------------------------
# Test cases
# ---------------------------------------------------------------------------

def main() -> int:
    failures: list[str] = []

    def check(cond: bool, msg: str) -> None:
        if not cond:
            failures.append(msg)

    # -------------------------------------------------------------------------
    # (1) GraphQL CROWN JEWEL: schema defines `type Order`; resolver + gql query
    #     reference it; all three couple via api_type::Order with NO code edge.
    # -------------------------------------------------------------------------
    g1 = _build({
        "schema/order.graphql": (
            "type Order {\n"
            "  id: ID!\n"
            "  total: Float!\n"
            "  customer: Customer!\n"
            "}\n"
            "type Customer {\n"
            "  id: ID!\n"
            "}\n"
        ),
        "server/resolvers.js": (
            "const resolvers = {\n"
            "  Order: {\n"
            "    total(parent) { return parent.total; },\n"
            "  },\n"
            "  Query: {\n"
            "    order(_, args) { return db.find(args.id); },\n"
            "  },\n"
            "};\n"
            "module.exports = resolvers;\n"
        ),
        "web/OrderPage.js": (
            "import { useQuery } from '@apollo/client';\n"
            "const ORDER_Q = gql`{\n"
            "  order(id: 1) {\n"
            "    id\n"
            "    ... on Order { total }\n"
            "  }\n"
            "}`;\n"
            "export function OrderPage() { return useQuery(ORDER_Q); }\n"
        ),
    })
    order_id = "api_type::Order"
    api_types1 = _nodes_of_kind(g1, "api_type")
    check(order_id in api_types1, f"(1) api_type::Order node missing; api_type nodes: {list(api_types1)}")
    # schema is the definer (alters)
    alters_order = _alters_to(g1, order_id)
    check(
        any("order.graphql" in s for s in alters_order),
        f"(1) alters edge from schema/order.graphql to api_type::Order missing; alters: {alters_order}"
    )
    # resolver references via resolver-map key (queries)
    q_order = _queries_to(g1, order_id)
    check(
        any("resolvers.js" in s for s in q_order),
        f"(1) queries edge from resolvers.js (resolver map key) to api_type::Order missing; queries: {q_order}"
    )
    # frontend references via gql tagged template (queries)
    check(
        any("OrderPage.js" in s for s in q_order),
        f"(1) queries edge from OrderPage.js (gql template) to api_type::Order missing; queries: {q_order}"
    )
    check(
        all("reference_status" not in e for e in _edge_records_to(g1, order_id)),
        "(1) unambiguous GraphQL edges must remain unchanged (no status marker)",
    )
    # CROWN JEWEL: no direct code edge between the three files (coupling is via the contract node)
    direct1 = _direct_code_edges_between(g1, "order.graphql", "resolvers.js", "OrderPage.js")
    check(not direct1, f"(1) crown-jewel: unexpected direct code edge between the three files: {direct1}")

    # -------------------------------------------------------------------------
    # (2) PRECISION: Query / Mutation / String / ID do NOT mint api_type nodes.
    # -------------------------------------------------------------------------
    g2 = _build({
        "schema/root.graphql": (
            "type Query {\n"
            "  ping: String!\n"
            "  thing: ID\n"
            "}\n"
            "type Mutation {\n"
            "  noop: Boolean\n"
            "}\n"
            "scalar String\n"   # even an explicit redecl of a builtin must not mint
            "type Thing {\n"
            "  id: ID!\n"
            "}\n"
        ),
    })
    api_types2 = _nodes_of_kind(g2, "api_type")
    names2 = {n.get("name") for n in api_types2.values()}
    for forbidden in ("Query", "Mutation", "Subscription", "String", "Int", "Float", "Boolean", "ID"):
        check(
            forbidden not in names2,
            f"(2) precision: stoplisted GraphQL name '{forbidden}' must NOT mint an api_type node; names: {names2}"
        )
    # a real user-defined type IS minted (sanity: the stoplist did not over-suppress)
    check("Thing" in names2, f"(2) sanity: user-defined 'Thing' should still mint; names: {names2}")

    # -------------------------------------------------------------------------
    # (3) protobuf CROWN JEWEL: .proto defines service+message; client references them.
    # -------------------------------------------------------------------------
    g3 = _build({
        "proto/order.proto": (
            'syntax = "proto3";\n'
            "package shop;\n"
            "message CreateOrderRequest {\n"
            "  string sku = 1;\n"
            "  int32 qty = 2;\n"
            "}\n"
            "message CreateOrderResponse {\n"
            "  string order_id = 1;\n"
            "}\n"
            "service OrderService {\n"
            "  rpc Create(CreateOrderRequest) returns (CreateOrderResponse);\n"
            "}\n"
        ),
        "client/order_client.go": (
            "package client\n"
            "import pb \"shop/proto\"\n"
            "func PlaceOrder(c OrderServiceClient) {\n"
            "    req := &CreateOrderRequest{Sku: \"abc\", Qty: 1}\n"
            "    c.Create(req)\n"
            "}\n"
        ),
    })
    svc_id = "api_service::OrderService"
    msg_id = "api_message::CreateOrderRequest"
    api_services3 = _nodes_of_kind(g3, "api_service")
    api_messages3 = _nodes_of_kind(g3, "api_message")
    check(svc_id in api_services3, f"(3) api_service::OrderService node missing; services: {list(api_services3)}")
    check(msg_id in api_messages3, f"(3) api_message::CreateOrderRequest node missing; messages: {list(api_messages3)}")
    # .proto is the definer (alters)
    check(
        any("order.proto" in s for s in _alters_to(g3, svc_id)),
        f"(3) alters edge from order.proto to api_service::OrderService missing; alters: {_alters_to(g3, svc_id)}"
    )
    check(
        any("order.proto" in s for s in _alters_to(g3, msg_id)),
        f"(3) alters edge from order.proto to api_message::CreateOrderRequest missing; alters: {_alters_to(g3, msg_id)}"
    )
    # client references service (stub form OrderServiceClient) + message (exact)
    q_svc = _queries_to(g3, svc_id)
    q_msg = _queries_to(g3, msg_id)
    check(
        any("order_client.go" in s for s in q_svc),
        f"(3) queries edge from order_client.go to api_service::OrderService (stub form) missing; queries: {q_svc}"
    )
    check(
        any("order_client.go" in s for s in q_msg),
        f"(3) queries edge from order_client.go to api_message::CreateOrderRequest missing; queries: {q_msg}"
    )
    # CROWN JEWEL: no direct code edge between .proto and the client
    direct3 = _direct_code_edges_between(g3, "order.proto", "order_client.go")
    check(not direct3, f"(3) crown-jewel: unexpected direct code edge between .proto and client: {direct3}")

    # -------------------------------------------------------------------------
    # (4) PRECISION: an unanchored English word equal to a type name does NOT couple.
    # -------------------------------------------------------------------------
    g4 = _build({
        "schema/order.graphql": (
            "type Order {\n"
            "  id: ID!\n"
            "}\n"
        ),
        "docs/notes.txt_as_code.js": (
            "// Order the items by date before shipping.\n"
            "// The Order of operations matters here.\n"
            "function sortByDate(items) { return items.sort(); }\n"
            "const note = 'Please Order more stock';\n"
        ),
    })
    order_id4 = "api_type::Order"
    q4 = _queries_to(g4, order_id4)
    check(
        not any("notes" in s for s in q4),
        f"(4) precision: unanchored 'Order' in prose/code must NOT create a queries edge; queries: {q4}"
    )
    # proto precision: a known message name that appears ONLY inside a COMMENT must NOT couple
    # (comments are stripped on the reference side — a name in prose is not a real stub use).
    g4b = _build({
        "proto/widget.proto": (
            'syntax = "proto3";\n'
            "message WidgetConfig {\n"
            "  string color = 1;\n"
            "}\n"
        ),
        "src/readme_ish.py": (
            "# The WidgetConfig describes how a widget looks.\n"
            "x = 1  # nothing here references the generated stub\n"
        ),
    })
    msg4b = _nodes_of_kind(g4b, "api_message")
    wc_id = "api_message::WidgetConfig"
    # The node still mints (from the .proto definition)…
    check(
        any(n.get("name") == "WidgetConfig" for n in msg4b.values()),
        f"(4b) sanity: WidgetConfig should mint (distinctive proto name); messages: {list(msg4b)}"
    )
    # …but the comment-only mention in readme_ish.py must NOT create a queries edge (precision).
    q4b = _queries_to(g4b, wc_id)
    check(
        not any("readme_ish" in s for s in q4b),
        f"(4b) precision: comment-only proto name must NOT create a queries edge; queries: {q4b}"
    )
    # And a REAL stub use (in code, not a comment) DOES couple (recall sanity):
    g4c = _build({
        "proto/widget.proto": (
            'syntax = "proto3";\n'
            "message WidgetConfig {\n"
            "  string color = 1;\n"
            "}\n"
        ),
        "src/use_widget.py": (
            "cfg = WidgetConfig(color='red')\n"   # real code use, not a comment
        ),
    })
    q4c = _queries_to(g4c, "api_message::WidgetConfig")
    check(
        any("use_widget" in s for s in q4c),
        f"(4c) recall: real code use of WidgetConfig SHOULD couple; queries: {q4c}"
    )

    # -------------------------------------------------------------------------
    # (5) Never-crash on empty + binary-ish .graphql/.proto content.
    # -------------------------------------------------------------------------
    crashed = False
    try:
        with tempfile.TemporaryDirectory() as d:
            _w(d, "schema/empty.graphql", "")
            _w(d, "proto/empty.proto", "")
            # binary-ish content (NUL bytes) — build_graph's guards should skip it; must not raise
            _wb(d, "proto/blob.proto", b"\x00\x01\x02\xff\xfe garbage \x00 message X {")
            _wb(d, "schema/blob.graphql", b"\x00\x00 type X \x00\x00")
            # also exercise the substrate entry directly on the binary file path (defensive)
            g5 = X.build_graph(d)
            check("nodes" in g5 and "edges" in g5, "(5) build_graph result must have nodes/edges keys")
    except Exception as exc:  # noqa: BLE001
        crashed = True
        check(False, f"(5) never-crash: build_graph raised on empty/binary contract files: {exc!r}")
    check(not crashed, "(5) never-crash: an exception was raised")

    # -------------------------------------------------------------------------
    # (6) Content-free: a secret in a .proto / .graphql COMMENT does not appear in output.
    # -------------------------------------------------------------------------
    SECRET = "SUPER-SECRET-TOKEN-abc123-should-not-appear"
    g6 = _build({
        "schema/sec.graphql": (
            f"# api key: {SECRET}\n"
            "type Account {\n"
            f'  # the password default is {SECRET}\n'
            "  id: ID!\n"
            "}\n"
        ),
        "proto/sec.proto": (
            'syntax = "proto3";\n'
            f"// shared secret: {SECRET}\n"
            "message Credential {\n"
            f"  string token = 1; // {SECRET}\n"
            "}\n"
        ),
    })
    # Scan ALL api_* nodes and ALL alters/queries edges for the secret string.
    leaked = False
    for n in g6["nodes"]:
        if str(n.get("kind", "")).startswith("api_"):
            if SECRET in str(n):
                leaked = True
                check(False, f"(6) content-free: secret leaked into api node: {n}")
    for e in g6["edges"]:
        if e.get("kind") in ("alters", "queries") and str(e.get("dst", "")).startswith("api_"):
            if SECRET in str(e):
                leaked = True
                check(False, f"(6) content-free: secret leaked into edge: {e}")
    check(not leaked, "(6) content-free: a secret leaked into the api-contract graph output")
    # sanity: the legitimate type/message names ARE present (so we did not just emit nothing)
    api6 = {n.get("name") for n in g6["nodes"] if str(n.get("kind", "")).startswith("api_")}
    check("Account" in api6 and "Credential" in api6,
          f"(6) sanity: expected Account + Credential names; got {api6}")

    # -------------------------------------------------------------------------
    # (7) Additive regression: pure-Python repo produces NO api_* nodes.
    # -------------------------------------------------------------------------
    g7 = _build({
        "app/views.py": "def index(): return 'ok'\n",
        "app/models.py": "class User: pass\n",
    })
    for kind in ("api_type", "api_message", "api_service"):
        got = _nodes_of_kind(g7, kind)
        check(not got, f"(7) additive regression: pure-Python repo must produce NO {kind} nodes; got: {list(got)}")

    # -------------------------------------------------------------------------
    # (8) PRECISION (CLASS A multi-definer GraphQL) + RECALL (single-definer survives).
    #     AUDIT 2026-06-20: the SAME `input SampleInput` defined in multiple .graphql files all
    #     emitted `alters` to the one api_type::SampleInput node and coupled the definers falsely.
    #     A multi-definer type must NOT couple; a single-definer type↔resolver MUST still couple.
    # -------------------------------------------------------------------------
    g8 = _build({
        # SampleInput defined in THREE unrelated schema fixtures → ambiguous → must NOT couple.
        "tests/a/schema.graphql": "input SampleInput {\n  x: Int\n}\n",
        "tests/b/schema.graphql": "input SampleInput {\n  y: Int\n}\n",
        "tests/c/schema.graphql": "input SampleInput {\n  z: Int\n}\n",
        "server/ambiguous.js": "const resolvers = { SampleInput: { x(v) { return v.x; } } };\n",
        # Order is defined ONCE and referenced from a resolver map → single-definer crown jewel.
        "schema/order.graphql": "type Order {\n  id: ID!\n}\n",
        "server/resolvers.js": (
            "const resolvers = {\n  Order: { id(p) { return p.id; } },\n};\n"
            "module.exports = resolvers;\n"
        ),
    })
    si_id = "api_type::SampleInput"
    si_node = _nodes_of_kind(g8, "api_type").get(si_id)
    check(
        si_node is not None
        and (si_node.get("provenance") or {}).get("ambiguous") is True,
        "(8) ambiguous GraphQL type must remain a first-class provenance-bearing node",
    )
    si_edges = _edge_records_to(g8, si_id)
    check(
        {e.get("src") for e in si_edges}
        == {
            "tests/a/schema.graphql",
            "tests/b/schema.graphql",
            "tests/c/schema.graphql",
            "server/ambiguous.js",
        }
        and all(e.get("reference_status") == "ambiguous" for e in si_edges),
        f"(8) all ambiguous GraphQL definition/reference evidence must survive inert; edges={si_edges}",
    )
    si_file_statuses = {
        n.get("path"): n.get("analysis_status")
        for n in g8["nodes"]
        if n.get("kind") == "file"
        and n.get("path") in {e.get("src") for e in si_edges}
    }
    check(
        set(si_file_statuses) == {e.get("src") for e in si_edges}
        and set(si_file_statuses.values()) == {"ambiguous"},
        f"(8) ambiguous GraphQL sources must be locally Unknown; statuses={si_file_statuses}",
    )
    check(
        not _couples(g8, si_id),
        f"(8) precision: multi-definer 'SampleInput' (3 schemas) must NOT couple; "
        f"files touching it: {sorted(_files_touching(g8, si_id))}"
    )
    # The single-definer crown jewel still couples (schema definer + resolver referencer).
    order_id8 = "api_type::Order"
    check(
        _couples(g8, order_id8),
        f"(8) recall: single-definer 'Order' (schema + resolver) MUST still couple; "
        f"files touching it: {sorted(_files_touching(g8, order_id8))}"
    )

    # -------------------------------------------------------------------------
    # (9) PRECISION (CLASS B GraphQL stoplist): Hasura root names query_root/mutation_root and a
    #     cross-language collision word 'Body' must NOT mint a node nor couple unrelated files.
    # -------------------------------------------------------------------------
    g9 = _build({
        # An introspection-style schema declaring the convention root names + a Body type, plus a
        # real user type Widget for the recall sanity below.
        "schema/introspection.graphql": (
            "type query_root {\n  widgets: [Widget!]!\n}\n"
            "type mutation_root {\n  noop: Boolean\n}\n"
            "type Body {\n  text: String\n}\n"
            "type Widget {\n  id: ID!\n}\n"
        ),
        # Unrelated code that merely contains the words as identifiers/keys (would falsely couple
        # via api_type::query_root / api_type::Body if those minted).
        "rust/mod.rs": "pub mod query_root { pub fn x() {} }\n",
        "ts/server.ts": "function handler(req: { Body: string }) { return req.Body; }\n",
        "go/http.go": "func read(r *Request) { defer r.Body.Close() }\n",
        "js/resolvers.js": "const resolvers = { query_root: {}, Body: {}, mutation_root: {} };\n",
    })
    for forbidden in ("api_type::query_root", "api_type::mutation_root",
                      "api_type::subscription_root", "api_type::Body"):
        check(
            not _node_exists(g9, forbidden),
            f"(9) precision: stoplisted GraphQL name must NOT mint a node: {forbidden}; "
            f"api_type nodes: {list(_nodes_of_kind(g9, 'api_type'))}"
        )
        check(
            not _couples(g9, forbidden),
            f"(9) precision: stoplisted GraphQL name must NOT couple: {forbidden}; "
            f"files: {sorted(_files_touching(g9, forbidden))}"
        )
    # recall sanity: a real user type IS still minted (the stoplist did not over-suppress).
    check(_node_exists(g9, "api_type::Widget"),
          f"(9) sanity: user-defined 'Widget' should still mint; api_type nodes: "
          f"{list(_nodes_of_kind(g9, 'api_type'))}")
    # the GraphQL KEYWORD 'type' must never mint (it appears as a bare token in every schema).
    check(not _node_exists(g9, "api_type::type"),
          "(9) precision: GraphQL keyword 'type' must NEVER mint an api_type node")

    # -------------------------------------------------------------------------
    # (10) PRECISION (proto): a message named Stat must NOT couple a file using os.Stat, NOR a
    #      non-code (.sh) file that merely contains the message name as expected-output text.
    #      RECALL: a genuine .proto↔client crown jewel MUST still couple.
    # -------------------------------------------------------------------------
    g10 = _build({
        "proto/profiling.proto": (
            'syntax = "proto3";\n'
            "message Stat {\n  int64 ns = 1;\n}\n"
            "message Snapshot {\n  string id = 1;\n}\n"
            "service Profiler {\n  rpc Get(Snapshot) returns (Stat);\n}\n"
        ),
        # Unrelated Go code using the standard library os.Stat — NOT the proto message.
        "internal/files.go": (
            "package internal\n"
            "import \"os\"\n"
            "func size(p string) int64 { fi, _ := os.Stat(p); return fi.Size() }\n"
        ),
        # A shell script whose expected-output string happens to contain the message name. Non-code
        # files must be EXCLUDED from proto reference scanning (audit: examples_test.sh ↔ Feature).
        "examples/run_test.sh": (
            "#!/bin/sh\n"
            "echo 'expected output: Snapshot received, Stat written'\n"
        ),
        # The GENUINE crown jewel: a hand-written client referencing the service stub + message.
        "client/profiler_client.go": (
            "package client\n"
            "func Run(c ProfilerClient) {\n"
            "    snap := &Snapshot{Id: \"x\"}\n"
            "    c.Get(snap)\n"
            "}\n"
        ),
    })
    stat_id = "api_message::Stat"
    # Stat IS stoplisted now → it must not even mint (so it certainly cannot couple).
    check(not _node_exists(g10, stat_id),
          f"(10) precision: stoplisted proto name 'Stat' must NOT mint; "
          f"messages: {list(_nodes_of_kind(g10, 'api_message'))}")
    check(not _couples(g10, stat_id),
          f"(10) precision: 'Stat' (os.Stat / .sh text) must NOT couple; "
          f"files: {sorted(_files_touching(g10, stat_id))}")
    # The .sh file must contribute NO coupling on ANY api_* node (non-code-file exclusion).
    sh_couples = [n["id"] for n in g10["nodes"]
                  if str(n.get("kind", "")).startswith("api_")
                  and "run_test.sh" in _files_touching(g10, n["id"])]
    check(not sh_couples,
          f"(10) precision: non-code .sh file must not reference any proto symbol; "
          f"offending nodes: {sh_couples}")
    # RECALL: the genuine .proto definer ↔ client crown jewel still couples via Snapshot + service.
    snap_id = "api_message::Snapshot"
    check(_couples(g10, snap_id),
          f"(10) recall: genuine .proto<->client coupling on 'Snapshot' MUST survive; "
          f"files: {sorted(_files_touching(g10, snap_id))}")
    svc_id10 = "api_service::Profiler"
    check(_couples(g10, svc_id10),
          f"(10) recall: genuine .proto<->client coupling on service 'Profiler' MUST survive; "
          f"files: {sorted(_files_touching(g10, svc_id10))}")

    # -------------------------------------------------------------------------
    # (11) PRECISION (CLASS A multi-definer proto): the same `message X` defined in two .proto
    #      files is ambiguous and must NOT couple its definers.
    # -------------------------------------------------------------------------
    g11 = _build({
        "a/v1.proto": ('syntax = "proto3";\npackage a;\nmessage Shared {\n  string a = 1;\n}\n'),
        "b/v2.proto": ('syntax = "proto3";\npackage b;\nmessage Shared {\n  string b = 1;\n}\n'),
        "b/shared_client.go": ("package b\nfunc SharedRef() { x := &Shared{}; _ = x }\n"),
        # plus a uniquely-defined message that a client references (recall control).
        "c/uniq.proto": ('syntax = "proto3";\npackage c;\nmessage Uniq {\n  string u = 1;\n}\n'),
        "c/uniq_client.go": ("package c\nfunc R() { x := &Uniq{}; _ = x }\n"),
    })
    shared_id = "api_message::Shared"
    shared_node = _nodes_of_kind(g11, "api_message").get(shared_id)
    shared_edges = _edge_records_to(g11, shared_id)
    check(
        shared_node is not None
        and (shared_node.get("provenance") or {}).get("ambiguous") is True,
        "(11) ambiguous proto message must remain a first-class provenance-bearing node",
    )
    check(
        {e.get("src") for e in shared_edges}
        == {"a/v1.proto", "b/v2.proto", "b/shared_client.go"}
        and all(e.get("reference_status") == "ambiguous" for e in shared_edges),
        f"(11) all ambiguous proto definition/reference evidence must survive inert; edges={shared_edges}",
    )
    shared_file_statuses = {
        n.get("path"): n.get("analysis_status")
        for n in g11["nodes"]
        if n.get("kind") == "file"
        and n.get("path") in {e.get("src") for e in shared_edges}
    }
    check(
        set(shared_file_statuses) == {e.get("src") for e in shared_edges}
        and set(shared_file_statuses.values()) == {"ambiguous"},
        f"(11) ambiguous proto sources must be locally Unknown; statuses={shared_file_statuses}",
    )
    check(not _couples(g11, shared_id),
          f"(11) precision: multi-definer proto 'Shared' (2 .proto) must NOT couple; "
          f"files: {sorted(_files_touching(g11, shared_id))}")
    uniq_id = "api_message::Uniq"
    check(_couples(g11, uniq_id),
          f"(11) recall: single-definer proto 'Uniq' (proto + client) MUST still couple; "
          f"files: {sorted(_files_touching(g11, uniq_id))}")

    # -------------------------------------------------------------------------
    # Summary
    # -------------------------------------------------------------------------
    if failures:
        for f in failures:
            print(f"  FAIL: {f}")
        print("API-CONTRACT SUBSTRATE GATE: FAIL")
        return 1

    print("api-contract substrate: GraphQL type definition couples to anchored references "
          "(resolver map + gql template, crown-jewel, no code edge);")
    print("  precision: Query/Mutation/String/ID do NOT mint api_type nodes;")
    print("  protobuf: service+message definition couples to client stub/message references (no code edge);")
    print("  precision: unanchored prose word equal to a GraphQL type name does NOT couple;")
    print("  precision: a type/message DEFINED in >1 file (multi-definer) does NOT couple, "
          "but a single-definer type/message↔referencer crown-jewel still couples (recall);")
    print("  precision: Hasura root names (query_root/mutation_root) + cross-language collision "
          "words (Body) + GraphQL keywords (type) do NOT mint or couple;")
    print("  precision: proto reference scanning ignores non-code files (.sh/.md/...) and "
          "stoplists std-collision names (Stat=os.Stat), without dropping genuine .proto↔client;")
    print("  never-crash: empty + binary-ish .graphql/.proto do not raise;")
    print("  content-free: a secret in a comment never appears in graph output;")
    print("  additive: pure-Python repo gets zero api_* nodes (no regression).")
    print("API-CONTRACT SUBSTRATE GATE: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
