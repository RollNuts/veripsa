#!/usr/bin/env python3
"""Veripsa — the OWNER ACTIVATION-FUNNEL REPORT (Issue #648 / docs/PRODUCT_ACTIVATION_FUNNEL.md).

WHY THIS EXISTS: "a GitHub App install is not activation." Before there is any web console, the weekly GTM
review still needs ONE screen answering how far live installations get down the activation path — install ->
selected a repo -> first eligible PR traffic -> first non-Clear signal -> repeat use. This is that screen: a
terminal report the operator runs by hand (or from cron), no UI required.

WHAT IT DOES: connects as the OWNER (VERIPSA_OWNER_DSN, or VERIPSA_DSN as a fallback) — the cross-account,
owner-only role — and calls `core.owner_activation_funnel_surface()` (a content-free aggregate the sibling DB
lane owns). It prints the A1..A9 funnel as COUNTS only: installs (total / live / owner-vs-external split),
installations that selected a repo (A2), reached first PR traffic (A3), saw a first non-Clear signal (A5, with
the warn-vs-serialize split), and showed repeat use within 7 days (A9), plus the install->first-PR latency
aggregate. A4 (first PR Check published) is now DB-derivable from the 'check_published' event ledger — printed
with its first-check signal split and two content-free latency aggregates (install->check, first-PR->check).
Stages A6 (PR comment), A7 (ACK) and A8 (required-check config) remain GitHub-only and are NOT persisted in this
DB — they are printed as an explicit "not DB-derivable (GitHub-only)" line, never fabricated.

READ-ONLY (it only SELECTs the surface — it never changes the write gate, webhook routing, or any runtime
decision). NEVER-CRASH: a missing surface / unset DSN / a DB hiccup degrades to a clean one-line message, never
a traceback (an operator running this at 2am must get a sentence, not a stack). CONTENT-FREE (the moat,
unchanged): it prints ONLY what the surface returns — counts, latency SECONDS and timestamps. No repo names, no
paths, no code, no SHAs, no DSN. The FORMATTER (render_report) is a pure function over the surface JSON, so it
is unit-testable with no DB.

AUTHORITATIVE-COUNT CAVEAT: the DB only ever sees an installation that has fired at least ONE webhook Veripsa
persisted. The GitHub App owner console's install count is the authoritative install total; this report's A1 is
"installs that reached the DB", which can lag the console for a brand-new install that has produced no traffic.

ENV:
  VERIPSA_OWNER_DSN          the OWNER DSN (cross-account, owner-only). Preferred.
  VERIPSA_DSN                fallback DSN if VERIPSA_OWNER_DSN is unset (the least-privilege app role may not be
                             able to read the cross-account surface — then you get a clean denied message, not
                             a crash).

RUN:  python3 github-app/activation_report.py     # print the report once, then exit (a cron-friendly one-shot).
"""
from __future__ import annotations

import json
import os
import sys


def _int(v):
    """Best-effort int: a number -> int, anything else -> 0 (a report cell must never throw)."""
    return int(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else 0


def human_duration(seconds) -> str:
    """A compact human duration from SECONDS: 0 -> '0s', 5400 -> '1.5h', 200000 -> '2.3d'. Non-number -> '-'."""
    try:
        n = float(seconds)
    except (TypeError, ValueError):
        return "-"
    if n < 0:
        return "-"
    if n < 60:
        return f"{int(n)}s"
    if n < 3600:
        return f"{n / 60:.1f}m"
    if n < 86400:
        return f"{n / 3600:.1f}h"
    return f"{n / 86400:.1f}d"


def fetch_activation_surface(dsn: str) -> dict:
    """Run ONE read-only SELECT of core.owner_activation_funnel_surface() over the open DSN. Returns the dict.

    Raises on a real DB/permission error — main() turns that into a clean message (this stays simple + testable).
    """
    import psycopg2
    conn = psycopg2.connect(dsn)
    try:
        with conn, conn.cursor() as cur:
            cur.execute("SET search_path=core")
            cur.execute("SELECT core.owner_activation_funnel_surface()")
            row = cur.fetchone()
            res = row[0] if row else None
            if isinstance(res, dict):
                return res
            return json.loads(res) if res else {}
    finally:
        conn.close()


def _pct(num, den) -> str:
    """A content-free conversion-rate cell: num/den as a percent, or '-' when the denominator is zero."""
    n, d = _int(num), _int(den)
    if d <= 0:
        return "-"
    return f"{100.0 * n / d:.0f}%"


def render_report(surface: dict | None) -> str:
    """Pure FORMATTER: a surface dict (or None / empty) -> the activation-funnel report text. Never raises.

    Degrades cleanly: a falsy / non-dict / shape-less surface returns a single honest line, not a traceback, so
    this is safe to call on whatever the DB (or a not-yet-deployed surface) hands back. Content-free by
    construction — it renders only counts, latency seconds and timestamps, with the A1..A9 labels (A4 first PR
    check published + its signal split and latencies) + the explicit GitHub-only note for A6/A7/A8.
    """
    if not isinstance(surface, dict) or not surface:
        return ("Activation funnel report: no activation surface available yet "
                "(is core.owner_activation_funnel_surface() deployed?).")

    installs_total = _int(surface.get("installs_total"))
    installs_live = _int(surface.get("installs_live"))
    live_accounts = _int(surface.get("live_accounts"))
    external = _int(surface.get("external_accounts"))
    owner = _int(surface.get("owner_accounts"))
    a2 = _int(surface.get("a2_selected_repo"))
    a3 = _int(surface.get("a3_first_pr"))
    a5 = _int(surface.get("a5_first_signal"))
    a9 = _int(surface.get("a9_repeat_7d"))
    a4 = _int(surface.get("a4_first_check"))
    sig = surface.get("a4_signal_distribution") if isinstance(surface.get("a4_signal_distribution"), dict) else {}
    ic = surface.get("install_to_check_latency") if isinstance(surface.get("install_to_check_latency"), dict) else {}
    pc = surface.get("pr_event_to_check_latency") if isinstance(surface.get("pr_event_to_check_latency"), dict) else {}
    signals = surface.get("signals") if isinstance(surface.get("signals"), dict) else {}
    warn = _int(signals.get("warn"))
    serialize = _int(signals.get("serialize"))
    lat = surface.get("a1_to_a3_latency") if isinstance(surface.get("a1_to_a3_latency"), dict) else {}
    lat_n = _int(lat.get("count_with_latency"))
    median = lat.get("median_seconds")
    p90 = lat.get("p90_seconds")
    buckets = lat.get("buckets") if isinstance(lat.get("buckets"), dict) else {}

    lines: list[str] = []
    lines.append("=" * 78)
    lines.append("  VERIPSA — ACTIVATION FUNNEL (owner)")
    lines.append("=" * 78)
    lines.append("")
    lines.append("A1 — INSTALLS")
    lines.append("-" * 78)
    lines.append(f"  Installs (rows)          : {installs_total}   (live: {installs_live})")
    lines.append(f"  Live tenants             : {live_accounts}   (external: {external}, owner/dogfood: {owner})")
    lines.append("  NOTE: the GitHub App owner console install count is AUTHORITATIVE. The DB only holds installs")
    lines.append("        that fired at least one webhook Veripsa persisted, so A1 can lag the console.")
    lines.append("")
    lines.append("FUNNEL  (installations reaching each stage; denominator = live tenants)")
    lines.append("-" * 78)
    lines.append(f"{'STAGE':<40} {'COUNT':>7} {'OF LIVE':>9}")
    lines.append("-" * 58)
    lines.append(f"{'A1  installed (live tenants)':<40} {live_accounts:>7} {'100%' if live_accounts else '-':>9}")
    lines.append(f"{'A2  selected >=1 repository':<40} {a2:>7} {_pct(a2, live_accounts):>9}")
    lines.append(f"{'A3  first eligible PR traffic (proxy)':<40} {a3:>7} {_pct(a3, live_accounts):>9}")
    lines.append(f"{'A4  first PR check published':<40} {a4:>7} {_pct(a4, live_accounts):>9}")
    lines.append(f"{'A5  first non-Clear signal':<40} {a5:>7} {_pct(a5, live_accounts):>9}")
    lines.append(f"{'A9  repeat use within 7 days':<40} {a9:>7} {_pct(a9, live_accounts):>9}")
    lines.append("")
    # HUMAN JUDGMENT — the only signal that counts toward market validation, and the one the honest-conclusion
    # rule keys on. Shown right after the funnel so an operator cannot read A1..A9 as evidence of value:
    # reaching A9 means the machinery ran, not that anybody found it useful.
    hj = surface.get("human_judgment") if isinstance(surface.get("human_judgment"), dict) else {}
    wa = surface.get("workflow_action") if isinstance(surface.get("workflow_action"), dict) else {}
    hj_total, hj_ext = _int(hj.get("total")), _int(hj.get("external_total"))
    hj_useful_ext = _int(hj.get("useful_external"))
    lines.append("HUMAN JUDGMENT (what a person said about the advice)")
    lines.append("-" * 78)
    lines.append(f"  judgments recorded        : {hj_total}   (external: {hj_ext})")
    lines.append(f"  EXTERNAL 'useful'         : {hj_useful_ext}")
    by = hj.get("by_judgment") if isinstance(hj.get("by_judgment"), dict) else {}
    if by:
        for k in sorted(by):
            lines.append(f"    {k:<26}: {_int(by.get(k))}")
    else:
        lines.append("    (none recorded yet)")
    if wa:
        lines.append("  workflow action taken:")
        for k in sorted(wa):
            lines.append(f"    {k:<26}: {_int(wa.get(k))}")
    if hj_useful_ext == 0:
        lines.append("  CONCLUSION: external human-confirmed useful judgments = 0.")
        lines.append("              Product-ready. External-validation-ready. Market value still unvalidated.")
        lines.append("              An install, a page view, or a published Check is NOT value evidence.")
    lines.append("")
    lines.append("A5 — FIRST NON-CLEAR SIGNAL (split)")
    lines.append("-" * 78)
    lines.append(f"  warn (Heads up)          : {warn}")
    lines.append(f"  serialize (Wait in line) : {serialize}")
    lines.append("  NOTE: a Clear or Unknown verdict writes NO event, so A5 measures the first warn/serialize only,")
    lines.append("        never the first clear/unknown signal.")
    lines.append("")
    lines.append("A4 — FIRST PR CHECK PUBLISHED (signal at first publish; A3 -> A4 conversion)")
    lines.append("-" * 78)
    lines.append(f"  A3 -> A4 conversion      : {_pct(a4, a3)}   ({a4} of {a3} PR-active installs got a check)")
    lines.append(f"  First-check signal split : Clear {_int(sig.get('clear'))}   "
                 f"Heads up {_int(sig.get('heads_up'))}   Wait in line {_int(sig.get('wait_in_line'))}   "
                 f"Unknown {_int(sig.get('unknown'))}   Paused {_int(sig.get('paused'))}")
    lines.append("  Publication failures     : not DB-derivable (a failed post writes no row; see operator log)")
    ic_med, ic_p90 = ic.get("median_seconds"), ic.get("p90_seconds")
    pc_med, pc_p90 = pc.get("median_seconds"), pc.get("p90_seconds")
    lines.append(f"  Install -> first check   : n={_int(ic.get('count_with_latency'))}   "
                 f"median {human_duration(ic_med) if isinstance(ic_med, (int, float)) else '-'}   "
                 f"p90 {human_duration(ic_p90) if isinstance(ic_p90, (int, float)) else '-'}")
    lines.append(f"  First PR -> first check  : n={_int(pc.get('count_with_latency'))}   "
                 f"median {human_duration(pc_med) if isinstance(pc_med, (int, float)) else '-'}   "
                 f"p90 {human_duration(pc_p90) if isinstance(pc_p90, (int, float)) else '-'}")
    lines.append("")
    lines.append("A1 -> A3 LATENCY  (install -> first PR-claim; content-free seconds)")
    lines.append("-" * 78)
    lines.append(f"  Installs with a first PR : {lat_n}")
    med_s = f"{human_duration(median)} ({_int(median)}s)" if isinstance(median, (int, float)) else "-"
    p90_s = f"{human_duration(p90)} ({_int(p90)}s)" if isinstance(p90, (int, float)) else "-"
    lines.append(f"  Median                   : {med_s}")
    lines.append(f"  p90                      : {p90_s}")
    lines.append(f"  Buckets                  : <1h {_int(buckets.get('under_1h'))}   "
                 f"1h-1d {_int(buckets.get('1h_to_1d'))}   "
                 f"1d-7d {_int(buckets.get('1d_to_7d'))}   "
                 f">=7d {_int(buckets.get('over_7d'))}")
    lines.append("")
    lines.append("GITHUB-ONLY STAGES  (not persisted in this DB — never fabricated)")
    lines.append("-" * 78)
    lines.append("  A6 first PR brief comment     : not DB-derivable (GitHub-only)")
    lines.append("  A7 first ACK                  : not DB-derivable (GitHub-only)")
    lines.append("  A8 required-check configured  : not DB-derivable (GitHub-only)")
    first = surface.get("first_activity_at")
    last = surface.get("last_activity_at")
    lines.append("")
    lines.append("WINDOW")
    lines.append("-" * 78)
    lines.append(f"  First activity observed  : {first if first else '-'}")
    lines.append(f"  Last activity observed   : {last if last else '-'}")
    if surface.get("capped"):
        lines.append(f"  ! Bounded view: scanned {_int(surface.get('accounts_scanned'))} of "
                     f"{_int(surface.get('account_count'))} tenants (cap {_int(surface.get('cap'))}) — "
                     f"A2..A9 cover the scanned set only.")
    lines.append("=" * 78)
    return "\n".join(lines)


def main() -> int:
    # OWNER DSN preferred; fall back to the app DSN (which may be denied the cross-account surface → a clean msg).
    dsn = os.environ.get("VERIPSA_OWNER_DSN") or os.environ.get("VERIPSA_DSN")
    if not dsn:
        print("Activation funnel report: neither VERIPSA_OWNER_DSN nor VERIPSA_DSN is set — nothing to query.",
              flush=True)
        return 2
    try:
        surface = fetch_activation_surface(dsn)
    except Exception as e:  # a missing surface / permission denied / DB hiccup → a sentence, never a traceback
        print(f"Activation funnel report: could not read the activation surface ({str(e)[:200].strip()}).",
              flush=True)
        return 1
    print(render_report(surface), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
