"""
Per-interval cost engine for Real Utility Savings.

This is the accurate upgrade over the hourly-average engine. It costs every
interval in the household's real series individually, which matters for three
things the averaged engine can only approximate:

  1. DEMAND CHARGES. Real NEM demand tariffs bill the single highest-demand
     interval inside a defined demand window, reset each month. An averaged
     profile flattens exactly the spike that drives this charge. Here we find
     the true monthly peak per the plan's own demand window.

  2. WEEKDAY / WEEKEND TOU. Many plans only apply peak rates on business days.
     Per-interval costing reads the actual calendar date of each interval.

  3. SEASONAL WINDOWS. Summer vs winter peak windows differ on some plans;
     per-interval costing can switch windows by month.

Net cost = usage + supply + demand - solar_credit - sign_up_credit
"""

from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


# ----------------------------------------------------------------------------
# Tariff window model (per-plan, calendar-aware)
# ----------------------------------------------------------------------------

@dataclass
class TouWindow:
    """A time-of-use window rule.

    period       : "peak" | "shoulder" | "offpeak"
    start_hour   : inclusive hour 0-23 (interval-ending hour is mapped to its
                   starting hour for window tests)
    end_hour     : exclusive hour 0-24
    weekdays_only: if True, only applies Mon-Fri; weekends fall to `weekend_period`
    months       : optional set of months (1-12) this window applies to
    """
    period: str
    start_hour: int
    end_hour: int
    weekdays_only: bool = False
    months: Optional[frozenset] = None


@dataclass
class DemandWindow:
    """Defines when demand is measured for a demand tariff.

    rate_per_kw_month : $/kW applied to the peak demand each month
    start_hour/end_hour: the window in which demand is measured
    weekdays_only     : restrict to business days
    months            : optional months the demand charge applies (e.g. summer)
    """
    rate_per_kw_month: float
    start_hour: int = 16
    end_hour: int = 21
    weekdays_only: bool = True
    months: Optional[frozenset] = None


# ----------------------------------------------------------------------------
# Plan model (per-interval aware)
# ----------------------------------------------------------------------------

@dataclass
class Plan:
    name: str
    tag: str
    supply: float                       # $/day
    fit: float                          # $/kWh exported
    credit: float = 0.0                 # one-off sign-up credit $
    flat: Optional[float] = None        # flat $/kWh; if set, TOU windows ignored
    rates: dict = field(default_factory=dict)   # {"peak":.42,"shoulder":.28,"offpeak":.18}
    windows: list = field(default_factory=list) # list[TouWindow]
    demand: Optional[DemandWindow] = None

    def period_for(self, ts: datetime) -> str:
        """Resolve the TOU period for an interval-ending timestamp."""
        if self.flat is not None:
            return "flat"
        # map interval-ending ts to its starting hour
        hour = (ts.hour - 1) % 24 if ts.minute == 0 else ts.hour
        is_weekday = ts.weekday() < 5
        month = ts.month
        for w in self.windows:
            if w.months and month not in w.months:
                continue
            if w.weekdays_only and not is_weekday:
                continue
            if w.start_hour <= hour < w.end_hour:
                return w.period
        return "shoulder"   # default fall-through

    def rate_for(self, ts: datetime) -> float:
        if self.flat is not None:
            return self.flat
        return self.rates.get(self.period_for(ts), self.rates.get("shoulder", 0.0))


# ----------------------------------------------------------------------------
# Result
# ----------------------------------------------------------------------------

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
    steady_state: float
    peak_demand_kw: float = 0.0       # max measured monthly demand (for transparency)
    demand_months: int = 0


# ----------------------------------------------------------------------------
# Per-interval costing
# ----------------------------------------------------------------------------

def cost_plan_intervals(plan: Plan, readings, interval_minutes=30) -> CostBreakdown:
    """Cost a plan against the full interval series.

    readings : list of objects with .ts (datetime), .import_kwh, .export_kwh
    """
    usage = 0.0
    imp_total = 0.0
    exp_total = 0.0
    days = set()
    intervals_per_hour = 60.0 / interval_minutes

    # monthly peak demand tracking: {(year,month): max_kw_in_window}
    monthly_peak = {}

    for r in readings:
        ts = r.ts
        usage += r.import_kwh * plan.rate_for(ts)
        imp_total += r.import_kwh
        exp_total += r.export_kwh
        days.add(ts.date())

        if plan.demand is not None:
            dw = plan.demand
            hour = (ts.hour - 1) % 24 if ts.minute == 0 else ts.hour
            in_months = (dw.months is None) or (ts.month in dw.months)
            in_days = (not dw.weekdays_only) or (ts.weekday() < 5)
            if in_months and in_days and dw.start_hour <= hour < dw.end_hour:
                # convert interval energy (kWh) to average power (kW) over the interval
                kw = r.import_kwh * intervals_per_hour
                key = (ts.year, ts.month)
                if kw > monthly_peak.get(key, 0.0):
                    monthly_peak[key] = kw

    n_days = max(1, len(days))
    supply = plan.supply * n_days

    demand_cost = 0.0
    peak_kw = 0.0
    if plan.demand is not None and monthly_peak:
        peak_kw = max(monthly_peak.values())
        demand_cost = sum(kw * plan.demand.rate_per_kw_month
                          for kw in monthly_peak.values())

    solar_credit = exp_total * plan.fit

    steady = usage + supply + demand_cost - solar_credit
    net = steady - plan.credit

    # Scale to a full year if the upload is partial
    scale = 365.0 / n_days if n_days < 360 else 1.0
    if scale != 1.0:
        usage *= scale
        supply = plan.supply * 365
        solar_credit *= scale
        demand_cost *= scale
        imp_total *= scale
        exp_total *= scale
        steady = usage + supply + demand_cost - solar_credit
        net = steady - plan.credit

    return CostBreakdown(
        plan=plan.name,
        net=round(net),
        usage=round(usage),
        supply=round(supply),
        demand=round(demand_cost),
        solar_credit=round(solar_credit),
        sign_up_credit=round(plan.credit),
        import_kwh=round(imp_total),
        export_kwh=round(exp_total),
        steady_state=round(steady),
        peak_demand_kw=round(peak_kw, 2),
        demand_months=len(monthly_peak),
    )


def rank_plans_intervals(plans, readings, interval_minutes=30):
    results = [cost_plan_intervals(p, readings, interval_minutes) for p in plans]
    results.sort(key=lambda c: c.net)
    return results


def explain_winner(plan: Plan, best: CostBreakdown) -> dict:
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
    elif plan.demand > 0 if isinstance(plan.demand, (int, float)) else plan.demand is not None:
        caveat = (f"this plan carries a demand charge \u2014 your peak hit "
                  f"{best.peak_demand_kw} kW. One high-draw evening can spike the "
                  f"bill, so it pays off only if your peaks stay modest.")
    return {"why": why, "caveat": caveat}


# ----------------------------------------------------------------------------
# Helper: build standard SA TOU windows
# ----------------------------------------------------------------------------

def sa_tou_windows():
    """Standard SA-style windows: weekday evening peak, overnight off-peak."""
    return [
        TouWindow("peak", 13, 21, weekdays_only=True),
        TouWindow("offpeak", 1, 6),
    ]


# Sample plans rebuilt in the per-interval model (illustrative)
SAMPLE_PLANS = [
    Plan("Aurora Energy", "Time-of-use + high feed-in", supply=0.98, fit=0.10,
         credit=75, rates={"peak":0.42,"shoulder":0.28,"offpeak":0.18},
         windows=sa_tou_windows()),
    Plan("Meridian Power", "Flat rate, no surprises", supply=0.95, fit=0.05,
         flat=0.32),
    Plan("Coastline Energy", "Low supply, demand charge", supply=0.78, fit=0.06,
         rates={"peak":0.36,"shoulder":0.26,"offpeak":0.16}, windows=sa_tou_windows(),
         demand=DemandWindow(rate_per_kw_month=14.5, start_hour=16, end_hour=21,
                             weekdays_only=True)),
    Plan("Saltbush Retail", "Best feed-in for big solar", supply=1.05, fit=0.12,
         credit=50, flat=0.30),
    Plan("Greenline Co-op", "Flat + sign-up credit", supply=0.92, fit=0.07,
         credit=150, flat=0.31),
]
