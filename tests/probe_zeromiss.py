#!/usr/bin/env python3
"""AUDIT3 probe (v2, isolated per-case repo): for each extraction edge-gap pattern, build a tiny
repo where file A is TRULY coupled to file B, ingest, claim A and B by DIFFERENT authors as
in-flight PRs to main, read main_impact_surface, classify A's verdict:
  warn/serialize = DETECTED (good)
  unknown        = honest miss (acceptable per unknown-first)
  clear          = SILENT MISS (the launch-critical bug).

Each case gets its OWN repo coordinate (acme/c<N>) so there is zero cross-case state bleed.
Run after: bash db/bootstrap_local.sh veripsa_audit3
"""
import json, os, sys, tempfile
import psycopg2

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import code_graph_extract as X

DB = os.environ.get("AUDIT_DB", "veripsa_audit3")


def conn(role):
    c = psycopg2.connect(f"postgresql://{role}@localhost/{DB}")
    c.autocommit = True
    return c


def ingest(repo, files):
    with tempfile.TemporaryDirectory() as d:
        for name, body in files.items():
            full = os.path.join(d, name)
            os.makedirs(os.path.dirname(full), exist_ok=True)
            with open(full, "w") as fh:
                fh.write(body)
        graph = X.build_graph(d)
    with conn("veripsa_app") as c, c.cursor() as cur:
        cur.execute("SET search_path=core")
        cur.execute("SELECT core.ingest_graph_with_authority(%s,%s,%s,%s)",
                    (json.dumps(graph), repo, "main", "a" * 40))
    return graph


def claim(cid, path, repo, author):
    with conn("veripsa_app") as c, c.cursor() as cur:
        cur.execute("SET search_path=core")
        cur.execute("SELECT core.act_for_claim_with_authority(%s,%s,%s,%s,%s)",
                    (cid, path, repo, "main", author))


def surface(repo, hub_degree=8):
    with conn("veripsa_app") as c, c.cursor() as cur:
        cur.execute("SET search_path=core")
        cur.execute("SET veripsa.hub_degree = %s", (str(hub_degree),))
        cur.execute("SELECT core.main_impact_surface(%s,%s)", (repo, "main"))
        res = cur.fetchone()[0]
    if isinstance(res, str):
        res = json.loads(res)
    return res


CASES = []


def case(name, files, a, b, extra_claims=(), hub_degree=8, note=""):
    CASES.append((name, files, a, b, extra_claims, hub_degree, note))


# 1. CONTROL — a plain resolvable import MUST warn.
case("CONTROL plain import A->B",
     {"a.py": "from b import f\ndef g():\n    return f()\n",
      "b.py": "def f():\n    return 1\n"},
     "a.py", "b.py", note="control: must DETECT")

# 2. HUB DAMPENING — leaf_0 truly imports hub.py; hub.py also imported by 9 more (in-degree 10 > 8).
hub_files = {"hub.py": "def helper():\n    return 1\n"}
for i in range(10):
    hub_files[f"leaf_{i}.py"] = f"from hub import helper\ndef u{i}():\n    return helper()\n"
case("HUB DAMPENING leaf_0<->hub (hub in-degree 10>8)",
     hub_files, "leaf_0.py", "hub.py", hub_degree=8,
     note="real 1<->1 import coupling; hub dampened because in-degree>8")

# 3. DYNAMIC import (importlib).
case("DYNAMIC importlib.import_module('b')",
     {"a.py": "import importlib\ndef g():\n    m = importlib.import_module('b')\n    return m.run_job()\n",
      "b.py": "def run_job():\n    return 1\n"},
     "a.py", "b.py", note="dynamic import string — no static import node; A does not call run_job by name either")

# 4. STAR import.
case("STAR from b import *",
     {"a.py": "from b import *\ndef g():\n    return widget()\n",
      "b.py": "def widget():\n    return 1\n"},
     "a.py", "b.py", note="star import")

# 5. ALIASED import.
case("ALIASED from b import widget as q",
     {"a.py": "from b import widget as q\ndef g():\n    return q()\n",
      "b.py": "def widget():\n    return 1\n"},
     "a.py", "b.py", note="aliased import")

# 6. BARREL re-export (JS).
case("BARREL re-export (JS) a->barrel->b",
     {"a.js": "import { widget } from './barrel';\nexport function g(){ return widget(); }\n",
      "barrel.js": "export { widget } from './b';\n",
      "b.js": "export function widget(){ return 1; }\n"},
     "a.js", "b.js", note="a imports barrel (not b directly); only barrel<->b is a direct edge")

# 7. SHARED CONSTANT by value (no edge at all).
case("SHARED CONSTANT by value",
     {"a.py": "STATUS = 'ACTIVE'\ndef g():\n    return STATUS\n",
      "b.py": "def check(s):\n    return s == 'ACTIVE'\n"},
     "a.py", "b.py", note="pure value coupling — no import/call/resource edge exists")

# 8. DI / interface call (no import; A calls a method B defines on an injected object).
case("DI interface call (no import)",
     {"a.py": "def g(handler):\n    return handler.process_order()\n",
      "b.py": "class Worker:\n    def process_order(self):\n        return 1\n"},
     "a.py", "b.py", note="A calls process_order on injected handler; B defines it; no import between them")

# 9. MONKEYPATCH (A imports module b, mutates it at runtime).
case("MONKEYPATCH a imports b, sets b.f",
     {"a.py": "import b\ndef patch():\n    b.run_job = lambda: 2\n",
      "b.py": "def run_job():\n    return 1\n"},
     "a.py", "b.py", note="A imports module b (resolvable) so coupling likely DETECTED via import")

# 10. REFLECTION call (getattr by string).
case("REFLECTION getattr(b,'run_job')()",
     {"a.py": "import b\ndef g():\n    return getattr(b, 'run_job')()\n",
      "b.py": "def run_job():\n    return 1\n"},
     "a.py", "b.py", note="A imports b (resolvable) — import edge likely carries the coupling")

# 11. DECORATOR registry / metaprogramming (a registry decorator couples handler to dispatcher;
#     no direct import between the two HANDLER files — they share the registry only).
case("DECORATOR registry (handlers share dispatcher only)",
     {"dispatch.py": "REGISTRY = {}\ndef register(name):\n    def deco(f):\n        REGISTRY[name] = f\n        return f\n    return deco\n",
      "h1.py": "from dispatch import register\n@register('one')\ndef do_one():\n    return 1\n",
      "h2.py": "from dispatch import register\n@register('two')\ndef do_two():\n    return 2\n"},
     "h1.py", "h2.py", note="two handlers coupled only via the shared registry/dispatcher; no h1<->h2 edge")


def main():
    rows = []
    for n, (name, files, a, b, extra, hub_degree, note) in enumerate(CASES):
        repo = f"acme/c{n}"
        graph = ingest(repo, files)
        claim(f"PR-A:{a}", a, repo, "adev")
        claim(f"PR-B:{b}", b, repo, "bdev")
        for (cid, path, author) in extra:
            claim(cid, path, repo, author)
        res = surface(repo, hub_degree=hub_degree)
        by = {ch["change_id"]: ch for ch in res.get("changes", [])}
        cha = by.get("PR-A", {})
        v = cha.get("verdict", "MISSING")
        # does a direct A<->B coupling edge exist in the extracted graph (resolved to file paths)?
        coupling = [(e["src"], e["dst"], e["kind"]) for e in graph["edges"]
                    if e["kind"] in ("imports", "calls")
                    and ((e["src"].endswith(a) and e["dst"].endswith(b))
                         or (e["src"].endswith(b) and e["dst"].endswith(a)))]
        vc = {"warn": "DETECTED", "serialize": "DETECTED", "unknown": "honest-unknown",
              "clear": "SILENT-MISS", "MISSING": "NO-PR-A"}.get(v, v)
        rows.append((name, v, vc, len(coupling), cha.get("impact_count", "-"), note))

    print("\n=== AUDIT3 SILENT-MISS PROBE (v2, isolated repos) ===")
    print(f"{'CASE':<46} {'VERDICT':<10} {'CLASS':<15} {'A<->B edge':<11} {'impact'}")
    print("-" * 100)
    for (name, v, vc, ne, imp, note) in rows:
        print(f"{name[:45]:<46} {v:<10} {vc:<15} {ne:<11} {imp}")
    print("\n--- detail ---")
    for (name, v, vc, ne, imp, note) in rows:
        flag = "   <<<<< SILENT FALSE-CLEAR" if vc == "SILENT-MISS" else ""
        print(f"[{vc}] {name}{flag}")
        if note:
            print(f"        {note}")
    silent = [r for r in rows if r[2] == "SILENT-MISS"]
    print(f"\nSILENT FALSE-CLEAR COUNT: {len(silent)}")
    for r in silent:
        print("  SILENT:", r[0])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
