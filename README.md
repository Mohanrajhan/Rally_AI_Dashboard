# GAIG - Rally AI-Adoption Dashboard

A dashboard that connects to **Rally (Broadcom Agile Central)** via the WSAPI,
pulls `TestCase` data across every Project in a Workspace, classifies each
test case as **AI-Assisted** or **Manual** based on Tags, and surfaces a
leaderboard ranking owners by their efficient use of AI in generating test
cases.

## Architecture

```
                ┌───────────────────┐
   cron/manual  │  scripts/run_sync │   (incremental or full)
   ─────────────▶       .py         │
                └─────────┬─────────┘
                          │ uses
              ┌───────────▼────────────┐      ┌────────────────────┐
              │   src/rally_client.py   │─────▶│  Rally WSAPI v2.0  │
              │ (auth, pagination,      │      └────────────────────┘
              │  retry/backoff)         │
              └───────────┬────────────┘
                          │ TestCaseRecord[]
              ┌───────────▼────────────┐
              │   src/classifier.py     │  (config-driven tag → category)
              └───────────┬────────────┘
                          │ category per record
              ┌───────────▼────────────┐
              │   src/sync_service.py   │  (orchestrates + watermarking)
              └───────────┬────────────┘
                          │ upsert
              ┌───────────▼────────────┐
              │   src/db.py (SQLite)    │  <── the "data layer" boundary
              └───────────┬────────────┘
                          │ read-only
              ┌───────────▼────────────┐
              │   src/metrics.py        │  (pandas aggregation)
              └───────────┬────────────┘
                          │
              ┌───────────▼────────────┐
              │   src/app.py (Dash)     │  filters, cards, charts, leaderboard,
              │                         │  drill-down
              └─────────────────────────┘
```

The dashboard **never calls Rally directly**. It only reads the local
SQLite cache. Sync (full or incremental) is a separate process you run on
a schedule. This keeps page loads fast and keeps Rally API usage low and
predictable, and means the same cached data layer could just as easily
feed Power BI / Tableau / Grafana instead of (or alongside) the bundled
Dash UI — point any of those at the same SQLite file, or swap in Postgres
(see "Reusing the data layer" below).

## Stack

- **Data layer / sync:** Python, `requests`, SQLite (swap-in Postgres-ready)
- **Dashboard UI:** Plotly Dash + Dash Bootstrap Components
- Chosen because it's one language end-to-end, has first-class pandas/Plotly
  integration for the charts required here, and doesn't require a separate
  frontend build step. If your org standardizes on Power BI/Tableau/Grafana
  instead, point one of those at the same SQLite (or migrated Postgres)
  cache as a custom/ODBC data source — none of the ingestion or
  classification logic needs to change.

## 1. Setup

```bash
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Edit `.env`:

```
RALLY_API_KEY=<your Rally API key>
```

Get an API key from Rally: **avatar menu → API Keys → Create New**. The key
is passed as the `zsessionid` header on every request (never Basic Auth,
never embedded in a URL/query string, never sent to the browser — it only
ever lives server-side, loaded from the environment).

### Find your Workspace ID

```bash
curl -H "zsessionid: $RALLY_API_KEY" \
  "https://rally1.rallydev.com/slm/webservice/v2.0/workspace.js"
```

Copy the numeric `ObjectID` of the workspace you want, then set it in
`config/config.yaml`:

```yaml
rally:
  workspace_id: "12345678910"
```

(or set `RALLY_WORKSPACE_ID` in `.env` to override without editing the file).

### Confirm project enumeration works

```bash
python scripts/run_sync.py --list-projects
```

This lists every Project in the workspace — useful to sanity-check
connectivity/permissions before pulling TestCases.

## 2. Configuring tag classification

Tag naming conventions vary by org and drift over time, so classification
is entirely config-driven — **no code changes needed** to add a new tag
variant. Edit `config/config.yaml`:

```yaml
classification:
  count_unclassified_as_manual: false
  categories:
    AI-Assisted:
      - "ai-assisted"
      - "ai-generated"
      - "genai"
      - "copilot-generated"
    Manual:
      - "manual"
      - "manually-created"
      - "human-authored"
```

Matching is case/whitespace/hyphen/underscore-insensitive (`"AI Assisted"`,
`"ai-assisted"`, `"AI_ASSISTED"` all match the same rule). To find the
**actual tag names in use** in your org before finalizing this list:

```bash
curl -H "zsessionid: $RALLY_API_KEY" \
  "https://rally1.rallydev.com/slm/webservice/v2.0/tag?fetch=Name&pagesize=200"
```

Review that list and add every real variant to the appropriate category in
`config.yaml`. A test case whose tags don't match anything falls into
`Unclassified`; toggle `count_unclassified_as_manual` if you'd rather have
those count against the "Manual" denominator instead of being excluded.

If a test case carries tags from *both* categories (a data-quality edge
case), `AI-Assisted` wins by default — see `ai_wins_conflicts` in
`src/classifier.py` if you'd rather it resolve the other way.

## 3. Running a sync

```bash
# first run: bounded full pull (default lookback: 730 days, config.yaml -> sync.full_sync_lookback_days)
python scripts/run_sync.py --full

# subsequent runs: incremental (only TestCases updated since last successful sync)
python scripts/run_sync.py
```

Schedule the incremental form via cron (or Airflow/Lambda/etc.):

```
*/30 * * * *  cd /app && python scripts/run_sync.py >> logs/sync.log 2>&1
```

**How incremental sync works:** the sync watermark (`sync_state` table in
SQLite) records the timestamp of the last *successful* run. The next run
queries Rally for `LastUpdateDate >= watermark - 15min` (a small overlap to
tolerate clock skew), so both new test cases and edits to existing ones
(e.g., someone adds an `AI-Assisted` tag after the fact) get picked up
without ever re-pulling the full dataset. The watermark only advances after
a fully successful run, so a failed sync safely retries the same window
next time instead of silently dropping data.

**Rate limiting / retries:** `src/rally_client.py` retries `429` and `5xx`
responses with exponential backoff + jitter (configurable via
`rally.max_retries` / `rally.backoff_base_secs`), and honors a `Retry-After`
header when Rally sends one. Non-retryable 4xx errors surface immediately
with the response body so bad auth/queries fail fast and loud.

## 4. Running the dashboard

```bash
./scripts/run_dashboard.sh
# or: python -m src.app
```

Then open `http://localhost:8050`.

**UI features:**

- Filters: Project (multi-select), Date range (on `CreationDate`), Tag category
- Summary cards: total test cases, org-wide AI-Assisted %, top contributor
- Leaderboard: toggle **Highest AI-Assisted %** vs **Most AI-Assisted
  (count)**; configurable minimum-test-case-count threshold (default 10,
  set in `config.yaml` under `leaderboard.min_test_case_count`) so low-volume
  contributors don't skew the % ranking; trend arrow vs. the prior period
  once at least two sync periods of history exist
- Charts: AI vs Manual split (pie), adoption trend over time (bar + line,
  weekly/monthly per `leaderboard.trend_period`), per-project stacked
  comparison
- Drill-down: click a leaderboard row to see that owner's individual test
  cases (ID, name, project, category, tags, created date)

## 5. Reusing the data layer outside this dashboard

`src/rally_client.py`, `src/classifier.py`, `src/sync_service.py`, and
`src/db.py` have no dependency on Dash. They're usable standalone, e.g.:

```python
from src.settings import settings
from src.rally_client import RallyClient
from src.classifier import build_default_classifier
from src.db import Database
from src.sync_service import SyncService

client = RallyClient(settings.rally_api_key, settings.rally_base_url)
db = Database(settings.sqlite_path)
SyncService(client, db, build_default_classifier(), settings.rally_workspace_id).run()
```

To swap SQLite for Postgres: replace `sqlite3.connect` in `src/db.py` with
your Postgres driver of choice and change the `ON CONFLICT` syntax (already
Postgres-compatible) and `?` placeholders to `%s` — the rest of the
pipeline (rally_client, classifier, sync_service, metrics) is unaffected.
To feed Power BI/Tableau/Grafana instead of (or alongside) Dash, point
them at the same database as a data source; `metrics.py`'s SQL-shaped
DataFrame output maps directly to the columns a BI tool would expect.

## 6. Security notes

- The API key is read **only** from the environment (`RALLY_API_KEY`),
  never from `config.yaml`, never hardcoded, and never sent to the
  browser/client — `src/app.py` and the Dash frontend never see it; only
  `src/rally_client.py` (server-side) does.
- `.env` is git-ignored (see `.gitignore`); use a real secrets manager
  (AWS Secrets Manager, HashiCorp Vault, Azure Key Vault, etc.) in
  production instead of a `.env` file, and inject `RALLY_API_KEY` as a
  process environment variable at deploy time.
- Rotate the Rally API key periodically per your org's policy; revoking a
  key immediately invalidates it without needing a password change.

## 7. Project layout

```
rally_ai_dashboard/
├── config/
│   └── config.yaml          # workspace id, tag mappings, thresholds — no secrets
├── .env.example              # template for RALLY_API_KEY etc.
├── requirements.txt
├── src/
│   ├── settings.py           # config loader (YAML + env overrides; key from env only)
│   ├── rally_client.py       # WSAPI auth, pagination, retry/backoff, project discovery
│   ├── classifier.py         # configurable tag -> AI-Assisted/Manual/Unclassified
│   ├── db.py                 # SQLite cache: schema, upserts, sync watermark
│   ├── sync_service.py       # full/incremental sync orchestration
│   ├── metrics.py            # per-owner metrics, leaderboard, trend, per-project
│   └── app.py                # Dash UI: filters, cards, charts, leaderboard, drill-down
├── scripts/
│   ├── run_sync.py           # CLI: full/incremental sync, --list-projects
│   ├── run_dashboard.sh      # launch the Dash app
│   └── smoke_test.py         # offline pipeline check with synthetic data (no network)
└── data/
    └── rally_cache.db        # created on first run (git-ignored)
```

## 8. Known Rally API nuance to verify against your instance

Rally's WSAPI can return a TestCase's `Tags` collection in slightly
different shapes depending on org/version — sometimes with tag names
inlined, sometimes as a bare collection ref requiring a follow-up fetch.
`src/rally_client.py::_to_record` handles the common inlined shapes; if
your instance returns bare refs with no names, use the included
`RallyClient.fetch_tag_names_expanded()` helper (already provided) to
resolve names in batch — wire it into `iter_test_cases` if you hit this
case. Run `python scripts/run_sync.py --full` against a small test project
first and inspect a few rows in `data/rally_cache.db` (`tags_json` column)
to confirm tag names are populating before scheduling recurring syncs.
