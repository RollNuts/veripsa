"""Gate: EXTRACTOR DETERMINISM — the serialized code graph is BYTE-IDENTICAL across runs.

The code-graph extractor (`code_graph_extract.build_graph`) accumulates nodes and edges from
many per-file / per-substrate passes. Several of those passes collect table/symbol NAMES into a
Python `set` and then iterate that set DIRECTLY into node / edge emission (e.g.
`_cg_schema_orm._orm_table_refs` → `_cg_schema._schema_graph`'s `for t in orm_tables:` edge loop,
the `_cg_schema_go._go_*` struct/table fns, the SQL/creator ref fns, and the routes/config/iac/
openapi/api-contract passes). A bare `set`'s STRING iteration order is randomized by the process's
`PYTHONHASHSEED`, so two runs over the IDENTICAL source tree produced the SAME node/edge SET but a
DIFFERENT serialized ORDER — DIFFERENT BYTES. That made every downstream byte-sensitive consumer
(graph diffs / "what changed" surfaces / freshness fingerprints / change-detection) churn on every
push for NO real change — a whole invisible regression class (an entire phantom-diff every push),
and a latent flake source for any byte-level reproducibility check.

The fix is two-layered and CONTENT-FREE (it only fixes ORDER; the node/edge SET is unchanged):
  (1) every ref-producing extractor fn that returns/iterates a `set` now emits in a STABLE order
      (`return sorted(out)`), so the names reach emission in a fixed order; and
  (2) belt-and-suspenders: `build_graph` performs a final canonical sort of the assembled nodes
      (by id) and edges (by (src,dst,kind)) BEFORE return, so the OUTPUT is canonical regardless of
      any upstream insertion order.

This gate is the STRUCTURAL GUARD for that property. It writes ONE rich fixture tree that exercises
the set-backed paths across MULTIPLE substrates (many ORM models in one file, many gorm structs in
one Go file, raw .sql CREATE TABLEs, raw-SQL queriers) — enough distinct NAMES that an unsorted
set's iteration order genuinely differs between hash seeds — then runs the FULL extractor TWICE in
two CHILD processes with DIFFERENT `PYTHONHASHSEED` (0 and 1) and asserts the serialized graph JSON
is BYTE-IDENTICAL. It ALSO asserts the node/edge SET is identical across the two seeds (so the
canonicalization is proven to be a pure REORDER, never a content change), and that the output is in
fact in canonical order.

Running the extractor in a CHILD process per seed is REQUIRED: `PYTHONHASHSEED` is read once at
interpreter start and cannot be changed in-process, so the only honest cross-seed check spawns fresh
interpreters. (Pre-fix, this gate FAILS: the two seeds reorder the ORM/Go refs and the bytes differ
— verified by reverting the `sorted()`/canonical-sort changes. Post-fix it PASSES.)

Content-free throughout: only table/struct NAMES + file paths are read, never bodies or values.
Prints EXTRACTOR DETERMINISM GATE: PASS on success, ... FAIL on any failure.
"""
import os
import sys
import json
import shutil
import tempfile
import subprocess

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.join(_HERE, "..")
sys.path.insert(0, _ROOT)


# A spread of words long enough that the set's hash-seed-dependent iteration order genuinely
# differs between seeds (a 2-3 element set can coincidentally agree; a dozen will not).
_WORDS = ["zebra", "apple", "mango", "kiwi", "orange", "banana", "fig", "grape",
          "lemon", "cherry", "date", "plum", "peach", "melon", "berry", "quince",
          "guava", "papaya", "lime", "olive"]
_STRUCTS = ["Zebra", "Apple", "Mango", "Kiwi", "Orange", "Banana", "Fig", "Grape",
            "Lemon", "Cherry", "Date", "Plum", "Peach", "Melon", "Berry", "Quince"]


def _make_fixture():
    """Write ONE rich multi-substrate fixture tree to a temp dir; return its path.

    Designed to drive the set-backed extractor paths hard:
      - ONE ORM model file declaring MANY tables (`_orm_table_refs` set → `_schema_graph` edge loop)
      - ONE raw-SQL querier touching many of them (`_sql_query_edges`)
      - ONE Go gorm file with MANY structs (`_go_gorm_struct_names` set)
      - ONE .sql file with MANY CREATE TABLEs (`_sql_creator_refs` set + DDL alters)
    """
    d = tempfile.mkdtemp(prefix="extractor_determinism_")
    mdir = os.path.join(d, "models")
    os.makedirs(mdir, exist_ok=True)

    # Many ORM models in ONE file → _orm_table_refs returns a many-element set, iterated into edges.
    models = "\n".join(
        "class M{i}(Base):\n    __tablename__ = 'tbl_{w}'\n".format(i=i, w=w)
        for i, w in enumerate(_WORDS))
    with open(os.path.join(mdir, "all_models.py"), "w") as fh:
        fh.write(models)

    # A querier file with raw SQL touching many of the same tables.
    qs = "def q(c):\n" + "\n".join(
        "    c.execute('SELECT id FROM tbl_{w} WHERE id = 1')".format(w=w)
        for w in _WORDS[:10])
    with open(os.path.join(mdir, "dao.py"), "w") as fh:
        fh.write(qs + "\n")

    # Many gorm structs in ONE Go file → _go_gorm_struct_names returns a many-element set.
    go = "package main\n\nimport \"gorm.io/gorm\"\n\n" + "\n".join(
        "type {n} struct {{ gorm.Model; Name string }}\n".format(n=n)
        for n in _STRUCTS)
    with open(os.path.join(mdir, "go_models.go"), "w") as fh:
        fh.write(go)

    # Many raw CREATE TABLEs in ONE .sql file → _sql_creator_refs set + DDL alters edges.
    sql = "\n".join(
        "CREATE TABLE crt_{w} (id int PRIMARY KEY, name text);".format(w=w)
        for w in _WORDS)
    with open(os.path.join(mdir, "schema.sql"), "w") as fh:
        fh.write(sql + "\n")

    return d


# A tiny driver script: import the extractor, build the graph for the given root, and print the
# EXACT serialized payload the extractor would push (nodes/edges as the --push path serializes them)
# plus a stable digest of the node/edge SET (order-independent) so the parent can assert both
# byte-identity AND set-identity across seeds.
_DRIVER = r"""
import os, sys, json
sys.path.insert(0, sys.argv[1])
import code_graph_extract as X
g = X.build_graph(sys.argv[2])
payload = {
    "nodes": [{"id": n["id"], "kind": n["kind"], "path": n.get("path"),
               "name": n.get("name"), "language": n.get("language"),
               "start_line": n.get("start_line"), "end_line": n.get("end_line")}
              for n in g["nodes"]],
    "edges": [{"src": e["src"], "dst": e["dst"], "kind": e["kind"]} for e in g["edges"]],
}
# The serialized graph EXACTLY as main() emits it (indent=1, ensure_ascii=False) — the bytes a
# byte-sensitive consumer would see.
serialized = json.dumps(payload, ensure_ascii=False, indent=1)
# Order-INDEPENDENT view of the SET (so the parent can prove the fix is a pure reorder).
node_set = sorted(n["id"] for n in g["nodes"])
edge_set = sorted([e["src"], e["dst"], e["kind"]] for e in g["edges"])
out = {
    "serialized": serialized,
    "node_set": node_set,
    "edge_set": edge_set,
    "n_nodes": len(g["nodes"]),
    "n_edges": len(g["edges"]),
    # canonical-order self-check from inside the child:
    "nodes_sorted": [n["id"] for n in g["nodes"]] == sorted(n["id"] for n in g["nodes"]),
    "edges_sorted": ([(e["src"], e["dst"], e["kind"]) for e in g["edges"]]
                     == sorted((e["src"], e["dst"], e["kind"]) for e in g["edges"])),
}
sys.stdout.write("@@DETERMINISM@@" + json.dumps(out))
"""


def _run(root, seed):
    """Run the extractor in a CHILD interpreter pinned to PYTHONHASHSEED=seed. Returns the parsed
    driver dict."""
    env = dict(os.environ)
    env["PYTHONHASHSEED"] = str(seed)
    proc = subprocess.run([sys.executable, "-c", _DRIVER, _ROOT, root],
                          env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if proc.returncode != 0:
        raise RuntimeError(
            "extractor child (seed=%s) exited %d:\n%s"
            % (seed, proc.returncode, proc.stderr.decode("utf-8", "replace")))
    out = proc.stdout.decode("utf-8")
    marker = "@@DETERMINISM@@"
    idx = out.find(marker)
    if idx < 0:
        raise RuntimeError("extractor child (seed=%s) produced no payload:\n%s"
                           % (seed, out[-2000:]))
    return json.loads(out[idx + len(marker):])


def main():
    failures = []
    root = _make_fixture()
    try:
        # Two genuinely-different hash seeds. (If the runner pins PYTHONHASHSEED, these CHILD
        # processes still get two DIFFERENT seeds, which is the whole point.)
        a = _run(root, 0)
        b = _run(root, 1)

        # Sanity: the fixture must actually have produced a non-trivial graph, otherwise the
        # gate could pass vacuously (two empty graphs are trivially byte-identical).
        if a["n_nodes"] < 20 or a["n_edges"] < 10:
            print("FAIL [fixture too small]: graph has %d nodes / %d edges — the determinism check "
                  "would be vacuous. The fixture must exercise the set-backed paths."
                  % (a["n_nodes"], a["n_edges"]))
            failures.append("fixture-too-small")

        # (1) THE GUARD: the serialized graph JSON is BYTE-IDENTICAL across the two seeds.
        if a["serialized"] != b["serialized"]:
            la = a["serialized"].splitlines()
            lb = b["serialized"].splitlines()
            first = next((i for i, (x, y) in enumerate(zip(la, lb)) if x != y), None)
            detail = ""
            if first is not None:
                detail = ("\n  first divergence at line %d:\n    seed0: %s\n    seed1: %s"
                          % (first, la[first].strip(), lb[first].strip()))
            print("FAIL [byte-identity]: the serialized graph DIFFERS across PYTHONHASHSEED 0 vs 1 "
                  "(same SET, reordered BYTES — extractor non-determinism)." + detail)
            failures.append("not-byte-identical")
        else:
            print("ok byte-identity: serialized graph IDENTICAL across PYTHONHASHSEED 0 vs 1 "
                  "(%d bytes, %d nodes, %d edges)"
                  % (len(a["serialized"]), a["n_nodes"], a["n_edges"]))

        # (2) PURE REORDER: the node/edge SET is identical across seeds — the canonicalization
        #     changed only ORDER, never content. (Belt: proves the fix is content-free.)
        if a["node_set"] != b["node_set"]:
            print("FAIL [node-set drift]: the node SET differs across seeds — the fix must be a pure "
                  "reorder, not a content change.")
            failures.append("node-set-drift")
        if a["edge_set"] != b["edge_set"]:
            print("FAIL [edge-set drift]: the edge SET differs across seeds — the fix must be a pure "
                  "reorder, not a content change.")
            failures.append("edge-set-drift")
        if a["node_set"] == b["node_set"] and a["edge_set"] == b["edge_set"]:
            print("ok set-identity: node/edge SET identical across seeds (fix is a pure reorder)")

        # (3) CANONICAL ORDER: the emitted output is in fact sorted (the belt-and-suspenders
        #     canonical order in build_graph). Checked from inside each child.
        if not (a["nodes_sorted"] and a["edges_sorted"]):
            print("FAIL [canonical-order]: build_graph output is not in canonical (sorted) order "
                  "(nodes_sorted=%s, edges_sorted=%s)" % (a["nodes_sorted"], a["edges_sorted"]))
            failures.append("not-canonical-order")
        else:
            print("ok canonical-order: nodes sorted by id, edges sorted by (src,dst,kind)")

    finally:
        shutil.rmtree(root, ignore_errors=True)

    if failures:
        print("EXTRACTOR DETERMINISM GATE: FAIL (%s)" % ", ".join(failures))
        return 1
    print("EXTRACTOR DETERMINISM GATE: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
