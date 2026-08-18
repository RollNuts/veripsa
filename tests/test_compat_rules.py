#!/usr/bin/env python3
"""Compatibility-rule layer gate — the PURE, OFFLINE shape-diff rules.

Proves on the real `_compat_rules.compare_shapes` (no DB, no network, no file
I/O, no extractor import):

  (1) each first rule fires with the right `compat_result`:
        required-arg added         -> breaking
        optional -> required       -> breaking
        keyword-only renamed       -> breaking
        exported symbol removed    -> breaking
        required-arg removed       -> risky
        variadic removed           -> risky
        identical / additive-only  -> compatible
        missing / unparseable      -> unknown  (never guessed)
  (2) CONTENT-FREE: no reason string can carry a default value, an annotation's
      source text, or a code body. We feed shapes that carry ONLY names + counts
      and assert every token in every reason is either a digit, an input
      parameter/symbol name, or a fixed English word — nothing foreign can appear.

Run:  python3 tests/test_compat_rules.py
"""
from __future__ import annotations

import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "github-app"))
import _compat_rules as C  # noqa: E402

FAIL = 0


def chk(cond: bool, label: str) -> None:
    global FAIL
    if cond:
        print("  [PASS]", label)
    else:
        FAIL += 1
        print("  [FAIL]", label)


def shape(
    symbol="fn",
    param_names=(),
    required_arity=0,
    optional_arity=0,
    has_varargs=False,
    has_kwargs=False,
    kwonly_names=(),
    shape_fingerprint="fp",
):
    return {
        "symbol": symbol,
        "param_names": list(param_names),
        "required_arity": required_arity,
        "optional_arity": optional_arity,
        "has_varargs": has_varargs,
        "has_kwargs": has_kwargs,
        "kwonly_names": list(kwonly_names),
        "shape_fingerprint": shape_fingerprint,
    }


def result_of(before, after):
    return C.compare_shapes(before, after)["compat_result"]


def rule_ids(before, after):
    return {f["rule_id"] for f in C.compare_shapes(before, after)["findings"]}


# ---------------------------------------------------------------------------
def main() -> int:
    # (1) required arg ADDED (name appears in after-required) -> breaking
    before = shape(param_names=["a", "b"], required_arity=2, shape_fingerprint="f1")
    after = shape(param_names=["a", "b", "c"], required_arity=3, shape_fingerprint="f2")
    chk(result_of(before, after) == C.BREAKING, "required arg added -> breaking")
    chk("required_arg_added" in rule_ids(before, after), "required arg added -> right rule_id")

    # required arg added detected purely by arity increase (renamed churn) -> breaking
    b2 = shape(param_names=["a"], required_arity=1, shape_fingerprint="f1")
    a2 = shape(param_names=["a", "b"], required_arity=2, shape_fingerprint="f2")
    chk(result_of(b2, a2) == C.BREAKING, "required arg added (arity up) -> breaking")

    # (2) required arg REMOVED -> risky
    before = shape(param_names=["a", "b"], required_arity=2, shape_fingerprint="f1")
    after = shape(param_names=["a"], required_arity=1, shape_fingerprint="f2")
    chk(result_of(before, after) == C.RISKY, "required arg removed -> risky")
    chk("required_arg_removed" in rule_ids(before, after), "required arg removed -> right rule_id")

    # (3) optional -> required -> breaking
    before = shape(param_names=["a", "b"], required_arity=1, optional_arity=1, shape_fingerprint="f1")
    after = shape(param_names=["a", "b"], required_arity=2, optional_arity=0, shape_fingerprint="f2")
    chk(result_of(before, after) == C.BREAKING, "optional -> required -> breaking")
    chk("optional_to_required" in rule_ids(before, after), "optional->required -> right rule_id")

    # (4) keyword-only RENAMED -> breaking
    before = shape(param_names=["a"], required_arity=1, kwonly_names=["mode"], shape_fingerprint="f1")
    after = shape(param_names=["a"], required_arity=1, kwonly_names=["style"], shape_fingerprint="f2")
    chk(result_of(before, after) == C.BREAKING, "kwonly renamed -> breaking")
    chk("kwonly_renamed" in rule_ids(before, after), "kwonly renamed -> right rule_id")

    # (5) varargs removed -> risky
    before = shape(param_names=["a"], required_arity=1, has_varargs=True, shape_fingerprint="f1")
    after = shape(param_names=["a"], required_arity=1, has_varargs=False, shape_fingerprint="f2")
    chk(result_of(before, after) == C.RISKY, "varargs removed -> risky")
    chk("varargs_removed" in rule_ids(before, after), "varargs removed -> right rule_id")

    # (6) exported symbol removed (after is None) -> breaking
    before = shape(symbol="createOrder", param_names=["a", "b"], required_arity=2)
    chk(result_of(before, None) == C.BREAKING, "exported symbol removed -> breaking")
    chk("exported_symbol_removed" in rule_ids(before, None), "symbol removed -> right rule_id")

    # (7) compatible: identical fingerprint short-circuit
    same = shape(param_names=["a", "b"], required_arity=2, shape_fingerprint="same")
    chk(result_of(same, dict(same)) == C.COMPATIBLE, "identical fingerprint -> compatible")

    # compatible: adding an OPTIONAL arg is not a break for existing callers
    before = shape(param_names=["a"], required_arity=1, optional_arity=0, shape_fingerprint="f1")
    after = shape(param_names=["a", "b"], required_arity=1, optional_arity=1, shape_fingerprint="f2")
    chk(result_of(before, after) == C.COMPATIBLE, "added optional arg -> compatible")
    chk(C.compare_shapes(before, after)["findings"] == [], "compatible -> no findings")

    # (8) unknown: either side missing / unparseable -> never guessed
    chk(result_of(None, after) == C.UNKNOWN, "before missing -> unknown")
    chk(result_of({"param_names": "oops", "required_arity": 1, "optional_arity": 0}, after) == C.UNKNOWN,
        "unparseable before (bad param_names) -> unknown")
    chk(result_of(before, {"required_arity": 1.5}) == C.UNKNOWN, "unparseable after (bad arity) -> unknown")
    chk(result_of({}, {}) == C.UNKNOWN, "empty dicts -> unknown")

    # never raises on junk input
    for junk in (None, 0, "", [], {"required_arity": True}):
        try:
            r = C.compare_shapes(junk, after)["compat_result"]
            chk(r in (C.UNKNOWN, C.BREAKING), f"junk before {junk!r} degrades, never raises")
        except Exception as exc:  # noqa: BLE001
            chk(False, f"junk before {junk!r} raised {exc!r}")

    # -----------------------------------------------------------------------
    # (9) CONTENT-FREE assertion. We feed shapes whose descriptors carry ONLY
    # identifiers + counts (there is no field that could hold a default value or
    # annotation source), then assert every reason string is built solely from:
    #   - an integer count, or
    #   - a parameter/symbol NAME that was present in the input, or
    #   - a fixed English word from the rule vocabulary.
    # A default value ("timeout=30"), an annotation ("x: int"), or a body would
    # introduce a foreign token and fail this — but none can, by construction.
    allowed_words = {
        "required", "parameter", "count", "increased", "decreased", "from", "to",
        "added", "removed", "optional", "changed", "keyword", "only", "renamed",
        "variadic", "positional", "exported", "symbol", "shape", "missing", "or",
        "unparseable",
    }
    input_names = {"a", "b", "c", "mode", "style", "createOrder", "fn"}
    token_re = re.compile(r"[A-Za-z_][A-Za-z0-9_]*|\d+")

    cases = [
        (shape(param_names=["a", "b", "c"], required_arity=3, shape_fingerprint="x"),
         shape(param_names=["a", "b", "c", "createOrder"], required_arity=4, shape_fingerprint="y")),
        (shape(param_names=["a", "b"], required_arity=2, shape_fingerprint="x"),
         shape(param_names=["a"], required_arity=1, shape_fingerprint="y")),
        (shape(param_names=["a", "mode"], required_arity=1, optional_arity=1, shape_fingerprint="x"),
         shape(param_names=["a", "mode"], required_arity=2, optional_arity=0, shape_fingerprint="y")),
        (shape(kwonly_names=["mode"], shape_fingerprint="x"),
         shape(kwonly_names=["style"], shape_fingerprint="y")),
        (shape(has_varargs=True, shape_fingerprint="x"), shape(has_varargs=False, shape_fingerprint="y")),
        (shape(symbol="createOrder"), None),
        (None, shape()),
    ]
    leaked = []
    for before, after in cases:
        for f in C.compare_shapes(before, after)["findings"]:
            reason = f["reason"]
            # A default/annotation would need one of these characters; assert none appear.
            if any(ch in reason for ch in "=:(){}[]<>\"'"):
                leaked.append(("punct", reason))
                continue
            for tok in token_re.findall(reason):
                if tok.isdigit() or tok in allowed_words or tok in input_names:
                    continue
                leaked.append(("token:" + tok, reason))
    chk(leaked == [], "content-free: every reason token is a count, an input name, or a vocabulary word")
    if leaked:
        for kind, reason in leaked:
            print("      LEAK", kind, "in:", reason)

    # counts in reasons are integers only (no default like `=30` sneaking in as a number-with-context)
    r = C.compare_shapes(
        shape(param_names=["a"], required_arity=1, shape_fingerprint="x"),
        shape(param_names=["a", "b"], required_arity=2, shape_fingerprint="y"),
    )
    chk(all("=" not in f["reason"] for f in r["findings"]), "content-free: no '=' (default-assignment) in any reason")

    print("COMPAT RULES GATE:", "PASS" if FAIL == 0 else "FAIL")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
