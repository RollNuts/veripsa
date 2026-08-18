"""Pure, offline compatibility-rule layer for two function-shape descriptors.

This is PR 3 of the compatibility-aware traffic-control plan
(`docs/COMPATIBILITY_TRAFFIC_CONTROL_PLAN.md`, §4 / §8). It is deliberately
**self-contained**: it takes two plain dicts (a "before" and an "after" shape)
and returns a deterministic compatibility result. It does NOT import the
extractor, touch the DB, open a network socket, read a file, or call an LLM.
It shares only the shape *contract* with the sibling extraction PR — never its
internals.

Content-free by construction (plan §2, PO: 「コンテンツフリーでな」)
--------------------------------------------------------------------
The shape descriptor carries ONLY structural identifiers — parameter *names*,
arity counts, required/optional flags, a hash fingerprint. It has no field that
could carry a default-value expression, an annotation's source text, or a body.
Every reason string this module emits is assembled from those inputs plus a
fixed English vocabulary, so no source bytes can leak into a finding. There is
nothing here to redact because nothing sensitive is ever in scope.

Shape descriptor contract (a plain dict; the sibling extractor emits this)
--------------------------------------------------------------------------
    {
      "symbol":            str,        # optional: the symbol NAME (identifier)
      "param_names":       [str, ...], # ordered positional-or-keyword params;
                                       #   the FIRST `required_arity` are required,
                                       #   the NEXT `optional_arity` are optional.
      "required_arity":    int,        # count of required positional-or-keyword params
      "optional_arity":    int,        # count of params that have a default
      "has_varargs":       bool,       # a `*args`-style variadic is present
      "has_kwargs":        bool,       # a `**kwargs`-style catch-all is present
      "kwonly_names":      [str, ...], # keyword-only parameter NAMES (order-insensitive)
      "shape_fingerprint": str,        # hash over the normalized shape (never source)
    }

A shape may also be `None` / absent, meaning "no such exported symbol here".

Result contract (a plain dict, JSON-serializable, content-free)
---------------------------------------------------------------
    {
      "compat_result": "breaking" | "risky" | "compatible" | "unknown",
      "findings": [
        {"rule_id": str, "compat_result": <one of the four>, "reason": str},
        ...
      ],
    }

`compat_result` is the worst finding by the precedence
`breaking > risky > unknown > compatible`. `findings` is empty only when the
result is `compatible`.

Never raises: a malformed / missing / unparseable descriptor degrades to
`unknown` (never a guess, never an exception).
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

# ----------------------------------------------------------------------------
# Result vocabulary + severity precedence (deterministic roll-up)
# ----------------------------------------------------------------------------
BREAKING = "breaking"
RISKY = "risky"
COMPATIBLE = "compatible"
UNKNOWN = "unknown"

_SEVERITY = {COMPATIBLE: 0, UNKNOWN: 1, RISKY: 2, BREAKING: 3}


def _worst(results: List[str]) -> str:
    """The highest-severity result in `results` (defaults to compatible)."""
    worst = COMPATIBLE
    for r in results:
        if _SEVERITY.get(r, 0) > _SEVERITY[worst]:
            worst = r
    return worst


# ----------------------------------------------------------------------------
# Descriptor validation + name derivation (pure)
# ----------------------------------------------------------------------------
def _is_parseable(shape: Any) -> bool:
    """True iff `shape` is a well-formed descriptor we can compare on.

    We require the structural core (param_names + the two arities); the boolean
    and kwonly fields default safely when absent. Anything malformed → not
    parseable → caller degrades to `unknown`.
    """
    if not isinstance(shape, dict):
        return False
    names = shape.get("param_names")
    if not isinstance(names, list) or not all(isinstance(n, str) for n in names):
        return False
    ra = shape.get("required_arity")
    oa = shape.get("optional_arity")
    if not isinstance(ra, int) or isinstance(ra, bool) or ra < 0:
        return False
    if not isinstance(oa, int) or isinstance(oa, bool) or oa < 0:
        return False
    return True


def _required_names(shape: Dict[str, Any]) -> List[str]:
    names = list(shape.get("param_names") or [])
    ra = int(shape.get("required_arity") or 0)
    return names[:ra]


def _optional_names(shape: Dict[str, Any]) -> List[str]:
    names = list(shape.get("param_names") or [])
    ra = int(shape.get("required_arity") or 0)
    oa = int(shape.get("optional_arity") or 0)
    return names[ra : ra + oa]


def _all_names(shape: Dict[str, Any]) -> List[str]:
    return list(shape.get("param_names") or []) + list(shape.get("kwonly_names") or [])


def _kwonly(shape: Dict[str, Any]) -> List[str]:
    kw = shape.get("kwonly_names")
    return [n for n in kw if isinstance(n, str)] if isinstance(kw, list) else []


def _ident(value: Any) -> Optional[str]:
    """Return `value` only if it is a bare identifier — never arbitrary text.

    A guard so a `symbol` field that somehow carried non-identifier text can
    never reach a reason string.
    """
    return value if isinstance(value, str) and value.isidentifier() else None


def _finding(rule_id: str, result: str, reason: str) -> Dict[str, str]:
    return {"rule_id": rule_id, "compat_result": result, "reason": reason}


# ----------------------------------------------------------------------------
# Individual rules — each is pure and returns a finding dict or None.
# Reasons name ONLY parameter identifiers and integer counts.
# ----------------------------------------------------------------------------
def rule_required_arg_added(before: Dict[str, Any], after: Dict[str, Any]) -> Optional[Dict[str, str]]:
    """A required argument was ADDED → breaking for existing callers."""
    before_all = set(_all_names(before))
    new_required = [n for n in _required_names(after) if n not in before_all]
    if new_required:
        return _finding(
            "required_arg_added",
            BREAKING,
            "required parameter " + " ".join(sorted(new_required)) + " added",
        )
    b, a = int(before.get("required_arity") or 0), int(after.get("required_arity") or 0)
    if a > b:
        return _finding(
            "required_arg_added",
            BREAKING,
            f"required parameter count increased from {b} to {a}",
        )
    return None


def rule_required_arg_removed(before: Dict[str, Any], after: Dict[str, Any]) -> Optional[Dict[str, str]]:
    """A required argument was REMOVED → risky."""
    after_all = set(_all_names(after))
    gone = [n for n in _required_names(before) if n not in after_all]
    if gone:
        return _finding(
            "required_arg_removed",
            RISKY,
            "required parameter " + " ".join(sorted(gone)) + " removed",
        )
    b, a = int(before.get("required_arity") or 0), int(after.get("required_arity") or 0)
    if a < b:
        return _finding(
            "required_arg_removed",
            RISKY,
            f"required parameter count decreased from {b} to {a}",
        )
    return None


def rule_optional_to_required(before: Dict[str, Any], after: Dict[str, Any]) -> Optional[Dict[str, str]]:
    """A previously-optional parameter is now required → breaking."""
    before_optional = set(_optional_names(before))
    now_required = [n for n in _required_names(after) if n in before_optional]
    if now_required:
        return _finding(
            "optional_to_required",
            BREAKING,
            "parameter " + " ".join(sorted(now_required)) + " changed from optional to required",
        )
    return None


def rule_kwonly_renamed(before: Dict[str, Any], after: Dict[str, Any]) -> Optional[Dict[str, str]]:
    """A keyword-only argument disappeared while a new one appeared → breaking."""
    before_kw = set(_kwonly(before))
    after_kw = set(_kwonly(after))
    removed = sorted(before_kw - after_kw)
    added = sorted(after_kw - before_kw)
    if removed and added:
        return _finding(
            "kwonly_renamed",
            BREAKING,
            "keyword only parameter " + " ".join(removed) + " renamed to " + " ".join(added),
        )
    return None


def rule_varargs_removed(before: Dict[str, Any], after: Dict[str, Any]) -> Optional[Dict[str, str]]:
    """Variadic positional (`*args`) was removed → risky."""
    if bool(before.get("has_varargs")) and not bool(after.get("has_varargs")):
        return _finding(
            "varargs_removed",
            RISKY,
            "variadic positional parameter removed",
        )
    return None


# Rules that compare two present, parseable shapes, in application order.
_PAIR_RULES = (
    rule_required_arg_added,
    rule_optional_to_required,
    rule_kwonly_renamed,
    rule_required_arg_removed,
    rule_varargs_removed,
)


# ----------------------------------------------------------------------------
# Public entry point
# ----------------------------------------------------------------------------
def compare_shapes(before: Any, after: Any) -> Dict[str, Any]:
    """Compare a `before` and an `after` shape descriptor.

    Returns the result contract documented at the top of this file. Pure and
    total: any missing / malformed input degrades to `unknown`; a removed
    symbol (after is None) is `breaking`; identical fingerprints short-circuit
    to `compatible`.
    """
    # 1. Exported symbol removed — after shape is gone entirely.
    if after is None:
        name = _ident(before.get("symbol")) if isinstance(before, dict) else None
        reason = "exported symbol removed"
        if name is not None:
            reason = "exported symbol " + name + " removed"
        return {
            "compat_result": BREAKING,
            "findings": [_finding("exported_symbol_removed", BREAKING, reason)],
        }

    # 2. Either side missing / unparseable → unknown (never guess).
    if before is None or not _is_parseable(before) or not _is_parseable(after):
        return {
            "compat_result": UNKNOWN,
            "findings": [_finding("unparseable_shape", UNKNOWN, "shape missing or unparseable")],
        }

    # 3. Identical normalized shape → compatible fast path.
    fb, fa = before.get("shape_fingerprint"), after.get("shape_fingerprint")
    if isinstance(fb, str) and fb and fb == fa:
        return {"compat_result": COMPATIBLE, "findings": []}

    # 4. Apply the deterministic rule layer.
    findings: List[Dict[str, str]] = []
    for rule in _PAIR_RULES:
        f = rule(before, after)
        if f is not None:
            findings.append(f)

    if not findings:
        return {"compat_result": COMPATIBLE, "findings": []}
    return {
        "compat_result": _worst([f["compat_result"] for f in findings]),
        "findings": findings,
    }


__all__ = [
    "BREAKING",
    "RISKY",
    "COMPATIBLE",
    "UNKNOWN",
    "compare_shapes",
    "rule_required_arg_added",
    "rule_required_arg_removed",
    "rule_optional_to_required",
    "rule_kwonly_renamed",
    "rule_varargs_removed",
]
