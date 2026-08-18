#!/usr/bin/env python3
"""CONTENT-FREE FUNCTION-SHAPE gate (compatibility-fact foundation, milestone 1).

Each Python `def`/method node now ALSO carries a content-free shape of its public signature — the
foundation the compatibility rules compare across two open PRs to decide who lands first. This gate
proves the shape is correct AND content-free, with NO database (a pure-extractor unit test):

  ARITY)  required vs optional counts are deterministic across positional / defaulted / keyword-only /
          *args / **kwargs / positional-only forms.
  STABLE) the fingerprint is identical across repeated runs (a stable hash, not id()/address).
  CHANGE) adding a REQUIRED argument changes the fingerprint (the diff signal the rules need).
  NEUTRAL) changing ONLY a default VALUE or an ANNOTATION does NOT change the shape/fingerprint —
          proof the hash is over structure, never over default/annotation source.
  FREE)   NO default-value expression and NO annotation source string appears ANYWHERE in the emitted
          node — the content-free invariant, asserted byte-wise.
  ADDITIVE) a class node gets NO shape fields; a syntactically-odd file never raises.

Run: python3 tests/test_cg_python_shape.py
"""
from __future__ import annotations

import ast
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import code_graph_extract as X  # noqa: E402
from _cg_python import _py_signature_shape  # noqa: E402


def _shape(src, name="f"):
    """Parse a snippet and return the shape dict for the named top-level def."""
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return _py_signature_shape(node)
    raise AssertionError(f"def {name} not found")


def _def_nodes(src):
    """Extract the file and return {name: node} for every `def` node emitted by the real extractor."""
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "m.py")
        with open(p, "w") as fh:
            fh.write(src)
        nodes, _edges, ok = X.extract_file_py(p, "m.py")
    assert ok, "extractor reported not-ok on valid source"
    return {n["name"]: n for n in nodes if n.get("kind") == "def"}


def main() -> int:
    checks = []

    def ck(name, cond):
        checks.append((name, bool(cond)))

    # -----------------------------------------------------------------------------------------
    # ARITY — required / optional / kwonly / varargs / kwargs across the full parameter grammar.
    # -----------------------------------------------------------------------------------------
    s = _shape("def f(a, b, c=1, d=2):\n    pass\n")
    ck("ARITY: 2 required + 2 optional positional", s["required_arity"] == 2 and s["optional_arity"] == 2)
    ck("ARITY: param_names are ordered NAMES only", s["param_names"] == ["a", "b", "c", "d"])
    ck("ARITY: no varargs / no kwargs on a plain def", s["has_varargs"] is False and s["has_kwargs"] is False)
    ck("ARITY: no keyword-only names on a plain def", s["kwonly_names"] == [])

    s = _shape("def f(a, *args, k, m=3, **kw):\n    pass\n")
    ck("ARITY: *args detected", s["has_varargs"] is True)
    ck("ARITY: **kwargs detected", s["has_kwargs"] is True)
    ck("ARITY: keyword-only names captured in order", s["kwonly_names"] == ["k", "m"])
    # required = a (pos) + k (kwonly, no default); optional = m (kwonly, default). varargs/kwargs excluded.
    ck("ARITY: kwonly required/optional split, varargs+kwargs excluded from arity",
       s["required_arity"] == 2 and s["optional_arity"] == 1)
    ck("ARITY: param_names include positional + keyword-only, exclude *args/**kwargs names",
       s["param_names"] == ["a", "k", "m"])

    # positional-only (py3.8+): `x` before `/` is positional-only, still a required positional param.
    s = _shape("def f(x, /, y, z=1):\n    pass\n")
    ck("ARITY: positional-only param counted (x,/ ,y required; z optional)",
       s["required_arity"] == 2 and s["optional_arity"] == 1 and s["param_names"] == ["x", "y", "z"])

    # async def is still a def with a shape.
    s = _shape("async def f(a, b=1):\n    pass\n")
    ck("ARITY: async def carries a shape", s["required_arity"] == 1 and s["optional_arity"] == 1)

    # -----------------------------------------------------------------------------------------
    # STABLE — the fingerprint is a stable hash, identical across independent runs.
    # -----------------------------------------------------------------------------------------
    fp1 = _shape("def f(a, b, c=1):\n    pass\n")["shape_fingerprint"]
    fp2 = _shape("def f(a, b, c=1):\n    pass\n")["shape_fingerprint"]
    ck("STABLE: identical signature => identical fingerprint across runs", fp1 == fp2 and isinstance(fp1, str) and fp1)

    # -----------------------------------------------------------------------------------------
    # CHANGE — adding a required arg changes the fingerprint (the rule-layer diff signal).
    # -----------------------------------------------------------------------------------------
    fp_before = _shape("def f(a, b):\n    pass\n")["shape_fingerprint"]
    fp_after = _shape("def f(a, b, c):\n    pass\n")["shape_fingerprint"]
    ck("CHANGE: adding a required arg changes the fingerprint", fp_before != fp_after)
    # optional->required is a shape change too.
    fp_opt = _shape("def f(a, b=1):\n    pass\n")["shape_fingerprint"]
    fp_req = _shape("def f(a, b):\n    pass\n")["shape_fingerprint"]
    ck("CHANGE: optional->required changes the fingerprint", fp_opt != fp_req)

    # -----------------------------------------------------------------------------------------
    # NEUTRAL — default VALUE and ANNOTATION are NOT part of the shape: changing them is a no-op on
    # the fingerprint. This is the positive proof the hash is over structure, never over source.
    # -----------------------------------------------------------------------------------------
    fp_d1 = _shape("def f(a, b=1):\n    pass\n")["shape_fingerprint"]
    fp_d2 = _shape("def f(a, b=99999):\n    pass\n")["shape_fingerprint"]
    ck("NEUTRAL: changing a default VALUE does not change the fingerprint", fp_d1 == fp_d2)
    fp_a1 = _shape("def f(a, b):\n    pass\n")["shape_fingerprint"]
    fp_a2 = _shape("def f(a: int, b: 'SomeSecretType') -> None:\n    pass\n")["shape_fingerprint"]
    ck("NEUTRAL: adding annotations does not change the fingerprint", fp_a1 == fp_a2)

    # -----------------------------------------------------------------------------------------
    # FREE — CONTENT-FREE: no default expression or annotation source appears in the emitted node.
    # The source embeds sentinel tokens in defaults + annotations; none may survive into the node.
    # -----------------------------------------------------------------------------------------
    src = (
        "def secretfn(user, token=SENTINEL_DEFAULT_VALUE, count: SENTINEL_ANNOTATION = 7, *, "
        "flag: 'SENTINEL_STRING_ANN' = SENTINEL_KW_DEFAULT):\n"
        "    body_secret = SENTINEL_BODY\n"
        "    return body_secret\n"
    )
    node = _def_nodes(src)["secretfn"]
    blob = repr(node)
    forbidden = [
        "SENTINEL_DEFAULT_VALUE", "SENTINEL_ANNOTATION", "SENTINEL_STRING_ANN",
        "SENTINEL_KW_DEFAULT", "SENTINEL_BODY", "body_secret",
    ]
    leaked = [tok for tok in forbidden if tok in blob]
    ck(f"FREE: no default/annotation/body source in the node (leaked={leaked})", not leaked)
    # the node DOES carry the structural facts (names, arity, fingerprint) — proof it's not empty.
    ck("FREE: node still carries the structural shape (names + arity + fingerprint)",
       node.get("param_names") == ["user", "token", "count", "flag"]
       and node.get("required_arity") == 1 and node.get("optional_arity") == 3
       and isinstance(node.get("shape_fingerprint"), str) and node.get("has_kwargs") is False)

    # -----------------------------------------------------------------------------------------
    # ADDITIVE — a class node gets NO shape fields; an exotic/odd file never raises.
    # -----------------------------------------------------------------------------------------
    nodes, _e, ok = X.extract_file_py(_write("class C:\n    def m(self, a):\n        pass\n"), "c.py")
    cls = [n for n in nodes if n.get("kind") == "class"]
    ck("ADDITIVE: class node carries NO signature-shape fields",
       cls and all("param_names" not in n and "shape_fingerprint" not in n for n in cls))
    meth = [n for n in nodes if n.get("kind") == "def"]
    ck("ADDITIVE: a method still gets a shape (self + a => 2 required)",
       meth and meth[0]["required_arity"] == 2 and meth[0]["param_names"] == ["self", "a"])

    # never-crash: a syntax-error file degrades to a bare file node, ok=False, no exception.
    nodes, _e, ok = X.extract_file_py(_write("def broken(:\n"), "bad.py")
    ck("ADDITIVE: a syntax-error file degrades to ok=False without raising", ok is False)

    ok_all = True
    for name, cond in checks:
        print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
        ok_all = ok_all and cond
    print("CG-PYTHON SHAPE GATE:", "PASS" if ok_all else "FAIL")
    return 0 if ok_all else 1


_TMP = []


def _write(src):
    """Write a snippet to a temp file that outlives the call (extractor re-reads the path)."""
    import tempfile

    fd, p = tempfile.mkstemp(suffix=".py")
    with os.fdopen(fd, "w") as fh:
        fh.write(src)
    _TMP.append(p)
    return p


if __name__ == "__main__":
    try:
        rc = main()
    finally:
        for p in _TMP:
            try:
                os.unlink(p)
            except OSError:
                pass
    raise SystemExit(rc)
