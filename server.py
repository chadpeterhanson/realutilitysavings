"""
Real Utility Savings - API server.

Wires the whole engine behind an HTTP upload endpoint so a real uploaded
interval file drives a live result. This is the seam where the website's
"connect your data" step stops being a mockup.

    POST /api/analyze
        multipart form:
            file      : the interval data file (NEM12 or flat CSV)
            postcode  : household postcode (filters eligible plans)
            current_bill : optional, current annual spend for saving estimate
        -> JSON: parse report + ranked plans + winner explanation

    GET  /            : serves the minimal upload UI (upload.html)

Plan data: loads bundled sample CDR payloads from plans_sample.json. In
production this is replaced by a nightly pull of Get Generic Plans /
Get Generic Plan Detail filtered to the household's distribution zone.
"""

from __future__ import annotations
import json
import os
from flask import Flask, request, jsonify, send_from_directory

from parser import parse_interval_file
from eme_loader import load_plans_from_details, plan_serves_postcode
from eme_fetcher import load_cached_plans, cache_status
from bill_parser import parse_bill, combine_bills
from cost_engine_intervals import rank_plans_intervals, explain_winner, SAMPLE_PLANS, Plan

app = Flask(__name__)
HERE = os.path.dirname(os.path.abspath(__file__))

# ----------------------------------------------------------------------------
# plan source
# ----------------------------------------------------------------------------

def _filter_and_map(raw, postcode):
    eligible = []
    for d in raw:
        inner = d.get("data", d)
        summary = {"geography": (inner.get("electricityContract", {}) or {}).get("geography")
                                  or inner.get("geography", {})}
        if plan_serves_postcode(summary, postcode):
            eligible.append(d)
    return load_plans_from_details(eligible or raw)


def load_market_plans(postcode: str):
    """Load eligible plans for the postcode, preferring real data:

      1. live AER cache (plan_cache/) populated by eme_fetcher  -> REAL offers
      2. bundled plans_sample.json                              -> sample CDR shapes
      3. built-in SAMPLE_PLANS                                  -> last-resort

    Returns (plans, notes) where notes flag the data source so the UI can be
    honest about whether these are live market offers or illustrative.
    """
    # 1. live cache from the fetcher
    try:
        cached = load_cached_plans(state="SA")
    except Exception:
        cached = None
    if cached:
        plans, notes = _filter_and_map(cached, postcode)
        if plans:
            st = cache_status(state="SA")
            notes = [f"live AER plan data ({st.get('count', len(plans))} plans, "
                     f"fetched {st.get('fetched_at', 'recently')})"] + notes
            return plans, notes, "live"

    # 2. bundled sample (real schema, illustrative values)
    path = os.path.join(HERE, "plans_sample.json")
    if os.path.exists(path):
        with open(path) as f:
            payload = json.load(f)
        raw = payload.get("plans", payload if isinstance(payload, list) else [payload])
        plans, notes = _filter_and_map(raw, postcode)
        if plans:
            return plans, ["illustrative sample plans (not live market offers)"] + notes, "sample"

    # 3. built-in
    return list(SAMPLE_PLANS), ["using built-in illustrative plans"], "sample"


# ----------------------------------------------------------------------------
# routes
# ----------------------------------------------------------------------------

@app.after_request
def add_cors(resp):
    """Allow the static marketing site (any origin) to call this API."""
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
    resp.headers["Access-Control-Allow-Methods"] = "POST, GET, OPTIONS"
    return resp


@app.route("/")
def index():
    return send_from_directory(HERE, "upload.html")


@app.route("/site")
def site():
    """Serve the full marketing site from the same origin as the API."""
    return send_from_directory(HERE, "real-utility-savings-website.html")


def synth_readings_from_answers(household, solar, heating):
    """Build a year of synthetic interval readings from the site's quiz answers.

    Lets the flow complete (and run through the SAME per-interval engine) when
    the household hasn't uploaded a real file yet. Uploading a file replaces
    this with their actual data.
    """
    from datetime import datetime, timedelta

    class _R:
        __slots__ = ("ts", "import_kwh", "export_kwh")
        def __init__(self, ts, i, e):
            self.ts = ts; self.import_kwh = i; self.export_kwh = e

    base = {"single": 9, "couple": 14, "family": 22, "large": 30,
            "business": 45}.get(household, 22)
    if heating == "electric":
        base *= 1.18
    res_shape = [.028,.022,.020,.019,.020,.028,.045,.055,.045,.035,.030,.030,
                 .032,.032,.033,.038,.050,.072,.085,.080,.065,.052,.042,.032]
    biz_shape = [.012,.010,.010,.010,.012,.018,.030,.050,.075,.085,.090,.092,
                 .085,.088,.085,.075,.060,.040,.025,.018,.012,.010,.008,.010]
    is_business = household == "business"
    shape = biz_shape if is_business else res_shape
    s = sum(shape); shape = [x / s for x in shape]
    sun = [0,0,0,0,0,0,.02,.08,.20,.35,.48,.55,.57,.54,.45,.32,.18,.06,.01,0,0,0,0,0]
    kw = {"none": 0, "small": 5, "large": 10}.get(solar, 5)
    start = datetime(2025, 1, 1)
    out = []
    for d in range(365):
        day = start + timedelta(days=d)
        # businesses run mostly on weekdays; ~35% load on weekends
        weekend_factor = 0.35 if (is_business and day.weekday() >= 5) else 1.0
        for i in range(48):
            ts = day + timedelta(minutes=30 * (i + 1))
            h = i // 2
            load = base * shape[h] / 2.0 * weekend_factor
            gen = kw * sun[h] / 2.0
            net = load - gen
            out.append(_R(ts, net if net > 0 else 0.0, -net if net < 0 else 0.0))
    return out


@app.route("/api/analyze", methods=["OPTIONS"])
def analyze_preflight():
    return ("", 204)


@app.route("/api/analyze", methods=["POST"])
def analyze():
    postcode = (request.form.get("postcode") or "5045").strip()
    current_bill = float(request.form.get("current_bill") or 0)

    # Source the consumption profile: real uploaded file if present, else a
    # modelled profile from the quiz answers so the flow always completes.
    source = "modelled"
    rep = None
    if "file" in request.files and request.files["file"].filename:
        text = request.files["file"].read().decode("utf-8", errors="replace")
        series = parse_interval_file(text)
        if not series.readings:
            return jsonify({"error": "could not parse any interval readings",
                            "warnings": series.report.warnings}), 422
        readings = series.readings
        interval_minutes = series.report.interval_minutes
        rep = series.report
        source = "uploaded"
    else:
        readings = synth_readings_from_answers(
            request.form.get("household", "family"),
            request.form.get("solar", "small"),
            request.form.get("heating", "electric"),
        )
        interval_minutes = 30

    # 2. load eligible market plans
    plans, plan_notes, plan_source = load_market_plans(postcode)

    # 3. rank with the per-interval engine
    ranked = rank_plans_intervals(plans, readings, interval_minutes)

    # 3a. FINAL-COST SANITY GUARD. Regardless of how a plan parsed, every real
    # plan charges a daily supply charge (~$300-450/yr) that solar credits do
    # NOT offset. A plan whose computed supply cost is near-zero has lost its
    # supply charge in parsing (the "$8/yr" bug) and must never be shown.
    days_covered = max(1, (readings[-1].ts - readings[0].ts).days + 1) if readings else 365
    supply_floor = 0.40 * days_covered   # >= ~40c/day of supply over the period
    plausible = [c for c in ranked if c.supply >= supply_floor]
    dropped_unreal = len(ranked) - len(plausible)
    if dropped_unreal:
        plan_notes.append(f"excluded {dropped_unreal} plan(s) with implausibly low supply cost (parsing artefact)")
    if plausible:
        ranked = plausible
    elif ranked:
        plan_notes.append("WARNING: all plans failed the supply sanity check \u2014 plan data may be malformed")

    best = ranked[0]
    best_plan = next((p for p in plans if p.name == best.plan), plans[0])
    ex = explain_winner(best_plan, best)

    # 3b. cost the user's ACTUAL current plan if they entered their tariff.
    # This is the real baseline (off their bill) - kept separate from the
    # illustrative comparison plans so the distinction stays honest.
    current_plan_cost = None
    cur_rate = request.form.get("cur_rate")
    if cur_rate:
        try:
            cur = Plan(
                name=(request.form.get("cur_name") or "Your current plan"),
                tag="your actual tariff",
                supply=float(request.form.get("cur_supply") or 1.0),
                fit=float(request.form.get("cur_fit") or 0.0),
                flat=float(cur_rate),
            )
            cc = rank_plans_intervals([cur], readings, interval_minutes)[0]
            current_plan_cost = {
                "name": cur.name, "net": cc.net, "usage": cc.usage,
                "supply": cc.supply, "solar_credit": cc.solar_credit,
                "import_kwh": cc.import_kwh, "export_kwh": cc.export_kwh,
                "fit": cur.fit, "rate": float(cur_rate),
            }
        except (ValueError, TypeError):
            current_plan_cost = None
    parse_block = {
        "source": source,
        "format": rep.fmt if rep else "modelled",
        "nmi": rep.nmi if rep else None,
        "interval_minutes": interval_minutes,
        "readings": rep.readings if rep else len(readings),
        "days_covered": rep.days_covered if rep else 365,
        "import_kwh": round(rep.total_import_kwh) if rep else best.import_kwh,
        "export_kwh": round(rep.total_export_kwh) if rep else best.export_kwh,
        "gaps": len(rep.gaps) if rep else 0,
        "warnings": rep.warnings if rep else [],
    }
    # Saving is measured against the REAL current plan when we have it,
    # otherwise against the rough current-spend figure the user typed.
    baseline = current_plan_cost["net"] if current_plan_cost else (round(current_bill) if current_bill else None)
    saving_vs_baseline = (baseline - best.net) if baseline is not None else None

    return jsonify({
        "parse": parse_block,
        "current_bill": round(current_bill),
        "current_plan": current_plan_cost,
        "baseline": baseline,
        "winner": best.plan,
        "saving": round(saving_vs_baseline) if saving_vs_baseline is not None else None,
        "saving_basis": "current_plan" if current_plan_cost else ("current_bill" if current_bill else None),
        "explanation": ex,
        "plans": [
            {
                "name": c.plan, "net": c.net, "usage": c.usage, "supply": c.supply,
                "demand": c.demand, "solar_credit": c.solar_credit,
                "sign_up_credit": c.sign_up_credit, "steady_state": c.steady_state,
                "import_kwh": c.import_kwh, "export_kwh": c.export_kwh,
                "peak_demand_kw": c.peak_demand_kw,
            } for c in ranked
        ],
        "plan_notes": plan_notes,
        "plan_source": plan_source,
    })


@app.route("/api/parse-bills", methods=["POST", "OPTIONS"])
def parse_bills():
    if request.method == "OPTIONS":
        return ("", 204)
    files = request.files.getlist("bills")
    if not files:
        return jsonify({"error": "no bill files uploaded"}), 400

    import tempfile, os as _os
    parsed = []
    for f in files:
        if not f.filename:
            continue
        # save to a temp file (pdftotext needs a path), parse, delete
        suffix = _os.path.splitext(f.filename)[1] or ".pdf"
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
        try:
            f.save(tmp.name)
            tmp.close()
            parsed.append(parse_bill(tmp.name))
        finally:
            try:
                _os.unlink(tmp.name)
            except OSError:
                pass

    combined = combine_bills(parsed)
    combined["per_bill"] = [
        {"fuel": b.fuel, "confidence": b.confidence, "days": b.days,
         "rate": b.usage_rate, "supply": b.supply, "fit": b.fit,
         "import_kwh": b.import_kwh, "export_kwh": b.export_kwh}
        for b in parsed
    ]
    return jsonify(combined)


@app.route("/api/plan-status")
def plan_status():
    return jsonify(cache_status(state="SA"))


@app.route("/api/health")
def health():
    st = cache_status(state="SA")
    return jsonify({
        "status": "ok",
        "plan_data": "live" if st.get("cached") else "sample",
        "plan_count": st.get("count", 0),
        "plans_fetched_at": st.get("fetched_at"),
    })


if __name__ == "__main__":
    import os
    port = int(os.environ.get("PORT", "5001"))
    host = os.environ.get("HOST", "127.0.0.1")
    print(f"Real Utility Savings engine -> http://{host}:{port}/site")
    app.run(host=host, port=port, debug=False)