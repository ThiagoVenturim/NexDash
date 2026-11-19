"""Security posture: open findings by severity, trend, gate coverage and outdated dependencies.

Rows come from security posture snapshots, one per project and period, in the shape of
the security package of the nexcoder platform (``SecurityPostureMetrics``): ``project``,
``period``, ``severity_counts``, ``outdated_dependencies``, ``gate_coverage_pct`` and
``gate_passed``. Plain dicts with the same keys work too.

Each snapshot becomes one row per severity in ``security_snapshots``. Current-state
panels read the latest snapshot date in the period; record every project on the same
snapshot date (for example one snapshot per project per week or month).
"""

from __future__ import annotations

import datetime as dt
import re
from collections.abc import Callable, Iterable, Mapping
from typing import Any

import sqlalchemy as sa

from nexdash.cube import Cube
from nexdash.plan import PlanFilter, PlanOrderBy, QueryPlan
from nexdash.report_models.base import SNAPSHOT_METRIC, Panel, ReportModel

TABLE_NAME = "security_snapshots"
SEVERITIES: tuple[str, ...] = ("critical", "high", "medium", "low", "info")
UNASSIGNED = "unassigned"

_metadata = sa.MetaData()

TABLE = sa.Table(
    TABLE_NAME,
    _metadata,
    sa.Column("project", sa.String(200)),
    sa.Column("team", sa.String(200)),
    sa.Column("period", sa.String(32)),
    sa.Column("snapshot_date", sa.String(10)),
    sa.Column("month", sa.String(7)),
    sa.Column("severity", sa.String(16)),
    sa.Column("severity_rank", sa.Integer),
    sa.Column("open_findings", sa.Integer),
    sa.Column("outdated_dependencies", sa.Integer),
    sa.Column("gate_coverage", sa.Float),
    sa.Column("gate_passed", sa.Integer),
)

CUBE = Cube.model_validate(
    {
        "name": "security_posture",
        "source": TABLE_NAME,
        "description": "Security posture snapshots, one row per project, snapshot and severity",
        "dimensions": [
            {"name": "project", "description": "Project or repository"},
            {"name": "team", "description": "Team that owns the project"},
            {"name": "period", "description": "Period label of the snapshot (2026-03, 2026-W12, 2026-Q1)"},
            {"name": "snapshot_date", "type": "date", "description": "Date of the snapshot (YYYY-MM-DD)"},
            {"name": "month", "description": "Month of the snapshot (YYYY-MM)"},
            {"name": "severity", "description": "critical, high, medium, low or info"},
            {"name": "severity_rank", "type": "integer", "description": "1 for critical up to 5 for info"},
        ],
        "metrics": [
            {
                "name": "open_findings",
                "aggregation": "sum",
                "column": "open_findings",
                "description": "Open (not suppressed) findings",
            },
            {
                "name": "outdated_dependencies",
                "aggregation": "sum",
                "column": "outdated_dependencies",
                "description": "Dependencies that need an upgrade",
            },
            {
                "name": "avg_gate_coverage",
                "aggregation": "avg",
                "column": "gate_coverage",
                "description": "Share of expected scanners the security gate ran (0 to 1)",
            },
            {
                "name": "gate_pass_rate",
                "aggregation": "avg",
                "column": "gate_passed",
                "description": "Share of snapshots whose gate passed (0 to 1)",
            },
            {
                "name": "project_count",
                "aggregation": "count_distinct",
                "column": "project",
                "description": "Distinct projects",
            },
            {
                "name": SNAPSHOT_METRIC,
                "aggregation": "max",
                "column": "snapshot_date",
                "description": "Most recent snapshot date",
            },
        ],
    }
)

_SEVERE = PlanFilter(field="severity", operator="in", value=["critical", "high"])


def _plan(**kwargs: Any) -> QueryPlan:
    return QueryPlan(cube=CUBE.name, **kwargs)


PANELS: tuple[Panel, ...] = (
    Panel(
        id="severe_open",
        kind="kpi",
        title="Open critical and high findings",
        plan=_plan(metrics=["open_findings"], filters=[_SEVERE]),
        y="open_findings",
        latest_snapshot=True,
        better="lower",
    ),
    Panel(
        id="open_total",
        kind="kpi",
        title="Open findings, all severities",
        plan=_plan(metrics=["open_findings"]),
        y="open_findings",
        latest_snapshot=True,
        better="lower",
    ),
    Panel(
        id="gate_coverage",
        kind="kpi",
        title="Security gate coverage",
        plan=_plan(metrics=["avg_gate_coverage"]),
        y="avg_gate_coverage",
        unit="percent",
        latest_snapshot=True,
        description="Share of the expected scanners that the gate ran",
    ),
    Panel(
        id="outdated_dependencies",
        kind="kpi",
        title="Outdated dependencies",
        plan=_plan(metrics=["outdated_dependencies"]),
        y="outdated_dependencies",
        latest_snapshot=True,
        better="lower",
    ),
    Panel(
        id="gate_pass_rate",
        kind="kpi",
        title="Gate pass rate in the period",
        plan=_plan(metrics=["gate_pass_rate"]),
        y="gate_pass_rate",
        unit="percent",
    ),
    Panel(
        id="by_severity",
        kind="bar",
        title="Open findings by severity, latest snapshot",
        plan=_plan(
            metrics=["open_findings"],
            dimensions=["severity_rank", "severity"],
            order_by=[PlanOrderBy(field="severity_rank")],
        ),
        x="severity",
        y="open_findings",
        latest_snapshot=True,
        keep_order=True,
        highlight="critical",
        better="lower",
    ),
    Panel(
        id="severe_trend",
        kind="line",
        title="Open critical and high findings per snapshot",
        plan=_plan(
            metrics=["open_findings"],
            dimensions=["snapshot_date"],
            filters=[_SEVERE],
            order_by=[PlanOrderBy(field="snapshot_date")],
        ),
        x="snapshot_date",
        y="open_findings",
        better="lower",
    ),
    Panel(
        id="coverage_trend",
        kind="line",
        title="Security gate coverage per snapshot",
        plan=_plan(
            metrics=["avg_gate_coverage"],
            dimensions=["snapshot_date"],
            order_by=[PlanOrderBy(field="snapshot_date")],
        ),
        x="snapshot_date",
        y="avg_gate_coverage",
        unit="percent",
    ),
    Panel(
        id="severe_by_project",
        kind="bar",
        title="Open critical and high findings per project, latest snapshot",
        plan=_plan(
            metrics=["open_findings"],
            dimensions=["project"],
            filters=[_SEVERE],
            order_by=[PlanOrderBy(field="open_findings", direction="desc")],
            limit=10,
        ),
        x="project",
        y="open_findings",
        latest_snapshot=True,
        better="lower",
    ),
)

MODEL = ReportModel(
    name="security_posture",
    title="Security posture",
    description="Open vulnerabilities by severity, their trend, security gate coverage and outdated dependencies.",
    cube=CUBE,
    table=TABLE,
    panels=PANELS,
    period_field="snapshot_date",
    snapshot_field="snapshot_date",
    source_note="security posture snapshots from the security gate",
)

_WEEK_RE = re.compile(r"^(\d{4})-W(\d{1,2})$", re.IGNORECASE)
_QUARTER_RE = re.compile(r"^(\d{4})-Q([1-4])$", re.IGNORECASE)
_MONTH_RE = re.compile(r"^(\d{4})-(\d{2})$")


def period_start(period: str | dt.date | dt.datetime) -> dt.date:
    """First day of a period label: ``2026-03-15``, ``2026-03``, ``2026-W12`` (ISO week) or ``2026-Q1``."""
    if isinstance(period, dt.datetime):
        return period.date()
    if isinstance(period, dt.date):
        return period
    text = str(period).strip()
    if match := _WEEK_RE.match(text):
        return dt.date.fromisocalendar(int(match.group(1)), int(match.group(2)), 1)
    if match := _QUARTER_RE.match(text):
        return dt.date(int(match.group(1)), (int(match.group(2)) - 1) * 3 + 1, 1)
    if match := _MONTH_RE.match(text):
        return dt.date(int(match.group(1)), int(match.group(2)), 1)
    try:
        return dt.date.fromisoformat(text[:10])
    except ValueError as exc:
        raise ValueError(f"Unrecognized period '{period}'") from exc


def _as_mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        return dict(dump(mode="python"))
    raise TypeError(f"Expected a mapping or a pydantic model, got {type(value).__name__}")


def _severity(value: Any) -> str:
    text = str(getattr(value, "value", value) or "").strip().lower()
    aliases = {"moderate": "medium", "informational": "info", "information": "info", "note": "info"}
    text = aliases.get(text, text)
    return text if text in SEVERITIES else "info"


def security_rows_from_snapshots(
    snapshots: Iterable[Any],
    *,
    team_of: Mapping[str, str] | Callable[[str], str | None] | None = None,
) -> list[dict[str, Any]]:
    """Turn posture snapshots into ``security_snapshots`` rows (five rows per snapshot, one per severity).

    Gate coverage and gate result repeat on each of the five rows, so averages over rows
    equal averages over snapshots. Outdated dependencies are counted per severity; any
    count without per-item detail goes to the ``info`` row.
    """
    rows: list[dict[str, Any]] = []
    for raw in snapshots:
        snap = _as_mapping(raw)
        project = str(snap.get("project") or UNASSIGNED)
        period = snap.get("period") or snap.get("snapshot_date")
        if period is None:
            raise ValueError(f"Snapshot of '{project}' has no period or snapshot_date")
        start = period_start(snap.get("snapshot_date") or period)
        team = snap.get("team")
        if not team and team_of is not None:
            team = team_of(project) if callable(team_of) else team_of.get(project)

        counts = {severity: 0 for severity in SEVERITIES}
        for key, value in (snap.get("severity_counts") or {}).items():
            counts[_severity(key)] += int(value or 0)

        outdated = {severity: 0 for severity in SEVERITIES}
        details = list(snap.get("outdated_dependencies") or [])
        for dep in details:
            outdated[_severity(_as_mapping(dep).get("severity"))] += 1
        declared = int(snap.get("outdated_dependencies_count") or 0)
        if declared > len(details):
            outdated["info"] += declared - len(details)

        coverage_pct = snap.get("gate_coverage_pct")
        coverage = None if coverage_pct is None else max(0.0, min(float(coverage_pct), 100.0)) / 100.0
        gate_passed = snap.get("gate_passed")
        for rank, severity in enumerate(SEVERITIES, start=1):
            rows.append(
                {
                    "project": project,
                    "team": str(team) if team else UNASSIGNED,
                    "period": str(period if not isinstance(period, dt.date) else period.isoformat()),
                    "snapshot_date": start.isoformat(),
                    "month": start.strftime("%Y-%m"),
                    "severity": severity,
                    "severity_rank": rank,
                    "open_findings": counts[severity],
                    "outdated_dependencies": outdated[severity],
                    "gate_coverage": coverage,
                    "gate_passed": None if gate_passed is None else int(bool(gate_passed)),
                }
            )
    return rows
