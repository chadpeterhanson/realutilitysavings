"""
Live AER / Energy Made Easy plan fetcher for Real Utility Savings.

The AER exposes Energy Made Easy plan data through unauthenticated Consumer
Data Right Product Reference Data (PRD) APIs. No accreditation is needed for
plan data (accreditation only applies to consumer usage data). Each retailer
has its own base URI under cdr.energymadeeasy.gov.au.

Flow:
  1. discover retailer base URIs        (refdata2 organisations list)
  2. for each retailer: Get Generic Plans  -> list of planIds  (paged, <=1000)
  3. for each planId:    Get Generic Plan Detail -> full tariff
  4. filter to the household's postcode / fuel, map to engine Plans
  5. cache to disk so we don't refetch every request (plans are fixed once set)

Endpoints (per AER docs):
  list   GET {base}/cds-au/v1/energy/plans?type=ALL&fuelType=ELECTRICITY
  detail GET {base}/cds-au/v1/energy/plans/{planId}
  headers: x-v: 1 (list), x-v: 3 (detail), x-min-v: 1
  orgs   GET https://api.energymadeeasy.gov.au/refdata2?keys=organisations

This module is network-guarded: if the AER hosts aren't reachable (e.g. not in
the egress allowlist), it falls back to the cached/bundled data and says so,
so the rest of the system keeps working.
"""

from __future__ import annotations
import json
import os
import time
import urllib.request
import urllib.error

HERE = os.path.dirname(os.path.abspath(__file__))
CACHE_DIR = os.path.join(HERE, "plan_cache")
ORG_URL = "https://api.energymadeeasy.gov.au/refdata2?keys=organisations"
CDR_BASE = "https://cdr.energymadeeasy.gov.au"

LIST_HEADERS = {"x-v": "1", "x-min-v": "1", "Accept": "application/json"}
DETAIL_HEADERS = {"x-v": "3", "x-min-v": "1", "Accept": "application/json"}


# ----------------------------------------------------------------------------
# low-level HTTP with retry + version fallback
# ----------------------------------------------------------------------------

def _get_json(url, headers, timeout=20, retries=2):
    last = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            # 406 = unsupported version: retry one major version lower
            if e.code == 406 and headers.get("x-v", "1") != "1":
                headers = dict(headers, **{"x-v": str(int(headers["x-v"]) - 1)})
                continue
            last = e
        except Exception as e:  # noqa: BLE001 - network guard
            last = e
            time.sleep(0.5 * (attempt + 1))
    raise last if last else RuntimeError("request failed")


# ----------------------------------------------------------------------------
# retailer discovery
# ----------------------------------------------------------------------------

def discover_retailers(timeout=20):
    """Return list of {name, base} for retailers with a PRD base URI.

    Uses the Energy Made Easy organisations reference feed. Falls back to a
    small known set if the feed isn't reachable.
    """
    try:
        data = _get_json(ORG_URL, {"Accept": "application/json"}, timeout=timeout)
        orgs = data.get("organisations") or data.get("data", {}).get("organisations") or []
        out = []
        for o in orgs:
            base = o.get("productReferenceDataBaseUri") or o.get("prdBaseUri")
            slug = o.get("slug") or o.get("id")
            if not base and slug:
                base = f"{CDR_BASE}/{slug}"
            if base:
                out.append({"name": o.get("displayName") or o.get("name") or slug, "base": base})
        if out:
            return out
    except Exception:
        pass
    # fallback: a few major SA retailers by known slug
    return [{"name": n.title(), "base": f"{CDR_BASE}/{n}"} for n in
            ["agl", "originenergy", "energyaustralia", "alinta", "red-energy",
             "simply-energy", "lumo-energy", "amber", "powershop"]]


# ----------------------------------------------------------------------------
# plan listing + detail
# ----------------------------------------------------------------------------

def list_plan_ids(base, fuel="ELECTRICITY", page_size=1000, timeout=20):
    """Return all current generally-available planIds for a retailer."""
    ids = []
    page = 1
    while True:
        url = (f"{base}/cds-au/v1/energy/plans?type=ALL&fuelType={fuel}"
               f"&effective=CURRENT&page={page}&page-size={page_size}")
        data = _get_json(url, dict(LIST_HEADERS), timeout=timeout)
        plans = (data.get("data") or {}).get("plans") or []
        for p in plans:
            pid = p.get("planId")
            if pid:
                ids.append(pid)
        meta = data.get("meta") or {}
        total_pages = meta.get("totalPages") or 1
        if page >= total_pages or not plans:
            break
        page += 1
    return ids


def get_plan_detail(base, plan_id, timeout=20):
    url = f"{base}/cds-au/v1/energy/plans/{plan_id}"
    data = _get_json(url, dict(DETAIL_HEADERS), timeout=timeout)
    return data.get("data") or data


# ----------------------------------------------------------------------------
# orchestration with caching
# ----------------------------------------------------------------------------

def _cache_path(state="SA"):
    return os.path.join(CACHE_DIR, f"plans_{state}.json")


def refresh_plan_cache(retailer_limit=None, plans_per_retailer=None, state="SA",
                       timeout=20, log=print):
    """Fetch live plan details and write them to the cache.

    retailer_limit / plans_per_retailer cap the crawl (useful for a first run;
    a full national crawl is thousands of calls). Returns (count, errors).
    """
    os.makedirs(CACHE_DIR, exist_ok=True)
    retailers = discover_retailers(timeout=timeout)
    if retailer_limit:
        retailers = retailers[:retailer_limit]

    details, errors = [], []
    for r in retailers:
        try:
            ids = list_plan_ids(r["base"], timeout=timeout)
            if plans_per_retailer:
                ids = ids[:plans_per_retailer]
            log(f"  {r['name']}: {len(ids)} plans")
            for pid in ids:
                try:
                    details.append({"data": get_plan_detail(r["base"], pid, timeout=timeout)})
                except Exception as e:  # noqa: BLE001
                    errors.append(f"{r['name']}/{pid}: {e}")
        except Exception as e:  # noqa: BLE001
            errors.append(f"{r['name']} list: {e}")

    payload = {"fetched_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
               "count": len(details), "plans": details}
    with open(_cache_path(state), "w") as f:
        json.dump(payload, f)
    log(f"cached {len(details)} plan details, {len(errors)} errors")
    return len(details), errors


def load_cached_plans(state="SA"):
    """Return cached plan-detail payloads, or None if no cache yet."""
    path = _cache_path(state)
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f).get("plans", [])
    return None


def cache_status(state="SA"):
    path = _cache_path(state)
    if not os.path.exists(path):
        return {"cached": False}
    with open(path) as f:
        d = json.load(f)
    return {"cached": True, "fetched_at": d.get("fetched_at"), "count": d.get("count", 0)}


if __name__ == "__main__":
    # small smoke crawl: 2 retailers, 5 plans each (won't run without network)
    import sys
    try:
        n, errs = refresh_plan_cache(retailer_limit=2, plans_per_retailer=5)
        print(f"done: {n} plans, {len(errs)} errors")
        for e in errs[:5]:
            print("  err:", e)
    except Exception as e:
        print(f"fetch unavailable (expected if AER hosts not in allowlist): {e}",
              file=sys.stderr)
