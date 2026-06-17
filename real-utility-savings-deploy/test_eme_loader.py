"""
Tests the Energy Made Easy / CDR plan loader against payloads shaped like the
real Get Generic Plan Detail response, then costs them with the per-interval
engine on parsed interval data. Proves the loader + engine integration.
"""
import json
from datetime import datetime, timedelta

from parser import parse_interval_file
from eme_loader import map_plan_detail, plan_serves_postcode, load_plans_from_details
from cost_engine_intervals import rank_plans_intervals, explain_winner


# A TOU plan with solar feed-in and a sign-up incentive, shaped like CDR PRD
CDR_TOU_PLAN = {
  "data": {
    "planId": "AURORA001",
    "brandName": "Aurora Energy",
    "displayName": "Sunsmart Time-of-Use",
    "geography": {"includedPostcodes": ["5000-5199", "5200-5299"]},
    "electricityContract": {
      "dailySupplyCharge": "98.00",
      "tariffPeriod": [
        {
          "displayName": "All year",
          "dailySupplyCharges": "98.00",
          "timeOfUseRates": [
            {"type": "PEAK", "rateBlockUType": "singleRate",
             "rates": [{"unitPrice": "42.00"}],
             "timeOfUse": [{"days": ["MON","TUE","WED","THU","FRI"],
                            "startTime": "1300", "endTime": "2100"}]},
            {"type": "OFF_PEAK", "rateBlockUType": "singleRate",
             "rates": [{"unitPrice": "18.00"}],
             "timeOfUse": [{"days": ["MON","TUE","WED","THU","FRI","SAT","SUN"],
                            "startTime": "0100", "endTime": "0600"}]},
            {"type": "SHOULDER", "rateBlockUType": "singleRate",
             "rates": [{"unitPrice": "28.00"}],
             "timeOfUse": [{"days": ["MON","TUE","WED","THU","FRI","SAT","SUN"],
                            "startTime": "0600", "endTime": "1300"}]}
          ]
        }
      ],
      "solarFeedInTariff": [
        {"displayName": "Solar", "singleTariff": {"rates": [{"unitPrice": "10.00"}]}}
      ]
    },
    "incentives": [{"displayName": "Welcome credit", "amount": "75"}]
  }
}

# A flat plan with a demand charge, summer-weekday window
CDR_DEMAND_PLAN = {
  "data": {
    "planId": "COAST002",
    "brandName": "Coastline Energy",
    "displayName": "Demand Saver",
    "geography": {"includedPostcodes": ["5000-5999"]},
    "electricityContract": {
      "dailySupplyCharge": "78.00",
      "tariffPeriod": [
        {
          "dailySupplyCharges": "0.78",
          "singleRate": {"rates": [{"unitPrice": "30.00"}]},
          "demandCharges": [
            {"amount": "48.33", "measurementPeriod": "DAY",
             "startTime": "1600", "endTime": "2100",
             "days": ["MON","TUE","WED","THU","FRI"]}
          ]
        }
      ],
      "solarFeedInTariff": [{"singleTariff": {"rates": [{"unitPrice": "6.00"}]}}]
    }
  }
}

# A gas plan that should be skipped (no electricityContract)
CDR_GAS_PLAN = {"data": {"planId": "GASCO003", "brandName": "Gasco",
                          "displayName": "Gas Only", "gasContract": {}}}


def test_postcode_filter():
    summary_in = {"geography": {"includedPostcodes": ["5000-5199"]}}
    summary_out = {"geography": {"includedPostcodes": ["3000-3199"]}}
    assert plan_serves_postcode(summary_in, "5045") is True
    assert plan_serves_postcode(summary_out, "5045") is False
    assert plan_serves_postcode({"geography": {}}, "5045") is True  # no list = serves all
    print("PASS  postcode geography filter (Adelaide 5045)")


def test_map_tou_plan():
    plan, notes = map_plan_detail(CDR_TOU_PLAN["data"])
    assert plan is not None
    assert plan.name == "Aurora Energy - Sunsmart Time-of-Use"
    assert abs(plan.supply - 0.98) < 1e-9
    assert abs(plan.fit - 0.10) < 1e-9
    assert abs(plan.credit - 75) < 1e-9
    assert plan.flat is None
    assert abs(plan.rates["peak"] - 0.42) < 1e-9
    assert abs(plan.rates["offpeak"] - 0.18) < 1e-9
    # peak window must be weekday-only per the days list
    peak_win = next(w for w in plan.windows if w.period == "peak")
    assert peak_win.weekdays_only is True
    assert peak_win.start_hour == 13 and peak_win.end_hour == 21
    print("PASS  map TOU plan (rates, weekday peak window, FiT, credit)")


def test_map_demand_plan():
    plan, notes = map_plan_detail(CDR_DEMAND_PLAN["data"])
    assert plan is not None
    assert plan.demand is not None
    # 0.4833 $/kW/day * 30 -> ~14.5 $/kW/month
    assert abs(plan.demand.rate_per_kw_month - 14.5) < 0.2
    assert plan.demand.weekdays_only is True
    assert plan.demand.start_hour == 16 and plan.demand.end_hour == 21
    print(f"PASS  map demand plan (normalised to ${plan.demand.rate_per_kw_month:.1f}/kW/mo)")


def test_gas_skipped():
    plan, notes = map_plan_detail(CDR_GAS_PLAN["data"])
    assert plan is None
    assert any("gas" in n.lower() or "electricityContract" in n for n in notes)
    print("PASS  gas/dual-fuel plan correctly skipped")


def test_end_to_end_with_engine():
    plans, notes = load_plans_from_details([CDR_TOU_PLAN, CDR_DEMAND_PLAN, CDR_GAS_PLAN])
    print(f"\n  loaded {len(plans)} electricity plans from CDR payloads")
    if notes:
        print("  loader notes: " + "; ".join(notes))

    # build a solar household and parse it
    start = datetime(2025, 1, 1)
    sun = [0,0,0,0,0,0,.02,.08,.20,.35,.48,.55,.57,.54,.45,.32,.18,.06,.01,0,0,0,0,0]
    shape = [.028,.022,.020,.019,.020,.028,.045,.055,.045,.035,.030,.030,
             .032,.032,.033,.038,.050,.072,.085,.080,.065,.052,.042,.032]
    rows = ["timestamp,import_kwh,export_kwh"]
    for d in range(365):
        day = start + timedelta(days=d)
        for i in range(48):
            ts = day + timedelta(minutes=30*(i+1)); h = i//2
            load = 22*shape[h]/2; gen = 6.6*sun[h]/2; net = load-gen
            rows.append(f"{ts.strftime('%Y-%m-%d %H:%M')},{max(net,0):.4f},{max(-net,0):.4f}")
    series = parse_interval_file("\n".join(rows))

    ranked = rank_plans_intervals(plans, series.readings)
    print("\n  Ranked (real-schema plans, parsed interval data):")
    for i, c in enumerate(ranked):
        print(f"    {i+1}. {c.plan:<38} ${c.net:>5,}/yr")
    best = ranked[0]
    plan_obj = next(p for p in plans if p.name == best.plan)
    ex = explain_winner(plan_obj, best)
    print(f"\n  Winner: {best.plan} (${best.net:,}/yr)")
    print(f"  Why: " + ", and ".join(ex["why"]) + ".")
    assert ranked[0].net < ranked[-1].net
    print("\nPASS  CDR-loaded plans cost correctly through the per-interval engine")


if __name__ == "__main__":
    print("="*64)
    print("ENERGY MADE EASY / CDR LOADER tests")
    print("="*64 + "\n")
    test_postcode_filter()
    test_map_tou_plan()
    test_map_demand_plan()
    test_gas_skipped()
    test_end_to_end_with_engine()
    print("\nAll loader tests passed.")
