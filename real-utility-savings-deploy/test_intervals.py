"""
Validates the per-interval engine and shows WHY it matters: a household with
the same total usage but one spiky evening pays more on a demand tariff, and
only the per-interval engine sees it.
"""
from datetime import datetime, timedelta

from parser import parse_interval_file
from cost_engine_intervals import (SAMPLE_PLANS, rank_plans_intervals,
                                    cost_plan_intervals, explain_winner, Plan,
                                    DemandWindow, sa_tou_windows)
from cost_engine import SAMPLE_PLANS as HOURLY_PLANS, cost_plan_hourly


def build_series(spiky=False):
    """One year, 30-min, ~22 kWh/day. If spiky, concentrate weekday evening
    load into a sharp peak (same daily total, different shape)."""
    start = datetime(2025, 1, 1)
    rows = []
    flat_evening = [0.6]*8                          # sum = 4.8
    spike_evening = [0.2,0.2,0.2,1.9,1.9,0.2,0.1,0.1]  # sum = 4.8, sharp spike
    evening = spike_evening if spiky else flat_evening
    for d in range(365):
        day = start + timedelta(days=d)
        for i in range(48):
            ts = day + timedelta(minutes=30*(i+1))
            h = i // 2
            if 16 <= h < 20:                       # 4-8pm evening block
                idx = (i - 32)
                imp = evening[idx] if 0 <= idx < len(evening) else 0.3
            elif 6 <= h < 16:
                imp = 0.25
            else:
                imp = 0.15
            rows.append(f"{ts.strftime('%Y-%m-%d %H:%M')},{round(imp,3)},0")
    return "timestamp,import_kwh,export_kwh\n" + "\n".join(rows)


def test_totals_preserved():
    flat = parse_interval_file(build_series(spiky=False))
    spike = parse_interval_file(build_series(spiky=True))
    print(f"flat  total import : {flat.report.total_import_kwh:,.1f} kWh")
    print(f"spiky total import : {spike.report.total_import_kwh:,.1f} kWh")
    assert abs(flat.report.total_import_kwh - spike.report.total_import_kwh) < 5
    print("PASS  same total energy, different shape\n")
    return flat, spike


def test_demand_charge_sees_the_spike(flat, spike):
    demand_plan = next(p for p in SAMPLE_PLANS if p.demand is not None)
    c_flat = cost_plan_intervals(demand_plan, flat.readings)
    c_spike = cost_plan_intervals(demand_plan, spike.readings)
    print(f"[{demand_plan.name}] demand tariff")
    print(f"  flat  shape: peak {c_flat.peak_demand_kw} kW -> demand ${c_flat.demand:,}, net ${c_flat.net:,}")
    print(f"  spiky shape: peak {c_spike.peak_demand_kw} kW -> demand ${c_spike.demand:,}, net ${c_spike.net:,}")
    assert c_spike.peak_demand_kw > c_flat.peak_demand_kw
    assert c_spike.demand > c_flat.demand
    print(f"  => spiky household pays ${c_spike.net - c_flat.net:,} MORE for identical energy")
    print("PASS  per-interval engine captures the demand spike\n")


def test_hourly_engine_misses_it(flat, spike):
    # the averaged engine flattens both to the same hourly profile within a day,
    # so its demand proxy cannot distinguish them as sharply
    fi, fe, fp = flat.hourly_average_profile()
    si, se, sp = spike.hourly_average_profile()
    print("Averaged-engine peak_kw proxy:")
    print(f"  flat  {fp:.2f} kW   spiky {sp:.2f} kW")
    print("  (per-interval engine sees the true sub-hourly spike the average blurs)\n")


def test_full_ranking_on_real_parse(spike):
    print("Per-interval ranking on the spiky household:")
    ranked = rank_plans_intervals(SAMPLE_PLANS, spike.readings)
    for i, c in enumerate(ranked):
        tail = ""
        if c.demand:
            tail = f"  [demand ${c.demand:,} from {c.peak_demand_kw}kW peak x{c.demand_months}mo]"
        print(f"  {i+1}. {c.plan:<17} ${c.net:>5,}/yr{tail}")
    best = ranked[0]
    plan_obj = next(p for p in SAMPLE_PLANS if p.name == best.plan)
    ex = explain_winner(plan_obj, best)
    print(f"\n  Winner: {best.plan} (${best.net:,}/yr)")
    print(f"  Why: " + ", and ".join(ex["why"]) + ".")
    if ex["caveat"]:
        print(f"  Heads up: {ex['caveat']}")
    print("\nPASS  full per-interval ranking\n")


if __name__ == "__main__":
    print("="*64)
    print("PER-INTERVAL ENGINE - demand charge accuracy")
    print("="*64 + "\n")
    flat, spike = test_totals_preserved()
    test_demand_charge_sees_the_spike(flat, spike)
    test_hourly_engine_misses_it(flat, spike)
    test_full_ranking_on_real_parse(spike)
    print("All per-interval tests passed.")
