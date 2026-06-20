"""
Tests the live AER fetcher's logic by mocking the HTTP layer with responses
shaped like the real Energy Made Easy CDR API (cents-based prices, paged list,
detail schema from AER docs). Confirms the discover -> list -> detail -> map ->
cost chain works, so when deployed against the live endpoint it behaves.
"""
import json
import eme_fetcher as F
from eme_loader import load_plans_from_details
from cost_engine_intervals import rank_plans_intervals
from datetime import datetime, timedelta

# ---- realistic fixtures (real CDR shapes) ---------------------------------

ORGS = {"organisations": [
    {"displayName": "AGL", "slug": "agl", "productReferenceDataBaseUri": "https://cdr.energymadeeasy.gov.au/agl"},
    {"displayName": "Origin Energy", "slug": "originenergy", "productReferenceDataBaseUri": "https://cdr.energymadeeasy.gov.au/originenergy"},
]}

PLANS_LIST_PAGE1 = {"data": {"plans": [
    {"planId": "AGL_VIC_RES_E1"}, {"planId": "AGL_SA_RES_E2"}]},
    "meta": {"totalPages": 1}}

# detail uses CENTS for unitPrice & dailySupplyCharges, per real API
PLAN_DETAIL = {"data": {
    "planId": "AGL_SA_RES_E2",
    "brandName": "AGL",
    "displayName": "Value Saver",
    "geography": {"includedPostcodes": ["5000-5999"]},
    "electricityContract": {
        "pricingModel": "SINGLE_RATE",
        "tariffPeriod": [{
            "displayName": "All year",
            "rateBlockUType": "singleRate",
            "dailySupplyCharges": "104.50",            # cents/day
            "singleRate": {"rates": [{"unitPrice": "38.72"}]},  # cents/kWh
        }],
        "solarFeedInTariff": [{"singleTariff": {"rates": [{"unitPrice": "5.00"}]}}],
    },
    "incentives": [{"displayName": "Welcome credit", "amount": "50"}],
}}


def make_mock():
    """Return a _get_json replacement that serves the fixtures by URL."""
    def mock(url, headers, timeout=20, retries=2):
        if "refdata2" in url:
            return ORGS
        if url.rstrip("/").endswith("/energy/plans") or "/energy/plans?" in url:
            return PLANS_LIST_PAGE1
        if "/energy/plans/" in url:
            return PLAN_DETAIL
        raise RuntimeError("unexpected url " + url)
    return mock


def test_discover_retailers():
    F._get_json = make_mock()
    rs = F.discover_retailers()
    names = [r["name"] for r in rs]
    assert "AGL" in names and "Origin Energy" in names
    assert rs[0]["base"].startswith("https://cdr.energymadeeasy.gov.au/")
    print(f"PASS  discover retailers ({len(rs)} found: {', '.join(names)})")


def test_list_and_detail():
    F._get_json = make_mock()
    ids = F.list_plan_ids("https://cdr.energymadeeasy.gov.au/agl")
    assert "AGL_SA_RES_E2" in ids
    detail = F.get_plan_detail("https://cdr.energymadeeasy.gov.au/agl", "AGL_SA_RES_E2")
    assert detail["planId"] == "AGL_SA_RES_E2"
    print(f"PASS  list + detail ({len(ids)} plan ids, detail fetched)")


def test_cents_conversion_through_loader():
    plans, notes = load_plans_from_details([PLAN_DETAIL])
    assert len(plans) == 1
    p = plans[0]
    # 104.50 c/day -> $1.045 ; 38.72 c/kWh -> $0.3872 ; 5.00 c -> $0.05
    assert abs(p.supply - 1.045) < 1e-6, p.supply
    assert abs(p.flat - 0.3872) < 1e-6, p.flat
    assert abs(p.fit - 0.05) < 1e-6, p.fit
    assert abs(p.credit - 50) < 1e-6      # credit stays dollars
    print(f"PASS  cents->dollars conversion (supply ${p.supply}/day, rate ${p.flat}/kWh, fit ${p.fit})")


def test_full_refresh_and_cost(tmp_state="TEST"):
    F._get_json = make_mock()
    # refresh writes to cache; small crawl
    n, errs = F.refresh_plan_cache(retailer_limit=2, plans_per_retailer=2,
                                   state=tmp_state, log=lambda *_: None)
    assert n >= 1, (n, errs)
    cached = F.load_cached_plans(state=tmp_state)
    assert cached and len(cached) >= 1
    plans, _ = load_plans_from_details(cached)
    assert plans, "no plans mapped from cache"

    # cost on a small solar profile
    class R:
        __slots__=("ts","import_kwh","export_kwh")
        def __init__(s,ts,i,e): s.ts=ts;s.import_kwh=i;s.export_kwh=e
    start=datetime(2025,1,1); rd=[]
    for d in range(30):
        for i in range(48):
            ts=start+timedelta(days=d,minutes=30*(i+1)); h=i//2
            rd.append(R(ts, 0.4 if 17<=h<21 else 0.2, 0.5 if 9<=h<15 else 0))
    ranked = rank_plans_intervals(plans, rd)
    assert ranked[0].net > 0
    print(f"PASS  full refresh+cost ({n} cached, cheapest ${ranked[0].net}/yr from live-shaped data)")

    st = F.cache_status(state=tmp_state)
    assert st["cached"] and st["count"] >= 1
    print(f"PASS  cache status reports {st['count']} plans @ {st['fetched_at']}")


if __name__ == "__main__":
    print("="*64)
    print("LIVE AER FETCHER tests (HTTP mocked with real CDR shapes)")
    print("="*64 + "\n")
    test_discover_retailers()
    test_list_and_detail()
    test_cents_conversion_through_loader()
    test_full_refresh_and_cost()
    # cleanup test cache
    import os
    p = F._cache_path("TEST")
    if os.path.exists(p): os.remove(p)
    print("\nAll fetcher tests passed.")
