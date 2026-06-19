#!/usr/bin/env python3
"""
CLI to populate the live AER / Energy Made Easy plan cache.

Usage:
    python3 refresh_plans.py                 # capped smoke crawl (safe first run)
    python3 refresh_plans.py --full          # larger crawl
    python3 refresh_plans.py --retailers 5 --per 100 --state SA

Requires network access to the AER hosts. Add these to your egress allowlist:
    cdr.energymadeeasy.gov.au
    api.energymadeeasy.gov.au

Plan values are fixed once published, so a nightly cron is plenty in prod.
"""
import argparse
import sys
from eme_fetcher import refresh_plan_cache, cache_status


def main():
    ap = argparse.ArgumentParser(description="Refresh the AER plan cache")
    ap.add_argument("--full", action="store_true",
                    help="larger crawl (10 retailers, 200 plans each)")
    ap.add_argument("--retailers", type=int, default=2,
                    help="cap number of retailers (default 2)")
    ap.add_argument("--per", type=int, default=10,
                    help="cap plans per retailer (default 10)")
    ap.add_argument("--state", default="SA", help="cache label (default SA)")
    args = ap.parse_args()

    rlimit = 10 if args.full else args.retailers
    plimit = 200 if args.full else args.per

    print(f"Refreshing plan cache (retailers<={rlimit}, plans/retailer<={plimit}, "
          f"state={args.state})...")
    try:
        n, errors = refresh_plan_cache(retailer_limit=rlimit,
                                       plans_per_retailer=plimit,
                                       state=args.state)
    except Exception as e:  # noqa: BLE001
        print(f"\nFETCH FAILED: {e}", file=sys.stderr)
        print("If this is a host-not-in-allowlist error, add cdr.energymadeeasy.gov.au "
              "and api.energymadeeasy.gov.au to your network egress settings.",
              file=sys.stderr)
        sys.exit(1)

    st = cache_status(state=args.state)
    print(f"\nCache now holds {st.get('count', 0)} plans "
          f"(fetched {st.get('fetched_at')}).")
    if errors:
        print(f"{len(errors)} errors during crawl; first few:")
        for e in errors[:5]:
            print("  -", e)


if __name__ == "__main__":
    main()
