"""
PDF bill parser for Real Utility Savings.

Reads electricity bills (Origin layout first) and extracts the figures the
engine needs: usage rate(s) c/kWh, daily supply charge, solar feed-in rate,
kWh used and exported, and the billing-period length. Multiple bills can be
combined to build a fuller annual picture.

Honest scope:
- Built and tested against Origin's bill layout. Other retailers' bills are
  laid out differently; we detect what we can and flag low confidence rather
  than guessing.
- Gas bills (MJ / MIRN) are detected and skipped for the electricity engine.
- This reads the BILL TOTALS, not interval data. It tells us what the customer
  paid and their tariff; it can't see *when* power was used. Good for a real
  current-plan baseline and flat-rate comparison; time-of-use is approximate.
"""

from __future__ import annotations
import re
import subprocess
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class BillData:
    fuel: str = "electricity"            # "electricity" | "gas" | "unknown"
    retailer: Optional[str] = None
    plan_name: Optional[str] = None
    nmi: Optional[str] = None
    days: Optional[int] = None
    usage_rate: Optional[float] = None   # $/kWh, first tier
    usage_rate_2: Optional[float] = None # $/kWh, remaining tier (if present)
    supply: Optional[float] = None       # $/day
    fit: Optional[float] = None          # $/kWh solar feed-in (positive number)
    import_kwh: Optional[float] = None
    export_kwh: Optional[float] = None
    total_cost: Optional[float] = None
    warnings: list = field(default_factory=list)

    @property
    def confidence(self) -> str:
        """How much of the essential set did we get?"""
        essential = [self.usage_rate, self.supply, self.days]
        got = sum(1 for x in essential if x is not None)
        if got == 3:
            return "high"
        if got >= 1:
            return "partial"
        return "low"


def _text(pdf_path: str) -> str:
    """Extract layout-preserving text via pdftotext (most reliable here)."""
    try:
        out = subprocess.run(
            ["pdftotext", "-layout", pdf_path, "-"],
            capture_output=True, timeout=30, check=True,
        )
        return out.stdout.decode("utf-8", errors="replace")
    except Exception:
        # fallback to pdfplumber if the CLI isn't present
        try:
            import pdfplumber
            text = []
            with pdfplumber.open(pdf_path) as pdf:
                for page in pdf.pages:
                    text.append(page.extract_text() or "")
            return "\n".join(text)
        except Exception as e:  # noqa: BLE001
            return ""


def _num(s: str) -> Optional[float]:
    try:
        return float(s.replace("$", "").replace(",", "").strip())
    except (ValueError, TypeError, AttributeError):
        return None


def parse_bill(pdf_path: str) -> BillData:
    """Parse a single bill PDF into BillData."""
    text = _text(pdf_path)
    b = BillData()
    if not text.strip():
        b.fuel = "unknown"
        b.warnings.append("no readable text in PDF (scanned image?)")
        return b

    low = text.lower()

    # ---- fuel type -------------------------------------------------------
    if "natural gas" in low or re.search(r"\bmirn\b", low) or re.search(r"\bMJ\b", text):
        if "kwh" not in low:  # purely gas
            b.fuel = "gas"
            b.warnings.append("gas bill detected; electricity engine skips gas")
            return b
    if "electricity" in low or "kwh" in low:
        b.fuel = "electricity"

    # ---- retailer / plan -------------------------------------------------
    if "origin" in low:
        b.retailer = "Origin Energy"
    m = re.search(r"Plan Summary\s*\n?\s*([A-Za-z0-9 \-]+?)(?:\n|Ending)", text)
    if m:
        b.plan_name = m.group(1).strip()

    # ---- NMI -------------------------------------------------------------
    m = re.search(r"National Metering Identifier.*?(\d{10,11})", text, re.S)
    if m:
        b.nmi = m.group(1)

    # ---- billing period (days) ------------------------------------------
    m = re.search(r"Billing period:.*?\((\d+)\s*days?\)", text, re.S | re.I)
    if m:
        b.days = int(m.group(1))

    # ---- usage rates (General Usage rows) -------------------------------
    # "General Usage  First 987   861 kWh   $0.462880   $398.54"
    rows = re.findall(
        r"General Usage\s+(First[^\n]*?|Remaining[^\n]*?)\s+([\d,]+)\s*kWh\s+\$([\d.]+)\s+\$?(-?[\d.,]+)",
        text)
    usage_total = 0.0
    rate_tiers = []
    for desc, units, price, amount in rows:
        rate = _num(price)
        kwh = _num(units)
        if rate is not None:
            rate_tiers.append(rate)
        if kwh:
            usage_total += kwh
    if rate_tiers:
        b.usage_rate = rate_tiers[0]
        if len(rate_tiers) > 1 and rate_tiers[1] > 0:
            b.usage_rate_2 = rate_tiers[1]
    if usage_total > 0:
        b.import_kwh = usage_total

    # ---- daily supply ----------------------------------------------------
    m = re.search(r"Daily Supply\s+(\d+)\s*days\s+\$([\d.]+)", text)
    if m:
        b.supply = _num(m.group(2))

    # ---- solar feed-in ---------------------------------------------------
    # "Solar feed-in credit ... 1236 kWh  $-0.020000  -$24.72"
    m = re.search(r"Solar feed-in credit.*?([\d,]+)\s*kWh\s+\$(-?[\d.]+)", text, re.S)
    if m:
        b.export_kwh = _num(m.group(1))
        fit = _num(m.group(2))
        if fit is not None:
            b.fit = abs(fit)   # store as positive rate
    else:
        b.fit = 0.0  # no solar line -> assume no feed-in

    # ---- total cost ------------------------------------------------------
    m = re.search(r"Your total for this bill\s+\$([\d.,]+)", text)
    if m:
        b.total_cost = _num(m.group(1))

    # ---- sanity checks ---------------------------------------------------
    if b.usage_rate and not (0.10 <= b.usage_rate <= 1.00):
        b.warnings.append(f"usage rate {b.usage_rate} $/kWh outside expected range")
    if b.supply and not (0.30 <= b.supply <= 3.00):
        b.warnings.append(f"supply {b.supply} $/day outside expected range")
    if b.fuel == "electricity" and b.usage_rate is None:
        b.warnings.append("could not read a usage rate from this bill")

    return b


def combine_bills(bills: list) -> dict:
    """Combine several electricity bills into one annualised picture.

    Rates are averaged (weighted by days); usage/export are summed and scaled
    to 365 days. Returns a dict matching the current-plan API fields plus an
    annualised usage estimate and an overall confidence/warnings summary.
    """
    elec = [b for b in bills if b.fuel == "electricity" and b.usage_rate is not None]
    skipped = [b for b in bills if b not in elec]
    warnings = []
    for b in bills:
        warnings.extend(b.warnings)

    if not elec:
        return {"ok": False, "reason": "no readable electricity bills",
                "warnings": warnings, "skipped": len(skipped)}

    total_days = sum(b.days or 0 for b in elec) or len(elec) * 91

    def wavg(attr):
        num = den = 0.0
        for b in elec:
            v = getattr(b, attr)
            if v is not None:
                w = b.days or 91
                num += v * w
                den += w
        return (num / den) if den else None

    usage_rate = wavg("usage_rate")
    supply = wavg("supply")
    fit = wavg("fit") or 0.0

    sum_import = sum(b.import_kwh or 0 for b in elec)
    sum_export = sum(b.export_kwh or 0 for b in elec)
    annual_import = round(sum_import / total_days * 365) if total_days else None
    annual_export = round(sum_export / total_days * 365) if total_days else None

    confidences = [b.confidence for b in elec]
    overall = "high" if all(c == "high" for c in confidences) else (
              "partial" if any(c in ("high", "partial") for c in confidences) else "low")

    return {
        "ok": True,
        "bills_used": len(elec),
        "bills_skipped": len(skipped),
        "days_covered": total_days,
        "retailer": next((b.retailer for b in elec if b.retailer), None),
        "plan_name": next((b.plan_name for b in elec if b.plan_name), None),
        "nmi": next((b.nmi for b in elec if b.nmi), None),
        "cur_rate": round(usage_rate, 6) if usage_rate else None,
        "cur_supply": round(supply, 6) if supply else None,
        "cur_fit": round(fit, 6),
        "annual_import_kwh": annual_import,
        "annual_export_kwh": annual_export,
        "confidence": overall,
        "warnings": sorted(set(warnings)),
    }


if __name__ == "__main__":
    import sys
    for p in sys.argv[1:]:
        b = parse_bill(p)
        print(f"\n{p}")
        print(f"  fuel={b.fuel} confidence={b.confidence}")
        print(f"  rate={b.usage_rate} supply={b.supply} fit={b.fit} "
              f"days={b.days} import={b.import_kwh} export={b.export_kwh}")
        if b.warnings:
            print("  warnings:", b.warnings)
