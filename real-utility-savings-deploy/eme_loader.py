"""
Energy Made Easy / AER CDR plan ingestion for Real Utility Savings.

The AER publishes the same plan data behind Energy Made Easy through the
Consumer Data Right Product Reference Data (PRD) APIs:

    GET {base}/cds-au/v1/energy/plans            -> Get Generic Plans (paged, 25/page)
    GET {base}/cds-au/v1/energy/plans/{planId}   -> Get Generic Plan Detail

This module maps a Get Generic Plan Detail JSON payload into the engine's
Plan model (the per-interval cost_engine_intervals.Plan), so the comparison
runs against real market offers instead of the illustrative samples.

Notes on the real schema (Consumer Data Standards - energy):
  detail.electricityContract
    .tariffPeriod[]                 one or more dated rate periods
        .singleRate.rates[]         flat usage rates ($/kWh) - amount as string
        .timeOfUseRates[]           TOU blocks, each with:
            .rateBlockUType         "singleRate"
            .timeOfUse[]            days[] + startTime/endTime ("HHMM")
            .type                   "PEAK" | "OFF_PEAK" | "SHOULDER"
            .rates[].unitPrice
        .demandCharges[]            $/kW with measure window + days
    .solarFeedInTariff[]            .singleTariff.amount or tiered
    .incentives[]                   sign-up credits etc (often descriptive)
  detail.meteringCharges[]          sometimes carries daily supply
  Supply charge usually appears as a dailySupplyCharges field on the contract
  or as a singleRate with period "DAY".

Real payloads vary by retailer; this loader is defensive and falls back
gracefully when optional blocks are missing, recording what it couldn't map.
"""

from __future__ import annotations
import json
from datetime import datetime
from typing import Optional

from cost_engine_intervals import Plan, TouWindow, DemandWindow


# ----------------------------------------------------------------------------
# small helpers
# ----------------------------------------------------------------------------

def _f(x, default=0.0) -> float:
    """Coerce a CDR amount string to float, safely."""
    if x is None:
        return default
    try:
        return float(str(x).replace("$", "").replace(",", "").strip())
    except (ValueError, TypeError):
        return default


def _cents(x, default=0.0) -> float:
    """CDR unit prices and supply charges are quoted in CENTS (e.g. '33.58'
    c/kWh, '92.00' c/day). The engine works in dollars, so convert."""
    return _f(x, default * 100.0) / 100.0


def _hhmm_to_hour(s: str) -> int:
    """'1600' or '16:00' -> 16 (start hour, floored)."""
    if s is None:
        return 0
    s = str(s).replace(":", "").strip()
    try:
        return int(s[:2]) if len(s) >= 2 else int(s)
    except ValueError:
        return 0


_DAY_MAP = {  # CDR day codes -> python weekday() (Mon=0)
    "MON": 0, "TUE": 1, "WED": 2, "THU": 3, "FRI": 4, "SAT": 5, "SUN": 6,
    "PUBLIC_HOLIDAY": None,
}


def _is_weekdays_only(days) -> bool:
    if not days:
        return False
    codes = {d.upper() for d in days}
    weekend = {"SAT", "SUN"}
    has_weekday = any(d in codes for d in ["MON", "TUE", "WED", "THU", "FRI"])
    has_weekend = bool(codes & weekend)
    return has_weekday and not has_weekend


_PERIOD_MAP = {"PEAK": "peak", "OFF_PEAK": "offpeak", "OFFPEAK": "offpeak",
               "SHOULDER": "shoulder", "CONTROLLED_LOAD": "shoulder"}


# ----------------------------------------------------------------------------
# core mapping
# ----------------------------------------------------------------------------

def map_plan_detail(detail: dict) -> tuple:
    """Map one Get Generic Plan Detail payload to (Plan, notes).

    `detail` is the object under the top-level "data" key of the API response.
    Returns (Plan | None, list_of_notes). Plan is None if it isn't an
    electricity plan we can cost.
    """
    notes = []
    plan_id = detail.get("planId") or detail.get("planID") or "unknown"
    display = detail.get("displayName") or detail.get("brandName") or plan_id
    brand = detail.get("brandName") or ""
    name = f"{brand} - {display}".strip(" -") if brand and brand not in display else display

    ec = detail.get("electricityContract")
    if not ec:
        return None, [f"{plan_id}: no electricityContract (gas/dual-fuel skipped)"]

    # ---- supply charge (daily) ------------------------------------------
    supply = 0.0
    # commonly on the contract directly
    supply = _cents(ec.get("dailySupplyCharge"), 0.0)
    tariff_periods = ec.get("tariffPeriod") or []
    if supply == 0.0 and tariff_periods:
        tp0 = tariff_periods[0]
        supply = _cents(tp0.get("dailySupplyCharges"), 0.0)

    # ---- usage rates: flat or TOU ---------------------------------------
    flat = None
    rates = {}
    windows = []
    demand = None

    if tariff_periods:
        tp = tariff_periods[0]   # use the first/current period
        if len(tariff_periods) > 1:
            notes.append(f"{plan_id}: {len(tariff_periods)} tariff periods, used first")

        # flat single rate
        single = tp.get("singleRate")
        if single and single.get("rates"):
            # may be tiered (block) rates; take first block as headline
            flat = _cents(single["rates"][0].get("unitPrice"))
            if len(single["rates"]) > 1:
                notes.append(f"{plan_id}: block/tiered single rate, used first block")

        # time-of-use rates
        tou = tp.get("timeOfUseRates") or []
        for block in tou:
            period = _PERIOD_MAP.get(str(block.get("type", "")).upper(), "shoulder")
            if block.get("rates"):
                rates[period] = _cents(block["rates"][0].get("unitPrice"))
            for tou_time in (block.get("timeOfUse") or []):
                windows.append(TouWindow(
                    period=period,
                    start_hour=_hhmm_to_hour(tou_time.get("startTime")),
                    end_hour=_hhmm_to_hour(tou_time.get("endTime")) or 24,
                    weekdays_only=_is_weekdays_only(tou_time.get("days")),
                ))
        if tou:
            flat = None  # TOU takes precedence when present

        # demand charges
        dcs = tp.get("demandCharges") or []
        if dcs:
            dc = dcs[0]
            rate = _cents(dc.get("amount") or dc.get("unitPrice"))
            # CDR demand amount is often $/kW/day; normalise to $/kW/month
            measure = str(dc.get("measurementPeriod", "")).upper()
            if "DAY" in measure:
                rate = rate * 30
            demand = DemandWindow(
                rate_per_kw_month=rate,
                start_hour=_hhmm_to_hour(dc.get("startTime")) or 16,
                end_hour=_hhmm_to_hour(dc.get("endTime")) or 21,
                weekdays_only=_is_weekdays_only(dc.get("days")),
            )
            if len(dcs) > 1:
                notes.append(f"{plan_id}: {len(dcs)} demand charges, used first")

    if flat is None and not rates:
        return None, notes + [f"{plan_id}: no usable usage rates found"]

    # ---- solar feed-in tariff -------------------------------------------
    fit = 0.0
    sfit = ec.get("solarFeedInTariff") or []
    if isinstance(sfit, list) and sfit:
        s0 = sfit[0]
        single_t = s0.get("singleTariff") or {}
        if single_t.get("rates"):
            fit = _cents(single_t["rates"][0].get("unitPrice"))
        elif single_t.get("amount"):
            fit = _cents(single_t.get("amount"))
        elif s0.get("amount"):
            fit = _cents(s0.get("amount"))

    # ---- incentives / sign-up credits -----------------------------------
    credit = 0.0
    for inc in (detail.get("incentives") or ec.get("incentives") or []):
        # credits are frequently descriptive text, not a number; pull $ if present
        amt = _f(inc.get("amount") or inc.get("value"), 0.0)
        credit += amt
    # discounts block can carry a guaranteed $ credit
    for dis in (ec.get("discounts") or []):
        if str(dis.get("type", "")).upper() in ("GUARANTEED", "FIXED"):
            credit += _f(dis.get("amount"), 0.0)

    tag_bits = []
    if windows:
        tag_bits.append("time-of-use")
    else:
        tag_bits.append("flat rate")
    if fit >= 0.08:
        tag_bits.append("strong feed-in")
    if demand:
        tag_bits.append("demand charge")
    if credit > 0:
        tag_bits.append("sign-up credit")
    tag = ", ".join(tag_bits)

    plan = Plan(
        name=name,
        tag=tag,
        supply=supply,
        fit=fit,
        credit=credit,
        flat=flat,
        rates=rates,
        windows=windows,
        demand=demand,
    )
    return plan, notes


# ----------------------------------------------------------------------------
# geography filter (which plans apply to this NMI's postcode)
# ----------------------------------------------------------------------------

def plan_serves_postcode(plan_summary: dict, postcode: str) -> bool:
    """Check a Get Generic Plans summary entry against a postcode.

    The geography block lists includedPostcodes (and/or excludedPostcodes),
    sometimes as ranges like '5000-5099'.
    """
    geo = plan_summary.get("geography") or {}
    excluded = geo.get("excludedPostcodes") or []
    included = geo.get("includedPostcodes")
    if _postcode_in(postcode, excluded):
        return False
    if not included:           # no list = available everywhere in jurisdiction
        return True
    return _postcode_in(postcode, included)


def _postcode_in(pc: str, ranges) -> bool:
    if not ranges:
        return False
    try:
        pcn = int(pc)
    except (ValueError, TypeError):
        return False
    for r in ranges:
        r = str(r).strip()
        if "-" in r:
            lo, hi = r.split("-", 1)
            try:
                if int(lo) <= pcn <= int(hi):
                    return True
            except ValueError:
                continue
        else:
            try:
                if int(r) == pcn:
                    return True
            except ValueError:
                continue
    return False


# ----------------------------------------------------------------------------
# batch loader
# ----------------------------------------------------------------------------

def load_plans_from_details(details: list) -> tuple:
    """Map a list of plan-detail payloads to (plans, all_notes)."""
    plans = []
    all_notes = []
    for d in details:
        # accept either {"data": {...}} or the inner object directly
        inner = d.get("data") if isinstance(d, dict) and "data" in d else d
        plan, notes = map_plan_detail(inner)
        all_notes.extend(notes)
        if plan:
            plans.append(plan)
    return plans, all_notes


def load_plans_from_file(path: str) -> tuple:
    with open(path) as f:
        payload = json.load(f)
    if isinstance(payload, dict) and "plans" in payload:
        return load_plans_from_details(payload["plans"])
    if isinstance(payload, list):
        return load_plans_from_details(payload)
    return load_plans_from_details([payload])
