"""Gate: IaC cross-MODULE reference RECALL (the OPPOSITE failure of the #330 precision fix).

The IaC detector (_cg_iac) namespaces Terraform resource ids by the defining module DIRECTORY
(#330) so the ubiquitous `aws_iam_role.this` idiom in two unrelated dirs no longer false-couples
(measured 64->18 pairs on terraform-aws-eks, 126->30 on kubernetes/examples). That precision fix
creates an OPPOSITE risk this gate guards against: a GENUINE cross-DIRECTORY Terraform contract
silently dropped. The canonical such contract is a ROOT/ENV module that instantiates a CHILD
module and consumes its OUTPUT:

    module "vpc" { source = "./modules/vpc" }
    resource "aws_instance" "app" { subnet_id = module.vpc.subnet_id }

with modules/vpc/outputs.tf: output "subnet_id" { value = aws_subnet.this.id }. Renaming the
child's `output "subnet_id"` BREAKS the root — a real coupling with NO code edge, NO import, NO
call (the link is carried by the `module` instance + its local `source` path, NOT a shared
`type.name`, so per-dir namespacing of `type.name` cannot see it). Before this fix the consumer
coupled to NOTHING; this gate pins the recall.

MEASURED on crafted repos (offline, no Postgres, content-free — only resource/output NAMES +
file paths + edge kinds are read):
  (1) RECALL CORE   — root `main.tf` referencing `module.vpc.<out>` couples to the child
                      `outputs.tf` that defines that output (the silent-miss the fix recovers).
  (2) RECALL NESTED — a deeply nested consumer `envs/prod/main.tf` with `source = "../../modules/net"`
                      couples to `modules/net/outputs.tf` (relative-path resolution across levels).
  (3) RECALL MAIN.TF DEFINER — the child's output may live in the child's `main.tf` (not a separate
                      outputs.tf); the consumer still couples to it.
  (4) PRECISION (#330 PRESERVED) — two UNRELATED modules each defining `output "id"` in different
                      dirs do NOT share a coupling node: a consumer of child A is NOT coupled to
                      child B's identically-named output (the node is namespaced by the child dir,
                      so a coincidental same-name output cannot drag in an unrelated dir).
  (5) PRECISION REMOTE — a `module { source = "terraform-aws-modules/eks/aws" }` (registry/remote,
                      not a local `./`/`../` path) is unresolvable to a repo dir and couples
                      NOTHING (it must never false-couple to an unrelated same-name local dir).
  (6) NEVER-CRASH    — malformed HCL (unterminated block, missing source, absolute-path source,
                      reference to an undeclared instance) degrades to no cross-module edges,
                      never raises.

Prints IAC RECALL GATE: PASS on success, ... FAIL on any failure.
"""
import sys
import os
import tempfile
import shutil

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import _cg_iac as IAC


def _write(root, path, body):
    fp = os.path.join(root, path)
    os.makedirs(os.path.dirname(fp), exist_ok=True)
    with open(fp, "w", encoding="utf-8") as fh:
        fh.write(body)


def _graph(files):
    """Write `files` (rel -> body) to a temp dir, run _iac_graph, return (root, nodes, edges).

    Calls the SAME entry point build_graph uses: _iac_graph(root, [(abs_path, ext), ...]).
    """
    root = tempfile.mkdtemp(prefix="iac_recall_")
    src = []
    for rel, body in files.items():
        _write(root, rel, body)
        ext = os.path.splitext(rel)[1].lower()
        src.append((os.path.join(root, rel), ext))
    nodes, edges = IAC._iac_graph(root, src)
    return root, nodes, edges


def _coupled_to(edges, f):
    """The set of files coupled to `f`: any file that shares an edge `dst` with `f`.

    Mirrors the engine's shared-resource adjacency (two files coupled iff they emit a
    q/a/rc edge to the SAME dst), the same coupling model the routes gate asserts on.
    """
    f = f.replace(os.sep, "/")
    by_dst = {}
    for e in edges:
        by_dst.setdefault(e["dst"], set()).add(e["src"].replace(os.sep, "/"))
    out = set()
    for srcs in by_dst.values():
        if f in srcs:
            out |= (srcs - {f})
    return out


def main():
    failures = []

    # -------------------------------------------------------------------------
    # (1) RECALL CORE: root module -> child module output (the silent-miss recovered).
    # -------------------------------------------------------------------------
    _root, _n, e1 = _graph({
        "main.tf": (
            'module "vpc" {\n  source = "./modules/vpc"\n}\n'
            'resource "aws_instance" "app" {\n'
            '  subnet_id = module.vpc.subnet_id\n}\n'
        ),
        "modules/vpc/main.tf": 'resource "aws_subnet" "this" {}\n',
        "modules/vpc/outputs.tf": 'output "subnet_id" {\n  value = aws_subnet.this.id\n}\n',
    })
    c1 = _coupled_to(e1, "main.tf")
    if "modules/vpc/outputs.tf" not in c1:
        print(f"FAIL [1 recall-core]: root main.tf must couple to the child outputs.tf it "
              f"references via module.vpc.subnet_id; coupled to {sorted(c1)!r}")
        failures.append("recall-core")
    shutil.rmtree(_root, ignore_errors=True)

    # -------------------------------------------------------------------------
    # (2) RECALL NESTED: a deeply-nested env module with `source = "../../modules/net"`.
    # -------------------------------------------------------------------------
    _root, _n, e2 = _graph({
        "envs/prod/main.tf": (
            'module "net" {\n  source = "../../modules/net"\n}\n'
            'resource "aws_x" "a" {\n  v = module.net.vpc_id\n}\n'
        ),
        "modules/net/main.tf": 'resource "aws_vpc" "this" {}\n',
        "modules/net/outputs.tf": 'output "vpc_id" {\n  value = aws_vpc.this.id\n}\n',
    })
    c2 = _coupled_to(e2, "envs/prod/main.tf")
    if "modules/net/outputs.tf" not in c2:
        print(f"FAIL [2 recall-nested]: nested consumer must couple across ../../ to "
              f"modules/net/outputs.tf; coupled to {sorted(c2)!r}")
        failures.append("recall-nested")
    shutil.rmtree(_root, ignore_errors=True)

    # -------------------------------------------------------------------------
    # (3) RECALL MAIN.TF DEFINER: the child output lives in the child's main.tf.
    # -------------------------------------------------------------------------
    _root, _n, e3 = _graph({
        "root.tf": 'module "db" {\n  source = "./db"\n}\nlocals {\n  e = module.db.endpoint\n}\n',
        "db/main.tf": (
            'resource "aws_db" "this" {}\n'
            'output "endpoint" {\n  value = aws_db.this.endpoint\n}\n'
        ),
    })
    c3 = _coupled_to(e3, "root.tf")
    if "db/main.tf" not in c3:
        print(f"FAIL [3 recall-main-definer]: consumer must couple to a child output defined "
              f"in the child's main.tf; coupled to {sorted(c3)!r}")
        failures.append("recall-main-definer")
    shutil.rmtree(_root, ignore_errors=True)

    # -------------------------------------------------------------------------
    # (4) PRECISION (#330 PRESERVED): two UNRELATED children each define `output "id"`.
    #     A consumer of child A must NOT couple to child B's identically-named output.
    # -------------------------------------------------------------------------
    _root, _n, e4 = _graph({
        "appA/main.tf": 'module "x" {\n  source = "../childA"\n}\nlocals { v = module.x.id }\n',
        "childA/outputs.tf": 'output "id" {\n  value = "a"\n}\n',
        "childB/outputs.tf": 'output "id" {\n  value = "b"\n}\n',
    })
    c4 = _coupled_to(e4, "appA/main.tf")
    if "childB/outputs.tf" in c4:
        print(f"FAIL [4 precision-#330]: consumer of childA must NOT couple to childB's "
              f"identically-named `output id` (different dir = different node); coupled to {sorted(c4)!r}")
        failures.append("precision-330-crossdir-output")
    # …and it MUST couple to the child it actually references (recall-safe, not over-suppressed).
    if "childA/outputs.tf" not in c4:
        print(f"FAIL [4 precision-#330 recall]: consumer must still couple to the child it "
              f"references (childA); coupled to {sorted(c4)!r}")
        failures.append("precision-330-lost-real")
    shutil.rmtree(_root, ignore_errors=True)

    # -------------------------------------------------------------------------
    # (5) PRECISION REMOTE: a registry/remote source is not a local dir → couples nothing
    #     (must never false-couple to a coincidental same-name local module dir).
    # -------------------------------------------------------------------------
    _root, _n, e5 = _graph({
        "main.tf": (
            'module "eks" {\n  source = "terraform-aws-modules/eks/aws"\n}\n'
            'locals { c = module.eks.cluster_id }\n'
        ),
        # a local module dir that coincidentally exposes `cluster_id` — must NOT be dragged in.
        "modules/eks/outputs.tf": 'output "cluster_id" {\n  value = "x"\n}\n',
    })
    c5 = _coupled_to(e5, "main.tf")
    if c5:
        print(f"FAIL [5 precision-remote]: a remote/registry module source must resolve to no "
              f"local dir and couple nothing; coupled to {sorted(c5)!r}")
        failures.append("precision-remote-source")
    shutil.rmtree(_root, ignore_errors=True)

    # -------------------------------------------------------------------------
    # (6) NEVER-CRASH: malformed HCL must degrade to no cross-module edges, never raise.
    # -------------------------------------------------------------------------
    malformed = [
        ("unterminated-module", 'module "x" {\n  source = "./y"\nlocals { v = module.x.out }\n'),
        ("missing-source", 'module "x" {\n  count = 2\n}\nlocals { v = module.x.out }\n'),
        ("absolute-source", 'module "x" {\n  source = "/etc/passwd"\n}\nlocals { v = module.x.out }\n'),
        ("undeclared-instance", 'locals { v = module.ghost.out }\n'),
        ("binary-bytes", '\x00\x01module "x" { source = "./y" }\xff locals { v = module.x.out }'),
    ]
    for label, body in malformed:
        try:
            _root, _n, _e = _graph({
                "a.tf": body,
                "y/out.tf": 'output "out" {\n  value = 1\n}\n',
            })
            shutil.rmtree(_root, ignore_errors=True)
        except Exception as ex:  # noqa: BLE001 — the property under test is "never raises"
            print(f"FAIL [6 never-crash:{label}]: cross-module parse raised {ex!r}")
            failures.append(f"never-crash-{label}")

    # -------------------------------------------------------------------------
    # Direct unit check of the resolver primitives (independent of the file walk).
    # -------------------------------------------------------------------------
    # Local source resolves relative to the referencing file's dir; remote → None.
    if IAC._tf_local_source_dir("envs/prod/main.tf", "../../modules/net") != "modules/net":
        print("FAIL [7 unit]: local source resolution ../../modules/net is wrong")
        failures.append("unit-local-resolve")
    if IAC._tf_local_source_dir("main.tf", "terraform-aws-modules/eks/aws") is not None:
        print("FAIL [7 unit]: a registry source must resolve to None (not a local dir)")
        failures.append("unit-remote-none")
    if IAC._tf_local_source_dir("main.tf", "/abs/path") is not None:
        print("FAIL [7 unit]: an absolute source must resolve to None")
        failures.append("unit-abs-none")
    srcs = IAC._tf_module_sources('module "a" { source = "./x" }\nmodule "b" { source = "../y" }\n')
    if srcs != {"a": "./x", "b": "../y"}:
        print(f"FAIL [7 unit]: module-source parse wrong; got {srcs!r}")
        failures.append("unit-module-sources")
    refs = IAC._tf_module_output_refs("v = module.a.out\nw = module.b.thing\n")
    if refs != {("a", "out"), ("b", "thing")}:
        print(f"FAIL [7 unit]: module-output-ref parse wrong; got {refs!r}")
        failures.append("unit-module-refs")

    if failures:
        print(f"IAC RECALL GATE: FAIL (failures: {failures})")
        sys.exit(1)
    print("IAC RECALL GATE: PASS")
    sys.exit(0)


if __name__ == "__main__":
    main()
