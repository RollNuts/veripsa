"""IaC (Infrastructure-as-Code) cross-substrate coupling extraction.

Owns the IAC GRAPH substrate: Terraform resource/data references + Kubernetes
label-selector and configmap/secret volume/env couplings.

WHY THIS MATTERS (the crown-jewel coupling code-only tools structurally miss):
Two IaC files that reference the SAME named resource — a Terraform `aws_s3_bucket.logs`
defined in one file and referenced in another, or a Kubernetes Deployment whose labels
match a Service's selector — are coupled with NO code edge, NO import, NO call. Neither
the call graph nor the import graph can see this coupling. This module surfaces it by
recovering the named-resource CONTRACT shared across files.

CONTENT-FREE: resource NAMES (type.name tuples, k8s kind::namespace::name identifiers)
only. Never resource bodies, never Terraform variable values, never Kubernetes Secret
data. A resource-type.name tuple or a kubernetes kind/namespace/name triple is pure
structural metadata.

PRECISION STRATEGY (two-pass, mirrors _cg_schema._schema_graph):
  Pass 1: collect the LOCALLY-DEFINED resource names → the known-resource set.
  Pass 2: scan every file for references to KNOWN names only.
  A reference that does not match a locally-defined resource is ignored — a random
  "type.name"-shaped token in a comment or string cannot mint a coupling.

TERRAFORM (primary, clean signal):
  - Node kind: `iac_resource`, id = `<type>.<name>` (e.g. `aws_s3_bucket.logs`).
  - alters edge: the file that DEFINES the resource block (`resource "type" "name" {}`)
  - queries edge: any other file that REFERENCES the known `type.name` tuple in an
    expression.
  - data sources: `data "type" "name" {}` is also a defining block; a reference to
    `data.type.name` (three-component) is also a cross-file coupling.

KUBERNETES (secondary, namespace-scoped):
  - A Service's `spec.selector` label-set couples to any Deployment/StatefulSet/Pod
    in the SAME namespace whose `metadata.labels` is a superset of the selector.
  - A Pod/Deployment/StatefulSet's `envFrom.configMapRef.name` or
    `envFrom.secretRef.name` (and `volumeMounts` via volumes[].configMap.name,
    volumes[].secret.secretName) couples to the ConfigMap/Secret that defines that name
    in the same namespace.
  - Node kind: `k8s_resource`, id = `<kind>::<namespace>::<name>`.
  - alters edge: the file that DEFINES the k8s resource.
  - queries edge: the file that REFERENCES the resource (selector match / configMapRef).
  - FP mitigation: namespace-scoped (never cross-namespace); label superset match only
    (exact OR subset of the Deployment's labels satisfies the selector); only files with
    `apiVersion` + `kind` are treated as k8s manifests (generic YAML goes to the config
    pass, not here).

ROUTING DISCIPLINE: a `.tf` file is routed ONLY to this module (Terraform path). A `.yaml`
or `.yml` file is a k8s manifest ONLY when it has both `apiVersion` and `kind` at the
top level; otherwise it passes through to the config graph unchanged (this module does NOT
consume it).  Pure-YAML routing is the caller's (build_graph's) responsibility — this
module ONLY reads files explicitly handed to it.
"""
from __future__ import annotations

import os
import re
from typing import Any

from _cg_io import _mark_incomplete, _read_capped

# ---------------------------------------------------------------------------
# Terraform: resource / data block definitions + references
# ---------------------------------------------------------------------------

# Match a Terraform `resource "type" "name" {` or `data "type" "name" {` block header.
# Groups: (1) "resource" or "data", (2) type string, (3) name string.
_TF_BLOCK_RE = re.compile(
    r'\b(resource|data)\s+"([A-Za-z_][A-Za-z0-9_]*)"\s+"([A-Za-z_][A-Za-z0-9_-]*)"\s*\{',
    re.I
)

# A Terraform reference is a two-component `type.name` token NOT preceded by a quote
# (i.e. not inside a string literal of the form `"type.name"` — those are HCL string
# expressions that CONTAIN a reference interpolated from ${type.name}, but the raw two-
# component form is fine). We use a word-boundary on the left to avoid matching
# `foo.aws_s3_bucket.logs` as `aws_s3_bucket.logs`. The type component must look like
# an underscore-prefixed provider resource type (`aws_s3_bucket`, `google_storage_bucket`,
# `azurerm_resource_group`, `data` for data sources). We restrict to the exact known-
# resource set in pass-2 (no standalone regex can mint new resources).
_TF_REF_RE = re.compile(
    r'(?<!["\w])([A-Za-z_][A-Za-z0-9_]*)\.([A-Za-z_][A-Za-z0-9_-]*)(?!\w)',
)

# ---------------------------------------------------------------------------
# Terraform: CROSS-MODULE references (recall fix, audit 2026-06-20)
# ---------------------------------------------------------------------------
# The multi-definer precision fix (#330) namespaces resource ids by the defining
# module DIRECTORY so the `aws_iam_role.this` idiom in two unrelated dirs no longer
# false-couples. The OPPOSITE failure that namespacing creates: a GENUINE cross-DIR
# reference now drops to silence. The canonical Terraform cross-dir contract is a
# ROOT/ENV module instantiating a CHILD module and consuming its OUTPUT:
#     module "vpc" { source = "./modules/vpc" }
#     resource "aws_instance" "app" { subnet_id = module.vpc.subnet_id }
# and modules/vpc/outputs.tf: output "subnet_id" { value = aws_subnet.this.id }
# Renaming the child's `output "subnet_id"` BREAKS the root — a real coupling with NO
# code edge that per-dir namespacing of `type.name` cannot see (the link is carried by
# the `module` instance + its `source` path, not by a shared `type.name`). We recover it
# by resolving the consumer's own `source = "<local path>"` to the child directory and
# coupling to the file(s) there that DEFINE the referenced `output`.
# Content-free: module-instance NAMES, output NAMES, resolved dir PATHS only.

# `module "NAME" {` block header. Group(1) = instance name.
_TF_MODULE_BLOCK_RE = re.compile(
    r'\bmodule\s+"([A-Za-z_][A-Za-z0-9_-]*)"\s*\{', re.I)

# `source = "<path>"` assignment (the first one inside a module block is the module source).
_TF_SOURCE_RE = re.compile(
    r'\bsource\s*=\s*"([^"]+)"')

# `output "NAME" {` block header (a child module exposes outputs; defining file = alters source).
_TF_OUTPUT_BLOCK_RE = re.compile(
    r'\boutput\s+"([A-Za-z_][A-Za-z0-9_-]*)"\s*\{', re.I)

# A cross-module reference `module.INSTANCE.OUTPUT` (consumer side; queries source).
_TF_MODULE_REF_RE = re.compile(
    r'(?<!["\w])module\.([A-Za-z_][A-Za-z0-9_-]*)\.([A-Za-z_][A-Za-z0-9_-]*)(?!\w)', re.I)

# Bound on distinct iac_resource nodes minted per repo. Real TF repos rarely exceed
# a few thousand resources; this ceiling guards against adversarial floods.
_MAX_IAC_RESOURCES = 20_000

# Terraform file extension.
_TF_EXT = ".tf"

# ---------------------------------------------------------------------------
# Kubernetes: YAML manifest coupling
# ---------------------------------------------------------------------------

# K8s resource kinds that can DEFINE label-based selector targets.
_K8S_WORKLOAD_KINDS = frozenset({
    "deployment", "statefulset", "pod", "replicaset", "daemonset", "job", "cronjob",
})
# K8s resource kinds that USE selectors to find their backend targets.
_K8S_SELECTOR_KINDS = frozenset({
    "service", "networkpolicy", "horizontalpodautoscaler",
})
# K8s resource kinds that can be REFERENCED by name via configMapRef / secretRef.
_K8S_NAMED_KINDS = frozenset({"configmap", "secret"})

# Bound on distinct k8s_resource nodes minted per repo.
_MAX_K8S_RESOURCES = 20_000

# YAML extensions (k8s manifests only — routing is by apiVersion+kind presence).
_K8S_EXTS = frozenset({".yaml", ".yml"})


# ---------------------------------------------------------------------------
# Terraform extraction — two passes
# ---------------------------------------------------------------------------

def _tf_defined_resources(text: str) -> dict[str, str]:
    """Pass 1: scan a .tf file for resource/data block definitions.
    Returns {canonical_id: normalized_ref_key} pairs:
      - `resource "aws_s3_bucket" "logs"` → {"aws_s3_bucket.logs": "aws_s3_bucket.logs"}
      - `data "aws_route53_zone" "this"` → {"data.aws_route53_zone.this": "data.aws_route53_zone.this"}
    Also maps the short two-component ref (type.name) for resource blocks so that
    `aws_s3_bucket.logs` in an expression matches regardless of which form the HCL uses.
    Content-free: type and name strings only.
    """
    defined: dict[str, str] = {}
    for m in _TF_BLOCK_RE.finditer(text):
        block_kind = m.group(1).lower()   # "resource" or "data"
        typ = m.group(2).lower()
        name = m.group(3).lower()
        if block_kind == "resource":
            # A resource block's canonical ref is `type.name`.
            canonical = f"{typ}.{name}"
        else:
            # A data block's canonical ref is `data.type.name`.
            canonical = f"data.{typ}.{name}"
        defined[canonical] = canonical
    return defined


def _tf_references(text: str, known: frozenset[str]) -> set[str]:
    """Pass 2: find all references to known resource IDs in a .tf file's text.
    Only matches IDs in the `known` set — no new IDs can be minted here.
    Content-free: ID strings only.
    """
    found: set[str] = set()
    for m in _TF_REF_RE.finditer(text):
        # Two-component match: `type.name`
        two = f"{m.group(1).lower()}.{m.group(2).lower()}"
        if two in known:
            found.add(two)
        # Three-component check: if the two-component match is "data.type", and
        # "data.type.name" is in known, emit it. This handles `data.aws_route53_zone.this`.
        # We do a second scan for three-component patterns from the full text.
    # Additional pass for three-component data references: `data.type.name`
    # Use a more specific pattern for data sources (three components).
    _data_ref = re.compile(
        r'(?<!["\w])(data)\.([A-Za-z_][A-Za-z0-9_]*)\.([A-Za-z_][A-Za-z0-9_-]*)(?!\w)', re.I)
    for m in _data_ref.finditer(text):
        three = f"data.{m.group(2).lower()}.{m.group(3).lower()}"
        if three in known:
            found.add(three)
    return found


def _tf_module_sources(text: str) -> dict[str, str]:
    """Parse `module "NAME" { ... source = "<path>" ... }` blocks → {instance_name: source}.

    Maps each module INSTANCE name to its declared `source`. We take the FIRST `source = "..."`
    that appears after a `module "NAME" {` header (Terraform requires `source` inside the block,
    and the first source in scope is the module's). Best-effort + bounded: a module without a
    parseable source is simply omitted (no edge can be minted for it — recall degrades to the
    prior silence for that one, never a crash). Content-free: instance names + source strings."""
    out: dict[str, str] = {}
    matches = list(_TF_MODULE_BLOCK_RE.finditer(text))
    for idx, m in enumerate(matches):
        name = m.group(1).lower()
        # Scan the window from this block header to the next module header (or EOF) for the
        # first `source = "..."`. This keeps each source bound to its own module instance.
        start = m.end()
        end = matches[idx + 1].start() if idx + 1 < len(matches) else len(text)
        sm = _TF_SOURCE_RE.search(text, start, end)
        if sm:
            out[name] = sm.group(1)
    return out


def _tf_local_source_dir(file_rel: str, source: str) -> str | None:
    """Resolve a module `source` to a repo-relative DIRECTORY, ONLY for LOCAL paths.

    Local sources are `./...` or `../...` (Terraform's local-module convention). Anything else
    (a registry ref `terraform-aws-modules/eks/aws`, a git/https/tarball URL) is NOT a path inside
    this repo → return None so it can NEVER false-couple to an unrelated repo dir. Resolution is
    relative to the directory of the REFERENCING file. Content-free: a path component only."""
    if not (source.startswith("./") or source.startswith("../")):
        return None
    import posixpath
    base_dir = _tf_module_dir(file_rel)
    resolved = posixpath.normpath(posixpath.join(base_dir, source))
    # normpath of a path that climbs above the repo root yields a leading "../" — reject it
    # (the target is outside the analyzed tree; we have no files there to couple to).
    if resolved.startswith("..") or resolved == ".":
        return None
    return resolved


def _tf_output_names(text: str) -> set[str]:
    """Parse `output "NAME" {}` block headers → the set of output names a file DEFINES.
    A child module's outputs are its public contract; the file defining an output is the
    `alters` source for the cross-module output node. Content-free: output names only."""
    return {m.group(1).lower() for m in _TF_OUTPUT_BLOCK_RE.finditer(text)}


def _tf_module_output_refs(text: str) -> set[tuple[str, str]]:
    """Parse `module.INSTANCE.OUTPUT` references → {(instance_name, output_name)}.
    The consumer side of a cross-module contract (the `queries` source). Content-free."""
    return {(m.group(1).lower(), m.group(2).lower()) for m in _TF_MODULE_REF_RE.finditer(text)}


def _tf_module_dir(rel: str) -> str:
    """The Terraform MODULE a .tf file belongs to = the directory containing it.

    A Terraform module is exactly one directory: all `.tf` files in the same dir share
    a namespace and a reference `type.name` resolves to the same-dir definition. Files
    in different dirs are different modules even if they define the same `type.name`
    (the `aws_iam_role.this` idiom). Returns the POSIX dirname ("" for repo root).
    Content-free: a path component only.
    """
    i = rel.rfind("/")
    return rel[:i] if i >= 0 else ""


def _tf_scoped_dst(module_dir: str, cid: str) -> str:
    """Module-namespaced canonical id used as the edge `dst` (the engine couples two
    files iff they emit a q/a/rc edge to the SAME dst). Namespacing by module dir means
    `aws_iam_role.this` in module A and module B are DIFFERENT dsts -> they do NOT couple
    (they are independent duplicate definitions, not a shared resource); but `main.tf` and
    `outputs.tf` in the SAME dir share the dst -> they still couple (recall preserved).
    Also closes a theoretical cross-substrate collision (a bare `type.name` colliding with
    a SQL table name) since the dst now carries a module path. Form: `<module_dir>::type.name`
    (or just `type.name` at repo root, preserving the prior flat id there)."""
    return f"{module_dir}::{cid}" if module_dir else cid


def _iac_graph_tf(
    root: str,
    tf_files: list[tuple[str, str]],
    incomplete_paths_out=None,
) -> tuple[list, list]:
    """Build Terraform cross-substrate nodes + edges.
    `tf_files`: list of (abs_path, rel_path) for .tf files (pre-filtered by caller).
    Returns (nodes, edges).

    MULTI-DEFINER PRECISION (audit 2026-06-20): a Terraform module is a single directory;
    a `type.name` reference is module-local. The same `type.name` DEFINED in two different
    module directories (the ubiquitous `aws_iam_role.this` idiom) is duplication, not a
    shared resource — so the resource node id is NAMESPACED by the defining module dir.
    Genuine shared-resource coupling (1-definer -> N-referencers within a module dir) is
    preserved; cross-module same-name false coupling is suppressed.
    """
    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []

    # --- Pass 1: collect defined resources PER MODULE DIR + which file defines them ---
    # scoped_id (module_dir::type.name) -> rel_path that first defines it.
    all_defined: dict[str, str] = {}
    # Keep every distinct definition site. Duplicate declarations in one module
    # make a local reference ambiguous (and Terraform itself rejects that
    # configuration), but the evidence must remain observable.
    definer_files: dict[str, set[str]] = {}
    # module_dir -> set of LOCAL canonical ids (type.name) defined in that module, so a
    # reference in a file resolves only against ITS OWN module's known set (TF refs are
    # module-local). This is what keeps `type.name` from coupling across modules.
    module_known: dict[str, set[str]] = {}
    file_texts: list[tuple[str, str, str]] = []   # (abs, rel, text)

    capped = False
    tf_read_loss = set()
    for abs_path, rel in tf_files:
        text = _read_capped(abs_path, tf_read_loss, rel)
        if text is None:
            continue
        file_texts.append((abs_path, rel, text))
        mod = _tf_module_dir(rel)
        defs = _tf_defined_resources(text)
        for cid in defs:
            scoped = _tf_scoped_dst(mod, cid)
            definer_files.setdefault(scoped, set()).add(rel)
            if scoped not in all_defined:
                if len(all_defined) >= _MAX_IAC_RESOURCES:
                    capped = True
                    _mark_incomplete(incomplete_paths_out, rel)
                    continue
                all_defined[scoped] = rel
            module_known.setdefault(mod, set()).add(cid)
    if capped or tf_read_loss:
        # Any Terraform file may reference a definition omitted by the global
        # catalog ceiling.  Keep the bounded evidence, but make the whole TF
        # candidate surface explicitly incomplete.
        for _abs_path, rel in tf_files:
            _mark_incomplete(incomplete_paths_out, rel)

    # Mint resource nodes (one per scoped ID; first-definer wins the path). The node
    # `name` keeps the bare canonical `type.name` (content-free, human-meaningful);
    # the `id`/edge-`dst` carry the module-dir namespace so they don't collide.
    resource_nodes: dict[str, dict] = {}
    for scoped, def_rel in all_defined.items():
        cid = scoped.split("::", 1)[1] if "::" in scoped else scoped
        n = {"id": f"iac_resource::{scoped}", "kind": "iac_resource",
             "name": cid, "path": def_rel, "language": "terraform"}
        if len(definer_files.get(scoped, ())) > 1:
            n["ambiguous"] = True
        resource_nodes[scoped] = n
        nodes.append(n)

    # --- Pass 2: emit alters (definer) + queries (reference) edges, module-scoped ---
    def _resource_edge(src: str, dst: str, kind: str) -> dict:
        edge = {"src": src, "dst": dst, "kind": kind}
        if len(definer_files.get(dst, ())) > 1:
            edge["reference_status"] = "ambiguous"
        return edge

    for _abs, rel, text in file_texts:
        mod = _tf_module_dir(rel)
        local_known = frozenset(module_known.get(mod, ()))
        # alters: every resource defined in this file (scoped to this module dir)
        defs_here = _tf_defined_resources(text)
        for cid in defs_here:
            scoped = _tf_scoped_dst(mod, cid)
            if scoped in resource_nodes:
                edges.append(_resource_edge(rel, scoped, "alters"))
        # queries: every resource REFERENCED in this file that is defined ELSEWHERE in
        # the SAME module (a module-local reference to another file's resource).
        refs = _tf_references(text, local_known)
        for cid in refs:
            scoped = _tf_scoped_dst(mod, cid)
            if cid not in defs_here and scoped in resource_nodes:
                edges.append(_resource_edge(rel, scoped, "queries"))

    # --- Pass 3: CROSS-MODULE output references (recall fix, audit 2026-06-20) ---
    # Recover the cross-DIRECTORY contract that per-dir namespacing of `type.name` drops:
    # a consumer's `module "X" { source = "./child" }` + `module.X.out` couples to the
    # file in ./child that DEFINES `output "out"`. This runs INDEPENDENTLY of the
    # `type.name` resource passes above (a root that only wires modules defines no
    # resources of its own, yet still genuinely depends on the child's outputs).
    _tf_cross_module_pass(
        file_texts, nodes, edges, incomplete_paths_out=incomplete_paths_out
    )

    return nodes, edges


def _tf_cross_module_pass(file_texts: list[tuple[str, str, str]],
                          nodes: list, edges: list,
                          incomplete_paths_out=None) -> None:
    """Emit cross-module output coupling edges (mutates `nodes`/`edges` in place).

    For each `.tf` file: resolve its `module "X" { source="<local>" }` instances to child
    directories, then for each `module.X.out` reference, couple to the file(s) in the child
    dir that define `output "out"`:
      - the child's output-defining file emits an `alters` edge to the output node,
      - the consuming file emits a `queries` edge to the same output node,
    so the two couple through the SHARED node exactly like every sibling cross-substrate
    detector (mirrors _cg_schema migration<->query).

    RECALL-SAFE & #330-PRESERVING: the coupling anchor is the consumer's OWN declared local
    `source` path resolved to a real child directory — NOT a coincidental shared name. Two
    UNRELATED modules that each define `output "id"` in different dirs never share a node
    (the node id is namespaced by the child dir + output name). The only files that couple to
    a child output node are (a) that child's own definer(s) and (b) consumers that explicitly
    instantiate THAT child via a local source — so an independent same-name output in another
    dir cannot be dragged in. Output nodes are bounded by the same _MAX_IAC_RESOURCES ceiling.
    """
    # dir -> {output_name -> [defining_rel_files]} (a child module's public contract).
    outputs_by_dir: dict[str, dict[str, list[str]]] = {}
    for _abs, rel, text in file_texts:
        d = _tf_module_dir(rel)
        names = _tf_output_names(text)
        if not names:
            continue
        bucket = outputs_by_dir.setdefault(d, {})
        for nm in names:
            bucket.setdefault(nm, []).append(rel)

    if not outputs_by_dir:
        return  # no child outputs anywhere → nothing to couple to

    out_nodes: dict[str, dict] = {}  # scoped output id -> node (dedup; bounded)
    output_cap_reached = False

    def _ensure_node(scoped: str, child_dir: str, output_name: str, def_rel: str) -> bool:
        nonlocal output_cap_reached
        if scoped in out_nodes:
            return True
        if len(out_nodes) >= _MAX_IAC_RESOURCES:
            output_cap_reached = True
            _mark_incomplete(incomplete_paths_out, def_rel)
            return False
        output_definers = outputs_by_dir[child_dir][output_name]
        ambiguous = len(set(output_definers)) > 1
        n = {"id": f"iac_resource::{scoped}", "kind": "iac_resource",
             "name": f"module-output {output_name}", "path": def_rel,
             "language": "terraform"}
        if ambiguous:
            n["ambiguous"] = True
        out_nodes[scoped] = n
        nodes.append(n)
        # alters edge(s): every file in the child dir that defines this output is a definer.
        for drel in output_definers:
            edge = {"src": drel, "dst": scoped, "kind": "alters"}
            if ambiguous:
                edge["reference_status"] = "ambiguous"
            edges.append(edge)
        return True

    for _abs, rel, text in file_texts:
        sources = _tf_module_sources(text)
        if not sources:
            continue
        refs = _tf_module_output_refs(text)
        if not refs:
            continue
        for inst, output_name in refs:
            src = sources.get(inst)
            if not src:
                continue  # reference to a module instance not declared in this file
            child_dir = _tf_local_source_dir(rel, src)
            if child_dir is None:
                continue  # remote/registry source → unresolvable, never false-couple
            dir_outputs = outputs_by_dir.get(child_dir)
            if not dir_outputs or output_name not in dir_outputs:
                continue  # the child dir does not actually define that output → no mint
            # Module-output node id namespaced by the CHILD dir + output name (cannot collide
            # with a `type.name` resource id, nor with another dir's same-named output).
            scoped = f"{child_dir}::output.{output_name}"
            # A consumer that lives IN the child dir referencing its own output would be intra-
            # dir (handled by the resource passes); skip self-couple to avoid a redundant edge.
            if _tf_module_dir(rel) == child_dir:
                continue
            if not _ensure_node(scoped, child_dir, output_name, dir_outputs[output_name][0]):
                continue
            edge = {"src": rel, "dst": scoped, "kind": "queries"}
            if len(set(dir_outputs[output_name])) > 1:
                edge["reference_status"] = "ambiguous"
            edges.append(edge)
    if output_cap_reached:
        for _abs, rel, _text in file_texts:
            _mark_incomplete(incomplete_paths_out, rel)


# ---------------------------------------------------------------------------
# Kubernetes extraction — YAML manifest parsing
# ---------------------------------------------------------------------------

def _looks_like_k8s_manifest_text(text: str) -> bool:
    """Cheap marker gate used only for diagnostics on YAML parser failure."""
    return bool(
        re.search(r"(?m)^\s*apiVersion\s*:\s*\S", text)
        and re.search(r"(?m)^\s*kind\s*:\s*\S", text)
    )


def _safe_yaml_load_all(
    text: str,
    incomplete_paths_out=None,
    relative_path: "str | None" = None,
) -> list[dict]:
    """Load all YAML documents from `text`. Returns a list of parsed dicts.
    Silently ignores parse errors (non-k8s YAML, malformed). Never-crash.
    Uses the explicitly declared PyYAML ``yaml.safe_load_all`` API. Returns []
    and records matching Kubernetes input as incomplete when PyYAML is absent."""
    try:
        import yaml  # pinned production dependency; import failure still degrades safely.
        docs = []
        try:
            for doc in yaml.safe_load_all(text):
                if isinstance(doc, dict):
                    docs.append(doc)
        except Exception:
            if _looks_like_k8s_manifest_text(text):
                _mark_incomplete(incomplete_paths_out, relative_path)
        return docs
    except ImportError:
        if _looks_like_k8s_manifest_text(text):
            _mark_incomplete(incomplete_paths_out, relative_path)
        return []


def _is_k8s_manifest(doc: dict) -> bool:
    """True when the YAML document looks like a k8s manifest (has apiVersion + kind)."""
    return bool(doc.get("apiVersion") and doc.get("kind"))


def _k8s_resource_id(kind: str, namespace: str, name: str) -> str:
    """Canonical k8s resource node id: `kind::namespace::name` (all lowercase)."""
    return f"{kind.lower()}::{namespace.lower()}::{name.lower()}"


def _k8s_labels(meta: dict) -> dict[str, str]:
    """Extract labels dict from a metadata block. Returns {} on missing/malformed."""
    labels = meta.get("labels") if isinstance(meta, dict) else None
    if not isinstance(labels, dict):
        return {}
    return {str(k): str(v) for k, v in labels.items() if isinstance(k, str) and isinstance(v, str)}


def _selector_matches(selector: dict[str, str], labels: dict[str, str]) -> bool:
    """True iff `selector` is a subset of `labels` (every selector key=value appears in labels)."""
    if not selector:
        return False
    return all(labels.get(k) == v for k, v in selector.items())


def _get_namespace(meta: dict) -> str:
    """Resource namespace from metadata; 'default' when absent."""
    ns = meta.get("namespace") if isinstance(meta, dict) else None
    return str(ns).lower() if isinstance(ns, str) and ns else "default"


def _pod_template_spec(spec: dict) -> dict | None:
    """Get the Pod template spec from a Deployment/StatefulSet/etc. spec block.
    Returns the inner template.spec dict, or None when absent."""
    if not isinstance(spec, dict):
        return None
    tmpl = spec.get("template")
    if not isinstance(tmpl, dict):
        return None
    tspec = tmpl.get("spec")
    return tspec if isinstance(tspec, dict) else None


def _collect_configmap_secret_refs(pod_spec: dict) -> list[tuple[str, str]]:
    """Collect all configmap/secret name references from a Pod spec's containers
    (envFrom, env.valueFrom, volumeMounts via volumes). Returns list of (kind, name)
    where kind ∈ {"configmap", "secret"} and name is the referenced name.
    Content-free: names only, never secret data / configmap values."""
    refs: list[tuple[str, str]] = []
    if not isinstance(pod_spec, dict):
        return refs

    containers_all = []
    for key in ("containers", "initContainers", "ephemeralContainers"):
        c = pod_spec.get(key)
        if isinstance(c, list):
            containers_all.extend(c)

    for c in containers_all:
        if not isinstance(c, dict):
            continue
        # envFrom: configMapRef.name / secretRef.name
        for ef in (c.get("envFrom") or []):
            if not isinstance(ef, dict):
                continue
            cmr = ef.get("configMapRef")
            if isinstance(cmr, dict) and cmr.get("name"):
                refs.append(("configmap", str(cmr["name"]).lower()))
            sr = ef.get("secretRef")
            if isinstance(sr, dict) and sr.get("name"):
                refs.append(("secret", str(sr["name"]).lower()))
        # env[*].valueFrom.configMapKeyRef / secretKeyRef
        for env in (c.get("env") or []):
            if not isinstance(env, dict):
                continue
            vf = env.get("valueFrom")
            if not isinstance(vf, dict):
                continue
            cmkr = vf.get("configMapKeyRef")
            if isinstance(cmkr, dict) and cmkr.get("name"):
                refs.append(("configmap", str(cmkr["name"]).lower()))
            skr = vf.get("secretKeyRef")
            if isinstance(skr, dict) and skr.get("name"):
                refs.append(("secret", str(skr["name"]).lower()))

    # volumes: configMap.name / secret.secretName
    for vol in (pod_spec.get("volumes") or []):
        if not isinstance(vol, dict):
            continue
        cm = vol.get("configMap")
        if isinstance(cm, dict) and cm.get("name"):
            refs.append(("configmap", str(cm["name"]).lower()))
        sec = vol.get("secret")
        if isinstance(sec, dict) and sec.get("secretName"):
            refs.append(("secret", str(sec["secretName"]).lower()))

    return refs


def _iac_graph_k8s(
    root: str,
    k8s_files: list[tuple[str, str]],
    incomplete_paths_out=None,
) -> tuple[list, list]:
    """Build Kubernetes cross-substrate nodes + edges.
    `k8s_files`: list of (abs_path, rel_path) for .yaml/.yml files (pre-filtered).
    Returns (nodes, edges).

    MULTI-DEFINER PRECISION (audit 2026-06-20): the same `(kind, namespace, name)` defined
    by MULTIPLE files is duplicate example manifests (the `kubernetes/examples` idiom: seven
    files each defining StorageClass `fast`, or `pod default/nginx` in four files), NOT one
    shared resource — so the engine must not couple those definer files to each other through
    that node. Two guards, mirroring the Terraform module-dir fix:
      (a) NAMESPACE the k8s_resource node by the manifest DIRECTORY (one app = one dir), so a
          `redis-replica` Deployment defined in app A and app B are DIFFERENT nodes — a Service
          in app A only couples to app A's Deployment, never to app B's duplicate.
      (b) Within a dir-scope, if a resource is still DEFINED (alters) by >1 file, those are
          duplicate ALTERNATIVES (e.g. `aws-ebs.yaml` + `gce-pd.yaml` both defining StorageClass
          `slow`): retain definitions/references with `reference_status=ambiguous`. Effective
          adjacency excludes those edges, while persistence retains why the source is Unknown.
          Genuine reference coupling is 1-definer -> N-referencers and stays unchanged.
    Selector / configMapRef resolution is scoped to the SAME directory (a selector resolves
    against same-app workloads, never a coincidental same-namespace match in an unrelated app).
    """
    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []

    # Per-directory (one app = one dir) structural metadata. Keys carry the directory so a
    # `(kind, ns, name)` in two different apps is two different resources (multi-definer fix).
    #   workloads: (dir, kind, ns, name) -> [{labels, rel, rid}, ...]
    #   selectors: list of {dir, kind, ns, name, selector, rel, rid}
    #   named:     (dir, kind, ns, name) -> rel   (ConfigMaps, Secrets, first definer)
    workloads: dict[tuple[str, str, str, str], list[dict]] = {}
    selectors: list[dict] = []
    named: dict[tuple[str, str, str, str], str] = {}
    resource_nodes: dict[str, dict] = {}   # scoped_rid -> node
    # definer_count[scoped_rid] = number of distinct files in the dir that DEFINE it. >1 ⇒
    # duplicate alternatives ⇒ explicit evidence, inert in cross-file coupling (guard (b)).
    definer_files: dict[str, set[str]] = {}

    # file_docs: list of (rel, dir, doc) for all parsed k8s docs
    file_docs: list[tuple[str, str, dict]] = []
    k8s_definition_loss = False

    for abs_path, rel in k8s_files:
        read_loss = set()
        text = _read_capped(abs_path, read_loss, rel)
        if text is None:
            # We cannot prove whether an unreadable YAML candidate was generic
            # config or a K8s definition. Record its own document and, if this
            # repo has confirmed manifests, conservatively taint that catalog.
            k8s_definition_loss = True
            _mark_incomplete(incomplete_paths_out, rel)
            continue
        is_k8s_candidate = _looks_like_k8s_manifest_text(text)
        # The apiVersion/kind marker pair may sit beyond the capped prefix.
        # Therefore truncation of any YAML candidate makes substrate routing
        # itself uncertain, even if the visible prefix looks like generic YAML.
        if read_loss:
            k8s_definition_loss = True
        parse_loss = set()
        docs = _safe_yaml_load_all(
            text,
            incomplete_paths_out=parse_loss if is_k8s_candidate else None,
            relative_path=rel,
        )
        if parse_loss:
            k8s_definition_loss = True
        rel_dir = _tf_module_dir(rel)   # directory = app scope (shared dirname helper)
        for doc in docs:
            if not _is_k8s_manifest(doc):
                continue
            file_docs.append((rel, rel_dir, doc))
    if k8s_definition_loss:
        for _abs_path, rel in k8s_files:
            _mark_incomplete(incomplete_paths_out, rel)

    if not file_docs:
        return nodes, edges

    def _scoped_rid(rel_dir: str, kind: str, ns: str, name: str) -> str:
        """Dir-namespaced canonical k8s id: `<dir>::kind::ns::name` (or bare at repo root).
        This is the edge `dst` / node id; two apps' duplicate resources get distinct ids."""
        bare = _k8s_resource_id(kind, ns, name)
        return f"{rel_dir}::{bare}" if rel_dir else bare

    # --- Pass 1: mint resource nodes + collect structural metadata + count definers ---
    k8s_cap_reached = False

    def _mint_k8s(rel_dir: str, kind: str, ns: str, name: str, rel: str) -> str | None:
        """Mint a dir-scoped k8s_resource node. Returns scoped id or None if over cap."""
        nonlocal k8s_cap_reached
        scoped = _scoped_rid(rel_dir, kind, ns, name)
        if scoped not in resource_nodes:
            if len(resource_nodes) >= _MAX_K8S_RESOURCES:
                k8s_cap_reached = True
                _mark_incomplete(incomplete_paths_out, rel)
                return None
            n = {"id": f"k8s_resource::{scoped}", "kind": "k8s_resource",
                 "name": f"{kind}/{ns}/{name}", "path": rel, "language": "kubernetes"}
            resource_nodes[scoped] = n
            nodes.append(n)
        return scoped

    for rel, rel_dir, doc in file_docs:
        kind_raw = str(doc.get("kind", "")).lower()
        meta = doc.get("metadata") or {}
        if not isinstance(meta, dict):
            meta = {}
        name = str(meta.get("name", "")).lower()
        ns = _get_namespace(meta)
        if not name:
            continue

        scoped = _mint_k8s(rel_dir, kind_raw, ns, name, rel)
        if scoped is None:
            continue
        definer_files.setdefault(scoped, set()).add(rel)

        if kind_raw in _K8S_WORKLOAD_KINDS:
            labels = _k8s_labels(meta)
            spec = doc.get("spec") or {}
            # For Deployments/StatefulSets/etc., the Pod template carries the labels
            # that Services select on.  Prefer template.metadata.labels over the
            # top-level metadata.labels (which is for the Deployment object itself).
            pod_tmpl = spec.get("template") if isinstance(spec, dict) else None
            pod_meta = pod_tmpl.get("metadata") if isinstance(pod_tmpl, dict) else None
            pod_labels = _k8s_labels(pod_meta) if isinstance(pod_meta, dict) else {}
            effective_labels = pod_labels or labels
            workloads.setdefault((rel_dir, kind_raw, ns, name), []).append({
                "labels": effective_labels, "rel": rel, "spec": spec, "rid": scoped,
            })

        elif kind_raw in _K8S_SELECTOR_KINDS:
            spec = doc.get("spec") or {}
            selector = spec.get("selector") if isinstance(spec, dict) else {}
            # For Services: spec.selector is the matchLabels dict directly.
            # For NetworkPolicy/HPA: spec.selector.matchLabels
            if isinstance(selector, dict):
                match_labels = selector.get("matchLabels") or selector
                if isinstance(match_labels, dict):
                    selectors.append({
                        "dir": rel_dir, "kind": kind_raw, "ns": ns, "name": name,
                        "selector": {str(k): str(v) for k, v in match_labels.items()
                                     if isinstance(k, str) and isinstance(v, str)},
                        "rel": rel, "rid": scoped,
                    })

        elif kind_raw in _K8S_NAMED_KINDS:
            named[(rel_dir, kind_raw, ns, name)] = rel
    if k8s_cap_reached:
        for rel, _rel_dir, _doc in file_docs:
            _mark_incomplete(incomplete_paths_out, rel)

    # A dir-scoped resource defined by >1 file = duplicate alternatives. Mark
    # its first-class node; edges below retain the evidence with an inert status.
    for scoped, paths in definer_files.items():
        if len(paths) > 1 and scoped in resource_nodes:
            resource_nodes[scoped]["ambiguous"] = True

    def _is_ambiguous(scoped: str) -> bool:
        return len(definer_files.get(scoped, ())) > 1

    def _k8s_edge(src: str, dst: str, kind: str) -> dict:
        edge = {"src": src, "dst": dst, "kind": kind}
        if _is_ambiguous(dst):
            edge["reference_status"] = "ambiguous"
        return edge

    # Preserve every definition edge. Duplicate-definer evidence is explicitly
    # inert so persistence/observability sees it while adjacency cannot couple it.
    for rel, rel_dir, doc in file_docs:
        kind_raw = str(doc.get("kind", "")).lower()
        meta = doc.get("metadata") or {}
        if not isinstance(meta, dict):
            meta = {}
        name = str(meta.get("name", "")).lower()
        ns = _get_namespace(meta)
        scoped = _scoped_rid(rel_dir, kind_raw, ns, name)
        if scoped in resource_nodes:
            edges.append(_k8s_edge(rel, scoped, "alters"))

    # --- Pass 2: selector matching → queries edges (SAME directory + namespace) ---
    # For each Selector (Service/NetworkPolicy/HPA), find matching Workloads in the SAME
    # directory (app) AND namespace whose labels are a superset of the selector.
    for sel in selectors:
        srel = sel["rel"]
        sns = sel["ns"]
        sdir = sel["dir"]
        for (wdir, wkind, wns, wname), candidates in workloads.items():
            if wdir != sdir:
                continue   # dir-scoped: a selector resolves only within its own app dir
            if wns != sns:
                continue   # namespace-scoped: never cross-namespace
            # Duplicate exact-identity manifests may disagree on labels. Preserve
            # a structurally anchored match if ANY candidate matches; the shared
            # resource id is already marked ambiguous and the edge becomes inert.
            matching = [
                candidate for candidate in candidates
                if _selector_matches(sel["selector"], candidate["labels"])
            ]
            if not matching:
                continue
            wrid = matching[0]["rid"]
            edges.append(_k8s_edge(srel, wrid, "queries"))

    # --- Pass 3: configMapRef / secretRef → queries edges (SAME directory + namespace) ---
    for rel, rel_dir, doc in file_docs:
        kind_raw = str(doc.get("kind", "")).lower()
        meta = doc.get("metadata") or {}
        if not isinstance(meta, dict):
            meta = {}
        ns = _get_namespace(meta)
        spec = doc.get("spec") or {}

        pod_spec = None
        if kind_raw == "pod":
            pod_spec = spec if isinstance(spec, dict) else None
        elif kind_raw in _K8S_WORKLOAD_KINDS:
            pod_spec = _pod_template_spec(spec)

        if pod_spec is None:
            continue

        for ref_kind, ref_name in _collect_configmap_secret_refs(pod_spec):
            key = (rel_dir, ref_kind, ns, ref_name)
            ref_rel = named.get(key)
            if ref_rel is None:
                continue   # precision: only couple to a same-dir LOCALLY-DEFINED named resource
            ref_scoped = _scoped_rid(rel_dir, ref_kind, ns, ref_name)
            if ref_scoped not in resource_nodes:
                continue
            edges.append(_k8s_edge(rel, ref_scoped, "queries"))

    return nodes, edges


# ---------------------------------------------------------------------------
# Public entry point — called by build_graph (mirrors _schema_graph wiring)
# ---------------------------------------------------------------------------

def _iac_graph(
    root: str,
    source_files: list[tuple[str, str]],
    incomplete_paths_out=None,
) -> tuple[list, list]:
    """Return (iac_nodes, iac_edges). Two substrates:

      - Terraform (.tf): resource/data block definitions → cross-file references via
        the canonical type.name tuple (two-pass, known-set precision).
      - Kubernetes (.yaml/.yml): label-selector coupling + configMapRef/secretRef
        coupling (namespace-scoped, YAML parsed with yaml.safe_load_all).

    `source_files`: the (abs_path, ext) list already filtered by build_graph's
    guards (size/binary/generated/symlink caps). Only .tf and .yaml/.yml files are
    consumed here; the rest are ignored.

    Content-free: resource NAMES (type.name, kind::namespace::name) only — never
    resource values, never Terraform variable values, never Secret data.
    Never-crash: all file reads and YAML parsing are guarded by try/except.
    """
    nodes: list = []
    edges: list = []

    tf_files: list[tuple[str, str]] = []
    k8s_candidates: list[tuple[str, str]] = []

    for abs_path, ext in source_files:
        rel = os.path.relpath(abs_path, root).replace(os.sep, "/")
        if ext == _TF_EXT:
            tf_files.append((abs_path, rel))
        elif ext in _K8S_EXTS:
            k8s_candidates.append((abs_path, rel))

    if tf_files:
        try:
            tf_nodes, tf_edges = _iac_graph_tf(
                root, tf_files, incomplete_paths_out=incomplete_paths_out
            )
            nodes.extend(tf_nodes)
            edges.extend(tf_edges)
        except Exception:
            for _abs_path, rel in tf_files:
                _mark_incomplete(incomplete_paths_out, rel)

    if k8s_candidates:
        try:
            k8s_nodes, k8s_edges = _iac_graph_k8s(
                root, k8s_candidates, incomplete_paths_out=incomplete_paths_out
            )
            nodes.extend(k8s_nodes)
            edges.extend(k8s_edges)
        except Exception:
            # The failing wrapper can no longer classify generic YAML from a
            # manifest whose marker lies beyond a capped/failed read.
            for _abs_path, rel in k8s_candidates:
                _mark_incomplete(incomplete_paths_out, rel)

    return nodes, edges
