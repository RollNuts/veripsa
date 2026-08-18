#!/usr/bin/env python3
"""Veripsa — the FOUNDER DB-COST REPORT ("俺用の画面": the one screen the operator runs to see what the DB costs).

WHY THIS EXISTS: the App runs on a small/cheap Postgres (the 256–512 MiB tier — see retention_prune.py /
github_rest.py). Every tenant's repo graph + append-only event ledger lives in that one box. There is NO web
console yet, so the founder has NO way to answer the questions a paid operator MUST answer before the disk
fills or before a single account quietly consumes the whole free tier: how big is the DB right now? which
account is the biggest footprint? who has crossed the free line? This is that screen — a terminal report the
founder runs by hand (or from cron), no UI required.

WHAT IT DOES: connects as the OWNER (VERIPSA_OWNER_DSN, or VERIPSA_DSN as a fallback) — the cross-account,
owner-only role — and calls `core.owner_cost_surface()` (a content-free aggregate the sibling DB lane owns).
It prints: the total DB size (human bytes) + the free-line thresholds, a per-account table sorted biggest-
footprint-first (account, repos, graph units, events, 7-day growth, % of the free line, and a clear OVER
FREE LINE marker), and a one-line summary (N accounts, O over the free line).

IT ALSO RENDERS THE SEAT (value) LINE — the product-led-growth conversion view. Per account it shows
active_agents (distinct HUMAN operators active in 30d; AI agents are FREE — the fleet-friendly stance) and
paid_seats (the humans BEYOND the free seat line) with an OVER LINE marker, then a CONVERSION + MRR summary:
a VERIPSA_SEAT_PRICE_USD knob (default $19), estimated MRR = paid_seats_total × seat_price, and the list of
conversion candidates (accounts AT/over the free seat line — they have hit the value moment). This is
VISIBILITY + PROJECTION ONLY (the founder's conversion funnel) — it does NOT charge; a customer-facing
upgrade nudge comes later with Marketplace billing.

IT ALSO RENDERS THE COMPATIBILITY SHADOW SECTION (compat lane PR-5) — the weekly GTM review's owner-only
input over what the shadow compatibility lane has observed: cumulative content-free aggregates from
core.owner_compat_shadow_surface() (finding/repo/head-pair COUNTS, per-rule-id counts, first/last observed).
Independently guarded: an undeployed or denied compat surface renders as an honest all-zeros block and can
never break the cost report. Shadow counts are INTERNAL observation only — never public proof, never a
customer-effect claim (docs/WEEKLY_GTM_REVIEW.md).

READ-ONLY (no writes — it only SELECTs the surface). NEVER-CRASH: a missing surface / unset DSN / a DB hiccup
degrades to a clean one-line message, never a traceback (a founder running this at 2am must get a sentence,
not a stack). CONTENT-FREE (the moat, unchanged): it prints ONLY what the surface returns — account ids +
counts. No repo names, no paths, no code, no DSN. The FORMATTER (render_report) is a pure function over the
surface JSON, so it is unit-testable with no DB.

ENV:
  VERIPSA_OWNER_DSN          the OWNER DSN (cross-account, owner-only). Preferred.
  VERIPSA_DSN                fallback DSN if VERIPSA_OWNER_DSN is unset (the least-privilege app role may not
                             be able to read the cross-account surface — then you get a clean denied message,
                             not a crash).

RUN:  python3 github-app/cost_report.py        # print the report once, then exit (a cron-friendly one-shot).
"""
from __future__ import annotations

import json
import os
import sys


def human_bytes(n) -> str:
    """A compact human size: 0 → '0 B', 1536 → '1.5 KiB', etc. Best-effort: a non-number → '?'."""
    try:
        n = float(n)
    except (TypeError, ValueError):
        return "?"
    if n < 0:
        return "?"
    units = ("B", "KiB", "MiB", "GiB", "TiB", "PiB")
    i = 0
    while n >= 1024.0 and i < len(units) - 1:
        n /= 1024.0
        i += 1
    # whole bytes print without a decimal; larger units get one decimal place
    return (f"{int(n)} {units[i]}" if i == 0 else f"{n:.1f} {units[i]}")


def seat_price_usd() -> float:
    """The per-seat monthly price for the MRR PROJECTION (NOT a charge). Tunable via VERIPSA_SEAT_PRICE_USD,
    default 19. Never-crash: junk / negative → the 19.0 default (a projection knob must never throw)."""
    raw = os.environ.get("VERIPSA_SEAT_PRICE_USD")
    if raw is None:
        return 19.0
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return 19.0
    return v if v >= 0 else 19.0


def fetch_cost_surface(dsn: str) -> dict:
    """Run ONE read-only SELECT of core.owner_cost_surface() over the open DSN. Returns the surface dict.

    Raises on a real DB/permission error — main() turns that into a clean message (this stays simple + testable).
    """
    import psycopg2
    conn = psycopg2.connect(dsn)
    try:
        with conn, conn.cursor() as cur:
            cur.execute("SET search_path=core")
            cur.execute("SELECT core.owner_cost_surface()")
            row = cur.fetchone()
            res = row[0] if row else None
            if isinstance(res, dict):
                return res
            return json.loads(res) if res else {}
    finally:
        conn.close()


def fetch_compat_shadow_surface(dsn: str) -> dict | None:
    """Run ONE read-only SELECT of core.owner_compat_shadow_surface() (compat lane PR-5) over the owner DSN.

    GUARDED, unlike fetch_cost_surface: ANY failure — the surface not deployed yet, permission denied, a DB
    hiccup — returns None instead of raising, because the compat shadow block is an ADD-ON section of the
    founder report and must never break the cost report it rides on. render_compat_shadow(None) renders an
    honest all-zeros block (absence of data = zeros, never an error)."""
    try:
        import psycopg2
        conn = psycopg2.connect(dsn)
        try:
            with conn, conn.cursor() as cur:
                cur.execute("SET search_path=core")
                cur.execute("SELECT core.owner_compat_shadow_surface()")
                row = cur.fetchone()
                res = row[0] if row else None
                if isinstance(res, dict):
                    return res
                return json.loads(res) if res else None
        finally:
            conn.close()
    except Exception:
        return None


def render_compat_shadow(surface: dict | None) -> str:
    """Pure FORMATTER for the COMPATIBILITY SHADOW section (compat lane PR-5 + corrective lane S3a). Never
    raises.

    Renders ONLY what the owner lens returns — counts, rule ids (bounded reason codes) and timestamps. No repo
    names, no paths, no symbols, no PR numbers, no SHAs (the surface never carries them). A None / non-dict
    surface (not deployed / denied / dormant lane) degrades to the SAME shape with zeros — the weekly GTM
    review always gets a block to read, never a traceback. These are SHADOW observation counts: internal
    input only, never public proof and never a customer-effect claim (docs/WEEKLY_GTM_REVIEW.md).

    S3a TAXONOMY SPLIT: the report keeps observations (delta/staleness/divergence telemetry — never a
    breakage claim) VISIBLY SEPARATE from the one evidence-backed incompatibility class, mirroring the
    surface's per-class totals — a reader can never mistake the grand total for a breakage count. Tolerates
    a pre-S3a surface (the render_report seat-fields pattern): observations_total falls back to the old
    findings_total key; the split rows then honestly read zero."""
    s = surface if isinstance(surface, dict) else {}

    def n(key, fallback_key=None):
        v = s.get(key)
        if not isinstance(v, (int, float)) and fallback_key is not None:
            v = s.get(fallback_key)
        return int(v) if isinstance(v, (int, float)) else 0

    lines: list[str] = []
    lines.append("")
    lines.append("COMPATIBILITY SHADOW  (internal observation only — never public proof)")
    lines.append("-" * 78)
    if not s:
        lines.append("  (shadow surface unavailable — treating as zero observed)")
    lines.append(f"  Observations recorded (all classes) : {n('observations_total', 'findings_total')}")
    lines.append(f"    - contract deltas (telemetry)     : {n('contract_deltas_total')}")
    lines.append(f"    - rebase needed (advisory)        : {n('rebase_needed_total')}")
    lines.append(f"    - divergent definitions           : {n('divergent_definitions_total')}")
    lines.append(f"    - unclassified (legacy detector)  : {n('unclassified_total')}")
    lines.append(f"  Incompatibilities (evidence-backed) : {n('incompatibilities_total')}")
    lines.append(f"  Head pairs w/ incompatibility       : {n('head_pairs_with_incompatibility')}")
    details = s.get("incompatibilities_by_detail")
    if isinstance(details, dict) and details:
        lines.append("  Incompatibilities by detail         :")
        for rule in sorted(details):
            cnt = details.get(rule)
            cnt_s = int(cnt) if isinstance(cnt, (int, float)) else "?"
            lines.append(f"    - {str(rule):<44} {cnt_s}")
    else:
        lines.append("  Incompatibilities by detail         : (none)")
    lines.append(f"  Repos observed        : {n('repos_observed')}")
    lines.append(f"  Head pairs observed   : {n('head_pairs_observed')}")
    lines.append(f"  Accounts w/ findings  : {n('accounts_with_findings')}")
    first = s.get("first_observed_at")
    last = s.get("last_observed_at")
    lines.append(f"  First observed        : {first if first else '-'}")
    lines.append(f"  Last observed         : {last if last else '-'}")
    rules = s.get("findings_by_rule")
    if isinstance(rules, dict) and rules:
        lines.append("  Observations by rule id (all classes):")
        for rule in sorted(rules):
            cnt = rules.get(rule)
            cnt_s = int(cnt) if isinstance(cnt, (int, float)) else "?"
            lines.append(f"    - {str(rule):<44} {cnt_s}")
    else:
        lines.append("  Observations by rule id (all classes): (none)")
    if s.get("capped"):
        lines.append(f"  ! Bounded view: scanned {n('accounts_scanned')} of {n('account_count')} accounts "
                     f"(cap {n('cap')}) — totals cover the scanned set only.")
    lines.append("=" * 78)
    return "\n".join(lines)


def render_report(surface: dict | None, seat_price: float | None = None) -> str:
    """Pure FORMATTER: a surface dict (or None / empty) → the founder report text. Never raises.

    Degrades cleanly: a falsy / non-dict / shape-less surface returns a single honest line, not a traceback,
    so this is safe to call on whatever the DB (or a not-yet-deployed surface) hands back.

    seat_price: the per-seat monthly price for the MRR PROJECTION (defaults to seat_price_usd() — the env knob).
    Pass an explicit value in tests for determinism. The MRR figure is a PROJECTION, never a charge.
    """
    if not isinstance(surface, dict) or not surface:
        return "DB cost report: no cost surface available yet (is core.owner_cost_surface() deployed?)."
    if seat_price is None:
        seat_price = seat_price_usd()

    total = surface.get("db_total_bytes")
    free = surface.get("free_line") or {}
    accounts = surface.get("accounts")
    if not isinstance(accounts, list):
        accounts = []
    account_count = surface.get("account_count", len(accounts))
    over_count = surface.get("over_line_count")
    if over_count is None:
        over_count = sum(1 for a in accounts if isinstance(a, dict) and a.get("over_free_line"))

    # SEAT (value) dimension — tolerate a surface that predates it (seat fields absent → derive defensively).
    free_seat_line = surface.get("free_seat_line")
    paid_seats_total = surface.get("paid_seats_total")
    if paid_seats_total is None:
        paid_seats_total = sum(int(a.get("paid_seats") or 0) for a in accounts
                               if isinstance(a, dict) and isinstance(a.get("paid_seats"), (int, float)))
    over_seat_count = surface.get("over_seat_line_count")
    if over_seat_count is None:
        over_seat_count = sum(1 for a in accounts if isinstance(a, dict) and a.get("over_seat_line"))

    lines: list[str] = []
    lines.append("=" * 78)
    lines.append("  VERIPSA — DB COST REPORT (owner)")
    lines.append("=" * 78)
    lines.append("")
    lines.append("OVERVIEW")
    lines.append("-" * 78)
    lines.append(f"  Total DB size  : {human_bytes(total)}"
                 + (f"  ({int(total):,} bytes)" if isinstance(total, (int, float)) else ""))
    fr = free.get("max_repos")
    fg = free.get("max_graph_units")
    fe = free.get("max_events")
    lines.append(f"  Free line      : repos<={fr if fr is not None else '?'}   "
                 f"graph_units<={fg if fg is not None else '?'}   "
                 f"events<={fe if fe is not None else '?'}")
    lines.append(f"  Free seats     : <={free_seat_line if free_seat_line is not None else '?'} active human agents "
                 f"(AI agents are free)")
    lines.append(f"  Seat price     : ${seat_price:g} / month  (projection only — not billed)")
    lines.append("")

    # per-account table, biggest footprint first (the surface already orders it; we don't re-sort the data,
    # we only trust + render its order — but we render content-free: ids + counts only).
    lines.append("PER-ACCOUNT FOOTPRINT  (biggest first)")
    lines.append("-" * 78)
    header = (f"{'ACCOUNT':<24} {'REPOS':>6} {'GRAPH':>9} {'EVENTS':>9} {'+7d EV':>8} {'% FREE':>8} "
              f"{'SEATS':>6} {'PAID':>5}   STATUS")
    lines.append(header)
    lines.append("-" * len(header))
    if not accounts:
        lines.append("(no accounts)")
    for a in accounts:
        if not isinstance(a, dict):
            continue
        acct = str(a.get("account_id", "?"))
        if len(acct) > 24:
            acct = acct[:21] + "..."
        repos = a.get("repos", 0)
        units = a.get("graph_units")
        if units is None:  # tolerate a surface that only sends nodes+edges
            n, e = a.get("graph_nodes"), a.get("graph_edges")
            units = (n or 0) + (e or 0) if (n is not None or e is not None) else 0
        events = a.get("events", 0)
        growth = a.get("events_7d", 0)
        pct = a.get("footprint_pct")
        pct_s = f"{float(pct):.1f}%" if isinstance(pct, (int, float)) else "?"
        seats = a.get("active_agents")
        seats_s = str(seats) if isinstance(seats, (int, float)) else "?"
        paid = a.get("paid_seats")
        paid_s = str(paid) if isinstance(paid, (int, float)) else "?"
        # STATUS shows BOTH lines: a footprint breach (OVER FREE LINE) and/or a seat breach (OVER SEAT LINE =
        # a conversion candidate). They are independent (a cheap account can still cross the value/seat line).
        flags = []
        if a.get("over_free_line"):
            flags.append("OVER FREE LINE")
        if a.get("over_seat_line"):
            flags.append("OVER SEAT LINE")
        status = " + ".join(flags) if flags else "ok"
        lines.append(f"{acct:<24} {repos:>6} {units:>9} {events:>9} {growth:>8} {pct_s:>8} "
                     f"{seats_s:>6} {paid_s:>5}   {status}")

    lines.append("")
    lines.append("FOOTPRINT SUMMARY")
    lines.append("-" * 78)
    plural = "s" if account_count != 1 else ""
    lines.append(f"  {account_count} account{plural}, {over_count} over the free line.")
    if over_count:
        lines.append("  ! At least one account is OVER the free line — review the OVER FREE LINE rows above.")

    # ── CONVERSION + MRR (product-led growth) — VISIBILITY + PROJECTION, never a charge. ───────────────────
    # The seat line is the value boundary; paid_seats_total are the humans beyond it; the conversion candidates
    # are the accounts AT/over the line (they have hit the value moment — the founder's funnel to act on).
    try:
        paid_total = int(paid_seats_total or 0)
    except (TypeError, ValueError):
        paid_total = 0
    est_mrr = paid_total * float(seat_price)
    lines.append("")
    lines.append("Conversion / MRR  (projection — not billed)")
    lines.append("-" * 78)
    lines.append(f"  Paid seats (Σ over the line)  : {paid_total}")
    lines.append(f"  Estimated MRR                 : ${est_mrr:,.2f}  "
                 f"({paid_total} seat{'s' if paid_total != 1 else ''} × ${seat_price:g}/mo)")
    cand_plural = "s" if over_seat_count != 1 else ""
    lines.append(f"  Conversion candidates         : {over_seat_count} account{cand_plural} at/over the seat line")
    # name the candidate ids (content-free — ids + their seat counts only), top seats first.
    candidates = [a for a in accounts if isinstance(a, dict) and a.get("over_seat_line")]
    candidates.sort(key=lambda a: (a.get("active_agents") or 0), reverse=True)
    for a in candidates:
        acct = str(a.get("account_id", "?"))
        seats = a.get("active_agents")
        paid = a.get("paid_seats")
        seats_s = seats if isinstance(seats, (int, float)) else "?"
        paid_s = paid if isinstance(paid, (int, float)) else "?"
        lines.append(f"    - {acct:<24} {seats_s} active seat(s), {paid_s} paid")
    lines.append("=" * 78)
    return "\n".join(lines)


def main() -> int:
    # OWNER DSN preferred; fall back to the app DSN (which may be denied the cross-account surface → a clean msg).
    dsn = os.environ.get("VERIPSA_OWNER_DSN") or os.environ.get("VERIPSA_DSN")
    if not dsn:
        print("DB cost report: neither VERIPSA_OWNER_DSN nor VERIPSA_DSN is set — nothing to query.", flush=True)
        return 2
    try:
        surface = fetch_cost_surface(dsn)
    except Exception as e:  # a missing surface / permission denied / DB hiccup → a sentence, never a traceback
        print(f"DB cost report: could not read the cost surface ({str(e)[:200].strip()}).", flush=True)
        return 1
    print(render_report(surface), flush=True)
    # COMPATIBILITY SHADOW section (compat lane PR-5) — the owner-only weekly-review input. Independently
    # guarded: an undeployed/denied compat surface renders as zeros and can never fail the cost report.
    print(render_compat_shadow(fetch_compat_shadow_surface(dsn)), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
