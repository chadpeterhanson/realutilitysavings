"""
Cost engine for Real Utility Savings.

Replays a household's real interval data against each retail plan and totals
the true annual cost. Logic kept to the current scope:

    net = usage + supply + demand - solar_credit - sign_up_credit

Usage is costed interval-by-interval against time-of-use windows (or a flat
rate), which is the whole point of the product: a household with the same
total kWh can land on a different cheapest plan depending on *when* they use
power and *how much* they export.
"""

from __future__ import annotations
from dataclasses import dataclass
from typing import Optional


# ----------------------------------------------------------------------------
# Time-of-use window definition (SA-style; configurable per plan)
# ----------------------------------------------------------------------------

def period_of(hour: int) -> str:
    """Map a clock hour (0..23) to a TOU period.

    Peak    : 1pm - 9pm (high-demand evening)
    Off-peak: 1am - 6am (overnight)
    Shoulder: everything else
    """
    if 13 <= hour < 21:
        return "peak"
    if 1 <= hour < 6:
        return "offpeak"
    return "shoulder"


# ----------------------------------------------------------------------------
# Plan model
# ----------------------------------------------------------------------------

@dataclass
class Plan:
    name: str
    tag: str
    supply: float                 # daily supply charge $/day
    fit: float                    # feed-in tariff $/kWh exported
    credit: float = 0.0           # one-off sign-up credit $
    demand: float = 0.0           # demand charge $/kW/month (0 = none)
    flat: Optional[float] = None  # flat usage rate $/kWh (if set, ignores TOU)
    peak: float = 0.0             # TOU rates $/kWh
    shoulder: float = 0.0
    offpeak: float = 0.0

    def rate_for(self, period: str) -> float:
        if self.flat is not None:
            return self.flat
        return getattr(self, period)


@dataclass
class CostBreakdown:
    plan: str
    net: float
    usage: float
    supply: float
    demand: float
    solar_credit: float
    sign_up_credit: float
    import_kwh: float
    export_kwh: float
    steady_state: float           # net WITHOUT the one-off credit (year 2+)


# ----------------------------------------------------------------------------
# Costing
# ----------------------------------------------------------------------------

def cost_plan_hourly(plan: Plan, imp_hourly, exp_hourly, peak_kw, days=365) -> CostBreakdown:
    """Cost a plan against a 24-hour average profile scaled to `days`.

    imp_hourly / exp_hourly : list[24] average kWh per clock hour
    peak_kw                 : observed peak demand (kW) for demand charge
    """
    usage = 0.0
    imp_total = 0.0
    exp_total = 0.0
    for h in range(24):
        rate = plan.rate_for(period_of(h))
        usage += imp_hourly[h] * rate
        imp_total += imp_hourly[h]
        exp_total += exp_hourly[h]

    usage *= days
    supply = plan.supply * days
    solar_credit = exp_total * plan.fit * days
    demand = plan.demand * peak_kw * 12 if plan.demand else 0.0

    steady = usage + supply + demand - solar_credit
    net = steady - plan.credit

    return CostBreakdown(
        plan=plan.name,
        net=round(net),
        usage=round(usage),
        supply=round(supply),
        demand=round(demand),
        solar_credit=round(solar_credit),
        sign_up_credit=round(plan.credit),
        import_kwh=round(imp_total * days),
        export_kwh=round(exp_total * days),
        steady_state=round(steady),
    )


def rank_plans(plans, imp_hourly, exp_hourly, peak_kw, days=365):
    """Cost every plan and return breakdowns sorted cheapest-first (by net)."""
    results = [cost_plan_hourly(p, imp_hourly, exp_hourly, peak_kw, days) for p in plans]
    results.sort(key=lambda c: c.net)
    return results


def explain_winner(plan: Plan, best: CostBreakdown) -> dict:
    """Produce the 'why it wins' reasons + caveat shown in the UI."""
    why = []
    if plan.fit >= 0.10 and best.export_kwh > 1500:
        why.append(f"high solar export earns more on its {round(plan.fit*100)}c feed-in tariff")
    if plan.flat is None:
        why.append("overnight and midday usage falls into cheaper time-of-use windows")
    else:
        why.append("a simple flat rate beats time-of-use for this usage shape")
    if plan.credit > 0:
        why.append(f"a ${round(plan.credit)} sign-up credit lowers year one")

    caveat = None
    if plan.credit > 0:
        caveat = (f"the ${round(plan.credit)} credit is one-off \u2014 in year two this "
                  f"plan costs about ${best.steady_state:,.0f}. Compare on "
                  f"steady-state if you set-and-forget.")
    elif plan.demand > 0:
        caveat = ("this plan carries a demand charge \u2014 one high-draw evening can "
                  "spike the bill. Worth it only if your peaks stay modest.")

    return {"why": why, "caveat": caveat}


# Sample SA-style plans (illustrative; production pulls Energy Made Easy data)
SAMPLE_PLANS = [
    Plan("Aurora Energy", "Time-of-use + high feed-in", supply=0.98,
         peak=0.42, shoulder=0.28, offpeak=0.18, fit=0.10, credit=75),
    Plan("Meridian Power", "Flat rate, no surprises", supply=0.95,
         flat=0.32, fit=0.05),
    Plan("Coastline Energy", "Low supply, demand charge", supply=0.78,
         peak=0.36, shoulder=0.26, offpeak=0.16, fit=0.06, demand=14.5),
    Plan("Saltbush Retail", "Best feed-in for big solar", supply=1.05,
         flat=0.30, fit=0.12, credit=50),
    Plan("Greenline Co-op", "Flat + sign-up credit", supply=0.92,
         flat=0.31, fit=0.07, credit=150),
]
