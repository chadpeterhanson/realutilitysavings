# Real Utility Savings — Testing Runbook (no go-live required)

This is the full system: the marketing site, the step-by-step finder, and the
Python engine (real NEM12/CSV parsing, per-interval costing, live AER plan
data). This guide gets it running and tested **privately**, without exposing
anything to the public.

---

## 0. What's in the box

```
server.py                 Flask app: serves the site + the /api endpoints
run.sh                    one-command launcher (venv + deps + start)
requirements.txt          dependencies (just Flask)
parser.py                 NEM12 + flat-CSV interval parser (auto-detect)
cost_engine_intervals.py  per-interval cost engine (TOU, demand, FiT, credits)
eme_loader.py             maps real AER CDR plan JSON -> engine plans
eme_fetcher.py            crawls the live AER plan APIs, caches to disk
refresh_plans.py          CLI to populate the live plan cache
real-utility-savings-website.html   the marketing site + finder modal
upload.html               minimal standalone upload tool (at /)
plans_sample.json         illustrative SA plans (used if no live cache)
plan_cache/               where live plan data is cached
test_*.py                 the test suites
run_tests.py              runs all suites
test_upload.csv           a year of sample interval data to test uploads
```

---

## 1. Run it locally (5 minutes)

```bash
cd engine
./run.sh
```

(or manually: `python3 -m venv .venv && source .venv/bin/activate &&
pip install -r requirements.txt && python3 server.py`)

Then open **http://127.0.0.1:5001/site**

- `/site` — the full marketing site; every CTA opens the finder
- `/` — a minimal upload-only tool
- `/api/health` — JSON status (shows whether plan data is live or sample)

Nothing here touches the public internet except, optionally, the AER plan
fetch in step 4. No customer data leaves your machine.

---

## 2. Confirm the engine is healthy

```bash
curl http://127.0.0.1:5001/api/health
# {"status":"ok","plan_data":"sample"|"live","plan_count":N,...}
```

Run the test suite (proves parser, engine, loader, fetcher all work):

```bash
python3 run_tests.py        # expect: ALL 5 SUITES PASSED
```

---

## 3. Functional test pass (click-through)

Open `/site` and walk the finder for each scenario. Use `test_upload.csv`
when a file is asked for.

**Test matrix**

| # | Household | Solar | Data step | Current plan entered? | Expect |
|---|-----------|-------|-----------|----------------------|--------|
| 1 | Family | 5kW | skip upload (modelled) | no | ranked plans, "modelled" label |
| 2 | Family | 10kW | upload `test_upload.csv` | no | "uploaded FLAT data, 17,520 readings" |
| 3 | Family | 10kW | upload | yes (46.29 / 1.09 / 2.0) | "you pay now vs could pay" + $ difference |
| 4 | Business | none | modelled | no | higher kWh, weekday-daytime shape |
| 5 | Single | none | upload | yes | small home, current-plan baseline row |

**What to verify each time**
- Step 3 data-method options expand with the CDR / SAPN / bills explainer.
- Uploading a file changes the result label from "modelled" to "uploaded".
- Entering a current plan shows the amber baseline row + the 3-way headline.
- The results footer correctly says "illustrative samples" (sample data) or
  "live Energy Made Easy offers" (live data) — see step 4.
- The demand-charge plan shows a kW peak in its breakdown.

**Real-bill regression:** `python3 real_case_bree.py` reproduces the worked
example from the Origin bills (import ~6,270 kWh, current plan ranks last on a
2c feed-in). Good sanity check after any engine change.

---

## 4. Switch from sample plans to LIVE AER plans (optional, still not "live" to public)

The AER's Energy Made Easy plan APIs are **public and unauthenticated** — you
do NOT need CDR accreditation for plan data (accreditation is only for consumer
*usage* data, which here always comes from the user's own upload/consent).

**a. Allow the AER hosts** in your environment's outbound network settings:
```
cdr.energymadeeasy.gov.au
api.energymadeeasy.gov.au
```

**b. Populate the cache:**
```bash
python3 refresh_plans.py                 # small safe crawl first
python3 refresh_plans.py --full          # wider crawl once happy
```

**c. Confirm it took:**
```bash
curl http://127.0.0.1:5001/api/health    # plan_data should now be "live"
```

Re-run the test matrix — results now say "live Energy Made Easy offers" and the
saving figure reflects real market plans for the postcode. This is real data
but still served only from your own machine.

> Note: a full national crawl is thousands of API calls. Plan values are fixed
> once published, so schedule `refresh_plans.py` nightly in real use rather than
> fetching on demand.

---

## 5. Private staging (let testers reach it without going public)

You want real people clicking it, but not a public launch. Options, easiest first:

1. **Tunnel from your laptop** — run `./run.sh` then expose with a tunnel
   (`cloudflared tunnel --url http://localhost:5001` or `ngrok http 5001`).
   Gives a private HTTPS URL you can share with a handful of testers, password
   it, and kill anytime. No server to manage.

2. **Private VM / staging host** — deploy on a small cloud VM bound to a
   non-public port or behind basic auth / an IP allowlist:
   ```bash
   HOST=0.0.0.0 PORT=8080 ./run.sh
   ```
   Put it behind nginx + HTTP basic auth, or a platform's "private/preview"
   environment (Render, Railway, Fly.io all have non-indexed preview URLs).

3. **Internal only** — run on `0.0.0.0` on your office/VPN network so only
   people on the network can reach it.

For any of these, swap Flask's dev server for a production WSGI server before
real traffic:
```bash
pip install gunicorn
gunicorn -w 2 -b 0.0.0.0:8080 server:app
```

---

## 6. Before you'd ever go truly public (the gaps to close first)

These are deliberately NOT done yet — flagged so testing stays honest:

- **Usage data handling & privacy.** Uploads are parsed in-memory and not
  persisted, but a public service needs a privacy policy, consent wording, and
  a data-retention decision (especially around NMI + interval data).
- **Gas comparison.** Engine is electricity-only; gas is summarised, not ranked.
- **Tariff edge cases.** Tiered/block rates use the first block; controlled load
  and seasonal demand windows are modelled but lightly tested against real plans.
- **Plan freshness & accuracy.** Validate a sample of live plan mappings against
  the retailer's own price fact sheets before trusting the $ figures publicly.
- **"Not financial advice."** The UI says this; keep it, and have the
  comparison methodology reviewed before presenting savings as switching advice.
- **Scale.** Per-interval costing over 17,520 points × many plans is fine for
  one user; load-test before concurrent public traffic.

---

## Quick reference

```bash
./run.sh                          # start (http://127.0.0.1:5001/site)
python3 run_tests.py              # run all test suites
python3 refresh_plans.py          # populate live plan cache (needs AER hosts allowed)
python3 real_case_bree.py         # worked example from real Origin bills
curl localhost:5001/api/health    # status + data source
```
