"""Python (stdlib `ast`) per-file extractor for the multi-language code graph.

Extracted from code_graph_extract as a leaf module — same discipline as the existing per-language
modules (`_cg_languages` for the tree-sitter family, `_cg_schema` / `_cg_config` / `_cg_iac` / …
for the substrate passes). Callers are `code_graph_extract._extract_source_files` (dispatch) +
the test suite; nothing in this file calls back into the extractor orchestrator.

Surface (re-exported by `code_graph_extract` for back-compat with `import code_graph_extract as X`):
  extract_file_py(path, rel)        -> (nodes, edges, ok)   the per-file Python AST walk
  _py_call_name(func)               -> str | None           callee NAME of a Call node
  _py_base_names(classdef)          -> list[str]            bare base-class NAMES of a ClassDef
  _py_decorator_names(node)         -> list[str]            bare decorator NAMES of a decorated def/class
  _py_signature_shape(funcdef)      -> dict                 content-free signature shape of a def/async-def

Content-free throughout: only symbol NAMES + line spans (metadata) ever leave this module; file
bodies are read for parsing and immediately dropped. Never raises on a syntax/encoding error — a
bad file degrades to a bare file node, never aborts the build.

The orchestrator-owned dependencies (`_read_source_text`, the per-file `_PER_FILE_SYMBOL_CAP` /
`_PER_FILE_EDGE_CAP`) are pulled from code_graph_extract via a DEFERRED import inside the function
— code_graph_extract re-exports this module's surface at its top, so a top-level `from
code_graph_extract import …` here would form an import cycle (X imports _cg_python imports X).
The cycle is broken by the function-local import: by the time extract_file_py runs, the
orchestrator's module body has finished executing and the symbols exist.
"""
import ast
import hashlib


# --------------------------------------------------------------------------------------------
# Python — stdlib ast (no third-party dep).
# --------------------------------------------------------------------------------------------
def extract_file_py(path, rel):
    """Return (nodes, edges, ok) for one Python file. Never raises on a syntax error."""
    # Deferred import to break the (X -> _cg_python -> X) cycle introduced by the split: by the time this
    # function actually runs, code_graph_extract's module body has finished executing and the symbols are
    # bound. Import cost is ~one dict lookup per call after the first; sys.modules caches the module.
    from code_graph_extract import (
        _read_source_text,
        _PER_FILE_SYMBOL_CAP,
        _PER_FILE_EDGE_CAP,
    )
    nodes = [{"id": rel, "kind": "file", "path": rel, "language": "python"}]
    edges = []
    try:
        # i18n: read in the file's DECLARED encoding (PEP-263 cookie: shift_jis / latin-1 / gb18030 …),
        # strip a UTF-8/UTF-16 BOM, and never raise on a bad encoding — so a non-UTF-8 Python file is
        # PARSED (symbols recovered), not silently reduced to a bare file node with every symbol dropped.
        tree = ast.parse(_read_source_text(path), filename=path)
    except (SyntaxError, UnicodeDecodeError, ValueError, RecursionError):
        # RecursionError: ast.parse hits a C-recursion guard on a deeply-nested input (py3.12+ raises it
        # rather than segfaulting). Caught here so a deeply-nested file degrades to a bare file node, never
        # crashes the ingest. (build_graph's outer except Exception is a second net.)
        return nodes, edges, False
    for node in ast.walk(tree):
        # PER-FILE SYMBOL/EDGE CAP (audit:dos): bail out of the walk the moment this single file's node/edge
        # output exceeds the cap — a pathological file (e.g. ~150k one-line defs under the 1.5 MB size cap)
        # is bounded HERE so the transient node-list/edge-list never grows unbounded. Degrade to a bare file
        # node (honest file-level), never balloon memory/time. build_graph applies the same cap as the
        # uniform net for the other languages.
        if len(nodes) > _PER_FILE_SYMBOL_CAP or len(edges) > _PER_FILE_EDGE_CAP:
            return [{"id": rel, "kind": "file", "path": rel, "language": "python"}], [], True
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            kind = "class" if isinstance(node, ast.ClassDef) else "def"
            sym = f"{rel}::{node.name}"
            # CONTENT-FREE LINE SPAN (finer-collision): a symbol's [start_line, end_line] is pure metadata
            # (line numbers, never code). ast gives 1-based lineno + end_lineno (end_lineno is None on
            # ancient pythons / odd nodes → omit, stays back-compatible). Two PRs editing DISJOINT spans of
            # one file do NOT collide; the same span does. Optional: a node without a span resolves at the
            # file level (the safety net), never a missed collision.
            sl, el = getattr(node, "lineno", None), getattr(node, "end_lineno", None)
            # DECORATOR LINES belong to the symbol's span: ast's `.lineno` points at the `def`/`class`
            # keyword, NOT the first `@decorator` line, so a PR touching ONLY a `@decorator` (e.g. flipping
            # `@app.route(...)` or `@pytest.mark...`) fell OUTSIDE the recorded span → a missed finer-
            # collision. Start the span at the earliest decorator line when the node is decorated (content-
            # free: still just line numbers). end_lineno already covers the body.
            if isinstance(sl, int):
                dlines = [getattr(d, "lineno", None) for d in getattr(node, "decorator_list", []) or []]
                dlines = [x for x in dlines if isinstance(x, int)]
                if dlines:
                    sl = min(sl, min(dlines))
            n = {"id": sym, "kind": kind, "name": node.name, "path": rel, "language": "python"}
            if isinstance(sl, int) and isinstance(el, int) and el >= sl:
                n["start_line"], n["end_line"] = sl, el
            # CONTENT-FREE SIGNATURE SHAPE (compatibility-fact foundation): a `def`/method carries the
            # normalized shape of its public signature — parameter NAMES, required/optional arity, varargs/
            # kwargs flags, keyword-only names, and a stable hash fingerprint. NEVER a default-value
            # expression, NEVER annotation source (only names + counts + flags + a hash leave this module).
            # Optional/additive: a class gets nothing new; a shape failure degrades to a bare node (the
            # helper is wrapped so it can never raise into the walk). This is the one field the compatibility
            # rules compare across two PRs to decide who lands first.
            if kind == "def":
                try:
                    n.update(_py_signature_shape(node))
                except Exception:
                    pass  # never-crash: a malformed/exotic signature degrades to a node without the shape
            nodes.append(n)
            edges.append({"src": rel, "dst": sym, "kind": "contains"})
            # SYMBOL-USE — base classes (recall, measure-first, this lane). A Python `class Sub(Base):`
            # DEPENDS ON the file that defines `Base`, but the call graph misses it: `Base` appears in the
            # ClassDef's `.bases` as a Name/Attribute, NOT an ast.Call, so no `calls` edge was emitted. This
            # is the SAME high-precision `sym_use: base` the generic tree-sitter spec already gives Java/C#/
            # Rust/PHP/C++/Swift (a superclass/interface is a SPECIFIC, rarely-ubiquitous name → measured
            # 87–94% co-change there); Python (the bespoke ast path) was the one language never given it.
            # We emit the base as a `calls` edge whose dst is the BARE base name (`Base` for `pkg.Base` /
            # `Base[T]` — content-free, same shape every call dst already carries), so it flows through the
            # EXACT same _claim_adjacency resolution every call passes: ≤3-definer fan-out cap, import-
            # confirmation, single-definer rule, hub dampening, prod→test guard. We never resolve a base
            # ourselves — the engine's proven precision guards decide, so a ubiquitous/unresolvable base
            # (`object`, an un-imported multi-definer name) is dropped, never fanned out to a decoy.
            if isinstance(node, ast.ClassDef):
                for bname in _py_base_names(node):
                    edges.append({"src": rel, "dst": bname, "kind": "calls"})
            # SYMBOL-USE — BARE decorators (recall, measure-first, this lane). A `@setupmethod` decorator
            # (a plain Name/Attribute, NOT a call) wraps the function and DEPENDS ON the file defining the
            # decorator — editing the decorator's contract ripples to every decorated symbol. The call graph
            # MISSED only the BARE form: a CALLED decorator `@app.route(...)` is an ast.Call already walked
            # above (emits `route`), but a bare `@setupmethod` is an ast.Name that produced NO edge. MEASURED
            # on pallets/flask: every NET-NEW pair was a REAL internal decorator coupling the call graph
            # silently CLEARED (e.g. sansio/app.py + sansio/blueprints.py each apply `@setupmethod`, imported
            # single-definer from sansio/scaffold.py — an import-confirmed link the call graph never had).
            # Like a base class, a decorator name is SPECIFIC and rarely ubiquitous (this is the same
            # high-precision `sym_use` family). We emit it as a CONTENT-FREE bare-name `calls` edge (`route`
            # for `@app.route`, same shape every call dst carries) so it flows through the EXACT same
            # _claim_adjacency resolution every call passes: ≤3-definer fan-out cap, import-confirmation,
            # single-definer rule, hub dampening, prod→test guard. We never resolve it ourselves — a builtin
            # bare decorator (`@property`, `@staticmethod`) or an un-imported multi-definer name is DROPPED by
            # those proven guards, never fanned out to a decoy. (A CALLED decorator is skipped here to avoid
            # double-emitting the name the ast.Call branch already carries.)
            for dname in _py_decorator_names(node):
                edges.append({"src": rel, "dst": dname, "kind": "calls"})
        elif isinstance(node, ast.Call):
            name = _py_call_name(node.func)
            if name:
                edges.append({"src": rel, "dst": name, "kind": "calls"})
        elif isinstance(node, ast.Import):
            for a in node.names:
                edges.append({"src": rel, "dst": a.name, "kind": "imports"})
        elif isinstance(node, ast.ImportFrom):
            base = node.module  # the FROM module ('shared' in `from shared import x`; None for `from . import x`)
            level = node.level or 0
            if level:
                # RELATIVE import (`from .app import X`, `from ..pkg import y`): resolve EXACTLY against the
                # importer's package directory, so `.app` names the SIBLING app.py — not EVERY app.py in the
                # repo. (Dropping `level` made `.app` a bare `app` that fanned out to test apps etc. → false
                # cross-package couplings = over-warning. Real-repo audit on flask: 12 false src→tests edges.)
                # We emit a './'/'../' path the resolver resolves against dirname(src) (level 1 = current
                # package = dirname; each extra level climbs one package).
                prefix = "./" if level == 1 else "../" * (level - 1)
                basepath = prefix + (base.replace(".", "/") if base else "")
                for a in node.names:
                    tgt = (basepath.rstrip("/") + "/" + a.name) if base else (prefix + a.name)
                    edges.append({
                        "src": rel,
                        "dst": tgt,
                        "kind": "imports",
                        # Resolver-only evidence: this coordinate may be a
                        # submodule file OR merely a member of the base module.
                        # _emit_resolution strips the marker before graph
                        # assembly/persistence/hash.
                        "resolution_probe": "member",
                    })
                if base:
                    edges.append({"src": rel, "dst": basepath, "kind": "imports"})  # the from-module file/package
            else:
                for a in node.names:
                    # `from pkg import name`: `name` is often a SUBMODULE/file — record pkg.name so the resolver
                    # can reach the actual FILE (the bare module alone loses which file was imported).
                    edges.append({
                        "src": rel,
                        "dst": (
                            (base + "." + a.name)
                            if base
                            else a.name
                        ),
                        "kind": "imports",
                        "resolution_probe": "member",
                    })
                if base:
                    edges.append({"src": rel, "dst": base, "kind": "imports"})
    return nodes, edges, True


def _py_call_name(func):
    """The callee name of a Call node: `f()` -> 'f', `m.f()` -> 'f' (attribute tail)."""
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _py_base_names(classdef):
    """The bare base-class NAMES of a `class Sub(...)`: `Base` -> 'Base', `pkg.Base` -> 'Base'
    (attribute tail), `Generic[T]` / `Protocol[T]` -> the SUBSCRIPTED name ('Generic'). Returns a
    de-duplicated, content-free list (names only, never the file body). Skips:
      • dunder bases (`__class_getitem__`-style metaclass plumbing — non-coupling noise),
      • keyword bases (`class M(metaclass=ABCMeta)` — a metaclass kwarg lands in `keywords`, not
        `bases`, so it is already excluded; we only read `bases`).
    A subscript base (`class P(Protocol[T])`) carries its type in `.value` — read the trailing name so
    a generic base resolves to the same bare symbol a plain base does. We deliberately do NOT recurse
    into subscript SLICES (the type ARGUMENTS `[T]`) — those are not the inherited type."""
    out, seen = [], set()
    for b in getattr(classdef, "bases", ()) or ():
        nm = None
        if isinstance(b, ast.Subscript):   # Generic[T] / Protocol[T] — the inherited type is the value
            b = b.value
        if isinstance(b, ast.Name):
            nm = b.id
        elif isinstance(b, ast.Attribute):
            nm = b.attr
        if nm and not nm.startswith("__") and nm not in seen:
            seen.add(nm)
            out.append(nm)
    return out


def _py_decorator_names(node):
    """The bare decorator NAMES of a `@dec`-decorated def/class: `@setupmethod` -> 'setupmethod',
    `@app.route` -> 'route' (attribute tail), de-duplicated and content-free (names only, never a body).
    DELIBERATELY skips a CALLED decorator (`@app.route(...)`, `@functools.wraps(f)`): that decorator node
    is an ast.Call which `ast.walk` already visits, so the ast.Call branch ALREADY emits its name — re-
    emitting here would double-count. We capture only the BARE Name/Attribute form, the one the call graph
    missed. Dunder decorators are skipped (plumbing noise, mirroring _py_base_names)."""
    out, seen = [], set()
    for d in getattr(node, "decorator_list", ()) or ():
        if isinstance(d, ast.Call):
            continue  # already emitted by the ast.Call walk — never double-emit
        nm = None
        if isinstance(d, ast.Name):
            nm = d.id
        elif isinstance(d, ast.Attribute):
            nm = d.attr
        if nm and not nm.startswith("__") and nm not in seen:
            seen.add(nm)
            out.append(nm)
    return out


def _py_signature_shape(funcdef):
    """Content-free shape of a `def` / `async def` public signature — the foundation the
    compatibility rules compare across two PRs.

    Returns a dict of ONLY structural identifiers (merged into the def node, all fields optional):
      param_names    list[str]  positional-only + positional-or-keyword + keyword-only NAMES, in order
      required_arity int        params with NO default (excludes *args / **kwargs)
      optional_arity int        params WITH a default (excludes *args / **kwargs)
      has_varargs    bool       a `*args` is present
      has_kwargs     bool       a `**kwargs` is present
      kwonly_names   list[str]  keyword-only param NAMES, in order
      shape_fingerprint str     sha1 hex (truncated) over a normalized tuple of the ordered param
                                structure (names + required/optional marker per param + varargs/kwargs
                                flags). STABLE across runs; CHANGES when a required arg is added/removed.

    CONTENT-FREE, non-negotiable: never reads a default-value EXPRESSION or an annotation SOURCE — only
    parameter names, per-param required/optional flags, arity counts, and the varargs/kwargs presence
    flags. `ast.arguments` carries defaults/annotations; we deliberately touch neither their values nor
    their source, only the arg NAME and whether a default slot is filled.

    Determinism: `args.defaults` fills the TAIL of (posonlyargs + args); `args.kw_defaults` is parallel
    to `kwonlyargs` with `None` marking a required keyword-only param. Counting the slots — never the
    expressions — yields required vs optional deterministically. A decorator/async def is still a def.
    """
    args = getattr(funcdef, "args", None)
    if args is None:
        return {}
    posonly = list(getattr(args, "posonlyargs", []) or [])
    positional = list(getattr(args, "args", []) or [])
    kwonly = list(getattr(args, "kwonlyargs", []) or [])

    pos_all = posonly + positional                         # positional slots, defaults apply to the tail
    pos_names = [a.arg for a in pos_all]
    kwonly_names = [a.arg for a in kwonly]

    num_pos_defaults = len(getattr(args, "defaults", []) or [])
    pos_optional = min(num_pos_defaults, len(pos_all))
    pos_required = len(pos_all) - pos_optional

    kw_defaults = list(getattr(args, "kw_defaults", []) or [])
    # kw_defaults is parallel to kwonly; None => required. Guard against a length mismatch defensively.
    kw_required = sum(1 for d in kw_defaults[: len(kwonly)] if d is None)
    kw_required += max(0, len(kwonly) - len(kw_defaults))   # any unpaired kwonly counts as required
    kw_optional = len(kwonly) - kw_required

    has_varargs = getattr(args, "vararg", None) is not None
    has_kwargs = getattr(args, "kwarg", None) is not None

    # Normalized, ORDERED structure for the fingerprint: (name, required/optional) per param + the
    # varargs/kwargs flags. No defaults, no annotations — only names + a required/optional marker.
    parts = []
    for i, a in enumerate(pos_all):
        parts.append(("p", a.arg, "R" if i < pos_required else "O"))
    parts.append(("*", has_varargs))
    for i, a in enumerate(kwonly):
        d = kw_defaults[i] if i < len(kw_defaults) else None
        parts.append(("k", a.arg, "R" if d is None else "O"))
    parts.append(("**", has_kwargs))
    fingerprint = hashlib.sha1(repr(tuple(parts)).encode("utf-8")).hexdigest()[:12]

    return {
        "param_names": pos_names + kwonly_names,
        "required_arity": pos_required + kw_required,
        "optional_arity": pos_optional + kw_optional,
        "has_varargs": has_varargs,
        "has_kwargs": has_kwargs,
        "kwonly_names": kwonly_names,
        "shape_fingerprint": fingerprint,
    }
