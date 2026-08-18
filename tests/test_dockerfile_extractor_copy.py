#!/usr/bin/env python3
"""DOCKERFILE EXTRACTOR-COPY gate — every ROOT module the App imports MUST be COPY'd into the image.

A pre-fix packaging regression split the extractor into a new root module and re-exported it, but the
Dockerfile's hand-maintained COPY list did not include it. Source-tree tests still passed while the built image
could not import the module. The gates missed it because nothing compared Dockerfile COPY inputs with imports.

This gate closes that gap: it parses which ROOT-level `.py` modules are imported (as a module) by
`code_graph_extract.py` AND the `github-app/` App code, then verifies the Dockerfile's `COPY ... ./` lines
(explicit names + globs expanded against the real root files) cover EVERY one. A future split that adds a
root module the COPY/glob doesn't reach FAILS here — before release.
"""
from __future__ import annotations
import os, re, fnmatch, glob

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _root_py_files():
    return {os.path.basename(p) for p in glob.glob(os.path.join(ROOT, "*.py"))}


def _imported_root_modules(roots):
    """Root .py modules imported (as a module) by code_graph_extract.py + every github-app/*.py."""
    stems = {f[:-3] for f in roots}                      # importable module names of root .py files
    sources = [os.path.join(ROOT, "code_graph_extract.py")] + glob.glob(os.path.join(ROOT, "github-app", "*.py"))
    needed, pat = set(), re.compile(r'^\s*(?:from\s+(\w+)\s+import|import\s+(\w+))', re.M)
    for src in sources:
        try:
            txt = open(src, encoding="utf-8", errors="ignore").read()
        except OSError:
            continue
        for m in pat.finditer(txt):
            mod = m.group(1) or m.group(2)
            if mod in stems:
                needed.add(mod + ".py")
    return needed


def _copied_root_files(roots):
    """Root .py files the Dockerfile COPYs into the image working root (`./`), names + glob expansion."""
    df = open(os.path.join(ROOT, "github-app", "Dockerfile"), encoding="utf-8").read()
    copied = set()
    for line in df.splitlines():
        s = line.strip()
        if not s.startswith("COPY "):
            continue
        args = s[len("COPY "):].split()
        if len(args) < 2 or args[-1] not in ("./", "."):  # only COPYs landing in the image root
            continue
        for a in args[:-1]:
            copied |= {f for f in roots if fnmatch.fnmatch(f, a)}
    return copied


def main() -> int:
    roots = _root_py_files()
    needed = _imported_root_modules(roots)
    copied = _copied_root_files(roots)
    missing = sorted(needed - copied)
    checks = [
        (f"found root .py modules ({len(roots)})", len(roots) > 0),
        (f"code_graph_extract + App import these root modules: {sorted(needed)}", len(needed) > 0),
        ("cg_generated.py is among the imported root modules (the #262 regression module)", "cg_generated.py" in needed),
        (f"Dockerfile COPYs cover EVERY imported root module (missing → would ImportError in the image: {missing})", not missing),
    ]
    ok = True
    for name, cond in checks:
        print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
        ok = ok and bool(cond)
    print("DOCKERFILE EXTRACTOR-COPY GATE:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
