# Real Utility Savings — Engine

The working backend that turns a household's real interval data into a ranked
list of energy plans, costed by what they'd *actually* pay.

## Pipeline

```
upload file ──> parser.py ──> cost_engine_intervals.py ──> ranked plans
                   │                      ▲
              auto-detect            eme_loader.py
              NEM12 / flat          (real CDR plan data)
```

## Modules

| File | Role |
|------|------|
| `parser.py` | Auto-detects NEM12 (AEMO/SAPN standard) vs flat CSV, separates import/export, infers interval length, scales partial years, flags gaps. Outputs an `IntervalSeries` the engine consumes. |
| `cost_engine_intervals.py` | Costs every interval individually: TOU windows (weekday-aware), flat rates, supply, **per-month demand charges**, solar feed-in, sign-up credits. Ranks plans and explains the winner. |
| `cost_engine.py` | The earlier hourly-average engine (kept; the website finder uses its profile shape). |
| `eme_loader.py` | Maps real Energy Made Easy / AER CDR `Get Generic Plan Detail` payloads into the engine's `Plan` model. Includes postcode geography filtering. |
| `server.py` | Flask API: `POST /api/analyze` (file + postcode + current_bill) → ranked JSON. Serves `upload.html`. |
| `upload.html` | Minimal upload UI that calls the API and renders results. |
| `plans_sample.json` | Five SA plans in real CDR schema (replace with a nightly CDR pull in production). |

## Run it

```bash
pip install flask
python3 server.py            # http://127.0.0.1:5001
```

Open the page, upload an interval CSV (or use `test_upload.csv`), enter a
postcode, and analyze.

## Tests

```bash
python3 test_pipeline.py     # NEM12 + flat parse, both match ground truth
python3 test_edge_cases.py   # messy uploads: headers, negative export, gaps, partial year
python3 test_intervals.py    # demand-charge accuracy (spiky vs flat, same total kWh)
python3 test_eme_loader.py   # CDR schema mapping + engine integration
```

## What's real vs next

Real now: format auto-detection, import/export separation, per-interval
costing with true monthly demand charges, CDR schema mapping, postcode
filtering, live HTTP upload → result.

Next for production:
- Replace `plans_sample.json` with a scheduled pull of `Get Generic Plans` /
  `Get Generic Plan Detail` for the household's distribution zone.
- Add controlled-load channels and seasonal TOU windows (the window model
  already supports `months=`).
- Add gas alongside electricity (gas tariffs are simpler: tiered blocks +
  supply, from bill totals).
- CDR consent flow for automatic data retrieval (vs manual upload).
