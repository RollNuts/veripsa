#!/usr/bin/env python3
"""Cost-report gate — the FOUNDER DB-cost screen + the DB-cost alert must not silently rot.

Two halves, both PURE (no DB, no network, a fake webhook poster):

FORMATTER (cost_report.render_report over a representative owner_cost_surface() JSON dict):
  • renders the total DB size (human bytes) + the free-line thresholds.
  • renders a per-account row and marks an over-the-line account OVER FREE LINE (and an in-line one 'ok').
  • preserves the surface's top-consumer ordering (it does NOT re-sort — biggest footprint stays first).
  • is content-free (prints only ids + counts the surface returned — no repo names / paths / DSN).
  • degrades to a CLEAN one-line message on a missing/empty surface (never a traceback) — never-crash.
  • human_bytes is sane across scales (B / KiB / MiB / GiB) and tolerates junk → '?'.

ALERT (cost_report's signal flowing through alerts.evaluate_cost on the shared AlertSink):
  • fires db_size_high when db_total_bytes ≥ threshold; RESOLVES (recovers) when back under.
  • fires account_over_line when over_line_count > 0; resolves when 0.
  • edge-triggered: a sustained breach within the window pages ONCE, not every tick.
  • content-free: the alert body carries only the size/count — no account id leaks.
  • fail-open: a webhook that THROWS on POST never propagates out of evaluate_cost.
  • a missing/empty surface fires NOTHING (absence is not a breach — no false page).

Run:  python3 tests/test_cost_report.py
"""
from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "github-app"))

import alerts  # noqa: E402
import cost_report as C  # noqa: E402


# ── a deterministic clock + a capturing fake webhook poster (same shape as test_ops_alerting) ───────────────
class FakeClock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class FakePoster:
    def __init__(self, explode=False):
        self.posts = []
        self.explode = explode

    def __call__(self, url, body):
        if self.explode:
            raise OSError("webhook is down")
        self.posts.append((url, body))


def sample_surface() -> dict:
    """A representative core.owner_cost_surface() return: two accounts, biggest footprint first, one OVER.

    Seat (value) dimension: free_seat_line=2. ACCT-BIG runs 4 active human seats → 2 paid, AT/over the line (a
    conversion candidate). ACCT-SMALL runs 2 → 0 paid but AT the line (also a candidate: the next hire tips it).
    paid_seats_total=2, over_seat_line_count=2.
    """
    return {
        "db_total_bytes": 180 * 1024 * 1024,  # 180 MiB
        "free_line": {"max_repos": 3, "max_graph_units": 50000, "max_events": 100000, "max_seats": 2},
        "account_count": 2,
        "over_line_count": 1,
        "free_seat_line": 2,
        "paid_seats_total": 2,
        "over_seat_line_count": 2,
        "accounts": [
            {"account_id": "ACCT-BIG", "repos": 5, "graph_nodes": 40000, "graph_edges": 30000,
             "graph_units": 70000, "events": 150000, "events_7d": 9000, "footprint_pct": 71.4,
             "over_free_line": True, "active_agents": 4, "paid_seats": 2, "over_seat_line": True},
            {"account_id": "ACCT-SMALL", "repos": 1, "graph_nodes": 800, "graph_edges": 600,
             "graph_units": 1400, "events": 1200, "events_7d": 50, "footprint_pct": 1.3,
             "over_free_line": False, "active_agents": 2, "paid_seats": 0, "over_seat_line": True},
        ],
    }


def formatter_checks():
    results = []

    def check(name, cond):
        results.append((name, bool(cond)))
        print(f"  [{'PASS' if cond else 'FAIL'}] {name}")

    surface = sample_surface()
    out = C.render_report(surface)

    # total size (human) + raw bytes, and the free-line thresholds, all present
    check("renders the total DB size in human bytes (180.0 MiB)", "180.0 MiB" in out)
    check("renders the raw byte count alongside the human size", f"{180 * 1024 * 1024:,}" in out)
    check("renders the free-line thresholds (repos / graph_units / events)",
          "repos<=3" in out and "graph_units<=50000" in out and "events<=100000" in out)

    # the over-line account is MARKED; the in-line one is not (footprint line)
    check("marks the over-line account OVER FREE LINE", "ACCT-BIG" in out and "OVER FREE LINE" in out)
    # the per-account TABLE rows (not the header, not the conversion-candidate detail lines) carry the footprint %.
    big_line = next((ln for ln in out.splitlines() if "ACCT-BIG" in ln and "71.4%" in ln), "")
    small_line = next((ln for ln in out.splitlines() if "ACCT-SMALL" in ln and "1.3%" in ln), "")
    check("the over-line ROW itself carries the OVER FREE LINE marker", "OVER FREE LINE" in big_line)
    check("the in-line account is NOT marked over the FOOTPRINT free line (no false OVER FREE LINE on small)",
          "OVER FREE LINE" not in small_line)
    check("renders each account's footprint % (71.4% for the big one)", "71.4%" in big_line)
    check("renders 7-day growth (the +7d column carries the big account's 9000)", "9000" in big_line)

    # TOP-CONSUMER ORDERING preserved (render does not re-sort — biggest footprint stays first)
    check("preserves top-consumer ordering (ACCT-BIG appears before ACCT-SMALL)",
          out.index("ACCT-BIG") < out.index("ACCT-SMALL"))

    # summary line
    check("summary line reports the account + over-line counts", "2 account" in out and "1 over the free line" in out)

    # ── SEAT (value) LINE + CONVERSION / MRR ──────────────────────────────────────────────────────────────
    out_seat = C.render_report(surface, seat_price=19.0)  # explicit price for a deterministic MRR figure
    # the free seat line is rendered + the seat columns carry per-account active_agents / paid_seats
    check("renders the free seat line (<=2)", "Free seats" in out_seat and "<=2" in out_seat)
    big_seat = next((ln for ln in out_seat.splitlines() if "ACCT-BIG" in ln and "71.4%" in ln), "")
    small_seat = next((ln for ln in out_seat.splitlines() if "ACCT-SMALL" in ln and "1.3%" in ln), "")
    # the SEATS/PAID columns sit just before STATUS; split off the status text, the last two numeric tokens are them.
    big_cols = big_seat.split("OVER")[0].split()  # tokens up to the STATUS flags
    check("the big account row shows 4 active seats + 2 paid (SEATS/PAID columns)", big_cols[-2:] == ["4", "2"])
    check("an at/over-seat-line account is marked OVER SEAT LINE", "OVER SEAT LINE" in big_seat)
    check("a conversion candidate at the line (0 paid) is STILL flagged OVER SEAT LINE", "OVER SEAT LINE" in small_seat)
    # an account can be OVER both lines at once (footprint + seat) — independent flags on one row
    check("the big account is flagged on BOTH lines (OVER FREE LINE + OVER SEAT LINE)",
          "OVER FREE LINE" in big_seat and "OVER SEAT LINE" in big_seat)
    # CONVERSION / MRR projection block
    check("renders a Conversion / MRR section (labelled a projection, not billed)",
          "Conversion / MRR" in out_seat and "projection" in out_seat.lower() and "not billed" in out_seat.lower())
    check("estimated MRR = paid_seats_total (2) × $19 = $38.00", "$38.00" in out_seat)
    check("reports paid seats total (2)", "Paid seats" in out_seat and "Conversion / MRR" in out_seat)
    check("reports 2 conversion candidates (both accounts are at/over the seat line)",
          "Conversion candidates" in out_seat and "2 account" in out_seat.split("Conversion candidates")[1])
    check("names each conversion-candidate account id (ACCT-BIG + ACCT-SMALL listed under candidates)",
          "ACCT-BIG" in out_seat.split("Conversion candidates")[1] and "ACCT-SMALL" in out_seat.split("Conversion candidates")[1])
    # the seat-price knob is honored: a different price changes only the MRR projection
    out_seat_9 = C.render_report(surface, seat_price=9.0)
    check("seat price knob flows into the MRR figure (price 9 → $18.00)", "$18.00" in out_seat_9)
    # seat MRR is content-free too (no human names; the surface carries ids + counts only)
    check("conversion/MRR block stays content-free (no display-name-shaped token)",
          "Alice" not in out_seat and "Bob" not in out_seat and "@" not in out_seat.split("Conversion / MRR")[1])
    # seat fields ABSENT (a surface predating the seat dimension) → seat columns degrade to '?' / 0, never crash
    legacy = {"db_total_bytes": 1024, "free_line": {"max_repos": 3},
              "account_count": 1, "over_line_count": 0,
              "accounts": [{"account_id": "ACCT-OLD", "repos": 1, "graph_units": 10, "events": 5,
                            "events_7d": 1, "footprint_pct": 5.0, "over_free_line": False}]}
    out_legacy = C.render_report(legacy, seat_price=19.0)
    check("a seat-less (legacy) surface still renders (seats degrade, no crash)",
          "ACCT-OLD" in out_legacy and "Conversion / MRR" in out_legacy)
    check("legacy surface → 0 paid seats / $0.00 MRR (no seat data = nothing projected)", "$0.00" in out_legacy)

    # CONTENT-FREE: only ids + counts; a (hypothetical) repo name / path / DSN must never appear. The sample has
    # none, so we assert the report stays free of obvious content-shaped leakage tokens.
    check("content-free: no DSN-shaped string in the report", "postgres://" not in out and "postgresql://" not in out)
    check("content-free: prints only ids + counts (no '.py' path-shaped token leaked)", ".py" not in out)

    # NEVER-CRASH: a missing / empty / shape-less surface degrades to a clean one-line message
    empty_msg = C.render_report({})
    none_msg = C.render_report(None)
    junk_msg = C.render_report("not a dict")  # type: ignore[arg-type]
    check("empty surface → a clean message (no crash, no table)", "no cost surface" in empty_msg.lower())
    check("None surface → the same clean message", "no cost surface" in none_msg.lower())
    check("non-dict surface → the same clean message (never-crash)", "no cost surface" in junk_msg.lower())

    # a surface with NO accounts still renders cleanly (header + an explicit '(no accounts)')
    no_accts = C.render_report({"db_total_bytes": 1024, "free_line": {}, "account_count": 0,
                                "over_line_count": 0, "accounts": []})
    check("zero-account surface renders cleanly with '(no accounts)'", "(no accounts)" in no_accts)

    # human_bytes across scales + junk tolerance
    check("human_bytes(0) == '0 B'", C.human_bytes(0) == "0 B")
    check("human_bytes(1536) == '1.5 KiB'", C.human_bytes(1536) == "1.5 KiB")
    check("human_bytes(1 GiB) == '1.0 GiB'", C.human_bytes(1024 ** 3) == "1.0 GiB")
    check("human_bytes(junk) → '?' (never-crash)", C.human_bytes(None) == "?" and C.human_bytes("x") == "?")

    return results


def alert_checks():
    results = []

    def check(name, cond):
        results.append((name, bool(cond)))
        print(f"  [{'PASS' if cond else 'FAIL'}] {name}")

    threshold = 200 * 1024 * 1024  # 200 MiB

    # 1) db_size_high fires at/over threshold and RESOLVES when back under (recovery edge)
    clk = FakeClock()
    poster = FakePoster()
    sink = alerts.AlertSink(webhook_url="https://hook/x", min_interval=900, clock=clk, poster=poster)
    alerts.evaluate_cost(sink, {"db_total_bytes": threshold, "over_line_count": 0}, size_threshold_bytes=threshold)
    check("db_size_high fires at/over the threshold (warning)",
          any(p[1]["key"] == "db_size_high" and p[1]["level"] == "warning" for p in poster.posts))
    poster.posts.clear()
    alerts.evaluate_cost(sink, {"db_total_bytes": threshold - 1, "over_line_count": 0}, size_threshold_bytes=threshold)
    check("db_size_high RESOLVES when the DB shrinks back under the threshold",
          any(p[1]["key"] == "db_size_high" and "recover" in p[1]["text"].lower() for p in poster.posts))

    # 2) account_over_line fires when over_line_count > 0 and resolves at 0
    clk2 = FakeClock()
    poster2 = FakePoster()
    sink2 = alerts.AlertSink(webhook_url="https://hook/x", min_interval=900, clock=clk2, poster=poster2)
    alerts.evaluate_cost(sink2, {"db_total_bytes": 10, "over_line_count": 2}, size_threshold_bytes=threshold)
    check("account_over_line fires when over_line_count > 0",
          any(p[1]["key"] == "account_over_line" for p in poster2.posts))
    check("under-threshold size does NOT also fire db_size_high",
          not any(p[1]["key"] == "db_size_high" for p in poster2.posts))
    poster2.posts.clear()
    alerts.evaluate_cost(sink2, {"db_total_bytes": 10, "over_line_count": 0}, size_threshold_bytes=threshold)
    check("account_over_line resolves when no account is over",
          any(p[1]["key"] == "account_over_line" and "recover" in p[1]["text"].lower() for p in poster2.posts))

    # 2b) over_line_count omitted → counted defensively from the accounts list (still content-free)
    clk2b = FakeClock()
    poster2b = FakePoster()
    sink2b = alerts.AlertSink(webhook_url="https://hook/x", min_interval=900, clock=clk2b, poster=poster2b)
    alerts.evaluate_cost(sink2b, {"db_total_bytes": 10,
                                  "accounts": [{"account_id": "A", "over_free_line": True},
                                               {"account_id": "B", "over_free_line": False}]},
                         size_threshold_bytes=threshold)
    check("over_line_count omitted → counted from accounts (fires account_over_line)",
          any(p[1]["key"] == "account_over_line" for p in poster2b.posts))

    # 3) edge-triggered: a SUSTAINED breach within the window pages ONCE, not every tick
    clk3 = FakeClock()
    poster3 = FakePoster()
    sink3 = alerts.AlertSink(webhook_url="https://hook/x", min_interval=900, clock=clk3, poster=poster3)
    for _ in range(5):
        clk3.t += 30  # 5 ticks, 30s apart, all within the 900s window
        alerts.evaluate_cost(sink3, {"db_total_bytes": threshold + 1, "over_line_count": 0},
                             size_threshold_bytes=threshold)
    size_pages = [p for p in poster3.posts if p[1]["key"] == "db_size_high"]
    check("sustained DB-size breach pages ONCE within the window (edge-triggered)", len(size_pages) == 1)

    # 4) content-free: the alert body carries only size/count — NO account id leaks
    leak = any(("ACCT" in str(p[1].get("text", "")) or "ACCT" in str(p[1].get("fields", "")))
               for p in poster2b.posts)
    check("content-free: no account id in the alert body (only counts cross the boundary)", not leak)
    over_body = next((p[1] for p in poster2.posts if p[1]["key"] == "account_over_line"), {})
    check("content-free: alert fields are the rollup count only",
          set(over_body.get("fields", {}).keys()) <= {"over_line_count"})

    # 5) FAIL-OPEN: a webhook that throws on POST never propagates out of evaluate_cost
    poster5 = FakePoster(explode=True)
    sink5 = alerts.AlertSink(webhook_url="https://hook/x", min_interval=0, clock=FakeClock(), poster=poster5)
    threw = False
    try:
        alerts.evaluate_cost(sink5, {"db_total_bytes": threshold + 1, "over_line_count": 1},
                             size_threshold_bytes=threshold)
    except Exception:
        threw = True
    check("evaluate_cost is FAIL-OPEN (a throwing webhook never propagates)", not threw)

    # 6) a missing/empty surface fires NOTHING (absence is not a breach)
    clk6 = FakeClock()
    poster6 = FakePoster()
    sink6 = alerts.AlertSink(webhook_url="https://hook/x", min_interval=0, clock=clk6, poster=poster6)
    alerts.evaluate_cost(sink6, None, size_threshold_bytes=threshold)
    alerts.evaluate_cost(sink6, {}, size_threshold_bytes=threshold)
    check("a missing/empty surface fires NO alert (no false page)", len(poster6.posts) == 0)

    return results


def main() -> int:
    print("== cost report FORMATTER (pure: render_report over a surface dict, no DB) ==")
    results = formatter_checks()
    print("== DB-cost ALERT (pure: evaluate_cost on the shared AlertSink, no DB) ==")
    results += alert_checks()

    ok = all(c for _, c in results)
    print("COST-REPORT GATE:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
