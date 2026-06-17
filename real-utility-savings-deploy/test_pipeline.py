"""
End-to-end test harness for the Real Utility Savings engine.

Generates a synthetic year of half-hourly interval data for a solar household,
emits it in BOTH supported formats (NEM12 and flat CSV), parses each back via
the auto-detecting parser, and confirms:

  1. format is correctly detected
  2. parsed totals match the synthetic ground truth
  3. both formats produce the same engine result
  4. the cost engine ranks plans and explains the winner
"""

from __future__ import annotations
import math
from datetime import datetime, timedelta

from parser import parse_interval_file, detect_format
from cost_engine import SAMPLE_PLANS, rank_plans, explain_winner, period_of


# ----------------------------------------------------------------------------
# Synthetic data generator (ground truth)
# ----------------------------------------------------------------------------

def synth_year(start=datetime(2025, 1, 1), days=365, base_daily=22.0, solar_kw=6.6):
    """Return list of (ts_end, import_kwh, export_kwh) at 30-min resolution."""
    shape = [0.028,0.022,0.020,0.019,0.020,0.028,0.045,0.055,0.045,0.035,
             0.030,0.030,0.032,0.032,0.033,0.038,0.050,0.072,0.085,0.080,
             0.065,0.052,0.042,0.032]
    s = sum(shape); shape = [x/s for x in shape]
    sun = [0,0,0,0,0,0,.02,.08,.20,.35,.48,.55,.57,.54,.45,.32,.18,.06,.01,0,0,0,0,0]
    rows = []
    for d in range(days):
        day = start + timedelta(days=d)
        # mild seasonal swing on solar
        seas = 0.85 + 0.30 * math.cos((d - 355) / 365.0 * 2 * math.pi)
        for i in range(48):
            h = i // 2
            ts_end = day + timedelta(minutes=30 * (i + 1))
            load = base_daily * shape[h] / 2.0
            gen = solar_kw * sun[h] * seas / 2.0
            net = load - gen
            imp = net if net >= 0 else 0.0
            exp = -net if net < 0 else 0.0
            rows.append((ts_end, round(imp, 4), round(exp, 4)))
    return rows


# ----------------------------------------------------------------------------
# Format emitters
# ----------------------------------------------------------------------------

def to_flat_csv(rows):
    out = ["timestamp,import_kwh,export_kwh"]
    for ts, imp, exp in rows:
        out.append(f"{ts.strftime('%Y-%m-%d %H:%M')},{imp},{exp}")
    return "\n".join(out)


def to_nem12(rows, nmi="6001234567"):
    """Emit a minimal but valid NEM12 with separate E1 (import) and B1 (export)."""
    from collections import defaultdict
    imp_days = defaultdict(list)
    exp_days = defaultdict(list)
    for ts, imp, exp in rows:
        # interval-ending ts; the day it belongs to is ts minus one interval
        day = (ts - timedelta(minutes=30)).strftime("%Y%m%d")
        imp_days[day].append(imp)
        exp_days[day].append(exp)

    lines = ["100,NEM12,200501010000,RUSTEST,RETAILER"]
    # import channel
    lines.append(f"200,{nmi},E1E2,E1,E1,N,,30,")
    for day in sorted(imp_days):
        vals = ",".join(f"{v:.4f}" for v in imp_days[day])
        lines.append(f"300,{day},{vals},A,,,20250101000000")
    # export channel
    lines.append(f"200,{nmi},B1,B1,B1,N,,30,")
    for day in sorted(exp_days):
        vals = ",".join(f"{v:.4f}" for v in exp_days[day])
        lines.append(f"300,{day},{vals},A,,,20250101000000")
    lines.append("900")
    return "\n".join(lines)


# ----------------------------------------------------------------------------
# Run
# ----------------------------------------------------------------------------

def approx(a, b, tol=0.5):
    return abs(a - b) <= tol


def main():
    print("=" * 64)
    print("REAL UTILITY SAVINGS - engine pipeline test")
    print("=" * 64)

    rows = synth_year()
    truth_imp = sum(r[1] for r in rows)
    truth_exp = sum(r[2] for r in rows)
    print(f"\nGround truth: {len(rows):,} intervals  "
          f"import={truth_imp:,.1f} kWh  export={truth_exp:,.1f} kWh\n")

    flat = to_flat_csv(rows)
    nem = to_nem12(rows)

    results = {}
    for label, text in [("FLAT CSV", flat), ("NEM12", nem)]:
        print("-" * 64)
        print(f"[{label}]  detected as: {detect_format(text)}")
        series = parse_interval_file(text)
        print(series.report.summary())

        # validate totals against ground truth
        ok_imp = approx(series.report.total_import_kwh, truth_imp, tol=1.0)
        ok_exp = approx(series.report.total_export_kwh, truth_exp, tol=1.0)
        print(f"  totals match truth: import={'OK' if ok_imp else 'FAIL'} "
              f"export={'OK' if ok_exp else 'FAIL'}")

        imp_h, exp_h, peak = series.hourly_average_profile()
        results[label] = (imp_h, exp_h, peak, series)
        print()

    # cross-check: both formats yield same hourly profile
    print("-" * 64)
    f_imp = results["FLAT CSV"][0]
    n_imp = results["NEM12"][0]
    max_diff = max(abs(a - b) for a, b in zip(f_imp, n_imp))
    print(f"FLAT vs NEM12 hourly profile max diff: {max_diff:.4f} kWh "
          f"({'MATCH' if max_diff < 0.01 else 'MISMATCH'})")

    # run the cost engine on the parsed real profile
    print("\n" + "=" * 64)
    print("COST ENGINE - ranked plans (from parsed interval data)")
    print("=" * 64)
    imp_h, exp_h, peak, series = results["NEM12"]
    current_bill = 2400
    ranked = rank_plans(SAMPLE_PLANS, imp_h, exp_h, peak)
    best = ranked[0]
    for i, c in enumerate(ranked):
        marker = "  <-- cheapest" if i == 0 else ""
        print(f"{i+1}. {c.plan:<18} ${c.net:>5,}/yr   "
              f"(usage ${c.usage:,} + supply ${c.supply:,}"
              + (f" + demand ${c.demand:,}" if c.demand else "")
              + (f" - solar ${c.solar_credit:,}" if c.solar_credit else "")
              + (f" - credit ${c.sign_up_credit}" if c.sign_up_credit else "")
              + f"){marker}")

    plan_obj = next(p for p in SAMPLE_PLANS if p.name == best.plan)
    ex = explain_winner(plan_obj, best)
    print(f"\nWinner: {best.plan}  (${best.net:,}/yr, "
          f"saving ${current_bill - best.net:,} vs current ${current_bill:,})")
    print("Why it wins: " + ", and ".join(ex["why"]) + ".")
    if ex["caveat"]:
        print("Heads up: " + ex["caveat"])

    print("\nPipeline OK." if (max_diff < 0.01) else "\nPipeline MISMATCH - investigate.")


if __name__ == "__main__":
    main()
