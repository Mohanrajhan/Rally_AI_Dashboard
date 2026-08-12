"""
Rally (Broadcom Agile Central) WSAPI v2.0 client.

Responsibilities
-----------------
- Authenticate via API key (zsessionid header) — never via username/password.
- Discover the Workspace and (optionally) enumerate Projects within it.
- Query TestCase objects at workspace scope with ProjectScopeUp/Down so a
  single query covers every project without an explicit per-project loop
  (loop-per-project is also supported and used as a fallback / for
  per-project breakdowns that require project-scoped queries).
- Handle WSAPI pagination (`start`, `pagesize`).
- Handle rate limiting (HTTP 429) and transient 5xx errors with exponential
  backoff + jitter.
- Normalize the raw JSON into plain Python dicts the rest of the pipeline
  can consume (flatten Tags to a list of tag name strings, etc.)
"""

from __future__ import annotations

import logging
import random
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterable, Iterator, Optional

import requests

logger = logging.getLogger("rally_client")

# NOTE: Standard Rally TestCase has no separate "Creator" field exposed via
# WSAPI in most orgs; Owner is the widely-available proxy for "who this test
# case is attributed to". If your org has a custom field (e.g. c_CreatedBy),
# change this constant — nothing else in the pipeline needs to change.
OWNER_FIELD = "Owner"


@dataclass
class TestCaseRecord:
    formatted_id: str
    name: str
    tags: list[str]
    owner: Optional[str]
    owner_ref: Optional[str]
    project: Optional[str]
    project_ref: Optional[str]
    workspace: Optional[str]
    creation_date: Optional[str]
    last_update_date: Optional[str]
    raw: dict = field(default_factory=dict, repr=False)

@dataclass
class TestCaseResultRecord:
    result_object_id: str
    test_case_formatted_id: Optional[str]
    execution_date: Optional[str]
    verdict: Optional[str]
    tester: Optional[str]
    build: Optional[str]


class RallyApiError(RuntimeError):
    pass


class RallyClient:
    def __init__(
        self,
        api_key: str,
        base_url: str,
        page_size: int = 200,
        timeout: int = 30,
        max_retries: int = 5,
        backoff_base: float = 1.5,
    ):
        if not api_key:
            raise RallyApiError("Rally API key is missing. Set RALLY_API_KEY.")
        self.base_url = base_url.rstrip("/") + "/"
        self.page_size = page_size
        self.timeout = timeout
        self.max_retries = max_retries
        self.backoff_base = backoff_base

        self.session = requests.Session()
        self.session.headers.update(
            {
                "zsessionid": api_key,
                "Content-Type": "application/json",
                "Accept": "application/json",
                "X-RallyIntegrationName": "AI-Adoption Dashboard",
                "X-RallyIntegrationVendor": "Internal",
                "X-RallyIntegrationVersion": "1.0",
            }
        )

    # ------------------------------------------------------------------
    # low-level HTTP with retry/backoff
    # ------------------------------------------------------------------
    def _get(self, path: str, params: dict) -> dict:
        url = self.base_url + path
        attempt = 0
        while True:
            attempt += 1
            try:
                resp = self.session.get(url, params=params, timeout=self.timeout)
            except requests.RequestException as exc:
                if attempt > self.max_retries:
                    raise RallyApiError(f"Network error calling {url}: {exc}") from exc
                self._sleep_backoff(attempt, reason=str(exc))
                continue

            if resp.status_code == 200:
                return resp.json()

            if resp.status_code == 429 or 500 <= resp.status_code < 600:
                if attempt > self.max_retries:
                    raise RallyApiError(
                        f"Rally API failed after {attempt} attempts: "
                        f"{resp.status_code} {resp.text[:500]}"
                    )
                retry_after = resp.headers.get("Retry-After")
                self._sleep_backoff(attempt, reason=f"HTTP {resp.status_code}", retry_after=retry_after)
                continue

            # 4xx (other than 429) — not retryable, surface immediately
            raise RallyApiError(
                f"Rally API error {resp.status_code} calling {url}: {resp.text[:1000]}"
            )

    def _sleep_backoff(self, attempt: int, reason: str, retry_after: Optional[str] = None):
        if retry_after:
            try:
                delay = float(retry_after)
            except ValueError:
                delay = self.backoff_base * (2 ** attempt)
        else:
            delay = self.backoff_base * (2 ** attempt)
        delay += random.uniform(0, 0.5)  # jitter
        logger.warning(
            "Retrying Rally request (attempt %d/%d) after %.1fs — reason: %s",
            attempt, self.max_retries, delay, reason,
        )
        time.sleep(delay)

    # ------------------------------------------------------------------
    # Workspace / Project discovery
    # ------------------------------------------------------------------
    def get_workspace_ref(self, workspace_id: str) -> str:
        return f"workspace/{workspace_id}"

    def list_projects(self, workspace_id: str) -> list[dict]:
        """Enumerate every Project in the workspace, including each one's
        Parent reference — this is Rally's REAL project hierarchy (distinct
        from the config.yaml `programs` grouping), used to auto-expand a
        program's listed projects to include their actual Rally sub-projects."""
        projects = []
        start = 1
        while True:
            params = {
                "workspace": self.get_workspace_ref(workspace_id),
                "fetch": "Name,ObjectID,State,Parent",
                "pagesize": self.page_size,
                "start": start,
                "order": "Name",
            }
            data = self._get("project", params)
            result = data.get("QueryResult", {})
            errors = result.get("Errors") or []
            if errors:
                raise RallyApiError(f"Rally query errors: {errors}")

            for p in result.get("Results", []):
                parent = p.get("Parent") or {}
                projects.append(
                    {
                        "object_id": p.get("ObjectID"),
                        "name": p.get("Name"),
                        "state": p.get("State"),
                        "ref": p.get("_ref"),
                        "parent_name": parent.get("_refObjectName"),
                    }
                )

            total = result.get("TotalResultCount", 0)
            start += self.page_size
            if start > total:
                break
        return projects

    # ------------------------------------------------------------------
    # TestCase queries
    # ------------------------------------------------------------------
    def iter_test_cases(
        self,
        workspace_id: str,
        project_id: Optional[str] = None,
        fields: Optional[list[str]] = None,
        updated_since: Optional[datetime] = None,
    ) -> Iterator[TestCaseRecord]:
        """
        Yield every TestCase in the workspace, paginating transparently.

        - If project_id is None: queries at workspace scope with
          ProjectScopeUp=true & ProjectScopeDown=true, i.e. every project
          in the workspace hierarchy is included in one query stream.
        - If project_id is set: scopes to that single project (used when the
          caller wants a strict per-project loop instead, e.g. to cross-check
          counts or when workspace-level scope is disabled by org policy).
        - If updated_since is set: adds a WSAPI query filter on
          LastUpdateDate for incremental sync.
        """
        fields = fields or [
            "FormattedID", "Name", "Tags", "Owner", "Project",
            "Workspace", "CreationDate", "LastUpdateDate",
        ]
        fetch = ",".join(fields)

        params = {
            "workspace": self.get_workspace_ref(workspace_id),
            "fetch": fetch,
            "pagesize": self.page_size,
            "start": 1,
            "order": "CreationDate ASC",
        }

        if project_id:
            params["project"] = f"project/{project_id}"
            # scope strictly to this project (no children roll-up), since the
            # caller is deliberately iterating project-by-project
            params["projectScopeUp"] = "false"
            params["projectScopeDown"] = "false"
        else:
            # workspace-wide: no project filter, but scope up/down ensures
            # every project in the hierarchy is covered in one pass
            params["projectScopeUp"] = "true"
            params["projectScopeDown"] = "true"

        if updated_since:
            ts = updated_since.strftime("%Y-%m-%dT%H:%M:%S.000Z")
            params["query"] = f"(LastUpdateDate >= {ts})"

        start = 1
        total = None
        fetched = 0
        while total is None or start <= total:
            params["start"] = start
            data = self._get("testcase", params)
            result = data.get("QueryResult", {})
            errors = result.get("Errors") or []
            if errors:
                raise RallyApiError(f"Rally query errors: {errors}")

            total = result.get("TotalResultCount", 0)
            results = result.get("Results", [])
            if not results:
                break

            for tc in results:
                yield self._to_record(tc)

            fetched += len(results)
            start += self.page_size
            logger.info("Fetched %d/%d test cases (workspace=%s, project=%s)",
                        fetched, total, workspace_id, project_id or "ALL")

    def iter_test_case_results(
        self,
        workspace_id: str,
        updated_since: Optional[datetime] = None,
    ) -> Iterator[TestCaseResultRecord]:
        """
        Yield every TestCaseResult (an actual test EXECUTION, not the test
        case definition itself) in the workspace. A single TestCase can have
        many TestCaseResult records over time — one per run.
        """
        fetch = ",".join([
            "ObjectID", "TestCase.FormattedID", "TestCase.Name",
            "Date", "Verdict", "Tester", "Build",
            "CreationDate", "LastUpdateDate",
        ])
        params = {
            "workspace": self.get_workspace_ref(workspace_id),
            "fetch": fetch,
            "pagesize": self.page_size,
            "start": 1,
            "order": "Date ASC",
            "projectScopeUp": "true",
            "projectScopeDown": "true",
        }
        if updated_since:
            ts = updated_since.strftime("%Y-%m-%dT%H:%M:%S.000Z")
            params["query"] = f"(Date >= {ts})"

        start = 1
        total = None
        fetched = 0
        while total is None or start <= total:
            params["start"] = start
            data = self._get("testcaseresult", params)
            result = data.get("QueryResult", {})
            errors = result.get("Errors") or []
            if errors:
                raise RallyApiError(f"Rally query errors: {errors}")

            total = result.get("TotalResultCount", 0)
            results = result.get("Results", [])
            if not results:
                break

            for r in results:
                yield self._to_result_record(r)

            fetched += len(results)
            start += self.page_size
            logger.info("Fetched %d/%d test case results (workspace=%s)",
                        fetched, total, workspace_id)

    @staticmethod
    def _to_result_record(r: dict) -> TestCaseResultRecord:
        testcase_ref = r.get("TestCase") or {}
        tester = r.get("Tester") or {}
        return TestCaseResultRecord(
            result_object_id=str(r.get("ObjectID")),
            test_case_formatted_id=testcase_ref.get("FormattedID"),
            execution_date=r.get("Date"),
            verdict=r.get("Verdict"),
            tester=tester.get("_refObjectName"),
            build=r.get("Build"),
        )


    @staticmethod
    def _to_record(tc: dict) -> TestCaseRecord:
        # Tags come back as a collection; each item typically carries
        # _refObjectName with the human-readable tag name.
        tag_names: list[str] = []
        tags_field = tc.get("Tags")
        if isinstance(tags_field, dict):
            for t in tags_field.get("_tagsNameArray", []) or []:
                if t.get("Name"):
                    tag_names.append(t["Name"])
            # Fallback shape: some WSAPI responses nest under "Results" if
            # Tags was separately fetched/expanded.
            for t in tags_field.get("Results", []) or []:
                name = t.get("Name") or t.get("_refObjectName")
                if name:
                    tag_names.append(name)

        owner = tc.get(OWNER_FIELD) or {}
        project = tc.get("Project") or {}
        workspace = tc.get("Workspace") or {}

        return TestCaseRecord(
            formatted_id=tc.get("FormattedID"),
            name=tc.get("Name"),
            tags=sorted(set(tag_names)),
            owner=owner.get("_refObjectName"),
            owner_ref=owner.get("_ref"),
            project=project.get("_refObjectName"),
            project_ref=project.get("_ref"),
            workspace=workspace.get("_refObjectName"),
            creation_date=tc.get("CreationDate"),
            last_update_date=tc.get("LastUpdateDate"),
            raw=tc,
        )

    def fetch_tag_names_expanded(self, tag_refs: Iterable[str]) -> dict[str, str]:
        """
        Some Rally configs return Tags as a collection ref WITHOUT inline
        names (only `_ref` + `Count`). In that case each TestCase's Tags
        collection must be fetched individually:
            GET {tags_collection_ref}?fetch=Name
        This helper batches that lookup and returns {tag_ref: tag_name}.
        Only needed if `_to_record` finds no names — see rally_client
        integration notes in README for when this path activates.
        """
        out = {}
        for ref in tag_refs:
            data = self._get(ref.replace(self.base_url, ""), {"fetch": "Name"})
            results = data.get("QueryResult", {}).get("Results", [])
            for r in results:
                out[r.get("_ref")] = r.get("Name")
        return out
