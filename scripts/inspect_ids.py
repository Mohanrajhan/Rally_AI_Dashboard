#!/usr/bin/env python3
"""
Look up specific Test Case IDs directly in the local cache and print their
creation date, project, owner, category, and tags — useful for investigating
"surplus"/"missing" IDs flagged by diagnose_mismatch.py.

Usage:
    python scripts/inspect_ids.py TC280917 TC280918 TC280919 ...
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd

from src import metrics
from src.db import Database
from src.settings import settings


def main():
    ids = sys.argv[1:]
    if not ids:
        print("Usage: python scripts/inspect_ids.py TC280917 TC280918 ...")
        return

    db = Database(settings.sqlite_path)
    df = metrics.load_dataframe(db)
    df["formatted_id"] = df["formatted_id"].astype(str)

    result = df[df["formatted_id"].isin(ids)].copy()
    result["creation_date"] = result["creation_date"].dt.strftime("%Y-%m-%d %H:%M:%S UTC")
    result["tags_display"] = result["tags"].apply(lambda t: ", ".join(t) if t else "(none)")

    cols = ["formatted_id", "owner", "project", "category", "creation_date", "tags_display"]
    print(result[cols].sort_values("formatted_id").to_string(index=False))

    found = set(result["formatted_id"])
    not_found = [i for i in ids if i not in found]
    if not_found:
        print(f"\nNot found in cache at all: {not_found}")


if __name__ == "__main__":
    main()