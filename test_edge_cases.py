"""
Edge-case tests for the parser against the messy realities of real uploads:
  - retailer header row with different column names
  - export carried as negative values in a single column
  - partial-year data (engine must scale to a full year)
  - a gap in coverage (missing intervals)
"""
from datetime import datetime, timedelta
from parser import parse_interval_file, detect_format
from cost_engine import SAMPLE_PLANS, rank_plans


def test_retailer_header_named_columns():
    txt = "Interval Ending,Consumption (kWh),Generation (kWh)\n"
    txt += "01/03/2025 00:30,0.42,0.0\n01/03/2025 01:00,0.31,0.0\n01/03/2025 12:30,0.10,0.95\n"
    s = parse_interval_file(txt)
    assert detect_format(txt) == "flat"
    assert s.report.readings == 3
    assert abs(s.report.total_import_kwh - 0.83) < 1e-6
    assert abs(s.report.total_export_kwh - 0.95) < 1e-6
    print("PASS  retailer header w/ named columns")


def test_negative_value_export_convention():
    # single value column; negative = export to grid
    txt = "timestamp,kwh\n2025-03-01 12:00,-0.80\n2025-03-01 18:00,0.65\n"
    s = parse_interval_file(txt)
    assert abs(s.report.total_export_kwh - 0.80) < 1e-6
    assert abs(s.report.total_import_kwh - 0.65) < 1e-6
    print("PASS  negative-value export convention")


def test_partial_year_scaling():
    # 30 days of data; annual_totals should scale ~12x
    rows = []
    start = datetime(2025, 1, 1)
    for d in range(30):
        for i in range(48):
            ts = start + timedelta(days=d, minutes=30 * (i + 1))
            rows.append(f"{ts.strftime('%Y-%m-%d %H:%M')},0.40,0.10")
    txt = "timestamp,import_kwh,export_kwh\n" + "\n".join(rows)
    s = parse_interval_file(txt)
    ann_imp, ann_exp = s.annual_totals()
    observed = s.report.total_import_kwh
    assert s.report.days_covered in (29, 30, 31)
    assert ann_imp > observed * 11  # scaled up to a year
    print(f"PASS  partial-year scaling (30d obs {observed:.0f} -> annual {ann_imp:.0f} kWh)")


def test_gap_detection():
    # skip a chunk of intervals mid-day
    start = datetime(2025, 6, 1)
    lines = ["timestamp,import_kwh,export_kwh"]
    for i in range(48):
        if 20 <= i <= 28:    # drop ~4.5 hours
            continue
        ts = start + timedelta(minutes=30 * (i + 1))
        lines.append(f"{ts.strftime('%Y-%m-%d %H:%M')},0.5,0.0")
    s = parse_interval_file("\n".join(lines))
    assert len(s.report.gaps) >= 1
    print(f"PASS  gap detection ({len(s.report.gaps)} gap flagged)")


def test_engine_runs_on_parsed_partial():
    txt = "timestamp,import_kwh,export_kwh\n"
    start = datetime(2025, 1, 1)
    rws = []
    for d in range(60):
        for i in range(48):
            ts = start + timedelta(days=d, minutes=30 * (i + 1))
            h = i // 2
            imp = 0.6 if 13 <= h < 21 else 0.25
            exp = 0.5 if 9 <= h < 15 else 0.0
            rws.append(f"{ts.strftime('%Y-%m-%d %H:%M')},{imp},{exp}")
    s = parse_interval_file(txt + "\n".join(rws))
    imp_h, exp_h, peak = s.hourly_average_profile()
    ranked = rank_plans(SAMPLE_PLANS, imp_h, exp_h, peak)
    assert ranked[0].net < ranked[-1].net
    print(f"PASS  engine runs on parsed partial data "
          f"(winner {ranked[0].plan} ${ranked[0].net:,}/yr)")


if __name__ == "__main__":
    print("Edge-case suite")
    print("-" * 40)
    test_retailer_header_named_columns()
    test_negative_value_export_convention()
    test_partial_year_scaling()
    test_gap_detection()
    test_engine_runs_on_parsed_partial()
    print("-" * 40)
    print("All edge cases passed.")
