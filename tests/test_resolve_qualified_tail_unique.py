#!/usr/bin/env python3
"""QUALIFIED-PATH LEADING-DROP UNIQUENESS — precision fix + recall-safety GUARD
(quality/resolve-qualified-tail-unique).

THE FINDING (real-repo audit, symfony + laravel, 2026-06-23): the resolver's Rust/PHP qualified-path
RECALL fallback (`_cg_resolve._candidates_bare`, the `qualified_path and "/" in mod` block) drops the
leading namespace segments of a `::`/`\` import and probes each MULTI-segment tail, taking the FIRST
non-empty one. The comment promised "resolves to `app/Models/User.php` and NOT to an unrelated file",
but the loop did `cands |= sub; break` — it FANNED OUT to EVERY file matching that tail.

A 2-segment tail like `Mapping/ClassMetadata` is too generic in a large PSR-4 namespace: it
suffix-matches MULTIPLE unrelated files. And the import that triggers the leading-drop is almost always
an EXTERNAL class (a vendored dep), which names NO internal file at all — so every fan-out target is a
FALSE edge. Measured raw fan-out (one import → multiple unrelated files) on real repos:

  src/Symfony/Bridge/Doctrine/Form/ChoiceList/IdReader.php
      use Doctrine\Persistence\Mapping\ClassMetadata;        (EXTERNAL Doctrine class)
   →  src/Symfony/Component/Serializer/Mapping/ClassMetadata.php    [FALSE]
   →  src/Symfony/Component/Validator/Mapping/ClassMetadata.php     [FALSE]

  Laravel: use Symfony\Component\Console\Application;            (EXTERNAL Symfony class)
   →  src/Illuminate/Console/Application.php                       [FALSE]
   →  src/Illuminate/Contracts/Console/Application.php             [FALSE]

Across the audited repos: symfony 119 ambiguous (multi-match) leading-drop fan-outs, laravel 11 — every
sampled one an external/decoy import attached to internal same-tail files. The 81 (symfony) / 799
(laravel) UNIQUE leading-drop matches are the legitimate recall (Laravel `types/` stubs mirroring
`Illuminate/`, a PSR interface re-implemented at exactly one internal path).

THE FIX (mirrors the Rust crate:: trailing-type fallback right below it, which already requires
`len(candidates) == 1`, and the Ruby bare-require uniqueness guard): accept a leading-drop tail match
ONLY when it resolves UNIQUELY (exactly one file). An ambiguous most-specific tail is suppressed (the
existing unresolved baseline is safer). This is recall-SAFE because the suffix index is monotone — a
shorter tail's match set is a SUPERSET of any longer tail's (verified 0 violations on symfony+laravel),
so an ambiguous most-specific tail can never disambiguate by shortening; nothing is recoverable.

VERIFIED on real repos (build_graph edge sets, origin vs fixed):
  • symfony  resolved_internal 31432 → 31014 (−418 false fan-out edge instances; 116 import-sites cleaned)
  • laravel  resolved_internal  8219 →  8193
  • ripgrep / tokio / vscode-Rust: byte-IDENTICAL (.rs-source edges 81 / 1681 / 127 unchanged) — Rust
    qualified imports resolve via the dedicated crate::/workspace fallbacks or hit the loop UNIQUELY,
    so they are untouched. The fix is scoped to `::`/`\` imports only; Java(.)/Go/C#/TS never enter it.

THIS GATE pins BOTH directions on a hermetic synthetic fixture:
  PRECISION  an EXTERNAL qualified import whose only internal matches are an AMBIGUOUS same-tail pair
             resolves to NEITHER (the false fan-out the fix removed must STAY removed).
  RECALL     a qualified import whose tail matches exactly ONE internal file STILL resolves to it
             (the legitimate leading-drop recall must NOT regress), for PHP `\` AND Rust `crate::`.

Hermetic: synthetic source files in a temp dir; reads the RAW edges build_graph emits. No DB, no network.
Content-free: asserts on edge KINDS and file PATHS only (never file bodies).
"""
from __future__ import annotations

import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import code_graph_extract as X  # noqa: E402


def _build(files):
    with tempfile.TemporaryDirectory() as d:
        for rel, body in files.items():
            p = os.path.join(d, rel)
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "w") as fh:
                fh.write(body)
        return X.build_graph(d)


def _internal_imports(graph, src):
    fp = {n["path"] for n in graph["nodes"] if n.get("kind") == "file"}
    return {e["dst"] for e in graph["edges"]
            if e["kind"] == "imports" and e["src"] == src and e["dst"] in fp}


def main():
    checks = []

    # ── PHP fixture: a Consumer that `use`s TWO external classes (no `Vendor\` dir exists in the repo).
    #    • `Vendor\Pkg\Mapping\ClassMetadata` — its 2-seg tail `Mapping/ClassMetadata` AMBIGUOUSLY matches
    #      TWO unrelated internal files (Serializer/Mapping + Validator/Mapping). Pre-fix: false fan-out
    #      to BOTH. Post-fix: NEITHER (ambiguous tail suppressed).
    #    • `Vendor\Pkg\Support\Collection` — its 2-seg tail `Support/Collection` matches exactly ONE
    #      internal file. This is the legitimate leading-drop recall — must STILL resolve. ──
    php = _build({
        "src/Serializer/Mapping/ClassMetadata.php":
            "<?php\nnamespace App\\Serializer\\Mapping;\nclass ClassMetadata {}\n",
        "src/Validator/Mapping/ClassMetadata.php":
            "<?php\nnamespace App\\Validator\\Mapping;\nclass ClassMetadata {}\n",
        "src/Support/Collection.php":
            "<?php\nnamespace App\\Support;\nclass Collection {}\n",
        "src/Consumer.php":
            "<?php\nnamespace App;\n"
            "use Vendor\\Pkg\\Mapping\\ClassMetadata;\n"
            "use Vendor\\Pkg\\Support\\Collection;\n"
            "class Consumer { function f(ClassMetadata $m, Collection $c){} }\n",
    })
    php_src = "src/Consumer.php"
    php_internal = _internal_imports(php, php_src)
    ser = "src/Serializer/Mapping/ClassMetadata.php"
    val = "src/Validator/Mapping/ClassMetadata.php"
    coll = "src/Support/Collection.php"

    # PRECISION: the ambiguous external import must resolve to NEITHER same-tail file (false fan-out gone).
    checks.append((
        "PHP precision: external `use Vendor\\Pkg\\Mapping\\ClassMetadata` does NOT fan out to the "
        "ambiguous same-tail pair (Serializer/Validator) — both are FALSE edges",
        ser not in php_internal and val not in php_internal,
    ))
    # RECALL: the unique-tail import still resolves (the legitimate leading-drop recall is preserved).
    checks.append((
        "PHP recall: `use Vendor\\Pkg\\Support\\Collection` STILL resolves to the SOLE internal "
        "Support/Collection.php (unique leading-drop tail kept)",
        coll in php_internal,
    ))

    # ── Rust control: a `crate::auth::login::do_login` whose tail uniquely names one module file STILL
    #    resolves (the shared loop must not regress Rust — verified byte-identical on ripgrep/tokio). ──
    rs = _build({
        "src/auth/login.rs": "pub fn do_login(){}\n",
        "src/main.rs": "mod auth;\nuse crate::auth::login::do_login;\nfn main(){ do_login(); }\n",
    })
    rs_internal = _internal_imports(rs, "src/main.rs")
    checks.append((
        "Rust recall (shared-path control): `use crate::auth::login::do_login` STILL resolves to "
        "src/auth/login.rs — the uniqueness guard leaves a UNIQUE qualified match untouched",
        "src/auth/login.rs" in rs_internal,
    ))

    # ── Rust precision control: an AMBIGUOUS qualified tail in Rust is also suppressed (same guard,
    #    same recall-safe outcome). Two unrelated modules share the tail `mapping/meta`; an external-style
    #    `use vendor::mapping::meta::Foo` (vendor is not a workspace member) must resolve to NEITHER. ──
    rs2 = _build({
        "src/a/mapping/meta.rs": "pub struct Foo;\n",
        "src/b/mapping/meta.rs": "pub struct Foo;\n",
        "src/main.rs": "use vendor::mapping::meta::Foo;\nfn main(){}\n",
    })
    rs2_internal = _internal_imports(rs2, "src/main.rs")
    checks.append((
        "Rust precision (shared-path control): an ambiguous external `use vendor::mapping::meta::Foo` "
        "does NOT fan out to the same-tail pair (a/mapping + b/mapping)",
        "src/a/mapping/meta.rs" not in rs2_internal and "src/b/mapping/meta.rs" not in rs2_internal,
    ))

    ok = True
    for name, cond in checks:
        print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
        ok = ok and bool(cond)
    print("QUALIFIED-PATH TAIL UNIQUENESS GATE:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
