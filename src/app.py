"""
GAIG - Rally AI-Adoption Dashboard — Dash front end.

Run with:  python -m src.app
(or via scripts/run_dashboard.sh)

The chart/table callbacks ONLY read from the local SQLite cache (src/db.py)
— they never call Rally directly, which keeps page interactions fast.
Freshness against Rally is handled two ways, both optional/independent:

  1. Background auto-sync (this process, via APScheduler): if
     sync.auto_sync.enabled is true in config.yaml, this process itself
     periodically calls SyncService.run() on a timer, so as long as the
     dashboard is running, new Rally test cases get pulled in automatically
     — no external cron / Task Scheduler required.
  2. Browser auto-refresh (dcc.Interval): the UI re-runs its chart/leaderboard
     callback every dashboard.auto_refresh_seconds, so once new data lands
     in the cache (from #1, or from a manual/cron run of
     scripts/run_sync.py) it shows up without a manual page reload.
"""

from __future__ import annotations

import io
import logging
import os

import dash
import dash_bootstrap_components as dbc
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
from apscheduler.schedulers.background import BackgroundScheduler
from dash import Input, Output, State, dcc, html, dash_table, ctx
from reportlab.lib import colors
from reportlab.lib.pagesizes import landscape, letter
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle

from src import metrics
from src.classifier import build_default_classifier
from src.db import Database
from src.rally_client import RallyClient
from src.settings import settings
from src.sync_service import SyncService

logger = logging.getLogger("dashboard")

db = Database(settings.sqlite_path)

app = dash.Dash(
    __name__,
    external_stylesheets=[dbc.themes.FLATLY],
    title="GAIG - Rally AI Adoption Dashboard",
)

TREND_ARROWS = {"up": "▲", "down": "▼", "flat": "▬", None: "–"}
TREND_COLORS = {"up": "#2e7d32", "down": "#c62828", "flat": "#757575", None: "#9e9e9e"}


def dataframe_to_pdf_bytes(df: pd.DataFrame, title: str) -> bytes:
    """Render a DataFrame as a simple landscape PDF table, for the Export
    PDF buttons on the leaderboard and drill-down tables."""
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=landscape(letter), topMargin=30, bottomMargin=30)
    data = [list(df.columns)] + df.astype(str).values.tolist()
    table = Table(data, repeatRows=1)
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1E2761")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 8),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F2F2F2")]),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
    ]))
    doc.build([table])
    buf.seek(0)
    return buf.getvalue()


LEADERBOARD_EXPORT_COLUMNS = ["rank", "owner", "total", "ai_assisted", "manual", "ai_adoption_pct", "program", "project"]
LEADERBOARD_EXPORT_HEADERS = ["Rank", "Owner", "Total Test Cases", "AI-Assisted", "Manual", "AI Adoption %", "Program", "Project"]

DRILLDOWN_EXPORT_COLUMNS = ["formatted_id", "name", "owner", "project", "category", "tags_display", "creation_date"]
DRILLDOWN_EXPORT_HEADERS = ["Formatted ID", "Name", "Owner", "Project", "Category", "Tags", "Created"]


# ==========================================================================
# Background auto-sync (in-process APScheduler)
# ==========================================================================
_scheduler: BackgroundScheduler | None = None


def _run_background_sync():
    """Executed on a timer by APScheduler. Any failure is logged and
    swallowed so one bad sync tick doesn't crash the dashboard process —
    the next tick will simply retry from the last successful watermark."""
    try:
        client = RallyClient(
            api_key=settings.rally_api_key,
            base_url=settings.rally_base_url,
            page_size=settings.rally_page_size,
            timeout=settings.rally_timeout,
            max_retries=settings.rally_max_retries,
            backoff_base=settings.rally_backoff_base,
        )
        service = SyncService(
            client=client,
            db=db,
            classifier=build_default_classifier(),
            workspace_id=settings.rally_workspace_id,
            full_sync_lookback_days=settings.full_sync_lookback_days,
        )
        summary = service.run()
        logger.info("Background auto-sync complete: %s", summary)
    except Exception:
        logger.exception("Background auto-sync failed — will retry on next interval.")


def start_background_sync():
    """Starts the APScheduler job once per actual running process.

    Dash's debug reloader (use_reloader=True, the default when debug=True)
    spawns a child process and re-imports this module in both the parent
    watcher and the child worker. WERKZEUG_RUN_MAIN is only set to "true"
    inside the child that actually serves requests, so we gate on it to
    avoid running two independent schedulers hammering Rally in parallel.
    In production (debug=False, e.g. behind gunicorn) WERKZEUG_RUN_MAIN is
    unset, so the check below simply allows it through.
    """
    global _scheduler
    if not settings.auto_sync_enabled:
        logger.info("Background auto-sync disabled (sync.auto_sync.enabled: false).")
        return
    if os.getenv("DASH_DEBUG", "false").lower() == "true" and os.getenv("WERKZEUG_RUN_MAIN") != "true":
        return
    if _scheduler is not None:
        return

    import datetime as _dt

    _scheduler = BackgroundScheduler(daemon=True)
    _scheduler.add_job(
        _run_background_sync,
        "interval",
        minutes=settings.auto_sync_interval_minutes,
        next_run_time=_dt.datetime.now() + _dt.timedelta(seconds=5),  # fire soon after startup, then on interval
        id="rally_auto_sync",
        max_instances=1,       # never let two syncs overlap if one runs long
        coalesce=True,
    )
    _scheduler.start()
    logger.info(
        "Background auto-sync started: every %d minute(s).",
        settings.auto_sync_interval_minutes,
    )


start_background_sync()


# ==========================================================================
# Layout
# ==========================================================================
def build_layout():
    df = metrics.load_dataframe(db)
    projects = sorted(df["project"].dropna().unique().tolist()) if not df.empty else []
    owners = sorted(df["owner"].dropna().unique().tolist()) if not df.empty else []
    programs = sorted(settings.programs.keys())

    return dbc.Container(
        fluid=True,
        children=[
            dbc.Row(
                [
                    dbc.Col(html.H2("GAIG - Rally AI-Adoption Dashboard"), width=8),
                    dbc.Col(
                        html.Div(id="last-synced-note", className="text-muted text-end"),
                        width=4,
                    ),
                ],
                className="mt-3 mb-2",
            ),

            # ---------------- Filters ----------------
            dbc.Card(
                dbc.CardBody(
                    [
                        dbc.Row(
                            [
                                dbc.Col(
                                    [
                                        html.Label("Program(s)"),
                                        dcc.Dropdown(
                                            id="filter-programs",
                                            options=[{"label": p, "value": p} for p in programs],
                                            multi=True,
                                            placeholder="All programs (adds their projects)",
                                        ),
                                    ],
                                    width=4,
                                ),
                                dbc.Col(
                                    [
                                        html.Label("Project(s)"),
                                        dcc.Dropdown(
                                            id="filter-projects",
                                            options=[{"label": p, "value": p} for p in projects],
                                            multi=True,
                                            placeholder="All projects",
                                        ),
                                    ],
                                    width=4,
                                ),
                                dbc.Col(
                                    [
                                        html.Label("Owner(s)"),
                                        dcc.Dropdown(
                                            id="filter-owners",
                                            options=[{"label": o, "value": o} for o in owners],
                                            multi=True,
                                            placeholder="All owners",
                                        ),
                                    ],
                                    width=4,
                                ),
                            ],
                            className="mb-3",
                        ),
                        dbc.Row(
                            [
                                dbc.Col(
                                    [
                                        html.Label("Date range (Created)"),
                                        dcc.DatePickerRange(id="filter-date-range"),
                                    ],
                                    width=6,
                                ),
                                dbc.Col(
                                    [
                                        html.Label("Tag category"),
                                        dcc.Dropdown(
                                            id="filter-category",
                                            options=[
                                                {"label": "All", "value": "All"},
                                                {"label": "AI-Assisted", "value": "AI-Assisted"},
                                                {"label": "Manual", "value": "Manual"},
                                                {"label": "Unclassified", "value": "Unclassified"},
                                            ],
                                            value="All",
                                            clearable=False,
                                        ),
                                    ],
                                    width=6,
                                ),
                            ]
                        ),
                    ]
                ),
                className="mb-3",
            ),

            # ---------------- Summary cards ----------------
            dbc.Row(id="summary-cards", className="mb-3 g-3"),

            # ---------------- Leaderboard controls ----------------
            dbc.Card(
                dbc.CardBody(
                    [
                        dbc.Row(
                            [
                                dbc.Col(
                                    [
                                        html.H4("Leaderboard: AI-Efficient Test Case Creators", className="d-inline me-3"),
                                        dbc.Button("Select All", id="select-all-owners", size="sm", color="link", className="p-0 me-2"),
                                        dbc.Button("Clear", id="clear-all-owners", size="sm", color="link", className="p-0"),
                                    ],
                                    width=6,
                                ),
                                dbc.Col(
                                    dcc.RadioItems(
                                        id="rank-mode",
                                        options=[
                                            {"label": " Highest AI-Assisted %", "value": "pct"},
                                            {"label": " Most AI-Assisted (count)", "value": "count"},
                                        ],
                                        value=settings.leaderboard_default_mode,
                                        inline=True,
                                        inputStyle={"marginRight": "6px", "marginLeft": "12px"},
                                    ),
                                    width=4,
                                ),
                                dbc.Col(
                                    dbc.InputGroup(
                                        [
                                            dbc.InputGroupText("Min. test cases"),
                                            dbc.Input(
                                                id="min-count",
                                                type="number",
                                                value=settings.leaderboard_min_count,
                                                min=1,
                                            ),
                                        ]
                                    ),
                                    width=2,
                                ),
                            ],
                            className="mb-3",
                        ),
                        dash_table.DataTable(
                            id="leaderboard-table",
                            columns=[
                                {"name": "Rank", "id": "rank"},
                                {"name": "Owner", "id": "owner"},
                                {"name": "Total Test Cases", "id": "total"},
                                {"name": "AI-Assisted", "id": "ai_assisted"},
                                {"name": "Manual", "id": "manual"},
                                {"name": "AI Adoption %", "id": "ai_adoption_pct"},
                                {"name": "Program", "id": "program"},
                                {"name": "Project", "id": "project"},
                            ],
                            row_selectable="multi",
                            sort_action="native",
                            page_size=10,
                            style_table={"overflowX": "auto", "minWidth": "100%"},
                            style_cell={
                                "textAlign": "left", "padding": "8px",
                                "whiteSpace": "normal", "height": "auto",
                            },
                            style_cell_conditional=[
                                {"if": {"column_id": "rank"}, "minWidth": "60px", "width": "60px", "maxWidth": "60px"},
                                {"if": {"column_id": "owner"}, "minWidth": "160px", "width": "180px"},
                                {"if": {"column_id": "total"}, "minWidth": "90px", "width": "100px"},
                                {"if": {"column_id": "ai_assisted"}, "minWidth": "90px", "width": "100px"},
                                {"if": {"column_id": "manual"}, "minWidth": "90px", "width": "100px"},
                                {"if": {"column_id": "ai_adoption_pct"}, "minWidth": "100px", "width": "110px"},
                                {"if": {"column_id": "program"}, "minWidth": "160px", "width": "200px"},
                                {"if": {"column_id": "project"}, "minWidth": "220px", "width": "320px"},
                            ],
                            style_header={"fontWeight": "bold"},
                            style_data_conditional=[
                                {
                                    "if": {"filter_query": "{rank} = 1"},
                                    "backgroundColor": "#fff8e1",
                                }
                            ],
                        ),
                        dbc.ButtonGroup(
                            [
                                dbc.Button("Export CSV", id="export-leaderboard-csv", size="sm", color="secondary", outline=True),
                                dbc.Button("Export Excel", id="export-leaderboard-xlsx", size="sm", color="secondary", outline=True),
                                dbc.Button("Export PDF", id="export-leaderboard-pdf", size="sm", color="secondary", outline=True),
                            ],
                            className="mt-2",
                        ),
                        dcc.Download(id="download-leaderboard"),
                        html.Div(
                            "Tip: check one or more rows to drill into those owners' individual test cases below.",
                            className="text-muted mt-2 small",
                        ),
                    ]
                ),
                className="mb-3",
            ),

            # ---------------- Charts ----------------
            dbc.Row(
                [
                    dbc.Col(dcc.Graph(id="chart-split"), width=4),
                    dbc.Col(dcc.Graph(id="chart-trend"), width=8),
                ],
                className="mb-3",
            ),
            dbc.Row(
                [
                    dbc.Col(dcc.Graph(id="chart-per-project"), width=12),
                ],
                className="mb-3",
            ),

            # ---------------- Drill-down ----------------
            dbc.Card(
                dbc.CardBody(
                    [
                        html.H4(id="drilldown-title", children="Select one or more owners above to drill in"),
                        dash_table.DataTable(
                            id="drilldown-table",
                            columns=[
                                {"name": "Formatted ID", "id": "formatted_id"},
                                {"name": "Name", "id": "name"},
                                {"name": "Owner", "id": "owner"},
                                {"name": "Project", "id": "project"},
                                {"name": "Category", "id": "category"},
                                {"name": "Tags", "id": "tags_display"},
                                {"name": "Created", "id": "creation_date"},
                            ],
                            page_size=10,
                            sort_action="native",
                            style_table={"overflowX": "auto", "minWidth": "100%"},
                            style_cell={
                                "textAlign": "left", "padding": "8px",
                                "whiteSpace": "normal", "height": "auto",
                            },
                            style_cell_conditional=[
                                {"if": {"column_id": "formatted_id"}, "minWidth": "100px", "width": "100px"},
                                {"if": {"column_id": "name"}, "minWidth": "300px", "width": "380px"},
                                {"if": {"column_id": "owner"}, "minWidth": "160px", "width": "180px"},
                                {"if": {"column_id": "project"}, "minWidth": "160px", "width": "200px"},
                                {"if": {"column_id": "creation_date"}, "minWidth": "100px", "width": "100px"},
                            ],
                            style_header={"fontWeight": "bold"},
                        ),
                        dbc.ButtonGroup(
                            [
                                dbc.Button("Export CSV", id="export-drilldown-csv", size="sm", color="secondary", outline=True),
                                dbc.Button("Export Excel", id="export-drilldown-xlsx", size="sm", color="secondary", outline=True),
                                dbc.Button("Export PDF", id="export-drilldown-pdf", size="sm", color="secondary", outline=True),
                            ],
                            className="mt-2",
                        ),
                        dcc.Download(id="download-drilldown"),
                    ]
                ),
            ),

            dcc.Store(id="filtered-data-store"),

            # Fires periodically to re-run the chart/leaderboard callback so
            # the UI picks up new cache data (from background auto-sync or a
            # manual/cron sync) without a manual page reload. This only
            # re-reads local SQLite — it never calls Rally itself.
            dcc.Interval(
                id="auto-refresh-interval",
                interval=settings.dashboard_auto_refresh_seconds * 1000,  # ms
                n_intervals=0,
            ),
        ],
    )


app.layout = build_layout


# ==========================================================================
# Callbacks
# ==========================================================================

@app.callback(
    Output("summary-cards", "children"),
    Output("last-synced-note", "children"),
    Output("chart-split", "figure"),
    Output("chart-trend", "figure"),
    Output("chart-per-project", "figure"),
    Output("leaderboard-table", "data"),
    Input("filter-projects", "value"),
    Input("filter-programs", "value"),
    Input("filter-owners", "value"),
    Input("filter-date-range", "start_date"),
    Input("filter-date-range", "end_date"),
    Input("filter-category", "value"),
    Input("rank-mode", "value"),
    Input("min-count", "value"),
    Input("auto-refresh-interval", "n_intervals"),
)
def refresh_dashboard(projects, selected_programs, owners, date_start, date_end, category, rank_mode, min_count, _n_intervals):
    df = metrics.load_dataframe(db)

    # Program narrows to its projects (auto-expanded to include real Rally
    # sub-projects); an explicit Project pick narrows further WITHIN that
    # program (intersection, not union).
    hierarchy_rows = db.get_project_hierarchy()
    children_map = metrics.build_descendant_map(hierarchy_rows)
    program_projects = metrics.resolve_program_projects(settings.programs, selected_programs, children_map)
    effective_projects = metrics.combine_project_filters(projects, program_projects)

    filtered = metrics.apply_filters(df, effective_projects, date_start, date_end, category, owners=owners)

    # --- summary cards ---
    s = metrics.summary_cards(filtered)
    cards = [
        _summary_card("Total Test Cases", f"{s['total']:,}", "primary"),
        _summary_card("AI-Assisted (org-wide)", f"{s['ai_pct']}%", "success"),
        _summary_card(
            "Top Contributor",
            s["top_contributor"] or "—",
            "info",
            subtitle=f"{s.get('top_contributor_ai_count', 0)} AI-assisted" if s["top_contributor"] else "",
        ),
    ]

    last_sync = db.get_last_sync(settings.rally_workspace_id)
    sync_note = f"Last synced: {last_sync}" if last_sync else "Not yet synced"

    # --- AI vs Manual split (pie) ---
    if filtered.empty:
        split_fig = go.Figure()
    else:
        counts = filtered["category"].value_counts().reset_index()
        counts.columns = ["category", "count"]
        split_fig = px.pie(counts, names="category", values="count", title="AI-Assisted vs Manual",
                            hole=0.45)

    # --- trend line (org-wide) ---
    period_freq = "W" if settings.trend_period == "weekly" else "M"
    trend_df = metrics.trend_over_time(filtered, freq=period_freq)
    if trend_df.empty:
        trend_fig = go.Figure()
    else:
        trend_fig = go.Figure()
        trend_fig.add_trace(go.Bar(x=trend_df["period"], y=trend_df["total"], name="Total", opacity=0.35))
        trend_fig.add_trace(
            go.Scatter(x=trend_df["period"], y=trend_df["ai_adoption_pct"], name="AI Adoption %",
                       yaxis="y2", mode="lines+markers")
        )
        trend_fig.update_layout(
            title=f"AI Adoption Trend ({settings.trend_period})",
            yaxis=dict(title="Test Case Volume"),
            yaxis2=dict(title="AI Adoption %", overlaying="y", side="right", range=[0, 100]),
            legend=dict(orientation="h"),
        )

    # --- per-project breakdown ---
    proj_df = metrics.per_project_breakdown(filtered)
    if proj_df.empty:
        proj_fig = go.Figure()
    else:
        proj_fig = px.bar(
            proj_df, x="project", y=["ai_assisted", "manual"], barmode="stack",
            title="AI-Assisted vs Manual by Project",
            labels={"value": "Test Cases", "project": "Project"},
        )

    # --- leaderboard (with trend vs. the immediately preceding period) ---
    period_days = 7 if settings.trend_period == "weekly" else 30

    if date_start and date_end:
        cur_start = pd.Timestamp(date_start, tz="UTC")
        cur_end = pd.Timestamp(date_end, tz="UTC")
    else:
        # No date filter set: compare "last period_days" vs the period_days before that
        cur_end = pd.Timestamp.now(tz="UTC")
        cur_start = cur_end - pd.Timedelta(days=period_days)

    prev_end = cur_start
    prev_start = prev_end - (cur_end - cur_start)

    # Previous-period data respects the Project/Tag-category filters but not
    # the date filter, since we're deliberately looking at a different window.
    base = metrics.apply_filters(df, projects, None, None, category)
    previous_df = base[(base["creation_date"] >= prev_start) & (base["creation_date"] < prev_end)]

    lb = metrics.leaderboard(filtered, mode=rank_mode, min_count=min_count or 1, previous_period_df=previous_df)

    program_display = ", ".join(selected_programs) if selected_programs else "All"
    project_display = ", ".join(projects) if projects else "All"
    if not lb.empty:
        lb["program"] = program_display
        lb["project"] = project_display

    lb_records = lb.to_dict("records") if not lb.empty else []

    return cards, sync_note, split_fig, trend_fig, proj_fig, lb_records


def _summary_card(title, value, color, subtitle=""):
    return dbc.Col(
        dbc.Card(
            dbc.CardBody(
                [
                    html.Div(title, className="text-muted small"),
                    html.H3(value, className=f"text-{color}"),
                    html.Div(subtitle, className="text-muted small") if subtitle else None,
                ]
            ),
            className="shadow-sm",
        ),
        width=4,
    )


@app.callback(
    Output("leaderboard-table", "selected_rows"),
    Input("select-all-owners", "n_clicks"),
    Input("clear-all-owners", "n_clicks"),
    State("leaderboard-table", "derived_virtual_data"),
    prevent_initial_call=True,
)
def toggle_select_all(_select_clicks, _clear_clicks, rows):
    if ctx.triggered_id == "select-all-owners":
        return list(range(len(rows or [])))
    return []


@app.callback(
    Output("drilldown-title", "children"),
    Output("drilldown-table", "data"),
    Input("leaderboard-table", "derived_virtual_selected_rows"),
    State("leaderboard-table", "derived_virtual_data"),
)
def drilldown(selected_rows, table_data):
    if not selected_rows or not table_data:
        return "Select one or more owners above to drill in", []

    owners = [table_data[i]["owner"] for i in selected_rows]
    df = metrics.load_dataframe(db)
    owner_df = df[df["owner"].isin(owners)].copy()
    owner_df["tags_display"] = owner_df["tags"].apply(lambda t: ", ".join(t) if t else "")
    owner_df["creation_date"] = owner_df["creation_date"].dt.strftime("%Y-%m-%d")

    return f"Test Cases owned by Selected User/Users ({len(owner_df)})", owner_df.to_dict("records")


@app.callback(
    Output("download-leaderboard", "data"),
    Input("export-leaderboard-csv", "n_clicks"),
    Input("export-leaderboard-xlsx", "n_clicks"),
    Input("export-leaderboard-pdf", "n_clicks"),
    State("leaderboard-table", "derived_virtual_data"),
    State("leaderboard-table", "data"),
    prevent_initial_call=True,
)
def export_leaderboard(_n_csv, _n_xlsx, _n_pdf, virtual_rows, rows):
    triggered = ctx.triggered_id
    if triggered not in ("export-leaderboard-csv", "export-leaderboard-xlsx", "export-leaderboard-pdf"):
        return dash.no_update

    source_rows = virtual_rows or rows or []
    df = pd.DataFrame(source_rows)
    df = df.reindex(columns=LEADERBOARD_EXPORT_COLUMNS) if not df.empty else pd.DataFrame(columns=LEADERBOARD_EXPORT_COLUMNS)
    df.columns = LEADERBOARD_EXPORT_HEADERS

    if triggered == "export-leaderboard-csv":
        return dcc.send_data_frame(df.to_csv, "leaderboard.csv", index=False)
    if triggered == "export-leaderboard-xlsx":
        return dcc.send_data_frame(df.to_excel, "leaderboard.xlsx", index=False, sheet_name="Leaderboard")
    pdf_bytes = dataframe_to_pdf_bytes(df, "Leaderboard: AI-Efficient Test Case Creators")
    return dcc.send_bytes(lambda buf: buf.write(pdf_bytes), "leaderboard.pdf")


@app.callback(
    Output("download-drilldown", "data"),
    Input("export-drilldown-csv", "n_clicks"),
    Input("export-drilldown-xlsx", "n_clicks"),
    Input("export-drilldown-pdf", "n_clicks"),
    State("drilldown-table", "derived_virtual_data"),
    State("drilldown-table", "data"),
    prevent_initial_call=True,
)
def export_drilldown(_n_csv, _n_xlsx, _n_pdf, virtual_rows, rows):
    triggered = ctx.triggered_id
    if triggered not in ("export-drilldown-csv", "export-drilldown-xlsx", "export-drilldown-pdf"):
        return dash.no_update

    source_rows = virtual_rows or rows or []
    df = pd.DataFrame(source_rows)
    df = df.reindex(columns=DRILLDOWN_EXPORT_COLUMNS) if not df.empty else pd.DataFrame(columns=DRILLDOWN_EXPORT_COLUMNS)
    df.columns = DRILLDOWN_EXPORT_HEADERS

    if triggered == "export-drilldown-csv":
        return dcc.send_data_frame(df.to_csv, "drilldown.csv", index=False)
    if triggered == "export-drilldown-xlsx":
        return dcc.send_data_frame(df.to_excel, "drilldown.xlsx", index=False, sheet_name="Test Cases")
    pdf_bytes = dataframe_to_pdf_bytes(df, "Drill-down: Individual Test Cases")
    return dcc.send_bytes(lambda buf: buf.write(pdf_bytes), "drilldown.pdf")


if __name__ == "__main__":
    settings.validate()
    app.run(
        host=os.getenv("DASH_HOST", "0.0.0.0"),
        port=int(os.getenv("DASH_PORT", "8050")),
        debug=os.getenv("DASH_DEBUG", "false").lower() == "true",
    )