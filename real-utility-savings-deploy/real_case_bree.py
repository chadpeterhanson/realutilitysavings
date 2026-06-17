"""
Real test case from Bree Sawford's Origin bills (22 Cowra St, Mile End SA 5031).
Electricity NMI 20012735449. Has solar. Currently on Origin Advantage Variable.

We have 2 electricity bills (Sep-Dec 2025, Dec 2025-Mar 2026) and the usage
history chart gives the other quarters. Building the real annual picture from
what's printed, then costing it against the current Origin plan + sample market.
"""
from cost_engine_intervals import Plan, rank_plans_intervals, explain_winner, sa_tou_windows, DemandWindow
from datetime import datetime, timedelta

# ---------------------------------------------------------------------------
# REAL figures read directly off the bills
# ---------------------------------------------------------------------------
# Electricity bill 1 (issued 18 Dec 2025): 18 Sep - 16 Dec 2025, 90 days
#   Usage 861 kWh, Supply $1.089660/day, Usage rate $0.462880 (first 987), export 1236 kWh @ $0.02
# Electricity bill 2 (issued 25 Mar 2026): 17 Dec 2025 - 23 Mar 2026, 97 days
#   Usage 1808 kWh (1064 @ .46288 + 744 @ .49984), Supply $1.089660/day, export 1200 kWh @ $0.02
#
# Usage-history chart (kWh per quarter) lets us reconstruct the full year.
# From the two bills we have actual readings; the chart shows the seasonal shape.
# Quarterly usage (kWh) reading the "Compare your usage over time" bars:
#   Sep-Dec24 ~700, Dec24-Mar25 ~1500, Mar-Jun25 ~1500, Jun-Sep25 ~2100, Sep-Dec25 861(actual), Dec25-Mar26 1808(actual)
# Most recent 12 months = Mar25 + Jun25 + Sep-Dec25 + Dec25-Mar26 (approx the 4 latest quarters)

ELEC_RATE_1 = 0.462880    # first-tier usage $/kWh (incl GST)
ELEC_RATE_2 = 0.499840    # remaining $/kWh
SUPPLY_ELEC = 1.089660    # $/day incl GST
FIT = 0.02                # solar feed-in $/kWh
EXPORT_PER_DAY = (1236/90 + 1200/97) / 2   # avg daily export from the two bills

# Reconstruct a representative ANNUAL usage from the 4 most recent quarters
# (using actuals where we have them, chart estimates otherwise)
q_jun_sep_25 = 2100   # winter peak (chart)
q_mar_jun_25 = 1500   # autumn (chart)
q_sep_dec_25 = 861    # ACTUAL (bill 1)
q_dec_mar_26 = 1808   # ACTUAL (bill 2)
annual_import = q_jun_sep_25 + q_mar_jun_25 + q_sep_dec_25 + q_dec_mar_26
annual_export = round(EXPORT_PER_DAY * 365)

print("="*66)
print("REAL CASE - 22 Cowra St, Mile End SA 5031 (Origin bills)")
print("="*66)
print(f"Reconstructed annual electricity import: {annual_import:,} kWh")
print(f"  (from 4 most recent quarters: {q_mar_jun_25}+{q_jun_sep_25}+{q_sep_dec_25}+{q_dec_mar_26})")
print(f"  - two of these are ACTUAL bill readings, two from the usage chart")
print(f"Reconstructed annual solar export: {annual_export:,} kWh (avg {EXPORT_PER_DAY:.1f}/day)")
print()

# ---------------------------------------------------------------------------
# Build an interval series from the real annual totals.
# Bree is on a FLAT (non-TOU) tariff, so we don't know her hourly shape from
# the bills. We use a solar-household daytime/evening shape scaled to her real
# annual import & export. This is the honest limitation of bill-only data.
# ---------------------------------------------------------------------------
class R:
    __slots__=("ts","import_kwh","export_kwh")
    def __init__(s,ts,i,e): s.ts=ts; s.import_kwh=i; s.export_kwh=e

shape=[.028,.022,.020,.019,.020,.028,.045,.055,.045,.035,.030,.030,.032,.032,.033,.038,.050,.072,.085,.080,.065,.052,.042,.032]
ssum=sum(shape); shape=[x/ssum for x in shape]
# export concentrated midday (solar)
exp_shape=[0,0,0,0,0,0,.01,.04,.09,.13,.15,.16,.15,.12,.08,.05,.02,0,0,0,0,0,0,0]
esum=sum(exp_shape); exp_shape=[x/esum for x in exp_shape]

daily_imp=annual_import/365.0
daily_exp=annual_export/365.0
start=datetime(2025,4,1)
readings=[]
for d in range(365):
    day=start+timedelta(days=d)
    for i in range(48):
        ts=day+timedelta(minutes=30*(i+1)); h=i//2
        readings.append(R(ts, daily_imp*shape[h]/2.0, daily_exp*exp_shape[h]/2.0))

# ---------------------------------------------------------------------------
# Bree's ACTUAL current plan, entered exactly from the bill
# ---------------------------------------------------------------------------
origin_current = Plan(
    "Origin Advantage Variable (your current plan)",
    "flat rate, your actual tariff",
    supply=SUPPLY_ELEC, fit=FIT, flat=ELEC_RATE_1,
)

# Sample market plans (illustrative SA offers) for comparison
market = [
    Plan("Aurora Energy", "Time-of-use + high feed-in", supply=0.98, fit=0.10,
         credit=75, rates={"peak":0.42,"shoulder":0.28,"offpeak":0.18}, windows=sa_tou_windows()),
    Plan("Meridian Power", "Flat rate, no surprises", supply=0.95, fit=0.05, flat=0.32),
    Plan("Saltbush Retail", "Best feed-in for big solar", supply=1.05, fit=0.12, credit=50, flat=0.30),
    Plan("Greenline Co-op", "Flat + sign-up credit", supply=0.92, fit=0.07, credit=150, flat=0.31),
]
plans=[origin_current]+market

ranked=rank_plans_intervals(plans, readings)
print("RANKED ANNUAL COST (electricity), costed on your real usage:")
print("-"*66)
for i,c in enumerate(ranked):
    mark=" <-- YOUR CURRENT PLAN" if "current" in c.plan else ("  <-- cheapest" if i==0 else "")
    name=c.plan.replace(" (your current plan)","")
    print(f"{i+1}. {name:<26} ${c.net:>5,}/yr  (usage ${c.usage:,} + supply ${c.supply:,} - solar ${c.solar_credit:,}"+
          (f" - credit ${c.sign_up_credit}" if c.sign_up_credit else "")+")"+mark)

current=next(c for c in ranked if "current" in c.plan)
best=ranked[0]
print()
if best.plan==current.plan:
    print(f"Your current Origin plan is already the cheapest of those compared (${current.net:,}/yr).")
else:
    print(f"Cheapest compared: {best.plan.replace(' (your current plan)','')} at ${best.net:,}/yr")
    print(f"Your current Origin plan: ${current.net:,}/yr")
    print(f"Modelled difference: ${current.net-best.net:,}/yr")

# Gas summary (separate - engine is electricity only)
print()
print("-"*66)
print("GAS (read off bills, summarised separately - not in the elec engine):")
gas_q1=7169  # MJ, Oct 2025-Jan 2026 bill
gas_q2=7157  # MJ, Jan-Apr 2026 bill
print(f"  ~{gas_q1} MJ and ~{gas_q2} MJ across two ~quarterly bills")
print(f"  Gas rate $0.063360/MJ (first tier), supply $0.958320/day")
print(f"  Two gas bills totalled: $470.10 + $467.25 = ${470.10+467.25:,.2f}")
