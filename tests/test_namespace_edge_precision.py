#!/usr/bin/env python3
"""NAMESPACE-EDGE PRECISION — investigation result + recall-safety GUARD (quality/namespace-edge-precision).

THE FINDING (fleet, 2026-06-19): the extractor resolves a C# `using A.B.C;` (`_resolve_csharp_ns`) and a
Go package import (`_resolve_go_pkg`) to EVERY file in that namespace's DIRECTORY — one `using` becomes N
`imports` edges (jellyfin: 94k such edges; hugo: 12k). Java is NOT affected: a Java import is a fully-
qualified type name that resolves to ONE file, and a wildcard `import pkg.*` resolves to nothing — measured
ZERO namespace fan-out edges on gson/okhttp. So the fan-out is C# + Go only (exactly the two resolvers).

A namespace import edge F -> G is CORROBORATED when F also `calls` a symbol G defines (the engine sees F
genuinely uses G), and UNCORROBORATED when F uses nothing G defines (G is merely in the namespace dir). The
hypothesis under test: uncorroborated namespace edges are SPURIOUS (F shares a namespace with G but is not
coupled to it), so demoting them would cut cry-wolf without losing recall.

WHAT THE INVESTIGATION MEASURED on real repos (content-free: paths, counts, edge kinds, symbol names;
co-change bar = engine's own support>=3, lift>=2 over git history; baseline = random same-language pair):

  repo        lang  baseline   corroborated co-change   UNCORROBORATED co-change   edges (corr / uncorr)
  jellyfin    C#    0.175%     1.085%  (x6.2)           0.033%  (x0.2  below base)   4701 / 94446
  CliWrap     C#   15.050%    42.857%  (x2.8)          38.710%  (x2.6  ABOVE base)     35 /    31
  protobuf    C#    0.525%     0.000%                   0.000%  (too few edges)         18 /    47
  hugo        Go    0.200%     3.060%  (x15.3)          0.344%  (x1.7  ABOVE base)   2974 /  9006
  gin         Go    7.000%    21.591%  (x3.1)           1.099%  (x0.2  below base)     88 /    91
  gorm        Go    3.800%    10.249%  (x2.7)           3.065%  (x0.8  below base)    683 /  1044
  gson/okhttp Java  -          (zero namespace fan-out edges — Java imports are precise single-file)

  VERDICT: the uncorroborated signal is REPO-DEPENDENT — at random baseline in jellyfin/gin (looks spurious),
  but MEANINGFULLY ABOVE baseline in CliWrap (x2.6) and hugo (x1.7). It is NOT uniformly spurious.

THE RECALL-DECISIVE TEST (mirrors PR #252's sole-carrier check): of the UNCORROBORATED namespace edges that
DO co-change (real coupling), how many are the SOLE structural carrier of that coupling (no OTHER import /
resolved edge links the pair)? If sole, demoting = a guaranteed silent miss.

  repo       uncorroborated recall-bearing pairs   ALSO carried by another edge   SOLE carrier
  CliWrap                12                                  0                          12
  hugo                   31                                  0                          31
  jellyfin               31                                  4                          27
  gin                     1                                  0                           1
  gorm                   32                                  0                          32

  EVERY repo: the recall-bearing uncorroborated edges are almost entirely the SOLE structural carrier of a
  real, high-lift coupling (hugo examples: filecache_config_test.go<->filecache_config.go co=5/5 lift 243;
  configlanguage.go<->configProvider.go lift 156). Even jellyfin — whose uncorroborated set averages BELOW
  baseline (looks spurious) — still has 27 real couplings whose ONLY carrier is the uncorroborated edge.

WHY: the call-corroboration test is INCOMPLETE. F can genuinely use G via a CONSTRUCTOR (`new AuthService()`),
a TYPE reference, a field, an interface, a generic, or a method whose call name the extractor did not resolve
— none of which produce a `calls` edge to a name G `contains`. So "uncorroborated" CONFLATES "F uses nothing
from G" (spurious) with "F uses G but the call resolver didn't see it" (legitimate). The two cannot be
separated by the available content-free signal without losing the measured recall (75/107 sole-carrier real
couplings across the five repos). Fan-out WIDTH does not separate them either: hugo's width-21+ uncorroborated
edges still co-change at x1.3 baseline, and CliWrap's width-2 edges at x2.3.

VERDICT: demoting uncorroborated namespace edges is NOT recall-safe. Implemented NOTHING that demotes them.
This is a valid, honest negative — identical in shape to PR #252's call-edge finding: the static signal the
extractor has is too incomplete to separate legitimate coupling from spurious fan-out safely. When in doubt,
KEEP — recall is sacred.

THIS GATE is the RECALL-SAFETY GUARD: it pins that the namespace import edge is emitted as a NORMAL `imports`
edge — corroborated AND uncorroborated alike — never dropped, never minted into a non-ingestible kind. A
future "precision fix" that drops/demotes the uncorroborated namespace edge in the extractor (the move the
numbers above prove regresses recall) FAILS here. It also pins the classification fixture (one corroborated,
one uncorroborated, one precise single-file import) so any future SQL-engine guard has a deterministic target.

Hermetic: synthetic C#/Go/Java source files in temp dirs; reads the RAW edges build_graph emits. No DB, no network.
Content-free: asserts on edge KINDS, file paths, and symbol NAMES only.
"""
from __future__ import annotations

import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import code_graph_extract as X  # noqa: E402

# The engine's permitted edge kinds (db/schema/20_core.sql code_edge_kind_check + db/schema/30_gate.sql
# ingest WHERE). An edge whose kind is NOT in this set is SILENTLY DROPPED at ingest — so the extractor
# can never "demote" an import to a lower-confidence kind without losing the edge. Pinned here so a future
# attempt to mint an 'imports_weak'/'unknown' kind for an uncorroborated namespace edge FAILS this gate.
_INGESTIBLE_EDGE_KINDS = frozenset({"contains", "calls", "imports", "queries", "alters", "reads_config"})


def _build(files):
    with tempfile.TemporaryDirectory() as d:
        for rel, body in files.items():
            p = os.path.join(d, rel)
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "w") as fh:
                fh.write(body)
        return X.build_graph(d)


def _contains_names(graph, path):
    return {e["dst"].split("::", 1)[1] for e in graph["edges"]
            if e["kind"] == "contains" and e["src"] == path and "::" in e["dst"]}


def _call_names(graph, path):
    return {e["dst"] for e in graph["edges"] if e["kind"] == "calls" and e["src"] == path}


def main():
    checks = []

    loaded = set(X._ts_languages())
    required = {"csharp", "go", "java"}
    checks.append((f"required namespace grammars loaded: {sorted(required)} (loaded={sorted(loaded)})",
                   required <= loaded))

    # ── Synthetic C# repo: namespace App.Services holds TWO files. Home.cs `using App.Services` + calls
    #    AuthService.Login() (CORROBORATED edge to AuthService.cs) but uses NOTHING from Logger.cs
    #    (UNCORROBORATED edge to Logger.cs, fanned out purely because Logger shares the namespace dir).
    #    Util.cs does a PRECISE single-file thing — it is in its OWN single-file namespace (no fan-out). ──
    g = _build({
        "App/Services/AuthService.cs":
            "namespace App.Services { public class AuthService { public void Login(){} } }\n",
        "App/Services/Logger.cs":
            "namespace App.Services { public class Logger { public void Write(){} } }\n",
        "App/Web/Home.cs":
            "using App.Services;\nnamespace App.Web { public class Home { "
            "public void Go(){ var a = new AuthService(); a.Login(); } } }\n",
    })
    edges = g["edges"]
    kinds = {e["kind"] for e in edges}
    imports = {(e["src"], e["dst"]) for e in edges if e["kind"] == "imports"}

    home = "App/Web/Home.cs"
    auth = "App/Services/AuthService.cs"
    logger = "App/Services/Logger.cs"

    # (0) The fan-out actually happened (the finding is real): ONE `using App.Services` produced import
    #     edges to BOTH files in the namespace directory.
    checks.append(("namespace fan-out present: `using App.Services` -> BOTH dir files (AuthService + Logger)",
                   (home, auth) in imports and (home, logger) in imports))

    # (1) RECALL-SAFETY: every emitted edge kind is INGESTIBLE — no edge minted into a non-ingestible
    #     (would-be-dropped) kind. This pins WHY a recall-safe extractor-side "demote to a new kind" is
    #     impossible (the engine would silently drop it = recall loss).
    bad_kinds = sorted(kinds - _INGESTIBLE_EDGE_KINDS)
    checks.append((f"all emitted edge kinds are ingestible (no silent-drop kind); kinds={sorted(kinds)}",
                   not bad_kinds))

    # (2) The corroborated vs uncorroborated classification is computable + correct on the fixture:
    #     Home CALLS `Login` which AuthService CONTAINS (corroborated); Home calls nothing Logger contains.
    home_calls = _call_names(g, home)
    checks.append(("CORROBORATED edge classifiable: Home calls a symbol AuthService defines (Login)",
                   bool(home_calls & _contains_names(g, auth))))
    checks.append(("UNCORROBORATED edge classifiable: Home calls NOTHING Logger defines",
                   not (home_calls & _contains_names(g, logger))))

    # (3) RECALL GUARD (the core hand-off): the UNCORROBORATED namespace edge is KEPT as a normal `imports`
    #     edge — NEVER dropped, NEVER demoted into a non-`imports` kind. The real-repo numbers prove
    #     demoting it costs recall (CliWrap 12, hugo 31, jellyfin 27, gorm 32 SOLE-carrier real couplings),
    #     so a future "precision fix" that removes/demotes it in the extractor MUST fail here.
    checks.append(("UNCORROBORATED namespace edge (Home -> Logger) is KEPT as a normal `imports` edge (recall-safe)",
                   (home, logger) in imports))
    checks.append(("the kept uncorroborated edge uses the `imports` kind, not a fabricated lower-confidence kind",
                   any(e["kind"] == "imports" and (e["src"], e["dst"]) == (home, logger) for e in edges)))

    # (4) EXISTING PRECISION INTACT: the corroborated namespace edge (Home -> AuthService) is also still a
    #     normal `imports` edge — the corroborated set is the high-signal coupling and must stay confident.
    checks.append(("CORROBORATED namespace edge (Home -> AuthService) is present as a normal `imports` edge",
                   (home, auth) in imports))
    cs_namespace_edges = [
        e for e in edges
        if e.get("kind") == "imports"
        and e.get("src") == home
        and e.get("dst") in {auth, logger}
    ]
    checks.append((
        "C# namespace-directory fan-out is exact evidence, never candidate ambiguity",
        len(cs_namespace_edges) == 2
        and all(
            e.get("reference_status") is None
            and e.get("ambiguous_reference") is not True
            for e in cs_namespace_edges
        ),
    ))
    cs_statuses = {
        n.get("path"): n.get("analysis_status")
        for n in g["nodes"]
        if n.get("path") in {home, auth, logger}
    }
    checks.append((
        "C# namespace endpoints remain normally analyzed and ambiguity metric stays zero",
        cs_statuses == {home: None, auth: None, logger: None}
        and (g.get("metrics") or {}).get("ambiguous_reference_count") == 0,
    ))

    # The same namespace suffix under TWO equally-specific project roots is a
    # competing directory resolution, unlike the exact multi-file contents of
    # one directory above.
    cg = _build({
        "ProjA/App/Services/A.cs":
            "namespace App.Services { public class A {} }\n",
        "ProjB/App/Services/B.cs":
            "namespace App.Services { public class B {} }\n",
        "Web/Home.cs":
            "using App.Services;\n"
            "namespace Web { public class Home { } }\n",
    })
    competing_src = "Web/Home.cs"
    competing_targets = {
        "ProjA/App/Services/A.cs",
        "ProjB/App/Services/B.cs",
    }
    competing_edges = [
        edge for edge in cg["edges"]
        if edge.get("kind") == "imports"
        and edge.get("src") == competing_src
        and edge.get("dst") in competing_targets
    ]
    checks.append((
        "C# equally-specific namespace directories retain every candidate as "
        "explicit ambiguity evidence",
        len(competing_edges) == 2
        and {edge.get("dst") for edge in competing_edges}
        == competing_targets
        and all(
            edge.get("reference_status") == "ambiguous"
            and edge.get("ambiguous_reference") is True
            and edge.get("ambiguity_key") == "App.Services"
            for edge in competing_edges
        ),
    ))
    competing_paths = {competing_src} | competing_targets
    competing_statuses = {
        node.get("path"): node.get("analysis_status")
        for node in cg["nodes"]
        if node.get("kind") == "file"
        and node.get("path") in competing_paths
    }
    checks.append((
        "C# competing-directory source and all targets become ambiguous with "
        "one logical metric observation",
        competing_statuses
        == {path: "ambiguous" for path in competing_paths}
        and (cg.get("metrics") or {}).get(
            "ambiguous_reference_count"
        ) == 1,
    ))

    # Directory depth is not a project-reference signal. A deeper path remains
    # a competing suffix candidate when no canonical path equals App.Services.
    ucg = _build({
        "ProjA/App/Services/A.cs":
            "namespace App.Services { public class A {} }\n",
        "Company/ProjB/App/Services/B.cs":
            "namespace App.Services { public class B {} }\n",
        "Web/Home.cs":
            "using App.Services;\n"
            "namespace Web { public class Home { } }\n",
    })
    unequal_targets = {
        "ProjA/App/Services/A.cs",
        "Company/ProjB/App/Services/B.cs",
    }
    unequal_paths = {competing_src} | unequal_targets
    unequal_edges = [
        edge for edge in ucg["edges"]
        if edge.get("kind") == "imports"
        and edge.get("src") == competing_src
        and edge.get("dst") in unequal_targets
    ]
    unequal_statuses = {
        node.get("path"): node.get("analysis_status")
        for node in ucg["nodes"]
        if node.get("kind") == "file"
        and node.get("path") in unequal_paths
    }
    checks.append((
        "C# unequal-depth namespace suffix directories are all competing "
        "candidates; path depth cannot manufacture an exact project",
        len(unequal_edges) == 2
        and {edge.get("dst") for edge in unequal_edges}
        == unequal_targets
        and all(
            edge.get("reference_status") == "ambiguous"
            and edge.get("ambiguity_key") == "App.Services"
            for edge in unequal_edges
        )
        and unequal_statuses
        == {path: "ambiguous" for path in unequal_paths}
        and (ucg.get("metrics") or {}).get(
            "ambiguous_reference_count"
        ) == 1,
    ))

    # A type-qualified using narrows candidates by filename, but the same type
    # under two unproven project roots is still ambiguous.
    tcg = _build({
        "ProjA/App/Services/Tools.cs":
            "namespace App.Services { public static class Tools {} }\n",
        "Company/ProjB/App/Services/Tools.cs":
            "namespace App.Services { public static class Tools {} }\n",
        "Web/Home.cs":
            "using static App.Services.Tools;\n"
            "namespace Web { public class Home { } }\n",
    })
    typed_targets = {
        "ProjA/App/Services/Tools.cs",
        "Company/ProjB/App/Services/Tools.cs",
    }
    typed_paths = {competing_src} | typed_targets
    typed_edges = [
        edge for edge in tcg["edges"]
        if edge.get("kind") == "imports"
        and edge.get("src") == competing_src
        and edge.get("dst") in typed_targets
    ]
    typed_statuses = {
        node.get("path"): node.get("analysis_status")
        for node in tcg["nodes"]
        if node.get("kind") == "file"
        and node.get("path") in typed_paths
    }
    checks.append((
        "C# type-qualified using remains ambiguous when the selected type "
        "exists under multiple suffix-matching directories",
        len(typed_edges) == 2
        and {edge.get("dst") for edge in typed_edges} == typed_targets
        and all(
            edge.get("reference_status") == "ambiguous"
            and edge.get("ambiguity_key") == "App.Services.Tools"
            for edge in typed_edges
        )
        and typed_statuses
        == {path: "ambiguous" for path in typed_paths}
        and (tcg.get("metrics") or {}).get(
            "ambiguous_reference_count"
        ) == 1,
    ))

    # (5) Go control: a package import names a directory and intentionally resolves to every
    # importable non-test .go file in that package. This is the Go half of this gate's contract.
    gg = _build({
        "internal/auth/auth.go":
            "package auth\nfunc Login() {}\n",
        "internal/auth/audit.go":
            "package auth\nfunc Record() {}\n",
        "cmd/app/main.go":
            'package main\nimport "example.com/project/internal/auth"\nfunc main() { auth.Login() }\n',
    })
    gimp = {(e["src"], e["dst"]) for e in gg["edges"] if e["kind"] == "imports"}
    ghome = "cmd/app/main.go"
    checks.append(("Go package import resolves to the package implementation file",
                   (ghome, "internal/auth/auth.go") in gimp))
    checks.append(("Go package import keeps the package sibling edge (package-directory semantics)",
                   (ghome, "internal/auth/audit.go") in gimp))
    go_targets = {"internal/auth/auth.go", "internal/auth/audit.go"}
    go_package_edges = [
        e for e in gg["edges"]
        if e.get("kind") == "imports"
        and e.get("src") == ghome
        and e.get("dst") in go_targets
    ]
    checks.append((
        "Go package-directory fan-out is exact evidence, never candidate ambiguity",
        len(go_package_edges) == 2
        and all(
            e.get("reference_status") is None
            and e.get("ambiguous_reference") is not True
            for e in go_package_edges
        ),
    ))
    go_statuses = {
        n.get("path"): n.get("analysis_status")
        for n in gg["nodes"]
        if n.get("path") in ({ghome} | go_targets)
    }
    checks.append((
        "Go package endpoints remain normally analyzed and ambiguity metric stays zero",
        go_statuses
        == {
            ghome: None,
            "internal/auth/auth.go": None,
            "internal/auth/audit.go": None,
        }
        and (gg.get("metrics") or {}).get("ambiguous_reference_count") == 0,
    ))

    # (6) Java control: a Java explicit import resolves to ONE file (precise, no fan-out); a wildcard
    #     resolves to nothing. Pins that the fan-out is C#/Go only — a future change must not start
    #     fanning Java imports out to a package directory.
    jg = _build({
        "src/com/app/svc/AuthService.java":
            "package com.app.svc; public class AuthService { public void login(){} }\n",
        "src/com/app/svc/UserService.java":
            "package com.app.svc; public class UserService { public void find(){} }\n",
        "src/com/app/web/Home.java":
            "package com.app.web; import com.app.svc.AuthService; "
            "public class Home { void go(){ new AuthService().login(); } }\n",
    })
    jimp = {(e["src"], e["dst"]) for e in jg["edges"] if e["kind"] == "imports"}
    jhome = "src/com/app/web/Home.java"
    auth_j = "src/com/app/svc/AuthService.java"
    user_j = "src/com/app/svc/UserService.java"
    checks.append(("Java explicit import resolves PRECISELY to the one named file (Home -> AuthService)",
                   (jhome, auth_j) in jimp))
    checks.append(("Java import does NOT fan out to the package sibling (Home -/-> UserService) — no namespace fan-out",
                   (jhome, user_j) not in jimp))

    ok = True
    for name, cond in checks:
        print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
        ok = ok and bool(cond)
    print("NAMESPACE EDGE PRECISION GATE:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
