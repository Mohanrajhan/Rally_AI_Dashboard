"""
Sync orchestrator.

`SyncService.run()` is the single entry point used both by the CLI
(scripts/run_sync.py, e.g. via cron / Airflow / a scheduled Lambda) and by
the optional in-process APScheduler job started from app.py.

Incremental strategy
---------------------
- On first run (no sync_state row), do a bounded full pull: everything with
  CreationDate within `full_sync_lookback_days`.
- On subsequent runs, pull only TestCases with LastUpdateDate >= last
  successful sync watermark (minus a small overlap window to tolerate clock
  skew / in-flight writes), then upsert — which naturally handles both new
  test cases and edits (e.g. a tag added after creation) without re-pulling
  the whole dataset.
- The watermark is only advanced AFTER a fully successful sync, so a failed
  run safely retries the same window next time rather than silently skipping data.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from src.classifier import TagClassifier
from src.db import Database
from src.rally_client import RallyClient, TestCaseRecord

logger = logging.getLogger("sync_service")

CLOCK_SKEW_OVERLAP = timedelta(minutes=15)


class SyncService:
    def __init__(
        self,
        client: RallyClient,
        db: Database,
        classifier: TagClassifier,
        workspace_id: str,
        full_sync_lookback_days: int = 730,
    ):
        self.client = client
        self.db = db
        self.classifier = classifier
        self.workspace_id = workspace_id
        self.full_sync_lookback_days = full_sync_lookback_days

    def run(self, force_full: bool = False) -> dict:
        started_at = datetime.now(timezone.utc)
        last_sync_str = None if force_full else self.db.get_last_sync(self.workspace_id)

        if last_sync_str:
            updated_since = datetime.fromisoformat(last_sync_str) - CLOCK_SKEW_OVERLAP
            mode = "incremental"
        else:
            updated_since = started_at - timedelta(days=self.full_sync_lookback_days)
            mode = "full"

        logger.info("Starting %s sync for workspace %s (since %s)",
                    mode, self.workspace_id, updated_since.isoformat())

        # Refresh the real Rally project hierarchy every sync — cheap (one
        # paginated call) and keeps program->sub-project expansion current
        # without needing a dashboard restart.
        projects = self.client.list_projects(self.workspace_id)
        self.db.upsert_projects(projects)

        records: list[TestCaseRecord] = list(
            self.client.iter_test_cases(
                workspace_id=self.workspace_id,
                updated_since=updated_since,
            )
        )

        categories = {r.formatted_id: self.classifier.classify(r.tags) for r in records}
        self.db.upsert_test_cases(records, categories)

        # Test case EXECUTIONS (TestCaseResult) — separate object type,
        # separate incremental window, same watermark logic.
        result_records = list(
            self.client.iter_test_case_results(
                workspace_id=self.workspace_id,
                updated_since=updated_since,
            )
        )
        self.db.upsert_test_case_results(result_records)

        # only advance the watermark once everything above succeeded
        self.db.set_last_sync(self.workspace_id, started_at.isoformat())

        summary = {
            "mode": mode,
            "records_synced": len(records),
            "projects_synced": len(projects),
            "results_synced": len(result_records),
            "ai_assisted": sum(1 for c in categories.values() if c == "AI-Assisted"),
            "manual": sum(1 for c in categories.values() if c == "Manual"),
            "unclassified": sum(1 for c in categories.values() if c == "Unclassified"),
            "started_at": started_at.isoformat(),
            "finished_at": datetime.now(timezone.utc).isoformat(),
        }
        logger.info("Sync complete: %s", summary)
        return summary
