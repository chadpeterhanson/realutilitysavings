# Live AER plan data

The "compared plans" can now be real Energy Made Easy offers instead of samples.

## How it works

The AER exposes Energy Made Easy plan data through **unauthenticated** Consumer
Data Right Product Reference Data APIs — no accreditation needed for plan data
(accreditation only covers consumer *usage* data). `eme_fetcher.py` crawls them:

1. discovers retailer base URIs (energymadeeasy.gov.au organisations feed)
2. `Get Generic Plans` per retailer → plan IDs (paged, up to 1000/page)
3. `Get Generic Plan Detail` per plan → full tariff (headers `x-v: 1` list, `x-v: 3` detail)
4. caches results to `plan_cache/plans_SA.json`

`server.py` then prefers the live cache over the bundled sample, and the result
panel labels the source honestly ("live Energy Made Easy offers" vs
"illustrative — not live market offers").

## Refresh the cache (requires network access to the AER hosts)

Add these to your egress allowlist:
- `cdr.energymadeeasy.gov.au`
- `api.energymadeeasy.gov.au`

Then:

```python
from eme_fetcher import refresh_plan_cache
# full SA-relevant crawl (thousands of calls nationally; cap for a first run)
refresh_plan_cache(retailer_limit=10, plans_per_retailer=200)
```

Or run `python3 eme_fetcher.py` for a small smoke crawl.

## Important correctness note

Real CDR unit prices and supply charges are quoted in **cents** (e.g. unitPrice
`"38.72"` = 38.72 c/kWh, dailySupplyCharges `"104.50"` = 104.50 c/day). The
loader converts these to dollars via `_cents()`. Incentive/credit amounts stay
in dollars. This was corrected once the real schema was confirmed.

## Status endpoint

`GET /api/plan-status` → `{cached, count, fetched_at}` so you can see whether
live data is loaded and how fresh it is.

## What's real now vs caveats

Real: the fetcher, the cents-correct mapping, postcode filtering, the live cache
path, and honest source labelling. The comparison becomes genuine switching
guidance when the cache is populated from the live API.

Caveats: the API can't filter by distribution network/customer-type server-side,
so that's done client-side by postcode geography; tiered/block rates currently
use the first block; and this is a guide, not financial advice — confirm with
the retailer before switching.
