#!/usr/bin/env python3
"""
Diagnostic: find exactly which Test Cases are causing a count mismatch
between the dashboard's cache and a reference Excel export.

Usage:
    python scripts/diagnose_mismatch.py <path_to_excel> \
        --projects "CC Core Blue" "AutoPilot" "CC Core White" "CC Core Red" \
        --start 2026-07-01 --end 2026-07-28

Prints, per owner:
  - count in the Excel
  - count in the cache under the same filters
  - the exact FormattedIDs that are in the cache but NOT in the Excel (surplus)
  - the exact FormattedIDs that are in the Excel but NOT in the cache (missing)

This tells you definitively whether a mismatch is duplicate cache rows,
project-scope drift, or the Excel export simply being narrower/stale —
instead of guessing from aggregate counts.
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd # pyright: ignore[reportMissingModuleSource]

from src import metrics
from src.db import Database
from src.settings import settings


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("excel_path")
    ap.add_argument("--projects", nargs="*", default=None)
    ap.add_argument("--start", default=None)
    ap.add_argument("--end", default=None)
    ap.add_argument("--category", default="All")
    args = ap.parse_args()

    # --- reference data (Excel) ---
    # --- reference data (Excel) ---
    ref = pd.read_excel(args.excel_path, sheet_name="Sheet1")
    ref["FormattedID"] = ref["FormattedID"].astype(str)
    ref["CreationDate"] = pd.to_datetime(ref["CreationDate"], utc=True)

    if args.start:
        ref = ref[ref["CreationDate"] >= pd.Timestamp(args.start, tz="UTC")]
    if args.end:
        end_of_day = pd.Timestamp(args.end, tz="UTC") + pd.Timedelta(days=1) - pd.Timedelta(microseconds=1)
        ref = ref[ref["CreationDate"] <= end_of_day]

    # --- cache data (same filters the dashboard would apply) ---
    db = Database(settings.sqlite_path)
    df = metrics.load_dataframe(db)
    filtered = metrics.apply_filters(
        df, projects=args.projects, date_start=args.start, date_end=args.end, tag_category=args.category
    )
    filtered = filtered.copy()
    filtered["formatted_id"] = filtered["formatted_id"].astype(str)

    ref_owners = set(ref["Owner"].dropna().unique()) if "Owner" in ref.columns else set()
    cache_owners = set(filtered["owner"].dropna().unique())
    all_owners = sorted(ref_owners | cache_owners)

    print(f"{'Owner':<28} {'Excel':>6} {'Cache':>6} {'Surplus':>8} {'Missing':>8}")
    print("-" * 62)
    for owner in all_owners:
        ref_ids = set(ref.loc[ref["Owner"] == owner, "FormattedID"]) if "Owner" in ref.columns else set()
        cache_ids = set(filtered.loc[filtered["owner"] == owner, "formatted_id"])

        surplus = cache_ids - ref_ids   # in cache, not in Excel
        missing = ref_ids - cache_ids   # in Excel, not in cache

        print(f"{owner:<28} {len(ref_ids):>6} {len(cache_ids):>6} {len(surplus):>8} {len(missing):>8}")
        if surplus:
            print(f"    surplus IDs (in cache, not in Excel): {sorted(surplus)}")
        if missing:
            print(f"    missing IDs (in Excel, not in cache): {sorted(missing)}")

    # duplicate check: same FormattedID appearing more than once in the cache
    dupes = filtered[filtered.duplicated("formatted_id", keep=False)]
    if not dupes.empty:
        print("\n!! DUPLICATE FormattedIDs found in cache filtered result:")
        print(dupes[["formatted_id", "owner", "project", "creation_date"]].sort_values("formatted_id"))
    else:
        print("\nNo duplicate FormattedIDs in the filtered cache result.")


if __name__ == "__main__":
    main()