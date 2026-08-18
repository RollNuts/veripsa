"""Generated/vendored-path PREDICATE for the code-graph extractor (GAP-15: anti-cry-wolf).

Owns: _GENERATED_DIR_NAMES, _GENERATED_FILE_SUFFIXES, _gitattr_pat_to_regex,
_matches_gitattr, _is_generated.

These are the PURE, leaf-ish "is this path generated/vendored?" helpers — they take a
repo-relative path (and pre-parsed `.gitattributes` matchers) and answer yes/no with NO
filesystem WALK and NO shared mutable globals. The WALK that produces the
`.gitattributes` matchers (_gitattributes_generated_matchers) stays in code_graph_extract
beside the other walks (it needs the extractor's _SKIP_DIRS). The split keeps this
generated-exclusion predicate group out of build_graph's core loop while preserving the
full public surface: code_graph_extract re-exports every name moved here unchanged.

Generated or vendored code that lives OUTSIDE _SKIP_DIRS (protobuf `*_pb2.py`, `*.pb.go`,
gRPC stubs, `*.gen.*`, `__generated__/`, …) is still hand-edited by NOBODY — but parsing it
produces synthetic symbols whose `calls`/`contains` edges create false adjacency, i.e. we
cry wolf on files no one touches. We EXCLUDE such files via two complementary signals:
  (1) `.gitattributes` `linguist-generated` / `linguist-vendored` markers (the repo's OWN
      declaration);
  (2) common generated filename SUFFIXES + directory names (when the repo did not declare them).
Static + glob, no deps, content-free (path/pattern strings only, never file bodies).
"""
import os
import re
import string

# Directory names that are unambiguously generated (matched at any walk depth, like _SKIP_DIRS but
# specific to codegen output trees rather than build/cache/deps). Kept TIGHT: `migrations` is NOT here
# (Django/Rails migrations are hand-relevant and often hand-edited — excluding them would lose real
# coupling), and neither is the over-broad `gen` (too many false positives, e.g. a `gen/` of real code).
_GENERATED_DIR_NAMES = frozenset({"__generated__", "generated", "_generated", "__pb__"})

# Generated-filename SUFFIXES: a file whose name ends with one of these is machine-emitted (protobuf /
# gRPC / Thrift / codegen / source maps / lockfiles-as-code). Suffix-matched on the BASENAME so a
# hand-written `foo.go` is never caught, only `foo_pb2.py` / `foo.pb.go` / `foo.gen.go` etc.
_GENERATED_FILE_SUFFIXES = (
    "_pb2.py", "_pb2_grpc.py", "_pb2.pyi", "_pb2_grpc.pyi",   # python protobuf / grpc stubs
    ".pb.go", "_grpc.pb.go",                                  # go protobuf / grpc stubs
    ".pb.cc", ".pb.h",                                        # c++ protobuf stubs
    "_pb2.cs", ".pbobjc.h", ".pbobjc.m",                      # c# / objc protobuf stubs
    "_pb.js", "_pb.ts", ".pb.js", ".pb.ts",                   # js/ts protobuf stubs
    ".gen.go", ".gen.ts", ".gen.js", ".generated.go",         # generic codegen markers
    ".g.dart", ".freezed.dart", ".g.cs", ".designer.cs",      # dart / c# codegen
    "_string.go",                                             # go `stringer` output
    ".min.js", ".min.css",                                    # minified bundles (synthetic symbols)
    ".map",                                                   # source maps
)

# Git wildmatch's named POSIX character classes are ASCII-oriented.  Expand
# them inside the surrounding Python regex class; the outer FNM_PATHNAME guard
# below independently prevents even `graph`/`print`/`punct` from consuming `/`.
_POSIX_CLASS_REGEX = {
    "alnum": "A-Za-z0-9",
    "alpha": "A-Za-z",
    "blank": r" \t",
    "cntrl": r"\x00-\x1f\x7f",
    "digit": "0-9",
    "graph": r"\x21-\x7e",
    "lower": "a-z",
    "print": r"\x20-\x7e",
    "punct": re.escape(string.punctuation),
    "space": r" \t\n\v\f\r",
    "upper": "A-Z",
    "xdigit": "A-Fa-f0-9",
}


def _gitattr_pat_to_regex(pat):
    """Translate a gitattributes/gitignore glob to a regex, honoring the semantics linguist markers
    rely on: `*` matches within ONE path segment (NOT '/'), `**` matches across segments, `?` is one
    non-'/' char, `[abc]` is a char class, and `[!x]`/`[^x]` are negated
    classes. A backslash quotes the next wildmatch character. Anchored at both
    ends (full-candidate match)."""
    out, i, n = ["^"], 0, len(pat)
    while i < n:
        c = pat[i]
        if c == "*":
            run_end = i
            while run_end < n and pat[run_end] == "*":
                run_end += 1
            star_count = run_end - i
            # Git gives exactly two stars cross-directory meaning only in
            # these documented positions: leading `**/`, middle `/**/`, and
            # trailing `/**`. Every other consecutive run is ordinary `*`
            # behavior and therefore cannot consume `/`.
            if (
                star_count == 2
                and i == 0
                and run_end < n
                and pat[run_end] == "/"
            ):
                out.append("(?:.*/)?")
                i = run_end + 1
            elif (
                star_count == 2
                and i > 0
                and pat[i - 1] == "/"
                and run_end < n
                and pat[run_end] == "/"
            ):
                out.append("(?:.*/)?")
                i = run_end + 1
            elif (
                star_count == 2
                and i > 0
                and pat[i - 1] == "/"
                and run_end == n
            ):
                out.append(".*")
                i = run_end
            else:
                out.append("[^/]*")
                i = run_end
        elif c == "?":
            out.append("[^/]"); i += 1
        elif c == "\\":
            # Git wildmatch treats a backslash as quoting the next pattern
            # byte. This is also how a C-quoted attributes pattern represents
            # an escaped space: ``"space\\\\ file.py"`` C-decodes to
            # ``space\ file.py`` before wildmatch sees it.
            if i + 1 < n:
                out.append(re.escape(pat[i + 1]))
                i += 2
            else:
                out.append(re.escape(c))
                i += 1
        elif c == "[":
            # POSIX fnmatch permits either `!` or `^` as the first class byte
            # to negate it. Python regex only accepts `^`; normalize both.
            # A `]` immediately after the optional negator is literal, so do
            # not mistake it for the closing delimiter.
            j = i + 1
            negated = j < n and pat[j] in ("!", "^")
            if negated:
                j += 1
            body_start = j
            if j < n and pat[j] == "]":
                j += 1
            while j < n:
                if pat[j:j + 2] == "[:":
                    posix_end = pat.find(":]", j + 2)
                    if posix_end == -1:
                        return re.compile(r"(?!)")
                    class_name = pat[j + 2:posix_end]
                    if class_name not in _POSIX_CLASS_REGEX:
                        return re.compile(r"(?!)")
                    j = posix_end + 2
                    continue
                if pat[j] == "\\" and j + 1 < n:
                    j += 2
                    continue
                if pat[j] == "]":
                    break
                j += 1
            if j >= n or j == body_start:
                out.append(re.escape(c))
                i += 1
                continue

            raw = pat[body_start:j]
            body = []
            k = 0
            while k < len(raw):
                if raw[k:k + 2] == "[:":
                    posix_end = raw.find(":]", k + 2)
                    if posix_end == -1:
                        return re.compile(r"(?!)")
                    class_name = raw[k + 2:posix_end]
                    class_fragment = _POSIX_CLASS_REGEX.get(class_name)
                    if class_fragment is None:
                        return re.compile(r"(?!)")
                    body.append(class_fragment)
                    k = posix_end + 2
                    continue
                char = raw[k]
                if char == "\\" and k + 1 < len(raw):
                    body.append(re.escape(raw[k + 1]))
                    k += 2
                    continue
                if char in ("\\", "]", "[", "&", "~", "|"):
                    body.append("\\" + char)
                elif char == "^" and not body:
                    body.append(r"\^")
                else:
                    # Preserve `-` so ordinary ranges such as `[0-9]` retain
                    # their existing behavior.
                    body.append(char)
                k += 1
            class_re = "[" + ("^" if negated else "") + "".join(body) + "]"
            # Even a negated class may never consume a path separator under
            # Git's FNM_PATHNAME semantics.
            out.append("(?:(?!/)" + class_re + ")")
            i = j + 1
        else:
            out.append(re.escape(c)); i += 1
    out.append("$")
    try:
        return re.compile("".join(out))
    except re.error:
        # A malformed repository-controlled bracket range must not crash graph
        # extraction. Git treats malformed patterns as non-useful; a literal
        # full-string fallback is conservative and bounded.
        return re.compile("^" + re.escape(pat) + "$")


def _matches_gitattr(rel, base_rel, pattern):
    """True if repo-relative `rel` matches a gitattributes `pattern` declared in directory `base_rel`.
    Honors git's pathspec semantics for linguist markers: a pattern with no '/' (ignoring `**`) matches
    by BASENAME at any depth; a pattern containing a '/' is anchored relative to `base_rel`; `*` does not
    cross '/' but `**` does."""
    # The path we test the pattern against, made relative to where the .gitattributes lives.
    if base_rel:
        if rel.startswith(base_rel + "/"):
            cand = rel[len(base_rel) + 1:]
        else:
            return False
    else:
        cand = rel
    anchored = pattern.startswith("/")
    pat = pattern[1:] if anchored else pattern
    # Git tree paths never contain an empty path component, and Git's
    # attributes matcher treats a pathological `foo//bar` pattern as matching
    # neither `foo/bar` nor a doubly-spelled query path. Ignore it explicitly
    # instead of giving an artificial direct-call match.
    if "//" in pat:
        return False
    if not anchored and "/" not in pat.replace("**", ""):
        # no directory separator (ignoring **) → match by basename at any depth (git semantics)
        return _gitattr_pat_to_regex(pat.lstrip("/")).match(os.path.basename(cand)) is not None
    return _gitattr_pat_to_regex(pat).match(cand) is not None


def _is_generated(rel, attr_matchers):
    """True if repo-relative path `rel` is GENERATED/VENDORED and must be EXCLUDED from the graph
    (GAP-15: anti-cry-wolf). Two signals, in order: (1) the repo's own `.gitattributes`
    linguist-generated/linguist-vendored markers (each attribute folded independently left→right);
    (2) common generated filename suffixes + generated directory names. A NUL/binary/oversized file
    is handled elsewhere — this predicate is purely about generated-ness."""
    # (1) Git attributes are independent state machines. Unsetting vendored
    # must not erase generated=set, and vice versa. `None` represents
    # unspecified (`!attr`), which restores heuristic behavior.
    states = {
        "linguist-generated": None,
        "linguist-vendored": None,
    }
    for base_rel, pattern, attribute, state in attr_matchers:
        if _matches_gitattr(rel, base_rel, pattern):
            states[attribute] = state
    if True in states.values():
        return True
    if states["linguist-generated"] is False:
        # An explicit generated=false is the authoritative carve-out from the
        # generated filename/directory heuristic. A simultaneous vendored=true
        # already returned above.
        return False
    # generated=None means there is no explicit generated decision. A lone
    # vendored=false does not suppress generated filename/directory heuristics.

    # (2) heuristics: generated filename suffixes (basename) or a generated directory anywhere in path.
    base = os.path.basename(rel)
    if any(base.endswith(sfx) for sfx in _GENERATED_FILE_SUFFIXES):
        return True
    parts = rel.replace(os.sep, "/").split("/")
    if any(seg in _GENERATED_DIR_NAMES for seg in parts[:-1]):   # any DIRECTORY segment (not the file)
        return True
    return False
