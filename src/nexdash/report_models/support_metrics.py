"""Support metrics: volume, first response, resolution, sentiment, deflection and CSAT.

Rows come from help desk tickets in the shape of the Support Intelligence Engine
``Ticket`` model (``id``, ``source``, ``status``, ``priority``, ``product``, ``tags``,
``created_at``, ``solved_at``, ``csat`` from 0 to 1, ``comments`` and ``extra``).
Plain dicts with the same keys work too. ``support_rows_from_tickets`` derives:

- first response: minutes until the first public comment by an agent;
- resolution: status ``solved`` or ``closed``, and hours until ``solved_at``;
- deflection: ``extra["deflected"]``, a ``deflected`` tag, or a ticket resolved
  without any agent comment (answered by self-service or a bot);
- sentiment: ``extra["sentiment"]`` and/or ``extra["sentiment_score"]`` (-1 to 1).
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable, Iterable, Mapping
from typing import Any

import sqlalchemy as sa

from nexdash.cube import Cube
from nexdash.plan import PlanOrderBy, QueryPlan
from nexdash.report_models.base import Panel, ReportModel

TABLE_NAME = "support_conversations"
UNASSIGNED = "unassigned"
RESOLVED_STATUSES = frozenset({"solved", "closed", "resolved"})
SENTIMENTS: tuple[str, ...] = ("negative", "neutral", "positive", "unknown")
SENTIMENT_THRESHOLD = 0.25

_metadata = sa.MetaData()

TABLE = sa.Table(
    TABLE_NAME,
    _metadata,
    sa.Column("ticket_id", sa.String(200)),
    sa.Column("source", sa.String(64)),
    sa.Column("project", sa.String(200)),
    sa.Column("team", sa.String(200)),
    sa.Column("channel", sa.String(64)),
    sa.Column("priority", sa.String(32)),
    sa.Column("status", sa.String(32)),
    sa.Column("opened_date", sa.String(10)),
    sa.Column("opened_month", sa.String(7)),
    sa.Column("resolved", sa.String(3)),
    sa.Column("resolved_flag", sa.Integer),
    sa.Column("deflected", sa.String(3)),
    sa.Column("deflected_flag", sa.Integer),
    sa.Column("sentiment", sa.String(16)),
    sa.Column("sentiment_rank", sa.Integer),
    sa.Column("sentiment_score", sa.Float),
    sa.Column("first_response_minutes", sa.Float),
    sa.Column("resolution_hours", sa.Float),
    sa.Column("csat", sa.Float),
)

CUBE = Cube.model_validate(
    {
        "name": "support_metrics",
        "source": TABLE_NAME,
        "description": "Support tickets and conversations, one row per ticket",
        "dimensions": [
            {"name": "ticket_id", "description": "Ticket identifier in the help desk"},
            {"name": "source", "description": "Help desk connector (zendesk, freshdesk, csv, ...)"},
            {"name": "project", "description": "Product or project the ticket is about"},
            {"name": "team", "description": "Support team"},
            {"name": "channel", "description": "Contact channel (email, chat, phone, web)"},
            {"name": "priority", "description": "Ticket priority"},
            {"name": "status", "description": "Ticket status"},
            {"name": "opened_date", "type": "date", "description": "Date the ticket was opened (YYYY-MM-DD)"},
            {"name": "opened_month", "description": "Month the ticket was opened (YYYY-MM)"},
            {"name": "resolved", "description": "yes when solved or closed"},
            {"name": "deflected", "description": "yes when resolved without a human agent"},
            {"name": "sentiment", "description": "negative, neutral, positive or unknown"},
            {
                "name": "sentiment_rank",
                "type": "integer",
                "description": "1 negative, 2 neutral, 3 positive, 4 unknown",
            },
        ],
        "metrics": [
            {"name": "ticket_count", "aggregation": "count", "description": "Number of tickets"},
            {
                "name": "avg_first_response_minutes",
                "aggregation": "avg",
                "column": "first_response_minutes",
                "description": "Average minutes to the first agent reply",
            },
            {
                "name": "avg_resolution_hours",
                "aggregation": "avg",
                "column": "resolution_hours",
                "description": "Average hours to resolve",
            },
            {
                "name": "resolution_rate",
                "aggregation": "avg",
                "column": "resolved_flag",
                "description": "Share of tickets resolved (0 to 1)",
            },
            {
                "name": "deflection_rate",
                "aggregation": "avg",
                "column": "deflected_flag",
                "description": "Share of tickets resolved without a human agent (0 to 1)",
            },
            {
                "name": "avg_csat",
                "aggregation": "avg",
                "column": "csat",
                "description": "Average satisfaction from 0 to 1",
            },
            {
                "name": "avg_sentiment_score",
                "aggregation": "avg",
                "column": "sentiment_score",
                "description": "Average sentiment score from -1 to 1",
            },
        ],
    }
)


def _plan(**kwargs: Any) -> QueryPlan:
    return QueryPlan(cube=CUBE.name, **kwargs)


def _monthly(metric: str) -> QueryPlan:
    return _plan(metrics=[metric], dimensions=["opened_month"], order_by=[PlanOrderBy(field="opened_month")])


PANELS: tuple[Panel, ...] = (
    Panel(
        id="volume",
        kind="kpi",
        title="Tickets opened",
        plan=_plan(metrics=["ticket_count"]),
        y="ticket_count",
    ),
    Panel(
        id="first_response",
        kind="kpi",
        title="Time to first response",
        plan=_plan(metrics=["avg_first_response_minutes"]),
        y="avg_first_response_minutes",
        unit="minutes",
        lower_is_better=True,
    ),
    Panel(
        id="resolution_rate",
        kind="kpi",
        title="Resolution rate",
        plan=_plan(metrics=["resolution_rate"]),
        y="resolution_rate",
        unit="percent",
    ),
    Panel(
        id="deflection_rate",
        kind="kpi",
        title="Deflection rate",
        plan=_plan(metrics=["deflection_rate"]),
        y="deflection_rate",
        unit="percent",
        description="Tickets resolved without a human agent",
    ),
    Panel(
        id="csat",
        kind="kpi",
        title="Customer satisfaction (CSAT)",
        plan=_plan(metrics=["avg_csat"]),
        y="avg_csat",
        unit="percent",
    ),
    Panel(
        id="volume_trend",
        kind="line",
        title="Tickets opened per month",
        plan=_monthly("ticket_count"),
        x="opened_month",
        y="ticket_count",
    ),
    Panel(
        id="first_response_trend",
        kind="line",
        title="Average time to first response per month",
        plan=_monthly("avg_first_response_minutes"),
        x="opened_month",
        y="avg_first_response_minutes",
        unit="minutes",
        lower_is_better=True,
    ),
    Panel(
        id="csat_trend",
        kind="line",
        title="Customer satisfaction per month",
        plan=_monthly("avg_csat"),
        x="opened_month",
        y="avg_csat",
        unit="percent",
    ),
    Panel(
        id="sentiment",
        kind="bar",
        title="Tickets by customer sentiment",
        plan=_plan(
            metrics=["ticket_count"],
            dimensions=["sentiment_rank", "sentiment"],
            order_by=[PlanOrderBy(field="sentiment_rank")],
        ),
        x="sentiment",
        y="ticket_count",
        keep_order=True,
        highlight="negative",
    ),
    Panel(
        id="resolution_by_channel",
        kind="bar",
        title="Average hours to resolve per channel",
        plan=_plan(
            metrics=["avg_resolution_hours"],
            dimensions=["channel"],
            order_by=[PlanOrderBy(field="avg_resolution_hours", direction="desc")],
            limit=10,
        ),
        x="channel",
        y="avg_resolution_hours",
        unit="hours",
        lower_is_better=True,
    ),
)

MODEL = ReportModel(
    name="support_metrics",
    title="Support metrics",
    description="Ticket volume, response and resolution times, sentiment, deflection and satisfaction.",
    cube=CUBE,
    table=TABLE,
    panels=PANELS,
    period_field="opened_date",
    source_note="help desk tickets collected by the Support Engine connectors",
)


def _as_mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        return dict(dump(mode="python"))
    raise TypeError(f"Expected a mapping or a pydantic model, got {type(value).__name__}")


def _parse_time(value: Any) -> dt.datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, dt.datetime):
        parsed = value
    elif isinstance(value, dt.date):
        parsed = dt.datetime(value.year, value.month, value.day)
    else:
        text = str(value).strip()
        if text.endswith(("Z", "z")):
            text = text[:-1] + "+00:00"
        parsed = dt.datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.UTC)
    return parsed.astimezone(dt.UTC)


def _flag(value: Any) -> bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "y"}:
        return True
    if text in {"0", "false", "no", "n"}:
        return False
    return None


def _sentiment(extra: Mapping[str, Any]) -> tuple[str, float | None]:
    raw_score = extra.get("sentiment_score")
    score = None if raw_score is None else max(-1.0, min(float(raw_score), 1.0))
    label = str(extra.get("sentiment") or "").strip().lower()
    if label not in SENTIMENTS or label == "unknown":
        if score is None:
            label = "unknown"
        elif score >= SENTIMENT_THRESHOLD:
            label = "positive"
        elif score <= -SENTIMENT_THRESHOLD:
            label = "negative"
        else:
            label = "neutral"
    return label, score


def support_rows_from_tickets(
    tickets: Iterable[Any],
    *,
    team_of: Mapping[str, str] | Callable[[str], str | None] | None = None,
) -> list[dict[str, Any]]:
    """Turn help desk tickets into ``support_conversations`` rows.

    ``team_of`` maps a product to a support team when ``extra["team"]`` is absent.
    """
    rows: list[dict[str, Any]] = []
    for raw in tickets:
        ticket = _as_mapping(raw)
        extra = dict(ticket.get("extra") or {})
        created = _parse_time(ticket.get("created_at"))
        solved = _parse_time(ticket.get("solved_at"))
        status = str(getattr(ticket.get("status"), "value", ticket.get("status")) or "open").strip().lower()
        resolved = status in RESOLVED_STATUSES
        project = str(ticket.get("product") or extra.get("project") or UNASSIGNED)
        team = extra.get("team")
        if not team and team_of is not None:
            team = team_of(project) if callable(team_of) else team_of.get(project)

        agent_times = []
        for comment in ticket.get("comments") or []:
            data = _as_mapping(comment)
            if str(data.get("author_role") or "").lower() == "agent" and data.get("public", True):
                when = _parse_time(data.get("created_at"))
                if when is not None:
                    agent_times.append(when)
        first_agent = min(agent_times) if agent_times else None
        first_response = (
            round((first_agent - created).total_seconds() / 60.0, 1)
            if first_agent is not None and created is not None and first_agent >= created
            else None
        )
        resolution = (
            round((solved - created).total_seconds() / 3600.0, 2)
            if resolved and solved is not None and created is not None and solved >= created
            else None
        )

        tags = {str(tag).strip().lower() for tag in ticket.get("tags") or []}
        deflected = _flag(extra.get("deflected"))
        if deflected is None:
            deflected = "deflected" in tags or (resolved and first_agent is None)
        deflected = bool(deflected and resolved)

        sentiment, score = _sentiment(extra)
        csat = ticket.get("csat")
        rows.append(
            {
                "ticket_id": str(ticket.get("id") or ""),
                "source": str(ticket.get("source") or UNASSIGNED),
                "project": project,
                "team": str(team) if team else UNASSIGNED,
                "channel": str(extra.get("channel") or ticket.get("source") or UNASSIGNED),
                "priority": str(ticket.get("priority") or "none"),
                "status": status,
                "opened_date": created.date().isoformat() if created else None,
                "opened_month": created.strftime("%Y-%m") if created else None,
                "resolved": "yes" if resolved else "no",
                "resolved_flag": int(resolved),
                "deflected": "yes" if deflected else "no",
                "deflected_flag": int(deflected),
                "sentiment": sentiment,
                "sentiment_rank": SENTIMENTS.index(sentiment) + 1,
                "sentiment_score": score,
                "first_response_minutes": first_response,
                "resolution_hours": resolution,
                "csat": None if csat is None else max(0.0, min(float(csat), 1.0)),
            }
        )
    return rows
