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

    # ---- choose the MAIN tariff period --------------------------------
    # Controlled-load (CL) / off-peak-dedicated periods often appear as extra
    # tariffPeriods with $0 supply and a low rate. Blindly taking the first
    # period can grab the CL block, producing impossible $0-supply plans.
    # Pick the period that looks like the primary residential tariff: prefer
    # one whose name isn't controlled-load and that carries a real supply
    # charge; fall back sensibly.
    tariff_periods = ec.get("tariffPeriod") or []

    def _is_controlled_load(tp):
        name = (str(tp.get("displayName", "")) + " " +
                str(tp.get("type", ""))).lower()
        return ("controlled" in name or "control load" in name or
                name.strip() in ("cl", "controlled load") or "off peak dedicated" in name)

    def _read_supply(obj):
        """Read a daily supply charge from an object, tolerating the several
        field names real AER plans use (singular/plural) and nested rate
        blocks. Returns $/day or 0.0."""
        if not isinstance(obj, dict):
            return 0.0
        for key in ("dailySupplyCharges", "dailySupplyCharge",
                    "dailySupplyChargeAmount", "supplyCharge"):
            v = obj.get(key)
            if v not in (None, "", []):
                c = _cents(v, 0.0)
                if c > 0:
                    return c
        # sometimes nested inside a singleRate / rate block
        for blk_key in ("singleRate", "supplyCharges"):
            blk = obj.get(blk_key)
            if isinstance(blk, dict):
                got = _read_supply(blk)
                if got > 0:
                    return got
            if isinstance(blk, list):
                for item in blk:
                    got = _read_supply(item)
                    if got > 0:
                        return got
        return 0.0

    main_tp = None
    if tariff_periods:
        # 1) a non-CL period that has a real daily supply charge (any variant)
        for tp in tariff_periods:
            if not _is_controlled_load(tp) and _read_supply(tp) > 0:
                main_tp = tp
                break
        # 2) else any non-CL period
        if main_tp is None:
            for tp in tariff_periods:
                if not _is_controlled_load(tp):
                    main_tp = tp
                    break
        # 3) else fall back to the first
        if main_tp is None:
            main_tp = tariff_periods[0]
        if len(tariff_periods) > 1:
            notes.append(f"{plan_id}: {len(tariff_periods)} tariff periods, "
                         f"used '{main_tp.get('displayName','main')}'")

    # ---- supply charge (daily) ------------------------------------------
    # check contract level (all variants), then the chosen period
    supply = _read_supply(ec)
    if supply == 0.0 and main_tp is not None:
        supply = _read_supply(main_tp)

    # ---- usage rates: flat or TOU ---------------------------------------
    flat = None
    rates = {}
    windows = []
    demand = None

    if main_tp is not None:
        tp = main_tp

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
    sometimes as ranges like '5000-5099'. Some plans use a wildcard marker
    such as 'ALL' to mean 'available everywhere in the jurisdiction'.
    """
    geo = plan_summary.get("geography") or {}
    excluded = geo.get("excludedPostcodes") or []
    included = geo.get("includedPostcodes")
    if _postcode_in(postcode, excluded):
        return False
    if not included:           # no list = available everywhere in jurisdiction
        return True
    # wildcard markers meaning "everywhere"
    if any(str(x).strip().upper() in ("ALL", "*", "ANY") for x in included):
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

def is_plausible_plan(plan) -> tuple:
    """Guard against impossible plans reaching the user (e.g. the $8/yr bug).

    Returns (ok, reason). A residential electricity plan must have a real
    daily supply charge and a sane usage rate; anything outside these bounds
    is almost certainly a parsing artefact (controlled-load block read as the
    main tariff, missing field, unit error) and is excluded.
    """
    if plan is None:
        return (False, "no plan")
    # supply: real plans are ~$0.70-$2.00/day; below $0.30 is implausible
    if plan.supply is None or plan.supply < 0.30:
        return (False, f"supply ${plan.supply}/day too low")
    if plan.supply > 4.0:
        return (False, f"supply ${plan.supply}/day too high")
    # headline usage rate: flat or the cheapest TOU rate must be sane
    rate = plan.flat
    if rate is None and getattr(plan, "rates", None):
        vals = [v for v in plan.rates.values() if v]
        rate = min(vals) if vals else None
    if rate is None or rate < 0.10:
        return (False, f"usage rate ${rate}/kWh too low")
    if rate > 1.20:
        return (False, f"usage rate ${rate}/kWh too high")
    # feed-in shouldn't exceed usage rate by a wild margin
    if plan.fit is not None and plan.fit > 0.60:
        return (False, f"feed-in ${plan.fit}/kWh implausible")
    return (True, "")


def load_plans_from_details(details: list) -> tuple:
    """Map a list of plan-detail payloads to (plans, all_notes).

    Plans that fail the plausibility check are excluded so an impossible
    result can never be shown to a user.
    """
    plans = []
    all_notes = []
    dropped = 0
    for d in details:
        # accept either {"data": {...}} or the inner object directly
        inner = d.get("data") if isinstance(d, dict) and "data" in d else d
        plan, notes = map_plan_detail(inner)
        all_notes.extend(notes)
        if plan:
            ok, reason = is_plausible_plan(plan)
            if ok:
                plans.append(plan)
            else:
                dropped += 1
    if dropped:
        all_notes.append(f"excluded {dropped} implausible plan(s) (failed validation)")
    return plans, all_notes


def load_plans_from_file(path: str) -> tuple:
    with open(path) as f:
        payload = json.load(f)
    if isinstance(payload, dict) and "plans" in payload:
        return load_plans_from_details(payload["plans"])
    if isinstance(payload, list):
        return load_plans_from_details(payload)
    return load_plans_from_details([payload])
