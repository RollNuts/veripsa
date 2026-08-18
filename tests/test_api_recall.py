"""Gate: API-contract / OpenAPI cross-substrate RECALL (coverage holes) + precision regression-lock.

The precision fixes #329 (openapi multi-definer suppression) and #332 (api-contract graphql/proto
multi-definer suppression + stoplists) removed a real FALSE-COUPLE class (a name DEFINED by >1 file
is ambiguous → keep it as inert evidence). This gate measures the OPPOSITE failure they could have introduced
(over-suppression → a GENUINE single-definer contract dropped) AND real cross-file contract shapes
that were never extracted at all (coverage holes). It locks the recall fixes below and keeps the
precision gains.

MEASURED on crafted temp repos (OFFLINE — no Postgres, no network, no PyYAML dependency). Coupling
is read exactly as the product reads it: two files COUPLE iff both carry an `alters`/`queries` edge
to the SAME api_* node (the shared-resource adjacency `res_adj` in db/schema/70_social.sql). We run
the real build_graph end-to-end so the test exercises the production wiring.

RECALL HOLES FIXED (each proven below):
  (R1) GraphQL `extend type X` — a type SPLIT across files via `extend` (schema stitching /
       federation: `type Product` in one file, `extend type Product` in another) is ONE contract,
       not two. Before the fix the `extend` was counted as a 2nd DEFINER → the multi-definer guard
       SUPPRESSED the whole type → the base, the extension, and EVERY referencer silently lost
       coupling. Fix: an `extend` emits a REFERENCE (queries) edge, not a DEFINER (alters) edge, so
       it never inflates the definer count. {base, extension} now couple; a genuine '2 bare `type X`
       in 2 files' case is STILL suppressed (R5 below).
  (R2) OpenAPI `${...}` template-literal path — a frontend builds a REST path with a JS/TS template
       literal (`fetch(`/orders/${id}`)`), the single most common frontend path form. Before the
       fix that normalized to `/orders/${}` and never matched the spec's `/orders/{}` → the
       spec↔frontend path coupling was silently dropped. Fix: `${...}` collapses to `{}` like
       `{id}`/`:id`.
  (R3) OpenAPI 3.1 `webhooks:` operations — a webhook operationId DEFINED in the spec and
       IMPLEMENTED by a backend handler is a real contract. Before the fix only `paths:` was parsed;
       `webhooks:` (a top-level sibling) was ignored → the webhook operationId↔handler coupling was
       silently dropped. Fix: webhook operationId-named operations are extracted (operationId only —
       a webhook key is an event name, not a URL path, so no bogus path anchor).

PRECISION REGRESSION-LOCKS (the #329/#332 gains must NOT be undone):
  (R4) single-definer crown jewel STILL couples (GraphQL `type Order`, proto `service OrderService`).
  (R5) genuine multi-definer STILL excluded from effective adjacency (the SAME bare `type X` /
       `message X` in TWO files remains first-class ambiguous evidence,
       AND `extend` must NOT rescue a genuinely-ambiguous 2-base-def type).

Content-free throughout: only api_* symbol NAMES + file paths are read. Prints
API RECALL GATE: PASS on success, ... FAIL on any failure.
"""
import sys
import os
import json
import tempfile
import shutil

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import code_graph_extract as X


_CONTRACT_KINDS = ("api_type", "api_message", "api_service", "api_operation", "api_schema")


def _write(root, path, body):
    fp = os.path.join(root, path)
    os.makedirs(os.path.dirname(fp), exist_ok=True)
    with open(fp, "w") as fh:
        fh.write(body)


def _coupled_pairs(edges):
    """Files coupled through a shared api_* node — the product's res_adj semantics: a file with an
    resolved alters OR queries edge to node N couples to every other file with a resolved
    alters/queries edge to N. Ambiguous/unresolved edges are persisted evidence but are
    intentionally inert in every effective-adjacency query."""
    by_node = {}
    for e in edges:
        if (
            e.get("kind") in ("alters", "queries")
            and e.get("reference_status") is None
        ):
            dst = e.get("dst", "")
            if isinstance(dst, str) and any(dst.startswith(k + "::") for k in _CONTRACT_KINDS):
                by_node.setdefault(dst, set()).add((e.get("src") or "").replace(os.sep, "/"))
    pairs = set()
    for files in by_node.values():
        fl = sorted(files)
        for i in range(len(fl)):
            for j in range(i + 1, len(fl)):
                pairs.add((fl[i], fl[j]))
    return pairs


def _coupled_to(pairs, f):
    f = f.replace(os.sep, "/")
    out = set()
    for a, b in pairs:
        if a == f:
            out.add(b)
        elif b == f:
            out.add(a)
    return out


def _scan(root):
    g = X.build_graph(root)
    return _coupled_pairs(g["edges"])


def main():
    failures = []
    root = tempfile.mkdtemp(prefix="api_recall_")
    try:
        # ---------------------------------------------------------------------------
        # (R1) GraphQL `extend type` — base + extension are ONE contract → must couple.
        # ---------------------------------------------------------------------------
        _write(root, "schema/base.graphql", "type Product {\n  id: ID!\n}\n")
        _write(root, "schema/ext.graphql", "extend type Product {\n  price: Float\n}\n")
        # A single-base type extended in TWO files (federation across 3 files) must keep coupling all.
        _write(root, "core/acct.graphql", "type Account {\n  id: ID!\n}\n")
        _write(root, "billing/acct.graphql", "extend type Account {\n  plan: String\n}\n")
        _write(root, "audit/acct.graphql", "extend type Account {\n  lastSeen: String\n}\n")

        # ---------------------------------------------------------------------------
        # (R4) single-definer crown jewels (GraphQL + proto) — recall regression-lock.
        # ---------------------------------------------------------------------------
        _write(root, "gqlschema/order.graphql", "type Order {\n  id: ID!\n  total: Float\n}\n")
        _write(root, "gqlsrv/resolvers.ts",
               "export const resolvers = { Order: { id: () => 1 } }\n")
        _write(root, "proto/order.proto",
               'syntax="proto3";\nmessage CreateOrderRequest { string id = 1; }\n'
               'service OrderService { rpc Create(CreateOrderRequest) returns (CreateOrderRequest); }\n')
        _write(root, "pclient/c.go",
               'package c\nfunc f(cli OrderServiceClient) { cli.Create(&CreateOrderRequest{}) }\n')

        # ---------------------------------------------------------------------------
        # (R5) genuine multi-definer — precision regression-lock (#332 gain must hold).
        #   - the SAME bare `input SampleInput` in TWO files (ambiguous) → suppressed.
        #   - TWO bare `type Thing` BASE defs (+ an extend) → still ambiguous → suppressed
        #     (an `extend` must NOT rescue a genuinely-multi-base type).
        #   - the SAME `message Dup` in TWO .proto files → suppressed.
        # ---------------------------------------------------------------------------
        _write(root, "svcA/a.graphql", "input SampleInput {\n  x: Int\n}\n")
        _write(root, "svcB/b.graphql", "input SampleInput {\n  y: Int\n}\n")
        _write(root, "svc1/t.graphql", "type Thing {\n  id: ID!\n}\n")
        _write(root, "svc2/t.graphql", "type Thing {\n  name: String\n}\n")
        _write(root, "svc3/t.graphql", "extend type Thing {\n  z: Int\n}\n")
        _write(root, "dup1/d.proto", 'message Dup { int32 a = 1; }\n')
        _write(root, "dup2/d.proto", 'message Dup { int32 b = 1; }\n')

        # ---------------------------------------------------------------------------
        # (R2)+(R3) OpenAPI: `${...}` path literal + 3.1 webhooks. JSON spec so the test is
        # PyYAML-INDEPENDENT (stdlib json always parses; prod may lack PyYAML).
        # ---------------------------------------------------------------------------
        spec = {
            "openapi": "3.1.0",
            "info": {"title": "t", "version": "1.0.0"},
            "paths": {"/orders/{id}": {"get": {"operationId": "getOrderById"}}},
            "webhooks": {"newOrder": {"post": {"operationId": "onNewOrder"}}},
        }
        _write(root, "api/openapi.json", json.dumps(spec))
        _write(root, "oasrv/handlers.py",
               "def getOrderById(id):\n    return id\ndef onNewOrder(payload):\n    return payload\n")
        # frontend builds the path with a JS template literal (the common case R2 fixes).
        _write(root, "oaweb/client.ts", "export const f = (id) => fetch(`/orders/${id}`)\n")

        pairs = _scan(root)

        # --- (R1) extend type ---
        prod = _coupled_to(pairs, "schema/base.graphql")
        if "schema/ext.graphql" not in prod:
            print(f"FAIL [R1 extend-type]: a base `type Product` and `extend type Product` must couple "
                  f"(same contract split across files); schema/base.graphql coupled to {sorted(prod)!r}")
            failures.append("extend-type-recall")
        acct = _coupled_to(pairs, "core/acct.graphql")
        if not {"billing/acct.graphql", "audit/acct.graphql"} <= acct:
            print(f"FAIL [R1 extend-fanout]: a base + 2 extensions must all couple; core/acct.graphql "
                  f"coupled to {sorted(acct)!r}")
            failures.append("extend-fanout-recall")

        # --- (R4) single-definer crown jewels ---
        order = _coupled_to(pairs, "gqlschema/order.graphql")
        if "gqlsrv/resolvers.ts" not in order:
            print(f"FAIL [R4 gql single-definer]: single-definer `type Order` must couple to its "
                  f"resolver; got {sorted(order)!r}")
            failures.append("gql-single-definer-recall")
        oproto = _coupled_to(pairs, "proto/order.proto")
        if "pclient/c.go" not in oproto:
            print(f"FAIL [R4 proto single-definer]: single-definer `service OrderService` must couple "
                  f"to its client; got {sorted(oproto)!r}")
            failures.append("proto-single-definer-recall")

        # --- (R5) genuine multi-definer suppression (precision lock) ---
        si = _coupled_to(pairs, "svcA/a.graphql")
        if si:
            print(f"FAIL [R5 gql multi-definer]: the SAME `input SampleInput` in 2 files is ambiguous "
                  f"and must be SUPPRESSED; svcA/a.graphql coupled to {sorted(si)!r}")
            failures.append("gql-multi-definer-precision")
        thing = _coupled_to(pairs, "svc1/t.graphql")
        if thing:
            print(f"FAIL [R5 extend-no-rescue]: TWO bare `type Thing` base defs are ambiguous even with "
                  f"an `extend`; svc1/t.graphql must be SUPPRESSED, coupled to {sorted(thing)!r}")
            failures.append("extend-no-rescue-precision")
        dup = _coupled_to(pairs, "dup1/d.proto")
        if dup:
            print(f"FAIL [R5 proto multi-definer]: the SAME `message Dup` in 2 .proto files is ambiguous "
                  f"and must be SUPPRESSED; dup1/d.proto coupled to {sorted(dup)!r}")
            failures.append("proto-multi-definer-precision")

        # --- (R2) ${...} template-literal path ---
        web = _coupled_to(pairs, "oaweb/client.ts")
        if "api/openapi.json" not in web:
            print(f"FAIL [R2 template-path]: a frontend `fetch(`/orders/${{id}}`)` template literal must "
                  f"couple to the spec's `/orders/{{id}}` operation; oaweb/client.ts coupled to {sorted(web)!r}")
            failures.append("template-literal-path-recall")

        # --- (R3) 3.1 webhook operationId ---
        srv = _coupled_to(pairs, "oasrv/handlers.py")
        if "api/openapi.json" not in srv:
            print(f"FAIL [R3 webhook-op]: a webhook operationId `onNewOrder` defined in the spec must "
                  f"couple to its handler; oasrv/handlers.py coupled to {sorted(srv)!r}")
            failures.append("webhook-operation-recall")
        # The webhook coupling specifically (handler ↔ spec) — independent of the path-op coupling.
        # handlers.py implements BOTH getOrderById (path op) and onNewOrder (webhook op); either edge
        # makes it couple to the spec, so confirm the webhook node exists end-to-end via build_graph.
        g = X.build_graph(root)
        wh_nodes = [n for n in g["nodes"] if n.get("id") == "api_operation::onNewOrder"]
        if not wh_nodes:
            print("FAIL [R3 webhook-node]: the webhook operation `api_operation::onNewOrder` must MINT "
                  "a node (webhooks: block parsed); none found.")
            failures.append("webhook-node-mint")

    finally:
        shutil.rmtree(root, ignore_errors=True)

    if failures:
        print(f"API RECALL GATE: FAIL (failures: {failures})")
        sys.exit(1)
    print("API RECALL GATE: PASS")
    sys.exit(0)


if __name__ == "__main__":
    main()
