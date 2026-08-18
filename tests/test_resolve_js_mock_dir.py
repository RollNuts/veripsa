#!/usr/bin/env python3
"""JS/TS BARE-IMPORT vs Jest/Vitest __mocks__ DECOY gate (no DB, no network).

WHY THIS GATE EXISTS (measured precision miss on gitlabhq: 1,388 false src→test edges):

  A BARE single-segment JS/TS import names an EXTERNAL package — a LOCAL module is always
  reached by a relative `./`/`../` or an aliased `~/`/`@/` path (those carry a `/`), NEVER a
  bare name:

    // app/assets/javascripts/abuse_reports/index.js
    import Vue from 'vue';        // external package
    import $ from 'jquery';       // external package

  Jest/Vitest let a repo SHADOW such an external package with a MANUAL MOCK in a `__mocks__/`
  directory (`spec/frontend/__mocks__/vue/index.js`). That mock is swapped in at TEST time only;
  production code never imports it by path. But the resolver's recall-biased basename/suffix probe
  matched the bare name `vue` to the mock's `vue/index.js` folder-index file — minting a FALSE
  production→test coupling. Measured on gitlabhq: 1,388 such edges (`vue`/`lodash-es`/`jquery` →
  `__mocks__/<pkg>/index.js` and `__helpers__/jquery.js`), 37 surviving hub-dampening as real
  customer-visible false couplings.

  FIX (in _cg_resolve, end of candidate generation for a bare web import): drop any candidate that
  lives inside a Jest/Vitest test-double directory (`__mocks__`, `__mock__`, `__fixtures__`). This
  is the SAME separate-test-target rule the Rust (`_RS_TARGET_SEG`) / C# (`_CS_TEST_SEG`) / Go
  (`_test.go`) resolvers already apply. RECALL-SAFE: a real local module under such a directory is
  never imported by a BARE name (it would be a relative/aliased path = not bare_single), so only the
  external-package decoy is removed; relative/aliased/multi-segment imports are untouched.

CONTENT-FREE: only repo file paths and import specifiers are used.
Hermetic: stands up tiny synthetic repos in a tempdir; no DB, no network; deterministic.
"""
from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import code_graph_extract as X  # noqa: E402


def _build(files: dict):
    with tempfile.TemporaryDirectory() as d:
        for rel, body in files.items():
            p = os.path.join(d, rel)
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "w") as fh:
                fh.write(body)
        g = X.build_graph(d)
    fset = {n["path"] for n in g["nodes"] if n.get("kind") == "file"}
    resolved = {(e["src"], e["dst"]) for e in g["edges"]
                if e["kind"] == "imports" and e["dst"] in fset}
    return resolved, fset


def main() -> int:
    checks = []

    # ── Case 1: bare external `vue` must NOT resolve to __mocks__/vue/index.js ────────────
    # Exactly the gitlabhq pattern. The bare name `vue` is external; the mock is a test double.
    c1, _ = _build({
        "app/javascripts/abuse_reports/index.js":
            "import Vue from 'vue';\nimport Foo from './components/foo.js';\n",
        "app/javascripts/abuse_reports/components/foo.js": "export default 1;\n",
        "spec/frontend/__mocks__/vue/index.js": "export default {};\n",
    })
    mock_edge = ("app/javascripts/abuse_reports/index.js", "spec/frontend/__mocks__/vue/index.js")
    rel_edge = ("app/javascripts/abuse_reports/index.js", "app/javascripts/abuse_reports/components/foo.js")
    checks.append(("bare external 'vue' does NOT couple to __mocks__/vue/index.js", mock_edge not in c1))
    checks.append(("relative './components/foo.js' still resolves (recall kept)", rel_edge in c1))

    # ── Case 2: bare `jquery` must NOT resolve to __mocks__/__helpers__ jquery.js ─────────
    c2, _ = _build({
        "app/javascripts/activities.js": "import $ from 'jquery';\n",
        "spec/frontend/__mocks__/jquery.js": "export default {};\n",
    })
    jq_edge = ("app/javascripts/activities.js", "spec/frontend/__mocks__/jquery.js")
    checks.append(("bare 'jquery' does NOT couple to a __mocks__ decoy", jq_edge not in c2))

    # ── Case 2b: bare external `clipboard` must NOT resolve to a __helpers__ DOM-shim ─────
    # Exactly the gitlabhq `copy_to_clipboard.js` pattern: `import Clipboard from 'clipboard'`
    # (external npm) matched `__helpers__/dom_shims/clipboard.js` (a test DOM shim). __helpers__
    # is the sibling Jest test-double convention and must be excluded for a bare import too.
    c2b, _ = _build({
        "app/javascripts/behaviors/copy_to_clipboard.js":
            "import Clipboard from 'clipboard';\nimport { f } from '~/lib/utils/common_utils';\n",
        "spec/frontend/__helpers__/dom_shims/clipboard.js": "export default {};\n",
    })
    helper_edge = ("app/javascripts/behaviors/copy_to_clipboard.js",
                   "spec/frontend/__helpers__/dom_shims/clipboard.js")
    checks.append(("bare 'clipboard' does NOT couple to a __helpers__ DOM-shim decoy", helper_edge not in c2b))

    # ── Case 3: RECALL-SAFE — a RELATIVE import INTO __mocks__ still resolves ─────────────
    # A spec importing its own mock by a relative path is a REAL coupling (not bare) and must stay.
    c3, _ = _build({
        "spec/frontend/foo_spec.js": "import m from './__mocks__/thing.js';\n",
        "spec/frontend/__mocks__/thing.js": "export default {};\n",
    })
    rel_mock = ("spec/frontend/foo_spec.js", "spec/frontend/__mocks__/thing.js")
    checks.append(("relative import INTO __mocks__ still resolves (recall-safe, only bare is guarded)",
                   rel_mock in c3))

    # ── Case 4: PRECISION FLOOR — bare name to a REAL non-mock local module unaffected ────
    c4, _ = _build({
        "src/index.js": "import x from 'mylib';\n",
        "src/mylib/index.js": "export default 1;\n",     # a real local package dir, NOT a mock
    })
    real_edge = ("src/index.js", "src/mylib/index.js")
    checks.append(("bare name to a real local index (non-mock dir) is untouched", real_edge in c4))

    # ── Case 5: a bare import with a NON-mock test sibling co-resolved keeps the real one ─
    # If the same bare name also matches a real source file, that real edge must survive while only
    # the __mocks__ decoy is dropped.
    c5, _ = _build({
        "src/app.js": "import widget from 'widget';\n",
        "src/widget/index.js": "export default 1;\n",            # real
        "spec/frontend/__mocks__/widget/index.js": "export default {};\n",  # decoy
    })
    real5 = ("src/app.js", "src/widget/index.js")
    decoy5 = ("src/app.js", "spec/frontend/__mocks__/widget/index.js")
    checks.append(("real local 'widget' kept while the __mocks__ widget decoy is dropped",
                   real5 in c5 and decoy5 not in c5))

    ok = True
    for name, cond in checks:
        print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
        ok = ok and bool(cond)
    print(f"JS-MOCK-DIR GATE: {'PASS' if ok else 'FAIL'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
