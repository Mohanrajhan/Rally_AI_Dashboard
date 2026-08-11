#!/usr/bin/env python3
"""Offline smoke test: exercises classifier -> db -> metrics with synthetic
TestCaseRecords, without touching the network. Not part of the deliverable
runtime; just used to sanity-check the pipeline wiring."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import os
os.environ.setdefault("RALLY_API_KEY", "dummy-for-smoke-test")

from datetime import datetime, timedelta, timezone
import random

from src.classifier import build_default_classifier
from src.db import Database
from src.rally_client import TestCaseRecord
from src import metrics

classifier = build_default_classifier()

owners = ["Alice Chen", "Bob Singh", "Carla Diaz", "Dev Patel"]
projects = ["Payments", "Checkout", "Auth"]
tag_pool_ai = ["AI-Assisted", "ai-generated", "GenAI"]
tag_pool_manual = ["Manual", "manually-created"]

records = []
random.seed(42)
base = datetime.now(timezone.utc) - timedelta(days=60)
for i in range(240):
    owner = random.choice(owners)
    is_ai = random.random() < (0.7 if owner == "Alice Chen" else 0.3)
    tags = [random.choice(tag_pool_ai)] if is_ai else [random.choice(tag_pool_manual)]
    created = base + timedelta(days=random.randint(0, 60))
    records.append(TestCaseRecord(
        formatted_id=f"TC{i:04d}",
        name=f"Test case {i}",
        tags=tags,
        owner=owner,
        owner_ref=f"user/{owner}",
        project=random.choice(projects),
        project_ref="project/1",
        workspace="Demo Workspace",
        creation_date=created.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        last_update_date=created.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        raw={},
    ))

db = Database("data/smoke_test.db")
categories = {r.formatted_id: classifier.classify(r.tags) for r in records}
db.upsert_test_cases(records, categories)

print("Total rows in DB:", db.count())

df = metrics.load_dataframe(db)
print("\n--- per_owner_metrics ---")
print(metrics.per_owner_metrics(df))

print("\n--- leaderboard (pct, min_count=10) ---")
print(metrics.leaderboard(df, mode="pct", min_count=10))

print("\n--- per_project_breakdown ---")
print(metrics.per_project_breakdown(df))

print("\n--- summary_cards ---")
print(metrics.summary_cards(df))

print("\n--- trend_over_time (weekly) ---")
print(metrics.trend_over_time(df, freq="W").head())

print("\nSMOKE TEST PASSED")
