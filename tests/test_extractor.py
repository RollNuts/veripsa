#!/usr/bin/env python3
"""Extractor gate (no DB): import→file resolution (#1) + multi-language coverage (#4).

Self-contained: builds a tiny polyglot fixture in a temp dir, runs the real extractor, asserts. Language
checks are CONDITIONAL on the grammar being installed (so a Python-only environment still passes — the
product degrades to fewer languages, never crashes). The Python import-resolution check always runs.
"""
import os
import shutil
import sys
import tempfile
import unicodedata

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import code_graph_extract as X  # noqa: E402
import _cg_config as _CFG  # noqa: E402  (direct unit-level access for a deterministic regression check)
import _cg_schema as _SCH  # noqa: E402  (direct unit-level access for the schema-graph bounded-output guard)


def _w(path, body):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as fh:
        fh.write(body)


def _wb(path, body_bytes):
    """Write raw bytes to a file (for binary / oversized fixtures)."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(body_bytes)


def shipped_grammar_checks():
    """M1 guard (drift CLASS, not just the instance): the production image (github-app/Dockerfile) must ship a
    tree-sitter grammar for EVERY language requirements.txt declares as supported. Found by audit — kotlin + swift
    were declared (the extractor enumerates them in _ts_languages) but the Dockerfile omitted them, so Android/iOS
    files silently fell out of the graph in PROD (false 'unknown', missed coupling). Pure static file comparison,
    no Docker build needed; it just can't drift again. (The core 'tree-sitter' package has no '-<lang>' suffix, so
    the regex compares only language grammars — Python uses stdlib ast and needs no grammar in either file.)"""
    import re
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    def grammars(path):
        try:
            with open(path) as fh:
                return set(re.findall(r"tree-sitter-[a-z0-9-]+", fh.read()))
        except FileNotFoundError:
            return set()

    declared = grammars(os.path.join(root, "requirements.txt"))
    shipped = grammars(os.path.join(root, "github-app", "Dockerfile"))
    missing = sorted(declared - shipped)
    return [
        ("shipped grammars: requirements.txt declares the polyglot grammar set (sanity, ≥10)", len(declared) >= 10),
        (f"shipped grammars: the Dockerfile ships EVERY declared grammar — no silent prod language gap (missing={missing})",
         bool(declared) and not missing),
    ]


def robustness_checks():
    """build_graph must never crash and must correctly skip oversized / binary / vendored files.

    Fixture tree:
      normal.py        — a plain valid Python file (must be included)
      big.js           — a 2 MB junk JS file (exceeds _FILE_SIZE_CAP → must be excluded)
      binary_hack.py   — bytes with a NUL (binary probe → must be excluded)
      vendor/util.py   — under the 'vendor' skip-dir (must be excluded)
      bad_encoding.py  — valid-looking Python with invalid UTF-8 but no NUL (extractor handles,
                         file node must appear — the per-file extractor already tolerates bad encodings)
    """
    d = tempfile.mkdtemp(prefix="veripsa_robust_")
    try:
        # A normal Python file — must appear in the graph.
        _w(os.path.join(d, "normal.py"), "def hello():\n    pass\n")

        # Oversized JS file (2 MB of repeated ASCII) — must be skipped.
        _wb(os.path.join(d, "big.js"), b"x = 1;\n" * 300_000)  # ~2.1 MB

        # Binary file with a source-like extension (contains NUL bytes) — must be skipped.
        _wb(os.path.join(d, "binary_hack.py"), b"# looks like python\x00\x01\x02but has NUL\n")

        # File under a vendor/ directory — the whole dir must be pruned.
        _w(os.path.join(d, "vendor", "util.py"), "def vendored_func():\n    pass\n")

        # File with a broken UTF-8 sequence but no NUL — binary probe passes, extractor
        # handles the UnicodeDecodeError internally (returns ok=False, file node still present).
        _wb(os.path.join(d, "bad_encoding.py"), b"def bad():\n    x = '\xff\xfe'\n    pass\n")

        # build_graph must not raise.
        try:
            g = X.build_graph(d)
        except Exception as exc:
            return [("build_graph does NOT raise on pathological inputs", False,
                     f"raised {type(exc).__name__}: {exc}")]

        file_paths = {n["path"] for n in g["nodes"] if n["kind"] == "file"}

        checks = []
        checks.append(("robustness: build_graph returns without raising",
                        True))  # we got here, so it did not raise

        checks.append(("robustness: normal.py IS included in the graph",
                        any(p.endswith("normal.py") for p in file_paths)))

        checks.append(("robustness: big.js (>1.5 MB) is EXCLUDED (size cap)",
                        not any(p.endswith("big.js") for p in file_paths)))

        checks.append(("robustness: binary_hack.py (NUL byte) is EXCLUDED (binary check)",
                        not any(p.endswith("binary_hack.py") for p in file_paths)))

        checks.append(("robustness: vendor/util.py is EXCLUDED (vendored dir skip)",
                        not any("vendor" in p and p.endswith("util.py") for p in file_paths)))

        return checks
    finally:
        shutil.rmtree(d, ignore_errors=True)


def adversarial_source_checks():
    """DEEPER never-crash / never-hang audit against NASTIER attacker/customer-controlled source.

    The extractor parses ARBITRARY cloned source. One pathological file must NEVER crash or hang the
    whole tenant's ingest — it must degrade (skip / bare node / partial graph). This pushes the envelope
    `robustness_checks()` opened: deep nesting (stack risk), a multi-MB single line (no-newline + ReDoS
    bait on the regex schema/config passes that run OUTSIDE build_graph's per-file try/except), broken
    syntax in every language (tree-sitter ERROR nodes → a PARTIAL graph, never a raise), zero-width /
    control / RTL-override unicode + very long identifiers in symbols AND paths, binary garbage behind a
    source extension, pathological deep/odd paths — and the ONE real bug this audit found: a CRLF YAML
    `environment:` block (Windows line endings, ubiquitous) raised an unhandled StopIteration out of the
    config pass (offset↔line desync) → silent partial config graph, or an outright build_graph crash
    when os.walk had nothing left to absorb the StopIteration. Fixed minimally in _cg_config.py.

    Each case asserts: build_graph (a) never raises, (b) completes within a sane wall-clock bound (no
    hang), (c) returns a bounded/sane graph (no node/edge explosion). Language cases are robust to a
    grammar being ABSENT (python-only env): a crash/hang must NOT happen whether a file is parsed, bare-
    noded, or skipped — so they assert no-raise + the file is accounted for, never a specific parse.

    A generous wall-clock ceiling (each fixture is tiny work; a hang would be seconds→minutes). Kept
    loose so a slow CI box never flakes, but tight enough to catch a real ReDoS / stack-thrash / O(n^2)."""
    import time

    HANG_CEILING_S = 30.0          # any single build_graph below must finish FAR under this (else: a hang)
    BIG_LINE = 1_400_000           # just UNDER _FILE_SIZE_CAP (1.5 MB) so the file is NOT size-skipped —
    #                                this routes a multi-MB single line THROUGH the regex schema/config
    #                                passes (which run outside the per-file guard), the real ReDoS surface.

    checks = []

    def _run(label, build_fixture, assert_graph):
        """Build a temp fixture, time build_graph, assert it neither raised nor hung, then run extra
        graph assertions. Appends one or more (label, cond) tuples. A raise/hang is itself a FAIL."""
        d = tempfile.mkdtemp(prefix="veripsa_adv_")
        try:
            build_fixture(d)
            t0 = time.time()
            try:
                g = X.build_graph(d)
            except Exception as exc:
                checks.append((f"{label}: build_graph does NOT raise", False))
                checks.append((f"{label}: (raised {type(exc).__name__}: {str(exc)[:80]})", False))
                return
            dt = time.time() - t0
            checks.append((f"{label}: build_graph completes (no crash)", True))
            checks.append((f"{label}: completes in a sane bound (no hang, {dt:.2f}s < {HANG_CEILING_S}s)",
                           dt < HANG_CEILING_S))
            try:
                assert_graph(g)
            except Exception as exc:    # an assertion helper itself blowing up is a FAIL, not a crash
                checks.append((f"{label}: post-graph assertions run cleanly", False))
                checks.append((f"{label}: (assert raised {type(exc).__name__}: {str(exc)[:80]})", False))
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def _files(g):
        return {n["path"] for n in g["nodes"] if n["kind"] == "file"}

    # ---- 1. Deeply nested syntax (recursion / stack risk) -----------------------------------------
    # Thousands of nested brackets/parens. CPython's tokenizer caps nesting depth → SyntaxError, which
    # extract_file_py already catches; the tree-sitter walkers are ITERATIVE (explicit stack) so a deep
    # tree cannot blow the Python stack, and any escape is caught by build_graph's per-file guard. We
    # DOCUMENT graceful handling: never raise, and a sane (tiny) graph — no explosion.
    def _deep(d):
        _w(os.path.join(d, "deep.py"), "x = " + "(" * 40_000 + "1" + ")" * 40_000 + "\n")
        _w(os.path.join(d, "deep.js"), "var x = " + "(" * 20_000 + "1" + ")" * 20_000 + ";\n")
        _w(os.path.join(d, "deep.go"), "package p\nfunc f(){ _ = " + "(" * 20_000 + "1" + ")" * 20_000 + " }\n")
        _w(os.path.join(d, "Deep.cpp"), "int " + "(" * 4_000 + "f" + ")" * 4_000 + "(){return 1;}\n")
        _w(os.path.join(d, "anchor.py"), "def anchor():\n    pass\n")  # a sane file must still appear

    def _deep_ok(g):
        # never-explode: a handful of deeply-nested files yields a SMALL graph (no per-bracket nodes).
        assert len(g["nodes"]) < 5_000, f"node explosion on deep nesting: {len(g['nodes'])}"
        assert len(g["edges"]) < 5_000, f"edge explosion on deep nesting: {len(g['edges'])}"
        assert any(p.endswith("anchor.py") for p in _files(g)), "a normal file dropped alongside deep ones"
    _run("deep-nesting", _deep, _deep_ok)

    # ---- 2. A single multi-megabyte line, no newlines (ReDoS / O(n^2) bait on the regex passes) ----
    # Under the size cap → NOT skipped → flows through extract_file_* AND the schema/config regex passes
    # (which run outside the per-file try/except). A catastrophic-backtracking regex would HANG here.
    def _bigline(d):
        _wb(os.path.join(d, "huge.py"), b"x = \"" + b"A" * BIG_LINE + b"\"\n")
        _wb(os.path.join(d, "huge.sql"), b"CREATE TRIGGER " + b"a" * BIG_LINE + b"\n")      # re.S [^;]*? bait
        _wb(os.path.join(d, "huge2.sql"), b"SELECT " + b"x" * BIG_LINE + b" FROM t\n")      # DML regex bait
        _wb(os.path.join(d, "Huge.java"), b"@Entity " + b" " * BIG_LINE + b"class X {}\n")  # JPA re.S bait
        _wb(os.path.join(d, "huge.yaml"), b"environment:\n  - " + b"A" * BIG_LINE + b"\n")  # config regex bait
        _wb(os.path.join(d, "Huge.dockerfile"), b"ENV " + b"A" * BIG_LINE + b"=v\n")        # dockerfile bait

    def _bigline_ok(g):
        # bounded: a few giant single-line files cannot mint thousands of nodes/edges.
        assert len(g["nodes"]) < 2_000, f"node explosion on multi-MB line: {len(g['nodes'])}"
        assert len(g["edges"]) < 2_000, f"edge explosion on multi-MB line: {len(g['edges'])}"
    _run("multi-MB-single-line", _bigline, _bigline_ok)

    # ---- 3. Broken / incomplete syntax in EACH supported language (tree-sitter ERROR nodes) --------
    # A truncated / garbage file must yield a PARTIAL graph (at least the bare file node), NEVER a raise.
    # Robust to a grammar being absent: either way the file is walked and accounted for (parsed-partial,
    # bare-noded, or — if its ext is grammar-only and that grammar is missing — a bare node).
    def _broken(d):
        _w(os.path.join(d, "b.py"), "def (:\n  class {{{ import from return ;;; lambda yield\n")  # py: SyntaxError path
        _w(os.path.join(d, "b.js"), "function f( { return ;;; const class import from {{{[[[\n")
        _w(os.path.join(d, "b.ts"), "class { function ( : => from import }} extends extends\n")
        _w(os.path.join(d, "b.go"), "package func ){{ import \"\" type type ()()( struct interface\n")
        _w(os.path.join(d, "B.java"), "class { void ( { } public import package extends extends {{\n")
        _w(os.path.join(d, "b.rb"), "class def def end end if while begin rescue ensure %%%\n")
        _w(os.path.join(d, "B.php"), "<?php class function { ( } namespace use trait :: ->->-> \n")
        _w(os.path.join(d, "B.cs"), "class { void ( { } namespace using public => => \n")
        _w(os.path.join(d, "b.rs"), "fn ( { } struct impl trait use pub pub -> -> ::: <<< >>>\n")
        _w(os.path.join(d, "b.cpp"), "class { int ( { } namespace template<<< >>> :: -> ((( )))\n")
        _w(os.path.join(d, "b.c"), "int ( { } struct # include <<< >>> ((( ))) ;;; ===\n")
        _w(os.path.join(d, "B.kt"), "fun ( { } class object interface import package <<< >>> ?: \n")
        _w(os.path.join(d, "B.swift"), "func ( { } class struct protocol import enum <<< >>> ?? -> \n")
        _w(os.path.join(d, "b.html"), "<html><script src= <<< >>> <div <span <<<unclosed <a href=\n")

    def _broken_ok(g):
        fp = _files(g)
        # Every broken source file must be accounted for as a file node (partial graph, never dropped to
        # a crash). Each ext is in _SOURCE_EXT (parsed-partial, bare-noded, or unknown-noded) so it nodes.
        for fname in ("b.py", "b.js", "b.ts", "b.go", "B.java", "b.rb", "B.php", "B.cs",
                      "b.rs", "b.cpp", "b.c", "B.kt", "B.swift", "b.html"):
            assert any(p.endswith(fname) for p in fp), f"broken {fname} produced NO file node (partial graph lost)"
    _run("broken-syntax-all-langs", _broken, _broken_ok)

    # ---- 4. Zero-width / control / RTL-override unicode + very long identifiers (symbols AND paths) -
    def _unicode(d):
        # zero-width space + RTL override inside a python identifier (invalid → SyntaxError; still noded)
        _wb(os.path.join(d, "zw.py"), "def f​oo‮():\n    pass\n".encode("utf-8"))
        # RTL override in a Go identifier (parsed if grammar present; bare-noded otherwise)
        _wb(os.path.join(d, "rtl.go"), "package p\nfunc N‮ame(){}\n".encode("utf-8"))
        # a 1 MB function name (long-identifier stress; still under the size cap)
        _wb(os.path.join(d, "long.js"), b"function " + b"a" * 1_000_000 + b"(){return 1}\n")
        # control chars sprinkled through valid-ish python
        _wb(os.path.join(d, "ctrl.py"), b"def f():\x07\x08\x0b\x0c\n    pass\n")
        # RTL-override + zero-width INSIDE the path components themselves
        _wb(os.path.join(d, "pkg‮​/m​od.py"), "def g():\n    pass\n".encode("utf-8"))
        _w(os.path.join(d, "anchor.py"), "def anchor():\n    pass\n")

    def _unicode_ok(g):
        fp = _files(g)
        # never-crash + the unicode-in-PATH file survives. (Its basename embeds a zero-width space:
        # the bytes round-trip verbatim — `m​od.py` — so we match the tail AFTER the zero-width
        # char and confirm the RTL-override (U+202E) survived in the path, proving raw unicode paths
        # are carried, not mangled/dropped.)
        assert any(p.endswith("od.py") and "‮" in p for p in fp), "unicode-in-path file dropped"
        assert any(p.endswith("anchor.py") for p in fp), "normal file dropped alongside unicode ones"
        # no explosion from a 1 MB identifier (at most a couple of symbols per file)
        assert len(g["nodes"]) < 1_000, f"node explosion on unicode/long-id: {len(g['nodes'])}"
    _run("unicode-control-rtl-longid", _unicode, _unicode_ok)

    # ---- 5. Binary / garbage bytes behind a SOURCE extension (no NUL in first probe → not size/NUL-skip)
    def _garbage(d):
        # 10 KB of non-NUL high bytes (invalid UTF-8, no NUL in first 8 KB → passes the binary probe);
        # the per-file extractor must tolerate the decode failure and still emit the bare file node.
        _wb(os.path.join(d, "garbage.py"), bytes(b for b in range(1, 256) if b != 0) * 40)
        _wb(os.path.join(d, "garbage.go"), bytes(b for b in range(1, 256) if b != 0) * 40)
        _w(os.path.join(d, "anchor.py"), "def anchor():\n    pass\n")

    def _garbage_ok(g):
        assert any(p.endswith("anchor.py") for p in _files(g)), "normal file dropped alongside garbage"
    _run("garbage-bytes-source-ext", _garbage, _garbage_ok)

    # ---- 6. Pathological paths: very deep dir nesting + odd characters --------------------------------
    def _paths(d):
        deep = "/".join("d%d" % i for i in range(200)) + "/leaf.py"   # 200-level deep tree
        _w(os.path.join(d, deep), "def leaf():\n    pass\n")
        _w(os.path.join(d, "weird .dir/sub..dir/has space.py"), "def s():\n    pass\n")  # spaces + double dots
        _w(os.path.join(d, "anchor.py"), "def anchor():\n    pass\n")

    def _paths_ok(g):
        fp = _files(g)
        assert any(p.endswith("leaf.py") for p in fp), "deeply-nested file dropped"
        assert any("has space.py" in p for p in fp), "odd-char path file dropped"
    _run("pathological-paths", _paths, _paths_ok)

    # ---- 7. REGRESSION (the real bug this audit fixed): CRLF YAML / .env env-block ---------------------
    # A config file with `\r\n` line endings (Windows default) holding an `environment:` / `env:` block
    # preceded by ≥1 CRLF line raised an unhandled StopIteration out of _cg_config._yaml_env_var_names
    # (whole-text byte offset ↔ splitlines() line index desynced because `\r\n` is one line but the
    # offset math counted one char). That StopIteration either (a) was silently absorbed as os.walk
    # end-of-iteration → a PARTIAL config graph (later files/keys dropped), or (b) escaped build_graph
    # entirely → a tenant-wide ingest CRASH. The fix iterates physical lines directly (no offset math).
    # This fixture is the exact pre-fix trigger; it must now neither crash nor drop a sibling's key.
    def _crlf(d):
        _wb(os.path.join(d, "compose.yaml"),
            b"version: \"3\"\r\nservices:\r\n  web:\r\n    environment:\r\n"
            b"      - SHARED_CRLF_TOKEN=secret\r\n      - OTHER_CRLF_VAR=x\r\n")
        # a top-level env block preceded by CRLF lines — the most direct StopIteration trigger
        _wb(os.path.join(d, "render.yml"),
            b"a: 1\r\nb: 2\r\nenvironment:\r\n  - TOP_CRLF_VAR=1\r\n")
        # a sibling config whose key MUST survive (proves the walk was not silently truncated)
        _wb(os.path.join(d, "settings.json"), b'{"crlf_survivor_key": 1}\n')
        # a code file that reads one of the CRLF-declared env vars — the coupling must still form
        _w(os.path.join(d, "reader.py"), 'import os\ndef r():\n    return os.environ["SHARED_CRLF_TOKEN"]\n')

    def _crlf_ok(g):
        ckeys = {n.get("name") for n in g["nodes"] if n.get("kind") == "config_key"}
        cedges = [e for e in g["edges"] if e["kind"] == "reads_config"]
        # the walk completed (the sibling AFTER the CRLF files was reached → no silent truncation)
        assert "crlf_survivor_key" in ckeys, "CRLF config file silently truncated the walk (sibling key lost)"
        # the CRLF env-block NAME was captured (recall preserved on Windows line endings)
        assert "SHARED_CRLF_TOKEN" in ckeys, "CRLF `environment:` block env-var name not captured"
        assert "TOP_CRLF_VAR" in ckeys, "top-level CRLF `environment:` block name not captured"
        # and the reader couples to it (the moat fires on a CRLF-authored repo)
        assert any(e["src"].endswith("reader.py") and e["dst"] == "SHARED_CRLF_TOKEN" for e in cedges), \
            "code reading a CRLF-declared env var did not couple to it"
    _run("crlf-yaml-env-block (regression)", _crlf, _crlf_ok)

    # DETERMINISTIC unit-level regression guard. Through build_graph the bug's manifestation is
    # filesystem-walk-ORDER dependent (the escaping StopIteration is sometimes silently absorbed as
    # os.walk end-of-iteration rather than crashing), so the end-to-end check above can pass even on
    # buggy code on some platforms. The CONFIG LAYER is where the bug lives, so we hit it directly:
    # a CRLF env-block text must return cleanly (pre-fix it raised StopIteration here, every time, on
    # every platform). This is the check with teeth — it fails hard on the un-fixed function.
    crlf_text = "a: 1\r\nb: 2\r\nenvironment:\r\n  - UNIT_CRLF_VAR=1\r\n"
    try:
        names = _CFG._yaml_env_var_names(crlf_text)
        checks.append(("crlf regression (unit): _yaml_env_var_names does NOT raise on CRLF env-block",
                       True))
        checks.append(("crlf regression (unit): the CRLF env-block name IS captured",
                       "UNIT_CRLF_VAR" in names))
    except Exception as exc:
        checks.append((f"crlf regression (unit): _yaml_env_var_names does NOT raise on CRLF env-block "
                       f"(raised {type(exc).__name__})", False))
    try:
        keys = _CFG._config_keys(".yaml", crlf_text)
        checks.append(("crlf regression (unit): _config_keys('.yaml', CRLF) does NOT raise",
                       True and "UNIT_CRLF_VAR" in keys))
    except Exception as exc:
        checks.append((f"crlf regression (unit): _config_keys('.yaml', CRLF) does NOT raise "
                       f"(raised {type(exc).__name__})", False))

    return checks


def i18n_encoding_recovery_checks():
    """i18n CORRECTNESS audit (worldwide repos): the extractor must RECOVER symbols — name + content-free
    line span — from non-English / non-ASCII / non-UTF-8 source, not merely avoid crashing. The fix this
    audit landed: `extract_file_py` read with a STRICT `encoding='utf-8'`, so a Python file in any other
    encoding hit a UnicodeDecodeError → returned ok=False → kept a bare file node but DROPPED EVERY symbol.
    Real-world impact: a shift_jis repo (ubiquitous in Japan), a latin-1 / gb18030 repo, or a UTF-8/UTF-16
    BOM file (common on Windows / legacy editors) lost ALL coupling signal — the product silently went blind
    on those files. The read now honors the PEP-263 coding cookie + strips BOMs (`tokenize.open` + a UTF-16
    fallback), never raising. These checks have TEETH: they assert the SYMBOL is recovered, and that NO file
    BODY (a string / comment) ever leaks into the graph (content-free, even for a unicode file).

    Separate from `adversarial_source_checks`'s unicode case, which only proves never-crash on INVALID
    unicode (zero-width/RTL → SyntaxError → noded). Here the source is VALID and the symbol must SURVIVE."""
    import json
    checks = []

    d = tempfile.mkdtemp(prefix="veripsa_i18n_")
    try:
        # (1) VALID unicode identifiers in a UTF-8 Python file (Python 3 allows non-ASCII names).
        _wb(os.path.join(d, "uni_id.py"),
            "def 関数():\n    return 1\n\n\nclass Café:\n    def método(self):\n        pass\n".encode("utf-8"))
        # (2) latin-1 with a PEP-263 cookie + a latin-1 char in a STRING BODY (must parse, body must NOT leak).
        _wb(os.path.join(d, "latin1.py"),
            "# -*- coding: latin-1 -*-\ndef greet_latin():\n    return \"caf\xe9_body\"\n".encode("latin-1"))
        # (3) shift_jis with a Japanese COMMENT (body) + an ASCII symbol — the ubiquitous Japan-repo case.
        _wb(os.path.join(d, "sjis.py"),
            ("# -*- coding: shift_jis -*-\n# 日本語コメント本文\ndef handler_sjis():\n    pass\n").encode("shift_jis"))
        # (4) gb18030 (Chinese) with a cookie + a Chinese comment body.
        _wb(os.path.join(d, "gb.py"),
            ("# -*- coding: gb18030 -*-\n# 中文注释内容\ndef chinese_fn():\n    pass\n").encode("gb18030"))
        # (5) UTF-8 BOM prefix (Windows / legacy editors) — strict utf-8 used to choke on the BOM.
        _wb(os.path.join(d, "bom8.py"), b"\xef\xbb\xbf" + "def bom8_fn():\n    pass\n".encode("utf-8"))
        # (6) UTF-16 (LE, with BOM — the default `utf-16` codec) — used to be DROPPED as binary (NUL probe).
        _wb(os.path.join(d, "u16le.py"), "def u16le_fn():\n    pass\n".encode("utf-16"))
        # (7) UTF-16-BE WITH a BOM (what editors actually write) — must also be recovered, not binary-skipped.
        _wb(os.path.join(d, "u16be.py"), ("﻿" + "class U16BEClass:\n    pass\n").encode("utf-16-be"))
        # anchor: a plain file must coexist (no collateral regression).
        _wb(os.path.join(d, "anchor.py"), b"def anchor_fn():\n    pass\n")

        try:
            g = X.build_graph(d)
        except Exception as exc:
            checks.append((f"i18n: build_graph does NOT raise on non-UTF-8 source (raised {type(exc).__name__})", False))
            return checks

        def _sym(name):
            return any(n.get("name") == name and n.get("kind") in ("def", "class") for n in g["nodes"])

        def _has_span(name):
            return any(n.get("name") == name and isinstance(n.get("start_line"), int)
                       and isinstance(n.get("end_line"), int) for n in g["nodes"])

        # SYMBOL RECOVERY (the teeth) — each non-ASCII / non-UTF-8 file's symbol must be in the graph.
        checks.append(("i18n: valid unicode Python identifier `関数` extracted (def)", _sym("関数")))
        checks.append(("i18n: valid unicode Python class `Café` extracted", _sym("Café")))
        checks.append(("i18n: unicode method `método` extracted with a content-free line span", _has_span("método")))
        checks.append(("i18n: latin-1 (PEP-263 cookie) source — `greet_latin` recovered, not symbol-dropped", _sym("greet_latin")))
        checks.append(("i18n: shift_jis (Japan) source — `handler_sjis` recovered, not symbol-dropped", _sym("handler_sjis")))
        checks.append(("i18n: gb18030 (Chinese) source — `chinese_fn` recovered, not symbol-dropped", _sym("chinese_fn")))
        checks.append(("i18n: UTF-8 BOM source — `bom8_fn` recovered (BOM stripped, not a parse drop)", _sym("bom8_fn")))
        checks.append(("i18n: UTF-16 LE+BOM source — `u16le_fn` recovered (not binary-skipped)", _sym("u16le_fn")))
        checks.append(("i18n: UTF-16 BE+BOM source — `U16BEClass` recovered (not binary-skipped)", _sym("U16BEClass")))
        checks.append(("i18n: a plain ASCII file still parses alongside the non-ASCII ones (no regression)", _sym("anchor_fn")))

        # CONTENT-FREE (the hard constraint): NO file BODY — string literal or comment — ever in the graph.
        blob = json.dumps(g, ensure_ascii=False)
        body_markers = ("café_body", "caf\xe9_body", "日本語コメント本文", "中文注释内容")
        leaked = [m for m in body_markers if m in blob]
        checks.append((f"i18n: content-free — no file BODY (string/comment) leaked from a unicode file (leaked={leaked})",
                       not leaked))
    finally:
        shutil.rmtree(d, ignore_errors=True)

    # SURFACE SAFETY: non-ASCII metadata (path / branch / login) flowing to the CUSTOMER SURFACE must escape
    # safely, render, and stay content-free. Imports render's sinks directly (no DB). A unicode value passes
    # through (correct — GitHub renders UTF-8); the escaping must still neutralize active markdown/HTML and a
    # backtick can never break a code span (layout safety), no matter the surrounding unicode.
    try:
        sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "github-app"))
        import render_safe as RS
        # (a) a CJK + emoji + combining-mark path inside a code span must FENCE around an embedded backtick.
        tricky_path = "ソース/café́/`malicious`.py 🚀"
        coded = RS._code(tricky_path)
        checks.append(("i18n surface: a backtick inside a unicode path does NOT break the code span (fenced)",
                       coded.startswith("``") and tricky_path in coded))
        # (b) a branch with an HTML-injection + RTL override in a NON-code context must escape `<`/`>`.
        rtl_branch = "feature/‮<img src=x onerror=alert(1)>‬-ブランチ"
        safe = RS._safe(rtl_branch)
        checks.append(("i18n surface: HTML in a unicode/RTL branch name is neutralized (no `<`/`>` survives)",
                       "<img" not in safe and "&lt;img" in safe))
        # (c) a non-ASCII author login passes through unchanged (no mojibake / double-encode) and is content-safe.
        login = "開発者-α"
        checks.append(("i18n surface: a non-ASCII author login renders unchanged (no mojibake / double-encode)",
                       RS._safe(login) == login and RS._safe_agent(login) == login))
    except Exception as exc:
        checks.append((f"i18n surface: render sinks import + run cleanly (raised {type(exc).__name__}: {str(exc)[:80]})", False))

    return checks


def unicode_normalization_and_spoof_checks():
    """audit:unicode — NORMALIZATION CONSISTENCY + bidi/zero-width SPOOF + homoglyph cross-tenant, across the
    whole path (extractor → graph coordinate → claim/collision join → rendered surface → tenant key).

    Two REAL holes this audit fixed, plus two invariants it pins (probes b/d, proven sound):

      (a) NFC vs NFD FALSE-MISS (FIXED). A path/symbol is the SAME logical file yet byte-DIFFERENT across the
          two graph sources: the EXTRACTOR (a filesystem walk / git tarball — git preserves whatever bytes were
          committed, e.g. NFD `café/résumé.py`) vs the WEBHOOK (GitHub reports NFC). The engine's collision /
          coupling join is `path = ANY(touched)` — Postgres CODEPOINT equality — so an NFC vs NFD pair did NOT
          match → a FALSE MISS (two PRs on the 'same' file not seen to collide) + a DUPLICATE node on patch
          (the NFD node is not DELETEd, an NFC twin is re-INSERTed). Fixed: build_graph folds every coordinate
          (path / id / name / src / dst) to NFC; the webhook's changed/removed paths are folded to NFC too
          (server._code_paths / _push_changed_sets). Both sources now land on the ONE canonical form git uses.

      (c) BIDI / ZERO-WIDTH SURFACE SPOOF (FIXED). A customer-controlled path/branch/login flows verbatim onto
          the PR surface. A bidirectional OVERRIDE (U+202E RLO — the 'Trojan Source' trick) makes the text after
          it render REVERSED, so `safe<RLO>evil.py` displays benign; a ZERO-WIDTH char (ZWSP/BOM/…) splits a
          label invisibly so two names differ in bytes but look identical. Both defeat the surface's job: a
          FAITHFUL, content-free display. Fixed: render_safe._oneline (the shared first layer of _code/_safe)
          now DROPS every bidi-control / zero-width / invisible FORMAT (Cf) char.

      (b) emoji / astral / combining / zero-width in a PATH or SYMBOL (SOUND): the extractor never crashes and
          preserves the codepoints intact; the DB caps use Postgres left()/length() which are CODEPOINT-based,
          so a multi-byte char can never be split mid-sequence (no mojibake on truncation).

      (d) HOMOGLYPH cross-tenant (SOUND): the tenant ACCOUNT key roots on the IMMUTABLE NUMERIC owner id
          (repository.owner.id, server._event_account_key), NOT the display login — so a look-alike owner name
          (Cyrillic 'а' for Latin 'a') can NEVER collide onto another tenant's ACCT-GH-<id>. The numeric id is
          the wall; the display name only keys the repo coordinate WITHIN that one tenant.
    """
    import json
    checks = []
    here = os.path.dirname(os.path.abspath(__file__))
    repo_root = os.path.dirname(here)
    sys.path.insert(0, repo_root)
    sys.path.insert(0, os.path.join(repo_root, "github-app"))

    # ---------- (a) NFC/NFD normalization consistency (extractor + webhook → ONE canonical form) ----------
    d = tempfile.mkdtemp(prefix="veripsa_nfc_")
    try:
        nfc_dir, nfc_file = "café", "résumé.py"                      # composed (é = U+00E9)
        nfd_dir = unicodedata.normalize("NFD", nfc_dir)               # decomposed (e + U+0301)
        nfd_file = unicodedata.normalize("NFD", nfc_file)
        sub = os.path.join(d, nfd_dir)                               # commit the tree in NFD (the byte-different source)
        os.makedirs(sub, exist_ok=True)
        _wb(os.path.join(sub, nfd_file), b"def handler():\n    pass\n")
        g = X.build_graph(d)
        file_nodes = [n for n in g["nodes"] if n.get("kind") == "file"]
        graph_path = file_nodes[0]["path"] if file_nodes else ""
        # the SAME logical file, as GitHub's webhook reports it (NFC):
        webhook_path = nfc_dir + "/" + nfc_file
        checks.append(("unicode(a): extractor emits the file path in NFC (canonical), not the on-disk NFD bytes",
                       graph_path == unicodedata.normalize("NFC", graph_path)))
        checks.append(("unicode(a): an NFD-committed path now BYTE-MATCHES the NFC webhook path (the `path = ANY` join hits — no false miss)",
                       graph_path == webhook_path))
        # symbol ids (path::name) are normalized too (so a symbol-level claim/collision matches):
        sym_ids = [n.get("id") for n in g["nodes"] if n.get("kind") in ("def", "class")]
        checks.append(("unicode(a): symbol coordinate ids are NFC-normalized (path::name folds the path component too)",
                       all(sid == unicodedata.normalize("NFC", sid) for sid in sym_ids if isinstance(sid, str))))
    finally:
        shutil.rmtree(d, ignore_errors=True)

    # webhook-side: a PR whose diff carries the path in NFD must fold to the SAME NFC the graph stores.
    try:
        import server as SRV
        nfd_changed = unicodedata.normalize("NFD", "café/résumé.py")
        out = SRV._code_paths([nfd_changed])
        checks.append(("unicode(a): server._code_paths folds a changed path to NFC (webhook side meets the graph)",
                       out == ["café/résumé.py"]))
        changed, removed, _ = SRV._push_changed_sets(
            {"commits": [{"modified": [unicodedata.normalize("NFD", "café/a.py")],
                          "removed": [unicodedata.normalize("NFD", "café/b.py")]}]})
        checks.append(("unicode(a): server._push_changed_sets folds push changed/removed paths to NFC (patch DELETE matches)",
                       changed == ["café/a.py"] and removed == ["café/b.py"]))
        # idempotency: an ALREADY-NFC path is unchanged (re-normalizing is a no-op).
        checks.append(("unicode(a): NFC normalization is idempotent (an already-NFC path is byte-unchanged)",
                       SRV._code_paths(["café/x.py"]) == ["café/x.py"]))
    except Exception as exc:
        checks.append((f"unicode(a): server path sinks import + normalize cleanly (raised {type(exc).__name__}: {str(exc)[:80]})", False))

    # ---------- (b) emoji / astral / combining / zero-width path + symbol → never crash, codepoints intact ----------
    d2 = tempfile.mkdtemp(prefix="veripsa_astral_")
    try:
        os.makedirs(os.path.join(d2, "pkg📦"), exist_ok=True)
        # astral letters aren't valid Python identifiers → SyntaxError → bare file node (degrade, never crash);
        # the PATH (emoji + astral + a zero-width joiner) must survive intact.
        _wb(os.path.join(d2, "pkg📦", "m𝕒th‍.py"), b"def f():\n    pass\n")
        crashed = False
        try:
            g2 = X.build_graph(d2)
        except Exception:
            crashed = True
        checks.append(("unicode(b): build_graph never crashes on an emoji/astral/zero-width PATH (degrades to a node)", not crashed))
        if not crashed:
            fp = [n["path"] for n in g2["nodes"] if n.get("kind") == "file"]
            # codepoints preserved (the emoji + astral survive; NFC just re-encodes, never truncates).
            checks.append(("unicode(b): the astral/emoji path is preserved intact (no mid-codepoint truncation / mojibake)",
                           any("📦" in p and "𝕒" in p for p in fp)))
    finally:
        shutil.rmtree(d2, ignore_errors=True)

    # ---------- (c) bidi / zero-width SURFACE spoof — stripped before the PR surface; legit values intact ----------
    try:
        import render_safe as RS2
        spoofs = {
            "RLO bidi (Trojan Source)": "safe‮evil.py",   # the after-RLO text renders reversed
            "LRE/PDF embed": "a‫b‬c",
            "isolates": "x⁦y⁩z",
            "LRM/RLM/ALM marks": "p‎q‏r؜s",
            "ZWSP/ZWNJ/ZWJ": "ad​m‌i‍n",
            "BOM / word-joiner": "f﻿i⁠le.py",
        }
        def _has_spoof(s):
            return any(0x202A <= ord(c) <= 0x202E or 0x2066 <= ord(c) <= 0x2069
                       or ord(c) in (0x200E, 0x200F, 0x061C, 0x200B, 0x200C, 0x200D, 0x2060, 0xFEFF, 0x180E)
                       or unicodedata.category(c) == "Cf" for c in s)
        all_clean = True
        for label, v in spoofs.items():
            cleaned = RS2._oneline(v)
            if _has_spoof(cleaned):
                all_clean = False
                checks.append((f"unicode(c): {label} — spoof chars stripped from the surface (got {cleaned!r})", False))
        if all_clean:
            checks.append(("unicode(c): every bidi-override / zero-width / invisible-format char is stripped before the PR surface (no Trojan-Source spoof)", True))
        # specifically: the Trojan-Source RLO is gone and the TRUE suffix is now visible.
        checks.append(("unicode(c): the Trojan-Source RLO is removed so the real path suffix is shown (`safe<RLO>evil.py` → `safeevil.py`)",
                       RS2._oneline("safe‮evil.py") == "safeevil.py"))
        # legit values (ASCII / accented / CJK paths, symbols, branch names) must be BYTE-IDENTICAL (no collateral).
        legit = ["src/app.py", "café/résumé.py", "feat/finer-collision", "alice",
                 "日本語/モジュール.py", "user-名前_42", "a/b/c.py::método"]
        unchanged = [v for v in legit if RS2._oneline(v) == v]
        checks.append((f"unicode(c): legit unicode paths/labels render byte-identical (no over-stripping; {len(unchanged)}/{len(legit)} unchanged)",
                       len(unchanged) == len(legit)))
        # content-free still holds: a bidi/zero-width string never leaks any non-printing char downstream.
        rendered = RS2._code("safe‮evil.py") + RS2._safe("ad​min")
        checks.append(("unicode(c): content-free — no bidi/zero-width char survives into _code/_safe output",
                       not _has_spoof(rendered)))
    except Exception as exc:
        checks.append((f"unicode(c): render sinks import + sanitize cleanly (raised {type(exc).__name__}: {str(exc)[:80]})", False))

    # ---------- (d) HOMOGLYPH cross-tenant: the account key roots on the IMMUTABLE NUMERIC owner id ----------
    try:
        import server as SRV2
        # Two installations whose owner LOGINS are visual homoglyphs ('a' vs Cyrillic 'а' U+0430) but whose
        # NUMERIC owner ids differ. The tenant key MUST come from the numeric id — never the login — so the
        # look-alike can NOT cross onto the other tenant's ACCT-GH-<id>.
        latin = {"repository": {"owner": {"login": "acme", "id": 111}}}
        cyril = {"repository": {"owner": {"login": "аcme", "id": 222}}}     # 'асme' — looks identical
        k_latin = SRV2._event_account_key(latin)
        k_cyril = SRV2._event_account_key(cyril)
        checks.append(("unicode(d): the tenant key is the NUMERIC owner id, not the display login (homoglyph-proof)",
                       k_latin == "111" and k_cyril == "222"))
        checks.append(("unicode(d): two homoglyph owner LOGINS resolve to DIFFERENT tenant keys (no cross-tenant collision)",
                       k_latin != k_cyril))
        # and the SAME numeric id with a different (renamed) login still keys the SAME tenant (id is stable).
        renamed = {"repository": {"owner": {"login": "acme-renamed", "id": 111}}}
        checks.append(("unicode(d): a RENAMED owner login with the same numeric id keys the SAME tenant (id is the stable wall)",
                       SRV2._event_account_key(renamed) == k_latin))
    except Exception as exc:
        checks.append((f"unicode(d): server._event_account_key imports + keys by numeric id cleanly (raised {type(exc).__name__}: {str(exc)[:80]})", False))

    return checks


def adversarial_schema_checks():
    """Never-crash / never-hang / never-explode audit of the SCHEMA-graph pass (_cg_schema) against
    ATTACKER/CUSTOMER-controlled `.sql` DDL + ORM source.

    DISTINCT surface from the config/code passes: `_schema_graph` reads each .sql / source file in FULL
    and routes it through the DDL/DML/ORM regexes — and it is called bare at build_graph's tail, OUTSIDE
    the per-file parse try/except. It now consumes the SAME guarded (path, ext) list build_graph computes
    (so an OVERSIZED / binary-renamed / generated .sql is filtered out before this pass ever reads it —
    fixed: the pass no longer re-walks independently and bypasses those caps). A pathological `.sql` UNDER
    the size cap still reaches the regexes, so it must NEVER raise (it would crash the WHOLE tenant's
    ingest, not just that file), never hang (ReDoS on the DDL / DML / ORM regexes), and never explode the
    graph.

    Probed: malformed/incomplete CREATE TABLE; SQL comments + CRLF line endings (the #69 class — checked
    on the schema parser, not just config); deeply-nested parens + pathological column defs; multi-
    statement files; weird/long/unicode + quoted identifiers; ORM models with unusual patterns (dynamic
    table names, inheritance, no __tablename__); a `.sql` that is actually binary garbage; a multi-MB
    single line as ReDoS bait on every schema regex.

    The ONE real finding fixed by this audit: UNBOUNDED OUTPUT. A single `.sql` UNDER the size cap (so it
    is NOT size-skipped), packed with distinct `CREATE TABLE t1 … tN;`, minted N table nodes + N edges
    with no ceiling — 67k tables from a ~1.4 MB file, blowing past the graph's bounded-output budget and
    amplifying the downstream O(pairs) shared-resource adjacency. Fixed minimally in _cg_schema.py with a
    per-repo table ceiling (_MAX_TABLES); the schema analogue of _FILE_SIZE_CAP. Real schemas (hundreds →
    low thousands of tables) are never clipped; a flood is truncated, not crashed.

    Each case asserts: build_graph (a) never raises, (b) finishes well under a sane wall-clock bound (no
    hang/ReDoS), (c) returns a bounded graph (no node/edge explosion)."""
    import time

    HANG_CEILING_S = 30.0          # any single build_graph below must finish FAR under this (else: a hang)
    BIG_LINE = 1_400_000           # just UNDER _FILE_SIZE_CAP (1.5 MB) — so the file PASSES the shared size
    #                                guard and IS read in FULL by the schema pass, routing this multi-MB line
    #                                through every schema regex (the real ReDoS surface for this module).

    checks = []

    def _run(label, build_fixture, assert_graph):
        d = tempfile.mkdtemp(prefix="veripsa_sch_")
        try:
            build_fixture(d)
            t0 = time.time()
            try:
                g = X.build_graph(d)
            except Exception as exc:
                checks.append((f"{label}: build_graph does NOT raise", False))
                checks.append((f"{label}: (raised {type(exc).__name__}: {str(exc)[:80]})", False))
                return
            dt = time.time() - t0
            checks.append((f"{label}: build_graph completes (no crash)", True))
            checks.append((f"{label}: completes in a sane bound (no hang, {dt:.2f}s < {HANG_CEILING_S}s)",
                           dt < HANG_CEILING_S))
            try:
                assert_graph(g)
            except Exception as exc:
                checks.append((f"{label}: post-graph assertions run cleanly", False))
                checks.append((f"{label}: (assert raised {type(exc).__name__}: {str(exc)[:80]})", False))
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def _tables(g):
        return {n["name"] for n in g["nodes"] if n.get("kind") == "table"}

    def _no_explosion(g, ntables=2_000, nnodes=5_000, nedges=5_000):
        assert sum(1 for n in g["nodes"] if n.get("kind") == "table") < ntables, "table-node explosion"
        assert len(g["nodes"]) < nnodes, f"node explosion ({len(g['nodes'])})"
        assert len(g["edges"]) < nedges, f"edge explosion ({len(g['edges'])})"

    # ---- 1. Malformed / incomplete DDL: every keyword form truncated or empty -----------------------
    # A `.sql` that is all broken/partial statements must yield a sane (near-empty) graph, never a raise:
    # no table name to capture → no node minted, no orphan edge. (The trigger `[^;]*?` is `;`-bounded so a
    # bare `CREATE TRIGGER x;` can't leak the next statement's `ON t` — that precision must survive here.)
    def _malformed(d):
        _w(os.path.join(d, "bad.sql"),
           "CREATE TABLE\nCREATE TABLE ;\nALTER TABLE\nDROP TABLE  \nCREATE TABLE ((((( \n"
           "CREATE INDEX\nCREATE UNIQUE INDEX CONCURRENTLY\nCREATE TRIGGER\nCREATE TRIGGER x;\n"
           "INSERT INTO\nUPDATE  SET\nDELETE FROM\nSELECT FROM\nCREATE INDEX i ON next_table (c);\n")
        _w(os.path.join(d, "anchor.sql"), "CREATE TABLE real_one (id int);\n")
    def _malformed_ok(g):
        _no_explosion(g, ntables=50, nnodes=100, nedges=100)
        # the one valid table in a sibling file is still captured (broken statements don't poison the walk)
        assert "real_one" in _tables(g), "a valid table dropped alongside malformed DDL"
    _run("schema malformed/incomplete DDL", _malformed, _malformed_ok)

    # ---- 2. HUGE schema: one under-cap .sql packed with DISTINCT tables (the unbounded-output FIX) ----
    # ~1.4 MB (UNDER the 1.5 MB cap → NOT size-skipped) of `CREATE TABLE t<i>;`. Pre-fix this minted one
    # table node + one alters edge PER distinct name (~67k) — an unbounded blowup. Post-fix the per-repo
    # _MAX_TABLES ceiling truncates it; the graph stays bounded and a sibling normal table still appears.
    def _huge(d):
        body, i, size = [], 0, 0
        while size < BIG_LINE:
            s = "CREATE TABLE flood_%d (id int);\n" % i
            body.append(s); size += len(s); i += 1
        _w(os.path.join(d, "flood.sql"), "".join(body))
        _w(os.path.join(d, "ok.sql"), "CREATE TABLE legit_table (id int);\n")
    def _huge_ok(g):
        tn = sum(1 for n in g["nodes"] if n.get("kind") == "table")
        # bounded at (not far above) the ceiling — NOT the ~67k a 1.4 MB flood would otherwise mint
        assert tn <= _SCH._MAX_TABLES + 5, f"schema table-node count not bounded ({tn} > {_SCH._MAX_TABLES})"
        aedges = sum(1 for e in g["edges"] if e["kind"] == "alters")
        assert aedges <= _SCH._MAX_TABLES + 5, f"schema alters-edge count not bounded ({aedges})"
    _run("schema HUGE under-cap .sql (distinct-table flood → bounded output)", _huge, _huge_ok)

    # ---- 3. Pathological column defs + deeply-nested parens in DDL (regex/stack risk) ----------------
    # Tens of thousands of nested parens in a column DEFAULT, plus baroque type/constraint clauses. The
    # regexes are linear and never descend into the body, so this is a near-empty graph, never a raise.
    def _nested(d):
        _w(os.path.join(d, "nested.sql"),
           "CREATE TABLE deep (\n  c int DEFAULT " + "(" * 30_000 + "1" + ")" * 30_000 + ",\n"
           "  d numeric(38,( ( ( 10 ) ) )) CHECK (((((d > 0))))),\n"
           "  e text COLLATE \"C\" GENERATED ALWAYS AS ((((upper(d)))))  STORED\n);\n")
    def _nested_ok(g):
        _no_explosion(g, ntables=50, nnodes=100, nedges=100)
        assert "deep" in _tables(g), "the table with a deeply-nested column def was not captured"
    _run("schema deeply-nested parens + pathological column defs", _nested, _nested_ok)

    # ---- 4. SQL COMMENTS + CRLF line endings (the #69 class, on the SCHEMA parser) -------------------
    # `-- line` and `/* block */` comments interleaved with DDL, the whole file CRLF-terminated (Windows
    # default). The regexes are `re.I`/`re.S` over raw text with no offset↔line math, so CRLF cannot
    # desync them (the #69 StopIteration bug was config-only) — the table is still captured, no raise.
    def _comments_crlf(d):
        _wb(os.path.join(d, "commented.sql"),
            b"-- migration: add the widgets table\r\n"
            b"/* multi\r\n   line\r\n   block comment ON not_a_table */\r\n"
            b"CREATE TABLE widgets ( -- inline comment ON also_not_a_table\r\n"
            b"  id int,\r\n  name text /* trailing */\r\n);\r\n"
            b"ALTER TABLE widgets ADD COLUMN qty int;  -- ON still_not_a_table\r\n")
        # a code file (CRLF too) that queries the commented table — the moat must still couple
        _wb(os.path.join(d, "use.py"),
            b'def q(db):\r\n    return db.run("SELECT id FROM widgets")  # CRLF source\r\n')
    def _comments_crlf_ok(g):
        t = _tables(g)
        assert "widgets" in t, "CRLF/comment-laden .sql lost the real table"
        # comments name words after `ON` — none of those may be mistaken for a table
        for decoy in ("not_a_table", "also_not_a_table", "still_not_a_table"):
            assert decoy not in t, f"a word after ON inside a comment was minted as a table: {decoy}"
        # the moat fires across the CRLF .sql migration and the CRLF code query
        assert any(e["src"].endswith("commented.sql") and e["dst"] == "widgets" and e["kind"] == "alters"
                   for e in g["edges"]), "CRLF .sql ALTER did not produce an alters edge"
        assert any(e["src"].endswith("use.py") and e["dst"] == "widgets" and e["kind"] == "queries"
                   for e in g["edges"]), "CRLF code query did not couple to the table"
    _run("schema SQL comments + CRLF line endings (#69 class)", _comments_crlf, _comments_crlf_ok)

    # ---- 5. Multi-statement file: thousands of small statements in ONE .sql --------------------------
    # Many INSERT/UPDATE/DELETE + CREATE INDEX/TRIGGER statements mixing the SAME handful of real tables.
    # Output is bounded by the DISTINCT table count (de-duped per file), not the statement count.
    def _multi(d):
        parts = []
        for i in range(20_000):
            t = "tbl_%d" % (i % 5)              # only 5 distinct tables across 20k statements
            parts.append("INSERT INTO %s (id) VALUES (%d);\n" % (t, i))
            parts.append("UPDATE %s SET id = id + 1;\n" % t)
        _w(os.path.join(d, "many.sql"), "CREATE TABLE tbl_0 (id int);\n" + "".join(parts))
    def _multi_ok(g):
        # de-dup per file: a handful of tables, never 40k edges from 40k statements
        _no_explosion(g, ntables=50, nnodes=100, nedges=100)
    _run("schema multi-statement file (thousands of statements, few tables)", _multi, _multi_ok)

    # ---- 6. Weird / long / unicode + QUOTED identifiers ----------------------------------------------
    # A 1 MB table name (long-ident stress, still under the cap), a unicode/RTL table name, and quoted
    # vs unquoted forms of the SAME name (`public."Orders"` / `[orders]` / orders) that must COLLIDE on
    # the normalized `orders`. Never raise, never explode, and the quoted/unquoted forms couple.
    def _idents(d):
        _wb(os.path.join(d, "long.sql"), b"CREATE TABLE " + b"a" * 1_000_000 + b" (id int);\n")
        _wb(os.path.join(d, "uni.sql"), "CREATE TABLE t‮able_rtl_éü (id int);\n".encode("utf-8"))
        _w(os.path.join(d, "quoted.sql"),
           'CREATE TABLE public."Orders" (id int);\n'
           'ALTER TABLE [orders] ADD COLUMN note text;\n'
           "INSERT INTO `orders` (id) VALUES (1);\n")
        _w(os.path.join(d, "q.py"), 'def r(db):\n    return db.run("SELECT id FROM orders")\n')
    def _idents_ok(g):
        t = _tables(g)
        # all quote styles + the schema-qualified form normalize to the single `orders`
        assert "orders" in t, "quoted/bracketed/backticked Orders did not normalize to `orders`"
        # the 1 MB identifier did not blow the graph up
        _no_explosion(g, ntables=50, nnodes=100, nedges=100)
        # the plain code query couples to the table declared under three different quote styles
        assert any(e["src"].endswith("q.py") and e["dst"] == "orders" and e["kind"] == "queries"
                   for e in g["edges"]), "code query did not couple to the quoted-identifier table"
    _run("schema weird/long/unicode + quoted identifiers", _idents, _idents_ok)

    # ---- 7. ORM models with unusual patterns ---------------------------------------------------------
    # Dynamic/computed table names (NOT a string literal → must NOT mint), abstract base + inheritance,
    # a model with NO __tablename__, metaclass tricks. Precision-biased patterns must neither raise nor
    # over-mint: only the genuine literal/model-name declarations become tables.
    def _orm(d):
        _w(os.path.join(d, "models.py"),
           "from db import Base\n\n\n"
           "PREFIX = 'app_'\n\n"
           "class AbstractThing(Base):\n"
           "    __abstract__ = True\n"
           "    __tablename__ = PREFIX + 'dynamic'      # computed, NOT a literal → must NOT mint\n\n\n"
           "class NoTableName(Base):\n"
           "    id = 1                                  # no __tablename__, no model-base match here\n\n\n"
           "class RealModel(Base):\n"
           "    __tablename__ = 'real_orm_table'        # the one genuine literal declaration\n\n\n"
           "class DerivedModel(RealModel):\n"
           "    pass                                    # inheritance must not re-mint / explode\n\n\n"
           "class Widget(models.Model):\n"
           "    pass                                    # Django model-name bridge → 'widget'\n")
        # the dynamic table name's would-be literal appears ONLY in an f-string elsewhere — still no mint
        _w(os.path.join(d, "use.py"),
           "table = f'{__import__(\"x\")}'\n"
           "def q(db):\n    return db.run('SELECT 1 FROM real_orm_table')\n")
    def _orm_ok(g):
        t = _tables(g)
        assert "real_orm_table" in t, "a genuine __tablename__ literal was not minted"
        assert "widget" in t, "Django model-name bridge did not mint the model name"
        # computed/dynamic table name must NOT be minted (content-free precision: literals only)
        assert "app_dynamic" not in t and "dynamic" not in t, "a computed table name was minted"
        _no_explosion(g, ntables=50, nnodes=100, nedges=100)
    _run("schema ORM unusual patterns (dynamic name / inheritance / no __tablename__)", _orm, _orm_ok)

    # ---- 8. Binary garbage behind a .sql extension ---------------------------------------------------
    # Non-NUL high bytes (invalid UTF-8) saved as `.sql`. errors="replace" tolerates the decode; the
    # regexes run over the replacement text and find no DDL → a bare file node, never a raise.
    def _garbage(d):
        with open(os.path.join(d, "garbage.sql"), "wb") as fh:
            fh.write(bytes(b for b in range(1, 256) if b != 0) * 4_000)
        _w(os.path.join(d, "anchor.sql"), "CREATE TABLE survivor (id int);\n")
    def _garbage_ok(g):
        assert "survivor" in _tables(g), "a valid .sql dropped alongside binary garbage"
        _no_explosion(g, ntables=50, nnodes=100, nedges=100)
    _run("schema binary garbage behind .sql ext", _garbage, _garbage_ok)

    # ---- 9. Multi-MB single line: ReDoS bait on EVERY schema regex -----------------------------------
    # One giant line per regex family (all under the size cap; the schema pass reads them in full). A
    # catastrophic-backtracking regex would HANG here; all schema regexes are linear, so it is bounded.
    def _redos(d):
        _wb(os.path.join(d, "ddl.sql"), b"CREATE TABLE " + b"a" * BIG_LINE + b"\n")           # DDL ident
        _wb(os.path.join(d, "idx.sql"), b"CREATE INDEX " + b"x " * (BIG_LINE // 2) + b"\n")    # INDEX pre-ON
        _wb(os.path.join(d, "trg.sql"), b"CREATE TRIGGER t " + b"a " * (BIG_LINE // 2) + b"\n")# TRIGGER re.S [^;]*?
        _wb(os.path.join(d, "dml.sql"), b"SELECT " + b"x" * BIG_LINE + b" FROM t\n")           # DML re
        _wb(os.path.join(d, "ent.java"), b"@Entity " + b"a " * (BIG_LINE // 2) + b"class X {}\n")  # JPA re.S
    def _redos_ok(g):
        _no_explosion(g, ntables=50, nnodes=100, nedges=100)
    _run("schema multi-MB single line (ReDoS bait on every schema regex)", _redos, _redos_ok)

    # DETERMINISTIC unit-level bound guard (the check with TEETH — independent of fs-walk noise). Hit
    # _schema_graph directly with a flood and assert the table-node count is capped. Pre-fix this minted
    # one node per distinct name with no ceiling; post-fix it is bounded at _MAX_TABLES, every platform.
    du = tempfile.mkdtemp(prefix="veripsa_sch_unit_")
    try:
        with open(os.path.join(du, "flood.sql"), "w") as fh:
            fh.write("".join("CREATE TABLE u_%d (id int);\n" % i for i in range(_SCH._MAX_TABLES + 5_000)))
        # _schema_graph now takes the GUARDED (path, ext) list build_graph computes (size/binary/generated/
        # symlink already filtered), not a bare skip-dir set — pass it via the real source-file walk.
        snodes, sedges = _SCH._schema_graph(du, list(X._iter_source_files(du)))
        tn = sum(1 for n in snodes if n.get("kind") == "table")
        checks.append(("schema bound (unit): _schema_graph does NOT raise on a distinct-table flood", True))
        checks.append((f"schema bound (unit): table-node count is capped at _MAX_TABLES (got {tn})",
                       tn == _SCH._MAX_TABLES))
        checks.append(("schema bound (unit): alters-edge count is bounded with it",
                       sum(1 for e in sedges if e["kind"] == "alters") <= _SCH._MAX_TABLES))
    except Exception as exc:
        checks.append((f"schema bound (unit): _schema_graph does NOT raise on a flood "
                       f"(raised {type(exc).__name__})", False))
    finally:
        shutil.rmtree(du, ignore_errors=True)

    # And a normal-sized schema is NOT clipped by the ceiling (the fix must not regress real repos).
    dn = tempfile.mkdtemp(prefix="veripsa_sch_normal_")
    try:
        with open(os.path.join(dn, "schema.sql"), "w") as fh:
            fh.write("".join("CREATE TABLE real_%d (id int);\n" % i for i in range(800)))
        snodes, _ = _SCH._schema_graph(dn, list(X._iter_source_files(dn)))
        tn = sum(1 for n in snodes if n.get("kind") == "table")
        checks.append(("schema bound (unit): a normal 800-table schema is NOT clipped (all minted)",
                       tn == 800))
    finally:
        shutil.rmtree(dn, ignore_errors=True)

    return checks


def nodes_and_generated_checks():
    """GAP-14 (honesty) + GAP-15 (precision), in ONE fixture tree:

    GAP-14 — a hand-edited source file in a language we have NO grammar for (.scala / .hs / .gate) must STILL
    appear as a `file` node (language="unknown") so direct (storey-1) collision + unknown-marking are
    complete — but with NO contains/calls/imports edges (we don't invent structure we couldn't parse).

    GAP-15 — generated / vendored code OUTSIDE _SKIP_DIRS must be EXCLUDED entirely (no node, no edge),
    via (a) generated filename suffixes (`*_pb2.py`, `*.pb.go`, `*.gen.go`, `*.min.js`, source maps),
    (b) a generated directory name (`__generated__/`), and (c) `.gitattributes` `linguist-generated` /
    `linguist-vendored` markers. Parsing a generated `*_pb2.py` (valid Python!) would mint synthetic
    symbols whose `calls` adjacency cries wolf on a file nobody hand-edits — so it must produce nothing.

    Fixture tree (real os.walk, real .gitattributes — the matcher reads the file directly, no git needed):
      app/service.py            — real source (kept, parsed; call site names `save` = precision decoy)
      app/models.py             — real source (kept, parsed)
      Payment.scala             — UNSUPPORTED lang source → GAP-14 bare node (language="unknown")
      Helper.hs               — UNSUPPORTED lang source → GAP-14 bare node
      gates.d/example.gate      — release-gate DSL → GAP-14 bare node
      app/proto/order_pb2.py    — GAP-15 protobuf suffix → EXCLUDED (would otherwise mint a `save` symbol)
      gosvc/order.pb.go         — GAP-15 .pb.go suffix → EXCLUDED
      gosvc/wire.gen.go         — GAP-15 .gen.go suffix → EXCLUDED
      web/__generated__/t.py    — GAP-15 generated dir → EXCLUDED
      assets/bundle.min.js      — GAP-15 minified bundle → EXCLUDED
      assets/app.js.map         — GAP-15 source map → EXCLUDED
      app/api/schema_gen.py     — GAP-15 linguist-generated=true (.gitattributes) → EXCLUDED
      thirdparty/leftpad/lp.py  — GAP-15 linguist-vendored (.gitattributes) → EXCLUDED
      migrations/0001_init.py   — NOT excluded (migrations are hand-relevant — kept) [intentional]
    """
    d = tempfile.mkdtemp(prefix="veripsa_nodes_gen_")
    try:
        # real source (a call site that names `save` — to prove the GENERATED proto `save` is what would
        # have created false adjacency, and is now gone).
        _w(os.path.join(d, "app", "service.py"), "from app.models import Order\ndef place_order(o):\n    return Order(o).save()\n")
        _w(os.path.join(d, "app", "models.py"), "class Order:\n    def save(self):\n        return True\n")
        # GAP-14: unsupported-language source (no grammar) — must STILL be a file node.
        _w(os.path.join(d, "Payment.scala"), "package com.acme\nobject Payment {\n  def charge(a: Int): Boolean = a > 0\n}\n")
        _w(os.path.join(d, "Helper.hs"), "module Helper where\nadd :: Int -> Int -> Int\nadd a b = a + b\n")
        _w(os.path.join(d, "gates.d", "example.gate"),
           'register_gate "tests/test_example.py" "EXAMPLE GATE: PASS" "example" "ok" "desc" 16\n')
        # GAP-15a: protobuf-generated python (valid python — WOULD be parsed, minting a synthetic `save`).
        _w(os.path.join(d, "app", "proto", "order_pb2.py"),
           "# Generated by the protocol buffer compiler. DO NOT EDIT!\nclass Order(object):\n    pass\ndef save():\n    return None\n")
        # GAP-15b: go protobuf / wire codegen.
        _w(os.path.join(d, "gosvc", "order.pb.go"), "// Code generated by protoc-gen-go. DO NOT EDIT.\npackage gosvc\ntype Order struct{}\n")
        _w(os.path.join(d, "gosvc", "wire.gen.go"), "// Code generated by Wire. DO NOT EDIT.\npackage gosvc\nfunc Inject() {}\n")
        # GAP-15c: generated DIRECTORY name.
        _w(os.path.join(d, "web", "__generated__", "types.py"), "def synthetic_type():\n    return None\n")
        # GAP-15: minified bundle + source map (synthetic-symbol noise) — EXCLUDED by suffix.
        _w(os.path.join(d, "assets", "bundle.min.js"), "function z(){return 1}\n")
        _w(os.path.join(d, "assets", "app.js.map"), '{"version":3,"sources":["app.js"]}\n')
        # GAP-15d: linguist markers via .gitattributes (the repo's OWN declaration).
        _w(os.path.join(d, "app", "api", "schema_gen.py"), "def generated_handler():\n    return 1\n")
        _w(os.path.join(d, "thirdparty", "leftpad", "lp.py"), "def leftpad(s, n):\n    return s.rjust(n)\n")
        _w(os.path.join(d, ".gitattributes"),
           "# linguist markers (GAP-15)\napp/api/schema_gen.py linguist-generated=true\nthirdparty/** linguist-vendored\n")
        # INTENTIONALLY NOT excluded: a migration (hand-relevant — Django/Rails migrations couple).
        _w(os.path.join(d, "migrations", "0001_init.py"), "def upgrade():\n    return None\n")

        g = X.build_graph(d)
        files = {n["path"] for n in g["nodes"] if n["kind"] == "file"}
        lang_of = {n["path"]: n.get("language") for n in g["nodes"] if n["kind"] == "file"}

        checks = []
        # GAP-14: unsupported-language source IS noded (was dropped entirely before).
        checks.append(("GAP-14: Payment.scala (no grammar) IS a file node",
                       "Payment.scala" in files))
        checks.append(("GAP-14: Helper.hs (no grammar) IS a file node",
                       "Helper.hs" in files))
        checks.append(("GAP-14: gates.d/example.gate (release-gate DSL) IS a file node",
                       "gates.d/example.gate" in files))
        checks.append(("GAP-14: those bare nodes are language='unknown'",
                       lang_of.get("Payment.scala") == "unknown"
                       and lang_of.get("Helper.hs") == "unknown"
                       and lang_of.get("gates.d/example.gate") == "unknown"))
        checks.append(("GAP-14: a bare (unparsed) node has NO outgoing edges (no false structure)",
                       not any(e["src"] in ("Payment.scala", "Helper.hs", "gates.d/example.gate")
                               for e in g["edges"])))
        # GAP-15: generated / vendored files are EXCLUDED entirely.
        checks.append(("GAP-15: *_pb2.py (protobuf) is EXCLUDED (no node)",
                       not any(p.endswith("order_pb2.py") for p in files)))
        checks.append(("GAP-15: *.pb.go is EXCLUDED (no node)",
                       not any(p.endswith("order.pb.go") for p in files)))
        checks.append(("GAP-15: *.gen.go is EXCLUDED (no node)",
                       not any(p.endswith("wire.gen.go") for p in files)))
        checks.append(("GAP-15: __generated__/ dir is EXCLUDED (no node)",
                       not any("__generated__" in p for p in files)))
        checks.append(("GAP-15: *.min.js (minified bundle) is EXCLUDED (no node)",
                       not any(p.endswith("bundle.min.js") for p in files)))
        checks.append(("GAP-15: *.map (source map) is EXCLUDED (no node)",
                       not any(p.endswith("app.js.map") for p in files)))
        checks.append(("GAP-15: linguist-generated=true (.gitattributes) is EXCLUDED",
                       "app/api/schema_gen.py" not in files))
        checks.append(("GAP-15: linguist-vendored (.gitattributes, thirdparty/**) is EXCLUDED",
                       not any("thirdparty" in p for p in files)))
        # GAP-15 anti-cry-wolf payoff: the synthetic `save` from order_pb2.py must NOT create a calls edge.
        checks.append(("GAP-15: no `calls`→`save` edge from the generated proto file (cry-wolf gone)",
                       not any(e["kind"] == "calls" and e["dst"] == "save" and "proto" in e["src"] for e in g["edges"])))
        # precision decoy: the REAL service.py call site to `save` IS still a calls edge (we didn't over-exclude).
        checks.append(("precision decoy: real app/service.py STILL has a `calls`→`save` edge (real coupling kept)",
                       any(e["kind"] == "calls" and e["dst"] == "save" and e["src"].endswith("service.py") for e in g["edges"])))
        # balance: real source must NOT be collateral damage; migrations are intentionally KEPT.
        checks.append(("balance: real app/service.py + app/models.py are KEPT (parsed)",
                       "app/service.py" in files and "app/models.py" in files
                       and lang_of.get("app/service.py") == "python"))
        checks.append(("balance: migrations/0001_init.py is KEPT (migrations are NOT generated-excluded — intentional)",
                       "migrations/0001_init.py" in files))
        return checks
    finally:
        shutil.rmtree(d, ignore_errors=True)


def main():
    d = tempfile.mkdtemp(prefix="veripsa_extract_")
    try:
        _w(os.path.join(d, "a.py"), "from b import thing\nimport os\n\n\ndef run():\n    thing()\n")
        _w(os.path.join(d, "b.py"), "def thing():\n    pass\n")
        _w(os.path.join(d, "svc/auth.go"), 'package svc\nimport "fmt"\nfunc Login(u string){ fmt.Println(u) }\ntype Session struct{}\n')
        _w(os.path.join(d, "svc/api.go"), 'package svc\nfunc Handle(){ Login("x") }\n')
        _w(os.path.join(d, "Account.java"), "class Account { void pay(){ charge(); } }\n")
        _w(os.path.join(d, "worker.rb"), "class Worker\n  def run; perform; end\nend\n")
        _w(os.path.join(d, "Pay.php"), "<?php\nclass Pay { function charge(){ return 1; } }\n")
        _w(os.path.join(d, "Api.php"), "<?php\nclass Api { function handle(){ (new Pay())->charge(); } }\n")
        _w(os.path.join(d, "Report.cs"), "class Report { void Run(){ Compute(); } }\n")
        _w(os.path.join(d, "index.html"), '<html><head><script src="app.js"></script></head></html>\n')
        _w(os.path.join(d, "app.js"), "function go(){ return 1; }\n")
        _w(os.path.join(d, "lib.rs"), "pub fn login(){}\nstruct Session;\n")
        _w(os.path.join(d, "api.rs"), "fn handle(){ login(); }\n")
        _w(os.path.join(d, "csrc/auth.h"), "int verify_user(const char* t);\n")
        _w(os.path.join(d, "csrc/auth.c"), '#include "auth.h"\nint verify_user(const char* t){ return 1; }\n')
        _w(os.path.join(d, "csrc/api.c"), '#include "auth.h"\nint serve_c(const char* t){ return verify_user(t); }\n')
        _w(os.path.join(d, "csrc/shape.cpp"), '#include "auth.h"\nclass Shape { public:\n  int area(){ return verify_user("x"); }\n};\n')
        # Kotlin fixtures: a class with members + a method that calls a top-level function in another
        # file. With a HEALTHY grammar these yield full symbols; with the pinned tree-sitter-kotlin
        # 1.0.0 (which the structural-health probe REJECTS — it returns has_error on every valid .kt)
        # they must degrade to NODE-ONLY (bare file node, no symbols), never a partial/inconsistent
        # salvage. The check block below asserts whichever branch matches the loaded grammar.
        _w(os.path.join(d, "android/Greeter.kt"), "package android\nfun greetKt(name: String): String { return \"Hello\" }\nclass KtGreeter {\n    fun run() { greetKt(\"world\") }\n}\n")
        _w(os.path.join(d, "android/Main.kt"), "package android\nfun mainKt() { greetKt(\"x\") }\n")
        # Swift fixtures: a struct with a method that calls a top-level function defined in another file,
        # plus a protocol (Swift's interface — a separate node type, must still be captured as a class def).
        _w(os.path.join(d, "ios/Greeter.swift"), "import Foundation\nprotocol Greetable {\n    func greet()\n}\nfunc greetSwift(_ name: String) -> String { return \"Hello\" }\nstruct SwiftGreeter {\n    func run() { greetSwift(\"world\") }\n}\n")
        _w(os.path.join(d, "ios/Main.swift"), "import Foundation\nfunc mainSwift() { greetSwift(\"x\") }\n")
        # schema graph fixtures: a migration (DDL) + code that queries the same table; a.py's `from b import`
        # must NOT be mistaken for a SQL `FROM b`.
        _w(os.path.join(d, "db/0007.sql"), "ALTER TABLE orders ADD COLUMN note text;\nCREATE TABLE audit (id int);\n")
        _w(os.path.join(d, "reports.py"), 'def report(db):\n    return db.run("SELECT id FROM orders WHERE id > 0")\n')
        # schema graph (CREATE INDEX): a migration that ONLY indexes `customers` (no CREATE/ALTER TABLE)
        # still TOUCHES that table → it must produce a `customers` table node + an `alters` edge, so code
        # that queries `customers` couples to it. Decoy: the index NAME `idx_orders_total` must NOT become
        # a table (we capture the table AFTER `ON`, not the index name).
        _w(os.path.join(d, "db/0008.sql"),
           "CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS idx_orders_total ON public.customers (total);\n")
        _w(os.path.join(d, "billing.py"), 'def bill(db):\n    return db.run("SELECT total FROM customers")\n')
        # schema graph (CREATE TRIGGER): a migration that ONLY defines a trigger ON `invoices`
        # (no CREATE/ALTER TABLE) still TOUCHES that table → it must produce an `invoices` table
        # node + an `alters` edge, so code that queries `invoices` couples to the migration. Decoys:
        # the trigger NAME `set_invoice_ts` must NOT become a table; a malformed `CREATE TRIGGER`
        # with no ON must NOT leak the FOLLOWING statement's `ON broken_leak` table into the match.
        _w(os.path.join(d, "db/0009.sql"),
           "CREATE OR REPLACE TRIGGER set_invoice_ts BEFORE UPDATE ON public.invoices\n"
           "  FOR EACH ROW EXECUTE FUNCTION touch_ts();\n"
           "CREATE TRIGGER broken_trigger;\nCREATE INDEX i ON broken_leak (c);\n")
        _w(os.path.join(d, "ledger.py"), 'def post(db):\n    return db.run("SELECT id FROM invoices")\n')
        # schema graph (ORM table declarations — most modern apps NEVER write raw DDL, so without
        # ORM coverage the known-table set is empty and the moat silently no-ops). Three ORM forms;
        # in each, a MIGRATION that touches table T (an `alters` source) couples to code that touches
        # T (a `queries` source) on the shared table NAME — exactly the raw-SQL moat, now on ORM apps.
        #
        # (a) SQLAlchemy declarative: `__tablename__ = "subscriptions"` → table node. The model file
        #     is a `queries` participant; a raw .sql migration ALTERs the same table (the common
        #     SQLAlchemy reality: declarative models in code, schema changes shipped as raw SQL DDL).
        #     They couple on `subscriptions` — the moat now fires on a SQLAlchemy app.
        _w(os.path.join(d, "models/subscription.py"),
           "from db import Base\n\n\nclass Subscription(Base):\n"
           "    __tablename__ = \"subscriptions\"\n    id = 1\n")
        _w(os.path.join(d, "migrations/0001_subs.sql"),
           "ALTER TABLE subscriptions ADD COLUMN plan text;\n")
        # (b) Django: a model with NO explicit db_table (Django derives the table from the model
        #     name → `loyaltycard`). A Django migration names the table by its model name. They must
        #     collide on `loyaltycard`. Decoy: `class Mixin(object)` is NOT an ORM model → no node.
        _w(os.path.join(d, "shop/models.py"),
           "from django.db import models\n\n\nclass Mixin(object):\n    pass\n\n\n"
           "class LoyaltyCard(models.Model):\n    points = models.IntegerField()\n")
        _w(os.path.join(d, "shop/migrations/0002_card.py"),
           "from django.db import migrations\n\n\nclass Migration(migrations.Migration):\n"
           "    operations = [migrations.AddField(model_name=\"loyaltycard\", name=\"points\")]\n")
        # (c) JPA: `@Entity` + `@Table(name = "warehouse_item")` → table node `warehouse_item`. A
        #     Flyway-style migration .sql ALTERs it (raw SQL alters an ORM-declared table — the
        #     cross-source case the shared _mint must de-dup, first declarer wins the node path).
        _w(os.path.join(d, "src/InventoryItem.java"),
           "import javax.persistence.*;\n\n@Entity\n@Table(name = \"warehouse_item\")\n"
           "public class InventoryItem {\n    @Id Long id;\n}\n")
        _w(os.path.join(d, "db/migrate/V3__warehouse.sql"),
           "ALTER TABLE warehouse_item ADD COLUMN sku text;\n")
        # (d) ORM precision + raw-query coupling: a JPA `@Entity` with NO @Table → the entity CLASS
        #     name is the table (`shipment`). A plain code file with a raw `SELECT ... FROM shipment`
        #     must couple to it (ORM-minted table is in the known-table set, so pass-2 raw queries
        #     hit it too). Decoy: an ORM model file's own attributes must NOT mint stray tables.
        _w(os.path.join(d, "src/Shipment.java"),
           "import javax.persistence.*;\n\n@Entity\npublic class Shipment {\n    @Id Long id;\n}\n")
        _w(os.path.join(d, "tracking.py"),
           'def track(db):\n    return db.run("SELECT id FROM shipment WHERE id > 0")\n')
        # config graph fixtures: a config file + two code files reading the SAME specific key.
        _w(os.path.join(d, "config.json"), '{"database_pool_size": 5, "feature_new_ui": true, "name": "x"}\n')
        _w(os.path.join(d, "svc.py"), 'def f(cfg):\n    return cfg["database_pool_size"]\n')
        _w(os.path.join(d, "job.py"), 'def g(settings):\n    return settings["database_pool_size"]\n')

        # config graph (ENV-VAR NAME idiom): the line-key regex misses env-var names because they are
        # declared as the VALUE of `key:` (Render/CI blueprints) or as `- NAME=...` items in an
        # `environment:` block (docker-compose / k8s) — NOT as yaml keys. Two code files reading the
        # SAME env var via os.environ are genuinely coupled (no call edge between them). Decoys:
        # `DEPLOY_REGION` is declared but read by only ONE file (no phantom pair); the env VALUE
        # `s3cr3t_TOKEN_VALUE` after `value:` must NOT become a key (content-free: names not values).
        _w(os.path.join(d, "deploy.yaml"),
           "services:\n  - type: web\n    envVars:\n"
           "      - key: SHARED_API_TOKEN     # read by two files → they contend\n"
           "        value: s3cr3t_TOKEN_VALUE\n"
           "      - key: DEPLOY_REGION\n"
           "    environment:\n      - WORKER_QUEUE_NAME=jobs\n")
        _w(os.path.join(d, "api_a.py"), 'import os\ndef a():\n    return os.environ["SHARED_API_TOKEN"]\n')
        _w(os.path.join(d, "api_b.py"), 'import os\ndef b():\n    return os.environ.get("SHARED_API_TOKEN")\n')
        _w(os.path.join(d, "region.py"), 'import os\ndef r():\n    return os.environ.get("DEPLOY_REGION")\n')

        # config graph (`.env` FILE idiom — arguably the MOST common env-var declaration form): a file
        # literally named `.env` is a DOTFILE, so os.path.splitext(".env") yields no `.env` extension —
        # the walk never recognized it and its declared env-var NAMES coupled 0 readers (the gap). A `.env`
        # declares env-var NAMES that app code reads via os.environ — the same differentiated coupling.
        # Two code files reading the SAME name via os.environ are genuinely coupled (no call edge between
        # them). Decoys: `lower_key` (lowercase, ordinary local — NOT swept); `s3cr3t_env_value` (the VALUE
        # after `=` — content-free, never captured); `ONLY_ONE_ENV` read by one file (no phantom pair).
        _w(os.path.join(d, ".env"),
           "# database + auth env vars\n"
           "SHARED_ENV_DSN=postgres://s3cr3t_env_value@db/app   # read by two files → they contend\n"
           "export ONLY_ONE_ENV=1\n"
           "lower_key=ignored\n")
        _w(os.path.join(d, "envread_a.py"), 'import os\ndef a():\n    return os.environ["SHARED_ENV_DSN"]\n')
        _w(os.path.join(d, "envread_b.py"), 'import os\ndef b():\n    return os.environ.get("SHARED_ENV_DSN")\n')
        _w(os.path.join(d, "envread_c.py"), 'import os\ndef c():\n    return os.environ.get("ONLY_ONE_ENV")\n')

        # config graph (Dockerfile idiom): a file named `Dockerfile` has NO extension, so
        # os.path.splitext("Dockerfile")[1] is "" — the same dotfile/no-ext miss that hid `.env`.
        # A Dockerfile DECLARES env-var NAMES via `ENV` and build-arg NAMES via `ARG`; app code
        # reads those NAMES via os.environ — the same differentiated coupling. Forms covered:
        # `ENV K=V`, multiple `K=V` on one `ENV` line, and the legacy space form `ENV K V`; `ARG K`
        # and `ARG K=default`. Two code files reading the SAME ENV name are genuinely coupled (no
        # call edge between them). Decoys: `BUILD_ONLY` (ARG) read by ONE file (no phantom pair);
        # the VALUE `s3cr3t_dock_value` after `=` (content-free — never a key); a lowercase
        # `lowercase_dock` env name (NOT swept); `apt-get`/`/app` on RUN/COPY lines (ignored).
        _w(os.path.join(d, "Dockerfile"),
           "FROM python:3.11\n"
           "ENV SHARED_DOCK_TOKEN=postgres://s3cr3t_dock_value@db/app   # read by two files → contend\n"
           "ENV WORKER_POOL_SIZE=8 CACHE_BACKEND_URL=redis://x   # two K=V on one ENV line\n"
           "ENV LEGACY_SPACE_VAR legacy_value_here   # space form: name is first token only\n"
           "ARG BUILD_ONLY=defaultbuildval   # build arg, read by one file → no phantom pair\n"
           "ENV lowercase_dock=ignored   # lowercase, not an env-style name → NOT swept\n"
           "RUN apt-get update && echo hello\n"
           "COPY . /app\n")
        _w(os.path.join(d, "dockread_a.py"), 'import os\ndef a():\n    return os.environ["SHARED_DOCK_TOKEN"]\n')
        _w(os.path.join(d, "dockread_b.py"), 'import os\ndef b():\n    return os.environ.get("SHARED_DOCK_TOKEN")\n')
        _w(os.path.join(d, "dockread_c.py"), 'import os\ndef c():\n    return os.environ.get("BUILD_ONLY")\n')
        _w(os.path.join(d, "dockread_d.py"), 'import os\ndef d():\n    return os.environ.get("CACHE_BACKEND_URL")\n')

        # config graph PRECISION (dependency-manifest sections): a manifest's `dependencies` /
        # `devDependencies` (package.json) and `[dependencies]` / `[dev-dependencies]` (Cargo.toml,
        # pyproject) lists THIRD-PARTY PACKAGE names, not app config keys. Two files merely importing
        # the SAME package are NOT coupled by it — minting those names as config keys floods
        # reads_config (real-repo: one `supertest_pkg` dep made every express importer false-couple).
        # Skip dep sections; a REAL app config key elsewhere in the SAME manifest must still be minted.
        # Decoys here: `express_pkg` / `supertest_pkg` (npm deps), `reqwest_pkg` (Cargo dep) — none may
        # become keys; `engines_minimum` (a package.json non-dep key) and `release_opt_level` (a Cargo
        # non-dep `[profile.release]` key) MUST still be minted (the skip is section-scoped, not global).
        _w(os.path.join(d, "package.json"),
           '{\n  "name": "demoapp",\n  "engines_minimum": "node18",\n'
           '  "dependencies": {"express_pkg": "^4.0.0", "shared_lib_pkg": "^1.0.0"},\n'
           '  "devDependencies": {"supertest_pkg": "^6.0.0"}\n}\n')
        _w(os.path.join(d, "Cargo.toml"),
           '[package]\nname = "demoapp"\n\n'
           '[dependencies]\nreqwest_pkg = "0.11"\n\n'
           '[profile.release]\nrelease_opt_level = 3\n')
        # two files that both `require` the SAME dep — if the dep leaked as a key, these would couple
        _w(os.path.join(d, "uses_express_a.js"), "const e = require('express_pkg');\nmodule.exports = e;\n")
        _w(os.path.join(d, "uses_express_b.js"), "const x = require('express_pkg');\nmodule.exports = x;\n")

        # PRECISION (real-repo audit, flask): a RELATIVE import must resolve to the SIBLING — not every
        # same-basename file in the repo; an EXTERNAL multi-segment import must not fan out to a local file of
        # the same last segment. (Both were false cross-package couplings → over-warning, the #1 adoption killer.)
        _w(os.path.join(d, "pkg", "__init__.py"), "from .helper import h\n")
        _w(os.path.join(d, "pkg", "helper.py"), "def h():\n    pass\n")
        _w(os.path.join(d, "decoy", "helper.py"), "def h():\n    pass\n")            # same basename, different dir
        _w(os.path.join(d, "consumer.py"), "from ext.libthing.widget import W\n")    # external, multi-segment
        _w(os.path.join(d, "elsewhere", "widget.py"), "def W():\n    pass\n")        # same last segment, local

        # PRECISION (Go stdlib, real-repo audit on hugo): a BARE single-segment Go import (`time`,
        # `strings`) is ALWAYS the standard library — Go local imports are full module paths
        # (`github.com/org/repo/pkg`). It must NOT resolve to a local same-basename file. Decoy:
        # a local tpl/strings/strings.go that `#import "strings"` must NOT fan out to.
        # RECALL (Go package): a Go import names a PACKAGE = a DIRECTORY of .go files (here the local
        # package `myapp/internal/auth`, imported by its FULL module path `github.com/org/repo/...`).
        # It must resolve to EVERY .go file in that package dir (auth.go + token.go), while the stdlib
        # `fmt`/`strings` and an external `go.uber.org/zap` still resolve to nothing.
        _w(os.path.join(d, "gosvc/handler.go"),
           'package gosvc\nimport (\n  "strings"\n  "fmt"\n  "go.uber.org/zap"\n'
           '  "github.com/org/repo/myapp/internal/auth"\n)\n'
           'func H(s string){ fmt.Println(strings.TrimSpace(s)) }\n')
        _w(os.path.join(d, "tpl/strings/strings.go"), "package strings\nfunc Helper(){}\n")  # decoy: local strings.go
        _w(os.path.join(d, "myapp/internal/auth/auth.go"), "package auth\nfunc Verify(t string) bool { return t != \"\" }\n")
        _w(os.path.join(d, "myapp/internal/auth/token.go"), "package auth\nfunc Mint() string { return \"x\" }\n")
        # PRECISION (real-repo audit on gin): a `*_test.go` file is NOT part of the importable package —
        # Go compiles tests into a SEPARATE binary, so production code can never import a `_test.go`. A
        # package import resolving to the dir's test files produced 55/286 (19%) FALSE production→test
        # couplings on gin (context.go → binding/json_test.go, …) = cry-wolf. A package import must resolve
        # to the package's NON-test .go files only, never auth_test.go.
        _w(os.path.join(d, "myapp/internal/auth/auth_test.go"), "package auth\nfunc TestVerify(t string){}\n")
        _w(os.path.join(d, "auth/decoy.go"), "package auth\nfunc D(){}\n")  # decoy: top-level auth/ pkg must NOT win

        # PRECISION (Java FQN, real-repo audit on retrofit): a Java import is a FULLY-QUALIFIED dotted
        # name. `import java.lang.reflect.Type` (the JDK class) must NOT basename-match a local
        # Type.java; a local `import demo.svc.Helper` MUST resolve to the file the FQN names.
        _w(os.path.join(d, "jsrc/demo/svc/Helper.java"), "package demo.svc;\nclass Helper { void go(){} }\n")
        _w(os.path.join(d, "jsrc/demo/app/Main.java"),
           "package demo.app;\nimport demo.svc.Helper;\nimport java.lang.reflect.Type;\n"
           "class Main { void run(){ new Helper(); } }\n")
        _w(os.path.join(d, "jtest/other/Type.java"), "package other;\nclass Type {}\n")  # decoy: local Type.java

        # PRECISION (C/C++ stdlib, real-repo audit on nlohmann/json): a SINGLE-segment ANGLE-bracket
        # include (`#include <array>`) is the C++ standard library — never a local file. (A LOCAL
        # include is quoted: `"util.h"`.) `<array>` must NOT match a local array.cpp.
        _w(os.path.join(d, "ccsrc/widget.cpp"),
           '#include <array>\n#include "ccsrc/util.h"\nint widget(){ return 1; }\n')
        _w(os.path.join(d, "ccsrc/util.h"), "int helper();\n")
        _w(os.path.join(d, "examples/array.cpp"), "int demo(){ return 0; }\n")  # decoy: local array.cpp

        # RE-EXPORT / BARREL (verified node types: a re-export is an `export_statement` carrying a
        # `source` field — the `from '...'` clause — NOT an `import_statement`). A barrel `re_index.ts`
        # that `export * from './ra'` + `export { z } from './rb'` emits ZERO edges in the old extractor
        # → it silently MISSES the coupling exactly where coupling concentrates. Both edges must appear
        # and resolve to the sibling files. DECOYS (precision): `export const LOCAL = 1` and
        # `export { localOnly }` have NO `source` → must emit NO edge (a local export is not a re-export);
        # an external `export * from 'react'` stays a bare module name → must not match a wrong local file.
        _w(os.path.join(d, "barrel/re_index.ts"),
           "export * from './ra';\n"
           "export { z } from './rb';\n"
           "export { x as y } from './ra';\n"
           "export * from 'react';\n"
           "export const LOCAL = 1;\n"
           "const localOnly = 2;\nexport { localOnly };\n")
        _w(os.path.join(d, "barrel/ra.ts"), "export const ra = 1;\n")
        _w(os.path.join(d, "barrel/rb.ts"), "export const rb = 2;\n")

        # FINDING 3 — TS type-level declarations are first-class symbols: an `interface`/`type`/`enum`
        # is exactly the unit a PR edits in isolation, so it MUST mint a symbol node (else a PR touching
        # ONLY an interface body degrades to a file-level collision — inconsistent with Java/C#/Rust/Swift).
        # PRECISION: an enum MEMBER (`Red`) is NOT a top-level symbol (its node type is enum_body, not
        # enum_declaration).
        _w(os.path.join(d, "tsdecl/types.ts"),
           "export interface Shape { area(): number; }\n"
           "export type ID = string;\n"
           "export enum Color { Red, Green }\n")

        # FINDING 4 — a decorated Python def/class span MUST include the `@decorator` line(s): ast's
        # `.lineno` points at the `def`/`class` keyword, so without the fix a PR touching ONLY a
        # decorator (`@app.route(...)`) fell outside the recorded span = a missed finer-collision. The
        # span start must be the FIRST decorator line. Content-free (line numbers only).
        _w(os.path.join(d, "deco/handlers.py"),
           "import functools\n\n\n"
           "@functools.cache\n"            # line 4 — first decorator of decofn (def keyword is line 6)
           "@staticmethod\n"
           "def decofn():\n"
           "    return 1\n\n\n"
           "@functools.total_ordering\n"   # line 10 — decorator of DecoCls (class keyword is line 11)
           "class DecoCls:\n"
           "    x = 0\n\n\n"
           "def plainfn():\n"              # undecorated control — span starts at its own def line
           "    return 2\n")

        g = X.build_graph(d)
        langs = X._ts_languages()
        files = {n["path"] for n in g["nodes"] if n["kind"] == "file"}
        names = {(n.get("language"), n.get("name")) for n in g["nodes"] if n.get("kind") in ("def", "class")}
        imports_to_file = [e for e in g["edges"] if e["kind"] == "imports" and e["dst"] in files]

        checks = []
        # #1 import→file resolution (Python; always available)
        checks.append(("import→file resolution: a.py imports b.py (resolved to the FILE)",
                       any(e["src"].endswith("a.py") and e["dst"].endswith("b.py") for e in imports_to_file)))
        # precision: relative import → sibling only (no cross-dir basename fan-out)
        checks.append(("precision: `from .helper` resolves to the SIBLING pkg/helper.py",
                       any(e["src"].endswith("pkg/__init__.py") and e["dst"] == "pkg/helper.py" for e in imports_to_file)))
        checks.append(("precision: `from .helper` does NOT fan out to decoy/helper.py (false coupling)",
                       not any(e["src"].endswith("pkg/__init__.py") and e["dst"] == "decoy/helper.py" for e in imports_to_file)))
        checks.append(("precision: external `from ext.libthing.widget` does NOT match local elsewhere/widget.py",
                       not any(e["src"].endswith("consumer.py") and e["dst"] == "elsewhere/widget.py" for e in imports_to_file)))
        # FINDING 4 — decorated Python symbol span includes the `@decorator` line(s) (Python; always available).
        def _pyspan(name):
            for n in g["nodes"]:
                if n.get("path", "").endswith("deco/handlers.py") and n.get("name") == name:
                    return n.get("start_line"), n.get("end_line")
            return (None, None)
        _decofn_span, _decocls_span, _plain_span = _pyspan("decofn"), _pyspan("DecoCls"), _pyspan("plainfn")
        checks.append(("decorator span (FINDING 4): a decorated `def decofn` span STARTS at its first @decorator line (4), not the def keyword (6)",
                       _decofn_span[0] == 4 and isinstance(_decofn_span[1], int) and _decofn_span[1] >= 6))
        checks.append(("decorator span (FINDING 4): a decorated `class DecoCls` span STARTS at its @decorator line (10), not the class keyword (11)",
                       _decocls_span[0] == 10))
        checks.append(("decorator span (FINDING 4): an UNDECORATED `def plainfn` span STARTS at its own def line (no regression)",
                       _plain_span[0] == 15))
        # #4 multi-language coverage (only if the grammar loaded)
        if "go" in langs:
            checks.append(("go coverage: Login/Handle/Session defs", {("go", "Login"), ("go", "Handle"), ("go", "Session")} <= names))
            checks.append(("go cross-file: api.go calls Login (resolves to auth.go)",
                           any(e["kind"] == "calls" and e["src"].endswith("api.go") and e["dst"] == "Login" for e in g["edges"])))
            # precision (hugo audit): a bare Go stdlib import must NOT resolve to a local same-basename file
            checks.append(("go precision: `import \"strings\"` (stdlib) does NOT match local tpl/strings/strings.go",
                           not any(e["src"].endswith("gosvc/handler.go") and e["dst"] == "tpl/strings/strings.go" for e in imports_to_file)))
            # RECALL: a repo-local Go package import (full module path) resolves to EVERY .go file in
            # the package DIRECTORY it names (a Go package = a dir of .go files, not one file).
            checks.append(("go recall: `import \".../myapp/internal/auth\"` resolves to the package dir's auth.go",
                           any(e["src"].endswith("gosvc/handler.go") and e["dst"] == "myapp/internal/auth/auth.go" for e in imports_to_file)))
            checks.append(("go recall: …same import also resolves to the package dir's token.go (all files in the pkg)",
                           any(e["src"].endswith("gosvc/handler.go") and e["dst"] == "myapp/internal/auth/token.go" for e in imports_to_file)))
            # PRECISION (gin audit): the package import must NOT resolve to the dir's *_test.go file —
            # a `_test.go` is unimportable (separate test binary), so coupling production code to it is a
            # pure false positive (55 such on gin: context.go → binding/json_test.go, …).
            checks.append(("go precision: `.../internal/auth` does NOT resolve to the pkg's auth_test.go (test files unimportable)",
                           not any(e["src"].endswith("gosvc/handler.go") and e["dst"] == "myapp/internal/auth/auth_test.go" for e in imports_to_file)))
            # precision: the import's MOST-SPECIFIC dir wins — it must NOT fan out to the unrelated top-level auth/ pkg
            checks.append(("go precision: `.../internal/auth` does NOT fan out to top-level auth/decoy.go",
                           not any(e["src"].endswith("gosvc/handler.go") and e["dst"] == "auth/decoy.go" for e in imports_to_file)))
            # precision: an EXTERNAL multi-segment import whose tail is no repo package resolves to nothing
            checks.append(("go precision: external `import \"go.uber.org/zap\"` matches no local file",
                           not any(e["src"].endswith("gosvc/handler.go") and e["dst"].endswith("zap") and e["dst"] in files for e in g["edges"])))
        if "java" in langs:
            checks.append(("java coverage: Account class + pay method", {("java", "Account"), ("java", "pay")} <= names))
            # precision (retrofit audit): Java import is a full FQN — `demo.svc.Helper` resolves to the file
            checks.append(("java precision: `import demo.svc.Helper` resolves to jsrc/demo/svc/Helper.java",
                           any(e["src"].endswith("demo/app/Main.java") and e["dst"] == "jsrc/demo/svc/Helper.java" for e in imports_to_file)))
            # precision: the JDK `java.lang.reflect.Type` must NOT basename-match a local Type.java
            checks.append(("java precision: `import java.lang.reflect.Type` (JDK) does NOT match local jtest/other/Type.java",
                           not any(e["src"].endswith("demo/app/Main.java") and e["dst"] == "jtest/other/Type.java" for e in imports_to_file)))
        if "ruby" in langs:
            checks.append(("ruby coverage: Worker class + run method", {("ruby", "Worker"), ("ruby", "run")} <= names))
        if "php" in langs:
            checks.append(("php coverage: Pay/Api classes + charge/handle", {("php", "Pay"), ("php", "charge"), ("php", "handle")} <= names))
            checks.append(("php cross-file: Api.php calls charge (defined in Pay.php)",
                           any(e["kind"] == "calls" and e["src"].endswith("Api.php") and e["dst"] == "charge" for e in g["edges"])))
        if "csharp" in langs:
            checks.append(("c# coverage: Report class + Run method", {("csharp", "Report"), ("csharp", "Run")} <= names))
        if "html" in langs:
            checks.append(("html coverage: index.html <script src=app.js> → imports edge (template↔asset)",
                           any(e["kind"] == "imports" and e["src"].endswith("index.html") and e["dst"].endswith("app.js") for e in g["edges"])))
        if "rust" in langs:
            checks.append(("rust coverage: login def + api.rs calls login (cross-file)",
                           ("rust", "login") in names and any(e["kind"] == "calls" and e["src"].endswith("api.rs") and e["dst"] == "login" for e in g["edges"])))
        if "c" in langs or "cpp" in langs:                  # cpp grammar is a C superset → covers .c too
            checks.append(("c coverage: verify_user def (name from declarator) + api.c calls it cross-file",
                           ("c", "verify_user") in names and any(e["kind"] == "calls" and e["src"].endswith("api.c") and e["dst"] == "verify_user" for e in g["edges"])))
            checks.append(("c++ coverage: Shape class + #include resolves to a local header/impl file",
                           ("cpp", "Shape") in names and any(e["kind"] == "imports" and e["src"].endswith("shape.cpp") and e["dst"] in files for e in g["edges"])))
            # precision (nlohmann/json audit): a single-segment ANGLE-bracket include `<array>` is the
            # C++ stdlib — must NOT match a local array.cpp; the quoted local "util.h" still resolves.
            checks.append(("c++ precision: `#include <array>` (stdlib) does NOT match local examples/array.cpp",
                           not any(e["src"].endswith("ccsrc/widget.cpp") and e["dst"] == "examples/array.cpp" for e in imports_to_file)))
            checks.append(("c++ recall: quoted `#include \"ccsrc/util.h\"` resolves to the local header",
                           any(e["src"].endswith("ccsrc/widget.cpp") and e["dst"] == "ccsrc/util.h" for e in imports_to_file)))
        # KOTLIN SILENT-MISS GUARD (FINDING 1). The pinned tree-sitter-kotlin 1.0.0 parses EVERY valid
        # .kt file with root_node.has_error == True and then salvages symbols INCONSISTENTLY — an
        # authoritative-looking graph that silently drops symbols (a missed collision read as "clear").
        # _ts_languages now PROBES each at-risk grammar and DROPS one that mis-parses, so kotlin files
        # take the recall-safe NODE-ONLY path. We assert whichever branch the loaded grammar selects, so
        # this stays correct if a WORKING kotlin grammar is ever pinned (full extraction auto-enables).
        kt_files = [n for n in g["nodes"] if n.get("kind") == "file" and n["path"].endswith(".kt")]
        kt_syms = [n for n in g["nodes"]
                   if n.get("language") == "kotlin" and n.get("kind") in ("def", "class")]
        kt_struct_edges = [e for e in g["edges"]
                           if e["src"].endswith(".kt") and e["kind"] in ("contains", "calls", "imports")]
        if "kotlin" in langs:
            # A HEALTHY grammar passed the probe → full symbol extraction (the original coverage).
            checks.append(("kotlin coverage (healthy grammar): KtGreeter class + greetKt def",
                           {("kotlin", "KtGreeter"), ("kotlin", "greetKt")} <= names))
            checks.append(("kotlin cross-file (healthy grammar): Main.kt calls greetKt (defined in Greeter.kt)",
                           any(e["kind"] == "calls" and e["src"].endswith("Main.kt") and e["dst"] == "greetKt" for e in g["edges"])))
        else:
            # The probe REJECTED the grammar (the current pin) → kotlin MUST be node-only, NOT partial.
            checks.append(("kotlin silent-miss (FINDING 1): the pinned grammar is REJECTED by the health probe (has_error on valid .kt)",
                           "kotlin" not in langs))
            checks.append(("kotlin silent-miss (FINDING 1): .kt files STILL appear as bare file nodes (recall-safe, language=kotlin)",
                           len(kt_files) == 2 and all(n.get("language") == "kotlin" for n in kt_files)))
            checks.append(("kotlin silent-miss (FINDING 1): a class-with-members .kt yields ZERO symbol nodes (node-only, NOT a partial/inconsistent salvage)",
                           len(kt_syms) == 0))
            checks.append(("kotlin silent-miss (FINDING 1): NO structural edges (contains/calls/imports) are emitted from .kt (honest node-only, no inconsistent recovery)",
                           len(kt_struct_edges) == 0))
        if "swift" in langs:
            checks.append(("swift coverage: SwiftGreeter struct + greetSwift def",
                           {("swift", "SwiftGreeter"), ("swift", "greetSwift")} <= names))
            checks.append(("swift coverage: Greetable protocol (interface — separate node type, must be captured)",
                           ("swift", "Greetable") in names))
            checks.append(("swift cross-file: Main.swift calls greetSwift (defined in Greeter.swift)",
                           any(e["kind"] == "calls" and e["src"].endswith("Main.swift") and e["dst"] == "greetSwift" for e in g["edges"])))
        if "typescript" in langs:
            # RE-EXPORT recall: a barrel's `export * from './ra'` / `export { z } from './rb'` is an
            # export_statement WITH a `source` — must emit an `imports` edge that resolves to the sibling.
            all_imports = [e for e in g["edges"] if e["kind"] == "imports"]
            checks.append(("re-export recall: `export * from './ra'` couples re_index.ts → barrel/ra.ts",
                           any(e["src"].endswith("barrel/re_index.ts") and e["dst"] == "barrel/ra.ts" for e in imports_to_file)))
            checks.append(("re-export recall: `export { z } from './rb'` couples re_index.ts → barrel/rb.ts",
                           any(e["src"].endswith("barrel/re_index.ts") and e["dst"] == "barrel/rb.ts" for e in imports_to_file)))
            # PRECISION: a LOCAL export (no `from`) emits NO edge — only the two re-export specifiers
            # (./ra, ./rb, plus the external bare `react`) may be import dsts of the barrel.
            barrel_import_dsts = {e["dst"] for e in all_imports if e["src"].endswith("barrel/re_index.ts")}
            checks.append(("re-export precision: `export const LOCAL = 1` (local) emits NO import edge",
                           "LOCAL" not in barrel_import_dsts))
            checks.append(("re-export precision: `export { localOnly }` (no `from`) emits NO import edge",
                           "localOnly" not in barrel_import_dsts))
            # the resolver rewrites a resolvable specifier in-place to its file (./ra → barrel/ra.ts);
            # the external `react` stays bare. NO local-export name (LOCAL / localOnly) may appear.
            checks.append(("re-export precision: barrel imports are ONLY the re-exports (ra.ts, rb.ts, react) — no local-export names",
                           barrel_import_dsts == {"barrel/ra.ts", "barrel/rb.ts", "react"}))
            # external re-export stays a bare module name → no wrong-file coupling
            checks.append(("re-export precision: external `export * from 'react'` matches no local file",
                           not any(e["src"].endswith("barrel/re_index.ts") and e["dst"] == "react" for e in imports_to_file)))
            # FINDING 3 — TS interface / type-alias / enum each mint a symbol node (with a span, so
            # finer-collision can refine to the symbol). Precision: an enum MEMBER is NOT a top-level node.
            ts_decl_syms = {(n.get("kind"), n.get("name")) for n in g["nodes"]
                            if n.get("path", "").endswith("tsdecl/types.ts") and n.get("kind") in ("def", "class")}
            checks.append(("TS type decls (FINDING 3): `interface Shape` mints a symbol node",
                           ("class", "Shape") in ts_decl_syms))
            checks.append(("TS type decls (FINDING 3): `type ID = string` mints a symbol node",
                           ("class", "ID") in ts_decl_syms))
            checks.append(("TS type decls (FINDING 3): `enum Color` mints a symbol node",
                           ("class", "Color") in ts_decl_syms))
            checks.append(("TS type decls (FINDING 3) precision: an enum MEMBER `Red` is NOT a top-level symbol",
                           ("class", "Red") not in ts_decl_syms and ("def", "Red") not in ts_decl_syms))
            _shape_span = next((( n.get("start_line"), n.get("end_line")) for n in g["nodes"]
                                if n.get("path", "").endswith("tsdecl/types.ts") and n.get("name") == "Shape"), (None, None))
            checks.append(("TS type decls (FINDING 3): the interface node carries a content-free line span (for finer-collision)",
                           isinstance(_shape_span[0], int) and isinstance(_shape_span[1], int)))

        # schema graph (always available — regex/AST, no grammar needed)
        qedges = [e for e in g["edges"] if e["kind"] == "queries"]
        aedges = [e for e in g["edges"] if e["kind"] == "alters"]
        tnodes = {n.get("name") for n in g["nodes"] if n.get("kind") == "table"}
        checks.append(("schema DDL: .sql ALTER orders → table node + alters edge",
                       "orders" in tnodes and any(e["src"].endswith("0007.sql") and e["dst"] == "orders" for e in aedges)))
        checks.append(("schema DML: reports.py SELECT FROM orders → queries edge",
                       any(e["src"].endswith("reports.py") and e["dst"] == "orders" for e in qedges)))
        checks.append(("schema precision: `from b import` is NOT a SQL queries edge to 'b'",
                       not any(e["dst"] == "b" for e in qedges)))
        checks.append(("schema DDL: CREATE INDEX ... ON customers → table node + alters edge (index migration couples)",
                       "customers" in tnodes and any(e["src"].endswith("0008.sql") and e["dst"] == "customers" for e in aedges)
                       and any(e["src"].endswith("billing.py") and e["dst"] == "customers" for e in qedges)))
        checks.append(("schema precision: the index NAME idx_orders_total is NOT mistaken for a table",
                       "idx_orders_total" not in tnodes))
        checks.append(("schema DDL: CREATE TRIGGER ... ON invoices → table node + alters edge (trigger migration couples)",
                       "invoices" in tnodes and any(e["src"].endswith("0009.sql") and e["dst"] == "invoices" for e in aedges)
                       and any(e["src"].endswith("ledger.py") and e["dst"] == "invoices" for e in qedges)))
        checks.append(("schema precision: the trigger NAME set_invoice_ts is NOT mistaken for a table",
                       "set_invoice_ts" not in tnodes and "broken_trigger" not in tnodes))

        # schema graph — ORM table declarations (always available — regex on raw text, no grammar
        # needed). The moat must fire on ORM apps (most modern apps), not just raw-SQL repos.
        # (a) SQLAlchemy `__tablename__`: model node + alembic migration ALTERs it → they couple.
        checks.append(("schema ORM (SQLAlchemy): `__tablename__ = \"subscriptions\"` → table node",
                       "subscriptions" in tnodes))
        checks.append(("schema ORM (SQLAlchemy): model QUERIES + raw .sql migration ALTERS subscriptions (moat fires)",
                       any(e["src"].endswith("models/subscription.py") and e["dst"] == "subscriptions" and e["kind"] == "queries" for e in g["edges"])
                       and any(e["src"].endswith("0001_subs.sql") and e["dst"] == "subscriptions" and e["kind"] == "alters" for e in aedges)))
        # (b) Django model-name bridge (no explicit db_table): model class + migration model_name couple.
        checks.append(("schema ORM (Django): `class LoyaltyCard(models.Model)` → table node loyaltycard (model-name bridge)",
                       "loyaltycard" in tnodes))
        checks.append(("schema ORM (Django): models.py QUERIES + migration `model_name=` ALTERS loyaltycard (moat fires)",
                       any(e["src"].endswith("shop/models.py") and e["dst"] == "loyaltycard" and e["kind"] == "queries" for e in g["edges"])
                       and any("shop/migrations" in e["src"] and e["dst"] == "loyaltycard" and e["kind"] == "alters" for e in aedges)))
        checks.append(("schema ORM (Django) precision: a plain `class Mixin(object)` is NOT minted as a table",
                       "mixin" not in tnodes))
        # (c) JPA @Table(name=) + a RAW .sql migration that ALTERs the same ORM-declared table → couple.
        checks.append(("schema ORM (JPA): `@Table(name = \"warehouse_item\")` → table node",
                       "warehouse_item" in tnodes))
        checks.append(("schema ORM (JPA): entity QUERIES + raw .sql migration ALTERS warehouse_item (cross-source moat)",
                       any(e["src"].endswith("src/InventoryItem.java") and e["dst"] == "warehouse_item" and e["kind"] == "queries" for e in g["edges"])
                       and any(e["src"].endswith("V3__warehouse.sql") and e["dst"] == "warehouse_item" and e["kind"] == "alters" for e in aedges)))
        # (d) JPA @Entity (no @Table) entity-name bridge + a raw embedded SELECT couples to the ORM table.
        checks.append(("schema ORM (JPA): `@Entity class Shipment` (no @Table) → table node shipment (entity-name bridge)",
                       "shipment" in tnodes))
        checks.append(("schema ORM: raw `SELECT ... FROM shipment` couples to the ORM-declared table (known-table set includes ORM tables)",
                       any(e["src"].endswith("tracking.py") and e["dst"] == "shipment" and e["kind"] == "queries" for e in qedges)))

        # config graph (always available — regex/JSON, no grammar needed)
        cedges = [e for e in g["edges"] if e["kind"] == "reads_config"]
        ckeys = {n.get("name") for n in g["nodes"] if n.get("kind") == "config_key"}
        checks.append(("config: config.json → config_key node 'database_pool_size'", "database_pool_size" in ckeys))
        checks.append(("config: svc.py + job.py both read 'database_pool_size' (→ they contend)",
                       any(e["src"].endswith("svc.py") and e["dst"] == "database_pool_size" for e in cedges)
                       and any(e["src"].endswith("job.py") and e["dst"] == "database_pool_size" for e in cedges)))
        checks.append(("config precision: no reads_config to a generic key (only real config keys match)",
                       all(e["dst"] in {"database_pool_size", "feature_new_ui", "SHARED_API_TOKEN",
                                        "DEPLOY_REGION", "WORKER_QUEUE_NAME", "payment_gateway_url",
                                        "SHARED_ENV_DSN", "ONLY_ONE_ENV",
                                        "SHARED_DOCK_TOKEN", "CACHE_BACKEND_URL", "BUILD_ONLY"} for e in cedges)))
        # ENV-VAR NAME idiom: yaml `- key: NAME` / `environment:` block names become config keys so the
        # code that reads them couples (the recall gap: render.yaml's VERIPSA_DSN coupled 0 files before).
        checks.append(("config env-var: deploy.yaml `- key: SHARED_API_TOKEN` → config_key node",
                       "SHARED_API_TOKEN" in ckeys))
        checks.append(("config env-var: `environment:` block name WORKER_QUEUE_NAME → config_key node",
                       "WORKER_QUEUE_NAME" in ckeys))
        checks.append(("config env-var: api_a.py + api_b.py both os.environ[SHARED_API_TOKEN] (→ they contend)",
                       any(e["src"].endswith("api_a.py") and e["dst"] == "SHARED_API_TOKEN" for e in cedges)
                       and any(e["src"].endswith("api_b.py") and e["dst"] == "SHARED_API_TOKEN" for e in cedges)))
        checks.append(("config env-var precision: a name read by ONE file makes no phantom pair",
                       sum(1 for e in cedges if e["dst"] == "DEPLOY_REGION") == 1))
        checks.append(("config env-var precision: the env VALUE after `value:` is NOT captured (names not values)",
                       "s3cr3t_TOKEN_VALUE" not in ckeys))
        # `.env` FILE idiom: the dotfile was never recognized as a config file (splitext gives no `.env`
        # ext), so its env-var NAMES coupled 0 readers. Now resolved by file name → readers couple.
        checks.append(("config .env: dotfile `.env` recognized → config_key node SHARED_ENV_DSN",
                       "SHARED_ENV_DSN" in ckeys))
        checks.append(("config .env: envread_a.py + envread_b.py both os.environ[SHARED_ENV_DSN] (→ they contend)",
                       any(e["src"].endswith("envread_a.py") and e["dst"] == "SHARED_ENV_DSN" for e in cedges)
                       and any(e["src"].endswith("envread_b.py") and e["dst"] == "SHARED_ENV_DSN" for e in cedges)))
        checks.append(("config .env: `export KEY=` form → config_key node ONLY_ONE_ENV",
                       "ONLY_ONE_ENV" in ckeys))
        checks.append(("config .env precision: a name read by ONE file makes no phantom pair",
                       sum(1 for e in cedges if e["dst"] == "ONLY_ONE_ENV") == 1))
        checks.append(("config .env precision: lowercase `lower_key` (ordinary local) is NOT swept as an env var",
                       "lower_key" not in ckeys))
        checks.append(("config .env precision: the VALUE after `=` is NOT captured (content-free: names not values)",
                       "s3cr3t_env_value" not in ckeys))
        # Dockerfile idiom: the no-extension `Dockerfile` was never recognized (same miss as `.env`),
        # so its ENV/ARG env-var NAMES coupled 0 readers. Now resolved by file name → readers couple.
        checks.append(("config Dockerfile: no-ext `Dockerfile` recognized → config_key node SHARED_DOCK_TOKEN",
                       "SHARED_DOCK_TOKEN" in ckeys))
        checks.append(("config Dockerfile: dockread_a.py + dockread_b.py both os.environ[SHARED_DOCK_TOKEN] (→ they contend)",
                       any(e["src"].endswith("dockread_a.py") and e["dst"] == "SHARED_DOCK_TOKEN" for e in cedges)
                       and any(e["src"].endswith("dockread_b.py") and e["dst"] == "SHARED_DOCK_TOKEN" for e in cedges)))
        checks.append(("config Dockerfile: multiple `K=V` on one ENV line → both names (CACHE_BACKEND_URL captured)",
                       "CACHE_BACKEND_URL" in ckeys and "WORKER_POOL_SIZE" in ckeys))
        checks.append(("config Dockerfile: dockread_d.py reads CACHE_BACKEND_URL (multi-pair ENV name couples)",
                       any(e["src"].endswith("dockread_d.py") and e["dst"] == "CACHE_BACKEND_URL" for e in cedges)))
        checks.append(("config Dockerfile: legacy space form `ENV K V` → name LEGACY_SPACE_VAR (not the value)",
                       "LEGACY_SPACE_VAR" in ckeys))
        checks.append(("config Dockerfile: `ARG K=default` → config_key node BUILD_ONLY",
                       "BUILD_ONLY" in ckeys))
        checks.append(("config Dockerfile precision: an ARG read by ONE file makes no phantom pair",
                       sum(1 for e in cedges if e["dst"] == "BUILD_ONLY") == 1))
        checks.append(("config Dockerfile precision: lowercase `lowercase_dock` (ordinary local) is NOT swept",
                       "lowercase_dock" not in ckeys))
        checks.append(("config Dockerfile precision: the VALUE after `=` is NOT captured (names not values)",
                       "s3cr3t_dock_value" not in ckeys and "defaultbuildval" not in ckeys))
        checks.append(("config Dockerfile precision: the space-form VALUE is NOT captured (legacy_value_here)",
                       "legacy_value_here" not in ckeys))
        # Dependency-manifest sections: their children are package names, NOT config keys — they must
        # be skipped so files merely sharing a dependency don't false-couple (one dep → thousands of pairs).
        checks.append(("config dep-skip: package.json `dependencies` name express_pkg is NOT a config_key",
                       "express_pkg" not in ckeys and "dependencies.express_pkg" not in ckeys))
        checks.append(("config dep-skip: package.json `devDependencies` name supertest_pkg is NOT a config_key",
                       "supertest_pkg" not in ckeys))
        checks.append(("config dep-skip: the dep-section keys themselves (dependencies/devDependencies) are NOT minted",
                       "dependencies" not in ckeys and "devDependencies" not in ckeys))
        checks.append(("config dep-skip: two files requiring the SAME dep do NOT couple (no reads_config to express_pkg)",
                       not any(e["dst"] == "express_pkg" for e in cedges)))
        checks.append(("config dep-skip: Cargo.toml `[dependencies]` name reqwest_pkg is NOT a config_key",
                       "reqwest_pkg" not in ckeys))
        checks.append(("config dep-skip precision: a NON-dep package.json key (engines_minimum) IS still minted",
                       "engines_minimum" in ckeys))
        checks.append(("config dep-skip precision: a NON-dep Cargo key after the dep table (release_opt_level) IS still minted",
                       "release_opt_level" in ckeys))

        # Robustness checks (world-scale safety: size cap, binary detection, vendor skip, no-crash)
        robust = robustness_checks()
        checks.extend(robust)

        # Deeper adversarial-source audit (never-crash / never-hang against the nastiest customer source:
        # deep nesting, multi-MB single lines, broken syntax in every language, unicode/RTL/long-id,
        # garbage bytes, pathological paths) + the CRLF YAML env-block regression this audit fixed.
        checks.extend(adversarial_source_checks())

        # Schema-graph adversarial audit (its OWN walk, no size cap / per-file guard): malformed DDL,
        # a distinct-table flood (the unbounded-output bug this audit fixed), nested parens, SQL
        # comments + CRLF, multi-statement files, weird/quoted/unicode idents, odd ORM, binary .sql,
        # multi-MB ReDoS bait — never raise, never hang, never explode.
        checks.extend(adversarial_schema_checks())

        # i18n correctness (worldwide repos): SYMBOL recovery from non-UTF-8 source (shift_jis / latin-1 /
        # gb18030 / UTF-8+UTF-16 BOM) + valid unicode identifiers + surface-safety of non-ASCII metadata.
        # The fix this landed: strict utf-8 read in extract_file_py dropped every symbol of a non-UTF-8 file.
        checks.extend(i18n_encoding_recovery_checks())

        # audit:unicode — NORMALIZATION (NFC/NFD false-miss) + bidi/zero-width SURFACE spoof (Trojan Source) +
        # homoglyph CROSS-TENANT, across extractor → graph coordinate → claim/collision join → render → tenant key.
        # Fixes landed: build_graph + server path sinks fold every coordinate to NFC (the engine's `path = ANY`
        # join no longer misses an NFC-vs-NFD pair / duplicates a node); render_safe._oneline strips bidi/zero-
        # width/Cf chars (no visual spoof on the PR surface). Pins: the tenant key roots on the numeric owner id.
        checks.extend(unicode_normalization_and_spoof_checks())

        # GAP-14 (unsupported-lang source → bare node) + GAP-15 (generated/vendored → excluded)
        checks.extend(nodes_and_generated_checks())

        # M1 (shipped == supported): the prod image ships a grammar for every declared language (no silent gap).
        checks.extend(shipped_grammar_checks())

        ok = True
        for name, cond in checks:
            print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
            ok = ok and bool(cond)
        installed = [k for k in ("go", "java", "ruby") if k in langs]
        print(f"  (grammars present: {installed or 'none — python-only, language checks skipped'})")
        print("EXTRACTOR GATE:", "PASS" if ok else "FAIL")
        return 0 if ok else 1
    finally:
        shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
