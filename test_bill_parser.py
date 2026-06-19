"""
Tests the PDF bill parser's extraction and combination logic against synthetic
Origin-style text (so it runs without the real PDFs). The text mirrors the
exact layout pdftotext produces from Origin bills.
"""
import bill_parser as BP


ELEC_TEXT = """
                                                       Electricity
National Metering Identifier (NMI)              20012735449
Plan Summary
Origin Advantage Variable
Ending 12 Apr 2026
 Understand your bill                Billing period: 18 Sep 2025 to 16 Dec 2025 (90 days)
Usage and supply charges             Billing period: 18 Sep 2025 to 16 Dec 2025 (90 days)
Item                Description          Units        Price        Amount
General Usage       First 987            861 kWh      $0.462880    $398.54
General Usage       Remaining            0 kWh        $0.499840    $0.00
Daily Supply                             90 days      $1.089660    $98.07
Total charges                                                      $496.61
Solar feed-in credit
                                         1236 kWh     $-0.020000   -$24.72
(incl GST, if any)
Your total for this bill                                           $396.89
Total kWh                                                          2097.0
"""

GAS_TEXT = """
                                                       Natural gas
Meter Installation Registration
Number (MIRN)                                   55102204212
General Usage       First 4684           4684 MJ      $0.063360    $296.78
Daily Supply                             95 days      $0.958320    $91.04
Total MJ                                                           7169.0
"""


def _parse_text(text):
    """Helper: drive parse_bill's regex logic on a text string directly."""
    # monkeypatch _text to return our fixture
    orig = BP._text
    BP._text = lambda path: text
    try:
        return BP.parse_bill("dummy.pdf")
    finally:
        BP._text = orig


def test_electricity_bill():
    b = _parse_text(ELEC_TEXT)
    assert b.fuel == "electricity", b.fuel
    assert abs(b.usage_rate - 0.46288) < 1e-6, b.usage_rate
    assert abs(b.supply - 1.08966) < 1e-6, b.supply
    assert abs(b.fit - 0.02) < 1e-6, b.fit
    assert b.days == 90, b.days
    assert b.import_kwh == 861.0, b.import_kwh
    assert b.export_kwh == 1236.0, b.export_kwh
    assert b.nmi == "20012735449", b.nmi
    assert b.plan_name == "Origin Advantage Variable", b.plan_name
    assert b.confidence == "high", b.confidence
    print(f"PASS  electricity bill (rate {b.usage_rate}, supply {b.supply}, fit {b.fit}, {b.days}d)")


def test_gas_skipped():
    b = _parse_text(GAS_TEXT)
    assert b.fuel == "gas", b.fuel
    assert b.usage_rate is None
    print("PASS  gas bill detected and skipped")


def test_combine_multiple():
    b1 = _parse_text(ELEC_TEXT)
    gas = _parse_text(GAS_TEXT)
    # second elec bill with different days/usage
    text2 = ELEC_TEXT.replace("(90 days)", "(97 days)").replace("90 days", "97 days")\
                     .replace("861 kWh", "1808 kWh").replace("1236 kWh", "1200 kWh")
    b2 = _parse_text(text2)

    combined = BP.combine_bills([b1, gas, b2])
    assert combined["ok"] is True
    assert combined["bills_used"] == 2, combined["bills_used"]
    assert combined["bills_skipped"] == 1, combined["bills_skipped"]
    assert abs(combined["cur_rate"] - 0.46288) < 1e-6
    assert abs(combined["cur_supply"] - 1.08966) < 1e-6
    assert abs(combined["cur_fit"] - 0.02) < 1e-6
    assert combined["days_covered"] == 187, combined["days_covered"]
    assert combined["annual_import_kwh"] > 0
    assert combined["confidence"] == "high"
    print(f"PASS  combine 2 elec + skip gas (annual import ~{combined['annual_import_kwh']} kWh, "
          f"export ~{combined['annual_export_kwh']} kWh)")


def test_no_electricity():
    gas = _parse_text(GAS_TEXT)
    combined = BP.combine_bills([gas])
    assert combined["ok"] is False
    print("PASS  all-gas upload reports no electricity bill")


def test_bad_rate_flagged():
    bad = ELEC_TEXT.replace("$0.462880", "$46.288000")  # cents mistakenly as dollars
    b = _parse_text(bad)
    assert any("outside expected range" in w for w in b.warnings), b.warnings
    print("PASS  out-of-range rate is flagged")


if __name__ == "__main__":
    print("=" * 60)
    print("PDF BILL PARSER tests (synthetic Origin-style text)")
    print("=" * 60 + "\n")
    test_electricity_bill()
    test_gas_skipped()
    test_combine_multiple()
    test_no_electricity()
    test_bad_rate_flagged()
    print("\nAll bill parser tests passed.")
