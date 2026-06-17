#!/usr/bin/env python3
"""Run the full engine test suite. Exit non-zero if any suite fails."""
import subprocess
import sys

SUITES = [
    ("Parser pipeline (NEM12 + flat CSV)", "test_pipeline.py"),
    ("Edge cases (headers, gaps, partial year)", "test_edge_cases.py"),
    ("Per-interval engine (demand charges)", "test_intervals.py"),
    ("CDR plan loader (cents-correct mapping)", "test_eme_loader.py"),
    ("Live AER fetcher (mocked HTTP)", "test_eme_fetcher.py"),
]

def main():
    failed = []
    for label, f in SUITES:
        print(f"\n{'='*60}\n{label}\n{'='*60}")
        r = subprocess.run([sys.executable, f])
        if r.returncode != 0:
            failed.append(label)
    print(f"\n{'='*60}")
    if failed:
        print(f"FAILED: {len(failed)} suite(s):")
        for s in failed:
            print("  -", s)
        sys.exit(1)
    print(f"ALL {len(SUITES)} SUITES PASSED")

if __name__ == "__main__":
    main()
