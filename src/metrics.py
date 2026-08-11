"""
Aggregation / metrics layer.

Everything here operates on a pandas DataFrame shaped like the `test_cases`
SQLite table (see db.py). Keeping this pure-function / DataFrame-in-
DataFrame-out means it's independently unit-testable and reusable from
any UI (Dash here, but equally usable from a notebook, a scheduled report,
or a different BI front end).
"""

from __future__ import annotations

import json

import pandas as pd


def load_dataframe(db) -> pd.DataFrame:
    rows = db.fetch_all()
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    df["tags"] = df["tags_json"].apply(lambda s: json.loads(s) if s else [])
    df["creation_date"] = pd.to_datetime(df["creation_date"], utc=True, errors="coerce")
    df["last_update_date"] = pd.to_datetime(df["last_update_date"], utc=True, errors="coerce")
    df["is_ai_assisted"] = df["category"] == "AI-Assisted"
    return df


def apply_filters(
    df: pd.DataFrame,
    projects: list[str] | None = None,
    date_start=None,
    date_end=None,
    tag_category: str | None = None,
    owners: list[str] | None = None,
) -> pd.DataFrame:
    if df.empty:
        return df
    out = df
    if projects:
        out = out[out["project"].isin(projects)]
    if owners:
        out = out[out["owner"].isin(owners)]
    if date_start is not None:
        out = out[out["creation_date"] >= pd.Timestamp(date_start, tz="UTC")]
    if date_end is not None:
        # date_end arrives as a bare date (midnight); push to the end of that
        # day so test cases created any time on the end date are included.
        end_of_day = pd.Timestamp(date_end, tz="UTC") + pd.Timedelta(days=1) - pd.Timedelta(microseconds=1)
        out = out[out["creation_date"] <= end_of_day]
    if tag_category and tag_category != "All":
        out = out[out["category"] == tag_category]
    return out
# ----------------------------------------------------------------------
# Per-Program metrics
# ----------------------------------------------------------------------

def build_descendant_map(hierarchy_rows: list[dict]) -> dict[str, list[str]]:
    """hierarchy_rows: [{'name': ..., 'parent_name': ...}, ...] (from
    db.get_project_hierarchy()) -> {parent_name: [child_name, ...]}"""
    children: dict[str, list[str]] = {}
    for row in hierarchy_rows:
        parent = row.get("parent_name")
        if parent:
            children.setdefault(parent, []).append(row["name"])
    return children


def expand_with_descendants(project_name: str, children_map: dict[str, list[str]]) -> list[str]:
    """Return project_name plus every descendant at any depth, per Rally's
    ACTUAL parent/child project hierarchy — not the config.yaml grouping."""
    result = [project_name]
    seen = {project_name}
    queue = list(children_map.get(project_name, []))
    while queue:
        node = queue.pop()
        if node in seen:
            continue
        seen.add(node)
        result.append(node)
        queue.extend(children_map.get(node, []))
    return result


def resolve_program_projects(
    programs: dict[str, list[str]],
    selected_programs: list[str] | None,
    children_map: dict[str, list[str]] | None = None,
) -> list[str]:
    """Expand selected program names into every project they contain,
    including any real Rally sub-projects beneath each listed project
    (per children_map), not just the literal names typed into config.yaml."""
    if not selected_programs:
        return []
    children_map = children_map or {}
    result: set[str] = set()
    for prog in selected_programs:
        for proj in programs.get(prog, []):
            result.update(expand_with_descendants(proj, children_map))
    return sorted(result)


def combine_project_filters(
    explicit_projects: list[str] | None,
    program_projects: list[str] | None,
) -> list[str] | None:
    """
    Combine the Project(s) dropdown and the Program(s) dropdown so Program
    acts as a scope and Project narrows further WITHIN that scope —
    intersection, not union.

    - Program only  -> every project in that program
    - Project only  -> exactly those projects
    - Both selected -> only the selected projects that also belong to the
                        selected program(s); e.g. Program=Claims (4 projects)
                        + Project=CC Core Blue -> CC Core Blue only
    - If both are selected but share nothing in common (the chosen project
      isn't actually part of the chosen program), fall back to the explicit
      project selection rather than silently returning "no results" —
      the person's Project pick is the more specific, more deliberate signal.
    - Neither selected -> None (no project filter, i.e. everything)
    """
    explicit = set(explicit_projects) if explicit_projects else None
    program = set(program_projects) if program_projects else None

    if explicit and program:
        overlap = explicit & program
        return sorted(overlap) if overlap else sorted(explicit)
    if program:
        return sorted(program)
    if explicit:
        return sorted(explicit)
    return None

# ----------------------------------------------------------------------
# Per-owner metrics
# ----------------------------------------------------------------------
def per_owner_metrics(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame(
            columns=["owner", "total", "ai_assisted", "manual", "unclassified", "ai_adoption_pct"]
        )

    grouped = df.groupby("owner").agg(
        total=("formatted_id", "count"),
        ai_assisted=("is_ai_assisted", "sum"),
        manual=("category", lambda s: (s == "Manual").sum()),
        unclassified=("category", lambda s: (s == "Unclassified").sum()),
    ).reset_index()

    grouped["ai_adoption_pct"] = (grouped["ai_assisted"] / grouped["total"] * 100).round(1)
    return grouped.sort_values("total", ascending=False)


# ----------------------------------------------------------------------
# Leaderboard
# ----------------------------------------------------------------------
def leaderboard(
    df: pd.DataFrame,
    mode: str = "pct",              # "pct" or "count"
    min_count: int = 10,
    previous_period_df: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """
    Returns columns: rank, owner, total, ai_assisted, manual, ai_adoption_pct, trend
    `trend` is "up" / "down" / "flat" / None (None when no prior-period data
    exists for that owner, e.g. a brand-new contributor).
    """
    metrics = per_owner_metrics(df)
    qualifying = metrics[metrics["total"] >= min_count].copy()

    sort_col = "ai_adoption_pct" if mode == "pct" else "ai_assisted"
    qualifying = qualifying.sort_values(sort_col, ascending=False).reset_index(drop=True)
    qualifying.insert(0, "rank", qualifying.index + 1)

    if previous_period_df is not None and not previous_period_df.empty:
        prev_metrics = per_owner_metrics(previous_period_df).set_index("owner")
        trends = []
        for _, row in qualifying.iterrows():
            prev = prev_metrics.loc[row["owner"]] if row["owner"] in prev_metrics.index else None
            if prev is None:
                trends.append(None)
                continue
            cur_val = row["ai_adoption_pct"] if mode == "pct" else row["ai_assisted"]
            prev_val = prev["ai_adoption_pct"] if mode == "pct" else prev["ai_assisted"]
            if cur_val > prev_val:
                trends.append("up")
            elif cur_val < prev_val:
                trends.append("down")
            else:
                trends.append("flat")
        qualifying["trend"] = trends
    else:
        qualifying["trend"] = None

    return qualifying


# ----------------------------------------------------------------------
# Trend over time (org-wide or per-owner)
# ----------------------------------------------------------------------
def trend_over_time(df: pd.DataFrame, freq: str = "W", owner: str | None = None) -> pd.DataFrame:
    """freq: 'W' weekly, 'M' monthly (pandas offset alias)."""
    if df.empty:
        return pd.DataFrame(columns=["period", "total", "ai_assisted", "ai_adoption_pct"])

    d = df if owner is None else df[df["owner"] == owner]
    if d.empty:
        return pd.DataFrame(columns=["period", "total", "ai_assisted", "ai_adoption_pct"])

    d = d.dropna(subset=["creation_date"]).copy()
    d["period"] = d["creation_date"].dt.tz_convert(None).dt.to_period(freq).dt.start_time

    grouped = d.groupby("period").agg(
        total=("formatted_id", "count"),
        ai_assisted=("is_ai_assisted", "sum"),
    ).reset_index()
    grouped["ai_adoption_pct"] = (grouped["ai_assisted"] / grouped["total"] * 100).round(1)
    return grouped.sort_values("period")


# ----------------------------------------------------------------------
# Per-project breakdown
# ----------------------------------------------------------------------
def per_project_breakdown(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame(columns=["project", "total", "ai_assisted", "manual", "ai_adoption_pct"])

    grouped = df.groupby("project").agg(
        total=("formatted_id", "count"),
        ai_assisted=("is_ai_assisted", "sum"),
        manual=("category", lambda s: (s == "Manual").sum()),
    ).reset_index()
    grouped["ai_adoption_pct"] = (grouped["ai_assisted"] / grouped["total"] * 100).round(1)
    return grouped.sort_values("total", ascending=False)


# ----------------------------------------------------------------------
# Summary cards
# ----------------------------------------------------------------------
def summary_cards(df: pd.DataFrame) -> dict:
    if df.empty:
        return {"total": 0, "ai_pct": 0.0, "top_contributor": None}

    total = len(df)
    ai_pct = round(df["is_ai_assisted"].sum() / total * 100, 1)

    owner_metrics = per_owner_metrics(df)
    top = owner_metrics.sort_values("ai_assisted", ascending=False).iloc[0] if not owner_metrics.empty else None

    return {
        "total": total,
        "ai_pct": ai_pct,
        "top_contributor": top["owner"] if top is not None else None,
        "top_contributor_ai_count": int(top["ai_assisted"]) if top is not None else 0,
    }
