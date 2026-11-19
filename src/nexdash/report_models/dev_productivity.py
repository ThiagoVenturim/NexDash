"""Development productivity: pull requests, review time, lead time and throughput.

Rows come from the connectors (GitHub, Azure DevOps, Redmine, Jira, generic REST):
``dev_rows_from_work_items`` turns normalized ``WorkItem`` objects into rows of the
``dev_activity`` table, adding the team of each person and, when known, the time to
first review of each pull request.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from typing import Any

import sqlalchemy as sa

from nexdash.connectors.models import WorkItem
from nexdash.cube import Cube
from nexdash.plan import PlanFilter, PlanOrderBy, QueryPlan
from nexdash.report_models.base import Panel, ReportModel

TABLE_NAME = "dev_activity"
UNASSIGNED = "unassigned"
PULL_REQUEST_TYPES = frozenset({"pull_request", "pullrequest", "pr", "merge_request"})
MERGED_STATUSES = frozenset({"merged", "completed"})

_metadata = sa.MetaData()

TABLE = sa.Table(
    TABLE_NAME,
    _metadata,
    sa.Column("source", sa.String(32)),
    sa.Column("project", sa.String(200)),
    sa.Column("team", sa.String(200)),
    sa.Column("person", sa.String(200)),
    sa.Column("item_key", sa.String(200)),
    sa.Column("item_type", sa.String(32)),
    sa.Column("state", sa.String(16)),
    sa.Column("merged", sa.String(3)),
    sa.Column("delivered", sa.String(3)),
    sa.Column("created_date", sa.String(10)),
    sa.Column("created_month", sa.String(7)),
    sa.Column("closed_date", sa.String(10)),
    sa.Column("closed_month", sa.String(7)),
    sa.Column("review_hours", sa.Float),
    sa.Column("lead_time_hours", sa.Float),
)

CUBE = Cube.model_validate(
    {
        "name": "dev_productivity",
        "source": TABLE_NAME,
        "description": "Pull requests and issues from the connectors, one row per item",
        "dimensions": [
            {"name": "source", "description": "Connector that produced the item"},
            {"name": "project", "description": "Repository or project"},
            {"name": "team", "description": "Team of the person"},
            {"name": "person", "description": "Author of a pull request or assignee of an issue"},
            {"name": "item_type", "description": "pull_request or issue"},
            {"name": "state", "description": "open or closed"},
            {"name": "merged", "description": "yes when a pull request was merged"},
            {"name": "delivered", "description": "yes for merged pull requests and closed issues"},
            {"name": "created_date", "type": "date", "description": "Creation date (YYYY-MM-DD)"},
            {"name": "created_month", "description": "Creation month (YYYY-MM)"},
            {"name": "closed_date", "type": "date", "description": "Close or merge date (YYYY-MM-DD)"},
            {"name": "closed_month", "description": "Close or merge month (YYYY-MM)"},
        ],
        "metrics": [
            {"name": "item_count", "aggregation": "count", "description": "Number of items"},
            {
                "name": "person_count",
                "aggregation": "count_distinct",
                "column": "person",
                "description": "Distinct people",
            },
            {
                "name": "avg_review_hours",
                "aggregation": "avg",
                "column": "review_hours",
                "description": "Average hours from opening a pull request to its first review",
            },
            {
                "name": "avg_lead_time_hours",
                "aggregation": "avg",
                "column": "lead_time_hours",
                "description": "Average hours from creation to close or merge",
            },
            {
                "name": "max_lead_time_hours",
                "aggregation": "max",
                "column": "lead_time_hours",
                "description": "Longest lead time in hours",
            },
        ],
    }
)

_PR = PlanFilter(field="item_type", operator="eq", value="pull_request")
_ISSUE = PlanFilter(field="item_type", operator="eq", value="issue")
_MERGED = PlanFilter(field="merged", operator="eq", value="yes")
_DELIVERED = PlanFilter(field="delivered", operator="eq", value="yes")


def _plan(**kwargs: Any) -> QueryPlan:
    return QueryPlan(cube=CUBE.name, **kwargs)


PANELS: tuple[Panel, ...] = (
    Panel(
        id="prs_merged",
        kind="kpi",
        title="Pull requests merged",
        plan=_plan(metrics=["item_count"], filters=[_PR, _MERGED]),
        y="item_count",
        period_field="closed_date",
    ),
    Panel(
        id="prs_open",
        kind="kpi",
        title="Pull requests still open",
        plan=_plan(metrics=["item_count"], filters=[_PR, PlanFilter(field="state", operator="eq", value="open")]),
        y="item_count",
        better="lower",
        description="Opened in the period and not closed yet",
    ),
    Panel(
        id="review_time",
        kind="kpi",
        title="Time to first review",
        plan=_plan(metrics=["avg_review_hours"], filters=[_PR]),
        y="avg_review_hours",
        unit="hours",
        better="lower",
    ),
    Panel(
        id="lead_time",
        kind="kpi",
        title="Pull request lead time",
        plan=_plan(metrics=["avg_lead_time_hours"], filters=[_PR, _MERGED]),
        y="avg_lead_time_hours",
        unit="hours",
        period_field="closed_date",
        better="lower",
        description="Hours from opening to merge",
    ),
    Panel(
        id="issues_closed",
        kind="kpi",
        title="Issues closed",
        plan=_plan(metrics=["item_count"], filters=[_ISSUE, PlanFilter(field="state", operator="eq", value="closed")]),
        y="item_count",
        period_field="closed_date",
    ),
    Panel(
        id="merged_trend",
        kind="line",
        title="Pull requests merged per month",
        plan=_plan(
            metrics=["item_count"],
            dimensions=["closed_month"],
            filters=[_PR, _MERGED],
            order_by=[PlanOrderBy(field="closed_month")],
        ),
        x="closed_month",
        y="item_count",
        period_field="closed_date",
    ),
    Panel(
        id="lead_time_trend",
        kind="line",
        title="Average pull request lead time per month",
        plan=_plan(
            metrics=["avg_lead_time_hours"],
            dimensions=["closed_month"],
            filters=[_PR, _MERGED],
            order_by=[PlanOrderBy(field="closed_month")],
        ),
        x="closed_month",
        y="avg_lead_time_hours",
        unit="hours",
        period_field="closed_date",
        better="lower",
    ),
    Panel(
        id="throughput_person",
        kind="bar",
        title="Items delivered per person (merged pull requests and closed issues)",
        plan=_plan(
            metrics=["item_count"],
            dimensions=["person"],
            filters=[_DELIVERED],
            order_by=[PlanOrderBy(field="item_count", direction="desc")],
            limit=10,
        ),
        x="person",
        y="item_count",
        period_field="closed_date",
    ),
    Panel(
        id="throughput_team",
        kind="bar",
        title="Items delivered per team",
        plan=_plan(
            metrics=["item_count"],
            dimensions=["team"],
            filters=[_DELIVERED],
            order_by=[PlanOrderBy(field="item_count", direction="desc")],
            limit=10,
        ),
        x="team",
        y="item_count",
        period_field="closed_date",
    ),
)

MODEL = ReportModel(
    name="dev_productivity",
    title="Development productivity",
    description="Pull requests, review and lead time, issues closed and throughput per person and team.",
    cube=CUBE,
    table=TABLE,
    panels=PANELS,
    period_field="created_date",
    source_note="work items collected by the nexdash connectors",
)

TeamLookup = Mapping[str, str] | Callable[[str], str | None] | None


def _team(person: str, team_of: TeamLookup) -> str:
    if team_of is None:
        return UNASSIGNED
    team = team_of(person) if callable(team_of) else team_of.get(person)
    return str(team) if team else UNASSIGNED


def _is_pull_request(item: WorkItem) -> bool:
    return item.item_type.strip().lower().replace(" ", "_").replace("-", "_") in PULL_REQUEST_TYPES


def _is_merged(item: WorkItem, merged_keys: set[str]) -> bool:
    if item.item_key in merged_keys or item.item_id in merged_keys:
        return True
    if item.state != "closed":
        return False
    labels = {label.strip().lower() for label in item.labels.split(",") if label.strip()}
    return item.status.strip().lower() in MERGED_STATUSES or "merged" in labels


def dev_rows_from_work_items(
    items: Iterable[WorkItem],
    *,
    team_of: TeamLookup = None,
    review_hours: Mapping[str, float] | None = None,
    merged_keys: Iterable[str] | None = None,
) -> list[dict[str, Any]]:
    """Turn connector work items into ``dev_activity`` rows.

    - ``team_of`` maps a person (login or name) to a team, as a dict or a function.
    - ``review_hours`` maps an item key or id to the hours until its first review.
    - A pull request counts as merged when it is closed with status ``merged`` or
      ``completed`` (Azure DevOps), carries a ``merged`` label, or is listed in
      ``merged_keys``. The GitHub issues API does not say whether a closed pull
      request was merged, so pass ``merged_keys`` for GitHub data.
    """
    reviews = dict(review_hours or {})
    merged_set = {str(key) for key in (merged_keys or [])}
    rows: list[dict[str, Any]] = []
    for item in items:
        is_pr = _is_pull_request(item)
        person = (item.author or item.assignee) if is_pr else (item.assignee or item.author)
        person = person or UNASSIGNED
        merged = is_pr and _is_merged(item, merged_set)
        closed = item.state == "closed"
        delivered = merged if is_pr else closed
        review = reviews.get(item.item_key, reviews.get(item.item_id)) if is_pr else None
        rows.append(
            {
                "source": item.source,
                "project": item.project,
                "team": _team(person, team_of),
                "person": person,
                "item_key": item.item_key,
                "item_type": "pull_request" if is_pr else "issue",
                "state": item.state,
                "merged": "yes" if merged else "no",
                "delivered": "yes" if delivered else "no",
                "created_date": item.created_at[:10] if item.created_at else None,
                "created_month": item.created_at[:7] if item.created_at else None,
                "closed_date": item.closed_at[:10] if closed and item.closed_at else None,
                "closed_month": item.closed_at[:7] if closed and item.closed_at else None,
                "review_hours": None if review is None else float(review),
                "lead_time_hours": item.resolution_hours if closed else None,
            }
        )
    return rows
