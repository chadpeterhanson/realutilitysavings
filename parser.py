"""
Interval data parser for Real Utility Savings.

Turns an uploaded electricity interval file into a clean, engine-ready
consumption profile. Handles two formats and auto-detects which one it is:

  1. NEM12  - the AEMO/SAPN standard metering format. A nested CSV with
              record types 100 (header), 200 (NMI data block), 300 (interval
              day), 400/500 (events/reads), 900 (end). Import and export are
              carried on separate channels identified by the register/suffix
              in the 200 row (E = consumption, B = export/generation).

  2. FLAT   - a friendly retailer-style export: one row per interval with a
              timestamp and a kWh value, optionally a direction/channel column.

Output is a normalised IntervalSeries: a list of (timestamp, import_kwh,
export_kwh) at the file's native resolution, plus helpers to roll up into
the 24-hour average profile the cost engine consumes today, and the full
8760-hour series for when the engine moves to per-interval costing.
"""

from __future__ import annotations
import csv
import io
import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional


# ----------------------------------------------------------------------------
# Data structures
# ----------------------------------------------------------------------------

@dataclass
class IntervalReading:
    ts: datetime          # interval ENDING timestamp (AEMO convention)
    import_kwh: float     # grid -> home for this interval
    export_kwh: float     # home -> grid (solar) for this interval


@dataclass
class ParseReport:
    fmt: str = "unknown"              # "nem12" | "flat"
    nmi: Optional[str] = None
    interval_minutes: int = 30
    readings: int = 0
    days_covered: int = 0
    first_ts: Optional[datetime] = None
    last_ts: Optional[datetime] = None
    total_import_kwh: float = 0.0
    total_export_kwh: float = 0.0
    gaps: list = field(default_factory=list)   # list of (start, end) missing spans
    warnings: list = field(default_factory=list)

    def summary(self) -> str:
        lines = [
            f"format            : {self.fmt}",
            f"NMI               : {self.nmi or 'n/a'}",
            f"interval          : {self.interval_minutes} min",
            f"readings          : {self.readings:,}",
            f"days covered      : {self.days_covered}",
            f"date range        : {self.first_ts} -> {self.last_ts}",
            f"total import      : {self.total_import_kwh:,.1f} kWh",
            f"total export      : {self.total_export_kwh:,.1f} kWh",
            f"gaps              : {len(self.gaps)}",
        ]
        if self.warnings:
            lines.append("warnings          : " + "; ".join(self.warnings))
        return "\n".join(lines)


@dataclass
class IntervalSeries:
    readings: list            # list[IntervalReading], time-ordered
    report: ParseReport

    # ---- engine adapters -------------------------------------------------

    def hourly_average_profile(self):
        """Average kWh per clock-hour (0..23) across all days.

        Returns (import[24], export[24], peak_kw). This is the array the
        current cost engine consumes, so the parser is a drop-in replacement
        for the modelled profile.
        """
        imp = [0.0] * 24
        exp = [0.0] * 24
        count = [0] * 24
        peak_kw = 0.0
        ipm = self.report.interval_minutes
        per_hour = max(1, 60 // ipm)
        for r in self.readings:
            h = r.ts.hour
            imp[h] += r.import_kwh
            exp[h] += r.export_kwh
            count[h] += 1
            # convert interval kWh to an instantaneous-ish kW for demand proxy
            kw = r.import_kwh * (60.0 / ipm)
            if kw > peak_kw:
                peak_kw = kw
        # average per hour: sum over days / number of days that hour appears
        # count[h] counts intervals; divide by intervals-per-hour to get day count
        for h in range(24):
            days = count[h] / per_hour if per_hour else 0
            if days > 0:
                imp[h] = imp[h] / days
                exp[h] = exp[h] / days
        return imp, exp, peak_kw

    def annual_totals(self):
        """Scale observed totals to a 365-day year (handles partial uploads)."""
        days = max(1, self.report.days_covered)
        scale = 365.0 / days
        return (self.report.total_import_kwh * scale,
                self.report.total_export_kwh * scale)


# ----------------------------------------------------------------------------
# Format detection
# ----------------------------------------------------------------------------

def detect_format(text: str) -> str:
    head = text.lstrip()
    # NEM12 files begin with a 100 record: "100,NEM12,..."
    first = head.split("\n", 1)[0].strip()
    cells = first.split(",")
    if cells and cells[0] == "100" and len(cells) > 1 and cells[1].upper().startswith("NEM"):
        return "nem12"
    # Some exports drop the 100 header but still use 200/300 blocks
    if any(line.strip().startswith("300,") for line in head.split("\n")[:40]) \
       and any(line.strip().startswith("200,") for line in head.split("\n")[:40]):
        return "nem12"
    return "flat"


# ----------------------------------------------------------------------------
# NEM12 parser
# ----------------------------------------------------------------------------

def _parse_nem12(text: str) -> IntervalSeries:
    rep = ParseReport(fmt="nem12")
    # Channel buffers keyed by date -> list of interval values
    # We collect import (E-suffix) and export (B-suffix) separately.
    imp_by_day: dict = {}
    exp_by_day: dict = {}
    interval_minutes = 30
    current_stream = None   # "import" | "export" | None
    current_ipm = 30

    reader = csv.reader(io.StringIO(text))
    for row in reader:
        if not row:
            continue
        rec = row[0].strip()

        if rec == "200":
            # 200,NMI,configuration,register,NMISuffix,...,interval_length,...
            # Suffix tells us the channel. E* = consumption, B* = export.
            rep.nmi = row[1].strip() if len(row) > 1 else rep.nmi
            suffix = row[4].strip().upper() if len(row) > 4 else ""
            try:
                current_ipm = int(row[8]) if len(row) > 8 and row[8].strip() else 30
            except ValueError:
                current_ipm = 30
            interval_minutes = current_ipm
            if suffix.startswith("B"):
                current_stream = "export"
            elif suffix.startswith("E"):
                current_stream = "import"
            else:
                # fall back: many SAPN files use E1 import, B1 export
                current_stream = "import"

        elif rec == "300":
            # 300,YYYYMMDD,val1,val2,...,valN,quality,...
            if len(row) < 3:
                continue
            day = row[1].strip()
            try:
                date = datetime.strptime(day, "%Y%m%d")
            except ValueError:
                rep.warnings.append(f"bad date in 300 record: {day}")
                continue
            n = 1440 // current_ipm  # intervals per day
            vals = []
            for i in range(2, 2 + n):
                if i < len(row):
                    try:
                        vals.append(float(row[i]))
                    except ValueError:
                        vals.append(0.0)
                else:
                    vals.append(0.0)
            target = imp_by_day if current_stream == "import" else exp_by_day
            # accumulate (a day can appear once per stream)
            existing = target.get(date)
            if existing is None:
                target[date] = vals
            else:
                target[date] = [a + b for a, b in zip(existing, vals)]

        elif rec == "900":
            break

    rep.interval_minutes = interval_minutes
    readings = _merge_day_buffers(imp_by_day, exp_by_day, interval_minutes, rep)
    return IntervalSeries(readings=readings, report=rep)


def _merge_day_buffers(imp_by_day, exp_by_day, ipm, rep) -> list:
    """Combine import/export day buffers into a single ordered reading list."""
    all_days = sorted(set(imp_by_day) | set(exp_by_day))
    n = 1440 // ipm
    readings = []
    for date in all_days:
        imps = imp_by_day.get(date, [0.0] * n)
        exps = exp_by_day.get(date, [0.0] * n)
        for i in range(n):
            # interval-ending timestamp
            ts = date + timedelta(minutes=ipm * (i + 1))
            iv = imps[i] if i < len(imps) else 0.0
            ev = exps[i] if i < len(exps) else 0.0
            readings.append(IntervalReading(ts=ts, import_kwh=iv, export_kwh=ev))
    _finalise(readings, ipm, rep)
    return readings


# ----------------------------------------------------------------------------
# Flat CSV parser
# ----------------------------------------------------------------------------

_TS_FORMATS = [
    "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S",
    "%d/%m/%Y %H:%M", "%d/%m/%Y %H:%M:%S", "%m/%d/%Y %H:%M",
    "%Y/%m/%d %H:%M",
]


def _parse_ts(s: str) -> Optional[datetime]:
    s = s.strip().strip('"')
    for fmt in _TS_FORMATS:
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def _parse_flat(text: str) -> IntervalSeries:
    rep = ParseReport(fmt="flat")
    reader = csv.reader(io.StringIO(text))
    rows = [r for r in reader if r]
    if not rows:
        rep.warnings.append("empty file")
        return IntervalSeries(readings=[], report=rep)

    # Identify header and column roles
    header = [c.strip().lower() for c in rows[0]]
    has_header = any(_parse_ts(c) is None for c in rows[0][:1]) and \
                 any(k in " ".join(header) for k in
                     ["time", "date", "kwh", "usage", "consum", "import", "export", "interval"])

    ts_col = imp_col = exp_col = dir_col = val_col = None
    generic_val_col = None    # a kWh column with no directional meaning
    if has_header:
        for i, h in enumerate(header):
            if ts_col is None and ("time" in h or "date" in h or "interval" in h):
                ts_col = i
            if "export" in h or "generat" in h:
                exp_col = i
            elif "import" in h or "consum" in h or "usage" in h:
                imp_col = i
            elif "kwh" in h or h in ("value", "reading", "kw"):
                generic_val_col = i
            if "direction" in h or h == "type" or "flow" in h or "channel" in h:
                dir_col = i
        data_rows = rows[1:]
        # Only treat the generic column as "import" when an explicit export
        # column also exists (two-column split). A LONE generic kWh column
        # keeps the single-value path so the negative=export convention works.
        if generic_val_col is not None:
            if exp_col is not None and imp_col is None:
                imp_col = generic_val_col
            elif imp_col is None and exp_col is None:
                val_col = generic_val_col
    else:
        # assume: timestamp, value  (optionally timestamp, value, direction)
        ts_col, val_col = 0, 1
        if len(rows[0]) >= 3:
            dir_col = 2
        data_rows = rows

    if ts_col is None:
        ts_col = 0
    if imp_col is None and exp_col is None and val_col is None:
        val_col = 1 if len(rows[0]) > 1 else 0

    readings = []
    seen = set()
    for r in data_rows:
        if ts_col >= len(r):
            continue
        ts = _parse_ts(r[ts_col])
        if ts is None:
            continue
        imp = exp = 0.0
        if imp_col is not None or exp_col is not None:
            if imp_col is not None and imp_col < len(r):
                imp = _num(r[imp_col])
            if exp_col is not None and exp_col < len(r):
                exp = _num(r[exp_col])
        else:
            v = _num(r[val_col]) if val_col is not None and val_col < len(r) else 0.0
            direction = (r[dir_col].strip().lower() if dir_col is not None and dir_col < len(r) else "")
            if direction.startswith("e") or "export" in direction or "gen" in direction:
                exp = abs(v)
            elif v < 0:
                exp = -v          # negative value convention = export
            else:
                imp = v
        # merge duplicate timestamps (e.g. import + export on separate rows)
        if ts in seen:
            for rd in readings:
                if rd.ts == ts:
                    rd.import_kwh += imp
                    rd.export_kwh += exp
                    break
        else:
            readings.append(IntervalReading(ts=ts, import_kwh=imp, export_kwh=exp))
            seen.add(ts)

    readings.sort(key=lambda x: x.ts)
    ipm = _infer_interval(readings)
    rep.interval_minutes = ipm
    _finalise(readings, ipm, rep)
    return IntervalSeries(readings=readings, report=rep)


def _num(s: str) -> float:
    try:
        return float(str(s).strip().strip('"').replace(",", ""))
    except ValueError:
        return 0.0


def _infer_interval(readings: list) -> int:
    if len(readings) < 2:
        return 30
    deltas = {}
    for a, b in zip(readings, readings[1:]):
        d = int((b.ts - a.ts).total_seconds() // 60)
        if d > 0:
            deltas[d] = deltas.get(d, 0) + 1
    if not deltas:
        return 30
    return max(deltas, key=deltas.get)


# ----------------------------------------------------------------------------
# Shared finalisation: totals, coverage, gap detection
# ----------------------------------------------------------------------------

def _finalise(readings: list, ipm: int, rep: ParseReport):
    if not readings:
        rep.warnings.append("no valid readings parsed")
        return
    readings.sort(key=lambda x: x.ts)
    rep.readings = len(readings)
    rep.first_ts = readings[0].ts
    rep.last_ts = readings[-1].ts
    rep.total_import_kwh = sum(r.import_kwh for r in readings)
    rep.total_export_kwh = sum(r.export_kwh for r in readings)
    span_days = (rep.last_ts - rep.first_ts).total_seconds() / 86400.0
    rep.days_covered = max(1, round(span_days))
    # gap detection: expected one reading every ipm minutes
    step = timedelta(minutes=ipm)
    expected = readings[0].ts
    gap_start = None
    for r in readings:
        while expected < r.ts - step / 2:
            if gap_start is None:
                gap_start = expected
            expected += step
        if gap_start is not None:
            rep.gaps.append((gap_start, r.ts))
            gap_start = None
        expected = r.ts + step
    if len(rep.gaps) > 0:
        rep.warnings.append(f"{len(rep.gaps)} gap(s) in interval coverage")


# ----------------------------------------------------------------------------
# Public entry point
# ----------------------------------------------------------------------------

def parse_interval_file(text: str) -> IntervalSeries:
    """Auto-detect format and parse. Returns an IntervalSeries."""
    fmt = detect_format(text)
    if fmt == "nem12":
        return _parse_nem12(text)
    return _parse_flat(text)
