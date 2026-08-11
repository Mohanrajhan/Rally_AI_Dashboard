#!/usr/bin/env python3
"""
CLI for pulling Rally TestCase data into the local cache.

Usage:
    python scripts/run_sync.py                # incremental (default)
    python scripts/run_sync.py --full          # force a full re-pull
    python scripts/run_sync.py --list-projects # just print projects in the workspace and exit

Intended to be invoked by cron / a scheduler for incremental refresh, e.g.:
    */30 * * * *  cd /app && python scripts/run_sync.py >> logs/sync.log 2>&1
"""

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.classifier import build_default_classifier
from src.db import Database
from src.rally_client import RallyClient
from src.settings import settings
from src.sync_service import SyncService

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)


def main():
    parser = argparse.ArgumentParser(description="Sync Rally TestCase data into local cache.")
    parser.add_argument("--full", action="store_true", help="Force a full re-pull instead of incremental.")
    parser.add_argument("--list-projects", action="store_true", help="List projects in the workspace and exit.")
    args = parser.parse_args()

    settings.validate()

    client = RallyClient(
        api_key=settings.rally_api_key,
        base_url=settings.rally_base_url,
        page_size=settings.rally_page_size,
        timeout=settings.rally_timeout,
        max_retries=settings.rally_max_retries,
        backoff_base=settings.rally_backoff_base,
    )

    if args.list_projects:
        projects = client.list_projects(settings.rally_workspace_id)
        for p in projects:
            print(f"{p['object_id']}\t{p['name']}\t{p['state']}\tparent={p.get('parent_name') or '(top-level)'}")
        return

    db = Database(settings.sqlite_path)
    classifier = build_default_classifier()

    service = SyncService(
        client=client,
        db=db,
        classifier=classifier,
        workspace_id=settings.rally_workspace_id,
        full_sync_lookback_days=settings.full_sync_lookback_days,
    )

    summary = service.run(force_full=args.full)
    print(summary)


if __name__ == "__main__":
    main()
