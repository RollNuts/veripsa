"""Co-change (logical coupling) extraction — the EMPIRICAL coupling signal that COMPLEMENTS the static code
graph.

The code graph sees a coupling only when there is a real call / import / schema / config EDGE between two
files. But many real couplings have NO such edge: an implicit contract, a naming convention, a business rule
split across two files, a config value and the code that reads it indirectly. The "human gut feeling" that
"if you touch A you probably also need to touch B" lives exactly there — and the ONLY way to see it is the
repo's CHANGE HISTORY: files that keep changing together are coupled, edge or no edge. This is a SECOND,
INDEPENDENT detector next to the structural graph (structure = "provable links"; co-change = "empirical links,
including the invisible ones"). When both point the same way, the signal is far harder to dismiss.

CONTENT-FREE by construction: only file PATHS, the per-commit GROUPING of paths, COUNTS, and (for a rename) the
one-letter STATUS + the OLD→NEW path arrow ever leave the repo — never a line of code, a commit message, an
author, or a diff. Computed from the SAME clone the graph extractor already holds (clone → extract → delete);
nothing extra is fetched or retained.

RENAME-AWARE history (recall, not noise): a file RENAME makes `git log` report the file's OLD path before the
rename and its NEW path after, so the SAME logical file looks like TWO short, unrelated histories — its coupling
is SPLIT and can fall below the support floor, silently lost. `_git_log_commits` reads `--name-status -M` and
CANONICALISES every historical path to the file's CURRENT name, so the pair is computed on the file's UNIFIED
history. Recall is RECOVERED, never INVENTED — two path-histories of the SAME file are merged; no pair is created.

PRECISION-FIRST (Veripsa's quality is PRECISE SILENCE, not firing frequency — a wrong co-change number is
worse than none):
  • GIANT commits are skipped (a 200-file refactor / a merge / a mass-format makes everything "co-change" — pure
    noise). `max_commit_files` caps it.
  • CONDITIONAL PROBABILITY, not a raw count: strength = max(P(B changes | A changes), P(A | B)) =
    co_change(A,B) / change(A). "A and B co-changed 11 times" means nothing without "out of how many times A
    changed"; 11/12 is a real signal, 11/400 is not.
  • A SUPPORT floor (`min_support`): a pair seen only a few times is coincidence, not coupling. Default=5
    (raised from 3 after a dogfood precision audit on Flask: support=3 yielded 108 src/ pairs of which
    ~94 were annotation/format-sweep FPs; support=5 drops them to 0 while losing only pairs co-changed
    3-4 times in an 800-commit window — weak signals that also had lift≫1 ONLY because both files are
    *rare* movers, so their strength=1.0 passes the `min_prob=0.4` render filter — a render-layer floor
    cannot catch these FPs; only the support floor can). Callers that measured at support=3 can pass it
    explicitly; the default is the precision-first value.
  • A STRENGTH floor (`min_prob`): below it, stay silent.
"""
from __future__ import annotations

import collections
import subprocess

# A delimiter byte git will never emit inside a path or a hash, so we can split the log into commits reliably.
_REC = "\x01"


def _git_log_commits(repo_dir: str, window: int, timeout: int) -> list[set]:
    """Each of the last `window` non-merge commits → the SET of file paths it touched, with FILE RENAMES FOLLOWED
    so a renamed file's history is ONE continuous history under its CURRENT path (not two short, unrelated ones).

    WHY rename-follow: `git log --name-only` reports the OLD path before a rename and the NEW path after, so the
    SAME logical file looks like two separate files → its co-change history is SPLIT in half (each half may now fall
    below the support floor → a real coupling silently vanishes). We read `--name-status -M` instead, which adds a
    one-letter STATUS (and, for renames, `R<sim>\\told\\tnew`) per path, and we CANONICALISE every historical path
    to the file's current name. The pair is then computed on the UNIFIED history — recall RECOVERED, never invented:
    we only MERGE two path-histories of the SAME file, we do not create any pair that didn't co-change.

    STILL CONTENT-FREE: `--name-status -M` emits ONLY a status letter (A/M/D/R<sim>/C<sim>) and PATHS — never a
    diff, a commit message, an author, or a line of code. Strictly the same content surface as `--name-only` plus
    the rename arrow, which is itself just two paths. The bare `%H` record marker is the only other token.

    Rename map is built by walking the log NEWEST→OLDEST (git's default order): the first time a path appears it
    IS its current name (maps to itself); a rename row `R old→new` records that OLDER commits' `old` must resolve to
    whatever `new` currently resolves to (chained renames collapse to the latest name). A COPY (`C old→new`) is NOT
    a rename — `old` keeps its own identity (a new file was forked from it) — so copies do not canonicalise. The
    rename commit itself touched BOTH the file (its path changed) and any co-edited files, so it counts as a change
    to the (canonical) renamed file, exactly as a normal edit would."""
    try:
        out = subprocess.run(
            ["git", "-C", repo_dir, "log", "--no-merges", "--name-status", "-M",
             f"--pretty=format:{_REC}%H", "-n", str(int(window))],
            capture_output=True, text=True, timeout=timeout, check=False).stdout
    except (subprocess.SubprocessError, OSError):
        return []
    # Per commit, keep the RAW touched paths AND the renames it declared (old→new). We resolve to current names in a
    # SECOND pass once the whole rename chain is known (a rename seen in a NEWER commit must rewrite OLDER commits).
    raw_commits: list[set] = []                   # newest→oldest; each = set of RAW (as-of-that-commit) paths
    renames: list[tuple[str, str]] = []           # (old, new) pairs, newest→oldest
    cur: set | None = None
    for line in out.split("\n"):
        if line.startswith(_REC):                 # a new commit record
            if cur is not None:
                raw_commits.append(cur)
            cur = set()
            continue
        if cur is None or not line.strip():
            continue
        parts = line.split("\t")                  # `--name-status` is TAB-separated: STATUS\tpath[\tpath2]
        status = parts[0].strip()
        if (status[:1] in ("R", "C")) and len(parts) >= 3:   # rename/copy: STATUS\told\tnew
            old, new = parts[1].strip(), parts[2].strip()
            if status[:1] == "R" and old and new:
                renames.append((old, new))        # only a RENAME unifies identity; a COPY forks a new file
            if new:
                cur.add(new)                      # the current-side path this commit produced
        else:
            p = parts[-1].strip()                 # A/M/D row: the single path is the last field
            if p:
                cur.add(p)
    if cur is not None:
        raw_commits.append(cur)

    # Build the canonicalisation map. Walking renames newest→oldest, `old` resolves to the CURRENT name of `new`
    # (following any later rename of `new` too — chained renames a→b→c collapse a and b to c). `current()` chases
    # the map to a fixed point so order/length of the chain doesn't matter.
    canon_of: dict[str, str] = {}
    for old, new in renames:                      # already newest→oldest
        canon_of[old] = new

    def current(path: str, _seen: set | None = None) -> str:
        _seen = _seen if _seen is not None else set()
        nxt = canon_of.get(path)
        if nxt is None or nxt == path or nxt in _seen:   # no further rename, self-loop, or cycle guard
            return path
        _seen.add(path)
        return current(nxt, _seen)

    commits: list[set] = []
    for raw in raw_commits:
        commits.append({current(p) for p in raw if p})   # collapse each historical path onto its CURRENT name
    return commits


def fold_commits(commits, change: "collections.Counter", co: "collections.Counter",
                 max_commit_files: int = 40) -> int:
    """Fold a SEQUENCE of per-commit file SETS into the two running co-change counters IN PLACE, returning how
    many NON-GIANT, non-empty commits were counted (the increment to the base-rate denominator N).

    `change[f]`  += 1 for every file f a counted commit touched (the directional denominator n_f).
    `co[(a,b)]`  += 1 (a<b, lexicographic) for every UNORDERED file pair both touched in a counted commit.

    This is THE one place the co-change counting math lives. `cochange_pairs` (batch, over a clone's whole
    history) and any INCREMENTAL caller (folding a single push's commits onto a seed) BOTH go through here, so
    they are byte-identical BY CONSTRUCTION — there is no second copy of the empty-skip / GIANT-skip / sorted
    pair-emission rule that could drift. PRECISION-FIRST is preserved exactly: an empty commit and a GIANT commit
    (> `max_commit_files` — a 200-file refactor / merge / mass-format makes everything "co-change" = pure noise)
    are SKIPPED and do NOT advance N. CONTENT-FREE: a commit is a set of PATHS; only paths + counts are touched.

    `commits` is any iterable of iterables of path strings (a clone's `_git_log_commits` output, or a push's
    per-commit added∪modified∪removed sets — the SAME shape, so the SAME math applies). Each commit is
    de-duplicated to a SET first, so a path listed twice in one commit (e.g. modified and removed) counts once,
    exactly as the clone path's set does."""
    n_added = 0
    for files in commits:
        fs = {f for f in files if f}                      # de-dup to a SET (same shape the clone path folds)
        n = len(fs)
        if n == 0 or n > max_commit_files:                # skip empty + GIANT (refactor/merge/mass-format) commits = noise
            continue
        n_added += 1
        fl = sorted(fs)
        for f in fl:
            change[f] += 1
        for i in range(n):
            for j in range(i + 1, n):
                co[(fl[i], fl[j])] += 1
    return n_added


# ── TWO floors, applied at DIFFERENT layers (PR #314 proper fix: keep raw counts in the store, filter at READ) ─
# A single WRITE-time support floor silently LOSES a coupling that ACCUMULATES across pushes: a pair seen 3× in
# one push + 2× in the next genuinely reaches co=5, but if the 3× were dropped at store time the per-push
# increment folds the 2× onto an EMPTY seed → the real coupling is never reported until the next full backfill.
# So the support/lift/prob PRECISION floor is a RENDER concern (apply it when reading for the customer), NOT a
# storage concern. The STORE keeps every REPEATED co-change (co ≥ PERSIST_MIN_SUPPORT) with RAW counts and NO
# prob/lift pruning, so seed_counters reconstructs the true counters and the increment accumulates exactly like a
# batch. Singletons (co==1) are not persisted — persisting all O(files²) once-together pairs would explode the
# store, and a coupling that forms strictly 1-at-a-time is recovered by the periodic full backfill (the source of
# truth), the same framing as before. The RENDER floor (co_change_partners_with_authority in db/schema/85_cochange.sql)
# keeps the annotation/format-sweep FP class (co 3-4 rare pairs, PR #314) off the CUSTOMER surface — the precision
# gain is preserved at the read layer, where it belongs, instead of corrupting the stored counters.
RENDER_MIN_SUPPORT = 5      # customer-facing precision floor (applied at READ): a real coupling is seen ≥5×
RENDER_MIN_PROB = 0.3
RENDER_MIN_LIFT = 2.0
PERSIST_MIN_SUPPORT = 2     # storage floor: keep every REPEATED co-change (≥2) raw so the increment accumulates


def emit_pairs(change: "collections.Counter", co: "collections.Counter", n_total: int,
               min_support: int = 5, min_prob: float = 0.3, min_lift: float = 2.0,
               max_pairs: int = 5000) -> list[dict]:
    """Turn the two folded counters + the base-rate denominator N into the ranked, precision-gated co-change
    pairs — the SECOND half of the count→pairs pipeline, shared by the batch and incremental callers (one copy
    of the support/strength/LIFT discipline, never two). For every unordered pair {a,b} in `co` past the SUPPORT
    floor whose LIFT and directional confidence clear their floors, emit
        {a, b, co, n_a, n_b, n_total, p_b_given_a, p_a_given_b, strength, lift}
    Sorted strongest-first (LIFT, then confidence, then support — NEVER an average), capped at `max_pairs`."""
    pairs: list[dict] = []
    for (a, b), c in co.items():
        if c < min_support:                                # SUPPORT floor: too few observations = coincidence, not coupling
            continue
        na, nb = change[a], change[b]
        pa = c / na if na else 0.0                         # P(b changes | a changed)  — directional confidence
        pb = c / nb if nb else 0.0                         # P(a changes | b changed)
        strength = max(pa, pb)
        # LIFT (the base-rate correction): co·N / (n_a·n_b) = how many times MORE than chance a & b co-change.
        # confidence alone is inflated for a HOT file that changes every commit (it "co-changes" with everything
        # because it ALWAYS changes, not because it is coupled); lift divides that base rate out — lift>>1 is a
        # real coupling, lift≈1 is just two independently-busy files. This is the precision gate; confidence is
        # kept only for the human-facing "X% of the time" display.
        lift = (c * n_total) / (na * nb) if (na and nb) else 0.0
        if lift < min_lift or strength < min_prob:
            continue
        pairs.append({"a": a, "b": b, "co": c, "n_a": na, "n_b": nb, "n_total": n_total,
                      "p_b_given_a": round(pa, 3), "p_a_given_b": round(pb, 3),
                      "strength": round(strength, 3), "lift": round(lift, 2)})
    # rank by LIFT first (real coupling beyond chance), then confidence, then support — never an average.
    pairs.sort(key=lambda p: (-p["lift"], -p["strength"], -p["co"], p["a"], p["b"]))
    return pairs[:max_pairs]


def cochange_pairs(repo_dir: str, window: int = 800, max_commit_files: int = 40,
                   min_support: int = 5, min_prob: float = 0.3, min_lift: float = 2.0,
                   max_pairs: int = 5000, timeout: int = 60) -> list[dict]:
    """The content-free co-change pairs of a repo. For every UNORDERED file pair {a,b} that changed together in
    at least `min_support` of the last `window` (non-merge, ≤`max_commit_files`) commits AND whose directional
    conditional probability max(P(b|a), P(a|b)) ≥ `min_prob`, return:
        {a, b, co, n_a, n_b, p_b_given_a, p_a_given_b, strength}
    where co = #commits touching BOTH, n_x = #commits touching x, p_b_given_a = co/n_a (given a changed, how
    often b did too), strength = max(p_b_given_a, p_a_given_b). Sorted strongest-first, capped at `max_pairs`
    (a busy monorepo's pair space is O(files²) — the cap bounds it; the per-commit file cap already keeps the
    inner loop O(max_commit_files²)). Every field is a path or a count — content-free.

    The two-stage pipeline (fold the commits into counters, then emit the ranked pairs) is shared verbatim with
    the incremental path via fold_commits + emit_pairs — so a push-time increment counts identically to a clone."""
    commits = _git_log_commits(repo_dir, window, timeout)
    change: collections.Counter = collections.Counter()   # per-file change count (denominator)
    co: collections.Counter = collections.Counter()       # per-(a<b) co-change count (numerator)
    n_total = fold_commits(commits, change, co, max_commit_files)   # # of non-giant commits = base-rate denominator N
    return emit_pairs(change, co, n_total, min_support, min_prob, min_lift, max_pairs)


def seed_counters(pairs):
    """Reconstruct the (change, co, n_total) co-change counters from a STORED pair list — the SEED an incremental
    push-fold accumulates onto. Each stored pair carries `co` (#commits touching both), `n_a`/`n_b` (#commits
    touching each), and the `n_total` (the base-rate N) captured when it was last computed. We take the MAX
    `n_total` across the seed pairs as N (every pair was computed against the same N at store time; MAX is robust
    to a partial/older row). co[(a,b)] = the pair's `co`; change[f] = the MAX `n_x` seen for f across the pairs it
    appears in (a file's change count is one number; the same file in two pairs reports the same n_x — MAX is the
    defensive read). Content-free (paths + counts only). Returns (change, co, n_total).

    HONEST LIMIT (documented, by design): a stored pair list is what the STORE persisted — every REPEATED co-change
    (co ≥ PERSIST_MIN_SUPPORT) with its RAW counts and NO prob/lift pruning (PR #314 proper fix: the render-floor is
    NOT applied at storage), so this seed faithfully reconstructs the counters for any pair that ever co-changed
    ≥PERSIST_MIN_SUPPORT times — the increment accumulates EXACTLY like a batch for those (a pair that grows across
    pushes is no longer lost). The ONLY things absent: singletons (co==1, never persisted — coincidence, and all
    O(files²) once-together pairs would explode the store) and pairs evicted by the max_pairs cap on an extreme
    monorepo. A coupling that forms strictly 1-at-a-time across pushes is recovered by the periodic re-backfill (a
    full clone + recompute), which stays the source of truth. seed_counters never INVENTS a pair — it only reads
    back counts that were already stored."""
    change: collections.Counter = collections.Counter()
    co: collections.Counter = collections.Counter()
    n_total = 0
    for p in (pairs or []):
        a, b = p.get("a"), p.get("b")
        if not a or not b or a == b:
            continue
        lo, hi = (a, b) if a < b else (b, a)
        co[(lo, hi)] = max(co[(lo, hi)], int(p.get("co", 0) or 0))
        change[a] = max(change[a], int(p.get("n_a", 0) or 0))
        change[b] = max(change[b], int(p.get("n_b", 0) or 0))
        n_total = max(n_total, int(p.get("n_total", 0) or 0))
    return change, co, n_total


def cochange_pairs_incremental(seed_pairs, new_commits, max_commit_files: int = 40,
                               min_support: int = 5, min_prob: float = 0.3, min_lift: float = 2.0,
                               max_pairs: int = 5000) -> list[dict]:
    """The PER-PUSH increment: fold a push's per-commit file SETS (`new_commits` — content-free, NO clone) onto
    the counters reconstructed from the repo's LAST STORED pairs (`seed_pairs`), then re-emit the ranked pairs.

    This keeps the co-change signal CURRENT between full backfills — a push's `commits[]` each carry their
    added/modified/removed file lists, which ARE the co-change input — without a clone. The counting goes through
    the EXACT SAME fold_commits + emit_pairs the batch path uses, so given the SAME counters the increment counts
    a set of commits BYTE-IDENTICALLY to a batch run over those commits (the gate proves this). The giant-commit
    skip, support floor, lift, and 0%→not-evaluated discipline are all inherited unchanged.

    `new_commits` = an iterable of per-commit path sets (one per commit in the push). All-time-additive: re-
    delivering the SAME push must be de-duplicated by the CALLER (key by commit sha) so a redelivery does not
    double-count — this function just folds whatever commit sets it is handed."""
    change, co, n_total = seed_counters(seed_pairs)
    n_total += fold_commits(new_commits, change, co, max_commit_files)
    return emit_pairs(change, co, n_total, min_support, min_prob, min_lift, max_pairs)


def cochange_partners(repo_dir: str, paths, **kw) -> dict:
    """For a set of EDITED `paths` (a PR's changed files), the strongest co-change PARTNER files NOT already in
    the edit — the "you touched A; historically B comes with it" completeness/coupling hint. Returns
    {edited_path: [{partner, strength, co, n}, …]} (strongest first), content-free. The PR-comment layer turns
    this into an advisory line; here it is just the data."""
    edited = {p for p in (paths or []) if p}
    by_partner: dict = {}
    for pr in cochange_pairs(repo_dir, **kw):
        a, b = pr["a"], pr["b"]
        # a→b: if the PR edits a but NOT b, b is a co-change partner the author may also need (and vice-versa).
        for src, dst, p_dst, n_src in ((a, b, pr["p_b_given_a"], pr["n_a"]), (b, a, pr["p_a_given_b"], pr["n_b"])):
            if src in edited and dst not in edited:
                by_partner.setdefault(src, []).append(
                    {"partner": dst, "strength": p_dst, "co": pr["co"], "n": n_src})
    for src in by_partner:
        by_partner[src].sort(key=lambda x: (-x["strength"], -x["co"], x["partner"]))
    return by_partner
