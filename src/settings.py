"""
Central settings loader.

Loads config/config.yaml, then applies environment-variable overrides
(from .env / real env) so the same config file can be reused across
dev/staging/prod without edits. The Rally API key is ALWAYS sourced from
the environment (or a secrets manager) and is never read from YAML —
this file intentionally has no code path that accepts a key from config.yaml.
"""

import os
from pathlib import Path
import yaml
from dotenv import load_dotenv

ROOT_DIR = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT_DIR / "config" / "config.yaml"

load_dotenv(ROOT_DIR / ".env")


def _load_yaml() -> dict:
    with open(CONFIG_PATH, "r") as f:
        return yaml.safe_load(f)


class Settings:
    def __init__(self):
        raw = _load_yaml()

        # --- Rally connection ---
        self.rally_base_url = os.getenv("RALLY_BASE_URL") or raw["rally"]["base_url"]
        self.rally_workspace_id = os.getenv("RALLY_WORKSPACE_ID") or raw["rally"]["workspace_id"]
        self.rally_page_size = int(raw["rally"].get("page_size", 200))
        self.rally_timeout = int(raw["rally"].get("request_timeout_secs", 30))
        self.rally_max_retries = int(raw["rally"].get("max_retries", 5))
        self.rally_backoff_base = float(raw["rally"].get("backoff_base_secs", 1.5))
        self.rally_fields = raw["rally"]["fields"]

        # --- Secret: API key comes ONLY from the environment ---
        self.rally_api_key = os.getenv("RALLY_API_KEY")

        # --- Classification ---
        self.tag_categories: dict[str, list[str]] = raw["classification"]["categories"]
        self.count_unclassified_as_manual = bool(
            raw["classification"].get("count_unclassified_as_manual", False)
        )
        
        # --- Programs (project groupings) ---
        self.programs: dict[str, list[str]] = raw.get("programs", {}) or {}

        # --- Leaderboard ---
        self.leaderboard_min_count = int(raw["leaderboard"].get("min_test_case_count", 10))
        self.leaderboard_default_mode = raw["leaderboard"].get("default_rank_mode", "pct")
        self.trend_period = raw["leaderboard"].get("trend_period", "weekly")

        # --- Sync ---
        self.sqlite_path = str(ROOT_DIR / raw["sync"]["sqlite_path"])
        self.full_sync_lookback_days = int(raw["sync"].get("full_sync_lookback_days", 730))

        auto_sync = raw["sync"].get("auto_sync", {}) or {}
        self.auto_sync_enabled = bool(auto_sync.get("enabled", False))
        self.auto_sync_interval_minutes = int(auto_sync.get("interval_minutes", 15))

        # --- Dashboard ---
        dashboard = raw.get("dashboard", {}) or {}
        self.dashboard_auto_refresh_seconds = int(dashboard.get("auto_refresh_seconds", 60))

    def validate(self):
        problems = []
        if not self.rally_api_key:
            problems.append(
                "RALLY_API_KEY is not set. Set it in .env or your secrets manager."
            )
        if not self.rally_workspace_id or self.rally_workspace_id.startswith("REPLACE_"):
            problems.append(
                "rally.workspace_id is not configured. Set it in config/config.yaml "
                "or via RALLY_WORKSPACE_ID env var."
            )
        if problems:
            raise RuntimeError("Configuration problem(s):\n- " + "\n- ".join(problems))


settings = Settings()
